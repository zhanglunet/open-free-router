#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRD-229/T2（trend #1146）：学习网关——流量数据汇总管道。

按 T1 设计（docs/plans/2026-10-26-learning-gateway-design.md）§2/§8 T2 契约实现：
  输入：usage.db（usage_events / failover_outcomes，**只读**打开）+ 窗口参数（7/14/30 天）
  输出：聚合集——按（窗口 × prompt_class × provider × model）成行，每行含
    §3 指标（count/success_rate/mean_latency/mean_cost/mean_tokens）+ n +
    覆盖率 + 溯源字段（窗口起止/classifier_version/metric_version/事件总数）；
    另附分类覆盖率报告。
口径（§2.1）：
  - 全量扫描不抽样；窗口整日 UTC，起止写入聚合集
  - 只计已结算事件；status_code=400/invalid_request 计入总量但不入后端质量分母，
    单列 client_error_count
  - 只读元数据，永不碰正文；热路径零改动（只读连接）
契约：同输入同输出（确定性）；任取一行能对回窗口内原始事件（event_ids 溯源字段）。

零网络；测试：tests/test_prd229_t2_pipeline.py
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CLASSIFIER_VERSION = "prompt-classifier/v1"
METRIC_VERSION = "gateway-aggregate/v1"
VALID_WINDOWS = (7, 14, 30)
RETENTION_DAYS_MAX = 30
CLIENT_ERROR_CODES = {400}
CLIENT_ERROR_KIND = "invalid_request"


class PipelineError(ValueError):
    """管道参数违规（窗口非法/库缺失）。"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def window_bounds(window_days: int, *, now: datetime | None = None) -> tuple[str, str]:
    """窗口以整日 UTC 为界：[start_date, end_date) 的 ISO 日期串。返回 (start, end_exclusive)。"""
    if window_days not in VALID_WINDOWS:
        raise PipelineError(f"window_days 须为 {VALID_WINDOWS}")
    moment = (now or _utc_now()).astimezone(timezone.utc)
    end_day = moment.date()
    start_day = end_day - timedelta(days=window_days)
    return start_day.isoformat(), (end_day + timedelta(days=1)).isoformat()


def classify_prompt_class(provider: str, model: str, status: str) -> str:
    """§2.2 简化分类器：按后端/模型形态归 prompt_class（元数据口径，不碰正文）。

    数据缺口走 (a) 保守路径：无法细分时统一 local/remote 两档（T1 §2.2 备注，回写口径）。
    """
    if provider in ("local", "ollama", "llamacpp") or model.startswith(("local-", "llama")):
        return "local-model"
    return "remote-api"


def _nearest_rank_p95(values: list[float]) -> float | None:
    """nearest-rank p95（§3.3：与 analytics._percentile 同算法）；空集返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    import math
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return round(ordered[min(rank, len(ordered)) - 1], 1)


