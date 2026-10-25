"""PRD-173/T3: 「已有订阅优先」排序与回退衔接（trend issue #867）.

Contract: T1 §4 of ``docs/plans/2026-10-25-byos-access.md`` — one stable
subscribed-first bucketing pass applied after the strategy has produced its
order; explicit requests are never reordered; fallback semantics (PRD-059)
are untouched and the plan keeps its original strategy name. Everything here
is local and deterministic: no network, no credentials, placeholder hosts
only.
"""
from open_free_router.registry import Registry
from open_free_router.routing import RoutePlanner, RoutingConfig


def _registry(extra: dict | None = None):
    providers = {
        "free-a": {
            "prefix": "fa",
            "models": [{"id": "m1"}, {"id": "m2"}],
        },
        "free-b": {
            "prefix": "fb",
            "models": [{"id": "m3"}],
        },
        # A BYOS subscription gateway registered mid-list: with
        # prefer_subscribed it must jump the free providers while keeping
        # its own internal order.
        "byos-gw": {
            "prefix": "oc",
            "base_url": "https://<byos-openai-compat-host>/v1",
            "api_key_env": "OFR_BYOS_OPENAI_COMPAT_API_KEY",
            "auth_mode": "bearer",
            "refresh_method": "byos",
            "models": [{"id": "sub-1"}, {"id": "sub-2"}],
            "byos": {
                "kind": "subscription_gateway",
                "priority_class": "subscribed",
                "enabled": True,
                "adopted_at": "2026-10-25T00:00:00Z",
            },
        },
    }
    if extra:
        providers.update(extra)
    return Registry(providers)


def _order(plan) -> list[str]:
    return [target.canonical_id for target in plan.candidates]


def test_byos_class_property_bucket_labels():
    registry = _registry()
    assert registry.providers["byos-gw"].byos.priority_class == "subscribed"
    planner = RoutePlanner(registry)
    targets = {t.provider_name: t for t in planner._targets()}
    assert targets["byos-gw"].byos_class == "subscribed"
    # Free providers have no byos block and always bucket as "free".
    assert targets["free-a"].byos_class == "free"
    assert targets["free-b"].byos_class == "free"


def test_byos_class_free_priority_class_stays_in_free_bucket():
    registry = _registry(extra={
        "byos-marked-free": {
            "prefix": "bf",
            "models": [{"id": "m9"}],
            "byos": {
                "kind": "subscription_gateway",
                "priority_class": "free",
                "enabled": True,
                "adopted_at": "2026-10-25T00:00:00Z",
            },
        },
    })
    planner = RoutePlanner(
        registry, {"fallback": {"max_attempts": 6}}, prefer_subscribed=True
    )
    plan = planner.plan("auto")
    ids = _order(plan)
    # subscribed first; the byos-marked-free provider stays with the fleet.
    assert ids[0].startswith("oc/")
    assert ids.index("bf/m9") > ids.index("fa/m2")


def test_explain_gains_byos_class_and_keeps_existing_shape():
    config = {"fallback": {"max_attempts": 5}}
    explanation = RoutePlanner(_registry(), config).plan("auto").explain()
    # New additive key: every candidate carries its bucket label.
    assert explanation["byos_class"]["oc/sub-1"] == "subscribed"
    assert explanation["byos_class"]["fa/m1"] == "free"
    # All pre-existing keys keep their exact shapes.
    assert explanation["candidates"] == ["fa/m1", "fa/m2", "fb/m3", "oc/sub-1", "oc/sub-2"]
    assert explanation["selected"] == "fa/m1"
    assert isinstance(explanation["rejected"], list)
    assert set(explanation) == {
        "requested_model", "strategy", "candidates", "byos_class",
        "selected", "rejected", "scoring",
    }


def test_default_off_is_byte_identical_to_legacy_behaviour():
    registry = _registry()
    config = {"fallback": {"max_attempts": 5}}
    legacy = RoutePlanner(registry, config).plan("auto")
    flag_off = RoutePlanner(registry, config, prefer_subscribed=False).plan("auto")
    assert _order(flag_off) == _order(legacy) == [
        "fa/m1", "fa/m2", "fb/m3", "oc/sub-1", "oc/sub-2",
    ]
    assert flag_off.strategy == legacy.strategy == "priority"
    assert flag_off.explain() == legacy.explain()


def test_prefer_subscribed_moves_bucket_front_and_keeps_inner_order():
    config = {"fallback": {"max_attempts": 5}}
    plan = RoutePlanner(_registry(), config, prefer_subscribed=True).plan("auto")
    ids = _order(plan)
    assert ids == ["oc/sub-1", "oc/sub-2", "fa/m1", "fa/m2", "fb/m3"]
    assert plan.selected.canonical_id == "oc/sub-1"
    # 回退衔接（T1 §4.3）：策略名保持原值，不新造 strategy 字符串。
    assert plan.strategy == "priority"


