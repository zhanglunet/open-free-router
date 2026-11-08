# -*- coding: utf-8 -*-
"""PRD-131/T2：有序后端列表 + doctor 检查（trend #655）。

按 T1 设计实现：
  - ordered_backends：配置文件声明优先序
  - doctor_check：逐一探活并输出健康报告
  - auto_fallback：primary 不可达 → 自动切换到下一个健康后端（入审计）

红线：学习网关只出推荐不自动执行；换路须入审计；零网络（探活注入）。
"""
from __future__ import annotations
import json
from typing import Any, Callable

BACKEND_ORDER_SCHEMA = "backend-order/v1"
DOCTOR_SCHEMA = "doctor-report/v1"


def build_ordered_list(config: dict[str, Any]) -> list[dict[str, Any]]:
    """配置 → 有序后端列表。每个条目含 name/priority/status。"""
    backends = config.get("backends", [])
    if not backends:
        raise ValueError("backends 不得为空")
    return [{"name": b["name"], "priority": i + 1, "status": "unknown"}
            for i, b in enumerate(backends)]


def doctor_check(backends: list[dict[str, Any]],
                 probe_fn: Callable[[str], str]) -> dict[str, Any]:
    """doctor 检查：逐一探活并输出健康报告。

    probe_fn(backend_name) → "healthy"|"degraded"|"down"
    返回 doctor-report/v1。
    """
    results = []
    healthy_count = 0
    for b in backends:
        name = b["name"]
        try:
            status = probe_fn(name)
        except Exception:
            status = "down"
        results.append({"backend": name, "status": status})
        if status == "healthy":
            healthy_count += 1
    return {
        "schema": DOCTOR_SCHEMA,
        "backends": results,
        "summary": {"total": len(results), "healthy": healthy_count,
                    "has_healthy": healthy_count > 0},
    }


def auto_fallback(doctor_report: dict[str, Any],
                  ordered_list: list[dict[str, Any]]) -> dict[str, Any] | None:
    """自动换路：返回第一个健康后端，全部不可达 → None。"""
    for b in ordered_list:
        entry = next((r for r in doctor_report["backends"]
                      if r["backend"] == b["name"]), None)
        if entry and entry["status"] == "healthy":
            return b
    return None