def aggregate(db_path: str | Path, window_days: int = 14,
              *, now: datetime | None = None) -> dict[str, Any]:
    """流量汇总管道主入口：只读 usage.db → 聚合集 + 覆盖率报告。"""
    window_days = int(window_days)
    if window_days not in VALID_WINDOWS:
        raise PipelineError(f"window_days 须为 {VALID_WINDOWS}")
    path = Path(db_path)
    if not path.exists():
        raise PipelineError(f"usage.db 不存在：{path}")
    moment = (now or _utc_now())
    start_iso, end_iso = window_bounds(window_days, now=moment)
    start_ts = datetime.fromisoformat(start_iso + "T00:00:00+00:00").timestamp()
    end_ts = datetime.fromisoformat(end_iso + "T00:00:00+00:00").timestamp()

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)  # 只读打开（§1 热路径零改动）
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT event_id, timestamp, provider, model, status, status_code,"
            " ttfb_ms, total_latency_ms, input_tokens, output_tokens,"
            " fallback_attempts, error_kind"
            " FROM usage_events WHERE timestamp >= ? AND timestamp < ?"
            " ORDER BY event_id",
            (start_ts, end_ts)).fetchall()
    finally:
        conn.close()

    groups: dict[tuple, dict[str, Any]] = {}
    total_events = len(rows)
    for row in rows:
        prompt_class = classify_prompt_class(row["provider"], row["model"], row["status"])
        key = (prompt_class, row["provider"], row["model"])
        g = groups.setdefault(key, {
            "prompt_class": prompt_class, "provider": row["provider"],
            "model": row["model"], "n": 0, "success": 0, "client_error_count": 0,
            "latencies": [], "cost_tokens_in": 0, "cost_tokens_out": 0,
            "event_ids": [],
            # §3 扩展列（T3 加列，纯加法）：p95/单位成功成本/浪费尝试比/token 覆盖
            "ttfbs": [], "fallback_total": 0, "days": set(),
            "tokens_present": 0, "success_tokens_in": 0, "success_tokens_out": 0,
        })
        g["n"] += 1
        g["event_ids"].append(row["event_id"])
        if row["ttfb_ms"] is not None:
            g["ttfbs"].append(float(row["ttfb_ms"]))
        g["fallback_total"] += int(row["fallback_attempts"] or 0)
        g["days"].add(datetime.fromtimestamp(row["timestamp"], timezone.utc).date().isoformat())
        if row["input_tokens"] is not None and row["output_tokens"] is not None:
            g["tokens_present"] += 1
        is_client_error = (row["status_code"] in CLIENT_ERROR_CODES
                           or row["error_kind"] == CLIENT_ERROR_KIND)
        if is_client_error:
            g["client_error_count"] += 1
        elif row["status"] == "success":
            g["success"] += 1
            g["latencies"].append(float(row["total_latency_ms"] or 0))
            g["success_tokens_in"] += int(row["input_tokens"] or 0)
            g["success_tokens_out"] += int(row["output_tokens"] or 0)
        g["cost_tokens_in"] += int(row["input_tokens"] or 0)
        g["cost_tokens_out"] += int(row["output_tokens"] or 0)

    aggregate_rows = []
    for (prompt_class, provider, model), g in sorted(groups.items()):
        backend_n = g["n"] - g["client_error_count"]
        aggregate_rows.append({
            "prompt_class": prompt_class,
            "provider": provider,
            "model": model,
            "n": g["n"],
            "success_rate": round(g["success"] / backend_n, 4) if backend_n else None,
            "client_error_count": g["client_error_count"],
            "mean_latency_ms": round(sum(g["latencies"]) / len(g["latencies"]), 1)
            if g["latencies"] else None,
            "mean_tokens_in": round(g["cost_tokens_in"] / g["n"], 1),
            "mean_tokens_out": round(g["cost_tokens_out"] / g["n"], 1),
            # §3 扩展列（T3 加列，纯加法；缺数据 null + 不填默认值 §3.4）
            "ttfb_p95_ms": _nearest_rank_p95(g["ttfbs"]),
            "tokens_per_success": (
                round((g["success_tokens_in"] + g["success_tokens_out"]) / g["success"], 1)
                if g["success"] else None),
            "wasted_attempt_ratio": round(g["fallback_total"] / g["n"], 4),
            "token_coverage_rate": round(g["tokens_present"] / g["n"], 4),
            "days_covered": len(g["days"]),
            # 溯源字段（§8 T2：任取一行能对回窗口内原始事件）
            "window": {"start": start_iso, "end_exclusive": end_iso, "days": window_days},
            "classifier_version": CLASSIFIER_VERSION,
            "metric_version": METRIC_VERSION,
            "event_total_in_window": total_events,
            "event_ids": list(g["event_ids"]),
        })

    classified = sum(1 for r in aggregate_rows)
    return {
        "schema": "gateway-aggregate/v1",
        "window": {"start": start_iso, "end_exclusive": end_iso, "days": window_days},
        "classifier_version": CLASSIFIER_VERSION,
        "metric_version": METRIC_VERSION,
        "total_events": total_events,
        "aggregate": aggregate_rows,
        "coverage": {
            "rows": classified,
            "note": "全量扫描不抽样（§2.1）；每行 event_ids 可逐条对回原始事件",
        },
    }
