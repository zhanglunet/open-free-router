#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRD-201/T2（trend #962 前区间）：路由决策可观测——实时 trace + 固定评估集跑分。

按 PRD-201/T1 设计实现 T2 最小范围：
  RouteTrace 记录器：每次路由/换路记录 trace（选中模型/延迟/成本/失败原因）；
  评估集跑分：固定提示词集按当前路由策略定期重跑，产出质量漂移报告（机读）；
  实时可观测视图：route_obs_view() 输出人读表格。
  与 PRD-149 审计账本划界（149 事后审计、201 实时可观测 + 质量回归）。
零网络；示例全虚构。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

TRACE_SCHEMA = "route-obs-trace-v1"
EVAL_SCHEMA = "route-eval-run-v1"


@dataclass
class RouteTrace:
    route_id: str
    backend_id: str
    model: str
    request_id: str
    latency_ms: float
    cost_usd: float
    status: str  # ok | failed | cancelled
    failure_class: str | None = None
    at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id, "backend_id": self.backend_id,
            "model": self.model, "request_id": self.request_id,
            "latency_ms": self.latency_ms, "cost_usd": self.cost_usd,
            "status": self.status, "failure_class": self.failure_class, "at": self.at,
        }


class RouteObservatory:
    """路由可观测记录器：实时 trace + 质量回归评估。"""

    def __init__(self) -> None:
        self.traces: list[dict[str, Any]] = []

    def record(self, trace: RouteTrace) -> None:
        self.traces.append(trace.to_dict())

    def view(self) -> str:
        """人读表格：route/backend/status/latency/cost/failure。"""
        lines = ["route_id      backend       model       status    latency(ms)  cost(USD)  failure"]
        for t in self.traces:
            lines.append(
                f"{t['request_id']:<14} {t['backend_id']:<12} {t['model']:<12} "
                f"{t['status']:<10} {t['latency_ms']:>8.0f}      ${t['cost_usd']:.4f}   "
                f"{t['failure_class'] or '-'}")
        return "\n".join(lines) + "\n"

    def export_jsonl(self) -> str:
        return "".join(json.dumps(t, ensure_ascii=False) + "\n" for t in self.traces)

    def quality_summary(self) -> dict[str, Any]:
        """按路由聚合：成功数/失败数/平均延迟/总成本。"""
        by_route: dict[str, list[dict[str, Any]]] = {}
        for t in self.traces:
            by_route.setdefault(t["route_id"], []).append(t)
        return {
            rid: {
                "count": len(rows),
                "success": sum(1 for r in rows if r["status"] == "ok"),
                "avg_latency_ms": round(sum(r["latency_ms"] for r in rows) / len(rows), 1),
                "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
                "failure_classes": sorted({r["failure_class"] for r in rows if r["failure_class"]}),
            }
            for rid, rows in by_route.items()
        }

    def eval_set(self, prompts: list[str], predict_fn: Callable[[str], dict],
                 label: str) -> dict[str, Any]:
        """固定评估集重跑：predict_fn(prompt) → {route_id, status, latency_ms, cost_usd}。"""
        results = []
        for p in prompts:
            r = predict_fn(p)
            results.append(r)
        ok = sum(1 for r in results if r["status"] == "ok")
        return {
            "schema": EVAL_SCHEMA,
            "label": label,
            "total": len(results),
            "success": ok,
            "success_rate": round(ok / len(results), 4) if results else 0,
            "avg_latency_ms": round(sum(r["latency_ms"] for r in results) / max(len(results), 1), 1),
            "results": results,
        }
