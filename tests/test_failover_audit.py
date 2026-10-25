"""PRD-149/T2: paired failover audit ledger (decision + outcome) hooks.

Covers the T1 §2.4 completeness matrix end to end through the executor --
successful switch, chained failure, no candidate, budget exhausted and the
deliberately incomplete gated destination -- plus the T1 §4.2 three-layer
secret gate (allowlist -> SENSITIVE_PATTERN rejection -> never interrupts
inference) and the T1 §2.1 ``user_version`` 1->2 migration that must run
before the strict column check.
"""
import logging
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from open_free_router.analytics import AnalyticsStore, ExportSecurityError
from open_free_router.executor import OpenedRoute, RouteFailure, UpstreamExecutor
from open_free_router.failover_audit import (
    AnalyticsAuditHook,
    FailoverAuditHook,
    NoopAuditHook,
    make_audit_hook,
)
from open_free_router.registry import Registry
from open_free_router.resilience import FailureDecision, ResilienceManager
from open_free_router.routing import RoutePlan, RoutePlanner, RouteTarget
from open_free_router.telemetry import RouteDecisionStore


# ---------------------------------------------------------------- fixtures ---

class _Handler(BaseHTTPRequestHandler):
    status = 200
    seen_auth = []
    retry_after = None

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        type(self).seen_auth.append(self.headers.get("Authorization"))
        body = b'{"model": "model", "ok": true}'
        if self.status >= 400:
            body = b'{"error": {"message": "boom"}}'
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.retry_after is not None:
            self.send_header("Retry-After", self.retry_after)
        self.end_headers()
        self.wfile.write(body)


def _handler(status=200, retry_after=None):
    return type(
        f"AuditHandler{status}{retry_after}",
        (_Handler,),
        {"status": status, "seen_auth": [], "retry_after": retry_after},
    )


