"""PRD-308/T2 路由决策重放包·replay-pack/v1 最小实现测试（trend issue #1552）。

覆盖：schema 组装与字段纪律（缺字段报错不猜、错误码封闭）、fpv1 指纹稳定性、
append-only 账本、verify_replay 二值结论（MATCH 逐字节一致 / MISMATCH 显形带两侧
产物、重放面零写盘零换路）、与学习网关 recommend 出口的端到端装订（同一输入重放
必 MATCH；建议恒 pending 零自动执行）。
全部合成数据零网络零实盘；provider/model/延迟均为占位。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from open_free_router import replay_pack as rp
from open_free_router.gateway_aggregate import aggregate
from open_free_router.gateway_recommend import recommend

GENERATED_AT = "2026-11-07T12:00:00+00:00"
DAY = 86400.0
BASE_TS = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC).timestamp()


def make_db(tmp: Path, events) -> Path:
    import sqlite3

    path = tmp / "usage.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE usage_events (
        event_id TEXT PRIMARY KEY, timestamp REAL NOT NULL, provider TEXT NOT NULL,
        model TEXT NOT NULL, status TEXT NOT NULL, status_code INTEGER NOT NULL,
        ttfb_ms REAL, total_latency_ms REAL, input_tokens INTEGER,
        output_tokens INTEGER, fallback_attempts INTEGER NOT NULL,
        error_kind TEXT NOT NULL, circuit_breaker_events INTEGER NOT NULL)""")
    for e in events:
        conn.execute(
            "INSERT INTO usage_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (e["event_id"], e["timestamp"], e["provider"], e["model"], e["status"],
             e["status_code"], e.get("ttfb_ms"), e.get("total_latency_ms"),
             e.get("input_tokens"), e.get("output_tokens"),
             e.get("fallback_attempts", 0), e.get("error_kind", "none"), 0))
    conn.commit()
    conn.close()
    return path


def evt(eid, ts, provider="remote", model="m-1", status="success",
        ttfb=100.0, latency=500.0, tokens=(100, 50), fallback=0):
    return {"event_id": eid, "timestamp": ts, "provider": provider, "model": model,
            "status": status, "status_code": 200, "ttfb_ms": ttfb,
            "total_latency_ms": latency, "input_tokens": tokens[0],
            "output_tokens": tokens[1], "fallback_attempts": fallback}


def events_two_backends(n_a=40, n_b=35, *, days=5, success_b=1.0):
    """两个后端各 n≥30、覆盖 ≥3 天；B 全面占优（成功率/延迟/成本）。"""
    events = []
    for i in range(n_a):
        ts = BASE_TS + (i % days) * DAY + i
        events.append(evt(f"a-{i:03d}", ts, "remote", "model-a", "success",
                          ttfb=200.0, latency=1000.0, tokens=(200, 100)))
    for i in range(n_b):
        ts = BASE_TS + (i % days) * DAY + i
        status = "success" if (i / max(1, n_b)) < success_b else "error"
        events.append(evt(f"b-{i:03d}", ts, "remote", "model-b", status,
                          ttfb=100.0, latency=600.0, tokens=(100, 50)))
    return events


def _run(tmp: Path):
    agg = aggregate(make_db(tmp, events_two_backends()), window_days=14,
                    now=datetime.fromisoformat(GENERATED_AT))
    return agg, recommend(agg, generated_at=GENERATED_AT)


def _rerun(inputs: dict, decision: dict) -> dict:
    """verify_replay 契约：rerun(inputs, decision) 返回与账本 output 同形状视图。"""
    run = recommend(inputs["aggregate"], generated_at=inputs["generated_at"])
    return rp.output_view_of_route_recommendation(run)


def test_schema_assembly_and_fingerprint_stability(tmp_path):
    agg, run = _run(tmp_path)
    p1 = rp.pack_from_route_recommendation(agg, run, generated_at=GENERATED_AT)
    p2 = rp.pack_from_route_recommendation(agg, run, generated_at=GENERATED_AT)
    assert p1["schema_version"] == "replay-pack/v1"
    assert p1["action"] == "route_decision"
    assert p1["input"]["snapshot_fingerprint"] == p2["input"]["snapshot_fingerprint"]
    assert p1["input"]["snapshot_fingerprint"].startswith("fpv1:")
    assert p1["replay"]["expect"] == "byte-identical"
    assert p1["replay"]["status"] == rp.REPLAY_NOT_RUN
    changed = dict(agg, aggregate=list(agg["aggregate"])[:-1])
    p3 = rp.pack_from_route_recommendation(changed, run, generated_at=GENERATED_AT)
    assert p3["input"]["snapshot_fingerprint"] != p1["input"]["snapshot_fingerprint"]


