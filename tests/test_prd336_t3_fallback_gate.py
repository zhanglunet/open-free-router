# -*- coding: utf-8 -*-
"""PRD-336/T3 测试（trend #1615 的 T3 环节）：GPU 回退结果回填闸门。

运行：.venv/bin/python -m pytest tests/test_prd336_t3_fallback_gate.py -q

覆盖（T1 设计 §4.2/§4.3）：OUTCOME_CODES 词表封闭（未知显形拒绝）；gate_check
前置核（infeasible 不可 settle/判据表缺席拒绝/缺 victims 拒绝）；settle 回填
（healthy 卸载生效/rolled_back victim 复用 T2 reserve 回到 reserved 回退底座
恢复/failed 零伪装显形打 fallback_broken 供人工介入）；events append-only
全留痕可回放；与 T2 串联（plan 真实产物→settle→账本核对）。零网络；全合成
占位。
"""
import ast
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "open_free_router"))

from gpu_fallback_gate import (  # noqa: E402
    OUTCOME_CODES,
    gate_check,
    settle,
)
from gpu_orchestrator import OrchestratorError, new_ledger, plan  # noqa: E402

NOW = "2026-11-10T09:00:00Z"


def _ledger_with_victim():
    led = new_ledger([{"gpu_id": "gpu-0", "total_vram_mb": 16000}])
    return led


def _plan_unload(led, target_mb=8000, current="chat-large（占位）"):
    # 先制造两个在服模型（一个在服一个 victim）
    from gpu_orchestrator import reserve
    reserve(led, current, "gpu-0", 8000)
    reserve(led, "ocr-stale（占位）", "gpu-0", 8000)
    led["models"] = led["models"]  # noop 保持形态
    last_used = {"chat-large（占位）": "2026-11-10T08:59:00Z",
                 "ocr-stale（占位）": "2026-11-01T08:00:00Z"}
    return plan("chat-small（占位）", target_mb, led,
                current_model=current, last_used=last_used)


class TestOutcomeCodes(unittest.TestCase):
    def test_closed_vocabulary(self):
        self.assertEqual(set(OUTCOME_CODES),
                         {"healthy", "rolled_back", "failed", "fallback_broken"})

    def test_unknown_outcome_rejected(self):
        led = _ledger_with_victim()
        p = _plan_unload(led)
        with self.assertRaises(OrchestratorError):
            settle(p, "maybe_ok", led, at=NOW)


class TestGateCheck(unittest.TestCase):
    def test_infeasible_plan_not_executable(self):
        led = new_ledger([{"gpu_id": "gpu-0", "total_vram_mb": 8000}])
        p = plan("超大模型（占位）", 99999, led)
        reasons = gate_check(p)
        self.assertTrue(any("不可执行" in r for r in reasons))
        with self.assertRaises(OrchestratorError):
            settle(p, "healthy", led, at=NOW)

    def test_missing_criteria_or_victims_rejected(self):
        p = {"action": "hot_swap", "gpu_id": "gpu-0", "target_model": "m"}
        self.assertTrue(any("判据表缺席" in r for r in gate_check(p)))
        led = _ledger_with_victim()
        real = _plan_unload(led)
        stripped = dict(real, victims=[])
        self.assertTrue(any("victims" in r for r in gate_check(stripped)))


class TestSettle(unittest.TestCase):
    def test_healthy_releases_victims(self):
        led = _ledger_with_victim()
        p = _plan_unload(led)
        out = settle(p, "healthy", led, at=NOW, detail="换入成功（占位）")
        gpu = led["gpus"][0]
        self.assertNotIn("ocr-stale（占位）", gpu["reserved"])
        self.assertIn("chat-large（占位）", gpu["reserved"])   # 在服底座不动
        self.assertEqual(out["outcome"], "healthy")

    def test_rolled_back_restores_victims(self):
        """§4.3：rolled_back → victim 回到 reserved（回退底座恢复）。"""
        led = _ledger_with_victim()
        p = _plan_unload(led)
        out = settle(p, "rolled_back", led, at=NOW, detail="探针全败回滚（占位）")
        gpu = led["gpus"][0]
        self.assertIn("ocr-stale（占位）", gpu["reserved"])
        self.assertEqual(out["restored_victims"], ["ocr-stale（占位）"])

    def test_failed_marks_fallback_broken_visibly(self):
        """§4.2：回退自身失败 → 零伪装显形 + fallback_broken 供人工介入。"""
        led = _ledger_with_victim()
        p = _plan_unload(led)
        out = settle(p, "failed", led, at=NOW, detail="双失败（占位）")
        self.assertTrue(out["fallback_broken"])
        self.assertIn("人工介入", out["note"])
        self.assertIn("ocr-stale（占位）", led["gpus"][0]["reserved"])

    def test_events_append_only_replayable(self):
        led = _ledger_with_victim()
        p = _plan_unload(led)
        settle(p, "rolled_back", led, at=NOW)
        settle(p, "healthy", led, at="2026-11-10T09:05:00Z")
        ev = [e["outcome"] for e in led["events"]]
        self.assertEqual(ev, ["rolled_back", "healthy"])   # 只增不删，可回放
        before = len(led["events"])
        settle(p, "healthy", led, at="2026-11-10T09:06:00Z")
        self.assertEqual(len(led["events"]), before + 1)


class TestChainWithT2(unittest.TestCase):
    def test_full_cycle_unload_swap_rollback(self):
        """与 T2 串联：plan 先卸后换 → 回滚 → 账本与初始等价（可再规划）。"""
        led = new_ledger([{"gpu_id": "gpu-0", "total_vram_mb": 16000}])
        from gpu_orchestrator import reserve
        reserve(led, "chat-large（占位）", "gpu-0", 8000)
        reserve(led, "ocr-stale（占位）", "gpu-0", 8000)
        snapshot = dict(led["gpus"][0]["reserved"])
        p = plan("chat-small（占位）", 8000, led,
                 current_model="chat-large（占位）",
                 last_used={"ocr-stale（占位）": "2026-11-01T00:00:00Z",
                            "chat-large（占位）": "2026-11-10T09:00:00Z"})
        self.assertEqual(p["action"], "unload_then_swap")
        settle(p, "rolled_back", led, at=NOW)
        self.assertEqual(led["gpus"][0]["reserved"], snapshot)   # 底座恢复


class TestRedline(unittest.TestCase):
    def test_gate_module_zero_device_zero_network(self):
        banned = {"urllib", "requests", "socket", "http", "subprocess",
                  "torch", "cuda", "pynvml"}
        tree = ast.parse((REPO / "src" / "open_free_router" /
                          "gpu_fallback_gate.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module.split(".")[0]]
            for mod in mods:
                self.assertNotIn(mod, banned)
                self.assertNotIn(mod, {"hot_swap"})


if __name__ == "__main__":
    unittest.main()
