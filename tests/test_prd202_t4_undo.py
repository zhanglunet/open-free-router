# -*- coding: utf-8 -*-
"""PRD-202/T4 自测（trend #1013）：一键回滚（切回上一后端）。

运行：python3 -m unittest tests.test_prd202_t4_undo -v（仓库根执行）

覆盖（验收「一键回滚可用」）：
  切换 a→b 后回滚 → 回到 a 且 trace 标记 rollback；历史弹栈（不可重复回滚同一次）
  连续两次切换 → 回滚回最近一次的 from_route
  无切换历史 → 拒绝；未确认 → 拒绝（回滚也须确认）
  回滚失败（switch_fn 抛错）→ 留 failed trace、历史不弹栈（可重试）

虚构纪律：全合成路由；零网络。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.model_switch import SwitchExecutor, undo_switch  # noqa: E402
from open_free_router.route_obs import RouteObservatory  # noqa: E402


def make_executor():
    obs = RouteObservatory()
    return SwitchExecutor(obs), obs


class UndoTests(unittest.TestCase):
    def test_switch_then_undo_restores_previous(self):
        ex, obs = make_executor()
        ex.execute({"action": "switch", "target_route": "prov-b/m-2", "confirmed": True},
                   "prov-a/m-1", lambda t: "ok", request_id="req-1")
        result = undo_switch(ex, "prov-b/m-2", lambda t: "ok", request_id="req-2")
        self.assertTrue(result["ok"])
        self.assertEqual(result["to_route"], "prov-a/m-1")     # 切回上一后端
        self.assertEqual(obs.traces[-1]["route_id"], "model-switch-rollback")
        self.assertEqual(obs.traces[-1]["model"], "prov-a/m-1")

    def test_double_switch_undo_goes_to_latest_from(self):
        ex, obs = make_executor()
        ex.execute({"action": "switch", "target_route": "b", "confirmed": True},
                   "a", lambda t: "ok", request_id="r1")
        ex.execute({"action": "switch", "target_route": "c", "confirmed": True},
                   "b", lambda t: "ok", request_id="r2")
        result = undo_switch(ex, "c", lambda t: "ok", request_id="r3")
        self.assertEqual(result["to_route"], "b")              # 最近一次的 from
        result2 = undo_switch(ex, "b", lambda t: "ok", request_id="r4")
        self.assertEqual(result2["to_route"], "a")             # 再回滚到更早

    def test_undo_without_history_rejected(self):
        ex, _ = make_executor()
        result = undo_switch(ex, "a", lambda t: "ok", request_id="r1")
        self.assertFalse(result["ok"])
        self.assertIn("无可回滚", result["error"])

    def test_undo_requires_confirmation(self):
        ex, _ = make_executor()
        ex.execute({"action": "switch", "target_route": "b", "confirmed": True},
                   "a", lambda t: "ok", request_id="r1")
        result = undo_switch(ex, "b", lambda t: "ok", request_id="r2", confirmed=False)
        self.assertFalse(result["ok"])
        self.assertIn("未确认", result["error"])

    def test_failed_undo_keeps_history_for_retry(self):
        ex, obs = make_executor()
        ex.execute({"action": "switch", "target_route": "b", "confirmed": True},
                   "a", lambda t: "ok", request_id="r1")

        def boom(t):
            raise RuntimeError("目标不可达（虚构）")

        failed = undo_switch(ex, "b", boom, request_id="r2")
        self.assertFalse(failed["ok"])
        self.assertEqual(obs.traces[-1]["status"], "failed")   # 失败留 trace
        retry = undo_switch(ex, "b", lambda t: "ok", request_id="r3")
        self.assertTrue(retry["ok"])                           # 历史未弹栈，可重试


if __name__ == "__main__":
    unittest.main(verbosity=2)
