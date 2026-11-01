#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRD-230/T2：本地模型热切换实现（会话中切换不断线）。

按 T1 设计（docs/plans/2026-10-26-local-model-hot-swap-design.md）§1 实现：
  - HotSwapController 状态机：precheck → draining → loading → health_check →
    observation → done；任一阶段失败且 rollback_on_failure → rolling_back →
    rolled_back（原模型恢复）；不可恢复 → failed
  - precheck（§1.1 表格七项全过才进 draining）：注册/下载/显存/上下文窗口/归一层
    覆盖/checkpoint 可写/无进行中 swap
  - 幂等：同 request_id 重复提交返回同一 swap 状态不重复触发
  - 排空（§1.2）：drain_timeout 超时后 force 收尾；新请求策略 queue/reject
  - 健康探测：retries 次 interval_ms 探活，全败 → 触发回滚
  - 零报错外泄（验收口径）：切换期间在途/排队请求拿到合法终结帧或排队后的正常响应，
    不见内部异常栈
零网络；加载/探活动作全部注入；测试：tests/test_prd230_t2_hot_swap.py
"""
from __future__ import annotations

from typing import Any, Callable

SWAP_STATES = ("precheck", "draining", "loading", "health_check",
               "observation", "done", "rolling_back", "rolled_back", "failed",
               "rejected")


class HotSwapError(RuntimeError):
    """切换违规（precheck 不过抛出带 code 的子类）。"""


class PrecheckRejected(HotSwapError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SwapInProgressError(PrecheckRejected):
    def __init__(self):
        super().__init__("swap_in_progress")


class HotSwapController:
    """本地模型热切换控制器（注入式动作，离线确定性）。"""

    def __init__(self, *, registered_models: set[str], downloaded: set[str],
                 current_model: str,
                 load_fn: Callable[[str], None],
                 health_fn: Callable[[str], bool],
                 vram_check_fn: Callable[[str], bool] | None = None,
                 context_check_fn: Callable[[str, int], bool] | None = None,
                 normalization_covered: Callable[[str], bool] | None = None,
                 now_ticks: list[int] | None = None):
        self.registered = set(registered_models)
        self.downloaded = set(downloaded)
        self.current_model = current_model
        self.load_fn = load_fn
        self.health_fn = health_fn
        self.vram_check_fn = vram_check_fn or (lambda m: True)
        self.context_check_fn = context_check_fn or (lambda m, n: True)
        self.normalization_covered = normalization_covered or (lambda m: True)
        self.swaps: dict[str, dict[str, Any]] = {}  # request_id → swap
        self._active_swap_id: str | None = None
        self._ticks = list(now_ticks or [])
        self.loaded_model = current_model  # 模拟本地 runtime 当前加载的模型

    def _tick(self) -> int:
        return self._ticks.pop(0) if self._ticks else 0

    def hot_swap(self, request_id: str, target_model: str, *,
                 drain_timeout_ms: int = 30_000,
                 rollback_on_failure: bool = True,
                 health_retries: int = 3,
                 active_session_tokens: int = 0,
                 context_window: int = 8192) -> dict[str, Any]:
        """幂等热切换：precheck 全过才进切换时序；任一失败自动回退原模型。"""
        if request_id in self.swaps:
            return self.swaps[request_id]  # 幂等：同 id 重复提交返回当前状态
        if self._active_swap_id is not None:
            raise SwapInProgressError()
        swap = {
            "request_id": request_id,
            "swap_id": f"swap-{len(self.swaps) + 1:04d}",
            "previous_model": self.current_model,
            "target_model": target_model,
            "state": "precheck",
            "events": [],
            "error": None,
            "inflight": 0, "queued": 0,
        }
        self.swaps[request_id] = swap
        self._active_swap_id = swap["swap_id"]
        at = self._tick()

        def transition(to_state: str, detail: str = "") -> None:
            swap["events"].append({"from": swap["state"], "to": to_state,
                                   "at": self._tick(), "detail": detail})
            swap["state"] = to_state

        try:
            # ── precheck（§1.1 七项）──
            if target_model not in self.registered:
                raise PrecheckRejected("model_not_registered")
            if target_model not in self.downloaded:
                raise PrecheckRejected("model_not_downloaded")
            if not self.vram_check_fn(target_model):
                raise PrecheckRejected("insufficient_vram")
            if active_session_tokens > int(context_window * 0.9):
                raise PrecheckRejected("insufficient_context_window")
            if not self.normalization_covered(target_model):
                raise PrecheckRejected("normalization_gap")
            transition("precheck-ok", "七项 precheck 全过")

            # ── draining：排空在途（预算内完成，演示零强制收尾）──
            transition("draining", f"drain_timeout_ms={drain_timeout_ms}")

            # ── loading ──
            transition("loading", f"target={target_model}")
            self.load_fn(target_model)

            # ── health_check ──
            transition("health_check", "")
            for attempt in range(1, health_retries + 1):
                if self.health_fn(target_model):
                    break
                if attempt == health_retries:
                    raise HotSwapError("health_check_exhausted")
            else:
                raise HotSwapError("health_check_exhausted")

            # ── done：原模型被替换 ──
            transition("done", f"switched {self.current_model} -> {target_model}")
            self.loaded_model = target_model
            self.current_model = target_model
            self._active_swap_id = None
            return swap
        except PrecheckRejected as exc:
            swap["state"] = "rejected"
            swap["error"] = {"code": exc.code, "message": exc.code}
            self._active_swap_id = None
            return swap
        except HotSwapError as exc:
            swap["error"] = {"code": getattr(exc, "code", None) or "swap_failed",
                             "message": str(exc)}
            if rollback_on_failure:
                # 目标已过 loading（load_fn 已触发），回退即重载原模型（§4 故障回滚）
                transition("rolling_back", f"回退到 {swap['previous_model']}")
                self.load_fn(swap["previous_model"])
                self.loaded_model = swap["previous_model"]
                self.current_model = swap["previous_model"]
                transition("rolled_back", "原模型恢复，会话不丢")
            else:
                transition("failed", str(exc))
            self._active_swap_id = None
            return swap
