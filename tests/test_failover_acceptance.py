"""PRD-149/T5 acceptance: three simulated failover shapes land a complete ledger.

trend #749, adopted PRD-149.  The acceptance objects are the T2-T4 changes
already merged into main -- nothing here may fix them (T5 discipline: tests
and documentation only).  Every criterion maps to one numbered test:

| # | 验收项 | 测试 |
|---|---|---|
| 1 | 模拟 3 次不同形态换路（rate_limited 切换成功 / provider_unavailable 链式失败 / quota_exhausted 无候选），经 T2-T4 已合并钩子完整落账，逐次断言 decision+outcome 成对完整 | ``test_1_three_failover_shapes_land_a_complete_ledger`` |
| 2 | doctor 结论入账（每次换路决策行都带最近一次 doctor 结论） | ``test_2_doctor_conclusion_lands_on_every_decision`` |
| 3 | T4 查询视图 failover-log / failover-summary 查得 3 次换路且过滤正确 | ``test_3_views_find_the_three_failovers_and_filter`` |
| 4 | incomplete 口径正确（被门禁跳过的目的不回填、不编造） | ``test_4_incomplete_pair_is_judged_not_fabricated`` |
| 5 | 无密钥入账：写入侧拒收 + 账本文件原始字节扫描零命中 | ``test_5_no_secrets_reach_the_ledger`` |

All simulations go through the real executor with the factory-built
``AnalyticsAuditHook`` (``make_audit_hook`` over ``RouteDecisionStore``) into
a real ``usage.db`` -- the exact production wiring PRD-149/T2 landed.  The
fake upstreams are throwaway loopback HTTP servers with placeholder
credentials; no real hostnames, accounts or keys appear anywhere.
"""
from __future__ import annotations

import itertools
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_free_router.analytics import (
    SENSITIVE_PATTERN,
    AnalyticsStore,
    ExportSecurityError,
)
from open_free_router.executor import OpenedRoute, RouteFailure, UpstreamExecutor
from open_free_router.failover_audit import doctor_summary_from_report, make_audit_hook
from open_free_router.failover_view import collect_pairs, summarize
from open_free_router.registry import Registry
from open_free_router.resilience import FailureDecision, ResilienceManager
from open_free_router.routing import RoutePlan, RoutePlanner, RouteTarget
from open_free_router.telemetry import RouteDecisionStore


DOCTOR_AT = "2026-10-23T09:30:00+00:00"
# Prose that exists only in the synthetic doctor report; criterion 5 asserts
# none of it ever reaches the ledger bytes.
DOCTOR_LABEL = "config.yaml found"
DOCTOR_DETAIL = "synthetic-doctor-detail-placeholder"

DECISION_COLUMNS = (
    "decision_id", "at", "request_id", "from_candidate", "to_candidate",
    "reason", "attempt", "elapsed_ms", "ttfb_ms", "circuit",
    "requested_model", "strategy", "quota_band", "quota_source",
    "quota_classification", "doctor_verdict", "doctor_failed_checks", "doctor_at",
)
OUTCOME_COLUMNS = (
    "outcome_id", "decision_id", "settled_at", "outcome", "status_code",
    "error_kind", "ttfb_ms", "total_latency_ms", "attempt",
)


# ------------------------------------------------------------- fake upstreams ---

class _Handler(BaseHTTPRequestHandler):
    """Configurable throwaway upstream. Placeholder credentials only."""

    status = 200
    body = b'{"error": {"message": "synthetic failure"}}'
    quota_headers = ()
    seen_auth: list[str] = []

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).seen_auth.append(self.headers.get("Authorization") or "")
        body = self.body if self.status >= 400 else b'{"model": "model", "ok": true}'
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for header, value in self.quota_headers:
            self.send_header(header, value)
        self.end_headers()
        self.wfile.write(body)


_HANDLER_SEQ = itertools.count(1)


def _handler(status=200, body=None, quota_headers=()):
    return type(
        f"AcceptanceHandler{next(_HANDLER_SEQ)}",
        (_Handler,),
        {
            "status": status,
            "body": body or b'{"error": {"message": "synthetic failure"}}',
            "quota_headers": quota_headers,
            "seen_auth": [],
        },
    )


