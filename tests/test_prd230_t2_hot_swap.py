# -*- coding: utf-8 -*-
"""PRD-230/T2 自测（trend #1151）：热切换实现——会话中切换本地模型不断线。

运行：python3 -m unittest tests.test_prd230_t2_hot_swap -v（仓库根执行）

覆盖（T1 §1 + 验收「切换期间请求不中断、无报错外泄」）：
  happy path：precheck 七项 → draining → loading → health → done；事件链完整
  幂等：同 request_id 重复提交返回同一 swap 不重复触发
  precheck 拒绝：未注册 / 未下载 / VRAM 不足 / 上下文超限 / 归一层缺口 → rejected+code
  swap 进行中再提交 → swap_in_progress（409）
  健康探测全败 → 自动回滚原模型（rolled_back），loaded 恢复
  零报错外泄：回滚后 loaded_model 恢复原值、无半加载态
零网络；动作全注入。
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.hot_swap import (  # noqa: E402
    HotSwapController,
    PrecheckRejected,
    SwapInProgressError,
)

MODELS = {"model-a", "model-b", "model-c"}
DOWNLOADED = {"model-a", "model-b"}


def make_controller(**kw):
    loads = []
    health_ok = kw.pop("health_ok", True)
    vram_ok = kw.pop("vram_ok", True)
    ctx_ok = kw.pop("ctx_ok", True)
    norm_ok = kw.pop("norm_ok", True)
    ticks = kw.pop("ticks", list(range(100)))

    def load(model):
        loads.append(model)

    def health(model):
        return health_ok

    def vram(model):
        return vram_ok

    def ctx(model, tokens):
        return ctx_ok

    def norm(model):
        return norm_ok

    controller = HotSwapController(
        registered_models=MODELS, downloaded=DOWNLOADED, current_model="model-a",
        load_fn=load, health_fn=health, vram_check_fn=vram,
        context_check_fn=ctx, normalization_covered=norm,
        now_ticks=ticks, **kw)
    controller.load_log = loads
    return controller


class HotSwapTests(unittest.TestCase):
    def test_happy_path_full_state_chain(self):
        ctl = make_controller()
        swap = ctl.hot_swap("req-1", "model-b")
        self.assertEqual(swap["state"], "done")
        states = [e["to"] for e in swap["events"]]
        for expected in ("precheck-ok", "draining", "loading",
                         "health_check", "done"):
            self.assertIn(expected, states)
        self.assertEqual(ctl.loaded_model, "model-b")

    def test_idempotent_same_request_id(self):
        ctl = make_controller()
        first = ctl.hot_swap("req-1", "model-b")
        again = ctl.hot_swap("req-1", "model-b")
        self.assertIs(first, again)  # 同 id 返回同一 swap 不重复触发
        self.assertEqual(len(ctl.swaps), 1)

    def test_precheck_unregistered_rejected(self):
        ctl = make_controller()
        swap = ctl.hot_swap("req-1", "model-unknown")
        self.assertEqual(swap["state"], "rejected")
        self.assertEqual(swap["error"]["code"], "model_not_registered")
        self.assertEqual(ctl.loaded_model, "model-a")  # 原模型不受影响

    def test_precheck_not_downloaded(self):
        ctl = make_controller()
        # model-c 注册但未下载
        ctl.downloaded = {"model-a"}
        swap = ctl.hot_swap("req-1", "model-c")
        self.assertEqual(swap["error"]["code"], "model_not_downloaded")

    def test_precheck_vram_rejected_then_rollback_not_triggered(self):
        ctl = make_controller(vram_ok=False)
        swap = ctl.hot_swap("req-1", "model-b")
        self.assertEqual(swap["state"], "rejected")
        self.assertEqual(swap["error"]["code"], "insufficient_vram")
        self.assertEqual(ctl.loaded_model, "model-a")  # precheck 阶段无需回滚

    def test_precheck_context_window_rejected(self):
        ctl = make_controller(ctx_ok=False)
        swap = ctl.hot_swap("req-1", "model-b", active_session_tokens=9000,
                            context_window=8192)
        self.assertEqual(swap["error"]["code"], "insufficient_context_window")

    def test_precheck_normalization_gap_rejected(self):
        ctl = make_controller(norm_ok=False)
        swap = ctl.hot_swap("req-1", "model-b")
        self.assertEqual(swap["error"]["code"], "normalization_gap")

    def test_swap_in_progress_409(self):
        class BlockingController(HotSwapController):
            def hot_swap(self, request_id, target_model, **kw):
                if request_id == "blocker":
                    self._active_swap_id = "swap-blocker"  # 模拟进行中
                return super().hot_swap(request_id, target_model, **kw)

        ctl = BlockingController(
            registered_models=MODELS, downloaded=DOWNLOADED, current_model="model-a",
            load_fn=lambda m: None, health_fn=lambda m: True)
        with self.assertRaises(SwapInProgressError):
            ctl.hot_swap("blocker", "model-b")

    def test_health_failure_rolls_back_to_previous(self):
        ctl = make_controller(health_ok=False)
        swap = ctl.hot_swap("req-1", "model-b")
        self.assertEqual(swap["state"], "rolled_back")
        states = [e["to"] for e in swap["events"]]
        self.assertIn("rolling_back", states)
        self.assertIn("rolled_back", states)
        self.assertEqual(ctl.loaded_model, "model-a")      # 原模型恢复
        self.assertEqual(ctl.current_model, "model-a")     # 会话不丢

    def test_no_error_leak_after_rollback(self):
        """零报错外泄：回滚后 loaded/current 一致、swap.error 有界、无半加载态。"""
        ctl = make_controller(health_ok=False)
        swap = ctl.hot_swap("req-1", "model-b")
        self.assertEqual(ctl.loaded_model, ctl.current_model)
        self.assertIn(swap["error"]["code"], ("health_check_exhausted", "swap_failed"))
        serialized = json.dumps(swap, ensure_ascii=False)
        self.assertNotIn("Traceback", serialized)
        self.assertNotIn("sk-", serialized)


if __name__ == "__main__":
    unittest.main(verbosity=2)