def test_decision_records_chosen_why_rules_version(tmp_path):
    agg, run = _run(tmp_path)
    pack = rp.pack_from_route_recommendation(agg, run, generated_at=GENERATED_AT)
    recs = [r for r in run["records"] if r["kind"] == "recommendation"]
    assert recs, "fixture 应至少产出一条推荐"
    assert recs[0]["scope"]["prompt_class"] in pack["decision"]["chosen"]
    assert "现任" in pack["decision"]["why"] and "→" in pack["decision"]["why"]
    assert pack["decision"]["rules_version"] == run["metric_version"]
    assert pack["output"]["result"]["recommendation_ids"] == [
        r["recommendation_id"] for r in recs
    ]
    assert pack["output"]["key_numbers"]["auto_apply"] is False  # 零自动执行


def test_field_discipline_errors_are_closed_vocabulary():
    good = {"action": "route_decision", "inputs": {"k": "v"},
            "decision": {"chosen": "c", "why": "w", "rules_version": "v1"},
            "output": {"result": "r", "key_numbers": {"n": 1}}, "replay_command": "rerun: x"}
    with pytest.raises(rp.ReplayPackError) as ei:
        rp.build_replay_pack(**{**good, "inputs": {}})
    assert ei.value.code == "REPLAY_INPUT_REQUIRED"
    for drop in ("chosen", "why", "rules_version"):
        decision = {k: v for k, v in good["decision"].items() if k != drop}
        with pytest.raises(rp.ReplayPackError) as ed:
            rp.build_replay_pack(**{**good, "decision": decision})
        assert ed.value.code == "REPLAY_DECISION_FIELDS_REQUIRED"
    with pytest.raises(rp.ReplayPackError) as eo:
        rp.build_replay_pack(**{**good, "output": {"result": "r"}})
    assert eo.value.code == "REPLAY_OUTPUT_REQUIRED"
    with pytest.raises(rp.ReplayPackError) as ec:
        rp.build_replay_pack(**{**good, "replay_command": ""})
    assert ec.value.code == "REPLAY_COMMAND_INVALID"
    with pytest.raises(ValueError, match="未登记错误码"):
        rp.ReplayPackError("NOT_IN_TABLE", "x")
    assert len(set(rp.ERROR_CODES)) == len(rp.ERROR_CODES)


def test_ledger_append_only_and_cold_start(tmp_path):
    agg, run = _run(tmp_path)
    pack = rp.pack_from_route_recommendation(agg, run, generated_at=GENERATED_AT)
    ledger = rp.ReplayLedger(tmp_path / "ledger.jsonl")
    assert ledger.read_all() == []
    ledger.append(pack)
    before = ledger.bytes_now()
    ledger.append(pack)
    packs = ledger.read_all()
    assert len(packs) == 2 and packs[0] == pack
    assert len(ledger.bytes_now()) > len(before)


def test_verify_match_is_byte_identical_and_read_only(tmp_path):
    agg, run = _run(tmp_path)
    pack = rp.pack_from_route_recommendation(agg, run, generated_at=GENERATED_AT)
    ledger = rp.ReplayLedger(tmp_path / "ledger.jsonl")
    ledger.append(pack)
    before = ledger.bytes_now()
    result = rp.verify_replay(pack, _rerun)
    assert result["status"] == rp.REPLAY_MATCH
    assert result["evidence"] is None
    assert ledger.bytes_now() == before  # 重放面只读：零写盘


def test_verify_mismatch_is_explicit_with_both_sides(tmp_path):
    agg, run = _run(tmp_path)
    pack = rp.pack_from_route_recommendation(agg, run, generated_at=GENERATED_AT)

    def drifted(inputs, decision):  # 模拟重放对不上：账本即失败证据
        run_now = recommend(inputs["aggregate"], generated_at=inputs["generated_at"])
        view = rp.output_view_of_route_recommendation(run_now)
        return dict(view, result=dict(view["result"], recommendation_ids=[]))

    result = rp.verify_replay(pack, drifted)
    assert result["status"] == rp.REPLAY_MISMATCH
    assert result["evidence"]["ledger"]["result"]["recommendation_ids"]
    assert result["evidence"]["rerun"]["result"]["recommendation_ids"] == []


def test_ledger_roundtrip_replay_still_match(tmp_path):
    agg, run = _run(tmp_path)
    pack = rp.pack_from_route_recommendation(agg, run, generated_at=GENERATED_AT)
    ledger = rp.ReplayLedger(tmp_path / "ledger.jsonl")
    ledger.append(pack)
    reloaded = rp.ReplayLedger(tmp_path / "ledger.jsonl").read_all()[0]
    assert rp.verify_replay(reloaded, _rerun)["status"] == rp.REPLAY_MATCH
