# -*- coding: utf-8 -*-
"""PRD-476/T2~T4（trend issue #2301/#2302/#2303 的 T2~T4 环节）：
决策模型选路由（档位+置信度门+按轮缓存+成本护栏）。

按 T1 设计（docs/plans/2026-11-17-decision-routing-design.md）§1~§3 落地：
  - decision_fn 注入式决策调用（T2）：小模型读请求→输出档位与置信度；
    只重加权本次请求（不改全局策略）；
  - 按轮缓存（T3）：同一轮（request_id）只决策一次；低置信/超时/不可用
    均降级均衡（走默认全量路由，不偏科）；
  - 成本护栏（T4）：decision_budget_per_hour 上限（默认 100 次/小时）；
    开关 decision_routing_enabled 默认 false；不可用时全链路降级均衡。
纯内存实现；decision_fn 注入式零真实网络；示例全占位。
测试：tests/test_prd476_t2_decision_routing.py
"""
from __future__ import annotations

import copy
import time
from collections import deque
from typing import Any, Callable

ROUTING_SCHEMA = "decision-routing/v1"
TIERS = ("coding", "vision", "default")
DEFAULT_CONFIDENCE_THRESHOLD = 0.6
DEFAULT_DECISION_TIMEOUT_SECONDS = 2
DEFAULT_BUDGET_PER_HOUR = 100
ERROR_CODES = ("DECISION_DISABLED", "DECISION_BUDGET_EXCEEDED",
               "DECISION_TIMEOUT", "DECISION_LOW_CONFIDENCE")


class DecisionRoutingError(ValueError):
    """决策路由违规——显形拒绝。"""


def _require(ok: bool, code: str, detail: str) -> None:
    if not ok:
        raise DecisionRoutingError(f"{code}：{detail}")


class DecisionRouter:
    """决策模型选路由：小模型读请求→档位+置信度→重加权本次请求。"""

    def __init__(self, *, enabled: bool = False,
                 confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
                 timeout_seconds: float = DEFAULT_DECISION_TIMEOUT_SECONDS,
                 budget_per_hour: int = DEFAULT_BUDGET_PER_HOUR,
                 now: float = 0.0) -> None:
        self.enabled = enabled
        self.confidence_threshold = confidence_threshold
        self.timeout = timeout_seconds
        self.budget_per_hour = budget_per_hour
        self.now = now
        self._decision_times: deque = deque()       # 滑动 1h 窗口计成本
        self._round_cache: dict[str, dict[str, Any]] = {}

    def decide(self, *, request_id: str, prompt: str,
               decision_fn: Callable[[str], dict[str, Any]],
               ) -> dict[str, Any]:
        """T2+T3：决策调用（按轮缓存；低置信/超时降级均衡）。"""
        _require(self.enabled, "DECISION_DISABLED",
                 "决策路由默认关闭——显式设置 decision_routing_enabled=true 才可用")
        # T3 按轮缓存：同一轮只决策一次
        if request_id in self._round_cache:
            return copy.deepcopy(self._round_cache[request_id])
        # T4 成本护栏：budget_per_hour 上限
        self._prune_old()
        if len(self._decision_times) >= self.budget_per_hour:
            return self._degrade(request_id, "DECISION_BUDGET_EXCEEDED")
        # 决策调用（注入式）
        import time as _time
        t0 = _time.monotonic()
        try:
            raw = decision_fn(prompt)
        except Exception as exc:
            return self._degrade(request_id,
                                 f"decision_fn raised: {exc}")
        elapsed = _time.monotonic() - t0
        if elapsed > self.timeout:
            return self._degrade(request_id, "DECISION_TIMEOUT")
        tier = raw.get("suggested_tier", "")
        confidence = float(raw.get("confidence", 0))
        if tier not in TIERS:
            return self._degrade(request_id,
                                 f"tier {tier!r} 词表外")
        if confidence < self.confidence_threshold:
            return self._degrade(request_id, "DECISION_LOW_CONFIDENCE")
        result = {"schema": ROUTING_SCHEMA, "request_id": request_id,
                  "suggested_tier": tier, "confidence": confidence,
                  "reason": str(raw.get("reason", "")),
                  "degraded": False}
        self._round_cache[request_id] = copy.deepcopy(result)
        self._decision_times.append(self.now)
        return copy.deepcopy(result)

    def _degrade(self, request_id: str, reason: str) -> dict[str, Any]:
        """降级均衡：走默认全量路由（不偏科）。"""
        result = {"schema": ROUTING_SCHEMA, "request_id": request_id,
                  "suggested_tier": "default", "confidence": 0.0,
                  "degrade_reason": reason, "degraded": True}
        self._round_cache[request_id] = copy.deepcopy(result)
        return copy.deepcopy(result)

    def _prune_old(self) -> None:
        """滑动 1h 窗口：去掉 1 小时前的决策时间戳。"""
        cutoff = self.now - 3600
        while self._decision_times and self._decision_times[0] < cutoff:
            self._decision_times.popleft()
