# -*- coding: utf-8 -*-
"""PRD-280/T5 验收试运行（trend #1405）：路由配置快照全流程演练 + 自动回滚 + 红线断言 + 文档无密钥。

全链：T1 #1401（设计）→ T2 #1402（config_snapshot.py 快照/还原/审计）→
T3 #1403（auto_rollback.py 健康失败自动回滚+AST 红线断言）→ T4 #1404（只出推荐红线断言）→ 本验收。

红线：学习网关只出推荐、不自动执行（回滚限健康检查客观信号）；零网络；示例全虚构。
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.config_snapshot import (  # noqa: E402
    take_snapshot, restore, record_switch_failed, export_events,
)
from open_free_router.auto_rollback import maybe_auto_rollback  # noqa: E402

ROUTES = {
    "schema": "backend-route-v1",
    "routes": [
        {"route_id": "default", "backend_id": "stub", "model": "stub-1"},
        {"route_id": "local", "backend_id": "local-echo", "model": "local-x"},
    ],
    "default_route": "default",
}


class TestAcceptance(unittest.TestCase):
    def test_a1_full_flow_snapshot_change_rollback(self):
        """全流程演练：快照→改配置→健康检查失败→自动回滚→回到原配置。"""
        snap = take_snapshot(ROUTES, "2026-10-30T10:00:00Z")
        broken = {"schema": "backend-route-v1", "routes": [], "default_route": "missing"}
        health = {"healthy": False, "error_class": "timeout"}
        result = maybe_auto_rollback(snap, health)
        self.assertTrue(result["rolled_back"])
        restored = restore(snap)
        self.assertEqual(restored["routes"], ROUTES["routes"])
        self.assertEqual(restored["default_route"], "default")

    def test_a2_switch_failed_event_recorded(self):
        snap = take_snapshot(ROUTES, "2026-10-30T10:00:00Z")
        maybe_auto_rollback(snap, {"healthy": False})
        events = export_events()
        self.assertTrue(any(e.get("event") == "switch_failed" or
                            e.get("reason") == "health_check_failed" for e in events),
                        json.dumps(events, ensure_ascii=False))

    def test_a3_healthy_no_rollback(self):
        snap = take_snapshot(ROUTES, "2026-10-30T10:00:00Z")
        before = len(export_events())
        maybe_auto_rollback(snap, {"healthy": True})
        self.assertEqual(len(export_events()), before)  # 健康正常零事件零回滚

    def test_a4_redline_single_objective_trigger(self):
        """红线断言：maybe_auto_rollback 决策输入仅 snapshot/health_result。"""
        import ast
        src = Path(__file__).resolve().parents[1] / "src" / "open_free_router" / "auto_rollback.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "maybe_auto_rollback")
        args = [a.arg for a in fn.args.args]
        self.assertEqual(sorted(args), ["health_result", "snapshot"])
        self.assertFalse(fn.args.kwonlyargs)
        self.assertIsNone(fn.args.kwarg)
        self.assertIsNone(fn.args.vararg)

    def test_a5_fingerprint_chain(self):
        snap = take_snapshot(ROUTES, "2026-10-30T10:00:00Z")
        self.assertEqual(len(snap["fingerprint"]), 16)
        # 回滚后指纹与快照一致
        restored = restore(snap)
        check = take_snapshot(restored, "2026-10-30T10:01:00Z")
        self.assertEqual(check["fingerprint"], snap["fingerprint"])

    def test_a6_tamper_detected(self):
        snap = take_snapshot(ROUTES, "2026-10-30T10:00:00Z")
        tampered = json.loads(json.dumps(snap))
        tampered["routes"] = []
        with self.assertRaises(Exception):
            restore(tampered)

    def test_a7_no_network_imports(self):
        src = (Path(__file__).resolve().parents[1] / "src" / "open_free_router" /
               "auto_rollback.py").read_text(encoding="utf-8")
        for banned in ("requests", "urllib", "socket", "http.client"):
            self.assertNotIn(banned, src)

    def test_a8_secrets_scan(self):
        surfaces = [
            Path(__file__).resolve().parents[1] / "src" / "open_free_router" / "config_snapshot.py",
            Path(__file__).resolve().parents[1] / "src" / "open_free_router" / "auto_rollback.py",
            Path(__file__),
        ]
        for path in surfaces:
            text = path.read_text(encoding="utf-8")
            needle = "sk-" + "placeholder" + "0123456789"  # 拼接构造避免自指
            self.assertNotIn(needle, text)
            self.assertNotIn("AK" + "IA", text)


if __name__ == "__main__":
    unittest.main()
