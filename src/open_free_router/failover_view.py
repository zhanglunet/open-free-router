"""PRD-149/T4: failover 审计查询与汇总视图（CLI/本地，只读本机 usage.db）。

T1 §5 落点：``failover-log``（按时间窗/后端/原因过滤成对记录，含 incomplete
标记）与 ``failover-summary``（按后端/原因聚合 + 配额档位/doctor 结论分布）。
只读：全部走 AnalyticsStore 的既有连接与窗口查询，不写任何表。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from open_free_router.analytics import (
    FAILOVER_DECISION_CONTEXT_FIELDS,
    FAILOVER_DECISION_FIELDS,
    FAILOVER_OUTCOME_FIELDS,
)

VIEW_VERSION = "failover-view-v1"

_DECISION_FIELDS = FAILOVER_DECISION_FIELDS + FAILOVER_DECISION_CONTEXT_FIELDS + ("decision_id",)


def _outcome_rows(store: Any, *, days: int, limit: int) -> list[dict]:
    """窗口内读 outcome 行（T4 新增读口；复用 store 连接与锁，只读）。"""
    if not getattr(store, "enabled", False):
        return []
    from datetime import datetime, timedelta, timezone

    days = max(1, min(int(days), int(getattr(store, "retention_days", 90))))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    limit = max(1, min(int(limit), 10000))
    sql = (f"SELECT {', '.join(FAILOVER_OUTCOME_FIELDS)} FROM failover_outcomes "
           "WHERE settled_at >= ? ORDER BY settled_at DESC LIMIT ?")
    with store._lock:  # noqa: SLF001 — 与 store 既有查询同款取数通道
        cursor = store._connection.execute(sql, (cutoff, limit))  # noqa: SLF001
        return [dict(zip(FAILOVER_OUTCOME_FIELDS, row)) for row in cursor.fetchall()]


def _decision_rows(store: Any, *, days: int, reason: str | None, limit: int) -> list[dict]:
    """窗口内读决策行（含 decision_id 与 T2 上下文列；旧仪表盘查询不含这些列）。"""
    if not getattr(store, "enabled", False):
        return []
    from datetime import datetime, timedelta, timezone

    days = max(1, min(int(days), int(getattr(store, "retention_days", 90))))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    limit = max(1, min(int(limit), 10000))
    conditions = ["at >= ?"]
    params: list = [cutoff]
    if reason:
        conditions.append("reason = ?")
        params.append(str(reason)[:64])
    sql = (f"SELECT {', '.join(_DECISION_FIELDS)} FROM failover_decisions "
           f"WHERE {' AND '.join(conditions)} ORDER BY at DESC LIMIT ?")
    params.append(limit)
    with store._lock:  # noqa: SLF001 — 与 store 既有查询同款取数通道
        cursor = store._connection.execute(sql, tuple(params))  # noqa: SLF001
        return [dict(zip(_DECISION_FIELDS, row)) for row in cursor.fetchall()]


def collect_pairs(store: Any, *, days: int = 14, backend: str | None = None,
                  reason: str | None = None, limit: int = 500) -> list[dict]:
    """成对记录视图：决策行 + 关联结果行；无结果行 → pair_status=incomplete。

    backend 匹配 from_candidate 或 to_candidate（换路两侧任一命中即算）。
    """
    decisions = _decision_rows(store, days=days, reason=reason, limit=limit)
    if backend:
        decisions = [d for d in decisions
                     if backend in (d.get("from_candidate"), d.get("to_candidate"))]
    outcomes: dict[str, dict] = {}
    for row in _outcome_rows(store, days=days, limit=limit * 4):
        outcomes.setdefault(str(row.get("decision_id")), row)
    pairs = []
    for d in decisions:
        decision_id = str(d.get("decision_id") or "")
        out = outcomes.get(decision_id)
        pairs.append({
            "at": d.get("at"),
            "from_candidate": d.get("from_candidate"),
            "to_candidate": d.get("to_candidate"),
            "reason": d.get("reason"),
            "quota_band": d.get("quota_band"),
            "doctor_verdict": d.get("doctor_verdict"),
            "outcome": (out or {}).get("outcome"),
            "settled_at": (out or {}).get("settled_at"),
            "pair_status": "incomplete" if out is None else "paired",
        })
    return pairs


def summarize(pairs: list[dict]) -> dict[str, Any]:
    """聚合视图：后端/原因计数 + 配额档位/doctor 结论分布 + incomplete 计数。"""
    def counter(key: str) -> dict[str, int]:
        return dict(Counter(str(p.get(key)) for p in pairs if p.get(key) is not None))

    return {
        "view_version": VIEW_VERSION,
        "total_pairs": len(pairs),
        "incomplete_pairs": sum(1 for p in pairs if p["pair_status"] == "incomplete"),
        "by_backend": counter("to_candidate"),
        "by_reason": counter("reason"),
        "by_quota_band": counter("quota_band"),
        "by_doctor_verdict": counter("doctor_verdict"),
        "by_outcome": counter("outcome"),
    }
