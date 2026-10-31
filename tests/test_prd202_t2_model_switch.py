# -*- coding: utf-8 -*-
"""PRD-202/T2 自测：一句话换模型——意图解析/回显确认/切换执行/回滚。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.model_switch import parse_intent, echo_confirm, do_switch  # noqa: E402


class TestParseIntent(unittest.TestCase):
    def test_switch_cheap(self):
        self.assertEqual(parse_intent("换成便宜的")["action"], "switch")
        self.assertEqual(parse_intent("换成便宜的")["qualifier"], "便宜")

    def test_switch_fast(self):
        self.assertEqual(parse_intent("换个快的")["action"], "switch")

    def test_rollback_detected(self):
        self.assertEqual(parse_intent("换回原来的")["action"], "rollback")

    def test_status_detected(self):
        self.assertEqual(parse_intent("现在用的什么")["action"], "status")

    def test_unknown(self):
        self.assertEqual(parse_intent("你好")["action"], "unknown")


class TestEchoConfirm(unittest.TestCase):
    def test_switch_echo(self):
        r = echo_confirm({"action": "switch", "qualifier": "便宜"}, "default", {})
        self.assertEqual(r["confirmed"], False)
        self.assertIn("将切换到", r["message"])

    def test_rollback_echo(self):
        r = echo_confirm({"action": "rollback"}, "default", {})
        self.assertIn("回滚", r["message"])


class TestDoSwitch(unittest.TestCase):
    def test_switch_executes(self):
        switched_to = []
        def switch_fn(target):
            switched_to.append(target)
            return {"ok": True}
        r = do_switch({"action": "switch", "target_route": "alt"}, "default", switch_fn)
        self.assertTrue(r["ok"])
        self.assertEqual(switched_to, ["alt"])

    def test_no_target_rejected(self):
        r = do_switch({"action": "switch"}, "default", lambda t: {})
        self.assertFalse(r["ok"])

    def test_no_switch_without_target(self):
        r = do_switch({"action": "unknown"}, "default", lambda t: {})
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
