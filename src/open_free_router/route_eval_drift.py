#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRD-201/T4（trend #1008）：路由可观测——固定评估集定期重跑 + 漂移标注。

按 PRD-201/T1 设计（docs/plans/2026-10-26-routing-observability-design.md）§7 实现，
消费 T2（route_obs.RouteObservatory.eval_set 的 run 记录，PR #112 已合并）：

  run_and_compare(observatory, prompts, predict_fn, label, previous_run=None)：
    固定评估集重跑一轮（eval_set 同口径），与上一轮 run 对比——
    success_rate / avg_latency_ms / avg_cost 逐项 delta；
    超关注线显式标注 DRIFT（默认：成功率降 >0.05、均值延迟涨 >50%、
    均值成本涨 >20%，T4 定值可覆盖）；无上一轮 → no_drift（首跑留痕）；
  save_run / load_latest_run：run 记录落盘与按 label 取最近一份（定期重跑的留痕）；
  报告随 route-observability 看板 D 区（漂移报告索引）消费。

零网络；predict_fn 注入（离线可跑）；示例全虚构。测试：tests/test_prd201_t4_drift.py
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from open_free_router.route_obs import RouteObservatory

DRIFT_SCHEMA = "route-eval-drift-v1"
DEFAULT_THRESHOLDS = {"success_rate_drop": 0.05, "latency_rise_ratio": 0.5,
                      "cost_rise_ratio": 0.2}


def run_and_compare(observatory: RouteObservatory, prompts: list[str],
                    predict_fn: Callable[[str], dict], label: str,
                    previous_run: dict[str, Any] | None = None,
                    thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    """重跑评估集并与上一轮对比；超关注线标注 DRIFT。"""
    th = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        th.update(thresholds)
    run = observatory.eval_set(prompts, predict_fn, label)
    diff: dict[str, Any] = {
        "schema": DRIFT_SCHEMA, "label": label,
        "verdict": "no_drift", "deltas": {}, "drift_items": [],
        "baseline_label": (previous_run or {}).get("label"),
    }
    if previous_run is None:
        diff["reason"] = "首跑无可比基线——留痕不判漂移"
        run["drift"] = diff
        return run
    base_metrics = _metrics_of(previous_run)
    cur = _metrics_of(run)
    sr_delta = round(cur["success_rate"] - base_metrics["success_rate"], 4)
    lat_ratio = round((cur["avg_latency_ms"] / base_metrics["avg_latency_ms"] - 1), 4) \
        if base_metrics["avg_latency_ms"] else 0.0
    cost_ratio = round((cur["avg_cost"] / base_metrics["avg_cost"] - 1), 4) \
        if base_metrics["avg_cost"] else 0.0
    diff["deltas"] = {"success_rate": sr_delta, "latency_rise_ratio": lat_ratio,
                      "cost_rise_ratio": cost_ratio}
    if sr_delta < -th["success_rate_drop"]:
        diff["drift_items"].append(f"成功率下降 {abs(sr_delta)} 超关注线 {th['success_rate_drop']}")
    if lat_ratio > th["latency_rise_ratio"]:
        diff["drift_items"].append(f"均值延迟上涨 {lat_ratio:.0%} 超关注线 {th['latency_rise_ratio']:.0%}")
    if cost_ratio > th["cost_rise_ratio"]:
        diff["drift_items"].append(f"均值成本上涨 {cost_ratio:.0%} 超关注线 {th['cost_rise_ratio']:.0%}")
    diff["verdict"] = "drift" if diff["drift_items"] else "no_drift"
    run["drift"] = diff
    return run


def _metrics_of(run: dict[str, Any]) -> dict[str, float]:
    return {"success_rate": float(run.get("success_rate", 0.0)),
            "avg_latency_ms": float(run.get("avg_latency_ms", 0.0)),
            "avg_cost": (sum(r.get("cost_usd", 0.0) for r in run.get("results", []))
                         / max(len(run.get("results", [])), 1))}


def save_run(run: dict[str, Any], runs_dir: Path) -> Path:
    path = Path(runs_dir) / f"eval-{run.get('label', 'run')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_latest_run(runs_dir: Path, label: str) -> dict[str, Any] | None:
    path = Path(runs_dir) / f"eval-{label}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
