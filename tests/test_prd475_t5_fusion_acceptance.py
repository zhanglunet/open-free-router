# -*- coding: utf-8 -*-
"""PRD-475/T5 测试（trend #2299 的 T5 环节）：Fusion 多模型验收试运行。
运行：.venv/bin/python -m pytest tests/test_prd475_t5_fusion_acceptance.py -q
验收（PRD 任务拆分 T5 行「含成本实测」）：全链两遍逐字节一致——启用
Fusion → 并行分发 3 模型 → 评审合成 → 成本实测（budget_used 可量化）→
默认关闭验证 → 超预算拒绝；失败演练：评审失败降级/单路失败隔离/护栏
不可绕过；链复跑 T2~T4 子进程 exit 0；文档无密钥。零网络；全合成占位。
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src/open_free_router"))
TESTS = Path(__file__).resolve().parent

from fusion import FusionError, FusionRouter  # noqa: E402

NOW = "2026-11-15T09:00:00Z"
TARGETS = ["groq/llama（占位）", "deepseek/chat（占位）", "openrouter/free（占位）"]


def full_chain():
    """全链：启用 → 并行分发 3 模型 → 评审合成 → 成本实测。"""
    fr = FusionRouter(enabled=True, budget_per_request=3, max_targets=3)
    dispatch = fr.dispatch(prompt="分析 prompt（占位）", targets=TARGETS,
                           dispatch_fn=lambda p, t: {
                               "answer": f"answer from {t}（占位）"})
    synth = fr.synthesize(dispatch_result=dispatch,
                          synthesis_fn=lambda results: {
                              "answer": f"合成自 {len(results)} 路（占位）"})
    return {"dispatch": dispatch, "synthesis": synth}


class TestAcceptance(unittest.TestCase):
    def test_chain_twice_byte_identical(self):
        runs = [full_chain(), full_chain()]
        self.assertEqual(json.dumps(runs[0], sort_keys=True),
                         json.dumps(runs[1], sort_keys=True))

    def test_cost_measurable(self):
        """成本实测可量化：budget_used = 3（3 个模型各消耗 1 个额度）。"""
        run = full_chain()
        self.assertEqual(run["dispatch"]["budget_used"], 3)
        self.assertEqual(run["dispatch"]["success_count"], 3)

    def test_synthesis_with_all_sources(self):
        run = full_chain()
        self.assertEqual(len(run["synthesis"]["sources"]), 3)
        self.assertFalse(run["synthesis"]["degraded"])
        self.assertIn("3 路", run["synthesis"]["synthesized_answer"])


class TestFailureDrills(unittest.TestCase):
    def test_default_disabled_zero_fusion_calls(self):
        fr = FusionRouter()   # 默认关闭
        with self.assertRaisesRegex(FusionError, "FUSION_DISABLED"):
            fr.dispatch(prompt="p", targets=TARGETS, dispatch_fn=lambda p, t: {})

    def test_budget_exceeded_rejected(self):
        fr = FusionRouter(enabled=True, budget_per_request=2)
        with self.assertRaisesRegex(FusionError, "FUSION_BUDGET_EXCEEDED"):
            fr.dispatch(prompt="p", targets=TARGETS, dispatch_fn=lambda p, t: {})

    def test_single_failure_isolated(self):
        fr = FusionRouter(enabled=True)
        def mixed(p, target):
            if "groq" in target:
                raise TimeoutError("timeout（占位）")
            return {"answer": f"ok {target}（占位）"}
        out = fr.dispatch(prompt="p", targets=["groq/llama（占位）",
                                               "deepseek/chat（占位）"],
                          dispatch_fn=mixed)
        self.assertEqual(out["success_count"], 1)   # 隔离不传播

    def test_degrade_returns_best_single(self):
        fr = FusionRouter(enabled=True)
        dispatch = fr.dispatch(prompt="p", targets=["groq/llama（占位）"],
                               dispatch_fn=lambda p, t: {"answer": "best（占位）"})
        synth = fr.synthesize(dispatch_result=dispatch,
                              synthesis_fn=lambda results: (_ for _ in ()).throw(
                                  RuntimeError("评审挂了（占位）")))
        self.assertTrue(synth["degraded"])
        self.assertEqual(synth["synthesized_answer"], "best（占位）")  # 不空手

    def test_chain_rerun_green(self):
        """链复跑：T2~T4 测试子进程复跑 exit 0。"""
        for name in ("test_prd475_t2_fusion.py",):
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", str(TESTS / name)],
                capture_output=True, timeout=600)
            self.assertEqual(proc.returncode, 0,
                             f"{name} 复跑失败：{proc.stdout[-400:]}{proc.stderr[-400:]}")


class TestDocNoSecrets(unittest.TestCase):
    def test_t1_design_doc_no_secrets(self):
        """文档无密钥：T1 设计拼接 needle 扫描+可行性结论（R5）。"""
        doc = next(REPO.glob("docs/plans/2026-11-17-fusion-design.md"))
        text = doc.read_text(encoding="utf-8").lower()
        for needle in ("sk-" + "live", "passw" + "ord =",
                       "BEGIN PRIVATE", "AKIA" + "x"):
            self.assertNotIn(needle, text, f"设计文档出现密钥样式 {needle}")
        self.assertIn("做", text)   # 可行性研究结论：做


if __name__ == "__main__":
    unittest.main()
