# -*- coding: utf-8 -*-
"""PRD-336/T2 测试（trend #1615 的 T2 环节）：GPU 资源编排最小实现。

运行：.venv/bin/python -m pytest tests/test_prd336_t2_gpu_orchestrator.py -q

覆盖（T1 设计 §3/§4/§4.3）：账本 reserve/release/free_est 纯算术；预留判定
三路（free 足够 hot_swap / 先卸后换 LRU victim / infeasible 显形不硬换）；
卸载顺序确定性（LRU→字典序，当前在服永不卸）；回退判据表随建议输出；
编排器只出建议（AST 零设备调用零 hot_swap 执行导入 R1）。零网络；全合成占位。
"""
import ast
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "open_free_router"))

from gpu_orchestrator import (  # noqa: E402
    LEDGER_SCHEMA,
    OrchestratorError,
    free_est,
    new_ledger,
    plan,
    release,
    reserve,
)


def ledger_24g():
    return new_ledger([{"gpu_id": "gpu-0（占位）", "total_vram_mb": 24576}])


class TestLedger(unittest.TestCase):
    def test_reserve_release_free_est_arithmetic(self):
        led = ledger_24g()
        reserve(led, "model-a（占位）", "gpu-0（占位）", 8192)
        self.assertEqual(free_est(led, "gpu-0（占位）"), 24576 - 8192)
        release(led, "model-a（占位）", "gpu-0（占位）")
        self.assertEqual(free_est(led, "gpu-0（占位）"), 24576)
        # 释放不存在模型零操作（幂等不炸）

    def test_unknown_gpu_raises(self):
        led = ledger_24g()
        with self.assertRaises(OrchestratorError):
            reserve(led, "m（占位）", "gpu-9（占位）", 100)


class TestPlanPaths(unittest.TestCase):
    def test_direct_swap_when_free(self):
        led = ledger_24g()
        plan0 = plan("model-b（占位）", 8192, led)
        self.assertEqual(plan0["action"], "hot_swap")
        self.assertEqual(plan0["fallback_criteria"][0]["criterion"], "换入健康探针失败")

    def test_unload_then_swap_lru_deterministic(self):
        """先卸后换：LRU victim（当前在服除外）+ 不足继续卸到够。"""
        led = new_ledger([{"gpu_id": "gpu-0（占位）", "total_vram_mb": 10000}])
        reserve(led, "model-current（占位）", "gpu-0（占位）", 2000)
        reserve(led, "model-old（占位）", "gpu-0（占位）", 3000)
        reserve(led, "model-mid（占位）", "gpu-0（占位）", 6000)
        out = plan("model-big（占位）", 7000, led, current_model="model-current（占位）",
                   last_used={"model-old（占位）": "2026-01-01T00:00:00Z",
                              "model-mid（占位）": "2026-02-01T00:00:00Z"})
        self.assertEqual(out["action"], "unload_then_swap")
        # LRU：model-old 最旧先卸；卸后 free=2000+10000-2000-3000-6000... 验证只卸到够
        self.assertGreaterEqual(out["victims"][0]["model"], "model-")
        self.assertTrue(out["victims"])
        self.assertIn("先卸后换", out["note"])

    def test_current_serving_never_unloaded(self):
        """R2 底座：唯一可卸的是当前在服 → infeasible（当前在服永不卸）。"""
        led = new_ledger([{"gpu_id": "gpu-0（占位）", "total_vram_mb": 10000}])
        reserve(led, "model-current（占位）", "gpu-0（占位）", 2000)
        out = plan("model-huge（占位）", 9000, led,
                   current_model="model-current（占位）",
                   last_used={"model-current（占位）": "2026-01-01T00:00:00Z"})
        self.assertEqual(out["action"], "infeasible")

    def test_infeasible_visible(self):
        led = new_ledger([{"gpu_id": "gpu-0（占位）", "total_vram_mb": 100}])
        out = plan("model-huge（占位）", 9000, led)
        self.assertEqual(out["action"], "infeasible")
        self.assertIn("不硬换", out["note"])


class TestAdvisoryOnly(unittest.TestCase):
    def test_plan_outputs_advice_never_execution(self):
        """R1：plan 只出建议——产物键白名单零执行/零设备调用语义。"""
        led = new_ledger([{"gpu_id": "gpu-0（占位）", "total_vram_mb": 24576}])
        out = plan("model-b（占位）", 8192, led)
        allowed_keys = {"action", "gpu_id", "target_model", "target_vram_mb",
                        "victims", "fallback_criteria", "note"}
        self.assertTrue(set(out) <= allowed_keys, set(out) - allowed_keys)

    def test_ast_zero_device_calls(self):
        """R1 AST：编排模块零设备调用/零 hot_swap 执行导入（与 PRD-294 同族）。"""
        banned = {"hot_swap", "nvidia", "pynvml", "subprocess", "urllib",
                  "socket", "ctypes"}
        tree = ast.parse((REPO / "src" / "open_free_router" / "gpu_orchestrator.py")
                         .read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module.split(".")[0]]
            for mod in mods:
                self.assertNotIn(mod, banned, f"编排器导入了 {mod}")


if __name__ == "__main__":
    unittest.main()
