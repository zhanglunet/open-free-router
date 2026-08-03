from __future__ import annotations

import json
from datetime import datetime, timezone

from open_free_router.public_catalog import _speed_tier, build_public_catalog, write_public_catalog
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
        # Per-model evidence, not the provider's verdict: a model is only
        # published available when something probed that model.
        "models": {"provider/model:free": {"availability": "available", "latency_ms": 100}},
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


def test_speed_tier_does_not_assert_unavailability_without_evidence():
    # "当前不可用" is an affirmative claim that the provider is down. A model we
    # simply have not probed is not down, and worker/probe.js copies this string
    # onto the live /api/catalog unchanged, so the lie reaches the status board.
    assert _speed_tier("unverified", None) == "未测"
    assert _speed_tier("unverified", 100) == "未测"
    assert _speed_tier("unavailable", None) == "当前不可用"
    assert _speed_tier("available", 100) == "快"


def test_public_catalog_accepts_an_injected_clock():
    # CI re-exports the catalog with generated_at pinned to the committed value
    # and diffs the result, which only works if the export is a pure function of
    # its inputs. datetime.now() inside the builder would defeat that.
    reg = Registry({
        "p": {"upstream_url": "https://api.example/v1", "prefix": "p", "models": [{"id": "m"}]},
    })
    pinned = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    snapshot = {"as_of": "2026-08-01T00:00:00Z", "providers": {}}
    first = build_public_catalog(reg, snapshot, None, now=pinned)
    second = build_public_catalog(reg, snapshot, None, now=pinned)
    assert first["generated_at"] == pinned.isoformat()
    assert first == second


def test_public_catalog_rejects_credential_like_values(tmp_path):
    output = tmp_path / "catalog.json"
    for value in ("Bearer hidden-placeholder", "sk-secretplaceholder123"):
        try:
            write_public_catalog({"schema_version": 2, "note": value}, output)
        except ValueError as exc:
            assert "credential" in str(exc).lower()
        else:
            raise AssertionError("credential-like catalog value was written")


def test_model_availability_comes_from_per_model_evidence_not_the_provider():
    # The provider verdict is one smoke test. Stamping it onto every model
    # published 7 models as available with no evidence of their own, and made
    # the per-model column carry zero per-model information.
    reg = Registry({
        "p": {
            "upstream_url": "https://api.example/v1",
            "prefix": "p",
            "models": [{"id": "good"}, {"id": "unprobed"}],
        },
    })
    catalog = build_public_catalog(reg, {
        "as_of": "2026-08-03T00:00:00Z",
        "providers": {"p": {"availability": "available", "latency_ms": 100}},
        "models": {
            "p/good": {"availability": "available", "latency_ms": 900, "reason": "实测成功"},
        },
    })
    models = {m["id"]: m for m in catalog["providers"][0]["models"]}
    assert models["good"]["availability"] == "available"
    assert models["good"]["speed_tier_zh"] == "快"
    # No per-model evidence: the provider being up says nothing about this model.
    assert models["unprobed"]["availability"] == "unverified"
    assert models["unprobed"]["speed_tier_zh"] == "未测"
    # The provider's own verdict is untouched.
    assert catalog["providers"][0]["availability"] == "available"


def test_a_snapshot_without_per_model_evidence_publishes_unverified_models():
    # Legacy provider-level snapshots (the local dashboard writes one) carry no
    # per-model evidence at all. Falling back to the provider verdict would be
    # the exact fabrication this replaces.
    reg = Registry({"p": {"upstream_url": "https://api.example/v1", "prefix": "p", "models": [{"id": "m"}]}})
    catalog = build_public_catalog(reg, {
        "as_of": "2026-08-03T00:00:00Z",
        "providers": {"p": {"availability": "available", "latency_ms": 100}},
    })
    assert catalog["providers"][0]["models"][0]["availability"] == "unverified"


def test_provider_reason_ratio_matches_the_published_model_count():
    # The provider reason is prose generated by the probe and copied verbatim.
    # After a model is removed from the registry the probe's snapshot still
    # counts it, so the published catalog said "已验证 1/3 个模型可用" for a
    # provider it simultaneously declared as having 2 models.
    reg = Registry({
        "p": {"upstream_url": "https://api.example/v1", "prefix": "p", "models": [{"id": "a"}, {"id": "b"}]},
    })
    catalog = build_public_catalog(reg, {
        "as_of": "2026-08-03T00:00:00Z",
        "providers": {"p": {
            "availability": "available",
            "latency_ms": 100,
            "reason": "Cloudflare 服务器已验证 1/3 个模型可用",
        }},
        "models": {"p/a": {"availability": "available", "latency_ms": 100}},
    })
    provider = catalog["providers"][0]
    assert provider["model_count"] == 2
    assert provider["reason"] == "Cloudflare 服务器已验证 1/2 个模型可用"


def test_a_reason_without_a_ratio_is_left_alone():
    reg = Registry({"p": {"upstream_url": "https://api.example/v1", "prefix": "p", "models": [{"id": "a"}]}})
    catalog = build_public_catalog(reg, {
        "as_of": "2026-08-03T00:00:00Z",
        "providers": {"p": {"availability": "unverified", "reason": "提供商对服务器探测请求限流"}},
        "models": {},
    })
    assert catalog["providers"][0]["reason"] == "提供商对服务器探测请求限流"
