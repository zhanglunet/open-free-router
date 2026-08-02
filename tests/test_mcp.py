"""MCP stdio server tests: JSON-RPC handshake, tool listing, tool calls."""
from __future__ import annotations

import io
import json

import yaml

from open_free_router.config import Config
from open_free_router.mcp_server import McpServer, TOOLS


def _server(tmp_path) -> McpServer:
    registry = tmp_path / "registry.yaml"
    registry.write_text(yaml.safe_dump({
        "groq": {
            "upstream_url": "https://user:password@api.groq.com/openai/v1?api_key=url-secret",
            "api_key": "upstream-secret",
            "prefix": "gq",
            "models": [
                {"id": "gpt-oss", "tool_calling": True},
                {"id": "chat-only"},
            ],
        }
    }))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "registry": str(registry),
        # unroutable ports so reachability probes fail fast without a server
        "proxy": {"host": "127.0.0.1", "port": 1},
        "ui": {"host": "127.0.0.1", "port": 1},
    }))
    return McpServer(Config(config_path))


def _call(server, method, params=None, msg_id=1):
    return server.handle({
        "jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {},
    })


def _tool_payload(response):
    assert "error" not in response, response
    result = response["result"]
    return json.loads(result["content"][0]["text"]), result.get("isError", False)


def test_initialize_handshake_and_version_negotiation(tmp_path):
    server = _server(tmp_path)
    response = _call(server, "initialize", {"protocolVersion": "2025-06-18"})
    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert response["result"]["serverInfo"]["name"] == "open-free-router"
    assert "tools" in response["result"]["capabilities"]
    # unknown client version falls back to the newest we support
    response = _call(server, "initialize", {"protocolVersion": "2099-01-01"})
    assert response["result"]["protocolVersion"] == "2025-06-18"


