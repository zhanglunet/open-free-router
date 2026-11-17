# -*- coding: utf-8 -*-
"""PRD-468/T4（trend issue #2263 的 T4 环节）：运维上限与一键清除。

按 T1 设计 §4 落地：
  - 冷却封顶：单槽冷却时长不超过 max_cooldown_seconds（可配，默认 1 天）；
    提供方明说 retry_after 的恢复时间不被缩短（retry_after > 封顶时以
    retry_after 为准——实锤优先）；
  - 一键清除：clear_all_cooldowns() 解除全部冷却（运维动作，落账留痕）；
  - 按密钥精细重置：reset_slot(platform, model_id, slot) 只解指定槽。
零 LLM 零网络零子进程；示例全虚构占位。
测试：tests/test_prd468_t4_cooldown_ops.py
"""
from __future__ import annotations

from typing import Any

from cooldown_ladder import CooldownLadder, LadderError, LADDER_SCHEMA

OPS_SCHEMA = "cooldown-ops/v1"
ERROR_CODES = ("OPS_INCOMPLETE",)


def clamp_cooldown(seconds: float, *, retry_after: float | None = None,
                   max_cooldown_seconds: float = 86400) -> float:
    """冷却封顶：不超过 max；retry_after（实锤）不缩短。"""
    if retry_after is not None and retry_after > max_cooldown_seconds:
        return float(retry_after)              # 实锤优先，不缩
    return min(seconds, max_cooldown_seconds)


def clear_all_cooldowns(ladder: CooldownLadder, *, by: str,
                        at: float) -> dict[str, Any]:
    """一键解除全部冷却（运维动作，落账留痕）。"""
    if not by:
        raise LadderError("OPS_INCOMPLETE：操作人缺一不可")
    cleared = []
    for key, state in ladder.slots.items():
        if state.get("cooldown_until", 0) > at or \
                state.get("cooldown_until") == float("inf"):
            state["cooldown_until"] = 0
            state["step"] = 0
            cleared.append(key)
    ladder._log("ops_clear_all", "*", {"by": by, "at": at,
                                       "cleared": cleared})
    return {"schema": LADDER_SCHEMA, "cleared": cleared,
            "count": len(cleared), "by": by}


def reset_slot(ladder: CooldownLadder, *, platform: str, model_id: str,
               slot: int, by: str, at: float) -> dict[str, Any]:
    """按密钥精细重置：只解指定槽。"""
    if not by:
        raise LadderError("OPS_INCOMPLETE：操作人缺一不可")
    key = ladder._key(platform, model_id, slot)
    state = ladder.slots.get(key)
    if state is None or (state.get("cooldown_until", 0) <= at and
                         state.get("cooldown_until") != float("inf")):
        return {"schema": LADDER_SCHEMA, "reset": key, "was_active": False}
    state["cooldown_until"] = 0
    state["step"] = 0
    ladder._log("ops_reset_slot", key, {"by": by, "at": at})
    return {"schema": LADDER_SCHEMA, "reset": key, "was_active": True}
