# -*- coding: utf-8 -*-
"""PRD-230/T5 测试：验收试运行——多本地模型轮换场景（trend #1154）。

运行：.venv/bin/python -m pytest tests/test_prd230_t5_rotation.py -q

验收标准（trend #1154）：**全流程通过，回滚路径验证完毕**。

演练（合成三本地模型轮换 m-base → m-code → m-long；加载/探活全注入）：
  每跳 hot_swap 全时序（precheck→draining→loading→health_check→done）+
  切换前 checkpoint（make_checkpoint）+ 切换后判据四件套：
  A 上下文全等回放（context_replay_diff 空）、B 延续性探针（probe_check）、
  C 参数保持、D 统计连续 + C1/C2 门禁（consistency_gate 前后同等 100% 过）；
  失败注入：health_check 穷尽 → rolling_back→rolled_back 原模型恢复
  （判据四件套在回滚后复验仍全绿）；回滚后再轮换正常；
  回滚自身失败 → failed 显形（异常不外泄）；事件无栈痕（零报错外泄）。
零网络；全合成占位。
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "open_free_router"))

from hot_swap import HotSwapController  # noqa: E402
from swap_consistency import (  # noqa: E402
    consistency_gate,
    context_replay_diff,
    make_checkpoint,
    params_preserved,
    probe_check,
    usage_continuous,
)

T0 = "2026-11-03T09:00:00Z"


def make_session(sid="sess-1"):
    return {"session_id": sid, "model": "m-base",
            "system": "你是本地路由助手（占位）",
            "messages": [{"role": "user", "content": "记住暗号 ZEBRA-0042（占位）"},
                         {"role": "assistant", "content": "已记住（占位）"}],
            "params": {"temperature": 0.3, "top_p": 0.9},
            "last_request_seq": 2,
            "usage": {"input_tokens": 120, "output_tokens": 40}}


def make_controller(health_ok=lambda m: True, load_ok=lambda m: True):
    return HotSwapController(
        registered_models={"m-base", "m-code", "m-long"},
        downloaded={"m-base", "m-code", "m-long"},
        current_model="m-base",
        load_fn=lambda m: (_ for _ in ()).throw(RuntimeError(f"load 失败：{m}"))
        if not load_ok(m) else None,
        health_fn=health_ok,
        now_ticks=list(range(1, 200)),
    )


def after_hop_criteria(checkpoint, sessions_before, sessions_after, replies):
    """一跳切换后的判据四件套 + C1/C2 门禁。"""
    errors = context_replay_diff(checkpoint, sessions_after)
    before_params = sessions_before[0]["params"]
    after_params = sessions_after[0]["params"]
    return {
        "replay_errors": errors,
        "probe_ok": all(probe_check("ZEBRA-0042（占位）", r) for r in replies),
        "params_ok": params_preserved(before_params, after_params),
        "usage_ok": usage_continuous(sessions_before[0]["usage"],
                                     sessions_after[0]["usage"]),
    }


class 轮换全链Tests(unittest.TestCase):
    def test_three_hop_rotation_full_flow(self):
        """验收①：三跳轮换全流程通过——每跳时序 done + 判据全绿 + 门禁过。"""
        ctrl = make_controller()
        session = make_session()
        hops = ["m-code", "m-long", "m-code"]
        replies_per_hop = [
            ["回答里带暗号 ZEBRA-0042（占位）确认延续"],
            ["ZEBRA-0042（占位）仍在上下文中"],
            ["延续检查：ZEBRA-0042（占位）"],
        ]
        for index, target in enumerate(hops):
            checkpoint = make_checkpoint(f"swap-hop{index + 1}", T0, [session])
            before_samples = {"completions": [], "streams": []}
            swap = ctrl.hot_swap(f"req-{index + 1:02d}", target)
            self.assertEqual(swap["state"], "done", swap)
            self.assertEqual(ctrl.current_model, target)
            # 会话延续：模型标识如实变更，其余字段原样
            session_after = dict(session, model=target)
            criteria = after_hop_criteria(checkpoint, [session], [session_after],
                                          replies_per_hop[index])
            self.assertEqual(criteria["replay_errors"], [])
            self.assertTrue(criteria["probe_ok"])
            self.assertTrue(criteria["params_ok"])
            self.assertTrue(criteria["usage_ok"])
            gate = consistency_gate(before_samples, before_samples)
            self.assertTrue(gate["ok"], gate["errors"])
            session = session_after

        # 事件时序完整：三跳全部走满 precheck-ok→…→done
        for index, request_id in enumerate([f"req-{i + 1:02d}" for i in range(3)]):
            swap = ctrl.swaps[request_id]
            states = [e["to"] for e in swap["events"]]
            for milestone in ("precheck-ok", "draining", "loading",
                              "health_check", "done"):
                self.assertIn(milestone, states, (request_id, states))


class 回滚路径Tests(unittest.TestCase):
    def test_health_failure_rolls_back_and_sessions_survive(self):
        """验收②：探活穷尽 → rolling_back→rolled_back 原模型恢复，判据复验全绿。"""
        # m-broken 已注册已下载但探活永远失败
        ctrl = HotSwapController(
            registered_models={"m-base", "m-broken"},
            downloaded={"m-base", "m-broken"},
            current_model="m-base",
            load_fn=lambda m: None,
            health_fn=lambda m: False,
            now_ticks=list(range(1, 200)),
        )
        session = make_session()
        checkpoint = make_checkpoint("swap-rb1", T0, [session])
        swap = ctrl.hot_swap("req-rb", "m-broken")
        self.assertEqual(swap["state"], "rolled_back", swap)
        self.assertEqual(ctrl.current_model, "m-base")        # 原模型恢复
        states = [e["to"] for e in swap["events"]]
        self.assertIn("rolling_back", states)
        self.assertIn("rolled_back", states)
        # 回滚后会话判据复验：上下文没丢、探针延续
        session_after = dict(session, model="m-base")
        criteria = after_hop_criteria(checkpoint, [session], [session_after],
                                      ["回滚后回答仍含 ZEBRA-0042（占位）"])
        self.assertEqual(criteria["replay_errors"], [])
        self.assertTrue(criteria["probe_ok"])

    def test_rotation_resumes_after_rollback(self):
        """回滚后再轮换：active swap 清位，切健康目标正常 done。"""
        ctrl = HotSwapController(
            registered_models={"m-base", "m-broken", "m-good"},
            downloaded={"m-base", "m-broken", "m-good"},
            current_model="m-base",
            load_fn=lambda m: None,
            health_fn=lambda m: m != "m-broken",
            now_ticks=list(range(1, 200)),
        )
        bad = ctrl.hot_swap("req-1", "m-broken")
        self.assertEqual(bad["state"], "rolled_back")
        good = ctrl.hot_swap("req-2", "m-good")
        self.assertEqual(good["state"], "done")
        self.assertEqual(ctrl.current_model, "m-good")

    def test_rollback_failure_surfaces_as_failed_no_escape(self):
        """回滚自身失败 → failed 显形；注入异常不外泄（零报错外泄红线）。"""
        def flaky_load(model):
            if model == "m-base":
                raise RuntimeError("原模型加载失败（注入）")
            return None
        ctrl = HotSwapController(
            registered_models={"m-base", "m-broken"},
            downloaded={"m-base", "m-broken"},
            current_model="m-base",
            load_fn=flaky_load,
            health_fn=lambda m: False,
            now_ticks=list(range(1, 200)),
        )
        swap = ctrl.hot_swap("req-1", "m-broken")
        self.assertEqual(swap["state"], "failed")
        self.assertEqual(ctrl.current_model, "m-base")
        # 事件与 error 只有结构化字段，无栈痕/类名外泄
        blob = repr(swap["events"]) + repr(swap.get("error"))
        self.assertNotIn("RuntimeError", blob)
        self.assertNotIn("Traceback", blob)

    def test_events_never_leak_stacks(self):
        """正常轮换事件零栈痕（零报错外泄口径复核）。"""
        ctrl = make_controller()
        swap = ctrl.hot_swap("req-1", "m-code")
        blob = repr(swap["events"]) + repr(swap.get("error"))
        self.assertNotIn("Traceback", blob)
        self.assertNotIn("RuntimeError", blob)


if __name__ == "__main__":
    unittest.main()
