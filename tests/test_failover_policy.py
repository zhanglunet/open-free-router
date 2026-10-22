"""PRD-059/T4: policy-driven failover.

Covers: T1 §2 retry/backoff/timeout policy knobs, T1 §7 failover thresholds,
the 401/402/context_overflow transfer rules, the three timeouts, the total
budget, and the desensitized T1 §5 decision log in usage.db.
"""
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from open_free_router.analytics import AnalyticsStore, ExportSecurityError
from open_free_router.executor import UpstreamExecutor
from open_free_router.failover import (
    FailoverThresholds,
    TimeoutPhase,
    TimeoutPolicy,
    compute_backoff,
)
from open_free_router.registry import Registry
from open_free_router.resilience import ResilienceManager
from open_free_router.routing import RoutePlan, RoutePlanner, RoutingConfig, RouteTarget
from open_free_router.telemetry import RouteDecisionStore


# ---------------------------------------------------------------- fixtures ---

class _Handler(BaseHTTPRequestHandler):
    status = 200
    seen_auth = []
    retry_after = None
    hang = False

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        type(self).seen_auth.append(self.headers.get("Authorization"))
        if type(self).hang:
            time.sleep(30)
            return
        body = json.dumps({"error": {"message": "boom"}}).encode()
        if self.status < 400:
            body = json.dumps({"model": "model", "ok": True}).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.retry_after is not None:
            self.send_header("Retry-After", self.retry_after)
        self.end_headers()
        self.wfile.write(body)


def _handler(status=200, retry_after=None, hang=False):
    return type(
        f"Handler{status}{retry_after}{hang}",
        (_Handler,),
        {"status": status, "seen_auth": [], "retry_after": retry_after, "hang": hang},
    )


def _start(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _provider(port, prefix, keys=None, models=None):
    value = {
        "prefix": prefix,
        "upstream_url": f"http://127.0.0.1:{port}/v1",
        "models": models or [{"id": "model", "tool_calling": True}],
    }
    if keys is None:
        value["api_key"] = f"placeholder-{prefix}"
    else:
        value["api_keys"] = keys
    return value


class _Sleeper:
    """Records requested sleeps without waiting."""

    def __init__(self):
        self.delays = []

    def __call__(self, delay):
        self.delays.append(delay)


def _executor(registry, resilience=None, routing=None, decisions=None, sleeper=None, **kw):
    planner = RoutePlanner(registry, routing or {})
    return UpstreamExecutor(
        planner,
        resilience or ResilienceManager(),
        decisions=decisions,
        sleeper=sleeper,
        **kw,
    )


def _plan_with(planner, registry, ordered_names, strategy="auto"):
    """Pin the candidate order: [(provider_name, model_id), ...]."""
    targets = []
    for provider_name, model_id in ordered_names:
        provider = registry.providers[provider_name]
        model = next(m for m in provider.models if m.id == model_id)
        targets.append(RouteTarget(provider_name, provider, model))
    plan = RoutePlan("auto", strategy, tuple(targets))
    planner.plan = lambda *a, **k: plan
    return plan


# ------------------------------------------------------- policy parsing ---

def test_policy_defaults_match_t1():
    thresholds = FailoverThresholds()
    assert thresholds.provider_threshold == 3
    assert thresholds.provider_cooldown_seconds == 30
    assert thresholds.credential_cooldown_seconds == 60
    assert thresholds.quota_cooldown_scale == 8.0
    assert thresholds.model_cooldown_seconds == 120
    assert thresholds.credential_terminal_probe_seconds == 900
    assert thresholds.credential_terminal_probe_max_scale == 8
    assert thresholds.credential_forbidden_threshold == 3

    config = RoutingConfig()
    assert config.retry_same_candidate_on_429 is True
    assert config.retry_after_max_seconds == 120
    assert config.backoff_base_seconds == 1.0
    assert config.backoff_factor == 2.0
    assert config.backoff_cap_seconds == 30.0
    assert config.backoff_jitter == 0.25
    assert config.connect_timeout_seconds == 10.0
    assert config.ttfb_timeout_seconds == 30.0
    assert config.request_timeout_seconds == 120.0
    assert config.total_budget_seconds == 300.0


def test_failover_thresholds_out_of_range_fall_back_with_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="open_free_router.failover"):
        thresholds = FailoverThresholds.from_dict({
            "provider_threshold": 0,
            "provider_cooldown_seconds": 99999,
            "quota_cooldown_scale": "junk",
            "credential_forbidden_threshold": 50,
        })
    assert thresholds.provider_threshold == 3
    assert thresholds.provider_cooldown_seconds == 30.0
    assert thresholds.quota_cooldown_scale == 8.0
    assert thresholds.credential_forbidden_threshold == 3
    assert "回退默认值" in caplog.text


