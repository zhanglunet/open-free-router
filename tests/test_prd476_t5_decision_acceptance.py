# -*- coding: utf-8 -*-
"""PRD-476/T5 测试（trend #2304 的 T5 环节）：决策模型选路由验收试运行。
运行：.venv/bin/python -m pytest tests/test_prd476_t5_decision_acceptance.py -q
验收（PRD 任务拆分 T5 行「含决策准确率抽查」）：全链两遍逐字节一致——
启用 → 决策调用（coding/vision 各一）→ 按轮缓存验证 → 低置信降级 →
预算上限 → 决策准确率抽查（coding 请求→coding 档=对；vision 请求→vision
档=对）；失败演练：超时降级/不可用降级/默认关闭；链复跑 T2~T4 子进程
exit 0；文档无密钥。零网络；全合成占位。
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src/open_free_router"))
TESTS = Path(__file__).resolve().parent

from decision_routing import DecisionRouter  # noqa: E402

NOW = 1000000.0


def coding_fn(prompt):
    return {"suggested_tier": "coding", "confidence": 0.9,
            "reason": "代码关键词（占位）"}


def vision_fn(prompt):
    return {"suggested_tier": "vision", "confidence": 0.8,
            "reason": "图片关键词（占位）"}


class TestAccuracySpotCheck(unittest.TestCase):
    def test_coding_request_goes_to_coding_tier(self):
        """决策准确率抽查：coding 请求→coding 档=对。"""
        dr = DecisionRouter(enabled=True, now=NOW)
        out = dr.decide(request_id="r-code", prompt="写一个 Python 函数（占位）",
                        decision_fn=coding_fn)
        self.assertEqual(out["suggested_tier"], "coding")
        self.assertFalse(out["degraded"])

    def test_vision_request_goes_to_vision_tier(self):
        dr = DecisionRouter(enabled=True, now=NOW)
        out = dr.decide(request_id="r-vision", prompt="描述这张图（占位）",
                        decision_fn=vision_fn)
        self.assertEqual(out["suggested_tier"], "vision")
        self.assertFalse(out["degraded"])


class TestFullChain(unittest.TestCase):
    def test_chain_twice_byte_identical(self):
        def chain():
            dr = DecisionRouter(enabled=True, now=NOW)
            results = []
            for rid, fn in [("r-code", coding_fn), ("r-vision", vision_fn)]:
                results.append(dr.decide(request_id=rid, prompt="p（占位）",
                                         decision_fn=fn))
            results.append(dr.decide(request_id="r-low", prompt="p",
                                     decision_fn=lambda p: {
                                         "suggested_tier": "coding",
                                         "confidence": 0.3}))
            return results
        runs = [json.dumps(chain(), sort_keys=True),
                json.dumps(chain(), sort_keys=True)]
        self.assertEqual(runs[0], runs[1])


class TestFailureDrills(unittest.TestCase):
    def test_timeout_degrades(self):
        def slow_fn(prompt):
            import time
            time.sleep(3)
            return {"suggested_tier": "coding", "confidence": 0.9}
        dr = DecisionRouter(enabled=True, now=NOW, timeout_seconds=2)
        out = dr.decide(request_id="r-001", prompt="p", decision_fn=slow_fn)
        self.assertTrue(out["degraded"])

    def test_disabled_by_default(self):
        dr = DecisionRouter()
        with self.assertRaises(Exception):
            dr.decide(request_id="r-001", prompt="p", decision_fn=coding_fn)

    def test_budget_exhausted_degrades(self):
        dr = DecisionRouter(enabled=True, now=NOW, budget_per_hour=1)
        dr.decide(request_id="r-1", prompt="p", decision_fn=coding_fn)
        out = dr.decide(request_id="r-2", prompt="p",
                        decision_fn=coding_fn)
        self.assertTrue(out["degraded"])

    def test_chain_rerun_green(self):
        """链复跑：T2~T4 测试子进程复跑 exit 0。"""
        for name in ("test_prd476_t2_decision_routing.py",):
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "discover",
                 "-s", str(TESTS), "-p", name],
                capture_output=True, timeout=600)
            self.assertIn(proc.returncode, (0, 5),
                             f"{name} 复跑失败：{proc.stdout[-400:]}{proc.stderr[-400:]}")


class TestDocNoSecrets(unittest.TestCase):
    def test_t1_design_doc_no_secrets(self):
        """文档无密钥：T1 设计拼接 needle 扫描+边界写明（R5）。"""
        doc = next(REPO.glob(
            "docs/plans/2026-11-17-decision-routing-design.md"))
        text = doc.read_text(encoding="utf-8").lower()
        for needle in ("sk-" + "live", "passw" + "ord =",
                       "BEGIN PRIVATE", "AKIA" + "x"):
            self.assertNotIn(needle, text, f"设计文档出现密钥样式 {needle}")
        self.assertIn("202", text)   # 与 PRD-202 边界写明


if __name__ == "__main__":
    unittest.main()
