import math

from open_free_router.registry import Registry
from open_free_router.routing import RoutePlanner, RoutingConfig, diagnose_routing_config
from open_free_router.scoring import ScoringConfig


def _registry():
    return Registry({
        "first": {"prefix": "a", "models": [{"id": "model", "tool_calling": True}]},
        "second": {"prefix": "b", "models": [{"id": "model", "tool_calling": True}]},
    })


def test_scoring_is_opt_in_and_disabled_keeps_registry_priority():
    planner = RoutePlanner(_registry(), {"scoring": {"enabled": False}})
    plan = planner.plan("auto", {"b/model": {"health": 1}, "a/model": {"health": 0}})
    assert plan.strategy == "priority"
    assert [target.canonical_id for target in plan.candidates] == ["a/model", "b/model"]
    assert plan.explain()["scoring"] == {"enabled": False, "candidates": []}


def test_scoring_orders_candidates_and_explains_every_factor():
    planner = RoutePlanner(_registry(), {
        "scoring": {
            "enabled": True,
            "weights": {"health": 1, "success_rate": 1, "latency": 1,
                        "quota": 1, "capability": 1, "free_evidence": 1},
            "missing_default": 0.25,
            "latency_good_ms": 100,
            "latency_bad_ms": 1100,
        }
    })
    plan = planner.plan("auto", {
        "a/model": {"health": 0, "success_rate": 0.2, "ttfb_p95_ms": 1000, "quota": 0},
        "b/model": {"health": 1, "success_rate": 1, "ttfb_p95_ms": 100, "quota": 1},
    })
    assert plan.strategy == "scored"
    assert [target.canonical_id for target in plan.candidates] == ["b/model", "a/model"]
    explanation = plan.explain()["scoring"]
    assert explanation["enabled"] is True
    assert set(explanation["candidates"][0]["factors"]) == {
        "health", "success_rate", "latency", "quota", "capability", "free_evidence",
    }
    assert math.isclose(sum(
        factor["weight"] for factor in explanation["candidates"][0]["factors"].values()
    ), 1.0, abs_tol=0.00001)


def test_scoring_config_normalizes_weights_and_nan_uses_safe_defaults():
    config = ScoringConfig.from_dict({
        "enabled": True,
        "weights": {"health": float("nan"), "quota": -1},
        "missing_default": float("nan"),
    })
    assert math.isclose(sum(config.weights.values()), 1.0)
    assert all(math.isfinite(value) and value >= 0 for value in config.weights.values())
    assert config.missing_default == 0.5


def test_scoring_diagnostics_reject_invalid_values():
    issues = diagnose_routing_config({
        "scoring": {
            "enabled": "yes",
            "weights": {"health": -1, "mystery": 4},
            "missing_default": 2,
            "latency_good_ms": 1000,
            "latency_bad_ms": 100,
        }
    }, _registry())
    codes = {issue.code for issue in issues}
    assert {"invalid_boolean", "invalid_score_weight", "unknown_score_factor",
            "invalid_missing_default", "invalid_latency_bounds"} <= codes
