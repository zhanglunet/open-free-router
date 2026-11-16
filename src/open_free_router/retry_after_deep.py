# -*- coding: utf-8 -*-
"""PRD-469/T2~T3（trend issue #2266/#2267 的 T2+T3 环节）：Retry-After 深度解析。

按 T1 设计（docs/plans/2026-11-17-retry-after-deep-parse-design.md）§2/§3 落地：
  - 三路解析（优先级合并）：响应头 → 正文结构化字段 → 正文锚定短语；
  - 响应头：delta-seconds 与 HTTP-date 共用 parse_retry_after_header()；
  - 正文结构化：深度封顶 6 层递归搜字段名（大小写不敏感）；
  - 锚定短语：白名单正则只认固定形态，只取前 3 个数字；
  - 钳制（§3）：0 ≤ seconds ≤ 86400（24 小时）——畸形/恶意提示无法永久停用；
  - 脱敏（§3）：结果只保留数字（秒数），正文原文/短语上下文从不保留；
  - 三路全无 → 返回 None。
零 LLM 零网络零子进程；示例全虚构占位。
测试：tests/test_prd469_t2_retry_parse.py
"""
from __future__ import annotations

import email.utils
import json
import re
from typing import Any

DEEP_SCHEMA = "retry-after-deep/v1"
MAX_DEPTH = 6
MAX_TEXT_MATCHES = 3
CLAMP_MAX = 86400          # 24 小时
_STRUCTURED_FIELDS = {"retry_after", "retry-after", "retry_after_seconds",
                       "wait_seconds"}
_TEXT_RE = re.compile(
    r"(?:try again in|retry after|wait)\s+(\d+)\s+(seconds?|minutes?)",
    re.IGNORECASE)


def parse_retry_after_header(value: str, now: float) -> float | None:
    """060/T4 共用解析器：delta-seconds 与 HTTP-date → 秒。只解析一次。"""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0, int(value))             # delta-seconds（含负值→钳 0）
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
        if dt is not None:
            import calendar
            return max(0, int(calendar.timegm(dt.timetuple()) - now))
    except (ValueError, TypeError):
        pass
    return None


def _search_structured(obj: Any, depth: int = 0) -> float | None:
    """§2.2 正文结构化字段：深度封顶遍历，数组只取第一个匹配。"""
    if depth > MAX_DEPTH:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower().strip() in _STRUCTURED_FIELDS:
                try:
                    return float(v)
                except (ValueError, TypeError):
                    pass
        for v in obj.values():
            r = _search_structured(v, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for item in obj[:1]:      # 数组只取第一个
            r = _search_structured(item, depth + 1)
            if r is not None:
                return r
    return None


def _search_text(body_text: str) -> float | None:
    """§2.3 锚定自然语言短语：白名单正则，只取前 3 个匹配。"""
    matches = _TEXT_RE.findall(body_text[:2048])
    for num_str, unit in matches[:MAX_TEXT_MATCHES]:
        n = int(num_str)
        if unit.lower().startswith("minute"):
            return n * 60.0
        return float(n)
    return None


def _clamp(seconds: float) -> float:
    return max(0.0, min(seconds, CLAMP_MAX))


def parse_retry_after_deep(
    *, status: int, headers: dict[str, str] | None = None,
    body_text: str | None = None, now: float = 0.0,
) -> dict[str, Any] | None:
    """三路合并（§2.4）：响应头 > 正文结构化 > 正文锚定短语。

    三路全无 → None。正文只保留数字，从不保留原文进日志/trace。
    """
    headers = headers or {}
    # 路 1：响应头
    header_val = headers.get("Retry-After") or headers.get("retry-after") \
        or headers.get("RETRY-AFTER")
    if header_val:
        secs = parse_retry_after_header(header_val, now)
        if secs is not None:
            clamped = secs != _clamp(secs)
            return {"schema": DEEP_SCHEMA, "retry_after_seconds": _clamp(secs),
                    "source": "header", "clamped": clamped}
    # 路 2：正文结构化
    if body_text:
        try:
            body_json = json.loads(body_text)
            secs = _search_structured(body_json)
            if secs is not None:
                clamped = secs != _clamp(secs)
                return {"schema": DEEP_SCHEMA, "retry_after_seconds": _clamp(secs),
                        "source": "structured", "clamped": clamped}
        except (json.JSONDecodeError, ValueError):
            pass
    # 路 3：锚定自然语言短语
    if body_text:
        secs = _search_text(body_text)
        if secs is not None:
            clamped = secs != _clamp(secs)
            return {"schema": DEEP_SCHEMA, "retry_after_seconds": _clamp(secs),
                    "source": "text", "clamped": clamped}
    return None
