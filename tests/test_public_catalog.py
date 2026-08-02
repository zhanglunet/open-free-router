from __future__ import annotations

import json

from open_free_router.public_catalog import build_public_catalog, write_public_catalog
from open_free_router.registry import Registry


def test_public_catalog_redacts_keys_and_scores_features(tmp_path):
    reg = Registry({
        "provider": {
            "upstream_url": "https://api.example/v1",
            "api_key": "must-never-appear",
            "prefix": "p",
            "models": [{
                "id": "model:free", "context_window": 131072, "max_tokens": 8192,
                "reasoning": True, "tool_calling": True,
            }],
        }
    })
    catalog = build_public_catalog(reg, {
        "as_of": "2026-08-01T00:00:00Z",
        "providers": {"provider": {"availability": "available", "latency_ms": 100}},
    })
    output = tmp_path / "catalog.json"
    write_public_catalog(catalog, output)
    text = output.read_text()
    assert "must-never-appear" not in text
    data = json.loads(text)
    model = data["providers"][0]["models"][0]
    assert model["codex_alias"] == "ofr-p-model-free"
    assert model["capability_score"] > 50
    assert model["description_zh"]
    assert model["recommended_for_zh"] == "复杂编码、Agent 工具链和多步分析"
    assert model["speed_tier_zh"] == "快"
    assert data["providers"][0]["availability"] == "available"
    assert data["schema_version"] == 2
    assert model["free_tier"]["status"] == "unknown"
    assert model["free_availability"] == "unknown"


def test_public_catalog_rejects_credential_like_values(tmp_path):
    output = tmp_path / "catalog.json"
    for value in ("Bearer hidden-placeholder", "sk-secretplaceholder123"):
        try:
            write_public_catalog({"schema_version": 2, "note": value}, output)
        except ValueError as exc:
            assert "credential" in str(exc).lower()
        else:
            raise AssertionError("credential-like catalog value was written")
