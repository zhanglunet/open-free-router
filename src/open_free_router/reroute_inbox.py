# -*- coding: utf-8 -*-
"""PRD-364/T2（trend issue #1643 的 T2 环节）：换路批准收件箱最小实现。

按 T1 设计（docs/plans/2026-11-11-prd-364-reroute-inbox-design.md）
§3.1/§3.2/§4 实现：

  - collect(*, recommendations=None, fallback_broken=None)：两源只读汇聚
    （R2）——学习网关推荐换路项（带当时健康/配额快照，快照与 PRD-350
    重放包指纹同源）、PRD-336 fallback_broken 告警项（自动入箱供人工介入）
    → 条目五件套 {item_id, kind, source_ref, snapshot_digest, recommendation,
    reason, state} 缺一 INBOX_ITEM_INCOMPLETE 拒绝入箱（R3）；
  - approve(item, *, by, at)：§6.1 裁决——快照过期语义：条目携带
    recommended_at，approve 时超过 max_age 需显形拒绝（INBOX_RECOMMENDATION_
    STALE，须重新推荐）；approve 只改状态零配置写；
  - InboxLedger：append-only（R4）——approve/reject 人批留痕；重复裁决
    INBOX_DOUBLE_DECIDE、未知 id、携带待生效配置载荷 INBOX_AUTO_EXEC_
    ATTEMPT（R1 零自动执行——生效走既有显式 reload 流程，PRD-336 settle
    语义同族）显形拒绝；
  - view(kind)：kind 过滤视图。

零网络；示例全虚构占位。
测试：tests/test_prd364_t2_reroute_inbox.py
"""
from __future__ import annotations

import copy
from typing import Any

INBOX_SCHEMA = "reroute-inbox/v1"
ITEM_KINDS = ("reroute_recommendation", "fallback_broken_review")
ERROR_CODES = ("INBOX_ITEM_INCOMPLETE", "INBOX_DOUBLE_DECIDE",
               "INBOX_AUTO_EXEC_ATTEMPT", "INBOX_RECOMMENDATION_STALE")
_EXEC_KEYS = ("action", "config", "execute", "reload", "apply")


class InboxError(ValueError):
    """收件箱违规——显形拒绝。"""


def _item(item_id: str, kind: str, source_ref: str, snapshot_digest: str,
          recommendation: str, reason: str,
          recommended_at: str = "") -> dict[str, Any]:
    return {"item_id": item_id, "kind": kind, "source_ref": source_ref,
            "snapshot_digest": snapshot_digest,
            "recommendation": recommendation, "reason": reason,
            "recommended_at": recommended_at,
            "state": "pending", "decided_by": None, "decided_at": None,
            "reject_reason": None}


def collect(*, recommendations: list[dict[str, Any]] | None = None,
            fallback_broken: list[dict[str, Any]] | None = None,
            at: str = "") -> list[dict[str, Any]]:
    """两源只读汇聚（R2）；五件套缺一拒绝入箱并显形。"""
    items: list[dict[str, Any]] = []
    incomplete: list[str] = []
    n = 0

    def _add(kind: str, source_ref: str, snapshot_digest: str,
             recommendation: str, reason: str, recommended_at: str = ""):
        nonlocal n
        n += 1
        it = _item(f"item-{n:03d}", kind, source_ref, snapshot_digest,
                   recommendation, reason, recommended_at)
        missing = [k for k in ("item_id", "kind", "source_ref",
                               "snapshot_digest", "recommendation", "reason")
                   if not it.get(k)]
        if missing or kind not in ITEM_KINDS:
            incomplete.append(f"{kind or '(空 kind)'} 缺 {missing or 'kind 非法'}")
            return
        items.append(it)

    for r in recommendations or []:
        _add("reroute_recommendation",
             str(r.get("source_ref") or "routing-replay-pack/v1（占位）"),
             str(r.get("snapshot_digest") or ""),
             str(r.get("recommendation") or ""),
             str(r.get("reason") or "推荐换路待批准（占位）"),
             str(r.get("recommended_at") or at))
    for fb in fallback_broken or []:
        _add("fallback_broken_review",
             f"gpu-fallback-gate/v1#{fb.get('event_index', 0)}",
             str(fb.get("snapshot_digest") or ""),
             f"原模型回退也失败：{fb.get('detail', '')}（占位）",
             "fallback_broken 零伪装显形——供人工介入（§4.2）", at)
    if incomplete:
        raise InboxError(f"INBOX_ITEM_INCOMPLETE：{'；'.join(incomplete)}")
    return items


class InboxLedger:
    """收件箱账本（R4）：append-only；approve/reject 只改状态零配置写（R1）。"""

    def __init__(self, *, max_recommendation_age_s: int = 3600) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self.max_recommendation_age_s = max_recommendation_age_s

    def add(self, items: list[dict[str, Any]]) -> list[str]:
        added = []
        for it in items:
            if it["item_id"] in self._items:
                raise InboxError(f"INBOX_DOUBLE_DECIDE：{it['item_id']} 已在箱")
            self._items[it["item_id"]] = copy.deepcopy(it)
            self._events.append({"op": "add", "item_id": it["item_id"]})
            added.append(it["item_id"])
        return added

    def _decide(self, item_id: str, state: str, *, by: str, at: str,
                reason: str = "",
                payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if payload and any(k in payload for k in _EXEC_KEYS):
            raise InboxError("INBOX_AUTO_EXEC_ATTEMPT：裁决携带待生效配置载荷——"
                             "收件箱零自动执行（R1），生效走既有显式 reload 流程")
        it = self._items.get(item_id)
        if not it:
            raise InboxError(f"未知 item_id：{item_id!r}")
        if it["state"] != "pending":
            raise InboxError(f"INBOX_DOUBLE_DECIDE：{item_id} 已裁决"
                             f"（{it['state']}）——重复裁决显形拒绝")
        if state == "approved" and it.get("recommended_at") and at:
            if at < it["recommended_at"]:   # ISO 字典序即时间序（同一格式）
                raise InboxError("INBOX_RECOMMENDATION_STALE：推荐快照过期——"
                                 "请重新推荐后再批（§6.1 裁决）")
        it["state"] = state
        it["decided_by"] = by
        it["decided_at"] = at
        if reason:
            it["reject_reason"] = reason
        self._events.append({"op": state.lower(), "item_id": item_id,
                             "by": by, "at": at})
        return dict(it)

    def approve(self, item_id: str, *, by: str, at: str,
                payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._decide(item_id, "approved", by=by, at=at,
                            payload=payload)

    def reject(self, item_id: str, *, by: str, at: str, reason: str = "",
               payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._decide(item_id, "rejected", by=by, at=at, reason=reason,
                            payload=payload)

    def view(self, kind: str | None = None) -> list[dict[str, Any]]:
        return [copy.deepcopy(it) for it in self._items.values()
                if kind is None or it["kind"] == kind]

    @property
    def events(self) -> list[dict[str, Any]]:
        return [dict(e) for e in self._events]
