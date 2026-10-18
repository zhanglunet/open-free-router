"""PRD-059/T2: cost-aware routing — downgrade, exclusion, tunable weights.

Covers ``docs/plans/failover-routing-policy.md`` §4:
- §4.4 downgrade rule (remaining/limit < 0.10 → cost × 0.5)
- §4.4 exclusion via ResilienceManager.can_attempt (no new classifier)
- §4.2 tunable policy weights (defaults 0.5/0.3/0.2, auto-normalized)
"""
import json

from open_free_router.costing import (
    CostConfig,
    cost_factor,
    credential_slots,
    excluded_by_cost,
    load_cost_table,
)
from open_free_router.registry import Registry
from open_free_router.routing import (
    POLICY_WEIGHT_DEFAULTS,
    RoutePlanner,
    RoutingConfig,
    diagnose_routing_config,
)


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


class _FakeResilience:
    """Stub exposing can_attempt(provider, model, slot, now)."""

    def __init__(self, blocked):
        self.blocked = blocked  # provider -> reason

    def can_attempt(self, provider, model, slot=0, now=None):
        if provider in self.blocked:
            return False, self.blocked[provider]
        return True, "ready"


# --- cost_factor (§4.4) ---


def test_cost_factor_missing_data_uses_default_without_punishment():
    config = CostConfig()
    assert cost_factor(None, config) == (0.5, "missing_default")
    assert cost_factor("junk", config) == (0.5, "missing_default")


def test_cost_factor_normal_ratio_passes_through():
    config = CostConfig()
    assert cost_factor(0.6, config) == (0.6, "quota_runtime")
    assert cost_factor(1.5, config) == (1.0, "quota_runtime")


def test_cost_factor_low_ratio_is_downgraded_not_cut():
    config = CostConfig()
    value, source = cost_factor(0.05, config)
    assert source == "quota_runtime_downgraded"
    assert value == 0.025  # 0.05 * 0.5: downgraded, still routable


def test_cost_factor_threshold_is_configurable():
    config = CostConfig(downgrade_threshold=0.5, downgrade_factor=0.5,
                        missing_default=0.5)
    value, source = cost_factor(0.4, config)
    assert source == "quota_runtime_downgraded"
    assert value == 0.2


# --- policy weights (§4.2) ---


def test_policy_weights_default_to_t1_values():
    assert RoutingConfig.from_dict({}).policy_weights == dict(POLICY_WEIGHT_DEFAULTS)
    assert POLICY_WEIGHT_DEFAULTS == {"quality": 0.5, "cost": 0.3, "latency": 0.2}


def test_policy_weights_are_tunable_and_normalized():
    config = RoutingConfig.from_dict(
        {"policy": {"weights": {"quality": 1.0, "cost": 1.0, "latency": 0.0}}}
    )
    assert config.policy_weights == {"quality": 0.5, "cost": 0.5, "latency": 0.0}


def test_policy_weights_all_zero_falls_back_to_defaults():
    config = RoutingConfig.from_dict(
        {"policy": {"weights": {"quality": 0, "cost": 0, "latency": 0}}}
    )
    assert config.policy_weights == dict(POLICY_WEIGHT_DEFAULTS)


def test_policy_weights_ignore_unknown_and_bad_values():
    config = RoutingConfig.from_dict(
        {"policy": {"weights": {"quality": -1, "bogus": 9}}}
    )
    # -1 falls back to the default 0.5; "bogus" is ignored entirely.
    assert config.policy_weights == dict(POLICY_WEIGHT_DEFAULTS)


def test_diagnose_flags_bad_policy_weights():
    registry = _registry()
    issues = diagnose_routing_config(
        {"policy": {"weights": {"quality": -1}}}, registry
    )
    assert any(issue.code == "invalid_policy_weight" for issue in issues)
    issues = diagnose_routing_config(
        {"policy": {"weights": {"quality": 0, "cost": 0, "latency": 0}}}, registry
    )
    assert any(issue.code == "zero_policy_weights" for issue in issues)
    issues = diagnose_routing_config(
        {"policy": {"cost_downgrade_threshold": 2}}, registry
    )
    assert any(issue.code == "invalid_cost_policy_value" for issue in issues)