def test_routing_policy_out_of_range_falls_back(caplog):
    config = RoutingConfig.from_dict({"policy": {
        "backoff_base_seconds": 99,
        "backoff_factor": 0.5,
        "backoff_jitter": 2,
        "retry_same_candidate_on_429": "yes",
        "total_budget_seconds": -5,
    }})
    assert config.backoff_base_seconds == 1.0
    assert config.backoff_factor == 2.0
    assert config.backoff_jitter == 0.25
    assert config.retry_same_candidate_on_429 is True
    assert config.total_budget_seconds == 300.0


def test_routing_policy_yaml_values_apply():
    config = RoutingConfig.from_dict({"policy": {
        "retry_after_max_seconds": 60,
        "backoff_base_seconds": 2.0,
        "connect_timeout_seconds": 5,
        "total_budget_seconds": 120,
    }})
    assert config.retry_after_max_seconds == 60.0
    assert config.backoff_base_seconds == 2.0
    assert config.connect_timeout_seconds == 5.0
    assert config.total_budget_seconds == 120.0


def test_compute_backoff_formula():
    # delay_n = min(cap, base * factor^(n-1)); jitter=0 -> exact.
    assert compute_backoff(1, 1.0, 2.0, 30.0, 0.0) == 1.0
    assert compute_backoff(2, 1.0, 2.0, 30.0, 0.0) == 2.0
    assert compute_backoff(3, 1.0, 2.0, 30.0, 0.0) == 4.0
    assert compute_backoff(10, 1.0, 2.0, 30.0, 0.0) == 30.0  # capped


def test_compute_backoff_jitter_bounds():
    import random
    rng = random.Random(42)
    for _ in range(50):
        delay = compute_backoff(1, 1.0, 2.0, 30.0, 0.25, rng=rng)
        assert 0.75 <= delay <= 1.25


# ------------------------------------------------- 429 same-candidate ---

