# -*- coding: utf-8 -*-
"""PRD-475/T2~T4 测试（trend #2296/#2297/#2298 的 T2~T4 环节）：Fusion 多模型。
运行：.venv/bin/python -m pytest tests/test_prd475_t2_fusion.py -q
覆盖（T1 §2~§4）：并行分发单路失败隔离/超时/评审合成来源标注/降级返回
最优单路/默认关闭/预算上限/硬上限不可绕过/AST 零网络。零网络；全合成占位。
"""
import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/open_free_router"))
from fusion import (  # noqa: E402
    DEFAULT_ENABLED, ERROR_CODES, FusionError, FusionRouter,
)

T0 = 1000000.0


def good_dispatch(prompt, target):
    return {"answer": f"answer from {target}（占位）"}


def bad_dispatch(prompt, target):
    if "groq" in target:
        raise TimeoutError("30s timeout（占位）")
    return {"answer": f"answer from {target}（占位）"}


class TestDispatch(unittest.TestCase):
    def test_parallel_dispatch_ok(self):
        fr = FusionRouter(enabled=True)
        out = fr.dispatch(prompt="p（占位）",
                          targets=["groq/llama（占位）", "deepseek/chat（占位）"],
                          dispatch_fn=good_dispatch)
        self.assertEqual(out["success_count"], 2)
        self.assertEqual(out["budget_used"], 2)

    def test_single_failure_isolated(self):
        fr = FusionRouter(enabled=True)
        out = fr.dispatch(prompt="p（占位）",
                          targets=["groq/llama（占位）", "deepseek/chat（占位）"],
                          dispatch_fn=bad_dispatch)
        self.assertEqual(out["success_count"], 1)   # 单路失败不拖垮整体
        self.assertFalse(out["results"][0]["ok"])
        self.assertTrue(out["results"][1]["ok"])

    def test_default_disabled(self):
        fr = FusionRouter()
        with self.assertRaisesRegex(FusionError, "FUSION_DISABLED"):
            fr.dispatch(prompt="p（占位）",
                        targets=["groq/llama（占位）"], dispatch_fn=good_dispatch)

    def test_default_enabled_is_false(self):
        self.assertFalse(DEFAULT_ENABLED)   # 默认关闭


class TestBudget(unittest.TestCase):
    def test_budget_exceeded(self):
        fr = FusionRouter(enabled=True, budget_per_request=2)
        with self.assertRaisesRegex(FusionError, "FUSION_BUDGET_EXCEEDED"):
            fr.dispatch(prompt="p（占位）",
                        targets=["a（占位）", "b（占位）", "c（占位）"],
                        dispatch_fn=good_dispatch)

    def test_max_targets_hard_limit(self):
        fr = FusionRouter(enabled=True, budget_per_request=5,
                          max_targets=3)
        with self.assertRaisesRegex(FusionError, "FUSION_MAX_TARGETS"):
            fr.dispatch(prompt="p（占位）",
                        targets=["a", "b", "c", "d"],
                        dispatch_fn=good_dispatch)

    def test_guard_cannot_be_bypassed(self):
        """护栏不可绕过：预算上限超不过 max_targets。"""
        fr = FusionRouter(enabled=True, budget_per_request=100,
                          max_targets=3)
        with self.assertRaisesRegex(FusionError, "FUSION_MAX_TARGETS"):
            fr.dispatch(prompt="p",
                        targets=["a", "b", "c", "d"], dispatch_fn=good_dispatch)


class TestSynthesize(unittest.TestCase):
    def test_synthesis_with_sources(self):
        fr = FusionRouter(enabled=True)
        dispatch = fr.dispatch(prompt="p（占位）",
                               targets=["groq/llama（占位）", "deepseek/chat（占位）"],
                               dispatch_fn=good_dispatch)
        synth = fr.synthesize(dispatch_result=dispatch,
                              synthesis_fn=lambda results: {
                                  "answer": "合成答案（占位）"})
        self.assertEqual(synth["synthesized_answer"], "合成答案（占位）")
        self.assertEqual(synth["sources"],
                         ["groq/llama（占位）", "deepseek/chat（占位）"])
        self.assertFalse(synth["degraded"])

    def test_synthesis_failure_degrades_to_best_single(self):
        fr = FusionRouter(enabled=True)
        dispatch = fr.dispatch(prompt="p（占位）",
                               targets=["groq/llama（占位）"],
                               dispatch_fn=good_dispatch)
        synth = fr.synthesize(dispatch_result=dispatch,
                              synthesis_fn=lambda results: (_ for _ in ()).throw(
                                  RuntimeError("评审模型挂了（占位）")))
        self.assertTrue(synth["degraded"])
        self.assertNotEqual(synth["synthesized_answer"], "")  # 降级不空手

    def test_all_fail_no_answer(self):
        fr = FusionRouter(enabled=True)
        dispatch = {"results": [{"target": "x", "ok": False,
                                 "error": "timeout"}]}
        synth = fr.synthesize(dispatch_result=dispatch,
                              synthesis_fn=lambda results: {
                                  "answer": "never（占位）"})
        self.assertTrue(synth["degraded"])
        self.assertIsNone(synth["synthesized_answer"])


class TestRedline(unittest.TestCase):
    def test_ast_zero_network(self):
        banned = {"urllib", "requests", "socket", "http", "subprocess"}
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "fusion.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & banned, imported & banned)

    def test_error_codes_closed(self):
        self.assertEqual(set(ERROR_CODES),
                         {"FUSION_DISABLED", "FUSION_BUDGET_EXCEEDED",
                          "FUSION_MAX_TARGETS", "FUSION_INCOMPLETE"})

    def test_no_intelligence_rank(self):
        """非目标：不做能力分（intelligenceRank）。"""
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "fusion.py").read_text(encoding="utf-8")
        self.assertNotIn("intelligencerank", src.lower())


if __name__ == "__main__":
    unittest.main()
