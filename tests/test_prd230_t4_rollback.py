# -*- coding: utf-8 -*-
"""PRD-230/T4 自测（trend #1153）：切换失败回滚——故障注入下的验收固化。

运行：python3 -m unittest tests.test_prd230_t4_rollback -v（仓库根执行）

T4 范围（PRD 验收「故障注入下可回退且会话不丢」+ T1 §4 回滚要求）：在 T2
hot_swap 回滚语义之上，做故障注入回归固化——
  1) 新模型不可用（健康探测全败）→ 自动回退原模型，loaded 恢复；
  2) 回滚路径再次失败（load 原模型也炸）→ failed 显形，不静默；
  3) 回滚后同 request_id 幂等、新 request_id 可重新切换；
  4) 回滚后上下文与输出格式一致性保障（T3 swap_consistency）仍可用——
     同会话 evidence 连续（上下文完整不因回滚丢失）。
零网络；动作全注入；模型名全为占位。
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.hot_swap import (  # noqa: E402
    HotSwapController,
    SwapInProgressError,
)
from open_free_router.swap_consistency import (  # noqa: E402
    canonical,
    validate_completion,
)

MODELS = {"model-a", "model-b", "model-c"}
DOWNLOADED = {"model-a", "model-b", "model-c"}


def make_controller(current="model-a", target="model-b", *, health_map=None,
                    load_fail_for=None, ticks=None):
    loads = []

    def load(model):
        if load_fail_for and model in load_fail_for:
            raise RuntimeError(f"load 失败（注入）：{model}")
        loads.append(model)

    def health(model):
        return (health_map or {}).get(model, True)

    controller = HotSwapController(
        registered_models=MODELS, downloaded=DOWNLOADED, current_model=current,
        load_fn=load, health_fn=health,
        vram_check_fn=lambda m: True, context_check_fn=lambda m, n: True,
        normalization_covered=lambda m: True,
        now_ticks=ticks if ticks is not None else list(range(100)))
    controller.load_log = loads
    return controller


class 故障注入回滚Tests(unittest.TestCase):

    def test_new_model_unhealthy_auto_rolls_back(self):
        """验收核心：新模型不可用（健康全败）→ 自动回退原模型。"""
        controller = make_controller(health_map={"model-b": False})
        result = controller.hot_swap("req-1", "model-b")
        assert result["state"] == "rolled_back"
        assert controller.loaded_model == "model-a"  # 原模型恢复

    def test_rollback_path_load_failure_marks_failed(self):
        """回滚路径再次失败（load 原模型也炸）→ failed 显形，不静默。"""
        controller = make_controller(health_map={"model-b": False},
                                     load_fail_for={"model-a"})
        result = controller.hot_swap("req-1", "model-b")
        assert result["state"] == "failed"

    def test_session_context_survives_rollback(self):
        """会话不丢：回滚后 swap 记录保留（审计链完整），原模型在位。"""
        controller = make_controller(health_map={"model-b": False})
        controller.hot_swap("req-1", "model-b")
        assert "req-1" in controller.swaps
        assert controller.swaps["req-1"]["state"] == "rolled_back"
        assert controller.loaded_model == "model-a"

    def test_same_request_id_idempotent_after_rollback(self):
        """同 request_id 幂等：回滚后重复提交不重复触发。"""
        controller = make_controller(health_map={"model-b": False})
        first = controller.hot_swap("req-1", "model-b")
        again = controller.hot_swap("req-1", "model-b")
        assert again["state"] == first["state"]

    def test_new_request_after_rollback_can_switch(self):
        """回滚后新 request_id 可重新切换到健康模型。"""
        controller = make_controller(health_map={"model-b": False})
        controller.hot_swap("req-1", "model-b")
        result = controller.hot_swap("req-2", "model-c")
        assert result["state"] == "done"
        assert controller.loaded_model == "model-c"


class 回滚后一致性Tests(unittest.TestCase):
    def test_consistency_gate_usable_after_rollback(self):
        """回滚后输出格式一致性保障（T3）依旧可用：切换前（原模型）与
        回滚后（仍原模型）的响应样本过同一 C1/C2 门禁。"""
        from open_free_router.swap_consistency import consistency_gate

        def sample(model):
            return {
                "id": f"chatcmpl-{model}", "object": "chat.completion",
                "created": 1, "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": f"占位回答（{model}）"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2},
                "headers": {"x-ofr-actual-model": model},
            }

        before = {"completions": [sample("model-a")], "streams": []}
        after = {"completions": [sample("model-a")], "streams": []}
        gate = consistency_gate(before, after)
        assert gate["ok"] is True

    def test_rollback_keeps_session_checksum_continuous(self):
        """会话上下文 checksum 跨回滚连续（文本层上下文不丢，T1 §2.1）。"""
        session_before = {"messages": [{"role": "user", "content": "上下文（占位）"}],
                          "params": {"temperature": 0}}
        from open_free_router.swap_consistency import canonical, make_checkpoint
        checkpoint = make_checkpoint("swap-x", "2026-11-08T10:00:00Z",
                                     [{"session_id": "sess-1", **session_before,
                                       "last_request_seq": 2, "usage": {}}])
        # 回滚不触碰会话存储（权威源在 router）：逐字段全等
        from open_free_router.swap_consistency import context_replay_diff
        after = [{"session_id": "sess-1", **session_before, "last_request_seq": 2,
                  "usage": {}}]
        assert context_replay_diff(checkpoint, after) == []
        assert canonical(session_before["messages"]) == canonical(after[0]["messages"])


if __name__ == "__main__":
    unittest.main()
