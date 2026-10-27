"""PRD-174/T5 acceptance: 模拟 doctor+换路——标注正确且无密钥入账.

trend #874, adopted PRD-174（``adopted/PRD-174-ofr-doctor-honest-labeling.md``）.
The acceptance objects are the T2-T4 changes already merged into main (doctor
conclusion labels, the additive ledger columns, the query filters).  T5
discipline: tests and documentation only, so nothing here fixes them; every
criterion maps to one numbered test:

| # | 验收项 | 测试 |
|---|---|---|
| 1 | 模拟 doctor：真实 ``cmd_doctor --json`` 每条结论带标注且符合 T1 §3.2 矩阵（实测/推断/存疑各就各位），``honesty_summary`` 与报告一致 | ``test_1_doctor_run_labels_match_the_matrix`` |
| 2 | 模拟 doctor+换路：真实 doctor 结论入账后，换路决策行标注按最保守原则推导正确（doctor 主导→推断 / 仅结构化额度头→推断 / 无信号→存疑），存量行不回填 | ``test_2_failover_decision_honesty_is_correct`` |
| 3 | 账本字段齐全含 honesty：``failover_decisions.honesty`` + ``doctor_state.honesty_summary`` 在 v4 schema 上齐全，doctor 运行的标注计数经生产路径落账 | ``test_3_ledger_fields_complete_including_honesty`` |
| 4 | 查询可按等级过滤：``collect_pairs``/``failover-log``/``failover-summary`` 按标注过滤正确，存量 NULL 行仅 ``unlabeled`` 可见，分布列正确 | ``test_4_query_filters_by_honesty_level`` |
| 5 | 无密钥入账 + 文档无内网主机名：写入侧门禁（越界标注折叠、密钥拒收）+ 账本原始字节零命中 + PRD-174 交付面占位符扫描 | ``test_5_no_secrets_enter_the_ledger_and_docs_stay_placeholder_clean`` |

The doctor half runs the real ``cmd_doctor`` (offline: the loopback status
probe is stubbed, the registry is a placeholder file) and lands its
conclusion through the production ``_record_doctor_conclusion`` path into
the **same** ``usage.db`` the failover half simulates against.  The
failover half runs the real ``UpstreamExecutor`` over throwaway loopback
HTTP servers with fake keys.  No real hostnames, accounts or keys appear
anywhere (assertion 5 enforces that discipline on the repo itself).
"""
from __future__ import annotations

import itertools
import json
import re
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
from open_free_router.cli import cmd_doctor, cmd_failover_log, cmd_failover_summary
from open_free_router.executor import OpenedRoute, UpstreamExecutor
from open_free_router.failover_audit import (
    HONESTY_LABELS,
    doctor_summary_from_report,
    honesty_summary_from_report,
    make_audit_hook,
)
from open_free_router.failover_view import collect_pairs, summarize
from open_free_router.registry import Registry
from open_free_router.resilience import ResilienceManager
from open_free_router.routing import RoutePlanner
from open_free_router.telemetry import RouteDecisionStore

FAKE_KEY = "test-key-0123456789abcdef"  # 本模块自造的假密钥，绝不代表任何真实凭据

DECISION_COLUMNS = (
    "decision_id", "at", "request_id", "from_candidate", "to_candidate",
    "reason", "attempt", "elapsed_ms", "ttfb_ms", "circuit",
    "requested_model", "strategy", "quota_band", "quota_source",
    "quota_classification", "doctor_verdict", "doctor_failed_checks", "doctor_at",
    "honesty",
)
DOCTOR_STATE_REQUIRED = {"id", "at", "verdict", "failed_checks", "honesty_summary"}


# ------------------------------------------------------------- fake upstreams ---

class _Handler(BaseHTTPRequestHandler):
    """Configurable throwaway upstream. Placeholder credentials only."""

    status = 200
    quota_headers = ()
    seen_auth: list[str] = []

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).seen_auth.append(self.headers.get("Authorization") or "")
        body = b'{"model": "model", "ok": true}'
        if self.status >= 400:
            body = b'{"error": {"message": "synthetic failure"}}'
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for header, value in self.quota_headers:
            self.send_header(header, value)
        self.end_headers()
        self.wfile.write(body)


