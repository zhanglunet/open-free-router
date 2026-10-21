"""PRD-116/T1: the OpenCode preset contract matches the researched format.

A later task may generate files. It may not grow the allowlist or start
claiming that the v1 reasoning boolean survives migration.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs" / "presets" / "opencode-contract.yaml"
DOC_PATH = ROOT / "docs" / "plans" / "2026-10-21-opencode-config.md"

PRESET_TOP_LEVEL = ("$schema", "model", "small_model", "provider")
FORBIDDEN_TOP_LEVEL = (
    "providers",
    "permissions",
    "mcp",
    "plugin",
    "plugins",
    "server",
    "tools",
    "agent",
    "instructions",
)
MODEL_SELECTION = re.compile(r"^[^/#]+/[^#]+$")
SECRET_LIKE = re.compile(r"sk-[A-Za-z0-9]{20,}")


def _load() -> dict:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    assert isinstance(data, dict)
    return data


def test_source_and_the_v1_shape_we_must_keep_writing():
    doc = _load()
    assert doc["trend_issue"] == 579
    assert doc["source"]["license"] == "MIT"
    assert doc["source"]["project"] == "anomalyco/opencode"
    assert doc["source"]["commit"] == "5fa426f68d1dbf3c8aaa01557e357719f0577438"
    assert doc["filenames"] == ["opencode.json", "opencode.jsonc"]
    assert doc["filename_order"] == ["opencode.json", "opencode.jsonc"]
    assert doc["keys"]["v1_provider_object"] == "provider"
    assert doc["keys"]["v2_provider_object"] == "providers"
    assert doc["parser"]["missing_env_becomes"] == "empty_string"
    assert doc["model_selection"]["pattern"] == r"^[^/#]+/[^#]+$"
    assert doc["model_selection"]["map_key_may_contain_slash"] is False
    assert doc["model_selection"]["upstream_id_may_contain_slash"] is True


def test_migration_drops_the_reasoning_boolean():
    migration = _load()["migration"]
    assert migration["npm_at_ai_sdk_openai_compatible"] == "aisdk:@ai-sdk/openai-compatible"
    assert migration["options_baseURL"] == "settings.baseURL"
    assert migration["options_apiKey"] == "settings.apiKey"
    assert migration["model_id"] == "modelID"
    assert migration["reasoning_boolean"] == "dropped"
    assert migration["tool_call"] == "capabilities.tools"


def test_sample_preset_stays_inside_the_allowlist():
    doc = _load()
    assert tuple(doc["preset_top_level"]) == PRESET_TOP_LEVEL
    assert tuple(doc["forbidden_top_level"]) == FORBIDDEN_TOP_LEVEL
    assert set(doc["preset_top_level"]).isdisjoint(doc["forbidden_top_level"])

    sample = doc["sample"]
    assert set(sample).issubset(set(PRESET_TOP_LEVEL))
    assert MODEL_SELECTION.match(sample["model"])
    assert MODEL_SELECTION.match(sample["small_model"])

    provider_name, provider = next(iter(sample["provider"].items()))
    assert sample["model"] == f"{provider_name}/placeholder-model"
    assert sample["small_model"].startswith(f"{provider_name}/")
    assert set(provider) == set(doc["preset_provider_keys"])
    assert set(provider["options"]) == set(doc["preset_option_keys"])
    assert provider["options"]["apiKey"] == "placeholder-local-proxy-token"
    assert provider["options"]["baseURL"] == "http://127.0.0.1:8337/v1"
    assert provider["npm"] == "@ai-sdk/openai-compatible"

    for key, model in provider["models"].items():
        assert "/" not in key
        assert set(model).issubset(set(doc["preset_model_keys"]))
        assert set(model["limit"]).issubset(set(doc["preset_limit_keys"]))
        assert isinstance(model["reasoning"], bool)
        assert isinstance(model["tool_call"], bool)

    migrated = doc["migrated_sample"]["providers"][provider_name]
    assert migrated["package"] == "aisdk:@ai-sdk/openai-compatible"
    assert migrated["settings"]["apiKey"] == "placeholder-local-proxy-token"
    for model in migrated["models"].values():
        assert "reasoning" not in model
        assert "tools" in model["capabilities"]

    # The contract may name the {file:} syntax. The shipped sample must not use it.
    sample_text = yaml.safe_dump(sample)
    assert "{file:" not in sample_text
    assert SECRET_LIKE.search(sample_text) is None
    assert SECRET_LIKE.search(yaml.safe_dump(doc["migrated_sample"])) is None


def test_design_note_records_the_commit_and_the_dropped_field():
    text = DOC_PATH.read_text()
    assert "zhanglunet/trend#579" in text
    assert "5fa426f68d1dbf3c8aaa01557e357719f0577438" in text
    assert "MIT" in text
    assert "reasoning" in text
    assert "不迁移" in text
    assert "placeholder-local-proxy-token" in text
