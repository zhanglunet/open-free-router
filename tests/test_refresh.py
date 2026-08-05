"""Regression tests for refresh()'s return-value semantics.

Before this fix, refresh() returned True for any provider whose fetch
*succeeded*, even if the model list was byte-for-byte identical to what
was already stored. Every caller (serve.py's scheduler, ui.py's
/api/refresh, cli.py's `refresh` command) treats True as "something
changed, go save + re-sync agent configs" via `any(results.values())`.
That mismatch meant a normal no-op refresh cycle still triggered a full
registry backup + rewrite of every synced agent config file.
"""
from __future__ import annotations

import types

from open_free_router.refresh import refresh
from open_free_router.registry import ModelInfo, Registry


def _fake_source(models):
    """Build a fake refresh_sources module returning a fixed model list."""
    return types.SimpleNamespace(fetch=lambda provider_base_url, api_key: models)


def test_unchanged_models_report_false(monkeypatch):
    reg = Registry({
        "fake": {
            "upstream_url": "https://example.com/v1",
            "api_key": "sk-test",
            "models": [{"id": "m1"}, {"id": "m2"}],
        },
    })
    same_models = [ModelInfo(id="m1"), ModelInfo(id="m2")]
    monkeypatch.setitem(
        __import__("open_free_router.refresh", fromlist=["SOURCE_MAP"]).SOURCE_MAP,
        "fake", _fake_source(same_models),
    )
    results = refresh(reg, provider_name="fake")
    assert results["fake"] is False
    assert [m.id for m in reg.get("fake").models] == ["m1", "m2"]


def test_changed_models_report_true_and_update_registry(monkeypatch):
    reg = Registry({
        "fake": {
            "upstream_url": "https://example.com/v1",
            "api_key": "sk-test",
            "models": [{"id": "m1"}],
        },
    })
    new_models = [ModelInfo(id="m1"), ModelInfo(id="m2")]
    monkeypatch.setitem(
        __import__("open_free_router.refresh", fromlist=["SOURCE_MAP"]).SOURCE_MAP,
        "fake", _fake_source(new_models),
    )
    results = refresh(reg, provider_name="fake")
    assert results["fake"] is True
    assert [m.id for m in reg.get("fake").models] == ["m1", "m2"]


def test_failed_fetch_reports_false(monkeypatch):
    reg = Registry({
        "fake": {
            "upstream_url": "https://example.com/v1",
            "api_key": "sk-test",
            "models": [{"id": "m1"}],
        },
    })
    monkeypatch.setitem(
        __import__("open_free_router.refresh", fromlist=["SOURCE_MAP"]).SOURCE_MAP,
        "fake", _fake_source([]),  # empty = fetch failed / returned nothing
    )
    results = refresh(reg, provider_name="fake")
    assert results["fake"] is False
    # registry must be untouched on a failed fetch
    assert [m.id for m in reg.get("fake").models] == ["m1"]


def test_any_results_values_reflects_real_change_not_fetch_success():
    """Sanity check on the actual pattern used by serve.py / ui.py / cli.py:
    `any(results.values())` must be False when every provider's fetch
    succeeded but nothing actually changed."""
    results = {"a": False, "b": False, "c": False}
    assert any(results.values()) is False


def test_metadata_change_updates_registry(monkeypatch):
    reg = Registry({
        "fake": {
            "upstream_url": "https://example.com/v1",
            "models": [{"id": "m1", "context_window": 4096}],
        },
    })
    changed_models = [ModelInfo(id="m1", context_window=8192, tool_calling=True)]
    monkeypatch.setitem(
        __import__("open_free_router.refresh", fromlist=["SOURCE_MAP"]).SOURCE_MAP,
        "fake", _fake_source(changed_models),
    )
    results = refresh(reg, provider_name="fake")
    assert results["fake"] is True
    assert reg.get("fake").models[0].context_window == 8192
    assert reg.get("fake").models[0].tool_calling is True
    results["b"] = True
    assert any(results.values()) is True


def _probe_stub(outcomes):
    """Map model id -> probe_model() result, defaulting to a healthy probe."""
    def probe(provider, model, timeout=30):
        return outcomes.get(model.id, {"ok": True, "status": "ok"})
    return probe


def _registry_with(models):
    return Registry({
        "fake": {
            "upstream_url": "https://example.com/v1",
            "api_key": "sk-test",
            "models": models,
        },
    })


def _install(monkeypatch, offered, outcomes):
    module = __import__("open_free_router.refresh", fromlist=["SOURCE_MAP"])
    monkeypatch.setitem(module.SOURCE_MAP, "fake", _fake_source(offered))
    monkeypatch.setattr(module, "probe_model", _probe_stub(outcomes))


def test_probing_is_off_by_default_so_the_scheduler_never_spends_quota(monkeypatch):
    reg = _registry_with([{"id": "m1"}])
    calls = []
    module = __import__("open_free_router.refresh", fromlist=["SOURCE_MAP"])
    monkeypatch.setitem(module.SOURCE_MAP, "fake", _fake_source([ModelInfo(id="m1"), ModelInfo(id="m2")]))
    monkeypatch.setattr(module, "probe_model", lambda *a, **k: calls.append(a) or {"ok": True})

    assert refresh(reg, provider_name="fake") == {"fake": True}
    assert calls == []
    assert [m.id for m in reg.get("fake").models] == ["m1", "m2"]


def test_probe_new_skips_unreachable_candidates_but_keeps_existing_models(monkeypatch):
    reg = _registry_with([{"id": "m1"}])
    _install(
        monkeypatch,
        [ModelInfo(id="m1"), ModelInfo(id="good"), ModelInfo(id="dead")],
        {"dead": {"ok": False, "status": "http_404"}},
    )
    assert refresh(reg, provider_name="fake", probe_new=True) == {"fake": True}
    # m1 was already registered, so it is never re-probed and never dropped.
    assert [m.id for m in reg.get("fake").models] == ["m1", "good"]


def test_probe_new_adopts_candidates_it_cannot_validate(monkeypatch):
    """A probe that never reached the upstream proves nothing about the model."""
    reg = _registry_with([{"id": "m1"}])
    _install(
        monkeypatch,
        [ModelInfo(id="m1"), ModelInfo(id="unverifiable")],
        {"unverifiable": {"ok": False, "status": "no_key"}},
    )
    assert refresh(reg, provider_name="fake", probe_new=True) == {"fake": True}
    assert [m.id for m in reg.get("fake").models] == ["m1", "unverifiable"]


def test_probe_new_never_empties_a_provider(monkeypatch):
    reg = _registry_with([{"id": "m1"}])
    _install(
        monkeypatch,
        [ModelInfo(id="only-new")],
        {"only-new": {"ok": False, "status": "http_404"}},
    )
    assert refresh(reg, provider_name="fake", probe_new=True) == {"fake": False}
    assert [m.id for m in reg.get("fake").models] == ["m1"]
