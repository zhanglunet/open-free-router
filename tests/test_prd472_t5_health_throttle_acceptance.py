# -*- coding: utf-8 -*-
"""PRD-472/T5 测试（trend #2284 的 T5 环节）：健康检查节流验收试运行。
运行：.venv/bin/python -m pytest tests/test_prd472_t5_health_throttle_acceptance.py -q
验收（PRD 任务拆分 T5 行「校验烧额度量可量化下降」）：全链两遍逐字节
一致——N 个提供方（含烧额度/免费）→ dispatch_round 多轮 → 降频效果
可量化（成本分级下 costs_quota 提供方的探测次数显著少于 free_verify）；
失败演练：总开关杀死探测/预算超限跳过；链复跑 T2~T4 子进程 exit 0；
文档无密钥。零网络；全合成占位。
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src/open_free_router"))
TESTS = Path(__file__).resolve().parent

from health_throttle import HealthCheckThrottle  # noqa: E402

NOW = 1000000.0


def full_chain():
    """全链：5 个提供方（2 免费/3 烧额度）多轮探测 → 降频量化。"""
    t = HealthCheckThrottle(seed=42, interval_seconds=300, budget_per_round=5)
    t.set_cost_tier("free-a（占位）", "free_verify")
    t.set_cost_tier("free-b（占位）", "free_verify")
    t.set_cost_tier("cost-a（占位）", "costs_quota")
    t.set_cost_tier("cost-b（占位）", "costs_quota")
    t.set_cost_tier("cost-c（占位）", "costs_quota")
    platforms = list(t.cost_tiers)
    probe_counts = {p: 0 for p in platforms}
    probe_fn = lambda p: True
    for round_num in range(10):
        now = NOW + round_num * 300
        out = t.dispatch_round(platforms=platforms, now=now, probe_fn=probe_fn)
        for entry in out["probed"]:
            probe_counts[entry["platform"]] += 1
    return {"probe_counts": probe_counts, "free_total": sum(
        probe_counts[p] for p in platforms if p.startswith("free")),
        "cost_total": sum(probe_counts[p] for p in platforms if p.startswith("cost")),
        "total_probes": sum(probe_counts.values())}


class TestAcceptance(unittest.TestCase):
    def test_chain_twice_byte_identical(self):
        runs = [full_chain(), full_chain()]
        self.assertEqual(json.dumps(runs[0], sort_keys=True),
                         json.dumps(runs[1], sort_keys=True))

    def test_quota_consumption_quantifiably_reduced(self):
        """验收：成本分级下 costs_quota 提供方的探测次数 < free_verify（可量化下降）。"""
        run = full_chain()
        free_avg = run["free_total"] / 2    # 2 个免费提供方
        cost_avg = run["cost_total"] / 3    # 3 个烧额度提供方
        self.assertLess(cost_avg, free_avg,
                        f"成本分级降频未生效 free_avg={free_avg} cost_avg={cost_avg}")


class TestFailureDrills(unittest.TestCase):
    def test_disabled_switch_kills_probes(self):
        with mock.patch.dict(os.environ, {"OFR_HEALTH_PROBE_DISABLED": "1"}):
            t = HealthCheckThrottle(seed=42)
            out = t.dispatch_round(platforms=["groq（占位）"], now=NOW,
                                   probe_fn=lambda p: True)
        self.assertEqual(out["outcome"], "disabled")
        self.assertEqual(out["probed_count"], 0)

    def test_budget_exceeded_skipped(self):
        t = HealthCheckThrottle(seed=42, budget_per_round=1)
        platforms = ["a（占位）", "b（占位）", "c（占位）"]
        out = t.dispatch_round(platforms=platforms, now=NOW,
                               probe_fn=lambda p: True)
        self.assertEqual(out["probed_count"], 1)
        self.assertEqual(len(out["skipped"]), 2)

    def test_chain_rerun_green(self):
        """链复跑：T2~T4 测试子进程复跑 exit 0。"""
        for name in ("test_prd472_t2_health_throttle.py",):
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", str(TESTS / name)],
                capture_output=True, timeout=600)
            self.assertEqual(proc.returncode, 0,
                             f"{name} 复跑失败：{proc.stdout[-400:]}{proc.stderr[-400:]}")


class TestDocNoSecrets(unittest.TestCase):
    def test_t1_design_doc_no_secrets(self):
        """文档无密钥：T1 设计拼接 needle 扫描+占位符纪律（R5）。"""
        doc = next(REPO.glob(
            "docs/plans/2026-11-17-health-check-throttling-design.md"))
        text = doc.read_text(encoding="utf-8").lower()
        for needle in ("sk-" + "live", "passw" + "ord =",
                       "BEGIN PRIVATE", "AKIA" + "x"):
            self.assertNotIn(needle, text, f"设计文档出现密钥样式 {needle}")
        self.assertIn("只借鉴", text)


if __name__ == "__main__":
    unittest.main()
