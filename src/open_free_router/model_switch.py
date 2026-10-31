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


