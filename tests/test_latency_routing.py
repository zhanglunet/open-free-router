"""PRD-059/T3: latency-aware routing — realtime TTFB sampling + latency dimension.

Covers ``docs/plans/failover-routing-policy.md`` §4:
- §4.1 sliding window (last 100, switchable) with p50/p95; the gauge
  ``latency_good_ms``/``latency_bad_ms`` reuses the existing one
- §4.2 tunable latency weight (defaults 0.5/0.3/0.2); cold start and
  disabled sampling fall back to the probe-snapshot latency factor
- §4.3 missing data scores missing_default without punishment
"""
from open_free_router.latency import (
    LatencyConfig,
    LatencyStore,
    latency_dimension,
    latency_factor,
)
from open_free_router.registry import Registry
from open_free_router.routing import RoutePlanner, diagnose_routing_config


def _registry():
    return Registry(
        {
            "p1": {"prefix": "p1", "api_key": "k1", "models": [{"id": "m"}]},
            "p2": {"prefix": "p2", "api_key": "k2", "models": [{"id": "m"}]},
        }
    )


def _scoring_planner(**policy):
    raw = {"scoring": {"enabled": True}}
    if policy:
        raw["policy"] = policy
    return RoutePlanner(_registry(), raw)


def _target(planner, provider="p1"):
    for target in planner._targets():
        if target.provider_name == provider:
            return target
    raise AssertionError("no target")


# --- LatencyStore: percentiles ---


def test_p50_p95_nearest_rank():
    store = LatencyStore()
    for value in (100, 200, 300, 400):
        store.record("p1", "m", value)
    assert store.p50("p1", "m") == 200  # ceil(0.5*4) = 2nd
    assert store.p95("p1", "m") == 400  # ceil(0.95*4) = 4th


def test_p50_single_sample():
    store = LatencyStore()
    store.record("p1", "m", 123)
    assert store.p50("p1", "m") == 123
    assert store.p95("p1", "m") == 123


def test_empty_store_returns_none():
    store = LatencyStore()
    assert store.p50("p1", "m") is None
    assert store.p95("p1", "m") is None


def test_junk_samples_ignored():
    store = LatencyStore()
    for junk in ("junk", None, -5, float("inf"), float("nan")):
        store.record("p1", "m", junk)
    assert store.p50("p1", "m") is None
    store.record("p1", "m", 100)
    assert store.p50("p1", "m") == 100


def test_window_evicts_oldest():
    store = LatencyStore(window_size=3)
    for value in (10, 20, 30, 40):
        store.record("p1", "m", value)
    assert store._values("p1", "m") == [20, 30, 40]
    assert store.p50("p1", "m") == 30


def test_provider_aggregate_fallback_when_model_has_no_samples():
    store = LatencyStore()
    store.record("p1", "other-model", 900)
    # exact model empty → provider-wide aggregate kicks in
    assert store.p50("p1", "m") == 900


def test_models_are_keyed_separately():
    store = LatencyStore()
    store.record("p1", "m1", 100)
    store.record("p1", "m2", 900)
    assert store.p50("p1", "m1") == 100
    assert store.p50("p1", "m2") == 900


def test_snapshot_shape():
    store = LatencyStore()
    store.record("p1", "m", 100)
    store.record("p1", "m", 200)
    snap = store.snapshot()
    assert snap[("p1", "m")]["count"] == 2
    assert snap[("p1", "m")]["p50_ms"] == 100
    assert snap[("p1", "m")]["p95_ms"] == 200


# --- latency_factor (§4.1 gauge, §4.3 missing policy) ---


def test_latency_factor_gauge():
    config = LatencyConfig(good_ms=500.0, bad_ms=10000.0)
    assert latency_factor(100, config) == (1.0, "latency_realtime_p50")  # under good
    assert latency_factor(500, config) == (1.0, "latency_realtime_p50")
    assert latency_factor(10000, config) == (0.0, "latency_realtime_p50")  # at bad
    assert latency_factor(20000, config) == (0.0, "latency_realtime_p50")  # over bad
    value, _ = latency_factor(5250, config)  # midpoint
    assert value == 0.5


def test_latency_factor_missing_data_no_punishment():
    config = LatencyConfig()
    assert latency_factor(None, config) == (0.5, "missing_default")
    assert latency_factor("junk", config) == (0.5, "missing_default")
    assert latency_factor(float("inf"), config) == (0.5, "missing_default")
    assert latency_factor(-1, config) == (0.5, "missing_default")


def test_latency_factor_custom_missing_default():
    config = LatencyConfig(missing_default=0.7)
    assert latency_factor(None, config) == (0.7, "missing_default")


# --- LatencyConfig parsing ---


def test_latency_config_defaults():
    config = LatencyConfig.from_dict({})
    assert config.enabled is True
    assert config.window_size == 100


def test_latency_config_disabled():
    config = LatencyConfig.from_dict({"latency_enabled": False})
    assert config.enabled is False


def test_latency_config_invalid_values_fall_back():
    config = LatencyConfig.from_dict(
        {"latency_enabled": "yes", "latency_window_size": -3}
    )
    assert config.enabled is True
    assert config.window_size == 1  # clamped low


