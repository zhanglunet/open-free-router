#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRD-201/T3（trend #1007）：路由可观测实时看板——路由分布 / 失败原因 Top / 成本曲线。

按 PRD-201/T1 设计（docs/plans/2026-10-26-routing-observability-design.md）§4/§5 实现：
  rollup_traces(traces, bucket_minutes=60)：按小时桶聚合（维度 bucket_at/provider/model/
    status/error_kind），度量 requests/successes/p50/p95/cost_sum/原因 Top——对应 §4.1
    聚合表口径（内存版；持久化 rollup 表归 T5 后的部署形态，本文 §4.2 允许）；
  board_summary(traces, window)          # A 路由分布 + B 失败原因 Top（含环比）+ C 成本曲线
  trace_drawer(traces, **filters)        # E 明细抽屉：维度过滤 + limit 硬上限 200

验收口径「看板数据与 trace 一致」：所有聚合值由同一份 trace 推导，测试逐项对账
（总数/占比/分位/成本曲线合计 == 原始 trace 合计）。零网络；示例全虚构。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

DRAWER_LIMIT_MAX = 200
TOP_REASONS = 10
WINDOWS = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7)}


def _parse_at(at: str) -> datetime:
    return datetime.fromisoformat(at.replace("Z", "+00:00"))


def _percentile(values: list[float], q: float) -> float | None:
    """最近邻序统计分位（口径同 latency.py 的 nearest-rank）。"""
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, __import__("math").ceil(q * len(ordered)) - 1))
    return ordered[idx]


def rollup_traces(traces: list[dict[str, Any]], bucket_minutes: int = 60) -> list[dict[str, Any]]:
    """按小时桶聚合（§4.1 维度与度量列）。"""
    buckets: dict[tuple, dict[str, Any]] = {}
    for t in traces:
        at = _parse_at(t["at"])
        slot = at.replace(minute=(at.minute // bucket_minutes) * bucket_minutes,
                          second=0, microsecond=0)
        key = (slot.isoformat(), t["backend_id"], t["model"], t["status"],
               t.get("failure_class"))
        b = buckets.setdefault(key, {
            "bucket_at": key[0], "selected_provider": key[1], "selected_model": key[2],
            "status": key[3], "error_kind": key[4],
            "requests": 0, "successes": 0,
            "latencies": [], "cost_units_sum": 0.0, "reasons": Counter(),
        })
        b["requests"] += 1
        b["successes"] += 1 if t["status"] == "ok" else 0
        b["latencies"].append(float(t["latency_ms"]))
        b["cost_units_sum"] += float(t["cost_usd"])
        if t.get("failure_class"):
            b["reasons"][t["failure_class"]] += 1
    out = []
    for b in buckets.values():
        lats = b.pop("latencies")
        reasons = b.pop("reasons")
        b["ttfb_p50_ms"] = _percentile(lats, 0.5)
        b["ttfb_p95_ms"] = _percentile(lats, 0.95)
        b["cost_units_sum"] = round(b["cost_units_sum"], 6)
        b["failover_reasons"] = dict(reasons.most_common(TOP_REASONS))
        out.append(b)
    out.sort(key=lambda b: (b["bucket_at"], b["selected_model"], b["error_kind"] or ""))
    return out


def board_summary(traces: list[dict[str, Any]], window: str = "24h",
                  now: datetime | None = None) -> dict[str, Any]:
    """A/B/C 三区块一次返回（§5.2）；window ∈ 1h/24h/7d；now 显式传入可复算。"""
    if window not in WINDOWS:
        raise ValueError(f"window 必须是 {sorted(WINDOWS)}")
    moment = now or (_parse_at(max(t["at"] for t in traces)) if traces
                     else datetime.now(timezone.utc))
    horizon = WINDOWS[window]
    in_window = [t for t in traces
                 if moment - _parse_at(t["at"]) <= horizon]
    prev_start, prev_end = moment - 2 * horizon, moment - horizon
    prev_window = [t for t in traces if prev_start < _parse_at(t["at"]) <= prev_end]

    total = len(in_window)
    dist: dict[str, dict[str, Any]] = {}
    for t in in_window:
        key = f"{t['backend_id']}/{t['model']}"
        d = dist.setdefault(key, {"requests": 0, "successes": 0, "failovers": 0})
        d["requests"] += 1
        d["successes"] += 1 if t["status"] == "ok" else 0
        d["failovers"] += 1 if t.get("failure_class") else 0
    routes = []
    for key, d in sorted(dist.items(), key=lambda kv: -kv[1]["requests"]):
        routes.append({
            "provider_model": key,
            "requests": d["requests"],
            "share": round(d["requests"] / total, 4) if total else 0.0,
            "success_rate": round(d["successes"] / d["requests"], 4) if d["requests"] else 0.0,
            "failover_rate": round(d["failovers"] / d["requests"], 4) if d["requests"] else 0.0,
        })

    cur_reasons = Counter(t["failure_class"] for t in in_window if t.get("failure_class"))
    prev_reasons = Counter(t["failure_class"] for t in prev_window if t.get("failure_class"))
    prev_total = sum(prev_reasons.values())
    failures = []
    for kind, count in cur_reasons.most_common(TOP_REASONS):
        prev_count = prev_reasons.get(kind, 0)
        failures.append({
            "error_kind": kind,
            "count": count,
            "share": round(count / total, 4) if total else 0.0,
            "delta_vs_prev": (round((count - prev_count) / prev_count, 4)
                              if prev_count else (1.0 if count else 0.0)),
        })

    by_bucket: dict[str, dict[str, float]] = {}
    for t in in_window:
        slot = _parse_at(t["at"]).replace(minute=0, second=0, microsecond=0).isoformat()
        b = by_bucket.setdefault(slot, {"cost_units": 0.0, "requests": 0})
        b["cost_units"] += float(t["cost_usd"])
        b["requests"] += 1
    cost_curve = [{"bucket_at": slot, "cost_units": round(v["cost_units"], 6),
                   "requests": v["requests"]}
                  for slot, v in sorted(by_bucket.items())]
    return {
        "window": window,
        "generated_at": moment.isoformat(timespec="seconds"),
        "total_requests": total,
        "route_distribution": routes,          # 区块 A
        "failure_top": failures,               # 区块 B
        "cost_curve": cost_curve,              # 区块 C
        "cost_note": "cost_units 为相对成本单位，非金额",
    }


def trace_drawer(traces: list[dict[str, Any]], *, window: str = "24h",
                 provider: str | None = None, model: str | None = None,
                 error_kind: str | None = None, limit: int = 50,
                 now: datetime | None = None) -> dict[str, Any]:
    """区块 E 明细抽屉：维度过滤点查；limit 超上限按 200 截断（§5.3）。"""
    limit = min(max(1, int(limit)), DRAWER_LIMIT_MAX)
    if window not in WINDOWS:
        raise ValueError(f"window 必须是 {sorted(WINDOWS)}")
    moment = now or (_parse_at(max(t["at"] for t in traces)) if traces
                     else datetime.now(timezone.utc))
    horizon = WINDOWS[window]
    rows = [t for t in traces
            if moment - _parse_at(t["at"]) <= horizon
            and (provider is None or t["backend_id"] == provider)
            and (model is None or t["model"] == model)
            and (error_kind is None or t.get("failure_class") == error_kind)]
    rows.sort(key=lambda t: t["at"], reverse=True)
    return {"window": window, "limit": limit, "matched": len(rows),
            "traces": rows[:limit]}
