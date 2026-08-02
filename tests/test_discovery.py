from __future__ import annotations

import json

from open_free_router.discovery import (
    _has_valid_chat_response,
    _public_https_host,
    adopt_validated,
    discover,
    normalize_models_dev,
    save_discovery,
    validate_candidates,
)
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
            "env": ["CANDIDATE_API_KEY"],
            "npm": "@ai-sdk/openai-compatible",
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
    assert providers[0]["auth_env"] == ["CANDIDATE_API_KEY"]
    assert providers[0]["credential_env"] == "OFR_CANDIDATE_API_KEY"
    assert providers[0]["protocol"] == "openai-compatible"


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


def test_validation_never_retains_credential_and_adopts_success(monkeypatch):
    snapshot = {
        "providers": [{
            "id": "candidate",
            "api": "https://candidate.example/v1",
            "auth_env": ["CANDIDATE_API_KEY"],
            "credential_env": "OFR_CANDIDATE_API_KEY",
            "protocol": "openai-compatible",
            "models": [{
                "id": "free-tool", "name": "Free Tool", "context_window": 32000,
                "max_tokens": 4096, "reasoning": True, "tool_calling": True,
            }],
        }],
    }

    class Response:
        status_code = 200
        def json(self): return {"choices": [{"message": {"content": "OK"}}]}

    class Session:
        trust_env = True
        def post(self, *args, **kwargs):
            assert self.trust_env is False
            assert kwargs["headers"]["Authorization"] == "Bearer private-value"
            assert kwargs["allow_redirects"] is False
            assert kwargs["json"]["max_tokens"] == 64
            return Response()

    monkeypatch.setattr("open_free_router.discovery._public_https_host", lambda _url: True)
    monkeypatch.setattr("open_free_router.discovery.requests.Session", Session)
    validate_candidates(snapshot, environment={"OFR_CANDIDATE_API_KEY": "private-value"})
    validation = snapshot["providers"][0]["validation"]
    assert validation["state"] == "ready"
    assert validation["successful_models"] == ["free-tool"]
    assert "private-value" not in json.dumps(snapshot)

    registry = Registry()
    assert adopt_validated(snapshot, registry) == ["candidate"]
    provider = registry.get("candidate")
    assert provider.api_key_env == "OFR_CANDIDATE_API_KEY"
    assert provider.api_key == ""
    assert provider.models[0].tool_calling is False


def test_chat_smoke_test_requires_non_empty_assistant_content():
    assert _has_valid_chat_response({"choices": [{"message": {"content": " OK "}}]}) is True
    assert _has_valid_chat_response({
        "choices": [{"message": {"content": [{"type": "text", "text": "OK"}]}}]
    }) is True
    assert _has_valid_chat_response({"choices": [{"message": {"content": ""}}]}) is False
    assert _has_valid_chat_response({"choices": [{}]}) is False


def test_validation_requires_declared_credential(monkeypatch):
    snapshot = {"providers": [{
        "id": "candidate", "api": "https://candidate.example/v1",
        "auth_env": ["CANDIDATE_API_KEY"], "protocol": "openai-compatible", "models": [],
        "credential_env": "OFR_CANDIDATE_API_KEY",
    }]}
    monkeypatch.setattr("open_free_router.discovery._public_https_host", lambda _url: True)
    validate_candidates(snapshot, environment={"CANDIDATE_API_KEY": "generic-token-must-not-be-read"})
    assert snapshot["providers"][0]["validation"]["state"] == "needs_credentials"
    assert adopt_validated(snapshot, Registry()) == []


def test_validation_can_adopt_a_verified_keyless_candidate(monkeypatch):
    snapshot = {"providers": [{
        "id": "keyless", "api": "https://keyless.example/v1",
        "credential_env": "OFR_KEYLESS_API_KEY", "protocol": "openai-compatible",
        "models": [{"id": "model-free", "name": "Free"}],
    }]}

    class Response:
        status_code = 200
        def json(self): return {"choices": [{"message": {"content": "OK"}}]}

    class Session:
        trust_env = True
        def post(self, *args, **kwargs):
            assert "Authorization" not in kwargs["headers"]
            return Response()

    monkeypatch.setattr("open_free_router.discovery._public_https_host", lambda _url: True)
    monkeypatch.setattr("open_free_router.discovery.requests.Session", Session)
    validate_candidates(snapshot, environment={})
    assert snapshot["providers"][0]["validation"]["state"] == "ready"
    registry = Registry()
    assert adopt_validated(snapshot, registry) == ["keyless"]
    assert registry.get("keyless").auth_mode == "none"
    assert registry.get("keyless").api_key_env == ""


def test_public_https_policy_rejects_private_resolution(monkeypatch):
    monkeypatch.setattr(
        "open_free_router.discovery.socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("127.0.0.1", 443))],
    )
    assert _public_https_host("https://candidate.example/v1") is False