def test_latency_config_window_clamped_high():
    config = LatencyConfig.from_dict({"latency_window_size": 999999})
    assert config.window_size == 10000


def test_latency_config_from_routing_reuses_scoring_gauge():
    planner = _scoring_planner()
    config = LatencyConfig.from_routing(planner.config)
    assert config.good_ms == planner.config.scoring.latency_good_ms == 500.0
    assert config.bad_ms == planner.config.scoring.latency_bad_ms == 10000.0
    assert config.missing_default == planner.config.scoring.missing_default == 0.5


# --- latency_dimension: realtime vs snapshot fallback ---


def test_dimension_uses_realtime_p50_when_available():
    planner = _scoring_planner()
    target = _target(planner)
    store = LatencyStore()
    store.record("p1", "m", 250)  # fast → factor 1.0
    config = LatencyConfig.from_routing(planner.config)
    value, source = latency_dimension(target, store, config, 0.25, "probe_snapshot")
    assert (value, source) == (1.0, "latency_realtime_p50")


def test_dimension_cold_start_falls_back_to_snapshot():
    planner = _scoring_planner()
    target = _target(planner)
    store = LatencyStore()  # no data
    config = LatencyConfig.from_routing(planner.config)
    value, source = latency_dimension(target, store, config, 0.25, "probe_snapshot")
    assert (value, source) == (0.25, "probe_snapshot")


def test_dimension_disabled_falls_back_to_snapshot():
    planner = _scoring_planner()
    target = _target(planner)
    store = LatencyStore()
    store.record("p1", "m", 250)
    config = LatencyConfig(enabled=False)
    value, source = latency_dimension(target, store, config, 0.25, "probe_snapshot")
    assert (value, source) == (0.25, "probe_snapshot")


def test_dimension_without_store_falls_back():
    planner = _scoring_planner()
    target = _target(planner)
    config = LatencyConfig.from_routing(planner.config)
    value, source = latency_dimension(target, None, config, 0.25, "probe_snapshot")
    assert (value, source) == (0.25, "probe_snapshot")


# --- RoutePlanner integration ---


def test_planner_prefers_low_latency_with_equal_quality():
    planner = _scoring_planner()
    store = LatencyStore()
    store.record("p1", "m", 200)    # fast
    store.record("p2", "m", 9000)   # slow
    plan = planner.plan("auto", latency_store=store)
    assert plan.candidates[0].provider_name == "p1"
    assert plan.scores[0].factors["latency"].source == "latency_realtime_p50"


def test_planner_latency_weight_zero_ignores_realtime():
    planner = _scoring_planner(weights={"quality": 1.0, "cost": 0.0, "latency": 0.0})
    store = LatencyStore()
    store.record("p1", "m", 9000)  # slow, but latency weight is 0
    plan = planner.plan("auto", latency_store=store)
    assert plan.scores[0].factors["latency"].weight == 0.0
    # total is driven by quality alone
    quality = plan.scores[0].factors["quality"]
    assert plan.scores[0].total == quality.contribution


def test_planner_latency_weight_tunable():
    planner = _scoring_planner(weights={"quality": 0.0, "cost": 0.0, "latency": 1.0})
    store = LatencyStore()
    store.record("p1", "m", 200)
    store.record("p2", "m", 9000)
    plan = planner.plan("auto", latency_store=store)
    assert plan.candidates[0].provider_name == "p1"
    assert plan.scores[0].total == plan.scores[0].factors["latency"].value


def test_planner_cold_start_keeps_snapshot_latency():
    planner = _scoring_planner()
    plan = planner.plan("auto", latency_store=LatencyStore())
    assert plan.scores[0].factors["latency"].source != "latency_realtime_p50"


def test_planner_sampling_disabled_config():
    planner = _scoring_planner(latency_enabled=False)
    store = LatencyStore()
    store.record("p1", "m", 200)
    plan = planner.plan("auto", latency_store=store)
    assert plan.scores[0].factors["latency"].source != "latency_realtime_p50"


# --- RoutingConfig policy parsing ---


def test_routing_config_latency_policy_defaults():
    planner = _scoring_planner()
    assert planner.config.latency_enabled is True
    assert planner.config.latency_window_size == 100


def test_routing_config_latency_policy_invalid_falls_back():
    planner = _scoring_planner(
        latency_enabled="nope", latency_window_size=0
    )
    assert planner.config.latency_enabled is True
    assert planner.config.latency_window_size == 1


# --- diagnose_routing_config ---


def test_diagnose_invalid_latency_policy():
    issues = diagnose_routing_config(
        {"policy": {"latency_enabled": "yes", "latency_window_size": -5}},
        _registry(),
    )
    codes = {issue.code for issue in issues}
    assert "invalid_boolean" in codes
    assert "invalid_latency_window" in codes


def test_diagnose_valid_latency_policy_is_quiet():
    issues = diagnose_routing_config(
        {"policy": {"latency_enabled": False, "latency_window_size": 50}},
        _registry(),
    )
    assert not {issue.code for issue in issues} & {
        "invalid_boolean", "invalid_latency_window"
    }