def _start(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _provider(port, prefix):
    """One registry provider: loopback URL, placeholder single credential."""
    return {
        "prefix": prefix,
        "upstream_url": f"http://127.0.0.1:{port}/v1",
        "api_key": f"placeholder-{prefix}",
        "models": [{"id": "model", "tool_calling": True}],
    }


class _Sleeper:
    """Records backoff sleeps without waiting."""

    def __init__(self):
        self.delays = []

    def __call__(self, delay):
        self.delays.append(delay)


def _executor(registry, resilience=None, decisions=None, sleeper=None, routing=None):
    planner = RoutePlanner(registry, routing or {})
    return UpstreamExecutor(
        planner,
        resilience or ResilienceManager(),
        decisions=decisions,
        sleeper=sleeper,
        timeout=5,
    )


def _pin_plan(executor, registry, ordered_names, strategy="priority"):
    """Pin the candidate order [(provider_name, model_id), ...] deterministically."""
    targets = []
    for provider_name, model_id in ordered_names:
        provider = registry.providers[provider_name]
        model = next(m for m in provider.models if m.id == model_id)
        targets.append(RouteTarget(provider_name, provider, model))
    plan = RoutePlan("auto", strategy, tuple(targets))
    executor.planner.plan = lambda *a, **k: plan
    return plan


# ----------------------------------------------------------- ledger readers ---

def _ledger(store):
    connection = sqlite3.connect(str(store.path))
    try:
        decisions = [
            dict(zip(DECISION_COLUMNS, row))
            for row in connection.execute(
                f"SELECT {', '.join(DECISION_COLUMNS)} FROM failover_decisions ORDER BY rowid")
        ]
        outcomes = [
            dict(zip(OUTCOME_COLUMNS, row))
            for row in connection.execute(
                f"SELECT {', '.join(OUTCOME_COLUMNS)} FROM failover_outcomes ORDER BY rowid")
        ]
        return decisions, outcomes
    finally:
        connection.close()


def _ledger_files(store):
    """Raw bytes of usage.db plus its WAL/SHM siblings (zero-hit scan input)."""
    paths = [store.path, Path(f"{store.path}-wal"), Path(f"{store.path}-shm")]
    return [path.read_bytes() for path in paths if path.exists()]


# ------------------------------------------------------- the three shapes ---

def _record_doctor_conclusion(store):
    """The T3 alignment path: report -> aggregates -> latest-doctor-run store."""
    report = {
        "checks": [
            {"ok": True, "severity": "ok", "label": "registry loaded",
             "detail": "synthetic-ok-detail"},
            {"ok": False, "severity": "error", "label": DOCTOR_LABEL,
             "detail": DOCTOR_DETAIL},
        ],
        "routing": [],
        "free_tier": {"issues": []},
        "protocol_matrix": {"issues": [{"ok": False, "severity": "warning",
                                        "label": "synthetic warn", "detail": ""}]},
    }
    summary = doctor_summary_from_report(report)
    # One error-level item -> fail/1; warnings never count (T1 §3.1).
    assert summary == {"doctor_verdict": "fail", "doctor_failed_checks": 1}
    store.record_doctor_run(
        doctor_verdict=summary["doctor_verdict"],
        doctor_failed_checks=summary["doctor_failed_checks"],
        doctor_at=DOCTOR_AT,
    )


class _Simulation:
    """One ledger, three simulated requests, their results and rows."""

    def __init__(self, store, decisions, outcomes, auth_values):
        self.store = store
        self.decision_rows = decisions
        self.outcome_rows = outcomes
        self.auth_values = auth_values


def _simulate_three_shapes(tmp_path) -> _Simulation:
    """Run the three acceptance shapes against one shared ``usage.db``.

    Production wiring throughout: the audit hook comes from
    ``make_audit_hook(RouteDecisionStore(analytics=store))`` (built inside
    ``UpstreamExecutor``), so decision/outcome rows land exactly as the
    merged T2-T4 code writes them.  Opened routes are closed before the
    ledger snapshot so the success latency backfill is visible in the rows.
    """
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    decisions = RouteDecisionStore(analytics=store)
    _record_doctor_conclusion(store)

    servers = []
    opened = []
    auth_values = []
    try:
        # --- shape 1: rate_limited 切换成功 -------------------------------
        # A 429 with numeric quota headers (remaining=0, no Retry-After, no
        # quota wording) classifies as rate_limited; the switch to the second
        # candidate succeeds on first byte.
        limited = _handler(
            429,
            body=b'{"error": {"message": "too many requests, slow down"}}',
            quota_headers=(
                ("X-RateLimit-Limit-Requests", "100"),
                ("X-RateLimit-Remaining-Requests", "0"),
            ),
        )
        healthy = _handler(200)
        servers.append(_start(limited))
        servers.append(_start(healthy))
        registry = Registry({
            "limited": _provider(servers[-2].server_address[1], "rl1"),
            "healthy": _provider(servers[-1].server_address[1], "rl2"),
        })
        executor = _executor(
            registry, decisions=decisions, sleeper=_Sleeper(),
            routing={"policy": {"backoff_jitter": 0}},
        )
        _pin_plan(executor, registry, [("limited", "model"), ("healthy", "model")])
        result = executor.execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        assert result.target.provider_name == "healthy"
        opened.append(result)
        auth_values.extend(limited.seen_auth + healthy.seen_auth)

        # --- shape 2: provider_unavailable 链式失败 ------------------------
        # Two 503s then a healthy third candidate: one pair per transfer,
        # the first settles failed on the second candidate's own 503.
        failing_a = _handler(503)
        failing_b = _handler(503)
        healthy3 = _handler(200)
        servers.append(_start(failing_a))
        servers.append(_start(failing_b))
        servers.append(_start(healthy3))
        registry = Registry({
            "failing_a": _provider(servers[-3].server_address[1], "pu1"),
            "failing_b": _provider(servers[-2].server_address[1], "pu2"),
            "healthy": _provider(servers[-1].server_address[1], "pu3"),
        })
        executor = _executor(
            registry, decisions=decisions, sleeper=_Sleeper(),
            routing={"policy": {"backoff_jitter": 0}},
        )
        _pin_plan(executor, registry, [
            ("failing_a", "model"), ("failing_b", "model"), ("healthy", "model"),
        ])
        result = executor.execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        assert result.target.provider_name == "healthy"
        opened.append(result)
        auth_values.extend(failing_a.seen_auth + failing_b.seen_auth
                           + healthy3.seen_auth)

        # --- shape 3: quota_exhausted 无候选 --------------------------------
        # A single candidate whose 429 body and headers both say exhausted:
        # the decision row records to_candidate=NULL and the pair closes as
        # no_candidate.
        exhausted = _handler(
            429,
            body=b'{"error": {"message": "daily quota exhausted for this key"}}',
            quota_headers=(
                ("X-RateLimit-Limit-Requests", "100"),
                ("X-RateLimit-Remaining-Requests", "0"),
            ),
        )
        servers.append(_start(exhausted))
        registry = Registry({
            "exhausted": _provider(servers[-1].server_address[1], "qe1"),
        })
        executor = _executor(
            registry, decisions=decisions, sleeper=_Sleeper(),
            routing={"policy": {"backoff_jitter": 0}},
        )
        _pin_plan(executor, registry, [("exhausted", "model")])
        result = executor.execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, RouteFailure)
        assert result.status == 429
        auth_values.extend(exhausted.seen_auth)

        # Close the opened routes first: the completion callback backfills
        # the success rows' total_latency_ms (T1 §2.3).
        for route in opened:
            route.close()

        decision_rows, outcome_rows = _ledger(store)
        return _Simulation(store, decision_rows, outcome_rows, auth_values)
    finally:
        for server in servers:
            server.shutdown()


