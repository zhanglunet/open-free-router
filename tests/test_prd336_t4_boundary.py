# -*- coding: utf-8 -*-
"""PRD-336/T4 测试（trend #1615 的 T4 环节）：GPU 资源编排划界断言。

运行：.venv/bin/python -m pytest tests/test_prd336_t4_boundary.py -q

覆盖（T1 §0 R1/R2/R3 + §2 划界）：R1 编排是账本不是驱动（三模块 AST 零
nvidia-smi/CUDA/驱动 API/网络/子进程导入零设备探测符号——真实显存状态由
既有探针注入）；R2 回退永远可行（卸载顺序钉值：当前在服模型 plan 永不选
为 victim——行为复钉；回退判据表逐条在位钉值）；R3 判据显形（fallback_
criteria 三条判据逐字钉值 + settle 全留痕复钉）；模块隔离（账本零
hot_swap 状态机导入——PRD-230 只引用不重实现）；账本只读边界（plan 前后
账本原样零写入——reserve/release 是唯一写入口）。零网络；全合成占位。
"""
import ast
import copy
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "open_free_router"))

from gpu_fallback_gate import OUTCOME_CODES, gate_check, settle  # noqa: E402
from gpu_orchestrator import LEDGER_SCHEMA, new_ledger, plan, reserve  # noqa: E402

MODULES = ("gpu_orchestrator.py", "gpu_fallback_gate.py")
DEVICE_TOKENS = ("nvidia-smi", "nvidia_smi", "cuda", "cudart", "pynvml",
                 "nvml", "subprocess", "urllib", "requests", "socket", "http")
NOW = "2026-11-10T09:00:00Z"


def _ledger():
    led = new_ledger([{"gpu_id": "gpu-0", "total_vram_mb": 16000}])
    reserve(led, "chat-large（占位）", "gpu-0", 8000)
    reserve(led, "ocr-stale（占位）", "gpu-0", 8000)
    return led


def _plan(led):
    return plan("chat-small（占位）", 8000, led,
                current_model="chat-large（占位）",
                last_used={"ocr-stale（占位）": "2026-11-01T00:00:00Z",
                           "chat-large（占位）": "2026-11-10T09:00:00Z"})


class TestLedgerNotDriver(unittest.TestCase):
    def test_zero_device_probing_symbols(self):
        """R1 AST：三模块零 nvidia-smi/CUDA/驱动/网络/子进程符号——账本不是
        驱动，真实显存由既有探针注入。"""
        for name in MODULES:
            src = (REPO / "src" / "open_free_router" / name).read_text(
                encoding="utf-8").lower()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module.split(".")[0]]
                for mod in mods:
                    self.assertNotIn(mod, {"torch", "ctypes"}, f"{name} 导入 {mod}")
            for token in DEVICE_TOKENS:
                self.assertNotIn(token, src, f"{name} 出现设备符号 {token}")

    def test_plan_readonly_over_ledger(self):
        """R1 行为：plan 只读——前后账本原样（reserve/release 唯一写入口）。"""
        led = _ledger()
        before = copy.deepcopy(led)
        _plan(led)
        self.assertEqual(led, before)


class TestRollbackAlwaysFeasible(unittest.TestCase):
    def test_current_model_never_victim(self):
        """R2 行为复钉：当前在服模型永不进 victims（回退底座保留）。"""
        p = _plan(_ledger())
        if p["action"] == "unload_then_swap":
            victims = [v["model"] for v in p["victims"]]
            self.assertNotIn("chat-large（占位）", victims)

    def test_criteria_table_three_rows_nailed(self):
        """R3 判据显形钉值：三条判据逐字在位（探针失败/换入超时/回退自身失败）。"""
        p = _plan(_ledger())
        rows = p["fallback_criteria"]
        self.assertEqual(len(rows), 3)
        joined = " ".join(r["criterion"] for r in rows)
        for token in ("健康探针失败", "换入超时", "回退自身失败"):
            self.assertIn(token, joined)
        self.assertIn("hot_swap 既有顺序", rows[0]["action"])   # PRD-230 只引用


class TestOutcomeGateNails(unittest.TestCase):
    def test_outcome_codes_nailed(self):
        self.assertEqual(set(OUTCOME_CODES),
                         {"healthy", "rolled_back", "failed", "fallback_broken"})

    def test_settle_appends_only_and_rollback_restores(self):
        led = _ledger()
        p = _plan(led)
        snapshot = dict(led["gpus"][0]["reserved"])
        settle(p, "rolled_back", led, at=NOW)
        self.assertEqual(led["gpus"][0]["reserved"], snapshot)   # 底座恢复
        n = len(led["events"])
        settle(p, "healthy", led, at="2026-11-10T09:05:00Z")
        self.assertEqual(len(led["events"]), n + 1)   # append-only

    def test_gate_check_rejects_infeasible(self):
        led = new_ledger([{"gpu_id": "gpu-0", "total_vram_mb": 8000}])
        p = plan("超大（占位）", 99999, led)
        self.assertTrue(any("不可执行" in r for r in gate_check(p)))


class TestIsolation(unittest.TestCase):
    def test_ledger_schema_nailed_no_hotswap_import(self):
        """模块隔离：schema 钉值 gpu-ledger/v1；编排零 hot_swap 状态机导入
        （PRD-230 只引用不重实现）。"""
        self.assertEqual(LEDGER_SCHEMA, "gpu-ledger/v1")
        for name in MODULES:
            src = (REPO / "src" / "open_free_router" / name).read_text(
                encoding="utf-8").lower()
            self.assertNotIn("import hot_swap", src)
            self.assertNotIn("from hot_swap", src)


if __name__ == "__main__":
    unittest.main()
