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
    assert data["providers"][0]["availability"] == "available"
