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
    assert ofr["type"] == "openai"
    assert ofr["base_url"] == "http://127.0.0.1:8337/v1"
    assert ofr["api_key"] == "local-proxy-token"
    assert data["models"]["ofr-gq-gpt-oss"]["model"] == "gq/gpt-oss"
    assert data["models"]["ofr-gq-gpt-oss"]["provider"] == "open-free-router"
    assert data["models"]["ofr-gq-gpt-oss"]["max_output_size"] == 8192
    assert data["models"]["ofr-gq-gpt-oss"]["capabilities"] == ["tool_use"]
    assert data["models"]["ofr-gq-gpt-oss"]["display_name"] == "gpt-oss（来源：GroqCloud）"
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


# ── review-driven regressions ──

def test_parse_json5_preserves_urls_containing_double_slash():
    from open_free_router.sync import _parse_json5
    raw = '{\n  // comment\n  "provider": {"x": {"options": {"baseURL": "http://127.0.0.1:8337/v1"}}},\n}\n'
    data = _parse_json5(raw)
    assert data["provider"]["x"]["options"]["baseURL"] == "http://127.0.0.1:8337/v1"


def test_workbuddy_sync_refuses_to_wipe_unparseable_file(tmp_path):
    models = tmp_path / "models.json"
    models.write_text("[{broken json!!")
    with patch("open_free_router.sync.WORKBUDDY_MODELS", models):
        assert sync_workbuddy(_registry(), explicit=True) == []
    assert models.read_text() == "[{broken json!!"


def test_opencode_sync_refuses_to_wipe_unparseable_file(tmp_path):
    from open_free_router.sync import sync_opencode
    config = tmp_path / "opencode.jsonc"
    config.write_text("{definitely not json")
    with patch("open_free_router.sync.OPENCODE_CONFIG", config):
        assert sync_opencode(_registry()) == []
    assert config.read_text() == "{definitely not json"


