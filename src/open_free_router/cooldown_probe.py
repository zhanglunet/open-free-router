# -*- coding: utf-8 -*-
"""PRD-468/T3（trend issue #2262 的 T3 环节）：探测恢复任务。

按 T1 设计 §3 落地：每轮扫冷却中的凭证槽，只对**猜测性冷却**（429 且
retry_after=None）发起探测；探测成功提前解除（阶梯清零）；失败不延长；
实锤冷却（402/401）绝不被探测；每轮预算 probe_budget（默认 3）。
probe_fn 注入式（不发真实网络）；返回 True=探测成功。
零 LLM 零网络零子进程（probe_fn 注入）；示例全虚构占位。
测试：tests/test_prd468_t3_cooldown_probe.py
"""
from __future__ import annotations

from typing import Any, Callable

from cooldown_ladder import LadderError, CooldownLadder, LADDER_SCHEMA

PROBE_SCHEMA = "cooldown-probe/v1"
ERROR_CODES = ("PROBE_INCOMPLETE", "PROBE_BUDGET_EXCEEDED")
DEFAULT_PROBE_BUDGET = 3


def probe_round(ladder: CooldownLadder, *, now: float,
                probe_fn: Callable[[str], bool] | None = None,
                budget: int = DEFAULT_PROBE_BUDGET) -> dict[str, Any]:
    """一轮探测：挑猜测性冷却槽逐个 probe_fn；成功解除，失败不延长。

    实锤冷却（402/401）绝不被探测；预算超限的槽留到下一轮。
    """
    if probe_fn is None:
        raise LadderError("PROBE_INCOMPLETE：probe_fn 未注入")
    ladder.now = now                       # 探测轮推进时钟（与 is_cooled_down 一致）
    candidates = []
    for key, state in ladder.slots.items():
        if state.get("reason") != "429_limited":
            continue                                  # 实锤绝不被探测
        if state.get("cooldown_until", 0) <= now:
            continue                                  # 已自然到点，无需探测
        candidates.append(key)
    results = {"schema": PROBE_SCHEMA, "at": now, "probed": [],
               "released": [], "budget_exceeded": len(candidates) - budget}
    for key in candidates[:budget]:
        ok = bool(probe_fn(key))
        results["probed"].append({"key": key, "probe_ok": ok})
        if ok:
            state = ladder.slots[key]
            state["step"] = 0                          # 阶梯清零
            state["reason"] = "probe_recovered"
            state["cooldown_until"] = now              # 提前解除
            results["released"].append(key)
            ladder._log("probe_recovered", key, {"at": now})
        # 失败不延长：cooldown_until 不动（阶梯自然到点）
    return results