def test_subscribed_first_with_scoring_stable_and_scores_realigned():
    registry = _registry()
    config = RoutingConfig.from_dict({
        "fallback": {"max_attempts": 5},
        "scoring": {"enabled": True},
    })
    baseline = RoutePlanner(registry, config).plan("auto")
    assert baseline.strategy == "scored"
    best_free = _order(baseline)[0]

    plan = RoutePlanner(registry, config, prefer_subscribed=True).plan("auto")
    ids = _order(plan)
    assert ids[:2] == ["oc/sub-1", "oc/sub-2"]
    # Relative order inside the free bucket equals the scored order.
    free_ids = [cid for cid in ids if cid.startswith(("fa/", "fb/"))]
    assert free_ids == [cid for cid in _order(baseline) if cid.startswith(("fa/", "fb/"))]
    # Scores stay aligned with the reordered candidates.
    assert [score.model for score in plan.scores] == ids[:len(plan.scores)]
    assert plan.scores[0].model == "oc/sub-1"
    # Bucketing never fabricates a score the strategy did not produce.
    assert {score.model for score in plan.scores} <= {
        score.model for score in baseline.scores
    } | {"oc/sub-1", "oc/sub-2"}
    assert best_free in ids


def test_explicit_request_is_never_hijacked_by_the_bucket():
    registry = _registry()
    for model_id in ("oc/sub-2", "fa/m2"):
        plan = RoutePlanner(registry, prefer_subscribed=True).plan(model_id)
        assert plan.strategy == "explicit"
        assert _order(plan) == [model_id]
        explanation = plan.explain()
        assert explanation["byos_class"][model_id] == (
            "subscribed" if model_id.startswith("oc/") else "free"
        )


def test_disabled_byos_provider_is_rejected_and_never_bucketed():
    registry = _registry(extra={
        "byos-off": {
            "prefix": "ox",
            "base_url": "https://<byos-custom-host>/v1",
            "api_key_env": "OFR_BYOS_CUSTOM_API_KEY",
            "models": [{"id": "off-1"}],
            "byos": {
                "kind": "subscription_gateway",
                "priority_class": "subscribed",
                "enabled": False,
                "adopted_at": "2026-10-25T00:00:00Z",
            },
        },
    })
    plan = RoutePlanner(registry, prefer_subscribed=True).plan("auto")
    assert "ox/off-1" not in _order(plan)
    assert {"model": "ox/off-1", "reason": "byos_disabled"} in plan.rejected
    # The rejected channel keeps its existing vocabulary (T1 §3.5).
    assert {item["reason"] for item in plan.rejected} == {"byos_disabled"}


def test_max_attempts_truncation_applies_after_bucketing():
    plan = RoutePlanner(
        _registry(), {"fallback": {"max_attempts": 2}}, prefer_subscribed=True
    ).plan("auto")
    assert _order(plan) == ["oc/sub-1", "oc/sub-2"]


def test_fallback_queue_is_the_same_chain_regardless_of_bucketing():
    """T1 §4.3: bucketing only changes the initial order — the candidate
    objects handed to the executor (and through it the PRD-059 trigger,
    backoff and circuit-breaker chain) are identical either way."""
    registry = _registry()
    config = {"fallback": {"max_attempts": 5}}
    off = RoutePlanner(registry, config).plan("auto")
    on = RoutePlanner(registry, config, prefer_subscribed=True).plan("auto")
    assert sorted(_order(on)) == sorted(_order(off))
    assert sorted(t.upstream_model_id for t in on.candidates) == sorted(
        t.upstream_model_id for t in off.candidates
    )


def test_config_byos_block_parsing(tmp_path, monkeypatch, capsys):
    from open_free_router.config import Config
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "registry: registry.yaml\n"
        "byos:\n"
        "  enabled: true\n"
        "  prefer_subscribed: true\n"
        "  adapters:\n"
        "    - byos-openai-compat\n"
        "    - not-a-known-adapter\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    cfg = Config(config_path=config_file)
    assert cfg.byos_enabled is True
    assert cfg.byos_prefer_subscribed is True
    # Unknown names are dropped with a warning — never a silent widening.
    assert cfg.byos_adapters == ["byos-openai-compat"]
    assert "not-a-known-adapter" in capsys.readouterr().out

    # Defaults: enabled/prefer_subscribed default true, adapters default
    # empty — upgrading users see zero behaviour change (T1 §6 redline 6).
    minimal = tmp_path / "minimal.yaml"
    minimal.write_text("registry: registry.yaml\n", encoding="utf-8")
    cfg = Config(config_path=minimal)
    assert cfg.byos_enabled is True
    assert cfg.byos_prefer_subscribed is True
    assert cfg.byos_adapters == []

    # Fault tolerance: non-dict block falls back to empty; non-true bools
    # are strictly false.
    weird = tmp_path / "weird.yaml"
    weird.write_text(
        "registry: registry.yaml\n"
        "byos: nope\n",
        encoding="utf-8",
    )
    cfg = Config(config_path=weird)
    assert cfg.byos_enabled is True
    assert cfg.byos_prefer_subscribed is True
    assert cfg.byos_adapters == []

    strict = tmp_path / "strict.yaml"
    strict.write_text(
        "registry: registry.yaml\n"
        "byos:\n"
        "  enabled: false\n"
        "  prefer_subscribed: yes-please\n",
        encoding="utf-8",
    )
    cfg = Config(config_path=strict)
    assert cfg.byos_enabled is False
    # Strict `is True` parsing: a truthy non-bool is not an opt-in.
    assert cfg.byos_prefer_subscribed is False
