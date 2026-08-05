import json
import threading

from open_free_router.quota import parse_quota_headers
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


def test_rate_limit_and_quota_recover_on_different_timescales_but_both_recover():
    limited = classify_failure(429, "rate limited")
    exhausted = classify_failure(429, "quota exhausted")
    assert limited.credential_cooldown is True and limited.retryable is True
    # Quota wording earns a far longer wait, but a 429 never becomes terminal:
    # "quota"/"credit"/"balance" appear in ordinary rate-limit copy, and treating
    # them as permanent took healthy providers offline until a manual reset.
    assert exhausted.credential_cooldown is True and exhausted.retryable is True
    assert exhausted.credential_terminal is False
    assert exhausted.cooldown_scale > limited.cooldown_scale


def test_quota_flavoured_429_waits_longer_than_a_plain_rate_limit():
    manager = ResilienceManager(credential_cooldown=60)
    manager.record_failure("p", "m", classify_failure(429, "rate limited"), now=100)
    manager.record_failure("p", "m", classify_failure(429, "quota exceeded"), credential_slot=1, now=100)
    states = manager.snapshot()["credentials"]
    assert states["p:slot-0"]["cooldown_until"] == 160
    assert states["p:slot-1"]["cooldown_until"] == 100 + 60 * 8


def test_terminal_credential_earns_a_bounded_recovery_probe():
    manager = ResilienceManager(credential_terminal_probe=900)
    manager.record_failure("p", "m", classify_failure(401), now=100)
    assert manager.can_attempt("p", "m", now=500) == (False, "credential_credential_invalid")
    # Once the window elapses exactly one request is let through to re-test.
    assert manager.can_attempt("p", "m", now=1001) == (True, "ready")
    assert manager.can_attempt("p", "m", now=1002) == (False, "credential_terminal_probe_busy")
    # A successful probe clears terminal outright -- this is how a rotated key
    # recovers without an operator calling reset.
    manager.record_success("p", "m", now=1003)
    assert manager.can_attempt("p", "m", now=1004) == (True, "ready")
    assert manager.snapshot()["credentials"]["p:slot-0"]["state"] == "ready"


def test_failed_recovery_probe_backs_off_but_duplicates_do_not():
    manager = ResilienceManager(credential_terminal_probe=100)
    manager.record_failure("p", "m", classify_failure(401), now=0)
    # Duplicate in-flight failures against a terminal slot must not extend it.
    manager.record_failure("p", "m", classify_failure(401), now=10)
    assert manager.can_attempt("p", "m", now=101) == (True, "ready")
    # That probe failed, so the next window doubles rather than staying flat.
    manager.record_failure("p", "m", classify_failure(401), now=102)
    assert manager.can_attempt("p", "m", now=250) == (False, "credential_credential_invalid")
    assert manager.can_attempt("p", "m", now=302) == (True, "ready")


def test_terminal_probe_reservation_is_released_by_any_outcome():
    manager = ResilienceManager(credential_terminal_probe=100, credential_cooldown=10)
    manager.record_failure("p", "m", classify_failure(401), now=0)
    assert manager.can_attempt("p", "m", now=101) == (True, "ready")
    # A probe that comes back 429 keeps the slot terminal, but must not leave the
    # reservation set -- otherwise the slot would never be probed again.
    manager.record_failure("p", "m", classify_failure(429), now=102)
    assert manager.snapshot()["credentials"]["p:slot-0"]["probe_in_flight"] is False
    assert manager.snapshot()["credentials"]["p:slot-0"]["state"] == "terminal"
    assert manager.can_attempt("p", "m", now=202) == (True, "ready")


def test_terminal_credential_is_not_downgraded_by_late_rate_limit():
    manager = ResilienceManager(credential_cooldown=60)
    manager.record_failure("p", "m", classify_failure(401), now=100)
    manager.record_failure("p", "m", classify_failure(429), now=101)
    state = manager.snapshot()["credentials"]["p:slot-0"]
    assert state["state"] == "terminal"
    assert state["reason"] == "credential_invalid"


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


