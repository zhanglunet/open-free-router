# -*- coding: utf-8 -*-
"""PRD-322/T3：后端注册表搜索闸门（trend #655 区段）。

T2（catalog_search 搜索接口）的产物在进入决策面（后端选择/排序展示）前
必过本闸门：搜索不变量逐条复验——
  - results ⊆ registry（不新增、不伪造条目）；
  - 每条命中确实满足 T2 的匹配谓词（name/description/tags 子串命中）；
  - 空 query 的「返回全量」语义只在查询为空白时合法。
MATCH 唯一放行；否则 CATALOG_GATE_REFUSED 逐条点名，零静默。
"""
from __future__ import annotations
from typing import Any

GATE_ALLOWED = "MATCH"
REFUSED = "CATALOG_GATE_REFUSED"


def _matches(query: str, entry: dict[str, Any]) -> bool:
    q = query.lower()
    return (q in str(entry.get("name", "")).lower()
            or q in str(entry.get("description", "")).lower()
            or any(q in str(t).lower() for t in entry.get("tags", [])))


def verify_search_result(results: list[dict[str, Any]], query: str,
                         registry: list[dict[str, Any]]) -> dict[str, Any]:
    """复验搜索不变量：MATCH 或 REFUSED+evidence。"""
    evidence: list[str] = []
    if not isinstance(results, list) or not isinstance(registry, list):
        return {"status": "REFUSED", "evidence": ["results/registry 必须是列表"]}
    by_id = {str(r.get("name", "")): r for r in registry}
    q = (query or "").strip()
    for i, r in enumerate(results):
        if not isinstance(r, dict):
            evidence.append(f"results[{i}] 不是对象")
            continue
        name = str(r.get("name", ""))
        if name not in by_id:
            evidence.append(f"results[{i}] 不在注册表（伪造或越界）：{name!r}")
            continue
        if q and not _matches(q, by_id[name]):
            evidence.append(f"results[{i}] 不满足匹配谓词：{name!r}")
    if evidence:
        return {"status": "REFUSED", "evidence": evidence}
    return {"status": GATE_ALLOWED, "count": len(results), "query": q}


def catalog_gate(results: list[dict[str, Any]], query: str,
                 registry: list[dict[str, Any]]) -> dict[str, Any]:
    """闸门：MATCH 放行（附计数）；否则 CATALOG_GATE_REFUSED 零静默。"""
    verdict = verify_search_result(results, query, registry)
    if verdict["status"] == GATE_ALLOWED:
        return {"allow": True, "code": None, "status": GATE_ALLOWED,
                "count": verdict["count"]}
    return {"allow": False, "code": REFUSED, "status": verdict["status"],
            "evidence": verdict["evidence"]}
