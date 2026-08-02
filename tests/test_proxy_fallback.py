import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from open_free_router.proxy import run_proxy
from open_free_router.registry import Registry


class _FailureHandler(BaseHTTPRequestHandler):
    hits = 0
    status = 503

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        type(self).hits += 1
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"error": {"message": "temporary upstream failure"}}).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _SuccessHandler(BaseHTTPRequestHandler):
    hits = 0

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        type(self).hits += 1
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length))
        if request.get("stream"):
            chunks = [
                b'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}\n\n',
                b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
                b"data: [DONE]\n\n",
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(chunk)
                self.wfile.flush()
            self.close_connection = True
            return

        body = json.dumps({
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": request["model"],
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _fresh_handler(base):
    return type(f"Fresh{base.__name__}", (base,), {"hits": 0})


def _start(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _proxy(first_port, second_port):
    registry = Registry({
        "failing": {
            "prefix": "a",
            "upstream_url": f"http://127.0.0.1:{first_port}/v1",
            "api_key": "test-first-placeholder",
            "models": [{"id": "model", "tool_calling": True}],
        },
        "healthy": {
            "prefix": "b",
            "upstream_url": f"http://127.0.0.1:{second_port}/v1",
            "api_key": "test-second-placeholder",
            "models": [{"id": "model", "tool_calling": True}],
        },
    })
    return run_proxy(registry, host="127.0.0.1", port=0)


@pytest.mark.parametrize(
    ("path", "payload", "expected_type"),
    [
        ("/v1/chat/completions", {"model": "auto", "messages": []}, "chat.completion"),
        ("/v1/responses", {"model": "auto", "input": "hi"}, "response"),
        (
            "/v1/messages",
            {"model": "auto", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
            "message",
        ),
    ],
)
def test_buffered_protocols_share_pre_byte_fallback(path, payload, expected_type):
    failure_handler = _fresh_handler(_FailureHandler)
    success_handler = _fresh_handler(_SuccessHandler)
    failing = _start(failure_handler)
    healthy = _start(success_handler)
    proxy, _ = _proxy(failing.server_address[1], healthy.server_address[1])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=5)
        conn.request("POST", path, json.dumps(payload), {"Content-Type": "application/json"})
        response = conn.getresponse()
        body = json.loads(response.read())
        assert response.status == 200
        type_key = "type" if path == "/v1/messages" else "object"
        assert body[type_key] == expected_type
        assert response.getheader("X-OFR-Provider") == "healthy"
        assert response.getheader("X-OFR-Fallback-Attempts") == "1"
        assert response.getheader("X-OFR-Request-Id").startswith("ofr_")
        assert failure_handler.hits == 1
        assert success_handler.hits == 1
    finally:
        proxy.shutdown()
        failing.shutdown()
        healthy.shutdown()


@pytest.mark.parametrize(
    ("path", "payload", "expected"),
    [
        ("/v1/chat/completions", {"model": "auto", "messages": [], "stream": True}, '"content":"ok"'),
        ("/v1/responses", {"model": "auto", "input": "hi", "stream": True}, "response.completed"),
        ("/v1/messages", {"model": "auto", "max_tokens": 8,
                           "messages": [{"role": "user", "content": "hi"}],
                           "stream": True}, "message_stop"),
    ],
)
def test_streaming_protocols_fall_back_only_before_downstream_headers(path, payload, expected):
    failure_handler = _fresh_handler(_FailureHandler)
    success_handler = _fresh_handler(_SuccessHandler)
    failing = _start(failure_handler)
    healthy = _start(success_handler)
    proxy, _ = _proxy(failing.server_address[1], healthy.server_address[1])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=5)
        conn.request("POST", path, json.dumps(payload), {"Content-Type": "application/json"})
        response = conn.getresponse()
        body = response.read().decode()
        assert response.status == 200
        assert expected in body
        assert response.getheader("X-OFR-Provider") == "healthy"
        assert response.getheader("X-OFR-Fallback-Attempts") == "1"
        assert failure_handler.hits == 1
        assert success_handler.hits == 1
    finally:
        proxy.shutdown()
        failing.shutdown()
        healthy.shutdown()


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/v1/chat/completions", {"model": "auto", "messages": [], "stream": True}),
        ("/v1/responses", {"model": "auto", "input": "hi", "stream": True}),
        ("/v1/messages", {"model": "auto", "max_tokens": 8,
                           "messages": [{"role": "user", "content": "hi"}],
                           "stream": True}),
    ],
)
def test_successful_stream_is_never_replayed_after_headers(path, payload):
    partial_handler = _fresh_handler(_SuccessHandler)
    unused_handler = _fresh_handler(_SuccessHandler)
    partial = _start(partial_handler)
    unused = _start(unused_handler)
    proxy, _ = _proxy(partial.server_address[1], unused.server_address[1])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=5)
        conn.request("POST", path, json.dumps(payload), {"Content-Type": "application/json"})
        response = conn.getresponse()
        response.read()
        assert response.status == 200
        assert response.getheader("X-OFR-Provider") == "failing"
        assert partial_handler.hits == 1
        assert unused_handler.hits == 0
    finally:
        proxy.shutdown()
        partial.shutdown()
        unused.shutdown()