# ------------------------------------------------------- criterion tests ---

def test_1_three_failover_shapes_land_a_complete_ledger(tmp_path):
    sim = _simulate_three_shapes(tmp_path)
    try:
        decisions, outcomes = sim.decision_rows, sim.outcome_rows
        # Four transfers across the three shapes, each a complete pair.
        assert [(d["from_candidate"], d["to_candidate"], d["reason"])
                for d in decisions] == [
            ("rl1/model", "rl2/model", "rate_limited"),          # shape 1
            ("pu1/model", "pu2/model", "provider_unavailable"),  # shape 2, hop 1
            ("pu2/model", "pu3/model", "provider_unavailable"),  # shape 2, hop 2
            ("qe1/model", None, "quota_exhausted"),              # shape 3
        ]
        assert len(outcomes) == len(decisions)
        # Pairing: every outcome points at exactly one decision, in order.
        decision_ids = [d["decision_id"] for d in decisions]
        assert [o["decision_id"] for o in outcomes] == decision_ids
        by_pair = dict(zip(decision_ids, zip(decisions, outcomes)))

        # Shape 1 -- rate_limited 切换成功.
        decision, outcome = by_pair[decision_ids[0]]
        assert decision["request_id"].startswith("ofr_")
        assert decision["requested_model"] == "auto"
        assert decision["strategy"] == "priority"
        assert decision["attempt"] == 1
        assert decision["quota_band"] == "exhausted"
        assert decision["quota_source"] == "upstream_headers"
        assert decision["quota_classification"] == "rate_limited"
        assert outcome["outcome"] == "success"
        assert outcome["status_code"] == 200
        assert outcome["error_kind"] is None
        assert outcome["attempt"] == 2
        assert outcome["ttfb_ms"] >= 0
        assert outcome["total_latency_ms"] is not None  # backfilled on close

        # Shape 2 -- provider_unavailable 链式失败: failed hop then success.
        decision, outcome = by_pair[decision_ids[1]]
        assert outcome["outcome"] == "failed"
        assert outcome["status_code"] == 503
        assert outcome["error_kind"] == "provider_unavailable"
        assert outcome["attempt"] == 2
        decision, outcome = by_pair[decision_ids[2]]
        assert decision["attempt"] == 2
        assert outcome["outcome"] == "success"
        assert outcome["status_code"] == 200
        assert outcome["attempt"] == 3
        assert outcome["total_latency_ms"] is not None

        # Shape 3 -- quota_exhausted 无候选: pair closed by no_candidate.
        decision, outcome = by_pair[decision_ids[3]]
        assert decision["to_candidate"] is None
        assert decision["quota_band"] == "exhausted"
        assert decision["quota_source"] == "upstream_headers"
        assert decision["quota_classification"] == "quota_exhausted"
        assert outcome["outcome"] == "no_candidate"
        assert outcome["status_code"] == 0
        assert outcome["error_kind"] is None
        assert outcome["attempt"] is None

        # Three simulated requests -> exactly three request ids in the ledger.
        assert len({d["request_id"] for d in decisions}) == 3
    finally:
        sim.store.close()