def test_quota_state_is_redacted_preserved_and_orders_credentials():
    manager = ResilienceManager()
    later = parse_quota_headers({"X-RateLimit-Reset-Requests": "200s"}, 200, now=100)
    sooner = parse_quota_headers({"X-RateLimit-Reset-Requests": "100s"}, 200, now=100)
    manager.record_quota("provider", 0, later)
    manager.record_quota("provider", 1, sooner)
    manager.record_success("provider", "model", credential_slot=0, now=150)
    manager.record_success("provider", "model", credential_slot=1, now=140)

    ordered = manager.order_credentials(
        "provider", [(0, "secret-zero"), (1, "secret-one")], now=150
    )
    assert [slot for slot, _ in ordered] == [1, 0]
    manager.record_failure(
        "provider", "model", classify_failure(401), credential_slot=1, now=160
    )
    state = manager.snapshot()["credentials"]["provider:slot-1"]
    assert state["quota"]["requests"]["reset_at"] == 200
    encoded = json.dumps(manager.snapshot())
    assert "secret-zero" not in encoded and "secret-one" not in encoded


def test_credential_order_prefers_attemptable_then_recent_success():
    manager = ResilienceManager()
    manager.record_success("p", "m", credential_slot=0, now=100)
    manager.record_success("p", "m", credential_slot=1, now=200)
    manager.record_failure("p", "m", classify_failure(429), credential_slot=1, now=210)
    slots = [(0, "zero"), (1, "one"), (2, "two")]
    assert [slot for slot, _ in manager.order_credentials("p", slots, now=211)] == [0, 2, 1]


def test_scoring_signals_are_provider_level_and_use_best_quota_slot():
    manager = ResilienceManager(provider_threshold=1, provider_cooldown=100)
    manager.record_quota(
        "p", 0,
        parse_quota_headers({"X-RateLimit-Limit-Requests": "100",
                             "X-RateLimit-Remaining-Requests": "20"}, 200, now=100),
    )
    manager.record_quota(
        "p", 1,
        parse_quota_headers({"X-RateLimit-Limit-Requests": "100",
                             "X-RateLimit-Remaining-Requests": "80"}, 200, now=100),
    )
    assert manager.scoring_signals(now=101)["p"] == {"health": 1.0, "quota": 0.8}
    manager.record_failure("p", "m", classify_failure(503), now=102)
    assert manager.scoring_signals(now=103)["p"]["health"] == 0.0


def test_retry_after_is_bounded():
    assert parse_retry_after("17", now=0) == 17
    assert parse_retry_after("99999", now=0, cap_seconds=30) == 30
    assert parse_retry_after("not-a-date", now=0) == 0


def test_concurrent_failures_do_not_extend_active_penalties_or_grow_state():
    manager = ResilienceManager(
        provider_threshold=1, provider_cooldown=30,
        credential_cooldown=60, model_cooldown=120,
    )
    manager.record_failure("provider", "model", classify_failure(503), now=100)
    manager.record_failure("provider", "model", classify_failure(429), now=100)
    manager.record_failure("provider", "missing", classify_failure(404), now=100)
    baseline = manager.snapshot()
    persist_calls = 0

    def count_persist():
        nonlocal persist_calls
        persist_calls += 1

    manager._persist_locked = count_persist

    threads = []
    for index in range(100):
        observed_at = 101 + index / 1000
        threads.extend([
            threading.Thread(
                target=manager.record_failure,
                args=("provider", "model", classify_failure(503)), kwargs={"now": observed_at},
            ),
            threading.Thread(
                target=manager.record_failure,
                args=("provider", "model", classify_failure(429)), kwargs={"now": observed_at},
            ),
            threading.Thread(
                target=manager.record_failure,
                args=("provider", "missing", classify_failure(404)), kwargs={"now": observed_at},
            ),
        ])
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    snapshot = manager.snapshot()
    assert snapshot["providers"]["provider"]["open_until"] == baseline["providers"]["provider"]["open_until"]
    assert snapshot["credentials"]["provider:slot-0"]["cooldown_until"] == baseline["credentials"]["provider:slot-0"]["cooldown_until"]
    assert snapshot["models"]["provider/missing"]["lockout_until"] == baseline["models"]["provider/missing"]["lockout_until"]
    assert len(snapshot["providers"]) == len(snapshot["credentials"]) == len(snapshot["models"]) == 1
    assert persist_calls == 0


def test_half_open_allows_only_one_probe_under_concurrency():
    manager = ResilienceManager(provider_threshold=1, provider_cooldown=10)
    manager.record_failure("provider", "model", classify_failure(503), now=100)
    results = []
    lock = threading.Lock()

    def attempt():
        result = manager.can_attempt("provider", "model", now=111)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=attempt) for _ in range(100)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(allowed for allowed, _ in results) == 1
    assert {reason for allowed, reason in results if not allowed} == {"provider_half_open_busy"}
