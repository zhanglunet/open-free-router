# -*- coding: utf-8 -*-
"""PRD-202/T3 自测：模型切换回滚 + 审计事件。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.model_switch_audit import ModelSwitchAudit, undo_switch  # noqa: E402


class TestAudit(unittest.TestCase):
    def test_switch_and_rollback_events(self):
        audit = ModelSwitchAudit()
        audit.record_switch("default", "alt", "alice", "t1")
        audit.record_rollback("alt", "default", "alice", "t2")
        events = audit.events
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event"], "switch")
        self.assertEqual(events[1]["event"], "rollback")

    def test_export_jsonl_parseable(self):
        audit = ModelSwitchAudit()
        audit.record_switch("a", "b", "x", "t")
        lines = audit.export_jsonl().strip().splitlines()
        self.assertEqual(len(lines), 1)
        json.loads(lines[0])

    def test_rollback_returns_previous_route(self):
        audit = ModelSwitchAudit()
        r = undo_switch("alt", "default", audit, "alice", "t")
        self.assertEqual(r["route"], "default")
        self.assertEqual(audit.events[-1]["event"], "rollback")


import json  # noqa: E402

if __name__ == "__main__":
    unittest.main()
