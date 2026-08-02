from datetime import datetime, timezone

from open_free_router.evidence import FreeTierEvidence, summarize_evidence
from open_free_router.public_catalog import build_public_catalog
from open_free_router.registry import ModelInfo, Registry


NOW = datetime(2026, 8, 2, tzinfo=timezone.utc)


def _evidence(expires_at="2026-09-01T00:00:00Z"):
    return {
        "type": "recurring_quota",
        "limit": 1000,
        "unit": "requests/day",
        "reset_period": "daily",
        "regions": ["global", "global"],
        "evidence_url": "https://example.com/docs/free-tier",
        "verified_at": "2026-08-01T00:00:00Z",
        "expires_at": expires_at,
        "terms_warning_zh": "限额可能调整",
        "requires_payment_method": False,
    }


def test_free_tier_evidence_validates_and_serializes_canonical_shape():
    evidence = FreeTierEvidence.from_dict(_evidence())
    assert evidence.status(NOW) == "verified"
    assert evidence.validation_errors == ()
    payload = evidence.to_dict(include_status=True, now=NOW)
    assert payload["type"] == "recurring_quota"
    assert payload["limit"] == 1000
    assert payload["regions"] == ["global"]
    assert payload["status"] == "verified"


def test_expired_invalid_and_unknown_evidence_are_never_verified():
    expired = FreeTierEvidence.from_dict(_evidence("2026-08-01T12:00:00Z"))
    invalid = FreeTierEvidence.from_dict({
        **_evidence(), "evidence_url": "http://localhost/private", "limit": "many",
    })
    unknown = FreeTierEvidence()
    assert expired.status(NOW) == "expired"
    assert invalid.status(NOW) == "invalid"
    assert "evidence_url must be public HTTPS" in invalid.validation_errors
    assert unknown.status(NOW) == "unknown"
    assert summarize_evidence([expired, invalid, unknown], NOW) == {
        "verified": 0, "expired": 1, "unverified": 0, "unknown": 1, "invalid": 1,
    }


def test_evidence_url_rejects_embedded_credentials_and_sensitive_query():
    embedded = FreeTierEvidence.from_dict({
        **_evidence(), "evidence_url": "https://user:pass@example.com/docs",
    })
    query = FreeTierEvidence.from_dict({
        **_evidence(), "evidence_url": "https://example.com/docs?access_token=secret",
    })
    assert embedded.status(NOW) == "invalid"
    assert query.status(NOW) == "invalid"
    assert "credential query" in query.validation_errors[0]


def test_model_override_and_provider_fallback_survive_registry_roundtrip(tmp_path):
    registry = Registry({
        "provider": {
            "free_tier": _evidence(),
            "models": [
                {"id": "inherits"},
                {"id": "trial", "free_tier": {**_evidence(), "type": "trial", "limit": 10}},
            ],
        }
    })
    provider = registry.get("provider")
    assert provider.free_tier_for(provider.models[0]).tier_type == "recurring_quota"
    assert provider.free_tier_for(provider.models[1]).tier_type == "trial"

    path = tmp_path / "registry.yaml"
    registry.save(path)
    restored = Registry.load(path).get("provider")
    assert restored.free_tier.to_dict() == provider.free_tier.to_dict()
    assert restored.models[1].free_tier.to_dict()["type"] == "trial"


def test_invalid_private_evidence_survives_registry_save_for_doctor_repair(tmp_path):
    registry = Registry({
        "provider": {
            "free_tier": {"type": ["bad"], "limit": ".nan", "evidence_url": "http://local"},
            "models": [{"id": "model"}],
        }
    })
    path = tmp_path / "registry.yaml"
    registry.save(path)
    restored = Registry.load(path).get("provider").free_tier
    assert restored.status(NOW) == "invalid"
    assert restored.raw_payload["type"] == ["bad"]


def test_refresh_preserves_existing_model_evidence():
    registry = Registry({
        "provider": {"models": [{"id": "model", "free_tier": _evidence()}]}
    })
    registry.update_models("provider", [ModelInfo(id="model"), ModelInfo(id="new")])
    provider = registry.get("provider")
    assert provider.models[0].free_tier.status(NOW) == "verified"
    assert provider.models[1].free_tier.status(NOW) == "unknown"


def test_public_catalog_marks_expired_claim_review_required():
    registry = Registry({
        "provider": {
            "free_tier": {
                **_evidence("2020-02-01T00:00:00Z"),
                "verified_at": "2020-01-01T00:00:00Z",
            },
            "models": [{"id": "model"}],
        }
    })
    catalog = build_public_catalog(registry, {"providers": {}})
    model = catalog["providers"][0]["models"][0]
    assert catalog["schema_version"] == 2
    assert model["free_tier"]["status"] == "expired"
    assert model["free_availability"] == "review_required"
