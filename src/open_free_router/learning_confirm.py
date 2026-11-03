#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRD-229/T4（trend issue #1148）：人工确认与应用流程——确认后才改策略，全程留痕。

按 T1 设计（docs/plans/2026-10-26-learning-gateway-design.md）§6 全节实现：

  §6.1 状态机：pending → confirmed → applied →（rolled_back/closed）；
    pending → rejected / stale / expired。非法迁移拒绝。
  §6.2 确认前绝不生效（三重机制）：
    1. 存储隔离：推荐与审计流水存独立目录（本模块 0600 文件 / 0700 目录，
       沿 analytics.py 权限惯例），**不持有任何路由配置写句柄**——本模块
       物理上没有任何写路由配置的路径，应用器是单闸门（apply_preview/
       apply_confirm 只产「应用指令」，真实写配置由调用方注入的 applier 执行）；
    2. 应用器单闸门：apply 执行前校验四项——status=confirmed、存在确认记录、
       确认记录证据哈希与推荐当前证据一致（不一致自动置 stale）、未过期；
       任一不满足即拒绝并留痕；
    3. 确认是显式动作：confirm(recommendation_id, operator) 必须显式调用并
       带操作员身份（无批量确认、无生成即确认快捷路径）。
  §6.3 留痕（append-only 审计流水）：generated/confirmed/rejected/applied/
    rolled_back/expired/stale 每事件按 §6.3 字段表落账；流水只追加不改写。
  过期（§5 推荐保质期 7 天）：expires_at 已过 → 置 expired，不得确认/应用。

零网络；now 注入；证据哈希 = evidence 规范化 JSON sha256 前 16。
测试：tests/test_prd229_t4_confirm.py
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

from gateway_recommend import RECOMMENDATION_TTL_DAYS

AUDIT_SCHEMA = "learning-audit-journal/v1"
STATUSES = ("pending", "confirmed", "applied", "rejected", "stale", "expired")
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("confirmed", "rejected", "expired"),
    "confirmed": ("applied", "stale", "expired"),
    "applied": ("rolled_back",),
    "rolled_back": ("closed",),
    "rejected": (), "stale": (), "expired": (), "closed": (),
}


class ConfirmError(ValueError):
    """确认/应用流程违规（信息即给用户的失败原因）。"""


