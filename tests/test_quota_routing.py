"""PRD-096/T2: quota-aware routing signals.

Covers the T1 design (issue #56) sections 2-3 as implemented in
``ResilienceManager.scoring_signals`` and consumed by the route planner:

- a credential slot's headroom is its binding dimension (the smaller of
  the requests/tokens remaining ratios), the provider signal is the
  best slot's headroom;
- stale quota observations (older than two probe cycles, 1800s) are
  unknown, not zero: they emit no quota signal and score neutral;
- end to end, fresh quota signals steer the planner's cost dimension,
  while stale ones leave the neutral registry order untouched.
"""
from open_free_router.quota import parse_quota_headers
from open_free_router.registry import Registry
from open_free_router.resilience import (
    QUOTA_SIGNAL_MAX_AGE_SECONDS,
    ResilienceManager,
)
from open_free_router.routing import RoutePlanner


def _observation(requests=None, tokens=None, now=1000):
    headers = {}
    if requests is not None:
        limit, remaining = requests
        headers["X-RateLimit-Limit-Requests"] = str(limit)
        headers["X-RateLimit-Remaining-Requests"] = str(remaining)
    if tokens is not None:
        limit, remaining = tokens
        headers["X-RateLimit-Limit-Tokens"] = str(limit)
        headers["X-RateLimit-Remaining-Tokens"] = str(remaining)
    return parse_quota_headers(headers, 200, now=now)


def test_quota_signal_uses_binding_dimension():
    manager = ResilienceManager()
    # Requests look plentiful (0.9) but tokens are nearly gone (0.1):
    # the slot's real headroom is the binding dimension, 0.1.
    manager.record_quota("p", 0, _observation(requests=(100, 90), tokens=(1000, 100)))
    signals = manager.scoring_signals(now=1001)
    assert signals["p"]["quota"] == 0.1


def test_quota_signal_picks_best_slot_by_binding_dimension():
    manager = ResilienceManager()
    manager.record_quota("p", 0, _observation(requests=(100, 20)))
    manager.record_quota(
        "p", 1, _observation(requests=(100, 90), tokens=(1000, 500))
    )
    # Slot 0 headroom 0.2; slot 1 headroom min(0.9, 0.5) = 0.5 -> best slot.
    signals = manager.scoring_signals(now=1001)
    assert signals["p"]["quota"] == 0.5


def test_quota_signal_drops_stale_observations():
    manager = ResilienceManager()
    manager.record_quota("p", 0, _observation(requests=(100, 80), now=1000))
    fresh = manager.scoring_signals(now=1000 + QUOTA_SIGNAL_MAX_AGE_SECONDS - 1)
    assert fresh["p"]["quota"] == 0.8
    stale = manager.scoring_signals(now=1000 + QUOTA_SIGNAL_MAX_AGE_SECONDS + 1)
    assert "quota" not in stale["p"]
    assert stale["p"]["health"] == 1.0


def test_quota_signal_ignores_observation_without_timestamp():
    manager = ResilienceManager()
    manager.record_quota("p", 0, _observation(requests=(100, 80), now=1000))
    # Simulate a legacy persisted payload that lost its observation time:
    # no evidence, no conviction -- it must not drive routing.
    state = manager._credentials[("p", 0)]
    state.quota = dict(state.quota, observed_at=0.0)
    signals = manager.scoring_signals(now=1001)
    assert "quota" not in signals["p"]


def _registry():
    return Registry(
        {
            "p1": {"prefix": "p1", "api_key": "k1", "models": [{"id": "m"}]},
            "p2": {"prefix": "p2", "api_key": "k2", "models": [{"id": "m"}]},
        }
    )


def test_planner_prefers_provider_with_more_quota_headroom():
    manager = ResilienceManager()
    manager.record_quota("p1", 0, _observation(requests=(100, 90), tokens=(1000, 100)))
    manager.record_quota("p2", 0, _observation(requests=(100, 90)))
    planner = RoutePlanner(_registry(), {"scoring": {"enabled": True}})
    plan = planner.plan("auto", signals=manager.scoring_signals(now=1001))
    assert [t.canonical_id for t in plan.candidates] == ["p2/m", "p1/m"]
    by_model = {score.model: score for score in plan.scores}
    assert by_model["p1/m"].factors["cost"].source == "quota_runtime"
    assert by_model["p1/m"].factors["cost"].value == 0.1


def test_planner_treats_stale_quota_as_unknown_not_zero():
    manager = ResilienceManager()
    manager.record_quota("p1", 0, _observation(requests=(100, 1), now=1000))
    manager.record_quota("p2", 0, _observation(requests=(100, 99), now=1000))
    planner = RoutePlanner(_registry(), {"scoring": {"enabled": True}})
    stale_at = 1000 + QUOTA_SIGNAL_MAX_AGE_SECONDS + 1
    plan = planner.plan("auto", signals=manager.scoring_signals(now=stale_at))
    # Both quotas are stale -> neutral cost -> registry order preserved.
    assert [t.canonical_id for t in plan.candidates] == ["p1/m", "p2/m"]
    by_model = {score.model: score for score in plan.scores}
    assert by_model["p1/m"].factors["cost"].source == "stale"
    assert by_model["p2/m"].factors["cost"].source == "stale"


def test_quota_time_boundary_and_future_are_explained():
    manager = ResilienceManager()
    manager.record_quota("p1", 0, _observation(requests=(100, 80), now=1000))
    assert manager.scoring_signals(now=2800)["p1"]["quota"] == 0.8
    for now in (999, 2801):
        signal = manager.scoring_signals(now=now)["p1"]
        assert "quota" not in signal
        assert signal["quota_source"] == "stale"
        plan = RoutePlanner(_registry(), {"scoring": {"enabled": True}}).plan("auto", signals={"p1": signal})
        cost = next(score for score in plan.scores if score.model == "p1/m").factors["cost"]
        assert cost.source == "stale"
        assert cost.value == 0.5


def test_zero_and_one_usable_slot_and_blocked_slots():
    from open_free_router.resilience import CredentialState, ProviderState
    manager = ResilienceManager()
    manager.record_quota("p1", 0, _observation(requests=(100, 0)))
    manager.record_quota("p1", 1, _observation(requests=(100, 0)))
    assert manager.scoring_signals(now=1001)["p1"]["quota"] == 0
    manager.record_quota("p1", 1, _observation(requests=(100, 80)))
    assert manager.scoring_signals(now=1001)["p1"]["quota"] == 0.8
    manager._credentials[("p1", 1)].state = "terminal"
    assert manager.scoring_signals(now=1001)["p1"]["quota"] == 0
    manager._credentials[("p1", 0)].state = "cooldown"
    manager._credentials[("p1", 0)].cooldown_until = 1100
    assert "quota" not in manager.scoring_signals(now=1001)["p1"]
    assert manager.scoring_signals(now=1100)["p1"]["quota"] == 0
    manager._providers["p1"] = ProviderState(state="open", open_until=1200)
    assert manager.scoring_signals(now=1100)["p1"]["health"] == 0


def test_missing_numbers_are_neutral_not_exhausted():
    manager = ResilienceManager()
    from open_free_router.resilience import CredentialState
    manager._credentials[("p1", 0)] = CredentialState()
    signal = manager.scoring_signals(now=1001)["p1"]
    assert "quota" not in signal
    assert signal["quota_source"] == "never_measured"
