"""Anthropic Messages API compatibility tests (Claude Code support).

Unit tests cover request/response conversion and the streaming adapter;
endpoint tests drive a real proxy + fake Chat Completions upstream over
real sockets, mirroring tests/test_responses.py.
"""
from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from open_free_router.anthropic import (
    AnthropicConversionError,
    AnthropicStreamAdapter,
    chat_to_messages,
    count_tokens_estimate,
    messages_to_chat,
)
from open_free_router.proxy import run_proxy
from open_free_router.registry import Registry


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
                    {"choices": [{"delta": {"content": "Let me check."}}]},
                    {"choices": [{"delta": {"tool_calls": [{
                        "index": 0, "id": "call_1", "type": "function",
                        "function": {"name": "read_file", "arguments": "{\"path\":"},
                    }]}}]},
                    {"choices": [{"delta": {"tool_calls": [{
                        "index": 0, "function": {"arguments": "\"a.txt\"}"},
                    }]}, "finish_reason": "tool_calls"}]},
                    {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 7}},
                ]
            else:
                chunks = [
                    {"choices": [{"delta": {"content": "hel"}}]},
                    {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
                    {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
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


def _messages_request(stream=False):
    return {
        "model": "t/coder",
        "max_tokens": 512,
        "system": "Be concise.",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ],
        "tools": [{
            "name": "read_file",
            "description": "read a file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }],
        "stream": stream,
    }


# ── conversion unit tests ──

def test_messages_to_chat_maps_system_tools_and_text():
    chat = messages_to_chat(_messages_request())
    assert chat["messages"][0] == {"role": "system", "content": "Be concise."}
    assert chat["messages"][1] == {"role": "user", "content": "hi"}
    assert chat["max_tokens"] == 512
    assert chat["tools"][0]["function"]["name"] == "read_file"
    assert chat["tools"][0]["function"]["parameters"]["properties"]["path"]["type"] == "string"


def test_messages_to_chat_preserves_tool_loop():
    request = _messages_request()
    request["messages"].extend([
        {"role": "assistant", "content": [
            {"type": "text", "text": "checking"},
            {"type": "tool_use", "id": "toolu_7", "name": "read_file", "input": {"path": "a.txt"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_7", "content": "file body"},
        ]},
    ])
    chat = messages_to_chat(request)
    assistant = chat["messages"][-2]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["id"] == "toolu_7"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"path": "a.txt"}
    assert chat["messages"][-1] == {"role": "tool", "tool_call_id": "toolu_7", "content": "file body"}


def test_messages_to_chat_tool_choice_and_stops():
    request = _messages_request()
    request["tool_choice"] = {"type": "any", "disable_parallel_tool_use": True}
    request["stop_sequences"] = ["END"]
    chat = messages_to_chat(request)
    assert chat["tool_choice"] == "required"
    assert chat["parallel_tool_calls"] is False
    assert chat["stop"] == ["END"]


def test_messages_to_chat_rejects_image_input():
    request = _messages_request()
    request["messages"][0]["content"] = [
        {"type": "image", "source": {"type": "base64", "data": "AA=="}}
    ]
    with pytest.raises(AnthropicConversionError):
        messages_to_chat(request)


def test_chat_to_messages_text_and_stop_reason():
    result = chat_to_messages({
        "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }, "t/coder")
    assert result["type"] == "message"
    assert result["content"] == [{"type": "text", "text": "hello"}]
    assert result["stop_reason"] == "end_turn"
    assert result["usage"] == {"input_tokens": 5, "output_tokens": 2}


def test_chat_to_messages_tool_use_block():
    result = chat_to_messages({
        "choices": [{"message": {"content": None, "tool_calls": [{
            "id": "call_9",
            "function": {"name": "read_file", "arguments": "{\"path\": \"a.txt\"}"},
        }]}, "finish_reason": "tool_calls"}],
    }, "t/coder")
    block = result["content"][0]
    assert block["type"] == "tool_use"
    assert block["id"] == "call_9"
    assert block["input"] == {"path": "a.txt"}
    assert result["stop_reason"] == "tool_use"


def test_chat_to_messages_reasoning_content_fallback():
    result = chat_to_messages({
        "choices": [{"message": {"content": None, "reasoning_content": "thought output"},
                     "finish_reason": "stop"}],
    }, "t/coder")
    assert result["content"] == [{"type": "text", "text": "thought output"}]


def test_count_tokens_estimate_scales_with_input():
    small = count_tokens_estimate({"messages": [{"role": "user", "content": "hi"}]})
    large = count_tokens_estimate({"messages": [{"role": "user", "content": "x" * 4000}]})
    assert small >= 1
    assert large >= 900


def test_stream_adapter_emits_ordered_block_events():
    adapter = AnthropicStreamAdapter("t/coder")
    events = adapter.start()
    events += adapter.feed({"choices": [{"delta": {"content": "hel"}}]})
    events += adapter.feed({"choices": [{"delta": {"tool_calls": [{
        "index": 0, "id": "call_1",
        "function": {"name": "read_file", "arguments": "{\"path\":\"a\"}"},
    }]}, "finish_reason": "tool_calls"}]})
    events += adapter.feed({"choices": [], "usage": {"completion_tokens": 6}})
    events += adapter.finish()
    kinds = [e["type"] for e in events]
    assert kinds == [
        "message_start",
        "content_block_start", "content_block_delta",   # text block
        "content_block_stop",                            # text closed before tool
        "content_block_start", "content_block_delta",   # tool_use block
        "content_block_stop",
        "message_delta", "message_stop",
    ]
    tool_start = events[4]
    assert tool_start["content_block"]["type"] == "tool_use"
    assert tool_start["content_block"]["name"] == "read_file"
    message_delta = events[-2]
    assert message_delta["delta"]["stop_reason"] == "tool_use"
    assert message_delta["usage"]["output_tokens"] == 6


# ── endpoint tests ──

def test_messages_endpoint_round_trip_and_upstream_key_private():
    upstream, handler = _start_upstream()
    (proxy, _), _ = _start_proxy(upstream.server_address[1])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=5)
        conn.request("POST", "/v1/messages", body=json.dumps(_messages_request()), headers={
            "Content-Type": "application/json", "x-api-key": "proxy-secret",
        })
        response = conn.getresponse()
        data = json.loads(response.read())
        assert response.status == 200
        assert data["type"] == "message"
        assert data["content"][0]["text"] == "hello"
        assert data["model"] == "t/coder"
        assert handler.last_request["model"] == "vendor/coder"
        assert handler.last_request["messages"][0]["role"] == "system"
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_messages_endpoint_accepts_bearer_auth_too():
    upstream, _ = _start_upstream()
    (proxy, _), _ = _start_proxy(upstream.server_address[1])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=5)
        conn.request("POST", "/v1/messages", body=json.dumps(_messages_request()), headers={
            "Content-Type": "application/json", "Authorization": "Bearer proxy-secret",
        })
        assert conn.getresponse().status == 200
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_messages_endpoint_rejects_bad_token_with_anthropic_error():
    upstream, _ = _start_upstream()
    (proxy, _), _ = _start_proxy(upstream.server_address[1])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=5)
        conn.request("POST", "/v1/messages", body=json.dumps(_messages_request()), headers={
            "Content-Type": "application/json", "x-api-key": "wrong",
        })
        response = conn.getresponse()
        data = json.loads(response.read())
        assert response.status == 401
        assert data["type"] == "error"
        assert data["error"]["type"] == "authentication_error"
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_messages_endpoint_unknown_model_is_anthropic_not_found():
    upstream, _ = _start_upstream()
    (proxy, _), _ = _start_proxy(upstream.server_address[1])
    try:
        request = _messages_request()
        request["model"] = "claude-nonexistent"
        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=5)
        conn.request("POST", "/v1/messages", body=json.dumps(request), headers={
            "Content-Type": "application/json", "x-api-key": "proxy-secret",
        })
        response = conn.getresponse()
        data = json.loads(response.read())
        assert response.status == 404
        assert data["error"]["type"] == "not_found_error"
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_streaming_messages_emits_anthropic_events_without_done():
    upstream, _ = _start_upstream()
    (proxy, _), _ = _start_proxy(upstream.server_address[1])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=5)
        conn.request("POST", "/v1/messages", body=json.dumps(_messages_request(stream=True)), headers={
            "Content-Type": "application/json", "x-api-key": "proxy-secret",
        })
        response = conn.getresponse()
        body = response.read().decode()
        assert response.status == 200
        assert "event: message_start" in body
        assert '"type":"text_delta","text":"hel"' in body
        assert "event: message_delta" in body
        assert "event: message_stop" in body
        assert "[DONE]" not in body
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_streaming_messages_tool_use_events():
    upstream, _ = _start_upstream(tool_call=True)
    (proxy, _), _ = _start_proxy(upstream.server_address[1])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=5)
        conn.request("POST", "/v1/messages", body=json.dumps(_messages_request(stream=True)), headers={
            "Content-Type": "application/json", "x-api-key": "proxy-secret",
        })
        response = conn.getresponse()
        body = response.read().decode()
        assert '"type":"tool_use"' in body
        assert '"name":"read_file"' in body
        assert '"type":"input_json_delta"' in body
        assert '"stop_reason":"tool_use"' in body
    finally:
        proxy.shutdown()
        upstream.shutdown()


def test_count_tokens_endpoint_returns_estimate():
    upstream, _ = _start_upstream()
    (proxy, _), _ = _start_proxy(upstream.server_address[1])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=5)
        conn.request("POST", "/v1/messages/count_tokens", body=json.dumps(_messages_request()), headers={
            "Content-Type": "application/json", "x-api-key": "proxy-secret",
        })
        response = conn.getresponse()
        data = json.loads(response.read())
        assert response.status == 200
        assert data["input_tokens"] >= 1
    finally:
        proxy.shutdown()
        upstream.shutdown()
