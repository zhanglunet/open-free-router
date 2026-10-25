"""PRD-149/T4: failover 审计查询与汇总视图测试（trend #748）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from open_free_router.analytics import AnalyticsStore
from open_free_router.failover_audit import AnalyticsAuditHook
from open_free_router.failover_view import collect_pairs, summarize

NOW = datetime.now(timezone.utc) - timedelta(hours=2)  # 窗口随墙钟滚动：种子必须用相对时间（硬编码日期会随时间推移落出 days 窗口）


def _seed(store: AnalyticsStore) -> None:
    hook = AnalyticsAuditHook(store)
    d1 = hook.on_failover_decision(
        at=NOW.isoformat(), request_id="req-1", from_candidate="a.example.test",
        to_candidate="b.example.test", reason="rate_limited", attempt=1,
        elapsed_ms=10, ttfb_ms=None, circuit=None,
        quota_band="loose", quota_source="config", quota_classification=None,
        doctor_verdict="pass", doctor_failed_checks=0, doctor_at=NOW.isoformat())
    hook.on_failover_outcome(
        decision_id=d1, settled_at=(NOW + timedelta(seconds=2)).isoformat(),
        outcome="success", status_code=200, error_kind=None,
        ttfb_ms=120, total_latency_ms=800, attempt=1)
    d2 = hook.on_failover_decision(
        at=NOW.isoformat(), request_id="req-2", from_candidate="b.example.test",
        to_candidate="c.example.test", reason="provider_unavailable", attempt=1,
        elapsed_ms=10, ttfb_ms=None, circuit=None,
        quota_band="tight", quota_source="config", quota_classification=None,
        doctor_verdict="warn", doctor_failed_checks=1, doctor_at=NOW.isoformat())
    # d2 不写 outcome → incomplete


def test_collect_pairs_and_incomplete(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db")
    _seed(store)
    pairs = collect_pairs(store, days=1)
    assert len(pairs) == 2
    by_to = {p["to_candidate"]: p for p in pairs}
    assert by_to["b.example.test"]["pair_status"] == "paired"
    assert by_to["b.example.test"]["outcome"] == "success"
    assert by_to["c.example.test"]["pair_status"] == "incomplete"
    assert by_to["c.example.test"]["outcome"] is None


def test_backend_and_reason_filters(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db")
    _seed(store)
    assert all("b.example.test" in (p["from_candidate"], p["to_candidate"])
               for p in collect_pairs(store, days=1, backend="b.example.test"))
    assert len(collect_pairs(store, days=1, reason="rate_limited")) == 1
    assert collect_pairs(store, days=1, reason="quota_exhausted") == []


def test_summarize_dimensions(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db")
    _seed(store)
    s = summarize(collect_pairs(store, days=1))
    assert s["total_pairs"] == 2 and s["incomplete_pairs"] == 1
    assert s["by_backend"]["c.example.test"] == 1
    assert s["by_reason"]["rate_limited"] == 1
    assert s["by_quota_band"]["tight"] == 1
    assert s["by_doctor_verdict"]["warn"] == 1
    assert s["by_outcome"]["success"] == 1


def test_cli_cmds_json_and_table(tmp_path, capsys, monkeypatch):
    from argparse import Namespace

    from open_free_router.cli import cmd_failover_log, cmd_failover_summary
    import open_free_router.cli as cli_mod

    store = AnalyticsStore(tmp_path / "usage.db")
    _seed(store)
    monkeypatch.setattr(
        "open_free_router.config.Config",
        lambda: Namespace(analytics_path=tmp_path / "usage.db",
                          analytics_retention_days=30))
    rc = cmd_failover_summary(Namespace(days=1, backend=None, reason=None,
                                        limit=500, json=True))
    assert rc == 0
    s = json.loads(capsys.readouterr().out)
    assert s["total_pairs"] == 2 and s["incomplete_pairs"] == 1

    rc = cmd_failover_log(Namespace(days=1, backend="c.example.test", reason=None,
                                    limit=500, json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "incomplete" in out and "c.example.test" in out
    assert cli_mod  # noqa: B018 — 引用占位（保持 import 区可读）


# ---------- PRD-174/T4：按标注等级过滤与展示（trend #873） ----------

def test_honesty_filter_and_summary_dimension(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db")
    _seed(store)
    seeded = collect_pairs(store, days=1)
    # _seed 的 doctor_verdict=pass/warn 都是 severity-count 推导 → inferred
    assert all(p["honesty"] == "inferred" for p in seeded)
    assert collect_pairs(store, days=1, honesty="ambiguous") == []
    s = summarize(seeded)
    assert s["by_honesty"] == {"inferred": 2}


def test_unlabeled_filter_for_legacy_rows(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db")
    _seed(store)
    import sqlite3
    conn = sqlite3.connect(tmp_path / "usage.db")
    conn.execute("UPDATE failover_decisions SET honesty = NULL")
    conn.commit(); conn.close()
    assert len(collect_pairs(store, days=1, honesty="unlabeled")) == 2
    assert collect_pairs(store, days=1, honesty="pass") == []


def test_cli_honesty_flag(tmp_path, capsys, monkeypatch):
    from argparse import Namespace
    from open_free_router.cli import cmd_failover_log
    store = AnalyticsStore(tmp_path / "usage.db")
    _seed(store)
    import open_free_router.config as config_mod
    monkeypatch.setattr(config_mod, "Config",
                        lambda: Namespace(analytics_path=tmp_path / "usage.db",
                                          analytics_retention_days=30))
    rc = cmd_failover_log(Namespace(days=1, backend=None, reason=None,
                                    limit=500, json=False, honesty="inferred"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "incomplete" in out and "rate_limited" in out
