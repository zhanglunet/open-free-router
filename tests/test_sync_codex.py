from __future__ import annotations

import json
from unittest.mock import patch

import yaml

from open_free_router.registry import Registry
from open_free_router.sync import sync_codex, sync_hermes, sync_opencode


def _registry():
    return Registry({
        "groq": {
            "upstream_url": "https://api.groq.com/openai/v1",
            "api_key": "upstream-secret-must-not-leak",
            "prefix": "gq",
            "models": [{"id": "gpt-oss", "upstream_id": "openai/gpt-oss", "tool_calling": True}],
        }
    })


def test_codex_profile_uses_responses_and_command_auth(tmp_path):
    profile = tmp_path / "open-free-router.config.toml"
    catalog = tmp_path / "open-free-router.models.json"
    with (
        patch("open_free_router.sync.CODEX_PROFILE", profile),
        patch("open_free_router.sync.CODEX_MODEL_CATALOG", catalog),
    ):
        changed = sync_codex(_registry(), proxy_url="http://127.0.0.1:8337/v1")
    text = profile.read_text()
    assert changed == ["ofr-gq-gpt-oss"]
    assert 'model = "ofr-gq-gpt-oss"' in text
    assert f'model_catalog_json = "{catalog}"' in text
    assert "include_apps_instructions = false" in text
    assert "[features]\napps = false\nplugins = false\nmemories = false" in text
    assert (
        "[mcp_servers.openaiDeveloperDocs]\n"
        'url = "https://developers.openai.com/mcp"\n'
        "enabled = false"
    ) in text
    assert "[skills]\ninclude_instructions = false" in text
    assert 'wire_api = "responses"' in text
    assert "[model_providers.open_free_router.auth]" in text
    assert "upstream-secret-must-not-leak" not in text

    models = json.loads(catalog.read_text())["models"]
    assert [item["slug"] for item in models] == ["ofr-gq-gpt-oss"]
    assert models[0]["visibility"] == "list"
    assert models[0]["context_window"] == 131072
    assert models[0]["shell_type"] == "shell_command"
    assert models[0]["use_responses_lite"] is False
    assert "upstream-secret-must-not-leak" not in catalog.read_text()


def test_codex_catalog_lists_unverified_models_without_shell_tools(tmp_path):
    profile = tmp_path / "open-free-router.config.toml"
    catalog = tmp_path / "open-free-router.models.json"
    reg = _registry()
    reg.providers["groq"].models.append(
        type(reg.providers["groq"].models[0])(id="chat-only", name="Chat Only")
    )
    with (
        patch("open_free_router.sync.CODEX_PROFILE", profile),
        patch("open_free_router.sync.CODEX_MODEL_CATALOG", catalog),
    ):
        sync_codex(reg)

    models = {item["slug"]: item for item in json.loads(catalog.read_text())["models"]}
    assert models["ofr-gq-chat-only"]["visibility"] == "list"
    assert models["ofr-gq-chat-only"]["shell_type"] == "disabled"
    assert models["ofr-gq-chat-only"]["apply_patch_tool_type"] is None


def test_codex_alias_is_accepted_as_requested_model(tmp_path):
    profile = tmp_path / "open-free-router.config.toml"
    catalog = tmp_path / "open-free-router.models.json"
    with (
        patch("open_free_router.sync.CODEX_PROFILE", profile),
        patch("open_free_router.sync.CODEX_MODEL_CATALOG", catalog),
    ):
        changed = sync_codex(_registry(), codex_model="ofr-gq-gpt-oss")
    assert changed == ["ofr-gq-gpt-oss"]


def test_opencode_receives_proxy_token_not_upstream_key(tmp_path):
    config = tmp_path / "opencode.jsonc"
    config.write_text(json.dumps({"provider": {}}))
    with patch("open_free_router.sync.OPENCODE_CONFIG", config):
        sync_opencode(_registry(), proxy_token="local-proxy-token")
    text = config.read_text()
    assert "local-proxy-token" in text
    assert "upstream-secret-must-not-leak" not in text


def test_existing_hermes_entry_is_scrubbed_to_proxy_token(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"custom_providers": [{
        "name": "open-free-router",
        "base_url": "http://127.0.0.1:8337/v1",
        "api_key": "upstream-secret-must-not-leak",
    }]}))
    with patch("open_free_router.sync.HERMES_CONFIG", config):
        changed = sync_hermes(_registry(), proxy_token="local-proxy-token")
    data = yaml.safe_load(config.read_text())
    assert changed == ["open-free-router"]
    assert data["custom_providers"][0]["api_key"] == "local-proxy-token"