_HANDLER_SEQ = itertools.count(1)


def _handler(status=200, quota_headers=()):
    return type(
        f"HonestyAcceptanceHandler{next(_HANDLER_SEQ)}",
        (_Handler,),
        {"status": status, "quota_headers": quota_headers, "seen_auth": []},
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


def _store(path):
    return AnalyticsStore(path, retention_days=30)


def _ledger(store):
    connection = sqlite3.connect(str(store.path))
    try:
        return [
            dict(zip(DECISION_COLUMNS, row))
            for row in connection.execute(
                f"SELECT {', '.join(DECISION_COLUMNS)} FROM failover_decisions "
                "ORDER BY rowid")
        ]
    finally:
        connection.close()


def _ledger_files(store):
    """Raw bytes of usage.db plus its WAL/SHM siblings (zero-hit scan input)."""
    paths = [store.path, Path(f"{store.path}-wal"), Path(f"{store.path}-shm")]
    return [path.read_bytes() for path in paths if path.exists()]


# ------------------------------------------------- real cmd_doctor harness ---

HEALTHY_PROVIDER = (
    "provider-a:\n"
    "  base_url: https://<provider-a-host>/v1\n"
    "  api_key: test-placeholder\n"
    "  models:\n"
    "    - id: model-x\n"
)


def _doctor_config(tmp_path, monkeypatch, data_dir, registry=HEALTHY_PROVIDER,
                   reachable=True, routing=""):
    """Point a real Config at tmp files; doctor writes its conclusion there."""
    config = tmp_path / "config.yaml"
    config.write_text(
        "registry: registry.yaml\n"
        f"data_dir: {data_dir}\n"
        + routing,
        encoding="utf-8",
    )
    (tmp_path / "registry.yaml").write_text(registry, encoding="utf-8")
    monkeypatch.setenv("OPEN_FREE_ROUTER_CONFIG", str(config))
    monkeypatch.setattr(
        "open_free_router.cli._status_payload",
        lambda cfg: {
            "providers": 1, "providers_with_key": 1, "models": 1,
            "proxy_reachable": reachable, "ui_reachable": reachable,
            "proxy_url": "http://127.0.0.1:8337",
            "ui_url": "http://127.0.0.1:9057",
        },
    )


def _run_doctor_json(monkeypatch, capsys):
    try:
        cmd_doctor(SimpleNamespace(json=True))
        exit_code = 0
    except SystemExit as stopped:
        exit_code = stopped.code
    return json.loads(capsys.readouterr().out), exit_code


# ------------------------------------------------------- criterion tests ---

def test_1_doctor_run_labels_match_the_matrix(tmp_path, monkeypatch, capsys):
    """模拟 doctor：真实 cmd_doctor --json 逐条标注符合 T1 §3.2 矩阵."""
    # 不可达探测 → 存疑；空端点 + 缺失别名候选 → 推断；文件/注册表读取 → 实测。
    declared_only = (
        "provider-b:\n"
        "  api_key: test-placeholder\n"  # 空端点 → protocol_matrix invalid_upstream_url
        "  models:\n"
        "    - id: model-x\n"
    )
    routing = "routing:\n  aliases:\n    preferred:\n      candidates: [missing]\n"
    _doctor_config(tmp_path, monkeypatch, tmp_path / "data",
                   registry=HEALTHY_PROVIDER + declared_only, reachable=False,
                   routing=routing)
    report, exit_code = _run_doctor_json(monkeypatch, capsys)
    assert exit_code == 1  # error 级问题存在（空端点/缺失候选）

    # 每条结论都带标注，且取值在枚举内（缺失标注即实现 bug，T1 §3.1）。
    for group in ("checks", "routing", "free_tier", "protocol_matrix"):
        items = report[group]
        if group in ("free_tier", "protocol_matrix"):
            items = items["issues"]
        for item in items:
            assert item["honesty"] in HONESTY_LABELS, (group, item)

    # 矩阵逐行：文件存在性/注册表计数=实测；不可达探测=存疑（不沿用上次）；
    # 协议矩阵与路由推导=推断；顶层汇总=推断。
    by_label = {item["label"]: item for item in report["checks"]}
    assert by_label["registry.yaml found"]["honesty"] == "extracted"
    assert by_label["config.yaml found"]["honesty"] == "extracted"
    for label in ("proxy reachable", "dashboard reachable"):
        assert by_label[label]["honesty"] == "ambiguous", label
        assert "上次" not in by_label[label]["detail"]
    assert report["protocol_matrix"]["issues"], "expected a declared-config issue"
    assert all(issue["honesty"] == "inferred"
               for issue in report["protocol_matrix"]["issues"])
    assert report["routing"] and all(issue["honesty"] == "inferred"
                                     for issue in report["routing"])
    assert report["honesty"] == "inferred"

    # honesty_summary 与报告一致（生产 reducer 对全报告重算相等）。
    assert report["honesty_summary"] == honesty_summary_from_report(report)
    for label in ("extracted", "inferred", "ambiguous"):
        assert report["honesty_summary"][label] >= 2, label


def test_2_failover_decision_honesty_is_correct(tmp_path, monkeypatch, capsys):
    """模拟 doctor+换路：决策行标注按最保守原则推导正确，doctor 结论入账."""
    data_dir = tmp_path / "data"
    failing = _start(_handler(429, quota_headers=(
        ("X-RateLimit-Limit-Requests", "100"),
        ("X-RateLimit-Remaining-Requests", "0"),
    )))
    healthy = _start(_handler(200))
    bare = _start(_handler(429))
    try:
        store = _store(data_dir / "usage.db")

        # --- 阶段 0：无 doctor 结论、429 无任何头（event）→ 无信号 → 存疑 ----
        registry = Registry({
            "failing": _provider(bare.server_address[1], "e1"),
            "healthy": _provider(healthy.server_address[1], "e2"),
        })
        planner = RoutePlanner(
            registry,
            {"fallback": {"max_attempts": 5}, "policy": {"backoff_jitter": 0}})
        executor = UpstreamExecutor(
            planner, ResilienceManager(),
            decisions=RouteDecisionStore(analytics=store),
            sleeper=_Sleeper(), timeout=5)
        result = executor.execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        result.close()

        # --- 阶段 1：仍无 doctor、429 带结构化额度头（upstream_headers）-------
        registry = Registry({
            "failing": _provider(failing.server_address[1], "h1"),
            "healthy": _provider(healthy.server_address[1], "h2"),
        })
        planner = RoutePlanner(
            registry,
            {"fallback": {"max_attempts": 5}, "policy": {"backoff_jitter": 0}})
        executor = UpstreamExecutor(
            planner, ResilienceManager(),
            decisions=RouteDecisionStore(analytics=store),
            sleeper=_Sleeper(), timeout=5)
        result = executor.execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        result.close()

        # --- 阶段 2：真实 cmd_doctor 经生产路径落结论进同一 usage.db ----------
        _doctor_config(tmp_path, monkeypatch, data_dir)
        report, _ = _run_doctor_json(monkeypatch, capsys)
        summary = doctor_summary_from_report(report)
        assert summary is not None

        # --- 阶段 3：doctor 之后再次换路 → 结论盖章 + 推断 -------------------
        registry = Registry({
            "failing": _provider(failing.server_address[1], "a1"),
            "healthy": _provider(healthy.server_address[1], "a2"),
        })
        planner = RoutePlanner(
            registry,
            {"fallback": {"max_attempts": 5}, "policy": {"backoff_jitter": 0}})
        executor = UpstreamExecutor(
            planner, ResilienceManager(),
            decisions=RouteDecisionStore(analytics=store),
            sleeper=_Sleeper(), timeout=5)
        result = executor.execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        result.close()

        rows = _ledger(store)
        assert len(rows) == 3
        no_doctor_event, no_doctor_headers, after_doctor = rows

        # 阶段 0：无信号 → 存疑；doctor 列为诚实缺省 never_run（不编造结论）。
        assert no_doctor_event["doctor_verdict"] == "never_run"
        assert no_doctor_event["quota_source"] == "event"
        assert no_doctor_event["honesty"] == "ambiguous"
        # 阶段 1：本轮真实解析了额度头 → 推断。
        assert no_doctor_headers["doctor_verdict"] == "never_run"
        assert no_doctor_headers["quota_source"] == "upstream_headers"
        assert no_doctor_headers["quota_band"] == "exhausted"
        assert no_doctor_headers["honesty"] == "inferred"
        # 阶段 3：真实 doctor 结论盖章 → 推断。
        assert after_doctor["doctor_verdict"] == summary["doctor_verdict"]
        assert after_doctor["doctor_failed_checks"] == summary["doctor_failed_checks"]
        assert after_doctor["doctor_at"]
        assert after_doctor["honesty"] == "inferred"
        # 存量行不回填：阶段 0/1 的 doctor 结论保持 never_run，未被追溯改写。
        assert no_doctor_event["doctor_verdict"] == "never_run"
        assert no_doctor_headers["doctor_verdict"] == "never_run"
        store.close()
    finally:
        failing.shutdown()
        healthy.shutdown()
        bare.shutdown()


def test_3_ledger_fields_complete_including_honesty(tmp_path, monkeypatch, capsys):
    """账本字段齐全含 honesty：v4 schema 两列齐全，标注计数经生产路径落账."""
    data_dir = tmp_path / "data"
    _doctor_config(tmp_path, monkeypatch, data_dir)
    report, _ = _run_doctor_json(monkeypatch, capsys)
    summary = doctor_summary_from_report(report)

    store = _store(data_dir / "usage.db")
    try:
        connection = sqlite3.connect(str(store.path))
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            decision_columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(failover_decisions)")}
            doctor_columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(doctor_state)")}
            verdict, failed_checks, summary_json = connection.execute(
                "SELECT verdict, failed_checks, honesty_summary FROM doctor_state "
                "WHERE id = 1").fetchone()
        finally:
            connection.close()

        # 纯加法迁移完成：v4，决策行带 honesty，doctor 行带 honesty_summary。
        assert version == 4
        assert decision_columns == set(DECISION_COLUMNS)
        assert DOCTOR_STATE_REQUIRED <= doctor_columns
        # 真实 cmd_doctor 的结论经 _record_doctor_conclusion 落账：聚合三元组
        # + 标注计数（≤128 字符紧凑 JSON），与报告逐项一致。
        assert verdict == summary["doctor_verdict"]
        assert failed_checks == summary["doctor_failed_checks"]
        assert json.loads(summary_json) == report["honesty_summary"]
        assert len(summary_json) <= 128
        # 标注是枚举一个词，不是自由文本口子：账本只有三键计数。
        assert set(json.loads(summary_json)) == {"extracted", "inferred", "ambiguous"}
    finally:
        store.close()


def test_4_query_filters_by_honesty_level(tmp_path, monkeypatch, capsys):
    """查询可按等级过滤：视图/CLI 按标注过滤正确，存量 NULL 仅 unlabeled 可见."""
    store = _store(tmp_path / "usage.db")
    bare = _start(_handler(429))
    headed = _start(_handler(429, quota_headers=(
        ("X-RateLimit-Limit-Requests", "100"),
        ("X-RateLimit-Remaining-Requests", "0"),
    )))
    healthy = _start(_handler(200))

    def _one(failing_port, prefixes):
        registry = Registry({
            "failing": _provider(failing_port, prefixes[0]),
            "healthy": _provider(healthy.server_address[1], prefixes[1]),
        })
        planner = RoutePlanner(
            registry,
            {"fallback": {"max_attempts": 5}, "policy": {"backoff_jitter": 0}})
        executor = UpstreamExecutor(
            planner, ResilienceManager(),
            decisions=RouteDecisionStore(analytics=store),
            sleeper=_Sleeper(), timeout=5)
        result = executor.execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        result.close()

    try:
        _one(bare.server_address[1], ("qf1", "qf2"))    # event → 存疑
        _one(headed.server_address[1], ("qh1", "qh2"))  # headers → 推断
        # 模拟一行 PRD-174 之前的存量行（NULL 永不回填）。
        rows = _ledger(store)
        assert [row["honesty"] for row in rows] == ["ambiguous", "inferred"]
        connection = sqlite3.connect(str(store.path))
        try:
            connection.execute(
                "UPDATE failover_decisions SET honesty = NULL WHERE rowid = 1")
            connection.commit()
        finally:
            connection.close()

        # 无过滤：全部可见（含 NULL 行）。
        pairs = collect_pairs(store, days=1)
        assert len(pairs) == 2
        assert sorted(str(p["honesty"]) for p in pairs) == ["None", "inferred"]
        # collect_pairs 按等级过滤：完全相等才命中。
        inferred = collect_pairs(store, days=1, honesty="inferred")
        assert [(p["from_candidate"], p["honesty"])
                for p in inferred] == [("qh1/model", "inferred")]
        ambiguous = collect_pairs(store, days=1, honesty="ambiguous")
        assert ambiguous == []  # 存疑行已被置 NULL，不再是 ambiguous
        # 存量 NULL 行仅在 honesty="unlabeled" 时返回（T4 契约）。
        unlabeled = collect_pairs(store, days=1, honesty="unlabeled")
        assert [p["from_candidate"] for p in unlabeled] == ["qf1/model"]
        assert all(p["honesty"] is None for p in unlabeled)
        assert collect_pairs(store, days=1, honesty="extracted") == []
        assert collect_pairs(store, days=1, honesty="not-a-level") == []

        # 聚合视图 by_honesty 分布（NULL 不计入，与 T4 口径一致）。
        summary = summarize(collect_pairs(store, days=1))
        assert summary["by_honesty"] == {"inferred": 1}
        assert summary["total_pairs"] == 2

        # CLI 子命令（真实 cmd_failover_log / cmd_failover_summary）。
        monkeypatch.setattr(
            "open_free_router.config.Config",
            lambda: SimpleNamespace(analytics_path=store.path,
                                    analytics_retention_days=30))
        rc = cmd_failover_log(SimpleNamespace(days=1, backend=None, reason=None,
                                              limit=500, json=True, honesty=None))
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["summary"]["by_honesty"] == {"inferred": 1}

        rc = cmd_failover_log(SimpleNamespace(days=1, backend=None, reason=None,
                                              limit=500, json=True,
                                              honesty="inferred"))
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert [(p["from_candidate"], p["honesty"])
                for p in payload["pairs"]] == [("qh1/model", "inferred")]

        rc = cmd_failover_summary(SimpleNamespace(days=1, backend=None, reason=None,
                                                  limit=500, json=True,
                                                  honesty="unlabeled"))
        assert rc == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["total_pairs"] == 1
        assert summary["by_honesty"] == {}

        # 人读模式同样按等级过滤（无命中时给出口径提示）。
        rc = cmd_failover_log(SimpleNamespace(days=1, backend=None, reason=None,
                                              limit=500, json=False,
                                              honesty="extracted"))
        assert rc == 0
        assert "窗口内无换路记录" in capsys.readouterr().out
        store.close()
    finally:
        bare.shutdown()
        headed.shutdown()
        healthy.shutdown()


def test_5_no_secrets_enter_the_ledger_and_docs_stay_placeholder_clean(
        tmp_path, monkeypatch, capsys):
    """无密钥入账（门禁 + 原始字节零命中）+ 文档无内网主机名（占位符纪律）."""
    failing_h = _handler(429, quota_headers=(
        ("X-RateLimit-Limit-Requests", "100"),
        ("X-RateLimit-Remaining-Requests", "0"),
    ))
    failing = _start(failing_h)
    healthy_h = _handler(200)
    healthy = _start(healthy_h)
    try:
        store = _store(tmp_path / "usage.db")
        registry = Registry({
            "failing": _provider(failing.server_address[1], "ns1"),
            "healthy": _provider(healthy.server_address[1], "ns2"),
        })
        planner = RoutePlanner(
            registry,
            {"fallback": {"max_attempts": 5}, "policy": {"backoff_jitter": 0}})
        executor = UpstreamExecutor(
            planner, ResilienceManager(),
            decisions=RouteDecisionStore(analytics=store),
            sleeper=_Sleeper(), timeout=5)
        result = executor.execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        result.close()
        # 凭据确实在场上：两个上游都看到了占位假密钥（写入侧有密钥可用）。
        assert failing_h.seen_auth == ["Bearer placeholder-ns1"]
        assert healthy_h.seen_auth == ["Bearer placeholder-ns2"]
        rows = _ledger(store)
        assert rows and rows[0]["reason"] == "rate_limited"

        hook = make_audit_hook(RouteDecisionStore(analytics=store))

        # 写入门禁（T1 §6 锚点 5）：越界标注折叠为存疑，行仍写入不拒收。
        decision_id = hook.on_failover_decision(
            request_id="ofr_t5_fold", from_candidate="a/model",
            to_candidate="b/model", reason="rate_limited", attempt=1,
            honesty="EXTRACTED ",  # 大写/带空格 → 折叠，原值绝不入账
        )
        assert decision_id
        folded = _ledger(store)
        assert folded[-1]["honesty"] == "ambiguous"

        # 层 1 allowlist：schema 外的密钥/正文字段直接丢弃，列集合不变。
        hook.on_failover_decision(
            request_id="ofr_t5_allowlist", from_candidate="a/model",
            to_candidate="b/model", reason="rate_limited", attempt=1,
            api_key=FAKE_KEY, prompt="synthetic prompt prose",
        )
        connection = sqlite3.connect(str(store.path))
        try:
            columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(failover_decisions)")}
        finally:
            connection.close()
        assert columns == set(DECISION_COLUMNS)

        # 层 2 拒收：红名单内容整行拒收；doctor 结论同样拒收。
        assert hook.on_failover_decision(
            request_id="ofr_t5_reject", from_candidate="sk-abcdefghijklmnop",
            to_candidate=None, reason="provider_unavailable", attempt=1,
        ) is None
        with pytest.raises(ExportSecurityError):
            store.record_doctor_run(doctor_verdict="pass",
                                    doctor_at="Bearer synthetic-token")

        # 账本文件原始字节扫描零命中：db / wal / shm。
        blob = b"".join(_ledger_files(store))
        assert blob
        decoded = blob.decode("utf-8", "ignore")
        assert SENSITIVE_PATTERN.search(decoded) is None
        for banned in (FAKE_KEY, "placeholder-ns1", "placeholder-ns2",
                       "Authorization", "Bearer", "synthetic prompt prose"):
            assert banned not in decoded, banned
        store.close()
    finally:
        failing.shutdown()
        healthy.shutdown()

    # 文档无内网主机名：PRD-174 交付面（T1 规范稿 + doctor/账本/查询测试 +
    # 本验收文件）无私网 IP、密钥样式、内网主机名后缀；环回与占位符允许。
    repo = Path(__file__).resolve().parents[1]
    surfaces = [
        repo / "docs" / "plans" / "2026-10-25-doctor-honest-labeling.md",
        repo / "tests" / "test_doctor_honesty.py",
        repo / "tests" / "test_doctor_ledger_honesty.py",
        repo / "tests" / "test_failover_view.py",
        repo / "tests" / "test_doctor_honesty_acceptance.py",
    ]
    banned_patterns = {
        "private-ip": re.compile(
            r"\b(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+"
            r"|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)\b"),
        "key-material": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|AKIA[0-9A-Z]{16})\b"),
        "internal-host": re.compile(
            r"https?://[^\s\"'<>)]*\.(?:local|lan|internal|corp|intranet|priv)\b"),
    }
    # 仓库红名单的合成样本（本文件与既有安全测试用的 sk-abcdefghijklmnop[1234]）
    # 是测「写入侧拒收」的夹具，不是真实凭据：文档面零容忍，测试面仅豁免此类。
    synthetic_fixtures = re.compile(r"sk-abcdefghijklmnop(?:1234)?")
    for surface in surfaces:
        text = surface.read_text(encoding="utf-8")
        for name, pattern in banned_patterns.items():
            hits = pattern.findall(text)
            if name == "key-material" and surface.suffix == ".py":
                hits = [hit for hit in hits if not synthetic_fixtures.fullmatch(hit)]
            assert hits == [], f"{name} in {surface.name}: {hits}"
