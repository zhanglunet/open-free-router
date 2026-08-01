"""Adapter tests for the newer sync targets: Claude Code, Kimi CLI,
OpenClaw, and WorkBuddy.

Each adapter must (1) write the client's documented schema, (2) never leak
upstream provider keys, (3) preserve unrelated user configuration, and
(4) stay a no-op when the client isn't installed and wasn't explicitly
requested.
"""
from __future__ import annotations

import json
import tomllib
from unittest.mock import patch

from open_free_router.registry import Registry
from open_free_router.sync import (
    sync_all,
    sync_claude,
    sync_kimi,
    sync_openclaw,
    sync_workbuddy,
)


def _registry():
    return Registry({
        "groq": {
            "upstream_url": "https://api.groq.com/openai/v1",
            "api_key": "upstream-secret-must-not-leak",
            "prefix": "gq",
            "models": [
                {"id": "gpt-oss", "upstream_id": "openai/gpt-oss", "tool_calling": True},
                {"id": "llama-8b-mini", "context_window": 8192},
            ],
        }
    })


# ── Claude Code ──

def test_claude_sync_writes_env_and_preserves_other_settings(tmp_path):
    settings = tmp_path / "claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls:*)"]},
        "env": {"CUSTOM_VAR": "keep-me"},
    }))
    with patch("open_free_router.sync.CLAUDE_SETTINGS", settings):
        changed = sync_claude(_registry(), proxy_token="local-proxy-token")
    data = json.loads(settings.read_text())
    assert changed == ["gq/gpt-oss"]
    env = data["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8337"  # no /v1 suffix
    assert env["ANTHROPIC_AUTH_TOKEN"] == "local-proxy-token"
    assert env["ANTHROPIC_MODEL"] == "gq/gpt-oss"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "gq/llama-8b-mini"
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "gq/llama-8b-mini"
    assert env["CUSTOM_VAR"] == "keep-me"
    assert data["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert "upstream-secret-must-not-leak" not in settings.read_text()


def test_claude_sync_skips_when_not_installed(tmp_path):
    settings = tmp_path / "claude" / "settings.json"  # parent dir doesn't exist
    with patch("open_free_router.sync.CLAUDE_SETTINGS", settings):
        assert sync_claude(_registry()) == []
        assert not settings.exists()
        # explicit request creates it
        assert sync_claude(_registry(), explicit=True) == ["gq/gpt-oss"]
        assert settings.exists()


def test_claude_sync_refuses_to_clobber_broken_settings(tmp_path):
    settings = tmp_path / "claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{not json")
    with patch("open_free_router.sync.CLAUDE_SETTINGS", settings):
        assert sync_claude(_registry()) == []
    assert settings.read_text() == "{not json"


def test_claude_sync_honors_requested_model(tmp_path):
    settings = tmp_path / "settings.json"
    with patch("open_free_router.sync.CLAUDE_SETTINGS", settings):
        changed = sync_claude(_registry(), claude_model="llama-8b-mini", explicit=True)
    assert changed == ["gq/llama-8b-mini"]


# ── Kimi CLI ──

def test_kimi_sync_writes_valid_toml_managed_block(tmp_path):
    config = tmp_path / "kimi" / "config.toml"
    config.parent.mkdir()
    config.write_text('# user comment\n[providers.mine]\ntype = "kimi"\nbase_url = "https://api.kimi.com/v1"\n')
    with patch("open_free_router.sync.KIMI_CONFIG", config):
        changed = sync_kimi(_registry(), proxy_token="local-proxy-token")
    text = config.read_text()
    data = tomllib.loads(text)
    assert "# user comment" in text                     # user content preserved
    assert data["providers"]["mine"]["type"] == "kimi"  # user provider intact
    ofr = data["providers"]["open-free-router"]
    assert ofr["type"] == "openai_legacy"
    assert ofr["base_url"] == "http://127.0.0.1:8337/v1"
    assert ofr["api_key"] == "local-proxy-token"
    assert data["models"]["ofr-gq-gpt-oss"]["model"] == "gq/gpt-oss"
    assert data["models"]["ofr-gq-gpt-oss"]["provider"] == "open-free-router"
    assert data["default_model"] == "ofr-gq-gpt-oss"    # tool-calling model chosen
    assert changed == ["ofr-gq-gpt-oss", "ofr-gq-llama-8b-mini"]
    assert "upstream-secret-must-not-leak" not in text


def test_kimi_sync_is_idempotent_and_replaces_managed_block(tmp_path):
    config = tmp_path / "config.toml"
    with patch("open_free_router.sync.KIMI_CONFIG", config):
        sync_kimi(_registry(), explicit=True)
        first = config.read_text()
        sync_kimi(_registry(), explicit=True)
        second = config.read_text()
    assert first == second
    assert second.count("[providers.open-free-router]") == 1


def test_kimi_sync_keeps_user_default_model(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('default_model = "my-own-model"\n')
    with patch("open_free_router.sync.KIMI_CONFIG", config):
        sync_kimi(_registry(), explicit=True)
    data = tomllib.loads(config.read_text())
    assert data["default_model"] == "my-own-model"


# ── OpenClaw ──

def test_openclaw_sync_writes_providers_and_primary_model(tmp_path):
    config = tmp_path / "openclaw.json"
    config.write_text(json.dumps({
        "models": {"providers": {
            "anthropic": {"baseUrl": "https://api.anthropic.com", "models": []},
            "stale-local": {"baseUrl": "http://127.0.0.1:8337/v1", "models": []},
        }},
    }))
    with patch("open_free_router.sync.OPENCLAW_CONFIG", config):
        changed = sync_openclaw(_registry(), proxy_token="local-proxy-token")
    data = json.loads(config.read_text())
    providers = data["models"]["providers"]
    assert "stale-local" not in providers            # dedup of local-proxy entries
    assert "anthropic" in providers                  # unrelated provider preserved
    ofr = providers["open-free-router"]
    assert ofr["api"] == "openai-completions"
    assert ofr["apiKey"] == "local-proxy-token"
    assert ofr["models"][0]["id"] == "gq/gpt-oss"
    assert ofr["models"][0]["cost"]["input"] == 0
    assert data["agents"]["defaults"]["model"]["primary"] == "open-free-router/gq/gpt-oss"
    assert changed == ["gq/gpt-oss", "gq/llama-8b-mini"]
    assert "upstream-secret-must-not-leak" not in config.read_text()


def test_openclaw_sync_keeps_user_primary_model(tmp_path):
    config = tmp_path / "openclaw.json"
    config.write_text(json.dumps({
        "agents": {"defaults": {"model": {"primary": "anthropic/claude-sonnet-4-6"}}},
        "models": {"providers": {}},
    }))
    with patch("open_free_router.sync.OPENCLAW_CONFIG", config):
        sync_openclaw(_registry(), explicit=True)
    data = json.loads(config.read_text())
    assert data["agents"]["defaults"]["model"]["primary"] == "anthropic/claude-sonnet-4-6"


def test_openclaw_sync_parses_json5_comments(tmp_path):
    config = tmp_path / "openclaw.json"
    config.write_text('{\n  // my comment\n  "models": {"providers": {}},\n}\n')
    with patch("open_free_router.sync.OPENCLAW_CONFIG", config):
        changed = sync_openclaw(_registry(), explicit=True)
    assert changed
    assert json.loads(config.read_text())["models"]["providers"]["open-free-router"]


def test_openclaw_sync_skips_when_no_config_file(tmp_path):
    config = tmp_path / "openclaw.json"
    with patch("open_free_router.sync.OPENCLAW_CONFIG", config):
        assert sync_openclaw(_registry()) == []
        assert not config.exists()


# ── WorkBuddy ──

def test_workbuddy_sync_writes_model_array_and_dedups(tmp_path):
    models = tmp_path / "wb" / "models.json"
    models.parent.mkdir()
    models.write_text(json.dumps([
        {"id": "user-model", "url": "https://example.com/v1", "apiKey": "user-key"},
        {"id": "old-local", "url": "http://localhost:8337/v1", "apiKey": "x"},
    ]))
    with patch("open_free_router.sync.WORKBUDDY_MODELS", models):
        changed = sync_workbuddy(_registry(), proxy_token="local-proxy-token")
    data = json.loads(models.read_text())
    ids = [e["id"] for e in data]
    assert "user-model" in ids                        # user entry preserved
    assert "old-local" not in ids                     # stale local entry removed
    entry = next(e for e in data if e["id"] == "gq/gpt-oss")
    assert entry["vendor"] == "Custom"
    assert entry["url"] == "http://127.0.0.1:8337/v1"
    assert entry["apiKey"] == "local-proxy-token"
    assert entry["supportsToolCall"] is True
    assert entry["useCustomProtocol"] is False
    mini = next(e for e in data if e["id"] == "gq/llama-8b-mini")
    assert mini["supportsToolCall"] is False
    assert changed == ["gq/gpt-oss", "gq/llama-8b-mini"]
    assert "upstream-secret-must-not-leak" not in models.read_text()


def test_workbuddy_sync_skips_when_not_installed(tmp_path):
    models = tmp_path / "wb" / "models.json"
    with patch("open_free_router.sync.WORKBUDDY_MODELS", models):
        assert sync_workbuddy(_registry()) == []
        assert not models.exists()


# ── sync_all integration ──

def test_sync_all_reaches_new_agents_explicitly(tmp_path):
    paths = {
        "CLAUDE_SETTINGS": tmp_path / "claude" / "settings.json",
        "KIMI_CONFIG": tmp_path / "kimi" / "config.toml",
        "OPENCLAW_CONFIG": tmp_path / "openclaw" / "openclaw.json",
        "WORKBUDDY_MODELS": tmp_path / "wb" / "models.json",
        "BACKUP_DIR": tmp_path / "backup",
    }
    with (
        patch("open_free_router.sync.CLAUDE_SETTINGS", paths["CLAUDE_SETTINGS"]),
        patch("open_free_router.sync.KIMI_CONFIG", paths["KIMI_CONFIG"]),
        patch("open_free_router.sync.OPENCLAW_CONFIG", paths["OPENCLAW_CONFIG"]),
        patch("open_free_router.sync.WORKBUDDY_MODELS", paths["WORKBUDDY_MODELS"]),
        patch("open_free_router.sync.BACKUP_DIR", paths["BACKUP_DIR"]),
    ):
        results = sync_all(
            _registry(),
            agents=["claude", "kimi", "openclaw", "workbuddy"],
            proxy_token="tok",
        )
    assert set(results) == {"claude", "kimi", "openclaw", "workbuddy"}
    for agent, changes in results.items():
        assert changes, f"{agent} produced no changes"
        assert not any(str(c).startswith("ERROR") for c in changes), results
    for key in ("CLAUDE_SETTINGS", "KIMI_CONFIG", "OPENCLAW_CONFIG", "WORKBUDDY_MODELS"):
        assert paths[key].exists()
