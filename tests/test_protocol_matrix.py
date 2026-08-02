"""Executable provider × protocol × capability acceptance matrix.

The fake provider emits standard text, reasoning-only text, tool calls, usage,
finish reasons, Retry-After and vendor extensions.  Every client protocol is
driven over a real proxy socket so the matrix verifies framing and envelopes,
not only pure conversion helpers.
"""
from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_free_router.cli import cmd_doctor, cmd_protocols
from open_free_router.protocol_matrix import PROTOCOLS, protocol_matrix
from open_free_router.proxy import run_proxy
from open_free_router.registry import Registry


CLIENTS = ("chat_completions", "responses", "anthropic_messages")


class _MatrixUpstream(BaseHTTPRequestHandler):
    mode = "text"

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if self.mode == "error":
            body = json.dumps({
                "error": {
                    "message": "rate limited", "type": "vendor_rate",
                    "code": "quota", "vendor_trace": "private-vendor-marker",
                },
                "account": "private-account-marker",
            }).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "11")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if request.get("stream"):
            if self.mode == "tool":
                delta = {"tool_calls": [{
                    "index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "read_file", "arguments": "{\"path\":\"a\"}"},
                    "vendor_extension": "private-vendor-marker",
                }]}
                finish = "tool_calls"
            elif self.mode == "reasoning":
                delta = {"content": None, "reasoning_content": "reasoned"}
                finish = "stop"
            else:
                delta = {"content": "hello", "vendor_extension": "private-vendor-marker"}
                finish = "stop"
            chunks = [
                {"choices": [{"delta": delta, "finish_reason": finish}],
                 "provider_extension": "private-vendor-marker"},
                {"choices": [], "usage": {
                    "prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5,
                }},
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
            return

        if self.mode == "tool":
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "read_file", "arguments": "{\"path\":\"a\"}"},
                "vendor_extension": "private-vendor-marker",
            }]}
            finish = "tool_calls"
        elif self.mode == "reasoning":
            message = {
                "role": "assistant", "content": None,
                "reasoning_content": "reasoned",
                "vendor_extension": "private-vendor-marker",
            }
            finish = "stop"
        else:
            message = {
                "role": "assistant", "content": "hello",
                "vendor_extension": "private-vendor-marker",
            }
            finish = "stop"
        body = json.dumps({
            "id": "chatcmpl-matrix", "object": "chat.completion",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            "provider_extension": "private-vendor-marker",
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start(mode):
    handler = type(f"Matrix{mode.title()}", (_MatrixUpstream,), {"mode": mode})
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    registry = Registry({
        "provider": {
            "prefix": "p", "api_key": "test-placeholder",
            "upstream_url": f"http://127.0.0.1:{upstream.server_address[1]}/v1",
            "models": [{"id": "model", "tool_calling": True, "reasoning": True}],
        }
    })
    proxy, _ = run_proxy(registry, host="127.0.0.1", port=0)
    return upstream, proxy


def _request(client, stream=False):
    if client == "chat_completions":
        return "/v1/chat/completions", {"model": "p/model", "messages": [], "stream": stream}
    if client == "responses":
        return "/v1/responses", {"model": "p/model", "input": "hi", "stream": stream}
    return "/v1/messages", {
        "model": "p/model", "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}], "stream": stream,
    }


def _round_trip(client, mode, stream=False):
    upstream, proxy = _start(mode)
    try:
        path, payload = _request(client, stream)
        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=5)
        conn.request("POST", path, json.dumps(payload), {"Content-Type": "application/json"})
        response = conn.getresponse()
        raw = response.read()
        return response.status, response.getheaders(), raw
    finally:
        proxy.shutdown()
        upstream.shutdown()


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("stream", (False, True))
@pytest.mark.parametrize("mode,expected", (("text", "hello"), ("reasoning", "reasoned")))
def test_text_reasoning_usage_and_finish_matrix(client, stream, mode, expected):
    status, _, raw = _round_trip(client, mode, stream)
    assert status == 200
    if stream:
        body = raw.decode()
        assert expected in body
        assert "private-vendor-marker" not in body if client != "chat_completions" else True
        if client == "responses":
            assert '"type":"response.completed"' in body
            assert '"input_tokens":3' in body
        elif client == "anthropic_messages":
            assert '"stop_reason":"end_turn"' in body
            assert '"input_tokens":3' in body
        else:
            assert '"finish_reason": "stop"' in body
            assert '"prompt_tokens": 3' in body
        return

    payload = json.loads(raw)
    serialized = json.dumps(payload)
    if client == "responses":
        assert payload["status"] == "completed"
        assert payload["output"][0]["content"][0]["text"] == expected
        assert payload["usage"]["input_tokens"] == 3
        assert "private-vendor-marker" not in serialized
    elif client == "anthropic_messages":
        assert payload["stop_reason"] == "end_turn"
        assert payload["content"][0]["text"] == expected
        assert payload["usage"]["input_tokens"] == 3
        assert "private-vendor-marker" not in serialized
    else:
        assert payload["choices"][0]["finish_reason"] == "stop"
        assert payload["choices"][0]["message"]["content"] == expected
        assert payload["usage"]["prompt_tokens"] == 3


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("stream", (False, True))
def test_tool_loop_matrix(client, stream):
    status, _, raw = _round_trip(client, "tool", stream)
    assert status == 200
    body = raw.decode()
    assert "read_file" in body and "call_1" in body
    if client == "responses":
        assert "function_call" in body
        assert "private-vendor-marker" not in body
    elif client == "anthropic_messages":
        assert "tool_use" in body
        assert "private-vendor-marker" not in body
        assert "tool_use" in body
    else:
        assert "tool_calls" in body


@pytest.mark.parametrize("client", CLIENTS)
def test_error_envelope_retry_after_and_vendor_redaction_matrix(client):
    status, headers, raw = _round_trip(client, "error")
    assert status == 429
    assert dict(headers)["Retry-After"] == "11"
    payload = json.loads(raw)
    assert "private-vendor-marker" not in json.dumps(payload)
    assert "private-account-marker" not in json.dumps(payload)
    assert payload["error"]["message"] == "rate limited"
    if client == "anthropic_messages":
        assert payload["type"] == "error"
        assert payload["error"]["type"] == "rate_limit_error"
    else:
        assert payload["error"]["type"] == "vendor_rate"
        assert payload["error"]["code"] == "quota"


def test_declared_matrix_covers_every_provider_and_protocol():
    registry = Registry({
        "alpha": {"upstream_url": "https://example.com/v1", "models": [{"id": "plain"}]},
        "beta": {"upstream_url": "https://example.org/v1", "api_key": "placeholder",
                 "models": [{"id": "coder", "tool_calling": True}]},
    })
    matrix = protocol_matrix(registry)
    assert matrix["row_count"] == 2 * len(PROTOCOLS)
    assert {(row["provider"], row["protocol"]) for row in matrix["rows"]} == {
        (provider, protocol["id"])
        for provider in ("alpha", "beta") for protocol in PROTOCOLS
    }
    assert matrix["notice_zh"].startswith("这是配置与协议适配矩阵")


def test_bundled_registry_is_an_11_by_3_issue_free_matrix():
    template = Path(__file__).parents[1] / "src/open_free_router/registry.default.yaml"
    matrix = protocol_matrix(Registry.load(template))
    assert matrix["provider_count"] == 11
    assert matrix["protocol_count"] == 3
    assert matrix["row_count"] == 33
    assert matrix["issues"] == []


def test_google_openai_compatibility_path_is_actionable_and_cli_json(tmp_path, monkeypatch, capsys):
    (tmp_path / "registry.yaml").write_text(
        "google-ai-studio:\n"
        "  upstream_url: https://generativelanguage.googleapis.com/v1beta\n"
        "  api_key: test-placeholder\n"
        "  models: [{id: gemini}]\n"
    )
    config = tmp_path / "config.yaml"
    config.write_text("registry: registry.yaml\n")
    monkeypatch.setenv("OPEN_FREE_ROUTER_CONFIG", str(config))
    cmd_protocols(SimpleNamespace(json=True))
    matrix = json.loads(capsys.readouterr().out)
    assert matrix["issues"][0]["code"] == "google_openai_compatibility_path_missing"
    assert matrix["issues"][0]["fix"].endswith("/v1beta/openai")

    with pytest.raises(SystemExit) as stopped:
        cmd_doctor(SimpleNamespace(json=True))
    assert stopped.value.code == 1
    report = json.loads(capsys.readouterr().out)
    issue = report["protocol_matrix"]["issues"][0]
    assert issue["code"] == "google_openai_compatibility_path_missing"
    assert issue["fix"].endswith("/v1beta/openai")