# --- exclusion via ResilienceManager (§4.4) ---


def test_excluded_by_cost_uses_can_attempt_verdict():
    planner = _scoring_planner()
    target = planner.resolve_explicit("p1/m")
    assert target is not None
    blocked = _FakeResilience({"p1": "credential_cooldown"})
    excluded, reason = excluded_by_cost(target, blocked)
    assert excluded is True
    assert reason == "credential_cooldown"
    ready = _FakeResilience({})
    assert excluded_by_cost(target, ready) == (False, "")


def test_excluded_by_cost_ignores_providers_without_credentials():
    registry = Registry({"p3": {"prefix": "p3", "models": [{"id": "m"}]}})
    planner = RoutePlanner(registry, {"scoring": {"enabled": True}})
    target = planner.resolve_explicit("p3/m")
    assert credential_slots(target.provider) == []
    # Left for the executor's credential_missing path, not excluded here.
    assert excluded_by_cost(target, _FakeResilience({"p3": "x"})) == (False, "")


def test_planner_excludes_fully_blocked_candidates():
    planner = _scoring_planner()
    resilience = _FakeResilience({"p1": "credential_cooldown"})
    plan = planner.plan("auto", resilience=resilience)
    assert [t.canonical_id for t in plan.candidates] == ["p2/m"]
    assert {"model": "p1/m", "reason": "credential_cooldown"} in plan.rejected


def test_planner_keeps_candidates_when_resilience_is_ready():
    planner = _scoring_planner()
    plan = planner.plan("auto", resilience=_FakeResilience({}))
    assert [t.canonical_id for t in plan.candidates] == ["p1/m", "p2/m"]
    assert plan.rejected == ()


def test_planner_exclusion_applies_without_scoring():
    planner = RoutePlanner(_registry(), {})
    plan = planner.plan("auto", resilience=_FakeResilience({"p2": "provider_circuit_open"}))
    assert [t.canonical_id for t in plan.candidates] == ["p1/m"]


# --- cost dimension in ranking ---


def test_low_quota_candidate_is_downgraded_in_ranking():
    planner = _scoring_planner()
    signals = {"p1": {"quota": 0.05}, "p2": {"quota": 0.9}}
    plan = planner.plan("auto", signals=signals)
    assert [t.canonical_id for t in plan.candidates] == ["p2/m", "p1/m"]
    by_model = {score.model: score for score in plan.scores}
    assert by_model["p1/m"].factors["cost"].source == "quota_runtime_downgraded"
    assert by_model["p2/m"].factors["cost"].source == "quota_runtime"
    assert set(by_model["p1/m"].factors) == {"quality", "cost", "latency"}


def test_cost_weight_zero_ignores_quota_signals():
    planner = _scoring_planner(weights={"quality": 1.0, "cost": 0.0, "latency": 0.0})
    signals = {"p1": {"quota": 0.05}, "p2": {"quota": 0.9}}
    plan = planner.plan("auto", signals=signals)
    # With cost disabled both candidates score on quality only → registry order.
    assert [t.canonical_id for t in plan.candidates] == ["p1/m", "p2/m"]


def test_missing_quota_scores_default_without_punishment():
    planner = _scoring_planner()
    plan = planner.plan("auto", signals={})
    by_model = {score.model: score for score in plan.scores}
    assert by_model["p1/m"].factors["cost"].value == 0.5
    assert by_model["p1/m"].factors["cost"].source == "missing_default"


# --- cost table loader ---


def test_load_cost_table_missing_file_returns_empty(tmp_path):
    assert load_cost_table(tmp_path / "nope.json") == {}


def test_load_cost_table_reads_free_tier_entries(tmp_path):
    path = tmp_path / "provider-profiles.json"
    path.write_text(json.dumps({
        "p1": {"free_tier": {"has_free_tier": True, "updated_at": "2026-10-18"}},
        "p2": {"free_quota_zh": "text only"},
    }))
    table = load_cost_table(path)
    assert table == {"p1": {"has_free_tier": True, "updated_at": "2026-10-18"}}
