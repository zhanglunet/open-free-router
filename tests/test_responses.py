"""Responses API compatibility and inference proxy authentication tests."""
from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from open_free_router.proxy import rebuild_proxy_index, run_proxy
from open_free_router.registry import ModelInfo, Registry
from open_free_router.responses import (
    ResponsesConversionError,
    ResponsesStreamAdapter,
    chat_to_response,
    responses_to_chat,
)


class _ChatUpstream(BaseHTTPRequestHandler):
    last_request = None
    stream_tool_call = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).last_request = json.loads(raw)
        if type(self).last_request.get("stream"):
            if type(self).stream_tool_call:
                chunks = [
                    {"choices": [{"delta": {"tool_calls": [{
                        "index": 0, "id": "call_1", "type": "function",
                        "function": {"name": "exec_command", "arguments": "{\"cmd\":"},
                    }]}}]},
                    {"choices": [{"delta": {"tool_calls": [{
                        "index": 0, "function": {"arguments": "\"pwd\"}"},
                    }]}, "finish_reason": "tool_calls"}]},
                ]
            else:
                chunks = [
                    {"choices": [{"delta": {"content": "hel"}}]},
                    {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
                    {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
                ]
            body = b"".join(
                b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks
            ) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps({
            "id": "chat_1",
            "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_upstream(tool_call=False):
    handler = type("ChatUpstream", (_ChatUpstream,), {"stream_tool_call": tool_call})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, handler


def _start_proxy(upstream_port, token="proxy-secret"):
    reg = Registry({
        "test": {
            "upstream_url": f"http://127.0.0.1:{upstream_port}/v1",
            "api_key": "upstream-secret",
            "prefix": "t",
            "models": [{"id": "coder", "upstream_id": "vendor/coder", "tool_calling": True}],
        }
    })
    return run_proxy(reg, port=0, auth_token=token), reg


def _responses_request(stream=False):
    return {
        "model": "t/coder",
        "instructions": "Be concise.",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "tools": [{
            "type": "function", "name": "exec_command", "description": "run",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
            "strict": False,
        }],
        "stream": stream,
    }


def test_responses_translation_preserves_tool_loop():
    request = _responses_request()
    request["input"].extend([
        {"type": "function_call", "call_id": "call_7", "name": "exec_command", "arguments": "{\"cmd\":\"pwd\"}"},
        {"type": "function_call_output", "call_id": "call_7", "output": "/tmp"},
    ])
    chat = responses_to_chat(request)
    assert chat["messages"][0] == {"role": "system", "content": "Be concise."}
    assert chat["messages"][-2]["tool_calls"][0]["id"] == "call_7"
    assert chat["messages"][-1] == {"role": "tool", "tool_call_id": "call_7", "content": "/tmp"}
    assert chat["tools"][0]["function"]["name"] == "exec_command"


def test_buffered_gemini_thought_signature_is_replayed():
    signature = "encrypted-buffered-signature"
    response = chat_to_response({
        "choices": [{"message": {"role": "assistant", "tool_calls": [{
            "id": "gemini_call_buffered",
            "type": "function",
            "function": {"name": "exec_command", "arguments": '{"cmd":"pwd"}'},
            "extra_content": {"google": {"thought_signature": signature}},
        }]}}],
    }, "gai/gemini-3.6-flash")
    request = _responses_request()
    request["model"] = "gai/gemini-3.6-flash"
    request["input"] = [
        response["output"][0],
        {
            "type": "function_call_output",
            "call_id": "gemini_call_buffered",
            "output": "/tmp",
        },
    ]

    chat = responses_to_chat(request)
    assistant = next(message for message in chat["messages"] if message["role"] == "assistant")
    replayed = assistant["tool_calls"][0]
    assert replayed["extra_content"]["google"]["thought_signature"] == signature


def test_streaming_gemini_thought_signature_is_replayed():
    signature = "encrypted-streaming-signature"
    adapter = ResponsesStreamAdapter("gai/gemini-3.6-flash")
    adapter.feed({"choices": [{"delta": {"tool_calls": [{
        "index": 0,
        "id": "gemini_call_streaming",
        "type": "function",
        "function": {"name": "exec_command", "arguments": '{"cmd":"pwd"}'},
        "extra_content": {"google": {"thought_signature": signature}},
    }]}}]})
    adapter.finish()
    request = _responses_request()
    request["model"] = "gai/gemini-3.6-flash"
    request["input"] = [{
        "type": "function_call",
        "call_id": "gemini_call_streaming",
        "name": "exec_command",
        "arguments": '{"cmd":"pwd"}',
    }]

    chat = responses_to_chat(request)
    assistant = next(message for message in chat["messages"] if message["role"] == "assistant")
    replayed = assistant["tool_calls"][0]
    assert replayed["extra_content"]["google"]["thought_signature"] == signature


def test_responses_translation_rejects_image_input():
    request = _responses_request()
    request["input"][0]["content"] = [{"type": "input_image", "image_url": "data:image/png;base64,AA=="}]
    with pytest.raises(ResponsesConversionError):
        responses_to_chat(request)


def test_proxy_requires_local_token_for_inference():
    upstream, _ = _start_upstream()
    (proxy, _), _ = _start_proxy(upstream.server_address[1])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=5)
        conn.request("POST", "/v1/responses", body=json.dumps(_responses_request()),
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        assert response.status == 401
        response.read()
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_buffered_responses_round_trip_and_upstream_key_is_private():
    upstream, handler = _start_upstream()
    (proxy, _), _ = _start_proxy(upstream.server_address[1])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=5)
        conn.request("POST", "/v1/responses", body=json.dumps(_responses_request()), headers={
            "Content-Type": "application/json", "Authorization": "Bearer proxy-secret",
        })
        response = conn.getresponse()
        data = json.loads(response.read())
        assert response.status == 200
        assert data["object"] == "response"
        assert data["output"][0]["content"][0]["text"] == "hello"
        assert handler.last_request["model"] == "vendor/coder"
        assert handler.last_request["messages"][0]["role"] == "system"
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_streaming_responses_emits_text_and_completed_events():
    upstream, _ = _start_upstream()
    (proxy, _), _ = _start_proxy(upstream.server_address[1])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=5)
        conn.request("POST", "/v1/responses", body=json.dumps(_responses_request(stream=True)), headers={
            "Content-Type": "application/json", "Authorization": "Bearer proxy-secret",
        })
        response = conn.getresponse()
        body = response.read().decode()
        assert response.status == 200
        assert '"type":"response.output_text.delta"' in body
        assert '"delta":"hel"' in body
        assert '"text":"hello"' in body
        assert '"type":"response.completed"' in body
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_streaming_responses_emits_function_call_events():
    upstream, _ = _start_upstream(tool_call=True)
    (proxy, _), _ = _start_proxy(upstream.server_address[1])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=5)
        conn.request("POST", "/v1/responses", body=json.dumps(_responses_request(stream=True)), headers={
            "Content-Type": "application/json", "Authorization": "Bearer proxy-secret",
        })
        response = conn.getresponse()
        body = response.read().decode()
        assert '"type":"response.function_call_arguments.delta"' in body
        assert '"type":"function_call"' in body
        assert '"name":"exec_command"' in body
        assert "exec_commandexec_command" not in body
        assert '\\"cmd\\":\\"pwd\\"' in body
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_rebuild_proxy_index_updates_the_active_handler():
    upstream, _ = _start_upstream()
    (proxy, handler), reg = _start_proxy(upstream.server_address[1], token="")
    try:
        reg.update_models("test", [ModelInfo(id="new", tool_calling=True)])
        rebuild_proxy_index()
        assert handler._model_index.get("new") == "test"
        assert "coder" not in handler._model_index
    finally:
        proxy.shutdown()
        upstream.shutdown()
