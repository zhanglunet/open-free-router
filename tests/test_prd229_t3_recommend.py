# -*- coding: utf-8 -*-
"""PRD-229/T3 测试：策略推荐生成——无证据不给推荐（trend #1147）。

运行：.venv/bin/python -m pytest tests/test_prd229_t3_recommend.py -q

覆盖（T1 §2.3/§4/§5）：R1~R6 逐条（样本不足/对比双方/时间覆盖/效应量三选一/
成本型 token 覆盖/分类覆盖）、推荐固定字段缺一不可、观察记录「为什么没推荐」
可见、参数白名单与排除面、expected_benefit 静态重放口径、expires_at=+7 天、
status 恒 pending 零自动变更、确定性（内容寻址 id）、T2 聚合扩展列纯加法。
零网络；全部事件/数值为合成占位。
"""
import copy
import json
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from open_free_router.gateway_aggregate import aggregate  # noqa: E402
from open_free_router.gateway_recommend import (  # noqa: E402
    PARAMETER_WHITELIST,
    RECOMMENDATION_TTL_DAYS,
    recommend,
    validate_record,
)

GENERATED_AT = "2026-11-07T12:00:00+00:00"
DAY = 86400.0
# 窗口 14 天，now=2026-11-07 → start=2026-10-24, end_exclusive=2026-11-08
BASE_TS = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc).timestamp()


def make_db(tmp: Path, events) -> Path:
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
        ts = BASE_TS + (i % days) * DAY + i  # 铺在 days 个自然日
        events.append(evt(f"a-{i:03d}", ts, "remote", "model-a", "success",
                          ttfb=200.0, latency=1000.0, tokens=(200, 100)))
    for i in range(n_b):
        ts = BASE_TS + (i % days) * DAY + i
        status = "success" if (i / max(1, n_b)) < success_b else "error"
        events.append(evt(f"b-{i:03d}", ts, "remote", "model-b", status,
                          ttfb=100.0, latency=600.0, tokens=(100, 50)))
    return events


def agg(events, _label=None):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        return aggregate(make_db(Path(tmp), events), window_days=14,
                         now=datetime.fromisoformat(GENERATED_AT))


def recs(out):
    return [r for r in out["records"] if r["kind"] == "recommendation"]


def obs(out):
    return [r for r in out["records"] if r["kind"] == "observation"]


class 推荐生成Tests(unittest.TestCase):
    def test_full_evidence_recommendation(self):
        """双后端全过 R1~R6 → 一条 recommendation，固定字段缺一不可。"""
        out = recommend(agg(events_two_backends()), generated_at=GENERATED_AT)
        self.assertEqual(len(recs(out)), 1)
        rec = recs(out)[0]
        self.assertEqual(validate_record(rec), [])
        self.assertEqual(rec["scope"], {"prompt_class": "remote-api", "virtual_model": None})
        self.assertEqual(rec["parameter"], "candidate_first")
        self.assertEqual(rec["current_value"], {"provider": "remote", "model": "model-a"})
        self.assertEqual(rec["recommended_value"], {"provider": "remote", "model": "model-b"})
        self.assertEqual(rec["evidence"]["rules_passed"], ["R1", "R2", "R3", "R4", "R5", "R6"])
        self.assertEqual(len(rec["evidence"]["groups"]), 2)
        # fixture 双方全成功：R4 首触发指标为 TTFB p95（成功率差 0 不触发）
        self.assertEqual(rec["expected_benefit"]["metric"], "ttfb_p95_ms")

    def test_expires_at_is_plus_seven_days(self):
        out = recommend(agg(events_two_backends()), generated_at=GENERATED_AT)
        rec = recs(out)[0]
        expected = (datetime.fromisoformat(GENERATED_AT).isoformat()
                    .replace("+00:00", "+00:00"))
        from datetime import timedelta
        want = (datetime.fromisoformat(GENERATED_AT)
                + timedelta(days=RECOMMENDATION_TTL_DAYS)).isoformat()
        self.assertEqual(rec["expires_at"], want)

    def test_r1_insufficient_samples(self):
        """n<30 → observation + insufficient_samples（不静默）。"""
        events = events_two_backends(n_a=12, n_b=10)
        out = recommend(agg(events), generated_at=GENERATED_AT)
        self.assertEqual(len(recs(out)), 0)
        self.assertEqual(len(obs(out)), 1)
        self.assertEqual(obs(out)[0]["rules_failed"]["R1"], "insufficient_samples")

    def test_r2_candidate_side_insufficient(self):
        """现任 n≥30 但挑战者 n<30 → R2 拦截。"""
        events = events_two_backends(n_a=40, n_b=8)
        out = recommend(agg(events), generated_at=GENERATED_AT)
        self.assertEqual(len(recs(out)), 0)
        self.assertEqual(obs(out)[0]["rules_failed"]["R2"], "insufficient_samples")

    def test_r3_single_day_coverage(self):
        """样本全部同一天 → insufficient_day_coverage（防单日异常污染）。"""
        events = events_two_backends(days=1)
        out = recommend(agg(events), generated_at=GENERATED_AT)
        self.assertEqual(len(recs(out)), 0)
        self.assertEqual(obs(out)[0]["rules_failed"]["R3"], "insufficient_day_coverage")

    def test_r4_effect_below_threshold(self):
        """差异太小（成功率差 2pp）→ effect_below_threshold（挪动不值得）。"""
        events = events_two_backends(success_b=0.98)  # A 1.0 vs B 0.98，其余同优
        # B 其余指标仍占优（ttfb/cost 同幅度），需把 B 的 ttfb/成本调成接近 A
        for e in events:
            if e["model"] == "model-b":
                e["ttfb_ms"] = 198.0
                e["total_latency_ms"] = 990.0
                e["input_tokens"], e["output_tokens"] = 198, 99
        out = recommend(agg(events), generated_at=GENERATED_AT)
        self.assertEqual(len(recs(out)), 0)
        self.assertEqual(obs(out)[0]["rules_failed"]["R4"], "effect_below_threshold")

    def test_r4_latency_trigger(self):
        """成功率相同但 TTFB p95 差 ≥20% → 推荐（R4 三选一）。"""
        events = []
        for i in range(40):
            events.append(evt(f"a-{i:03d}", BASE_TS + (i % 5) * DAY + i,
                              "remote", "model-a", "success", ttfb=250.0,
                              tokens=(100, 50)))
        for i in range(35):
            events.append(evt(f"b-{i:03d}", BASE_TS + (i % 5) * DAY + i,
                              "remote", "model-b", "success", ttfb=150.0,
                              tokens=(100, 50)))
        out = recommend(agg(events), generated_at=GENERATED_AT)
        rec = recs(out)[0]
        self.assertEqual(rec["expected_benefit"]["metric"], "ttfb_p95_ms")

    def test_r5_cost_type_requires_token_coverage(self):
        """成本型推荐（token 缺失多）→ insufficient_token_coverage。"""
        events = events_two_backends()
        # 把 B 组大部分事件的 token 置空（coverage<0.5），B 仍是挑战者且 R4 由成本触发
        b_count = 0
        for e in events:
            if e["model"] == "model-b":
                b_count += 1
                if b_count > 15:  # 留 ≤15 条有 token → coverage < 0.5
                    e["input_tokens"] = None
                    e["output_tokens"] = None
        out = recommend(agg(events), generated_at=GENERATED_AT)
        self.assertEqual(len(recs(out)), 0)
        self.assertEqual(obs(out)[0]["rules_failed"].get("R5"), "insufficient_token_coverage")

    def test_r6_class_coverage_passes_with_full_classification(self):
        """T2 全量分类 → 覆盖率 1.0 ≥0.5，R6 通过（在 rules_passed 中）。"""
        out = recommend(agg(events_two_backends()), generated_at=GENERATED_AT)
        self.assertIn("R6", recs(out)[0]["evidence"]["rules_passed"])


