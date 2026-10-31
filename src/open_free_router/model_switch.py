#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRD-202/T2（trend 前区间）：一句话换模型——意图解析 + 回显确认 + 回滚。

按 PRD-202/T1 设计实现 T2 最小范围：
  parse_intent(sentence) → {action: switch|rollback|status, target_route, qualifier}
  echo_confirm(intent, current_route) → {will_switch_to, estimated_cost_delta, confirmed: false}
  do_switch(confirmed_intent) → 切换 + 审计事件
  undo_switch() → 回滚到切换前路由
  红线：确认前零动作；学习网关只出推荐、不自动执行。
零网络；示例全虚构。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

QUALIFIERS = ("cheap", "fast", "quality", "current")
ACTIONS = ("switch", "rollback", "status")


def parse_intent(sentence: str) -> dict[str, Any]:
    """解析用户意图：换成便宜的 / 换回原来的 / 现在用的什么。"""
    s = sentence.strip().lower()
    if "换回" in s or "回滚" in s or "还原" in s:
        return {"action": "rollback"}
    if "现在" in s or "用的什么" in s or "当前路由" in s:
        return {"action": "status"}
    for kw in ("便宜", "快的", "高质量", "贵的", "快"):
        if kw in s:
            return {"action": "switch", "qualifier": kw}
    return {"action": "unknown"}


def echo_confirm(intent: dict[str, Any], current_route: str,
                 candidates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """回显确认：展示将切换到的后端与预估变化，不执行切换。"""
    action = intent.get("action")
    if action == "rollback":
        return {"action": "rollback", "confirmed": False,
                 "message": "回滚到切换前路由，需确认"}
    if action == "switch":
        qualifier = intent.get("qualifier", "")
        # 从候选中找最匹配的后端（示例逻辑：按 qualifier 选）
        target = qualifier  # 简化：目标路由 = qualifier
        return {"action": "switch", "target_route": target,
                 "current_route": current_route, "confirmed": False,
                 "message": f"将切换到 {target}，确认后执行"}
    return {"action": "unknown", "message": "无法解析意图"}


def do_switch(intent: dict[str, Any], current_route: str,
              switch_fn: Callable[[str], Any]) -> dict[str, Any]:
    """执行切换（只在 confirmed 后调用）。"""
    target = intent.get("target_route")
    if not target:
        return {"ok": False, "error": "无目标路由"}
    result = switch_fn(target)
    return {"ok": True, "switched_to": target, "result": result}




class SwitchExecutor:
    """PRD-202/T3（trend #1012）：确认后切换执行 + 与 PRD-201 可观测 trace 打通。

    红线（T1 §3/PRD）：未确认不切换——echo_confirm 产出 confirmed=False，
    只有用户明确确认后的 intent（confirmed=True）才允许到达本执行器；
    每次执行（成功或失败）向 PRD-201 的 RouteObservatory 记一条 trace（切换留痕）。
    """

    def __init__(self, observatory) -> None:
        from open_free_router.route_obs import RouteTrace  # 延迟导入：与 201 的 trace 结构对接
        self._trace_cls = RouteTrace
        self.observatory = observatory
        self._history = SwitchHistory()  # T4：切换历史（undo_switch 用）

    def execute(self, intent: dict[str, Any], current_route: str,
                switch_fn: Callable[[str], Any], *, request_id: str) -> dict[str, Any]:
        """执行一次已确认的切换；未确认/无目标一律拒绝且零副作用。"""
        if not intent.get("confirmed"):
            return {"ok": False, "error": "未确认不切换（echo_confirm 的 confirmed=False）",
                    "switched": False}
        target = intent.get("target_route")
        if not target:
            return {"ok": False, "error": "无目标路由", "switched": False}
        try:
            result = switch_fn(target)
        except Exception as exc:  # 失败也留 trace（切换留痕不漏失败）
            self._record(current_route, target, request_id, status="failed",
                         failure_class=type(exc).__name__)
            return {"ok": False, "error": str(exc), "switched": False,
                    "from_route": current_route, "to_route": target}
        self._record(current_route, target, request_id, status="ok")
        self._history.record(current_route, target, request_id)  # T4：入栈供回滚
        return {"ok": True, "switched": True, "from_route": current_route,
                "to_route": target, "result": result}

    def _record(self, from_route: str, to_route: str, request_id: str, *,
                status: str, failure_class: str | None = None) -> None:
        from datetime import datetime, timezone
        self.observatory.record(self._trace_cls(
            route_id="model-switch", backend_id=from_route, model=to_route,
            request_id=request_id, latency_ms=0.0, cost_usd=0.0, status=status,
            failure_class=failure_class,
            at=datetime.now(timezone.utc).isoformat(timespec="seconds")))


class SwitchHistory:
    """PRD-202/T4（trend #1013）：切换历史与一键回滚。

    execute 每次成功切换记录 {from_route, to_route}；undo_switch 切回上一后端——
    同样走确认后的执行路径并留 trace（rollback 标记）；无历史可回滚 → 拒绝。
    """

    def __init__(self) -> None:
        self.stack: list[dict[str, str]] = []

    def record(self, from_route: str, to_route: str, request_id: str) -> None:
        self.stack.append({"from_route": from_route, "to_route": to_route,
                           "request_id": request_id})


def undo_switch(executor: "SwitchExecutor", current_route: str,
                switch_fn: Callable[[str], Any], *, request_id: str,
                confirmed: bool = True) -> dict[str, Any]:
    """一键回滚：切回最近一次切换的 from_route（回滚也留 trace，标记 rollback）。

    失败时历史不弹栈（可重试）；无历史或未确认 → 拒绝。
    """
    history = getattr(executor, "_history", None)
    if history is None:
        history = SwitchHistory()
        executor._history = history
    if not history.stack:
        return {"ok": False, "error": "无可回滚的切换记录"}
    if not confirmed:
        return {"ok": False, "error": "未确认不回滚"}
    # 跳过已是回滚的条目：连发回滚应沿原始切换链逐步回退（回滚的回滚 = 前进）
    while history.stack and history.stack[-1].get("kind") == "rollback":
        history.stack.pop()
    if not history.stack:
        return {"ok": False, "error": "无可回滚的切换记录（已回滚到最早）"}
    last = history.stack[-1]
    target = last["from_route"]
    result = executor.execute({"action": "rollback", "target_route": target,
                               "confirmed": True}, current_route, switch_fn,
                              request_id=request_id)
    if result.get("ok"):
        # 双标记：被回滚的原始条目 + 回滚切换本身，都不再作为后续回滚目标
        last["kind"] = "rollback"
        history.stack[-1]["kind"] = "rollback"
        last_trace = executor.observatory.traces[-1]
        last_trace["route_id"] = "model-switch-rollback"  # 回滚留痕（可区分）
    return result