def test_notifications_get_no_response(tmp_path):
    server = _server(tmp_path)
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_matches_declared_tools(tmp_path):
    server = _server(tmp_path)
    response = _call(server, "tools/list")
    names = [t["name"] for t in response["result"]["tools"]]
    assert names == [t["name"] for t in TOOLS if t["name"] not in {"refresh_models", "sync_clients"}]
    assert {"list_models", "list_providers", "get_status", "chat", "explain_route",
            "get_resilience", "check_quota", "get_metrics"} <= set(names)
    assert "refresh_models" not in names and "sync_clients" not in names
    for tool in response["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"
    for name in ("explain_route", "get_resilience", "check_quota", "get_metrics"):
        tool = next(item for item in response["result"]["tools"] if item["name"] == name)
        assert tool["annotations"]["readOnlyHint"] is True


def test_write_tools_require_explicit_config_opt_in(tmp_path):
    server = _server(tmp_path)
    response = _call(server, "tools/call", {
        "name": "refresh_models", "arguments": {},
    })
    assert response["error"]["code"] == -32602
    server.cfg.mcp_allow_write_tools = True
    names = [tool["name"] for tool in _call(server, "tools/list")["result"]["tools"]]
    assert "refresh_models" in names and "sync_clients" in names


def test_list_models_tool_with_filters(tmp_path):
    server = _server(tmp_path)
    payload, is_error = _tool_payload(_call(server, "tools/call", {
        "name": "list_models", "arguments": {},
    }))
    assert not is_error
    assert payload["count"] == 2
    assert payload["models"][0]["id"] == "gq/gpt-oss"

    payload, _ = _tool_payload(_call(server, "tools/call", {
        "name": "list_models", "arguments": {"tool_calling_only": True},
    }))
    assert payload["count"] == 1
    assert payload["models"][0]["tool_calling"] is True


def test_list_providers_never_leaks_keys(tmp_path):
    server = _server(tmp_path)
    response = _call(server, "tools/call", {"name": "list_providers", "arguments": {}})
    text = response["result"]["content"][0]["text"]
    assert "upstream-secret" not in text
    assert "url-secret" not in text and "password" not in text
    payload = json.loads(text)
    assert payload["providers"][0]["has_key"] is True
    assert payload["providers"][0]["upstream_url"] == "https://api.groq.com/openai/v1"


def test_get_status_reports_unreachable_proxy(tmp_path):
    server = _server(tmp_path)
    payload, is_error = _tool_payload(_call(server, "tools/call", {
        "name": "get_status", "arguments": {},
    }))
    assert not is_error
    assert payload["providers"] == 1
    assert payload["models"] == 2
    assert payload["proxy_reachable"] is False


def test_chat_tool_fails_cleanly_without_serve(tmp_path):
    server = _server(tmp_path)
    response = _call(server, "tools/call", {
        "name": "chat", "arguments": {"model": "gq/gpt-oss", "prompt": "hi"},
    })
    result = response["result"]
    assert result["isError"] is True
    assert "proxy request failed" in result["content"][0]["text"]


def test_explain_route_is_offline_and_reports_scoring_boundary(tmp_path):
    server = _server(tmp_path)
    payload, is_error = _tool_payload(_call(server, "tools/call", {
        "name": "explain_route", "arguments": {"model": "auto/coding"},
    }))
    assert not is_error
    assert payload["selected"] == "gq/gpt-oss"
    assert payload["runtime_signals_included"] is False
    assert payload["strategy"] == "priority"


def test_read_only_runtime_tools_filter_and_never_return_keys(tmp_path):
    server = _server(tmp_path)

    def proxy_json(path):
        if path == "/api/resilience":
            return {
                "providers": {"groq": {"state": "closed"}, "other": {"state": "open"}},
                "credentials": {
                    "groq:slot-0": {
                        "state": "ready", "reason": "",
                        "quota": {"classification": "available", "requests": {
                            "limit": 100, "remaining": 50, "reset_at": 200,
                        }},
                    },
                    "other:slot-0": {"state": "terminal", "reason": "credential_invalid"},
                },
                "models": {"groq/model": {"reason": "model_unavailable"}},
                "persistence": {"enabled": True, "healthy": True, "error": ""},
            }
        if path.startswith("/api/metrics"):
            return {
                "enabled": True, "period_days": 7,
                "overall": {"requests": 3, "success_rate": 0.666667},
                "groups": [], "quota_estimates": [],
            }
        raise AssertionError(path)

    server._proxy_json = proxy_json
    resilience, is_error = _tool_payload(_call(server, "tools/call", {
        "name": "get_resilience", "arguments": {"provider": "groq"},
    }))
    assert not is_error
    assert set(resilience["providers"]) == {"groq"}
    assert set(resilience["credentials"]) == {"groq:slot-0"}

    quota_response = _call(server, "tools/call", {
        "name": "check_quota", "arguments": {"provider": "groq"},
    })
    quota, is_error = _tool_payload(quota_response)
    assert not is_error and quota["runtime"]["available"] is True
    assert quota["runtime"]["credential_slots"][0]["slot"] == "slot-0"
    assert quota["runtime"]["credential_slots"][0]["quota"]["requests"]["remaining"] == 50

    metrics, is_error = _tool_payload(_call(server, "tools/call", {
        "name": "get_metrics", "arguments": {"days": 7},
    }))
    assert not is_error and metrics["overall"]["requests"] == 3
    combined = json.dumps({"resilience": resilience, "quota": quota, "metrics": metrics})
    assert "upstream-secret" not in combined
    assert "authorization" not in combined.lower()
    assert "prompt" not in combined.lower()


def test_unknown_tool_and_method_errors(tmp_path):
    server = _server(tmp_path)
    response = _call(server, "tools/call", {"name": "nope", "arguments": {}})
    assert response["error"]["code"] == -32602
    response = _call(server, "bogus/method")
    assert response["error"]["code"] == -32601


def test_stdio_loop_rejects_non_object_messages(tmp_path):
    server = _server(tmp_path)
    stdin = io.StringIO('[1,2,3]\n"just a string"\n')
    stdout = io.StringIO()
    server.serve_stdio(stdin=stdin, stdout=stdout)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len(lines) == 2
    for response in lines:
        assert response["error"]["code"] == -32600


def test_probe_error_scrubs_credential_shaped_strings():
    from open_free_router.probe import _scrub
    fake_credential = "sk-" + "abcdef123456"
    assert fake_credential not in _scrub(f"invalid key: {fake_credential} provided")
    assert "[redacted-credential]" in _scrub("Bearer nvapi-secret-value-123")
    assert _scrub("quota exhausted") == "quota exhausted"


def test_stdio_loop_speaks_line_delimited_jsonrpc(tmp_path):
    server = _server(tmp_path)
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        + "not-json\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
    )
    stdout = io.StringIO()
    server.serve_stdio(stdin=stdin, stdout=stdout)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len(lines) == 3  # init response, parse error, tools/list response
    assert lines[0]["id"] == 1
    assert lines[1]["error"]["code"] == -32700
    assert lines[2]["id"] == 2
    assert lines[2]["result"]["tools"]
