from open_free_router.registry import Registry
from open_free_router.routing import RoutePlanner, RoutingConfig


def _registry():
    return Registry({
        "first": {
            "prefix": "a",
            "models": [
                {"id": "plain"},
                {"id": "coder", "upstream_id": "org/coder", "tool_calling": True},
            ],
        },
        "second": {
            "prefix": "b",
            "models": [{"id": "fast", "tool_calling": True}],
        },
    })


def test_explicit_model_keeps_single_target():
    plan = RoutePlanner(_registry()).plan("a/plain")
    assert plan.strategy == "explicit"
    assert [target.canonical_id for target in plan.candidates] == ["a/plain"]


def test_auto_uses_registry_order_and_bounds_attempts():
    planner = RoutePlanner(_registry(), {"fallback": {"max_attempts": 2}})
    plan = planner.plan("auto")
    assert [target.canonical_id for target in plan.candidates] == ["a/plain", "a/coder"]


def test_auto_coding_filters_models_without_tools():
    plan = RoutePlanner(_registry()).plan("auto/coding")
    assert [target.canonical_id for target in plan.candidates] == ["a/coder", "b/fast"]
    assert {item["reason"] for item in plan.rejected} == {"tool_calling_required"}


def test_custom_alias_resolves_all_supported_model_id_forms():
    config = RoutingConfig.from_dict({
        "aliases": {
            "my-coding": {
                "candidates": ["org/coder", "b/fast", "missing"],
                "require": {"tool_calling": True},
            }
        }
    })
    plan = RoutePlanner(_registry(), config).plan("my-coding")
    assert [target.canonical_id for target in plan.candidates] == ["a/coder", "b/fast"]
    assert {"model": "missing", "reason": "not_registered"} in plan.rejected


def test_custom_alias_without_candidates_uses_matching_registry_models():
    planner = RoutePlanner(_registry(), {
        "aliases": {"tools-only": {"require": {"tool_calling": True}}}
    })
    plan = planner.plan("tools-only")
    assert [target.canonical_id for target in plan.candidates] == ["a/coder", "b/fast"]


def test_routing_config_clamps_attempts_and_ignores_bad_aliases():
    config = RoutingConfig.from_dict({
        "fallback": {"max_attempts": 999, "explicit_model": True},
        "aliases": {"": {}, "bad": "value"},
    })
    assert config.max_attempts == 10
    assert config.explicit_model_fallback is True
    assert config.aliases == {}


def test_routing_config_ignores_non_mapping_aliases():
    assert RoutingConfig.from_dict({"aliases": ["bad"]}).aliases == {}
