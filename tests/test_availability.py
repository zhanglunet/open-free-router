"""PRD-014/T2 tests: read-only availability MCP tools (T1 schema).

Covers tool 1 (list_available_models) and tool 3 (get_live_status):
T1 input/output shape, availability from the same probe snapshot the
dashboard uses, fail-closed behaviour with no/stale evidence, virtual-model
membership via the real RoutePlanner, MODEL_NOT_FOUND, and strict
read-only-ness (no registry/probe/config writes).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import yaml

from open_free_router.availability import (
    error_rate_limited,
    error_unauthorized,
)
from open_free_router.config import Config
from open_free_router.mcp_server import McpServer


def _server(tmp_path, with_probe=True) -> McpServer:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    registry = tmp_path / "registry.yaml"
    registry.write_text(yaml.safe_dump({
        "groq": {
            "upstream_url": "https://api.groq.com/openai/v1",
            "api_key_env": "GROQ_API_KEY",
            "prefix": "gq",
            "models": [
                {"id": "gpt-oss", "tool_calling": True},
                {"id": "chat-only"},
            ],
        },
        "openrouter": {
            "upstream_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "prefix": "or",
            "models": [{"id": "deepseek-chat"}],
        },
    }))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "registry": str(registry),
        "data_dir": str(data_dir),
        "proxy": {"host": "127.0.0.1", "port": 1},
        "ui": {"host": "127.0.0.1", "port": 1},
        "routing": {"aliases": {"free-smart": {"candidates": ["or/deepseek-chat"]}}},
    }))
    if with_probe:
        now = datetime.now(timezone.utc)
        fresh = now.isoformat()
        stale = (now - timedelta(hours=2)).isoformat()
        (data_dir / "probe-results.json").write_text(json.dumps({
            "running": False,
            "started_at": fresh,
            "finished_at": fresh,
            "total": 3,
            "done": 3,
            "results": {
                # fresh success -> available
                "groq/gpt-oss": {
                    "provider": "groq", "model": "gpt-oss",
                    "display_id": "gq/gpt-oss", "ok": True,
                    "status": "http_200", "latency_ms": 812,
                    "error": "", "checked_at": fresh,
                },
                # fresh failure -> not available
                "groq/chat-only": {
                    "provider": "groq", "model": "chat-only",
                    "display_id": "gq/chat-only", "ok": False,
                    "status": "http_429", "latency_ms": 120,
                    "error": "rate limited", "checked_at": fresh,
                },
                # stale success -> not available (evidence aged out)
                "openrouter/deepseek-chat": {
                    "provider": "openrouter", "model": "deepseek-chat",
                    "display_id": "or/deepseek-chat", "ok": True,
                    "status": "http_200", "latency_ms": 1500,
                    "error": "", "checked_at": stale,
                },
            },
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


def _by_id(models):
    return {m["model_id"]: m for m in models}


def test_list_available_models_t1_shape_and_availability(tmp_path):
    server = _server(tmp_path)
    payload, is_error = _tool_payload(_call(server, "tools/call", {
        "name": "list_available_models", "arguments": {},
    }))
    assert not is_error
    # T1 required top-level keys
    assert set(payload) >= {"models", "probed_at"}
    assert len(payload["models"]) == 3
    for item in payload["models"]:
        # T1 required item keys
        assert set(item) >= {"model_id", "provider", "available"}
    # probed_at is the real batch time from the fixture, timezone-aware
    assert payload["probed_at"] is not None
    assert datetime.fromisoformat(payload["probed_at"]).tzinfo is not None

    models = _by_id(payload["models"])
    assert models["gq/gpt-oss"]["provider"] == "groq"
    assert models["gq/gpt-oss"]["available"] is True      # fresh ok
    assert models["gq/chat-only"]["available"] is False   # fresh failure
    assert models["or/deepseek-chat"]["available"] is False  # stale evidence

    # provider filter (registry provider name, per T1)
    payload, _ = _tool_payload(_call(server, "tools/call", {
        "name": "list_available_models", "arguments": {"provider": "groq"},
    }))
    assert {m["model_id"] for m in payload["models"]} == {"gq/gpt-oss", "gq/chat-only"}

    # unknown provider -> empty list, not an error (T1 defines no error here)
    payload, is_error = _tool_payload(_call(server, "tools/call", {
        "name": "list_available_models", "arguments": {"provider": "nope"},
    }))
    assert not is_error and payload["models"] == []


def test_list_available_models_virtual_model_membership(tmp_path):
    server = _server(tmp_path)
    payload, _ = _tool_payload(_call(server, "tools/call", {
        "name": "list_available_models", "arguments": {},
    }))
    models = _by_id(payload["models"])
    # custom alias wins over the builtin catch-alls
    assert models["or/deepseek-chat"]["virtual_model"] == "free-smart"
    # builtin fallback: every registered model is reachable via "auto"
    assert models["gq/gpt-oss"]["virtual_model"] == "auto"


def test_list_available_models_without_probe_snapshot_is_fail_closed(tmp_path):
    server = _server(tmp_path, with_probe=False)
    payload, is_error = _tool_payload(_call(server, "tools/call", {
        "name": "list_available_models", "arguments": {},
    }))
    assert not is_error
    # never probed: probed_at is null (never "now"), nothing is available
    assert payload["probed_at"] is None
    assert all(m["available"] is False for m in payload["models"])


def test_get_live_status_t1_shape(tmp_path):
    server = _server(tmp_path)
    payload, is_error = _tool_payload(_call(server, "tools/call", {
        "name": "get_live_status", "arguments": {},
    }))
    assert not is_error
    assert set(payload) >= {"statuses"}
    assert len(payload["statuses"]) == 3
    for item in payload["statuses"]:
        assert set(item) >= {"model_id", "last_probe_at", "latency_ms", "error_rate"}
        assert 0.0 <= item["error_rate"] <= 1.0
        if item["last_probe_at"] is not None:
            assert datetime.fromisoformat(item["last_probe_at"]).tzinfo is not None

    by_id = {s["model_id"]: s for s in payload["statuses"]}
    ok = by_id["gq/gpt-oss"]
    assert ok["latency_ms"] == 812 and ok["error_rate"] == 0.0
    failed = by_id["gq/chat-only"]
    assert failed["latency_ms"] == 120 and failed["error_rate"] == 1.0
    stale = by_id["or/deepseek-chat"]
    assert stale["error_rate"] == 0.0  # last probe succeeded, even though aged out


def test_get_live_status_model_id_filter_and_forms(tmp_path):
    server = _server(tmp_path)
    for form in ("gq/gpt-oss", "gpt-oss"):  # canonical and bare ID resolve the same
        payload, is_error = _tool_payload(_call(server, "tools/call", {
            "name": "get_live_status", "arguments": {"model_id": form},
        }))
        assert not is_error
        assert [s["model_id"] for s in payload["statuses"]] == ["gq/gpt-oss"]


def test_get_live_status_unknown_model_id(tmp_path):
    server = _server(tmp_path)
    response = _call(server, "tools/call", {
        "name": "get_live_status", "arguments": {"model_id": "no/such-model"},
    })
    result = response["result"]
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    # exact T1 error shape
    assert payload == {"error": "MODEL_NOT_FOUND", "model_id": "no/such-model"}


def test_get_live_status_without_probe_snapshot(tmp_path):
    server = _server(tmp_path, with_probe=False)
    payload, is_error = _tool_payload(_call(server, "tools/call", {
        "name": "get_live_status", "arguments": {},
    }))
    assert not is_error
    for item in payload["statuses"]:
        assert item["last_probe_at"] is None
        assert item["latency_ms"] is None
        assert item["error_rate"] == 1.0  # no evidence: fail-closed


def test_availability_tools_are_strictly_read_only(tmp_path):
    server = _server(tmp_path)
    data_dir = tmp_path / "data"
    registry_path = tmp_path / "registry.yaml"
    before = {
        "registry": registry_path.read_bytes(),
        "probe": (data_dir / "probe-results.json").read_bytes(),
        "files": sorted(p.name for p in data_dir.iterdir()),
    }
    for name, arguments in (
        ("list_available_models", {}),
        ("list_available_models", {"provider": "groq"}),
        ("get_live_status", {}),
        ("get_live_status", {"model_id": "gq/gpt-oss"}),
        ("get_live_status", {"model_id": "no/such-model"}),
    ):
        _call(server, "tools/call", {"name": name, "arguments": arguments})
    assert registry_path.read_bytes() == before["registry"]
    assert (data_dir / "probe-results.json").read_bytes() == before["probe"]
    assert sorted(p.name for p in data_dir.iterdir()) == before["files"]


def test_new_tools_listed_as_read_only(tmp_path):
    server = _server(tmp_path)
    tools = {t["name"]: t for t in _call(server, "tools/list")["result"]["tools"]}
    for name in ("list_available_models", "get_live_status"):
        assert name in tools
        assert tools[name]["annotations"]["readOnlyHint"] is True
        assert tools[name]["inputSchema"]["type"] == "object"
        assert tools[name]["inputSchema"]["additionalProperties"] is False


def test_t4_error_placeholders_match_t1_shapes():
    # Auth / rate limiting land in T4; the shapes are pinned now.
    assert error_unauthorized() == {"error": "UNAUTHORIZED"}
    assert error_rate_limited() == {"error": "RATE_LIMITED", "retry_after_s": 60}
    assert error_rate_limited(30)["retry_after_s"] == 30
