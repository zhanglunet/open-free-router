# -*- coding: utf-8 -*-
"""PRD-322/T2：后端注册表搜索接口（trend #655）。"""
from __future__ import annotations
from typing import Any


def search_catalog(registry: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """按名称/描述子串匹配搜索。"""
    q = (query or "").lower()
    if not q:
        return registry
    return [r for r in registry
            if q in str(r.get("name", "")).lower()
            or q in str(r.get("description", "")).lower()
            or any(q in str(t).lower() for t in r.get("tags", []))]