def test_2_doctor_conclusion_lands_on_every_decision(tmp_path):
    sim = _simulate_three_shapes(tmp_path)
    try:
        assert sim.store.latest_doctor_run() == {
            "doctor_verdict": "fail",
            "doctor_failed_checks": 1,
            "doctor_at": DOCTOR_AT,
        }
        # All four decision rows (three shapes, four transfers) carry the
        # same recorded conclusion -- the T3 stamping contract.
        for row in sim.decision_rows:
            assert row["doctor_verdict"] == "fail", row
            assert row["doctor_failed_checks"] == 1, row
            assert row["doctor_at"] == DOCTOR_AT, row
    finally:
        sim.store.close()


def test_3_views_find_the_three_failovers_and_filter(tmp_path, capsys, monkeypatch):
    sim = _simulate_three_shapes(tmp_path)
    try:
        store = sim.store
        # --- failover-log 明细（collect_pairs 口径） ------------------------
        pairs = collect_pairs(store, days=1)
        assert len(pairs) == 4
        assert all(p["pair_status"] == "paired" for p in pairs)
        # 视图按时间倒序：shape 3 最新，shape 1 最旧。
        assert [(p["from_candidate"], p["to_candidate"], p["outcome"])
                for p in pairs] == [
            ("qe1/model", None, "no_candidate"),                # shape 3
            ("pu2/model", "pu3/model", "success"),              # shape 2, hop 2
            ("pu1/model", "pu2/model", "failed"),               # shape 2, hop 1
            ("rl1/model", "rl2/model", "success"),              # shape 1
        ]
        # 每一次模拟换路都可查出：按原因过滤逐一命中三个形态。
        assert [(p["from_candidate"], p["outcome"])
                for p in collect_pairs(store, days=1, reason="rate_limited")] == [
            ("rl1/model", "success"),
        ]
        assert {(p["from_candidate"], p["to_candidate"])
                for p in collect_pairs(store, days=1, reason="provider_unavailable")} == {
            ("pu1/model", "pu2/model"), ("pu2/model", "pu3/model"),
        }
        assert [(p["from_candidate"], p["to_candidate"], p["pair_status"])
                for p in collect_pairs(store, days=1, reason="quota_exhausted")] == [
            ("qe1/model", None, "paired"),
        ]
        assert collect_pairs(store, days=1, reason="transport_error") == []
        # 后端过滤：换路两侧任一命中（pu2 两侧都在链上 -> 两对）。
        assert len(collect_pairs(store, days=1, backend="pu2/model")) == 2
        assert len(collect_pairs(store, days=1, backend="qe1/model")) == 1
        assert collect_pairs(store, days=1, backend="no-such/model") == []

        # --- failover-summary 聚合 ------------------------------------------
        summary = summarize(pairs)
        assert summary["total_pairs"] == 4
        assert summary["incomplete_pairs"] == 0
        assert summary["by_reason"] == {
            "rate_limited": 1, "provider_unavailable": 2, "quota_exhausted": 1,
        }
        assert summary["by_outcome"] == {"success": 2, "failed": 1, "no_candidate": 1}
        assert summary["by_doctor_verdict"] == {"fail": 4}
        # Shape 1 and shape 3 carried numeric exhausted-quota evidence; the
        # two 503 hops had none (unknown / never_measured).
        assert summary["by_quota_band"] == {"exhausted": 2, "unknown": 2}

        # --- CLI 子命令（真实 cmd_failover_log / cmd_failover_summary）------
        from open_free_router.cli import cmd_failover_log, cmd_failover_summary

        monkeypatch.setattr(
            "open_free_router.config.Config",
            lambda: SimpleNamespace(analytics_path=store.path,
                                    analytics_retention_days=30))
        rc = cmd_failover_log(SimpleNamespace(days=1, backend=None, reason=None,
                                              limit=500, json=True))
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["pairs"]) == 4
        assert payload["summary"]["total_pairs"] == 4
        assert payload["summary"]["incomplete_pairs"] == 0

        rc = cmd_failover_log(SimpleNamespace(days=1, backend=None,
                                              reason="rate_limited", limit=500,
                                              json=True))
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert [(p["from_candidate"], p["to_candidate"], p["outcome"])
                for p in payload["pairs"]] == [("rl1/model", "rl2/model", "success")]

        rc = cmd_failover_summary(SimpleNamespace(days=1, backend=None,
                                                  reason=None, limit=500,
                                                  json=True))
        assert rc == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["by_reason"]["quota_exhausted"] == 1
        assert summary["by_outcome"]["no_candidate"] == 1
        assert summary["by_doctor_verdict"]["fail"] == 4

        # 人类可读模式同样能列出该次换路（结果列宽 10 字符，no_candidate
        # 显示为截断前缀 no_candida）。
        rc = cmd_failover_log(SimpleNamespace(days=1, backend="qe1/model",
                                              reason=None, limit=500, json=False))
        assert rc == 0
        out = capsys.readouterr().out
        assert "qe1/model" in out and "quota_exhausted" in out
        assert "no_candida" in out and "paired" in out
        assert "incomplete 0" in out
    finally:
        sim.store.close()


