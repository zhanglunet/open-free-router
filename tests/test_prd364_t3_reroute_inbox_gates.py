# -*- coding: utf-8 -*-
"""PRD-364/T3 测试（trend #1643 的 T3 环节）：换路批准收件箱批阅闸门。

运行：.venv/bin/python -m pytest tests/test_prd364_t3_reroute_inbox_gates.py -q

覆盖（T1 §4 + R1/R4）：append_only_gate（无基线形态过/mark_baseline 后追加
pass/删除或首事件改写 LEDGER_DRIFT 显形/非账本对象显形）；state_gate 双向对账
（人批后一致 pass/直接改写/回滚/删除三破坏全部 STATE_INCONSISTENT 显形）；
gate_summary 一票否决；与 T2 串联（真实 add/approve/reject 产物过闸全 pass）；
AST 零网络零子进程+闸门只读。零网络；全合成占位。
"""
import ast
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "open_free_router"))

from reroute_inbox import InboxLedger, collect  # noqa: E402
from reroute_inbox_gates import (  # noqa: E402
    ERROR_CODES,
    append_only_gate,
    gate_summary,
    mark_baseline,
    state_gate,
)

NOW = "2026-11-11T09:00:00Z"
LATER = "2026-11-11T12:00:00Z"
RECS = [{"source_ref": "routing-replay-pack/v1（占位）",
         "snapshot_digest": "sha16:snap（占位）",
         "recommendation": "backend-a → backend-b（占位）",
         "reason": "健康+配额最优（占位）", "recommended_at": NOW}]
BROKEN = [{"event_index": 0, "snapshot_digest": "sha16:snap2（占位）",
           "detail": "双失败（占位）"}]


class TestAppendOnlyGate(unittest.TestCase):
    def setUp(self):
        self.led = InboxLedger()
        self.led.add(collect(recommendations=RECS, fallback_broken=BROKEN,
                             at=NOW))
        self.led.approve("item-001", by="admin（占位）", at=LATER)

    def test_no_baseline_form_check_passes(self):
        self.assertEqual(append_only_gate(self.led)["gate"], "pass")

    def test_mark_baseline_then_append_passes(self):
        mark_baseline(self.led)
        self.led.reject("item-002", by="admin（占位）", at=LATER,
                        reason="r（占位）")
        self.assertEqual(append_only_gate(self.led)["gate"], "pass")

    def test_deletion_detected(self):
        mark_baseline(self.led)
        self.led._events.pop()
        self.assertEqual(append_only_gate(self.led)["gate"], "LEDGER_DRIFT")

    def test_first_event_rewrite_detected(self):
        mark_baseline(self.led)
        self.led._events[0]["op"] = "deleted（篡改）（占位）"
        self.assertEqual(append_only_gate(self.led)["gate"], "LEDGER_DRIFT")

    def test_non_ledger_object_visible(self):
        self.assertEqual(append_only_gate(object())["gate"], "LEDGER_DRIFT")


class TestStateGate(unittest.TestCase):
    def setUp(self):
        self.led = InboxLedger()
        self.led.add(collect(recommendations=RECS, fallback_broken=BROKEN,
                             at=NOW))
        self.led.approve("item-001", by="admin（占位）", at=LATER)

    def test_consistent_state_passes(self):
        self.assertEqual(state_gate(self.led)["gate"], "pass")

    def test_direct_item_rewrite_detected(self):
        """条目被直接改写（未经 _decide）→ STATE_INCONSISTENT。"""
        self.led.reject("item-002", by="admin（占位）", at=LATER,
                        reason="r（占位）")
        self.led._items["item-002"]["state"] = "pending"
        out = state_gate(self.led)
        self.assertEqual(out["gate"], "STATE_INCONSISTENT")
        self.assertTrue(any("item-002" in r for r in out["reasons"]))

    def test_state_rollback_detected(self):
        self.led.reject("item-002", by="admin（占位）", at=LATER,
                        reason="r（占位）")
        self.led._items["item-002"]["state"] = "pending"
        self.assertEqual(state_gate(self.led)["gate"], "STATE_INCONSISTENT")

    def test_item_deleted_detected(self):
        self.led.reject("item-002", by="admin（占位）", at=LATER,
                        reason="r（占位）")
        del self.led._items["item-002"]
        out = state_gate(self.led)
        self.assertEqual(out["gate"], "STATE_INCONSISTENT")
        self.assertTrue(any("条目被删除" in r for r in out["reasons"]))


class TestSummaryAndChain(unittest.TestCase):
    def test_summary_one_block_votes_all(self):
        led = InboxLedger()
        led.add(collect(recommendations=RECS, at=NOW))
        led.approve("item-001", by="admin（占位）", at=LATER)
        led._items["item-001"]["state"] = "pending"
        out = gate_summary([append_only_gate(led), state_gate(led)])
        self.assertEqual(out["gate"], "block")
        self.assertEqual(out["gates_checked"], 2)

    def test_chain_with_real_t2_products(self):
        """与 T2 串联：真实 add/approve/reject 产物过闸全 pass。"""
        led = InboxLedger()
        led.add(collect(recommendations=RECS, fallback_broken=BROKEN, at=NOW))
        led.approve("item-001", by="admin（占位）", at=LATER)
        led.reject("item-002", by="admin（占位）", at=LATER, reason="r（占位）")
        mark_baseline(led)
        results = [append_only_gate(led), state_gate(led)]
        self.assertTrue(all(r["gate"] == "pass" for r in results))
        self.assertEqual(gate_summary(results)["gate"], "pass")

    def test_error_codes_closed(self):
        self.assertEqual(set(ERROR_CODES),
                         {"LEDGER_DRIFT", "STATE_INCONSISTENT",
                          "DECISION_MISSING"})


class TestRedline(unittest.TestCase):
    def test_ast_zero_network_zero_subprocess_readonly(self):
        """R1 AST：零网络零子进程+闸门只读（零裁决调用）。"""
        banned = {"urllib", "requests", "socket", "http", "subprocess"}
        src = (REPO / "src" / "open_free_router" / "reroute_inbox_gates.py"
               ).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module.split(".")[0]]
            for mod in mods:
                self.assertNotIn(mod, banned)
        for call in (".add(", ".approve(", ".reject("):
            self.assertNotIn(call, src, f"闸门出现裁决调用 {call}")


if __name__ == "__main__":
    unittest.main()
