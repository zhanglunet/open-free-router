#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRD-257/T4（trend #1288）：切换前后对比记录与审计导出——记录可机读。

事件源（T2/T3 起的内存流，T4 消费）：
  HarnessRouter.switch_events —— 切换事件（route_from/route_to/reason/actor）；
  HarnessRouter.request_events —— 每请求摘要（route/status/failure_class/
    duration_ms/synthetic_stream），send 与 stream 两面同一字段口径。

导出面（本模块）：
  export_jsonl(router) → JSONL 字符串：每行一个 JSON 对象（type=switch/request/
    request_stream），jq/awk 等机读工具可直接逐行消费；空路由 → 空串（零导出）。
  export_compare(results) → 同请求双路由结果对照表：每条输入一行
    （route/status/duration_ms/failure_class），供切换前后逐字段对照。

红线：零对话正文——导出仅含路由/状态/失败类/时长等元数据字段，白名单出料，
不出现 messages/content；doctor 诚实标注纪律不减——synthetic_stream 如实进导出。
示例全虚构；零网络。
"""
from __future__ import annotations

import json
from typing import Any

# 审计导出字段白名单（零对话正文的结构保证：不在此名单的键不出料）
SWITCH_FIELDS = ("route_from", "route_to", "reason", "actor")
REQUEST_FIELDS = ("request_id", "route", "status", "failure_class",
                  "duration_ms", "synthetic_stream")
# 对比记录（同请求双路由对照）每行四字段
COMPARE_FIELDS = ("route", "status", "duration_ms", "failure_class")


def _line(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def export_jsonl(router: Any) -> str:
    """切换事件 + 每请求摘要 → JSONL 字符串（每行一个 JSON 对象，可机读）。

    行序：先切换事件（switch），后请求摘要（request / request_stream）；
    每行仅白名单字段。路由器零事件 → 返回空串（零导出）。
    """
    lines: list[str] = []
    for seq, ev in enumerate(getattr(router, "switch_events", []), start=1):
        row = {"type": "switch", "seq": seq}
        row.update({k: ev.get(k, "") for k in SWITCH_FIELDS})
        lines.append(_line(row))
    for ev in getattr(router, "request_events", []):
        row = {"type": ev.get("type", "request")}
        row.update({k: ev.get(k, "" if k != "synthetic_stream" else False)
                    for k in REQUEST_FIELDS})
        lines.append(_line(row))
    return "\n".join(lines) + ("\n" if lines else "")


def export_compare(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同请求双路由结果对照表（切换前后逐字段对照，机读 dict 列表）。

    results 每条为 {"route": str, "response": Response, "duration_ms": float}
    （duration_ms 由调用方在 send 前后计时）；也容忍直接传 Response dict
    （此时 route/duration_ms 缺省为空/None，如实留空而非编造）。
    每条输出一行且仅 COMPARE_FIELDS 四字段。
    """
    rows: list[dict[str, Any]] = []
    for entry in results or []:
        if isinstance(entry, dict) and "response" in entry:
            route = entry.get("route", "")
            response = entry.get("response") or {}
            duration = entry.get("duration_ms")
        else:
            route = entry.get("route", "") if isinstance(entry, dict) else ""
            response = entry if isinstance(entry, dict) else {}
            duration = response.get("duration_ms")
        failure = response.get("failure") or {}
        rows.append({
            "route": route,
            "status": response.get("status", ""),
            "duration_ms": duration,
            "failure_class": failure.get("class", ""),
        })
    return rows
