# -*- coding: utf-8 -*-
"""PRD-202/T3 自测（trend #1012）：确认后切换执行 + 与 PRD-201 trace 打通。

运行：python3 -m unittest tests.test_prd202_t3_switch_exec -v（仓库根执行）

覆盖（验收「未确认不切换；切换留 trace」）：
  未确认（confirmed=False）→ 拒绝且 switch_fn 零调用、零 trace
  已确认 → switch_fn 收到目标路由；trace 落 observatory（from/to/status=ok）
  switch_fn 抛错 → trace status=failed 带失败类，错误回传，不误报成功
  打通验证：切换 trace 出现在 201 的 quality_summary 与 route_board 看板分布

虚构纪律：全合成路由；零网络。
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.model_switch import SwitchExecutor  # noqa: E402
from open_free_router.route_obs import RouteObservatory, RouteTrace  # noqa: E402
from open_free_router.route_board import board_summary  # noqa: E402

NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)


def confirmed(target="cheap"):
    return {"action": "switch", "target_route": target, "confirmed": True}


class SwitchExecTests(unittest.TestCase):
    def test_unconfirmed_no_switch_no_trace(self):
        obs = RouteObservatory()
        called = []
        result = SwitchExecutor(obs).execute({"action": "switch", "confirmed": False},
                                             "prov-a/m-1",
                                             lambda t: called.append(t) or "ok",
                                             request_id="req-1")
        self.assertFalse(result["ok"])
        self.assertIn("未确认", result["error"])
        self.assertEqual(called, [])          # 零切换动作
        self.assertEqual(obs.traces, [])      # 零 trace
        self.assertEqual(obs.quality_summary(), {})  # 无空壳聚合

    def test_confirmed_switch_and_trace(self):
        obs = RouteObservatory()
        seen = []
        result = SwitchExecutor(obs).execute(confirmed("prov-b/m-2"), "prov-a/m-1",
                                             lambda t: seen.append(t) or "switched",
                                             request_id="req-2")
        self.assertTrue(result["ok"])
        self.assertEqual(seen, ["prov-b/m-2"])
        self.assertEqual(len(obs.traces), 1)
        tr = obs.traces[0]
        self.assertEqual(tr["route_id"], "model-switch")
        self.assertEqual(tr["backend_id"], "prov-a/m-1")   # from
        self.assertEqual(tr["model"], "prov-b/m-2")        # to
        self.assertEqual(tr["status"], "ok")

    def test_switch_failure_leaves_failed_trace(self):
        obs = RouteObservatory()

        def boom(target):
            raise RuntimeError("后端不可达（虚构）")

        result = SwitchExecutor(obs).execute(confirmed("prov-c/m-3"), "prov-a/m-1",
                                             boom, request_id="req-3")
        self.assertFalse(result["ok"])
        tr = obs.traces[0]
        self.assertEqual(tr["status"], "failed")
        self.assertEqual(tr["failure_class"], "RuntimeError")

    def test_trace_visible_in_board(self):
        obs = RouteObservatory()
        SwitchExecutor(obs).execute(confirmed("prov-b/m-2"), "prov-a/m-1",
                                    lambda t: "switched", request_id="req-4")
        board = board_summary(obs.traces, window="24h", now=NOW)
        keys = [r["provider_model"] for r in board["route_distribution"]]
        self.assertIn("prov-a/m-1/prov-b/m-2", keys)  # 打通：from/to 复合键进看板分布
        self.assertEqual(board["total_requests"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
