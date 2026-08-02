import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from open_free_router.analytics import AnalyticsStore
from open_free_router.proxy import run_proxy
from open_free_router.registry import Registry


class UsageHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if request.get("stream"):
            chunks = [
                b'data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}\n\n',
                b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
                b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n',
                b'data: [DONE]\n\n',
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(chunk)
            self.close_connection = True
            return
        body = json.dumps({
            "id": "result", "object": "chat.completion", "model": request["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 5},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start():
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UsageHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    registry = Registry({
        "provider": {
            "prefix": "p", "api_key": "placeholder",
            "upstream_url": f"http://127.0.0.1:{upstream.server_address[1]}/v1",
            "free_tier": {
                "type": "recurring_quota", "limit": 100, "unit": "requests/day",
                "reset_period": "daily", "evidence_url": "https://example.com/free",
                "verified_at": "2026-08-01T00:00:00Z",
                "expires_at": "2099-08-01T00:00:00Z",
            },
            "models": [{"id": "model"}],
        }
    })
    return upstream, registry


def _request(proxy, method, path, body=None, authorized=False):
    headers = {"Content-Type": "application/json"}
    if authorized:
        headers["Authorization"] = "Bearer local-token"
    conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=5)
    conn.request(method, path, json.dumps(body) if body is not None else None, headers)
    response = conn.getresponse()
    payload = response.read()
    return response.status, response.getheader("Content-Type"), payload


def test_proxy_captures_buffered_and_streaming_usage_and_protects_metrics(tmp_path):
    upstream, registry = _start()
    analytics = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    proxy, _ = run_proxy(
        registry, host="127.0.0.1", port=0, auth_token="local-token", analytics=analytics,
    )
    try:
        status, _, _ = _request(
            proxy, "POST", "/v1/chat/completions",
            {"model": "p/model", "messages": []}, authorized=True,
        )
        assert status == 200
        status, _, _ = _request(
            proxy, "POST", "/v1/chat/completions",
            {"model": "p/model", "messages": [], "stream": True}, authorized=True,
        )
        assert status == 200
        for path, payload in (
            ("/v1/responses", {"model": "p/model", "input": "hi"}),
            ("/v1/messages", {"model": "p/model", "max_tokens": 8,
                              "messages": [{"role": "user", "content": "hi"}]}),
            ("/v1/responses", {"model": "p/model", "input": "hi", "stream": True}),
            ("/v1/messages", {"model": "p/model", "max_tokens": 8, "stream": True,
                              "messages": [{"role": "user", "content": "hi"}]}),
        ):
            status, _, _ = _request(proxy, "POST", path, payload, authorized=True)
            assert status == 200

        status, _, _ = _request(proxy, "GET", "/api/metrics")
        assert status == 401
        status, _, body = _request(proxy, "GET", "/api/metrics?days=7", authorized=True)
        summary = json.loads(body)
        assert status == 200
        assert summary["overall"]["requests"] == 6
        assert summary["overall"]["input_tokens"] == 54
        assert summary["overall"]["output_tokens"] == 24
        assert summary["quota_estimates"][0]["observed_usage"] == 6
        assert summary["quota_estimates"][0]["estimated_ratio"] == 0.06
        assert summary["quota_estimates"][0]["estimated"] is True

        status, content_type, body = _request(
            proxy, "GET", "/api/metrics/export?format=csv", authorized=True,
        )
        assert status == 200 and content_type.startswith("text/csv")
        assert b"timestamp,provider,model" in body
        assert b"placeholder" not in body
    finally:
        proxy.shutdown()
        upstream.shutdown()
        analytics.close()
