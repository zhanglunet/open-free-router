from __future__ import annotations

import json

from open_free_router.discovery import discover, normalize_models_dev, save_discovery
from open_free_router.registry import Registry


def _catalog():
    return {
        "registered": {
            "name": "Already Here",
            "api": "https://registered.example/v1",
            "models": {"free": {"cost": {"input": 0, "output": 0}}},
        },
        "candidate": {
            "name": "Candidate AI",
            "api": "https://candidate.example/v1",
            "doc": "https://candidate.example/docs",
            "models": {
                "free-tool": {
                    "name": "Free Tool",
                    "cost": {"input": 0, "output": 0},
                    "limit": {"context": 131072, "output": 8192},
                    "reasoning": True,
                    "tool_call": True,
                    "modalities": {"input": ["text", "image"]},
                },
                "paid": {"cost": {"input": 1, "output": 2}},
            },
        },
        "unsafe": {
            "name": "Unsafe",
            "api": "http://insecure.example/v1",
            "models": {"free": {"cost": {"input": 0, "output": 0}}},
        },
        "credentialed-query": {
            "name": "Credentialed Query",
            "api": "https://query.example/v1?api_key=must-not-be-published",
            "models": {"free": {"cost": {"input": 0, "output": 0}}},
        },
    }


def test_normalize_excludes_registered_paid_and_insecure():
    reg = Registry({"registered": {"upstream_url": "https://registered.example/v1"}})
    providers = normalize_models_dev(_catalog(), reg)
    assert [item["id"] for item in providers] == ["candidate"]
    assert providers[0]["models"][0]["id"] == "free-tool"
    assert providers[0]["models"][0]["tool_calling"] is True
    assert providers[0]["status"] == "candidate"


def test_discover_fetches_public_catalog(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self): return _catalog()

    monkeypatch.setattr("open_free_router.discovery.requests.get", lambda *a, **k: Response())
    snapshot = discover(Registry())
    assert snapshot["candidate_provider_count"] == 2
    assert snapshot["candidate_model_count"] == 2
    assert "manual verification" in snapshot["trust"]


def test_save_discovery_is_owner_only(tmp_path):
    path = tmp_path / "nested" / "discovery.json"
    save_discovery({"providers": []}, path)
    assert json.loads(path.read_text()) == {"providers": []}
    assert path.stat().st_mode & 0o077 == 0