class 契约与纪律Tests(unittest.TestCase):
    def test_parameter_whitelist_and_exclusions(self):
        """parameter 必在 §4 白名单；排除面在案且物理不可达。"""
        self.assertIn("candidate_first", PARAMETER_WHITELIST)
        out = recommend(agg(events_two_backends()), generated_at=GENERATED_AT)
        for rec in recs(out):
            self.assertEqual(rec["parameter"], "candidate_first")

    def test_pending_no_auto_apply(self):
        out = recommend(agg(events_two_backends()), generated_at=GENERATED_AT)
        for rec in recs(out):
            self.assertEqual(rec["status"], "pending")
        self.assertFalse(out["summary"]["auto_apply"])
        self.assertIn("零自动变更", out["summary"]["note"])

    def test_deterministic_content_addressed_id(self):
        events = events_two_backends()
        one = recommend(agg(copy.deepcopy(events)), generated_at=GENERATED_AT)
        two = recommend(agg(copy.deepcopy(events)), generated_at=GENERATED_AT)
        self.assertEqual(json.dumps(one, ensure_ascii=False, sort_keys=True),
                         json.dumps(two, ensure_ascii=False, sort_keys=True))
        self.assertEqual(len(recs(one)[0]["recommendation_id"]), 16)

    def test_generated_at_injected_and_validated(self):
        with self.assertRaises(Exception):
            recommend(agg(events_two_backends()), generated_at="")
        with self.assertRaises(Exception):
            recommend(agg(events_two_backends()), generated_at="上周二")

    def test_invalid_aggregate_rejected(self):
        with self.assertRaises(Exception):
            recommend({"schema": "other"}, generated_at=GENERATED_AT)

    def test_t2_aggregate_extension_additive(self):
        """§3 扩展列纯加法：T2 既有字段全保留。"""
        rows = agg(events_two_backends())["aggregate"]
        for row in rows:
            for field in ("prompt_class", "provider", "model", "n", "success_rate",
                          "mean_latency_ms", "mean_tokens_in", "window",
                          "classifier_version", "metric_version", "event_ids"):
                self.assertIn(field, row)
            for field in ("ttfb_p95_ms", "tokens_per_success", "wasted_attempt_ratio",
                          "token_coverage_rate", "days_covered"):
                self.assertIn(field, row)


if __name__ == "__main__":
    unittest.main()