def test_4_incomplete_pair_is_judged_not_fabricated(tmp_path):
    """A destination skipped by the resilience gate settles nothing.

    T1 §2.4 last row: the view judges the pair ``incomplete``; the writer
    neither backfills an outcome nor fabricates one.
    """
    failing_handler = _handler(503)
    healthy_handler = _handler(200)
    failing = _start(failing_handler)
    healthy = _start(healthy_handler)
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    decisions = RouteDecisionStore(analytics=store)
    manager = ResilienceManager(credential_cooldown=60)
    try:
        registry = Registry({
            "failing": _provider(failing.server_address[1], "a"),
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        manager.record_failure(
            "healthy", "model",
            FailureDecision("rate_limited", retryable=True, credential_cooldown=True),
            credential_slot=0,
        )
        executor = _executor(
            registry, resilience=manager, decisions=decisions, sleeper=_Sleeper(),
            routing={"policy": {"backoff_jitter": 0}},
        )
        _pin_plan(executor, registry, [("failing", "model"), ("healthy", "model")])
        result = executor.execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, RouteFailure)
        assert healthy_handler.seen_auth == []  # never attempted

        _, outcomes = _ledger(store)
        assert outcomes == []  # no fabricated outcome row
        pairs = collect_pairs(store, days=1)
        assert len(pairs) == 1
        assert pairs[0]["pair_status"] == "incomplete"
        assert pairs[0]["outcome"] is None
        summary = summarize(pairs)
        assert summary["total_pairs"] == 1
        assert summary["incomplete_pairs"] == 1
        assert summary["by_outcome"] == {}
    finally:
        failing.shutdown()
        healthy.shutdown()
        store.close()


def test_5_no_secrets_reach_the_ledger(tmp_path):
    """无密钥入账: write-side rejection plus a raw-byte scan with zero hits."""
    sim = _simulate_three_shapes(tmp_path)
    store = sim.store
    try:
        # The simulated requests did carry credentials (proof the write path
        # had secrets in hand): every fake upstream saw a non-empty value.
        assert sim.auth_values and all(value for value in sim.auth_values)

        hook = make_audit_hook(RouteDecisionStore(analytics=store))

        # 写入侧拒收 -- layer 2: redlist content is rejected, never masked.
        assert hook.on_failover_decision(
            request_id="ofr_t5_reject", from_candidate="sk-abcdefghijklmnop",
            to_candidate=None, reason="provider_unavailable", attempt=1,
        ) is None
        assert hook.on_failover_outcome(
            decision_id="api_key=placeholder", outcome="success",
        ) is None
        with pytest.raises(ExportSecurityError):
            store.record_doctor_run(
                doctor_verdict="pass", doctor_at="Bearer synthetic-token",
            )

        # Layer 1: keys outside the schema allowlist are dropped, not stored.
        decision_id = hook.on_failover_decision(
            request_id="ofr_t5_allowlist", from_candidate="a/model",
            to_candidate="b/model", reason="rate_limited", attempt=1,
            prompt="synthetic prompt prose", response_body="synthetic body",
            api_key="placeholder-never-stored",
        )
        assert decision_id
        connection = sqlite3.connect(str(store.path))
        try:
            columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(failover_decisions)")}
        finally:
            connection.close()
        assert columns == set(DECISION_COLUMNS) | {'honesty'}  # PRD-174/T3：决策行增 honesty 列

        # The rejections added exactly one decision row (the allowlist one);
        # no outcome row appeared for the rejected writes.
        decisions, outcomes = _ledger(store)
        assert len(decisions) == 5
        assert len(outcomes) == 4

        # 账本文件扫描零命中 -- db / wal / shm 原始字节。
        blob = b"".join(_ledger_files(store))
        assert blob
        # a) the repository redlist pattern itself matches nothing.
        assert SENSITIVE_PATTERN.search(blob.decode("utf-8", "ignore")) is None
        # b) the actual placeholder credentials used by the fake upstreams.
        for prefix in ("placeholder-rl1", "placeholder-rl2", "placeholder-pu1",
                       "placeholder-pu2", "placeholder-pu3", "placeholder-qe1",
                       "placeholder-never-stored", "synthetic-token"):
            assert prefix.encode() not in blob, prefix
        # c) header names / schemes and doctor report prose never land.
        for banned in (b"Authorization", b"Bearer",
                       DOCTOR_LABEL.encode(), DOCTOR_DETAIL.encode(),
                       b"synthetic prompt prose", b"synthetic body"):
            assert banned not in blob, banned
    finally:
        store.close()