def _start(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _provider(port, prefix, keys=None):
    value = {
        "prefix": prefix,
        "upstream_url": f"http://127.0.0.1:{port}/v1",
        "models": [{"id": "model", "tool_calling": True}],
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


def _executor(registry, resilience=None, routing=None, decisions=None,
              sleeper=None, **kw):
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


DECISION_COLUMNS = (
    "decision_id", "at", "request_id", "from_candidate", "to_candidate",
    "reason", "attempt", "elapsed_ms", "ttfb_ms", "circuit",
    "requested_model", "strategy", "quota_band", "quota_source",
    "quota_classification", "doctor_verdict", "doctor_failed_checks", "doctor_at",
    "honesty",  # PRD-174/T3 (T1 §4)
)
OUTCOME_COLUMNS = (
    "outcome_id", "decision_id", "settled_at", "outcome", "status_code",
    "error_kind", "ttfb_ms", "total_latency_ms", "attempt",
)


def _ledger(store):
    """Read both audit tables straight from usage.db, in insert order."""
    connection = sqlite3.connect(str(store.path))
    try:
        decisions = [
            dict(zip(DECISION_COLUMNS, row))
            for row in connection.execute(
                f"SELECT {', '.join(DECISION_COLUMNS)} FROM failover_decisions ORDER BY rowid"
            )
        ]
        outcomes = [
            dict(zip(OUTCOME_COLUMNS, row))
            for row in connection.execute(
                f"SELECT {', '.join(OUTCOME_COLUMNS)} FROM failover_outcomes ORDER BY rowid"
            )
        ]
        return decisions, outcomes
    finally:
        connection.close()


def _decision_row(store, decision_id):
    connection = sqlite3.connect(str(store.path))
    try:
        row = connection.execute(
            f"SELECT {', '.join(DECISION_COLUMNS)} FROM failover_decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        return dict(zip(DECISION_COLUMNS, row))
    finally:
        connection.close()


def _outcome_rows(store, decision_id):
    connection = sqlite3.connect(str(store.path))
    try:
        return [
            dict(zip(OUTCOME_COLUMNS, row))
            for row in connection.execute(
                f"SELECT {', '.join(OUTCOME_COLUMNS)} FROM failover_outcomes WHERE decision_id = ?",
                (decision_id,),
            )
        ]
    finally:
        connection.close()


# --------------------------------------------------- T1 §2.4 completeness ---

def test_successful_switch_writes_paired_decision_and_outcome(tmp_path):
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
        ).execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        assert result.target.provider_name == "healthy"
        decision_rows, outcome_rows = _ledger(analytics)
        assert len(decision_rows) == 1
        assert len(outcome_rows) == 1
        decision, outcome = decision_rows[0], outcome_rows[0]
        # The pair is keyed by decision_id.
        assert outcome["decision_id"] == decision["decision_id"]
        assert decision["from_candidate"] == "a/model"
        assert decision["to_candidate"] == "b/model"
        assert decision["reason"] == "provider_unavailable"
        assert decision["attempt"] == 1
        # T1 §2.2 context columns: request context, quota defaults for an
        # unmeasured credential; no doctor run recorded in this store, so
        # the doctor columns keep their T1 defaults (PRD-149/T3 stamps them
        # only after an actual ``doctor`` run).
        assert decision["requested_model"] == "auto"
        assert decision["strategy"] == "priority"
        assert decision["quota_band"] == "unknown"
        assert decision["quota_source"] == "never_measured"
        assert decision["quota_classification"] is None
        assert decision["doctor_verdict"] == "never_run"
        assert decision["doctor_failed_checks"] is None
        assert decision["doctor_at"] is None
        assert outcome["outcome"] == "success"
        assert outcome["status_code"] == 200
        assert outcome["error_kind"] is None
        assert outcome["ttfb_ms"] >= 0
        assert outcome["attempt"] == 2
        # T1 §2.3: latency is backfilled when the response completes.
        assert outcome["total_latency_ms"] is None
        result.close()
        settled = _outcome_rows(analytics, decision["decision_id"])[0]
        assert settled["total_latency_ms"] is not None
        assert settled["total_latency_ms"] >= 0
    finally:
        failing.shutdown()
        healthy.shutdown()
        analytics.close()


def test_chained_failure_writes_one_pair_per_transfer(tmp_path):
    failing_a = _start(_handler(503))
    failing_b = _start(_handler(503))
    healthy = _start(_handler(200))
    analytics = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    decisions = RouteDecisionStore(analytics=analytics)
    try:
        registry = Registry({
            "failing_a": _provider(failing_a.server_address[1], "a"),
            "failing_b": _provider(failing_b.server_address[1], "b"),
            "healthy": _provider(healthy.server_address[1], "c"),
        })
        result = _executor(
            registry, decisions=decisions, sleeper=_Sleeper(), timeout=5,
            routing={"policy": {"backoff_jitter": 0}},
        ).execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        result.close()
        decision_rows, outcome_rows = _ledger(analytics)
        assert [(d["from_candidate"], d["to_candidate"]) for d in decision_rows] == [
            ("a/model", "b/model"), ("b/model", "c/model"),
        ]
        assert outcome_rows[0]["decision_id"] == decision_rows[0]["decision_id"]
        assert outcome_rows[0]["outcome"] == "failed"
        assert outcome_rows[0]["status_code"] == 503
        assert outcome_rows[0]["error_kind"] == "provider_unavailable"
        assert outcome_rows[0]["attempt"] == 2
        assert outcome_rows[1]["decision_id"] == decision_rows[1]["decision_id"]
        assert outcome_rows[1]["outcome"] == "success"
        assert outcome_rows[1]["attempt"] == 3
    finally:
        failing_a.shutdown()
        failing_b.shutdown()
        healthy.shutdown()
        analytics.close()


def test_exhausted_candidates_close_the_pair_with_no_candidate(tmp_path):
    failing_a = _start(_handler(503))
    failing_b = _start(_handler(503))
    analytics = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    decisions = RouteDecisionStore(analytics=analytics)
    try:
        registry = Registry({
            "failing_a": _provider(failing_a.server_address[1], "a"),
            "failing_b": _provider(failing_b.server_address[1], "b"),
        })
        result = _executor(
            registry, decisions=decisions, sleeper=_Sleeper(), timeout=5,
            routing={"policy": {"backoff_jitter": 0}},
        ).execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, RouteFailure)
        assert result.status == 503
        decision_rows, outcome_rows = _ledger(analytics)
        # a -> b failed, then b -> nowhere closed as no_candidate.
        assert [(d["from_candidate"], d["to_candidate"]) for d in decision_rows] == [
            ("a/model", "b/model"), ("b/model", None),
        ]
        assert outcome_rows[0]["outcome"] == "failed"
        assert outcome_rows[1]["decision_id"] == decision_rows[1]["decision_id"]
        assert outcome_rows[1]["outcome"] == "no_candidate"
        assert outcome_rows[1]["status_code"] == 0
        assert outcome_rows[1]["error_kind"] is None
        assert outcome_rows[1]["attempt"] is None
    finally:
        failing_a.shutdown()
        failing_b.shutdown()
        analytics.close()


def test_budget_exhaustion_settles_the_pending_pair(tmp_path):
    failing_handler = _handler(503)
    healthy_handler = _handler(200)
    failing = _start(failing_handler)
    healthy = _start(healthy_handler)
    analytics = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    decisions = RouteDecisionStore(analytics=analytics)
    try:
        registry = Registry({
            "failing": _provider(failing.server_address[1], "a"),
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        # A backoff larger than the budget means the switch never happens.
        result = _executor(
            registry, decisions=decisions, sleeper=_Sleeper(), timeout=5,
            routing={"policy": {
                "backoff_base_seconds": 10, "backoff_jitter": 0,
                "total_budget_seconds": 10,
            }},
        ).execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, RouteFailure)
        assert healthy_handler.seen_auth == []
        decision_rows, outcome_rows = _ledger(analytics)
        assert len(decision_rows) == 1
        assert decision_rows[0]["to_candidate"] == "b/model"
        assert len(outcome_rows) == 1
        assert outcome_rows[0]["decision_id"] == decision_rows[0]["decision_id"]
        assert outcome_rows[0]["outcome"] == "budget_exhausted"
        assert outcome_rows[0]["status_code"] == 0
        assert outcome_rows[0]["attempt"] is None
    finally:
        failing.shutdown()
        healthy.shutdown()
        analytics.close()


def test_gated_destination_is_left_incomplete_not_fabricated(tmp_path):
    """A destination skipped by the resilience gate settles nothing.

    T1 §2.4 last row: a decision whose candidate was never attempted is
    judged ``incomplete`` by the summary view -- the writer must not
    backfill or fabricate an outcome for it.
    """
    failing_handler = _handler(503)
    healthy_handler = _handler(200)
    failing = _start(failing_handler)
    healthy = _start(healthy_handler)
    analytics = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    decisions = RouteDecisionStore(analytics=analytics)
    manager = ResilienceManager(credential_cooldown=60)
    try:
        registry = Registry({
            "failing": _provider(failing.server_address[1], "a"),
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        # Park the healthy credential in cooldown, then pin a plan that
        # still contains it -- the planner's pre-filter is bypassed so the
        # executor's own gate is what skips the candidate.
        manager.record_failure(
            "healthy", "model",
            FailureDecision("rate_limited", retryable=True, credential_cooldown=True),
            credential_slot=0,
        )
        executor = _executor(
            registry, resilience=manager, decisions=decisions,
            sleeper=_Sleeper(), timeout=5,
            routing={"policy": {"backoff_jitter": 0}},
        )
        _plan_with(executor.planner, registry, [("failing", "model"), ("healthy", "model")])
        result = executor.execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, RouteFailure)
        assert healthy_handler.seen_auth == []
        decision_rows, outcome_rows = _ledger(analytics)
        assert len(decision_rows) == 1
        assert decision_rows[0]["to_candidate"] == "b/model"
        assert outcome_rows == []
    finally:
        failing.shutdown()
        healthy.shutdown()
        analytics.close()


def test_same_candidate_retry_settles_and_reopens_pairs(tmp_path):
    limited_handler = _handler(429, retry_after="0")
    healthy_handler = _handler(200)
    limited = _start(limited_handler)
    healthy = _start(healthy_handler)
    analytics = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    decisions = RouteDecisionStore(analytics=analytics)
    try:
        registry = Registry({
            "limited": _provider(limited.server_address[1], "a"),
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        result = _executor(
            registry, decisions=decisions, sleeper=_Sleeper(), timeout=5,
            routing={"policy": {"backoff_jitter": 0}},
        ).execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        result.close()
        decision_rows, outcome_rows = _ledger(analytics)
        # The 429 retry is a decision of its own; its failure settles the
        # first pair and the transfer to the healthy candidate opens the next.
        assert [(d["from_candidate"], d["to_candidate"]) for d in decision_rows] == [
            (None, "a/model"), ("a/model", "b/model"),
        ]
        assert outcome_rows[0]["decision_id"] == decision_rows[0]["decision_id"]
        assert outcome_rows[0]["outcome"] == "failed"
        assert outcome_rows[0]["status_code"] == 429
        assert outcome_rows[0]["error_kind"] == "rate_limited"
        assert outcome_rows[0]["attempt"] == 2
        assert outcome_rows[1]["decision_id"] == decision_rows[1]["decision_id"]
        assert outcome_rows[1]["outcome"] == "success"
        assert outcome_rows[1]["attempt"] == 3
    finally:
        limited.shutdown()
        healthy.shutdown()
        analytics.close()


def test_quota_context_lands_in_the_decision_row(tmp_path):
    class QuotaHandler(_Handler):
        seen_auth = []

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            type(self).seen_auth.append(self.headers.get("Authorization"))
            body = b'{"error": {"message": "daily quota exhausted"}}'
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-RateLimit-Limit-Requests", "100")
            self.send_header("X-RateLimit-Remaining-Requests", "0")
            self.send_header("X-RateLimit-Reset-Requests", "1d")
            self.end_headers()
            self.wfile.write(body)

    limited = _start(QuotaHandler)
    healthy = _start(_handler(200))
    analytics = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    decisions = RouteDecisionStore(analytics=analytics)
    try:
        registry = Registry({
            "limited": _provider(limited.server_address[1], "a"),
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        result = _executor(
            registry, decisions=decisions, sleeper=_Sleeper(), timeout=5,
            routing={"policy": {"backoff_jitter": 0}},
        ).execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        result.close()
        decision_rows, _ = _ledger(analytics)
        assert len(decision_rows) == 1
        # T1 §3.2: band/source/classification come from quota_view, so the
        # ledger and the local quota panel can never disagree.
        assert decision_rows[0]["reason"] == "quota_exhausted"
        assert decision_rows[0]["quota_band"] == "exhausted"
        assert decision_rows[0]["quota_source"] == "upstream_headers"
        assert decision_rows[0]["quota_classification"] == "quota_exhausted"
    finally:
        limited.shutdown()
        healthy.shutdown()
        analytics.close()


# ------------------------------------------------ T1 §4.2 three-layer gate ---

def test_secret_like_identity_is_rejected_but_routing_continues(tmp_path, caplog):
    """A candidate id that trips the sensitive pattern rejects the row.

    The store raises (reject, never mask); the hook downgrades it to a
    warning; the request itself must still succeed.
    """
    failing_handler = _handler(503)
    healthy_handler = _handler(200)
    failing = _start(failing_handler)
    healthy = _start(healthy_handler)
    analytics = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    decisions = RouteDecisionStore(analytics=analytics)
    try:
        # The provider prefix looks like a key: any decision row carrying it
        # must be rejected by SENSITIVE_PATTERN, not stored.
        registry = Registry({
            "sk-lookalike1234": _provider(failing.server_address[1], "sk-lookalike1234"),
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        with caplog.at_level(logging.WARNING, logger="open_free_router.failover_audit"):
            result = _executor(
                registry, decisions=decisions, sleeper=_Sleeper(), timeout=5,
                routing={"policy": {"backoff_jitter": 0}},
            ).execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        assert result.target.provider_name == "healthy"
        result.close()
        decision_rows, outcome_rows = _ledger(analytics)
        assert decision_rows == []
        assert outcome_rows == []
        assert "rejected" in caplog.text
    finally:
        failing.shutdown()
        healthy.shutdown()
        analytics.close()


def test_exploding_hook_never_interrupts_inference(tmp_path):
    class ExplodingHook(FailoverAuditHook):
        def on_failover_decision(self, **fields):
            raise RuntimeError("hook exploded")

        def on_failover_outcome(self, **fields):
            raise RuntimeError("hook exploded")

    failing_handler = _handler(503)
    healthy_handler = _handler(200)
    failing = _start(failing_handler)
    healthy = _start(healthy_handler)
    try:
        registry = Registry({
            "failing": _provider(failing.server_address[1], "a"),
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        result = _executor(
            registry, sleeper=_Sleeper(), timeout=5, audit_hook=ExplodingHook(),
            routing={"policy": {"backoff_jitter": 0}},
        ).execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        assert result.target.provider_name == "healthy"
        result.close()
    finally:
        failing.shutdown()
        healthy.shutdown()


# ------------------------------------------------- T1 §2.1 v1 -> v2 schema ---

LEGACY_USAGE_EVENTS_DDL = """
    CREATE TABLE usage_events (
        event_id TEXT PRIMARY KEY,
        timestamp REAL NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        status TEXT NOT NULL,
        status_code INTEGER NOT NULL,
        ttfb_ms REAL,
        total_latency_ms REAL,
        input_tokens INTEGER,
        output_tokens INTEGER,
        fallback_attempts INTEGER NOT NULL,
        error_kind TEXT NOT NULL,
        circuit_breaker_events INTEGER NOT NULL
    )
"""
LEGACY_FAILOVER_DDL = """
    CREATE TABLE failover_decisions (
        decision_id TEXT PRIMARY KEY,
        at TEXT NOT NULL,
        request_id TEXT NOT NULL,
        from_candidate TEXT,
        to_candidate TEXT,
        reason TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        elapsed_ms REAL,
        ttfb_ms REAL,
        circuit TEXT
    )
"""


def _legacy_v1_database(path):
    """A store exactly as PRD-059/T4 created it: ten columns, user_version 1."""
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(LEGACY_USAGE_EVENTS_DDL)
        connection.execute(LEGACY_FAILOVER_DDL)
        connection.execute(
            "INSERT INTO failover_decisions VALUES "
            "('legacy1', '2026-10-01T00:00:00+00:00', 'ofr_legacy', "
            "'a/model', 'b/model', 'rate_limited', 1, 12.0, 5.0, NULL)"
        )
        connection.execute("PRAGMA user_version=1")
        connection.commit()
    finally:
        connection.close()


def test_v1_store_migrates_in_place_before_the_column_check(tmp_path):
    """The T1 §2.1 warning: migration must precede the strict column check.

    Before PRD-149 the check disabled the whole store on any schema
    mismatch; an unmigrated legacy store must upgrade, not disappear.
    """
    path = tmp_path / "usage.db"
    _legacy_v1_database(path)
    store = AnalyticsStore(path, retention_days=30)
    try:
        # The store stays enabled -- that is the entire point of running the
        # migration before the validation.
        assert store.enabled is True
        assert store.last_error == ""
        connection = sqlite3.connect(str(path))
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            columns = [row[1] for row in connection.execute(
                "PRAGMA table_info(failover_decisions)")]
            legacy = connection.execute(
                "SELECT requested_model, strategy, quota_band, doctor_verdict "
                "FROM failover_decisions WHERE decision_id = 'legacy1'"
            ).fetchone()
        finally:
            connection.close()
        # v1 -> v2 by the T2 migration; PRD-149/T3 raised the generation to
        # 3 with the doctor_state table; PRD-174/T3 raises it to 4 with the
        # honesty columns (declarative ALTERs, rows kept).
        assert version == 4
        assert columns == list(DECISION_COLUMNS)
        # The legacy row survived; its new columns read as NULL.
        assert legacy == (None, None, None, None)
        # The legacy row is still queryable through the unchanged API.
        rows = store.failover_decisions(days=30)
        assert [row["request_id"] for row in rows] == ["ofr_legacy"]
        # New writes carry the full context and pair through the same store.
        decision_id = store.record_failover_decision(
            request_id="ofr_new", from_candidate="a/model", to_candidate="b/model",
            reason="rate_limited", attempt=1, requested_model="auto",
            strategy="priority", quota_band="exhausted", quota_source="event",
            quota_classification="rate_limited",
        )
        assert decision_id and len(decision_id) == 32
        row = _decision_row(store, decision_id)
        assert row["requested_model"] == "auto"
        assert row["quota_band"] == "exhausted"
        assert row["quota_source"] == "event"
        assert row["quota_classification"] == "rate_limited"
        store.record_failover_outcome(
            decision_id=decision_id, outcome="failed", status_code=429,
            error_kind="rate_limited", attempt=2,
        )
        outcomes = _outcome_rows(store, decision_id)
        assert len(outcomes) == 1
        assert outcomes[0]["outcome"] == "failed"
    finally:
        store.close()


def test_fresh_store_is_created_at_v2_with_the_outcomes_table(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        connection = sqlite3.connect(str(store.path))
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            decision_columns = [row[1] for row in connection.execute(
                "PRAGMA table_info(failover_decisions)")]
            outcome_columns = [row[1] for row in connection.execute(
                "PRAGMA table_info(failover_outcomes)")]
        finally:
            connection.close()
        # PRD-149/T3 bumped the schema generation to 3 (doctor_state table);
        # PRD-174/T3 bumps it to 4 (honesty columns, T1 §4).
        assert version == 4
        assert decision_columns == list(DECISION_COLUMNS)
        assert outcome_columns == list(OUTCOME_COLUMNS)
    finally:
        store.close()


def test_outcome_rows_share_the_decision_retention(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        decision_id = store.record_failover_decision(
            request_id="ofr_old", from_candidate="a/model", to_candidate=None,
            reason="transport_error", attempt=1, at="2020-01-01T00:00:00+00:00",
        )
        store.record_failover_outcome(
            decision_id=decision_id, outcome="no_candidate",
            settled_at="2020-01-01T00:00:01+00:00",
        )
        assert store.purge() >= 2
        assert store.failover_decisions() == []
        _, outcomes = _ledger(store)
        assert outcomes == []
    finally:
        store.close()


# --------------------------------------------------------- store and hook ---

def test_outcome_row_gates_and_enum_folding(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        with pytest.raises(ValueError):
            store.record_failover_outcome(decision_id="d1", outcome="weird prose")
        decision_id = store.record_failover_decision(
            request_id="ofr_k", from_candidate="a/model", to_candidate="b/model",
            reason="provider_unavailable", attempt=1,
        )
        # Out-of-whitelist error kinds fold to "unknown"; free prose is
        # never stored (T1 §2.3).
        store.record_failover_outcome(
            decision_id=decision_id, outcome="failed", status_code=500,
            error_kind="free error prose nobody should store", attempt=2,
        )
        outcome = _outcome_rows(store, decision_id)[0]
        assert outcome["error_kind"] == "unknown"
        # The new context columns are inside the sensitive scan too.
        with pytest.raises(ExportSecurityError):
            store.record_failover_decision(
                request_id="ofr_sk", from_candidate="a/model", to_candidate=None,
                reason="provider_unavailable", attempt=1,
                requested_model="sk-abcdefghijklmnop",
            )
        with pytest.raises(ExportSecurityError):
            store.record_failover_outcome(
                decision_id="api_key=placeholder", outcome="success",
            )
    finally:
        store.close()


def test_decision_context_enums_fold_to_safe_values(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        decision_id = store.record_failover_decision(
            request_id="ofr_q", from_candidate="a/model", to_candidate="b/model",
            reason="provider_unavailable", attempt=1, requested_model="auto",
            strategy="priority", quota_band="made-up-band", quota_source="event",
            quota_classification="free prose", doctor_verdict="bogus",
            doctor_failed_checks=-3,
        )
        row = _decision_row(store, decision_id)
        assert row["quota_band"] == "unknown"       # out-of-enum folds to unknown
        assert row["quota_source"] == "event"       # valid enum passes through
        assert row["quota_classification"] is None  # non-enum dropped, not stored
        assert row["doctor_verdict"] == "never_run"
        assert row["doctor_failed_checks"] == 0     # negative clamps to zero
    finally:
        store.close()


def test_success_outcome_latency_backfill_first_write_wins(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        decision_id = store.record_failover_decision(
            request_id="ofr_bf", from_candidate="a/model", to_candidate="b/model",
            reason="provider_unavailable", attempt=1,
        )
        backfill = AnalyticsAuditHook(store).on_failover_outcome(
            decision_id=decision_id, outcome="success", status_code=200,
            ttfb_ms=640.1, attempt=2,
        )
        assert callable(backfill)
        backfill(2103.7)
        outcome = _outcome_rows(store, decision_id)[0]
        assert outcome["total_latency_ms"] == 2103.7
        # A duplicated completion callback must not rewrite the settled value.
        backfill(9.9)
        outcome = _outcome_rows(store, decision_id)[0]
        assert outcome["total_latency_ms"] == 2103.7
    finally:
        store.close()


def test_hook_factory_picks_the_implementation(tmp_path):
    assert isinstance(make_audit_hook(None), NoopAuditHook)
    assert isinstance(make_audit_hook(RouteDecisionStore()), NoopAuditHook)
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        assert isinstance(
            make_audit_hook(RouteDecisionStore(analytics=store)), AnalyticsAuditHook
        )
        assert isinstance(make_audit_hook(analytics=store), AnalyticsAuditHook)
    finally:
        store.close()


def test_noop_hook_records_nothing_and_never_raises():
    hook = NoopAuditHook()
    assert hook.on_failover_decision(request_id="ofr_n", reason="x", attempt=1) is None
    assert hook.on_failover_outcome(decision_id="d", outcome="success") is None


def test_analytics_hook_allowlist_drops_unknown_keys(tmp_path):
    """Layer 1: anything outside the T1 §2.2/§2.3 allowlist is dropped."""
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    hook = AnalyticsAuditHook(store)
    try:
        decision_id = hook.on_failover_decision(
            request_id="ofr_allow", from_candidate="a/model",
            to_candidate="b/model", reason="provider_unavailable", attempt=1,
            smuggled_free_text="api_key=should-never-reach-the-store",
        )
        assert decision_id
        row = _decision_row(store, decision_id)
        assert row["request_id"] == "ofr_allow"
        hook.on_failover_outcome(
            decision_id=decision_id, outcome="success", status_code=200,
            attempt=2, response_body="must be dropped by the allowlist",
        )
        outcomes = _outcome_rows(store, decision_id)
        assert len(outcomes) == 1
        connection = sqlite3.connect(str(store.path))
        try:
            columns = [r[1] for r in connection.execute(
                "PRAGMA table_info(failover_outcomes)")]
        finally:
            connection.close()
        assert "response_body" not in columns
        assert "smuggled_free_text" not in columns
    finally:
        store.close()


def test_analytics_hook_downgrades_rejections_to_none(tmp_path):
    """Layers 2 + 3: rejection is a warning-level None, never an exception."""
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    hook = AnalyticsAuditHook(store)
    try:
        assert hook.on_failover_decision(
            request_id="ofr_sk", from_candidate="sk-abcdefghij",
            to_candidate=None, reason="provider_unavailable", attempt=1,
        ) is None
        assert hook.on_failover_outcome(
            decision_id="api_key=placeholder", outcome="success",
        ) is None
        _, outcomes = _ledger(store)
        assert outcomes == []
    finally:
        store.close()
