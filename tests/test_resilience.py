import json

from open_free_router.resilience import (
    ResilienceManager,
    classify_failure,
    parse_retry_after,
)


def test_failure_classification_keeps_scopes_separate():
    assert classify_failure(503).provider_failure is True
    assert classify_failure(401).credential_terminal is True
    assert classify_failure(401).provider_failure is False
    assert classify_failure(404).model_lockout is True
    assert classify_failure(400, "maximum context length exceeded").retryable is True
    assert classify_failure(400, "invalid role").retryable is False


def test_rate_limit_and_quota_have_different_recovery():
    limited = classify_failure(429, "rate limited")
    exhausted = classify_failure(429, "quota exhausted")
    assert limited.credential_cooldown is True and limited.retryable is True
    assert exhausted.credential_terminal is True and exhausted.retryable is False


def test_provider_circuit_opens_and_allows_one_half_open_probe():
    manager = ResilienceManager(provider_threshold=2, provider_cooldown=10)
    failure = classify_failure(503)
    manager.record_failure("p", "m", failure, now=100)
    assert manager.can_attempt("p", "m", now=101) == (True, "ready")
    manager.record_failure("p", "m", failure, now=102)
    assert manager.can_attempt("p", "m", now=103) == (False, "provider_circuit_open")
    assert manager.can_attempt("p", "m", now=113) == (True, "ready")
    assert manager.can_attempt("p", "m", now=113) == (False, "provider_half_open_busy")
    manager.record_success("p", "m")
    assert manager.can_attempt("p", "m", now=114) == (True, "ready")


def test_narrow_failure_releases_half_open_provider_probe():
    manager = ResilienceManager(provider_threshold=1, provider_cooldown=10)
    manager.record_failure("p", "m", classify_failure(503), now=100)
    assert manager.can_attempt("p", "m", now=111) == (True, "ready")
    manager.record_failure("p", "m", classify_failure(401), now=112)
    assert manager.snapshot()["providers"]["p"]["state"] == "closed"
    assert manager.can_attempt("p", "m", credential_slot=1, now=113) == (True, "ready")


def test_credential_and_model_failure_do_not_disable_provider():
    manager = ResilienceManager(credential_cooldown=20, model_cooldown=30)
    manager.record_failure("p", "m1", classify_failure(429), credential_slot=0, now=10)
    assert manager.can_attempt("p", "m1", credential_slot=0, now=11)[1] == "credential_cooldown"
    assert manager.can_attempt("p", "m1", credential_slot=1, now=11) == (True, "ready")
    manager.record_failure("p", "m1", classify_failure(404), credential_slot=1, now=12)
    assert manager.can_attempt("p", "m1", credential_slot=1, now=13)[1] == "model_lockout"
    assert manager.can_attempt("p", "m2", credential_slot=1, now=13) == (True, "ready")


def test_snapshot_contains_slots_but_no_secret_material():
    manager = ResilienceManager()
    manager.record_failure("provider", "model", classify_failure(401), credential_slot=2)
    encoded = json.dumps(manager.snapshot())
    assert "provider:slot-2" in encoded
    assert "api_key" not in encoded
    assert "authorization" not in encoded.lower()


def test_retry_after_is_bounded():
    assert parse_retry_after("17", now=0) == 17
    assert parse_retry_after("99999", now=0, cap_seconds=30) == 30
    assert parse_retry_after("not-a-date", now=0) == 0