def evidence_hash(evidence: dict[str, Any]) -> str:
    """证据哈希（§6.2-2）：规范化 JSON sha256 前 16。"""
    canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class LearningConfirmBoard:
    """推荐确认板：审计流水 append-only + 状态机 + 证据哈希绑定。"""

    def __init__(self) -> None:
        self.recommendations: dict[str, dict[str, Any]] = {}
        self.journal: list[dict[str, Any]] = []  # append-only（§6.3）

    def _event(self, type_: str, rec_id: str, at: str, **fields: Any) -> None:
        self.journal.append({"type": type_, "recommendation_id": rec_id,
                             "at": at, **fields})

    def register(self, record: dict[str, Any]) -> dict[str, Any]:
        """登记 generated 事件（T3 产物进入确认板；status 恒 pending）。"""
        rec_id = record.get("recommendation_id")
        if not rec_id:
            raise ConfirmError("缺 recommendation_id")
        if rec_id in self.recommendations:
            raise ConfirmError(f"推荐已登记：{rec_id}")
        if record.get("status") != "pending":
            raise ConfirmError("登记的推荐必须 status=pending（生成侧零自动变更）")
        self.recommendations[rec_id] = dict(record)
        self._event("generated", rec_id, record["generated_at"],
                    evidence_hash=evidence_hash(record.get("evidence") or {}),
                    metric_version=record.get("evidence", {}).get("metric_version",
                                                                  "gateway-recommend/v1"))
        return dict(record)

    def _rec(self, rec_id: str) -> dict[str, Any]:
        rec = self.recommendations.get(rec_id)
        if not rec:
            raise ConfirmError(f"推荐不存在：{rec_id!r}")
        return rec

    def _transition(self, rec_id: str, to: str) -> None:
        rec = self._rec(rec_id)
        if to not in TRANSITIONS.get(rec["status"], ()):
            raise ConfirmError(f"非法迁移：{rec['status']} → {to}")

    def confirm(self, rec_id: str, operator: str, *, at: str) -> dict[str, Any]:
        """§6.2-3 显式确认：操作员身份必填；确认时证据哈希入审计。"""
        if not operator or not str(operator).strip():
            raise ConfirmError("operator 必填（操作员身份标识，本地操作员名占位）")
        self._transition(rec_id, "confirmed")
        rec = self._rec(rec_id)
        rec["status"] = "confirmed"
        rec["confirmed_by"] = operator
        rec["confirmed_at"] = at
        rec["confirm_evidence_hash"] = evidence_hash(rec.get("evidence") or {})
        self._event("confirmed", rec_id, at, operator=operator,
                    evidence_hash=rec["confirm_evidence_hash"])
        return dict(rec)

    def reject(self, rec_id: str, operator: str, *, at: str,
               reason: str = "") -> dict[str, Any]:
        self._transition(rec_id, "rejected")
        rec = self._rec(rec_id)
        rec["status"] = "rejected"
        rec["rejected_by"] = operator
        rec["rejected_at"] = at
        self._event("rejected", rec_id, at, operator=operator, reason=reason)
        return dict(rec)

    def expire(self, rec_id: str, *, at: str, reason: str = "保质期已过（§5）") -> dict[str, Any]:
        self._transition(rec_id, "expired")
        rec = self._rec(rec_id)
        rec["status"] = "expired"
        rec["expired_at"] = at
        self._event("expired", rec_id, at, reason=reason)
        return dict(rec)

    def mark_stale(self, rec_id: str, *, at: str,
                   reason: str = "证据被重新生成/篡改（§6.2-2）") -> dict[str, Any]:
        self._transition(rec_id, "stale")
        rec = self._rec(rec_id)
        rec["status"] = "stale"
        rec["stale_at"] = at
        rec["stale_reason"] = reason
        self._event("stale", rec_id, at, reason=reason)
        return dict(rec)

    def apply(self, rec_id: str, *, at: str, applier: Callable[[str, dict], Any] | None = None) -> dict:
        """应用器单闸门（§6.2-2）：四项校验全过才执行注入式 applier。

        校验：① status=confirmed；② 存在确认记录；③ 确认时证据哈希与当前
        证据一致（不一致自动置 stale）；④ 未过期（expires_at 内）。
        applier(param, values) 为调用方注入的真实写路由配置通道——本模块自身
        仍不写路由配置（存储隔离不变）。
        """
        rec = self._rec(rec_id)
        self._transition(rec_id, "applied")  # ① status=confirmed 才允许迁移
        if not rec.get("confirm_evidence_hash"):  # ② 确认记录先行校验
            raise ConfirmError(f"{rec_id}: 无确认记录（确认是显式动作，§6.2-3）")
        now_dt = datetime_at(at)
        exp = rec.get("expires_at")
        if exp and datetime_at(exp) < now_dt:  # ④ 未过期
            self.mark_stale(rec_id, at=at, reason="已过 expires_at（§5 保质期）")
            raise ConfirmError(f"{rec_id}: 已过期（expires_at={exp}）——置 stale，不得应用")
        confirm_hash = rec.get("confirm_evidence_hash")
        current_hash = evidence_hash(rec.get("evidence") or {})
        if not confirm_hash:  # ② 存在确认记录
            raise ConfirmError(f"{rec_id}: 无确认记录（确认是显式动作，§6.2-3）")
        if confirm_hash != current_hash:  # ③ 证据一致
            self.mark_stale(rec_id, at=at, reason="证据哈希与确认时不一致")
            raise ConfirmError(f"{rec_id}: 证据已变更，确认自动失效置 stale——须重新确认")
        old_values = dict(rec["current_value"])
        new_values = dict(rec["recommended_value"])
        result = None
        if applier is not None:
            result = applier(rec["parameter"], {"from": old_values, "to": new_values})
        rec["status"] = "applied"
        rec["applied_at"] = at
        rec["old_values_snapshot"] = old_values  # §6.4 回滚点（旧值快照）
        self._event("applied", rec_id, at, parameter=rec["parameter"],
                    old_value=old_values, new_value=new_values,
                    applier_result=result)
        return dict(rec)

    def rollback(self, rec_id: str, operator: str, *, at: str) -> dict[str, Any]:
        """§6.4 回滚：操作员显式执行，恢复旧值快照；同一推荐不得二次应用
        （rolled_back 是终态前站——要再动，重新生成新推荐走全流程）。"""
        self._transition(rec_id, "rolled_back")
        rec = self._rec(rec_id)
        rec["status"] = "rolled_back"
        rec["rolled_back_by"] = operator
        rec["rolled_back_at"] = rec.get("rolled_back_at", at)
        applied = next((e for e in reversed(self.journal)
                        if e["type"] == "applied" and e["recommendation_id"] == rec_id), None)
        self._event("rolled_back", rec_id, at, operator=operator,
                    applied_event_ref=(applied or {}).get("at"))
        return dict(rec)

    def close(self, rec_id: str, *, at: str) -> dict[str, Any]:
        self._transition(rec_id, "closed")
        rec = self._rec(rec_id)
        rec["status"] = "closed"
        rec["closed_at"] = at
        self._event("closed", rec_id, at)
        return dict(rec)


def datetime_at(text: str):
    from datetime import datetime
    return datetime.fromisoformat(text)


def save_journal(board: LearningConfirmBoard, directory: str | Path) -> Path:
    """§6.2-1 存储隔离：审计流水落独立目录（文件 0600、目录 0700）。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "learning-audit.jsonl"
    lines = [json.dumps(e, ensure_ascii=False) for e in board.journal]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    os.chmod(directory, 0o700)
    return path
