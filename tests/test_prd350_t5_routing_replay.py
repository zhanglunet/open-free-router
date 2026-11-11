# -*- coding: utf-8 -*-
"""PRD-350/T5 测试（trend #1629 的 T5 环节）：路由决策重放验收试运行。

运行：.venv/bin/python -m pytest tests/test_prd350_t5_routing_replay.py -q
（仓库根执行）

验收（T1 §5 T5 行「样例回放+MISMATCH 演练+文档无密钥」）：
  全链回放：合成健康/配额快照 → make_pack → replay → REPLAY_OK；两遍
  pack/verdict/账本 entries 逐字节一致（确定性可复现）；MISMATCH 演练：
  改写推荐 → REPLAY_MISMATCH 显形零静默（first_divergence_index 失败
  证据落账本可回看）；INPUT_DRIFT 演练：换快照 → REPLAY_INPUT_DRIFT
  拒绝（防当前状态冒充当时）；R2 零路由切换行为复验（演练返回值零配置
  写/路由切换载荷键）；账本 append-only；链复跑：T2/T3/T4 测试子进程
  复跑 exit 0（链上仍绿实测）；文档无密钥：T1 设计文档拼接 needle 扫描
  +占位符纪律。零网络；全合成占位。
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from open_free_router.routing_replay import (  # noqa: E402
    ReplayLedger,
    make_pack,
    replay,
)

NOW = "2026-11-11T09:00:00Z"
HEALTH = {"backend-a（占位）": "healthy", "backend-b（占位）": "degraded"}
QUOTA = {"backend-a（占位）": 0.8, "backend-b（占位）": 0.2}
RECS = [{"backend": "backend-a（占位）", "why": "健康且配额最优（占位）",
         "latency_ms_expected": 120, "cost_expected": 1}]


def rec_fn(health, quota):
    return [dict(r) for r in RECS]


def _fresh_replay():
    pack = make_pack(RECS, HEALTH, QUOTA, rules_version="v1")
    verdict = replay(pack, HEALTH, QUOTA, rec_fn, rules_version="v1")
    ledger = ReplayLedger()
    ledger.record(pack, verdict, at=NOW)
    return pack, verdict, ledger


class TestFullChainReplay(unittest.TestCase):
    def test_replay_ok_deterministic_twice(self):
        """全链回放：REPLAY_OK；两遍 pack/verdict/账本逐字节一致。"""
        pack1, verdict1, led1 = _fresh_replay()
        pack2, verdict2, led2 = _fresh_replay()
        self.assertEqual(verdict1["verdict"], "REPLAY_OK")
        self.assertEqual(json.dumps(pack1, sort_keys=True),
                         json.dumps(pack2, sort_keys=True))
        self.assertEqual(json.dumps(verdict1, sort_keys=True),
                         json.dumps(verdict2, sort_keys=True))
        self.assertEqual(json.dumps(led1.entries, sort_keys=True),
                         json.dumps(led2.entries, sort_keys=True))


class TestFailureDrills(unittest.TestCase):
    def test_mismatch_visible_and_ledger_evidence(self):
        """MISMATCH 演练：改写推荐 → REPLAY_MISMATCH 显形零静默；失败证据
        落账本；返回值零配置写/路由切换键（R2 行为复验）。"""
        pack = make_pack(RECS, HEALTH, QUOTA, rules_version="v1")
        out = replay(pack, HEALTH, QUOTA,
                     lambda h, q: [dict(RECS[0], backend="改写（占位）")],
                     rules_version="v1")
        self.assertEqual(out["verdict"], "REPLAY_MISMATCH")
        self.assertEqual(out["evidence"]["first_divergence_index"], 0)
        for key in ("config_written", "route_switched"):
            self.assertNotIn(key, out)
        ledger = ReplayLedger()
        ledger.record(pack, out, at=NOW)
        self.assertEqual(ledger.entries[-1]["verdict"], "REPLAY_MISMATCH")

    def test_input_drift_refuses(self):
        """INPUT_DRIFT 演练：换健康快照 → REPLAY_INPUT_DRIFT 拒绝（防当前
        状态冒充当时）。"""
        pack, _, _ = _fresh_replay()
        out = replay(pack, dict(HEALTH, **{"backend-a（占位）": "degraded"}),
                     QUOTA, rec_fn, rules_version="v1")
        self.assertEqual(out["refused"], "REPLAY_INPUT_DRIFT")

    def test_ledger_append_only_through_drills(self):
        """账本 append-only：全演练流程后 entries 只增不删。"""
        _, _, led1 = _fresh_replay()
        n0 = len(led1.entries)
        _, _, led2 = _fresh_replay()
        led2.record({"refused": "PACK_INCOMPLETE"},
                    {"verdict": None, "evidence": None}, at=NOW)
        self.assertEqual(len(led2.entries), n0 + 1)


class TestChainRerun(unittest.TestCase):
    def test_t2_t3_t4_rerun_green(self):
        """链复跑：T2/T3/T4 测试子进程复跑 exit 0（链上仍绿实测）。"""
        venv_py = REPO / ".venv" / "bin" / "python"
        py = str(venv_py) if venv_py.exists() else sys.executable
        for test_file in ("test_prd350_t2_routing_replay.py",
                          "test_prd350_t3_routing_replay_gates.py",
                          "test_prd350_t4_routing_replay_boundary.py"):
            proc = subprocess.run(
                [py, "-m", "pytest", "-q",
                 str(REPO / "tests" / test_file)],
                capture_output=True, timeout=300)
            self.assertEqual(proc.returncode, 0,
                             f"{test_file} 复跑失败：{proc.stdout[-400:]}")


class TestDocNoSecrets(unittest.TestCase):
    def test_t1_design_doc_no_secrets(self):
        """文档无密钥：T1 设计文档拼接 needle 扫描+占位符纪律。"""
        doc = next(REPO.glob("docs/plans/2026-11-11-prd-350-*.md"))
        text = doc.read_text(encoding="utf-8").lower()
        for needle in ("sk-" + "live", "passw" + "ord", "api_k" + "ey"):
            self.assertNotIn(needle, text, f"设计文档出现密钥词 {needle}")
        self.assertIn("占位", text)


if __name__ == "__main__":
    unittest.main()
