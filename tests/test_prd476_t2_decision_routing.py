# -*- coding: utf-8 -*-
"""PRD-476/T2~T4 测试（trend #2301/#2302/#2303 的 T2~T4 环节）：决策模型选路由。
运行：.venv/bin/python -m pytest tests/test_prd476_t2_decision_routing.py -q
覆盖（T1 §1~§3）：决策调用/按轮缓存/低置信降级/超时降级/成本护栏/
开关默认关闭/全局策略不受影响/AST 零网络。零网络；全合成占位。
"""
import ast
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/open_free_router"))
from decision_routing import (  # noqa: E402
    DEFAULT_BUDGET_PER_HOUR, ROUTING_SCHEMA, TIERS, DecisionRouter,
    DecisionRoutingError,
)

NOW = 1000000.0


def good_decision_fn(prompt):
    return {"suggested_tier": "coding", "confidence": 0.9,
            "reason": "代码关键词（占位）"}


def low_confidence_fn(prompt):
    return {"suggested_tier": "coding", "confidence": 0.3,
            "reason": "不确定（占位）"}


class TestDecisionCall(unittest.TestCase):
    def test_enabled_decision_ok(self):
        dr = DecisionRouter(enabled=True, now=NOW)
        out = dr.decide(request_id="r-001", prompt="写代码（占位）",
                        decision_fn=good_decision_fn)
        self.assertEqual(out["suggested_tier"], "coding")
        self.assertFalse(out["degraded"])

    def test_default_disabled(self):
        dr = DecisionRouter()   # 默认关闭
        with self.assertRaisesRegex(DecisionRoutingError, "DECISION_DISABLED"):
            dr.decide(request_id="r-001", prompt="p", decision_fn=good_decision_fn)

    def test_global_policy_untouched(self):
        """只重加权本次请求：决策不改全局策略。"""
        dr = DecisionRouter(enabled=True, now=NOW)
        dr.decide(request_id="r-001", prompt="写代码（占位）",
                  decision_fn=good_decision_fn)
        # 全局策略无变化（无全局变量修改、无权重写入）
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "decision_routing.py").read_text(encoding="utf-8")
        for word in ("global_weight", "set_weight", "route_weight"):
            self.assertNotIn(word, src, word)


class TestRoundCache(unittest.TestCase):
    def test_same_round_cached(self):
        """同一轮只决策一次。"""
        dr = DecisionRouter(enabled=True, now=NOW)
        calls = []
        def counting_fn(prompt):
            calls.append(1)
            return {"suggested_tier": "coding", "confidence": 0.9}
        dr.decide(request_id="r-001", prompt="p", decision_fn=counting_fn)
        dr.decide(request_id="r-001", prompt="p", decision_fn=counting_fn)
        self.assertEqual(len(calls), 1)     # 只决策一次


class TestDegrade(unittest.TestCase):
    def test_low_confidence_degrades(self):
        dr = DecisionRouter(enabled=True, now=NOW, confidence_threshold=0.6)
        out = dr.decide(request_id="r-001", prompt="p",
                        decision_fn=low_confidence_fn)
        self.assertTrue(out["degraded"])
        self.assertEqual(out["degrade_reason"], "DECISION_LOW_CONFIDENCE")

    def test_timeout_degrades(self):
        def slow_fn(prompt):
            import time
            time.sleep(3)    # 超过 2s 上限
            return {"suggested_tier": "coding", "confidence": 0.9}
        dr = DecisionRouter(enabled=True, now=NOW, timeout_seconds=2)
        out = dr.decide(request_id="r-001", prompt="p", decision_fn=slow_fn)
        self.assertTrue(out["degraded"])

    def test_decision_fn_raises_degrades(self):
        dr = DecisionRouter(enabled=True, now=NOW)
        def boom(prompt):
            raise RuntimeError("决策模型挂了（占位）")
        out = dr.decide(request_id="r-001", prompt="p", decision_fn=boom)
        self.assertTrue(out["degraded"])

    def test_tier_vocab_refused(self):
        dr = DecisionRouter(enabled=True, now=NOW)
        def ghost_tier(prompt):
            return {"suggested_tier": "ghost", "confidence": 0.9}
        out = dr.decide(request_id="r-001", prompt="p", decision_fn=ghost_tier)
        self.assertTrue(out["degraded"])


class TestCostGuard(unittest.TestCase):
    def test_budget_per_hour(self):
        dr = DecisionRouter(enabled=True, now=NOW, budget_per_hour=3)
        for i in range(3):
            dr.decide(request_id=f"r-{i}", prompt="p",
                      decision_fn=good_decision_fn)
        out = dr.decide(request_id="r-over", prompt="p",
                        decision_fn=good_decision_fn)
        self.assertTrue(out["degraded"])
        self.assertEqual(out["degrade_reason"], "DECISION_BUDGET_EXCEEDED")

    def test_budget_sliding_window(self):
        """滑动 1h 窗口：advance 2h 后预算重置（窗口滚动语义）。"""
        dr = DecisionRouter(enabled=True, now=NOW, budget_per_hour=2)
        for i in range(2):
            dr.decide(request_id=f"r-{i}", prompt="p",
                      decision_fn=good_decision_fn)
        dr.now = NOW + 3601       # 滑出 1h 窗口
        out = dr.decide(request_id="r-new", prompt="p",
                        decision_fn=good_decision_fn)
        self.assertFalse(out["degraded"])    # 预算重置


class TestRedline(unittest.TestCase):
    def test_ast_zero_network(self):
        banned = {"urllib", "requests", "socket", "http"}
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "decision_routing.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & banned, imported & banned)

    def test_tier_vocab_closed(self):
        self.assertEqual(set(TIERS), {"coding", "vision", "default"})

    def test_default_disabled(self):
        dr = DecisionRouter()
        self.assertFalse(dr.enabled)    # 默认关闭

    def test_budget_default_100(self):
        self.assertEqual(DEFAULT_BUDGET_PER_HOUR, 100)


if __name__ == "__main__":
    unittest.main()