def test_429_same_candidate_retry_once_then_switches():
    limited_handler = _handler(429, retry_after="1")
    healthy_handler = _handler(200)
    limited = _start(limited_handler)
    healthy = _start(healthy_handler)
    sleeper = _Sleeper()
    try:
        registry = Registry({
            "limited": _provider(limited.server_address[1], "a"),
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        decisions = RouteDecisionStore()
        result = _executor(
            registry, decisions=decisions, sleeper=sleeper, timeout=5,
            routing={"policy": {"backoff_jitter": 0}},
        ).execute("auto", "chat/completions", {"messages": []})
        assert result.attempts == 3  # 429 -> same-candidate retry -> 429 -> switch
        assert result.target.provider_name == "healthy"
        assert len(limited_handler.seen_auth) == 2  # same candidate hit twice
        assert sleeper.delays[0] == 1.0  # upstream's Retry-After, not backoff
        result.close()
    finally:
        limited.shutdown()
        healthy.shutdown()


def test_429_retry_disabled_by_policy():
    limited_handler = _handler(429, retry_after="1")
    healthy_handler = _handler(200)
    limited = _start(limited_handler)
    healthy = _start(healthy_handler)
    sleeper = _Sleeper()
    try:
        registry = Registry({
            "limited": _provider(limited.server_address[1], "a"),
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        result = _executor(
            registry, sleeper=sleeper, timeout=5,
            routing={"policy": {
                "retry_same_candidate_on_429": False, "backoff_jitter": 0}},
        ).execute("auto", "chat/completions", {"messages": []})
        assert result.attempts == 2
        assert result.target.provider_name == "healthy"
        assert len(limited_handler.seen_auth) == 1
        result.close()
    finally:
        limited.shutdown()
        healthy.shutdown()


def test_429_without_retry_after_switches_immediately():
    limited_handler = _handler(429)  # no Retry-After header
    healthy_handler = _handler(200)
    limited = _start(limited_handler)
    healthy = _start(healthy_handler)
    sleeper = _Sleeper()
    try:
        registry = Registry({
            "limited": _provider(limited.server_address[1], "a"),
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        result = _executor(
            registry, sleeper=sleeper, timeout=5,
            routing={"policy": {"backoff_jitter": 0}},
        ).execute("auto", "chat/completions", {"messages": []})
        assert result.attempts == 2
        assert len(limited_handler.seen_auth) == 1
        result.close()
    finally:
        limited.shutdown()
        healthy.shutdown()


def test_quota_429_never_retries_same_candidate():
    # T1 §1.1 #3: quota-flavoured 429 switches candidates without waiting.
    quota_handler = _handler(429)
    healthy_handler = _handler(200)
    quota = _start(quota_handler)
    healthy = _start(healthy_handler)
    sleeper = _Sleeper()
    try:
        registry = Registry({
            "quota": _provider(quota.server_address[1], "a"),
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        # Patch the handler to emit quota wording.
        orig = quota_handler.do_POST
        def quota_post(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            type(self).seen_auth.append(self.headers.get("Authorization"))
            body = json.dumps({"error": {"message": "quota exhausted"}}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", "1")
            self.end_headers()
            self.wfile.write(body)
        quota_handler.do_POST = quota_post
        result = _executor(
            registry, sleeper=sleeper, timeout=5,
            routing={"policy": {"backoff_jitter": 0}},
        ).execute("auto", "chat/completions", {"messages": []})
        assert result.attempts == 2
        assert len(quota_handler.seen_auth) == 1
        result.close()
    finally:
        quota.shutdown()
        healthy.shutdown()


# ------------------------------------------------------------- backoff ---

def test_backoff_between_candidate_switches():
    failing_handler = _handler(503)
    healthy_handler = _handler(200)
    failing = _start(failing_handler)
    healthy = _start(healthy_handler)
    sleeper = _Sleeper()
    try:
        registry = Registry({
            "failing": _provider(failing.server_address[1], "a"),
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        result = _executor(
            registry, sleeper=sleeper, timeout=5,
            routing={"policy": {"backoff_jitter": 0}},
        ).execute("auto", "chat/completions", {"messages": []})
        assert result.target.provider_name == "healthy"
        assert sleeper.delays == [1.0]  # n=1: base 1.0, no jitter
        result.close()
    finally:
        failing.shutdown()
        healthy.shutdown()


def test_total_budget_stops_failover():
    failing_handler = _handler(503)
    healthy_handler = _handler(200)
    failing = _start(failing_handler)
    healthy = _start(healthy_handler)
    sleeper = _Sleeper()
    try:
        registry = Registry({
            "failing": _provider(failing.server_address[1], "a"),
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        # A backoff larger than the budget means the switch never happens.
        result = _executor(
            registry, sleeper=sleeper, timeout=5,
            routing={"policy": {
                "backoff_base_seconds": 10, "backoff_jitter": 0,
                "total_budget_seconds": 10,
            }},
        ).execute("auto", "chat/completions", {"messages": []})
        assert result.status == 503
        assert result.attempts == 1
        assert healthy_handler.seen_auth == []
        assert sleeper.delays == []  # the sleep would have blown the budget
    finally:
        failing.shutdown()
        healthy.shutdown()


def test_timeout_policy_plumbing():
    # Defaults come from T1 §2.3; yaml overrides apply; an explicit
    # non-default legacy timeout still wins for the per-attempt budget.
    registry = Registry({"a": _provider(1, "a")})
    base = _executor(registry, timeout=5)
    assert (base.timeouts.connect, base.timeouts.ttfb) == (10.0, 30.0)
    assert base.timeouts.total == 300.0
    assert base.timeouts.request == 5  # explicit legacy timeout wins

    custom = _executor(
        registry, timeout=120,
        routing={"policy": {"request_timeout_seconds": 90, "total_budget_seconds": 150}},
    )
    assert custom.timeouts.request == 90.0
    assert custom.timeouts.total == 150.0


# ------------------------------------------------------- 401/402 rules ---

def test_401_tries_other_slots_but_never_crosses_candidates():
    # T1 §1.2 #9 and §3.2: a bad key terminal-marks its own slot; the
    # provider's other slots may be tried once, but failover never crosses
    # to another candidate.
    class AuthHandler(_Handler):
        seen_auth = []

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            auth = self.headers.get("Authorization")
            type(self).seen_auth.append(auth)
            status = 401 if auth == "Bearer bad-key" else 200
            body = json.dumps({"error": {"message": "bad key"}}).encode()
            if status == 200:
                body = json.dumps({"model": "model", "ok": True}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    bad_server = _start(AuthHandler)
    other_handler = _handler(200)
    other = _start(other_handler)
    sleeper = _Sleeper()
    try:
        registry = Registry({
            "flaky": _provider(bad_server.server_address[1], "a",
                               keys=["bad-key", "good-key"]),
            "other": _provider(other.server_address[1], "b"),
        })
        result = _executor(
            registry, sleeper=sleeper, timeout=5,
            routing={"policy": {"backoff_jitter": 0}},
        ).execute("auto", "chat/completions", {"messages": []})
        assert result.target.provider_name == "flaky"
        assert result.credential_slot == 1
        assert result.attempts == 2
        assert AuthHandler.seen_auth == ["Bearer bad-key", "Bearer good-key"]
        result.close()
    finally:
        bad_server.shutdown()
        other.shutdown()


def test_401_all_slots_bad_returns_401_without_crossing():
    bad_handler = _handler(401)
    other_handler = _handler(200)
    bad = _start(bad_handler)
    other = _start(other_handler)
    sleeper = _Sleeper()
    try:
        registry = Registry({
            "bad": _provider(bad.server_address[1], "a", keys=["k1", "k2"]),
            "other": _provider(other.server_address[1], "b"),
        })
        result = _executor(
            registry, sleeper=sleeper, timeout=5,
            routing={"policy": {"backoff_jitter": 0}},
        ).execute("auto", "chat/completions", {"messages": []})
        # T1 §1.2 #9: 401 never crosses candidates, even when the other
        # provider would have worked.
        assert result.status == 401
        assert result.attempts == 2
        assert other_handler.seen_auth == []
    finally:
        bad.shutdown()
        other.shutdown()


def test_402_virtual_crosses_providers_explicit_does_not():
    # T1 §1.2 #10.
    broke_handler = _handler(402)
    healthy_handler = _handler(200)
    broke = _start(broke_handler)
    healthy = _start(healthy_handler)
    sleeper = _Sleeper()
    try:
        registry = Registry({
            "broke": _provider(broke.server_address[1], "a"),
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        virtual = _executor(
            registry, sleeper=sleeper, timeout=5,
            routing={"policy": {"backoff_jitter": 0}},
        ).execute("auto", "chat/completions", {"messages": []})
        assert virtual.target.provider_name == "healthy"
        assert virtual.attempts == 2
        virtual.close()

        explicit = _executor(
            Registry({"broke": _provider(broke.server_address[1], "a")}),
            sleeper=sleeper, timeout=5,
            routing={"policy": {"backoff_jitter": 0}},
        ).execute("a/model", "chat/completions", {"messages": []})
        assert explicit.status == 402
        assert explicit.attempts == 1
    finally:
        broke.shutdown()
        healthy.shutdown()


# ------------------------------------------------------- context rule ---

def test_context_overflow_switches_only_to_larger_model():
    # T1 §1.1 #7: conditional transfer.
    class CtxHandler(_Handler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            body = json.dumps(
                {"error": {"message": "context length exceeded"}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    small_handler = CtxHandler
    big_handler = _handler(200)
    small = _start(small_handler)
    big = _start(big_handler)
    sleeper = _Sleeper()
    try:
        registry = Registry({
            "small": _provider(small.server_address[1], "a",
                              models=[{"id": "m", "context_window": 8000}]),
            "big": _provider(big.server_address[1], "b",
                             models=[{"id": "m", "context_window": 128000}]),
        })
        planner = RoutePlanner(registry, {"policy": {"backoff_jitter": 0}})
        _plan_with(planner, registry, [("small", "m"), ("big", "m")])
        executor = UpstreamExecutor(
            planner, ResilienceManager(), timeout=5, sleeper=sleeper)
        result = executor.execute("auto", "chat/completions", {"messages": []})
        assert result.target.provider_name == "big"
        assert result.attempts == 2
        result.close()

        # No larger-context candidate ahead: return the error directly.
        planner2 = RoutePlanner(registry, {"policy": {"backoff_jitter": 0}})
        _plan_with(planner2, registry, [("big", "m"), ("small", "m")])
        # big now answers 400-context too
        big_handler.status = 400
        executor2 = UpstreamExecutor(
            planner2, ResilienceManager(), timeout=5, sleeper=sleeper)
        result2 = executor2.execute("auto", "chat/completions", {"messages": []})
        assert result2.status == 400
        assert result2.attempts == 1
        assert small_handler.seen_auth == []
    finally:
        small.shutdown()
        big.shutdown()


# ------------------------------------------------------------- timeouts ---

def test_timeout_policy_defaults():
    policy = TimeoutPolicy()
    assert (policy.connect, policy.ttfb, policy.request, policy.total) == (10.0, 30.0, 120.0, 300.0)


def test_ttfb_timeout_is_transport_error():
    hang_handler = _handler(200, hang=True)
    server = _start(hang_handler)
    sleeper = _Sleeper()
    try:
        registry = Registry({"slow": _provider(server.server_address[1], "a")})
        decisions = RouteDecisionStore()
        result = _executor(
            registry, decisions=decisions, sleeper=sleeper, timeout=5,
            routing={"policy": {"ttfb_timeout_seconds": 1, "backoff_jitter": 0}},
        ).execute("a/model", "chat/completions", {"messages": []})
        assert result.status == 502
        assert result.timeout_phase == "ttfb"
        assert result.attempts == 1
        decision = decisions.get(result.request_id)
        assert decision["error_kind"] == "transport_error"
    finally:
        server.shutdown()


def test_connect_timeout_is_transport_error():
    # Nothing listens here: connection refused -> transport_error.
    import socket as _socket
    probe = _socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    sleeper = _Sleeper()
    registry = Registry({"dead": _provider(port, "a")})
    result = _executor(
        registry, sleeper=sleeper, timeout=5,
        routing={"policy": {"connect_timeout_seconds": 2, "backoff_jitter": 0}},
    ).execute("a/model", "chat/completions", {"messages": []})
    assert result.status == 502
    assert result.attempts == 1


def test_timeout_phase_exception_carries_phase():
    assert TimeoutPhase("connect").phase == "connect"
    assert TimeoutPhase("ttfb").phase == "ttfb"


# ------------------------------------------------- thresholds → manager ---

def test_failover_thresholds_drive_resilience_manager():
    thresholds = FailoverThresholds.from_dict({"provider_threshold": 1})
    manager = ResilienceManager(**thresholds.to_resilience_kwargs())
    assert manager.provider_threshold == 1
    from open_free_router.resilience import FailureDecision
    manager.record_failure("p", "m", FailureDecision("provider_unavailable", True, provider_failure=True))
    allowed, reason = manager.can_attempt("p", "m")
    assert not allowed and reason == "provider_circuit_open"


def test_quota_cooldown_scale_is_policy_driven():
    quota_handler = _handler(429)
    server = _start(quota_handler)
    sleeper = _Sleeper()
    try:
        registry = Registry({"q": _provider(server.server_address[1], "a")})
        manager = ResilienceManager(credential_cooldown=60, quota_cooldown_scale=2.0)
        orig = quota_handler.do_POST
        def quota_post(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            body = json.dumps({"error": {"message": "quota exhausted"}}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        quota_handler.do_POST = quota_post
        started = time.time()
        result = _executor(
            registry, resilience=manager, sleeper=sleeper, timeout=5,
            routing={"policy": {"backoff_jitter": 0}},
        ).execute("a/model", "chat/completions", {"messages": []})
        assert result.status == 429
        state = manager.snapshot()["credentials"]["q:slot-0"]
        assert state["reason"] == "quota_exhausted"
        # 60s * scale 2.0, not the classifier's built-in 8x.
        assert 100 < state["cooldown_until"] - started < 140
    finally:
        server.shutdown()


# ------------------------------------------------------- decision log ---

def test_decision_log_records_transfers_desensitized(tmp_path):
    failing_handler = _handler(503)
    healthy_handler = _handler(200)
    failing = _start(failing_handler)
    healthy = _start(healthy_handler)
    sleeper = _Sleeper()
    analytics = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    decisions = RouteDecisionStore(analytics=analytics)
    try:
        registry = Registry({
            "failing": _provider(failing.server_address[1], "a"),
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        result = _executor(
            registry, decisions=decisions, sleeper=sleeper, timeout=5,
            routing={"policy": {"backoff_jitter": 0}},
        ).execute("auto", "chat/completions", {"messages": [{"role": "user", "content": "secret-prompt-text"}]})
        assert result.target.provider_name == "healthy"
        result.close()
        rows = analytics.failover_decisions(days=30)
        assert len(rows) == 1
        row = rows[0]
        # T1 §5 columns, exactly.
        assert set(row) == {
            "at", "request_id", "from_candidate", "to_candidate", "reason",
            "attempt", "elapsed_ms", "ttfb_ms", "circuit",
        }
        assert row["from_candidate"] == "a/model"
        assert row["to_candidate"] == "b/model"
        assert row["reason"] == "provider_unavailable"
        assert row["attempt"] == 1
        assert row["circuit"] == "provider"
        assert row["request_id"].startswith("ofr_")
        encoded = json.dumps(rows, ensure_ascii=False)
        assert "secret-prompt-text" not in encoded
        assert "placeholder-a" not in encoded
        assert "Authorization" not in encoded
    finally:
        failing.shutdown()
        healthy.shutdown()
        analytics.close()


def test_decision_log_same_candidate_retry_has_null_from(tmp_path):
    limited_handler = _handler(429, retry_after="1")
    healthy_handler = _handler(200)
    limited = _start(limited_handler)
    healthy = _start(healthy_handler)
    sleeper = _Sleeper()
    analytics = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    decisions = RouteDecisionStore(analytics=analytics)
    try:
        registry = Registry({
            "limited": _provider(limited.server_address[1], "a"),
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        result = _executor(
            registry, decisions=decisions, sleeper=sleeper, timeout=5,
            routing={"policy": {"backoff_jitter": 0}},
        ).execute("auto", "chat/completions", {"messages": []})
        result.close()
        rows = analytics.failover_decisions(days=30)
        retry_rows = [r for r in rows if r["from_candidate"] is None]
        assert len(retry_rows) == 1
        assert retry_rows[0]["to_candidate"] == "a/model"
        assert retry_rows[0]["reason"] == "rate_limited"
        assert retry_rows[0]["circuit"] is None
    finally:
        limited.shutdown()
        healthy.shutdown()
        analytics.close()


def test_decision_log_query_filters(tmp_path):
    analytics = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        analytics.record_failover_decision(
            request_id="ofr_1", from_candidate="a/model", to_candidate="b/model",
            reason="provider_unavailable", attempt=1, elapsed_ms=12.0,
            ttfb_ms=5.0, circuit="provider")
        analytics.record_failover_decision(
            request_id="ofr_2", from_candidate=None, to_candidate="a/model",
            reason="rate_limited", attempt=1, elapsed_ms=3.0,
            ttfb_ms=2.0, circuit=None)
        assert len(analytics.failover_decisions()) == 2
        assert len(analytics.failover_decisions(reason="rate_limited")) == 1
        assert len(analytics.failover_decisions(model="b/model")) == 1
        assert len(analytics.failover_decisions(model="a/model")) == 2
        assert analytics.failover_decisions(reason="nope") == []
    finally:
        analytics.close()


def test_decision_log_rejects_unknown_reason_and_secrets(tmp_path):
    analytics = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        analytics.record_failover_decision(
            request_id="ofr_x", from_candidate="a/model", to_candidate=None,
            reason="weird free prose", attempt=1)
        rows = analytics.failover_decisions()
        assert rows[0]["reason"] == "unknown"
        # Smuggled secret-looking content must be rejected, not stored.
        with pytest.raises(ExportSecurityError):
            analytics.record_failover_decision(
                request_id="ofr_z", from_candidate="a/model sk-abc123XYZ",
                to_candidate=None, reason="provider_unavailable", attempt=1)
    finally:
        analytics.close()


def test_decision_log_retention_purges_old_rows(tmp_path):
    analytics = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        analytics.record_failover_decision(
            request_id="ofr_old", from_candidate="a/model", to_candidate=None,
            reason="transport_error", attempt=1,
            at="2020-01-01T00:00:00+00:00")
        analytics.record_failover_decision(
            request_id="ofr_new", from_candidate="a/model", to_candidate=None,
            reason="transport_error", attempt=1)
        assert analytics.purge() >= 1
        rows = analytics.failover_decisions()
        assert [r["request_id"] for r in rows] == ["ofr_new"]
    finally:
        analytics.close()


@pytest.mark.parametrize('retry_after, expected_attempts', [('0', 3), ('120', 3), ('121', 2), ('garbage', 2), ('nan', 2), ('inf', 2)])
def test_prd096_retry_after_boundary(retry_after, expected_attempts):
    first_handler, second_handler = _handler(429, retry_after), _handler(200)
    first, second = _start(first_handler), _start(second_handler)
    try:
        registry = Registry({'first': _provider(first.server_address[1], 'a'), 'second': _provider(second.server_address[1], 'b')})
        result = _executor(registry, sleeper=_Sleeper(), routing={'policy': {'backoff_base_seconds': 0, 'total_budget_seconds': 300}}).execute('auto', 'chat/completions', {'messages': []})
        assert result.attempts == expected_attempts
        assert result.target.provider_name == 'second'
        assert len(first_handler.seen_auth) == expected_attempts - 1
        result.close()
    finally:
        first.shutdown()
        second.shutdown()


def test_prd096_synthetic_failure_quota_harness(monkeypatch):
    """A fails, B exhausts, C succeeds; quota never revives blocked A/B."""
    from open_free_router.quota import parse_quota_headers
    from open_free_router.resilience import classify_failure
    clock = [10000.0]
    monkeypatch.setattr('open_free_router.resilience.time.time', lambda: clock[0])
    failed_handler, quota_handler, healthy_handler = _handler(503), _handler(429), _handler(200)

    def exhausted(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        type(self).seen_auth.append(self.headers.get('Authorization'))
        body = b'{"error":{"message":"quota exhausted"}}'
        self.send_response(429)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('x-ratelimit-limit-requests', '100')
        self.send_header('x-ratelimit-remaining-requests', '0')
        self.end_headers()
        self.wfile.write(body)
    quota_handler.do_POST = exhausted
    servers = [_start(handler) for handler in (failed_handler, quota_handler, healthy_handler)]
    try:
        registry = Registry({name: _provider(server.server_address[1], name) for name, server in zip(('a', 'b', 'c'), servers)})
        manager = ResilienceManager(provider_threshold=1, provider_cooldown=1000)
        for provider, remaining in [('a', 100), ('b', 80)]:
            manager.record_quota(provider, 0, parse_quota_headers({'x-ratelimit-limit-requests':'100', 'x-ratelimit-remaining-requests':str(remaining)}, 200, now=clock[0]))
        executor = _executor(registry, resilience=manager, sleeper=_Sleeper(), routing={'scoring': {'enabled': True}, 'policy': {'backoff_base_seconds': 0}})
        result = executor.execute('auto', 'chat/completions', {'messages': []})
        assert result.attempts == 3
        assert result.target.provider_name == 'c'
        result.close()
        # The quota failure retains the existing cooldown ×8 semantics.
        assert manager._credentials[('b', 0)].cooldown_until == 10480
        plan = executor.planner.plan('auto', manager.scoring_signals(), manager)
        assert [t.provider_name for t in plan.candidates] == ['c']
        # Even a new plentiful observation cannot override circuit/cooldown gates.
        for provider in ('a', 'b'):
            manager.record_quota(provider, 0, parse_quota_headers({'x-ratelimit-limit-requests':'100', 'x-ratelimit-remaining-requests':'100'}, 200, now=clock[0]))
        assert not manager.can_attempt('a', 'model')[0]
        assert not manager.can_attempt('b', 'model')[0]
        clock[0] = 10480
        assert manager.can_attempt('b', 'model')[0]
        assert not manager.can_attempt('a', 'model')[0]
        clock[0] = 12000
        signals = manager.scoring_signals()
        assert signals['b']['quota_source'] == 'stale'
        plan = executor.planner.plan('auto', signals)
        costs = {score.model: score.factors['cost'] for score in plan.scores}
        assert costs['b/model'].value == 0.5 and costs['b/model'].source == 'stale'
        assert costs['c/model'].value == 0.5  # no observed quota, neutral
    finally:
        for server in servers:
            server.shutdown()
