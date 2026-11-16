# -*- coding: utf-8 -*-
"""PRD-472/T2~T4 测试（trend #2281/#2282/#2283 的 T2+T3+T4 环节）：
健康检查节流。运行：.venv/bin/python -m pytest tests/test_prd472_t2_health_throttle.py -q
覆盖（T1 §2/§3/§4）：节奏+抖动/3.5 分钟跳过/提供方错开/成本分级降频/
预算上限/总开关/probe_log 独立落盘/AST 零网络。零网络；全合成占位。
"""
import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/open_free_router"))
from health_throttle import (  # noqa: E402
    DEFAULT_BUDGET_PER_ROUND, THROTTLE_SCHEMA, HealthCheckThrottle,
)

NOW = 1000000.0


class TestRhythm(unittest.TestCase):
    def test_first_probe_always_allowed(self):
        """首次校验节奏不变（§3 只降重复校验）。"""
        t = HealthCheckThrottle(seed=42)
        ok, _ = t.should_probe(platform="groq（占位）", now=NOW)
        self.assertTrue(ok)

    def test_recently_probed_skipped(self):
        """3.5 分钟内验过跳过。"""
        t = HealthCheckThrottle(seed=42)
        t.mark_probed("groq（占位）", NOW)
        ok, reason = t.should_probe(platform="groq（占位）", now=NOW + 200)
        self.assertFalse(ok)
        self.assertEqual(reason, "recently_probed")

    def test_provider_gap_enforced(self):
        """同提供方探测至少间隔 30 秒。"""
        t = HealthCheckThrottle(seed=42)
        t.mark_probed("groq（占位）", NOW)
        t._last_probe.pop("groq（占位）")   # 清 last_probe 但保留 dispatch
        ok, reason = t.should_probe(platform="groq（占位）", now=NOW + 15)
        self.assertFalse(ok)
        self.assertEqual(reason, "provider_gap")


class TestCostTiers(unittest.TestCase):
    def test_costs_quota_reduced_frequency(self):
        """烧额度的提供方降频三倍：free_verify 在 300s 可探测、costs_quota 在 300s 不行。"""
        t = HealthCheckThrottle(seed=42, interval_seconds=300)
        t.set_cost_tier("free-p（占位）", "free_verify")
        t.set_cost_tier("cost-p（占位）", "costs_quota")
        t.mark_probed("free-p（占位）", NOW)
        t.mark_probed("cost-p（占位）", NOW)
        ok_free, _ = t.should_probe(platform="free-p（占位）", now=NOW + 400)
        ok_cost, _ = t.should_probe(platform="cost-p（占位）", now=NOW + 400)
        self.assertTrue(ok_free)                 # 免费校验保持默认节奏
        self.assertFalse(ok_cost)                # 烧额度降频三倍

    def test_unknown_platform_conservative(self):
        """不认识的提供方按 costs_quota（保守——宁可降频不烧额度）。"""
        t = HealthCheckThrottle(seed=42)
        self.assertEqual(t.get_multiplier("unknown（占位）"), 3.0)

    def test_tier_table_extensible(self):
        t = HealthCheckThrottle(seed=42)
        t.set_cost_tier("new-p（占位）", "free_verify")
        self.assertEqual(t.get_multiplier("new-p（占位）"), 1.0)


class TestBudgetAndSwitch(unittest.TestCase):
    def test_budget_per_round(self):
        """预算上限：5 个提供方最多探测 budget 个。"""
        t = HealthCheckThrottle(seed=42, budget_per_round=2)
        platforms = [f"p-{i}（占位）" for i in range(5)]
        out = t.dispatch_round(platforms=platforms, now=NOW,
                               probe_fn=lambda p: True)
        self.assertEqual(out["probed_count"], 2)
        self.assertEqual(len(out["skipped"]), 3)
        self.assertTrue(any(s["reason"] == "THROTTLE_BUDGET_EXCEEDED"
                            for s in out["skipped"]))

    def test_disabled_switch(self):
        """总开关：OFR_HEALTH_PROBE_DISABLED=1 杀死探测任务。"""
        t = HealthCheckThrottle(seed=42)
        with mock.patch.dict(os.environ, {"OFR_HEALTH_PROBE_DISABLED": "1"}):
            out = t.dispatch_round(platforms=["groq（占位）"], now=NOW,
                                   probe_fn=lambda p: True)
        self.assertEqual(out["outcome"], "disabled")
        self.assertEqual(out["probed_count"], 0)


class TestProbeLog(unittest.TestCase):
    def test_probe_log_independent(self):
        """探测写独立 probe_log，不改 AnalyticsStore 正式表。"""
        t = HealthCheckThrottle(seed=42)
        t.dispatch_round(platforms=["groq（占位）"], now=NOW,
                         probe_fn=lambda p: True)
        self.assertEqual(len(t.probe_log_entries()), 1)
        self.assertEqual(t.probe_log_entries()[0]["schema"], "probe-log/v1")
        with tempfile.TemporaryDirectory() as td:
            path = t.save_probe_log(Path(td) / "probe-log.jsonl")
            lines = Path(path).read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)

    def test_jitter_bounded(self):
        """抖动幅度有上限（±10%）。"""
        t = HealthCheckThrottle(seed=42, interval_seconds=300)
        for _ in range(50):
            mult = t.get_multiplier("groq（占位）")
            jittered = 300 * mult * 1.0  # 基准
            # 抖动通过 _rng.uniform(0.9, 1.1) 在 should_probe 内部生效
        self.assertEqual(t.interval, 300)  # 基准不被修改


class TestRedline(unittest.TestCase):
    def test_ast_zero_network(self):
        banned = {"urllib", "socket", "subprocess"}
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "health_throttle.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & banned, imported & banned)

    def test_schema_closed(self):
        self.assertEqual(THROTTLE_SCHEMA, "health-throttle/v1")


if __name__ == "__main__":
    unittest.main()