def test_kimi_sync_keeps_single_quoted_user_default_and_ignores_table_keys(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("default_model = 'my-own'\n[providers.mine]\ndefault_model = \"ofr-inside-table\"\ntype = \"kimi\"\n")
    with patch("open_free_router.sync.KIMI_CONFIG", config):
        sync_kimi(_registry(), explicit=True)
    import tomllib
    data = tomllib.loads(config.read_text())
    assert data["default_model"] == "my-own"                       # user choice kept
    assert data["providers"]["mine"]["default_model"] == "ofr-inside-table"  # table key untouched


def test_kimi_sync_recovers_from_orphan_managed_block(tmp_path):
    from open_free_router.sync import KIMI_BLOCK_BEGIN
    config = tmp_path / "config.toml"
    config.write_text(f"# user\n{KIMI_BLOCK_BEGIN}\n[providers.open-free-router]\ntruncated")
    with patch("open_free_router.sync.KIMI_CONFIG", config):
        sync_kimi(_registry(), explicit=True)
    import tomllib
    text = config.read_text()
    tomllib.loads(text)  # must parse
    assert text.count("[providers.open-free-router]") == 1
    assert "# user" in text


def test_kimi_sync_adopts_unmanaged_router_tables_without_duplicates(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        'default_model = "open-free-router/old"\n'
        '[providers.mine]\n'
        'type = "openai"\n'
        '[providers.open-free-router]\n'
        'type = "openai"\n'
        'api_key = "stale-token"\n'
        'base_url = "http://127.0.0.1:8337/v1"\n'
        '[models."open-free-router/old"]\n'
        'provider = "open-free-router"\n'
        'model = "gq/old"\n'
        '[models.mine]\n'
        'provider = "mine"\n'
        'model = "kept"\n'
    )
    with patch("open_free_router.sync.KIMI_CONFIG", config):
        sync_kimi(_registry(), proxy_token="local-proxy-token", explicit=True)
    text = config.read_text()
    data = tomllib.loads(text)
    assert text.count("[providers.open-free-router]") == 1
    assert "stale-token" not in text
    assert "open-free-router/old" not in data["models"]
    assert data["providers"]["mine"]["type"] == "openai"
    assert data["models"]["mine"]["provider"] == "mine"
    assert data["providers"]["open-free-router"]["api_key"] == "local-proxy-token"
    assert data["default_model"] == "ofr-gq-gpt-oss"


def test_kimi_sync_can_include_only_probe_available_models(tmp_path):
    config = tmp_path / "config.toml"
    reg = Registry({
        "nvidia-nim": {
            "upstream_url": "https://integrate.api.nvidia.com/v1",
            "api_key": "upstream-secret-must-not-leak",
            "prefix": "nv",
            "models": [
                {"id": "glm-5.2", "name": "GLM-5.2", "tool_calling": True},
                {"id": "slow-one", "name": "Slow One"},
            ],
        }
    })
    with patch("open_free_router.sync.KIMI_CONFIG", config):
        changed = sync_kimi(
            reg,
            proxy_token="local-proxy-token",
            explicit=True,
            include_model_ids={"nvidia-nim/glm-5.2"},
        )
    data = tomllib.loads(config.read_text())
    assert changed == ["ofr-nv-glm-5-2"]
    assert "ofr-nv-slow-one" not in data["models"]
    assert data["models"]["ofr-nv-glm-5-2"]["display_name"] == "GLM-5.2（来源：NVIDIA NIM）"


def test_openclaw_sync_keeps_user_selected_router_model(tmp_path):
    config = tmp_path / "openclaw.json"
    config.write_text(json.dumps({
        "agents": {"defaults": {"model": {"primary": "open-free-router/gq/llama-8b-mini"}}},
        "models": {"providers": {}},
    }))
    with patch("open_free_router.sync.OPENCLAW_CONFIG", config):
        sync_openclaw(_registry(), explicit=True)
    data = json.loads(config.read_text())
    # user picked a *different* router model that still exists — keep it
    assert data["agents"]["defaults"]["model"]["primary"] == "open-free-router/gq/llama-8b-mini"


def test_openclaw_sync_repoints_dangling_router_model(tmp_path):
    config = tmp_path / "openclaw.json"
    config.write_text(json.dumps({
        "agents": {"defaults": {"model": {"primary": "open-free-router/gone/model"}}},
        "models": {"providers": {}},
    }))
    with patch("open_free_router.sync.OPENCLAW_CONFIG", config):
        sync_openclaw(_registry(), explicit=True)
    data = json.loads(config.read_text())
    assert data["agents"]["defaults"]["model"]["primary"] == "open-free-router/gq/gpt-oss"


def test_small_fast_model_does_not_match_mini_inside_gemini():
    from open_free_router.sync import _small_fast_model
    reg = Registry({
        "google": {
            "upstream_url": "https://example.com/v1",
            "prefix": "gai",
            "models": [{"id": "gemini-2.5-pro"}],
        }
    })
    assert _small_fast_model(reg, "fallback") == "fallback"


# ── sync_all integration ──

def test_sync_all_reaches_new_agents_explicitly(tmp_path):
    paths = {
        "CLAUDE_SETTINGS": tmp_path / "claude" / "settings.json",
        "KIMI_CONFIG": tmp_path / "kimi" / "config.toml",
        # Pinned too: KIMI_CONFIG's parent does not exist here, which is exactly
        # the condition that sends sync_kimi to the legacy path.
        "KIMI_LEGACY_CONFIG": tmp_path / "kimi-legacy" / "config.toml",
        "OPENCLAW_CONFIG": tmp_path / "openclaw" / "openclaw.json",
        "WORKBUDDY_MODELS": tmp_path / "wb" / "models.json",
        "BACKUP_DIR": tmp_path / "backup",
    }
    with (
        patch("open_free_router.sync.CLAUDE_SETTINGS", paths["CLAUDE_SETTINGS"]),
        patch("open_free_router.sync.KIMI_CONFIG", paths["KIMI_CONFIG"]),
        patch("open_free_router.sync.KIMI_LEGACY_CONFIG", paths["KIMI_LEGACY_CONFIG"]),
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


def test_kimi_sync_keeps_a_user_authored_table_that_points_at_the_router(tmp_path):
    """Aiming a personal alias at the local proxy is the user's call, not ours."""
    config = tmp_path / "config.toml"
    config.write_text(
        '[providers.open-free-router]\n'
        'type = "openai"\n'
        'api_key = "stale-token"\n'
        '[models."ofr-gq-retired"]\n'
        'provider = "open-free-router"\n'
        'model = "gq/retired"\n'
        '[models.my-favourite]\n'
        'provider = "open-free-router"\n'
        'model = "gq/gpt-oss"\n'
        'max_context_size = 4096\n'
    )
    with patch("open_free_router.sync.KIMI_CONFIG", config):
        sync_kimi(_registry(), proxy_token="local-proxy-token", explicit=True)
    data = tomllib.loads(config.read_text())
    # Ours by alias shape, so reclaimed rather than duplicated.
    assert "ofr-gq-retired" not in data["models"]
    assert "stale-token" not in config.read_text()
    # Theirs, even though it names us.
    assert data["models"]["my-favourite"]["model"] == "gq/gpt-oss"


def test_kimi_sync_falls_back_to_legacy_config_on_an_explicit_sync(tmp_path):
    """`sync --agent kimi` is explicit; it must not start a rival config."""
    legacy = tmp_path / "kimi" / "config.toml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("# user comment\n")
    modern = tmp_path / "kimi-code" / "config.toml"
    with patch("open_free_router.sync.KIMI_CONFIG", modern), \
         patch("open_free_router.sync.KIMI_LEGACY_CONFIG", legacy):
        sync_kimi(_registry(), proxy_token="local-proxy-token", explicit=True)
    assert not modern.exists()
    assert "open-free-router" in legacy.read_text()
    assert "# user comment" in legacy.read_text()


def test_kimi_sync_keeps_unsafe_default_providers_but_does_not_choose_them(tmp_path):
    config = tmp_path / "config.toml"
    reg = Registry({
        "groq": {
            "upstream_url": "https://api.groq.com/openai/v1",
            "api_key": "k", "prefix": "gq",
            "models": [{"id": "gpt-oss", "name": "GPT OSS", "tool_calling": True}],
        },
        "nvidia-nim": {
            "upstream_url": "https://integrate.api.nvidia.com/v1",
            "api_key": "k", "prefix": "nv",
            "models": [{"id": "nemotron", "name": "Nemotron", "tool_calling": True}],
        },
    })
    with patch("open_free_router.sync.KIMI_CONFIG", config):
        sync_kimi(reg, proxy_token="local-proxy-token", explicit=True)
    data = tomllib.loads(config.read_text())
    # Still selectable...
    assert "ofr-gq-gpt-oss" in data["models"]
    # ...but a working provider wins the default.
    assert data["default_model"] == "ofr-nv-nemotron"


def test_kimi_sync_still_defaults_to_an_unsafe_provider_when_it_is_all_there_is(tmp_path):
    """An imperfect default beats leaving the user with none."""
    config = tmp_path / "config.toml"
    reg = Registry({
        "groq": {
            "upstream_url": "https://api.groq.com/openai/v1",
            "api_key": "k", "prefix": "gq",
            "models": [{"id": "gpt-oss", "name": "GPT OSS", "tool_calling": True}],
        }
    })
    with patch("open_free_router.sync.KIMI_CONFIG", config):
        sync_kimi(reg, proxy_token="local-proxy-token", explicit=True)
    data = tomllib.loads(config.read_text())
    assert data["default_model"] == "ofr-gq-gpt-oss"


def test_demoted_provider_still_supplies_a_tool_calling_default(tmp_path):
    """Demotion must not cost the guarantee that the default can call tools.

    The shipped groq block lists a non-tool-calling model first, so a blind
    changes[0] fallback hands Kimi a model the registry says cannot call tools.
    """
    config = tmp_path / "config.toml"
    reg = Registry({
        "groq": {
            "upstream_url": "https://api.groq.com/openai/v1",
            "api_key": "k", "prefix": "gq",
            "models": [
                {"id": "llama-70b", "name": "Llama"},                      # no tools
                {"id": "gpt-oss", "name": "GPT OSS", "tool_calling": True},
            ],
        }
    })
    with patch("open_free_router.sync.KIMI_CONFIG", config):
        sync_kimi(reg, proxy_token="local-proxy-token", explicit=True)
    data = tomllib.loads(config.read_text())
    assert data["default_model"] == "ofr-gq-gpt-oss"


def test_unsafe_provider_models_survive_the_available_only_filter(tmp_path):
    """The Groq fix keeps the models; only the default choice avoids them."""
    config = tmp_path / "config.toml"
    reg = Registry({
        "groq": {
            "upstream_url": "https://api.groq.com/openai/v1",
            "api_key": "k", "prefix": "gq",
            "models": [{"id": "gpt-oss", "name": "GPT OSS", "tool_calling": True}],
        },
        "nvidia-nim": {
            "upstream_url": "https://integrate.api.nvidia.com/v1",
            "api_key": "k", "prefix": "nv",
            "models": [{"id": "nemotron", "name": "Nemotron", "tool_calling": True}],
        },
    })
    with patch("open_free_router.sync.KIMI_CONFIG", config):
        sync_kimi(
            reg, proxy_token="local-proxy-token", explicit=True,
            include_model_ids={"groq/gpt-oss", "nvidia-nim/nemotron"},
        )
    data = tomllib.loads(config.read_text())
    assert "ofr-gq-gpt-oss" in data["models"]
    assert data["default_model"] == "ofr-nv-nemotron"


# ── sync_all(exclude=...) ──

def test_sync_all_exclude_skips_agent_without_forcing_explicit_mode(tmp_path):
    """``exclude`` must drop an agent while leaving detect-only semantics intact.

    Passing ``agents=[...]`` would flip ``sync_all`` into explicit mode, which
    force-creates configs for clients that were never installed. ``exclude``
    must not have that side effect.
    """
    claude = tmp_path / "claude" / "settings.json"
    claude.parent.mkdir()
    claude.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}}))
    kimi = tmp_path / "kimi" / "config.toml"
    kimi.parent.mkdir()
    kimi.write_text("")
    openclaw = tmp_path / "openclaw" / "openclaw.json"  # never installed

    with (
        patch("open_free_router.sync.CLAUDE_SETTINGS", claude),
        patch("open_free_router.sync.KIMI_CONFIG", kimi),
        patch("open_free_router.sync.OPENCLAW_CONFIG", openclaw),
        patch("open_free_router.sync.BACKUP_DIR", tmp_path / "backup"),
    ):
        results = sync_all(
            _registry(),
            agents=["claude", "kimi", "openclaw"],
            exclude=["claude"],
            proxy_token="tok",
        )

    assert "claude" not in results          # excluded agent never ran
    assert results["kimi"]                  # siblings still synced
    # the excluded client keeps the user's own configuration untouched
    assert json.loads(claude.read_text())["env"]["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"


def test_sync_all_exclude_defaults_to_no_op():
    """Omitting ``exclude`` keeps every agent in the run."""
    with patch("open_free_router.sync.DEFAULT_AGENTS", ["kimi"]):
        for kwargs in ({}, {"exclude": None}, {"exclude": []}):
            with patch("open_free_router.sync.sync_kimi", return_value=["gq/gpt-oss"]) as fn:
                sync_all(_registry(), do_write=False, **kwargs)
            assert fn.called, kwargs
