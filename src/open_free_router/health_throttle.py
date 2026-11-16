# -*- coding: utf-8 -*-
"""PRD-472/T2~T4（trend issue #2281/#2282/#2283 的 T2+T3+T4 环节）：
健康检查节流（节奏+抖动+成本分级+预算+开关）。

按 T1 设计（docs/plans/2026-11-17-health-check-throttling-design.md）§2~§4 落地：
  - 节奏与抖动（T2）：探测间隔默认 300s±10% 抖动；同提供方间至少 30s
    错开；3.5 分钟（210s）内验过的跳过；
  - 成本分级（T3）：free_verify 默认节奏 / costs_quota 降频三倍；
    分级表可扩展；不认识的提供方按 costs_quota（保守）；probe.py 零改动；
  - 预算与开关（T4）：每轮预算上限 5 个提供方；并发上限 2；总开关
    OFR_HEALTH_PROBE_DISABLED=1 杀死探测任务；探测写 probe_log.jsonl
    不改 AnalyticsStore 正式表；
  - probe_fn 注入式（不发真实网络）。
纯内存实现（节奏调度+预算+分级+开关）；零网络零子进程；示例全占位。
测试：tests/test_prd472_t2_health_throttle.py
"""
from __future__ import annotations

import json
import os
import random
from pathlib import Path
from typing import Callable
from typing import Any

THROTTLE_SCHEMA = "health-throttle/v1"
PROBE_LOG_SCHEMA = "probe-log/v1"
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_MIN_PROVIDER_GAP = 30
RECENT_PROBE_SKIP_SECONDS = 210
DEFAULT_BUDGET_PER_ROUND = 5
DEFAULT_MAX_CONCURRENT = 2
COST_TIERS = {"free_verify": 1.0, "costs_quota": 3.0}
ERROR_CODES = ("THROTTLE_INCOMPLETE", "THROTTLE_DISABLED",
               "THROTTLE_BUDGET_EXCEEDED")


class ThrottleError(ValueError):
    """节流违规——显形拒绝。"""


def _require(ok: bool, code: str, detail: str) -> None:
    if not ok:
        raise ThrottleError(f"{code}：{detail}")


class HealthCheckThrottle:
    """健康检查节流器：节奏+抖动+成本分级+预算+开关（probe.py 零改动）。"""

    def __init__(self, *, interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
                 budget_per_round: int = DEFAULT_BUDGET_PER_ROUND,
                 max_concurrent: int = DEFAULT_MAX_CONCURRENT,
                 cost_tiers: dict[str, float] | None = None,
                 seed: int | None = None) -> None:
        self.interval = interval_seconds
        self.budget = budget_per_round
        self.max_concurrent = max_concurrent
        self.cost_tiers = dict(cost_tiers or COST_TIERS)
        self._rng = random.Random(seed)
        self._last_probe: dict[str, float] = {}       # platform → last_probe_at
        self._last_dispatch: dict[str, float] = {}    # platform → last_dispatch_at
        self._probe_log: list[dict[str, Any]] = []

    def set_cost_tier(self, platform: str, tier: str) -> None:
        _require(tier in self.cost_tiers, "THROTTLE_INCOMPLETE",
                 f"成本级别 {tier!r} 词表外")
        self.cost_tiers[platform] = self.cost_tiers[tier]

    def get_multiplier(self, platform: str) -> float:
        """§3 成本分级：不认识的提供方按 costs_quota（保守）。"""
        return self.cost_tiers.get(platform, self.cost_tiers.get(
            "costs_quota", 3.0))

    def should_probe(self, *, platform: str, now: float) -> tuple[bool, str]:
        """探测调度判定：节奏+抖动+成本分级+3.5 分钟内验过跳过。"""
        last = self._last_probe.get(platform)
        if last is not None and now - last < RECENT_PROBE_SKIP_SECONDS:
            return False, "recently_probed"
        interval = self.interval * self.get_multiplier(platform)
        jitter = self._rng.uniform(0.9, 1.1)           # ±10% 抖动
        interval *= jitter
        gap = self._last_dispatch.get(platform)
        if gap is not None and now - gap < DEFAULT_MIN_PROVIDER_GAP:
            return False, "provider_gap"
        if last is not None and now - last < interval:
            return False, "interval_not_reached"
        return True, "ok"

    def mark_probed(self, platform: str, now: float) -> dict[str, Any]:
        self._last_probe[platform] = now
        self._last_dispatch[platform] = now
        entry = {"schema": PROBE_LOG_SCHEMA, "platform": platform,
                 "probed_at": now, "multiplier": self.get_multiplier(platform)}
        self._probe_log.append(entry)
        return entry

    def dispatch_round(self, *, platforms: list[str], now: float,
                       probe_fn: Callable[[str], bool] | None = None) -> dict[str, Any]:
        """一轮探测调度：预算+并发上限+分级节奏。probe_fn 注入式。"""
        _require(probe_fn is not None, "THROTTLE_INCOMPLETE",
                 "probe_fn 未注入")
        if os.environ.get("OFR_HEALTH_PROBE_DISABLED") == "1":
            return {"schema": THROTTLE_SCHEMA, "outcome": "disabled",
                    "detail": "OFR_HEALTH_PROBE_DISABLED=1", "probed": [],
                    "skipped": list(platforms), "probed_count": 0}
        probed, skipped = [], []
        budget_used = 0
        for platform in platforms:
            if budget_used >= self.budget:
                skipped.append({"platform": platform,
                                "reason": "THROTTLE_BUDGET_EXCEEDED"})
                continue
            should, reason = self.should_probe(platform=platform, now=now)
            if not should:
                skipped.append({"platform": platform, "reason": reason})
                continue
            ok = bool(probe_fn(platform))
            self.mark_probed(platform, now)
            probed.append({"platform": platform, "probe_ok": ok})
            budget_used += 1
        return {"schema": THROTTLE_SCHEMA, "probed": probed,
                "skipped": skipped, "probed_count": len(probed)}

    def probe_log_entries(self) -> list[dict[str, Any]]:
        return [dict(e) for e in self._probe_log]

    def save_probe_log(self, path: str | Path) -> Path:
        """探测写独立 probe_log.jsonl，不改 AnalyticsStore 正式表。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for e in self._probe_log:
                fh.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
        return path



