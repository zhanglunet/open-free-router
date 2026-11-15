# -*- coding: utf-8 -*-
"""PRD-447/T2（trend issue #2156 的 T2 环节）：后端监督台 最小实现。

按 T1 设计（docs/plans/2026-11-15-prd-447-backend-supervision-bench-design.md）§2 落地：
  - 监督对象：一条换路建议（建议标识/内容/调度过程/状态），调度过程只读呈现，
    监督台展示过程，不重算、不改写 agent 的结论；
  - 三件套落账：进度记录/批注记录/发回反馈记录，统一用建议 ID 串联；
  - 状态机：出炉 draft → 待审 in_review → 已点头 approved → 进换路批准收件箱；
    发回 sent_back → agent 改完 → 回到待审；否决 rejected → 归档不进下游；
  - 关口（硬性）：只有 approved 的建议可进换路批准收件箱；改回来的新版本重新
    待审，不继承上一轮点头；否决的建议不进收件箱；
  - 裁决三选一：点头/发回/否决；发回与否决必须写理由；一键发回把批注与反馈单
    打包成结构化反馈（类型+正文+关联批注）发回 agent；
  - SupervisionLedger：进度/批注/反馈/裁决全程留痕，JSONL append-only 路径注入。
红线：学习网关只出推荐不自动执行——监督台本身不触发任何路由切换；换路建议
无论点头与否，都不由监督台执行。/ 人只批注和裁决，不下场改建议——本模块不
提供直接编辑建议内容的入口。
零网络零子进程；示例全虚构占位。
测试：tests/test_backend_supervision_bench.py
运行：.venv/bin/python -m pytest tests/test_backend_supervision_bench.py -q
"""
import copy
import json
from pathlib import Path
from typing import Any

SCHEMA = "backend-supervision-bench/v1"
SUGGESTION_STATUSES = ("draft", "in_review", "approved", "sent_back",
                       "rejected")
ANNOTATION_STATUSES = ("open", "accepted", "rejected")
FEEDBACK_TYPES = ("reroute", "keep_route", "add_note")
FEEDBACK_STATUSES = ("open", "in_progress", "returned", "closed")
DECISIONS = ("approve", "send_back", "reject")
ERROR_CODES = ("UNKNOWN_SUGGESTION", "BAD_STATUS", "BAD_ANCHOR",
               "BAD_FEEDBACK", "UNKNOWN_ANNOTATION", "UNKNOWN_FEEDBACK",
               "REASON_REQUIRED", "NOT_APPROVED", "GATE_BLOCKED")


class BenchError(ValueError):
    """监督台违规——显形拒绝。"""


def _now(at: str) -> str:
    return at or "1970-01-01T00:00:00Z"


def _progress(sug: dict[str, Any], kind: str, by: str, at: str,
              **extra: Any) -> dict[str, Any]:
    entry = {"kind": kind, "by": by, "at": _now(at), **extra}
    sug["progress"].append(entry)
    return entry


def report_suggestion(suggestion_id: str, *, agent: str, task_ref: str,
                      route_from: str, route_to: str,
                      quota_change: str = "", window: str = "",
                      reasoning_steps: list[dict[str, Any]] | None = None,
                      by: str = "", at: str = "") -> dict[str, Any]:
    """建议出炉时 agent 上报：建议内容 + 完整调度过程，状态为待审。"""
    if not suggestion_id or not agent or not route_from or not route_to:
        raise BenchError("BAD_ANCHOR：建议标识/agent/起止路由必填")
    steps = []
    for i, st in enumerate(reasoning_steps or []):
        steps.append({"index": i, "name": str(st.get("name", "")),
                      "conclusion": str(st.get("conclusion", "")),
                      "input_summary": str(st.get("input_summary", "")),
                      "at": str(st.get("at", _now(at)))})
    sug = {"schema": SCHEMA, "suggestion_id": suggestion_id,
           "agent": agent, "task_ref": task_ref,
           "route_from": route_from, "route_to": route_to,
           "quota_change": quota_change, "window": window,
           "status": "in_review", "by": by, "created_at": _now(at),
           "versions": [{"version": 1, "route_from": route_from,
                         "route_to": route_to, "quota_change": quota_change,
                         "window": window, "reasoning_steps": steps,
                         "status": "in_review"}],
           "reasoning_steps": steps, "annotations": [],
           "feedback": [], "decisions": [], "progress": []}
    _progress(sug, "reported", by or agent, at, version=1)
    return sug


def add_annotation(sug: dict[str, Any], *, author: str, text: str,
                   step_index: int | None = None,
                   at: str = "") -> dict[str, Any]:
    """人在建议详情页对任意推理步骤或整条建议写批注；批注只附加不覆盖原文。"""
    if not author or not text.strip():
        raise BenchError("BAD_ANCHOR：批注作者与文本必填")
    if step_index is not None and not any(
            s["index"] == step_index for s in sug["reasoning_steps"]):
        raise BenchError(f"BAD_ANCHOR：无此推理步骤 index={step_index!r}")
    ann = {"annotation_id": f"ann-{len(sug['annotations']) + 1}",
           "step_index": step_index, "author": author,
           "text": text, "status": "open", "created_at": _now(at)}
    sug["annotations"].append(ann)
    _progress(sug, "annotation", author, at,
              annotation_id=ann["annotation_id"], step_index=step_index)
    return dict(ann)


def send_back(sug: dict[str, Any], *, feedback_type: str, demand: str,
              annotation_ids: list[str] | tuple[str, ...] = (),
              reason: str = "", by: str = "",
              at: str = "") -> dict[str, Any]:
    """一键发回：批注与反馈单打包成结构化反馈发回 agent，建议置已发回。"""
    if feedback_type not in FEEDBACK_TYPES:
        raise BenchError(f"BAD_FEEDBACK：反馈类型词表外 {feedback_type!r}")
    if not demand.strip():
        raise BenchError("BAD_FEEDBACK：反馈正文必填")
    if not reason.strip():
        raise BenchError("REASON_REQUIRED：发回必须写理由")
    linked = []
    for aid in annotation_ids:
        ann = next((a for a in sug["annotations"]
                    if a["annotation_id"] == aid), None)
        if ann is None:
            raise BenchError(f"UNKNOWN_ANNOTATION：{aid!r}")
        linked.append(aid)
    if sug["status"] not in ("in_review", "approved"):
        raise BenchError(
            f"BAD_STATUS：当前状态不可发回 status={sug['status']!r}")
    ticket = {"ticket_id": f"fb-{len(sug['feedback']) + 1}",
              "feedback_type": feedback_type, "demand": demand,
              "annotation_ids": linked, "reason": reason, "by": by,
              "status": "open", "created_at": _now(at),
              "returned_version": None}
    sug["feedback"].append(ticket)
    sug["status"] = "sent_back"
    sug["versions"][-1]["status"] = "sent_back"
    _progress(sug, "send_back", by, at, ticket_id=ticket["ticket_id"],
              feedback_type=feedback_type)
    return dict(ticket)


def rework_return(sug: dict[str, Any], ticket_id: str, *,
                  route_from: str, route_to: str, quota_change: str = "",
                  window: str = "",
                  reasoning_steps: list[dict[str, Any]] | None = None,
                  changes: str = "", by: str = "",
                  at: str = "") -> dict[str, Any]:
    """agent 改完回来：新版本挂在同一条建议下，与旧版本并列可查；重新待审，
    不继承上一轮点头。"""
    ticket = next((t for t in sug["feedback"]
                   if t["ticket_id"] == ticket_id), None)
    if ticket is None:
        raise BenchError(f"UNKNOWN_FEEDBACK：{ticket_id!r}")
    if ticket["status"] != "open":
        raise BenchError(
            f"BAD_FEEDBACK：ticket 已处理 status={ticket['status']!r}")
    if not route_from or not route_to:
        raise BenchError("BAD_ANCHOR：改回版本起止路由必填")
    steps = []
    for i, st in enumerate(reasoning_steps or []):
        steps.append({"index": i, "name": str(st.get("name", "")),
                      "conclusion": str(st.get("conclusion", "")),
                      "input_summary": str(st.get("input_summary", "")),
                      "at": str(st.get("at", _now(at)))})
    version_no = len(sug["versions"]) + 1
    ticket["status"] = "returned"
    ticket["returned_version"] = version_no
    sug["versions"].append({"version": version_no,
                            "route_from": route_from,
                            "route_to": route_to,
                            "quota_change": quota_change,
                            "window": window, "reasoning_steps": steps,
                            "status": "in_review"})
    sug["reasoning_steps"] = steps
    sug["status"] = "in_review"
    _progress(sug, "rework_return", by, at, ticket_id=ticket_id,
              version=version_no, changes=changes)
    return dict(ticket)


def close_feedback(sug: dict[str, Any], ticket_id: str, *,
                   by: str = "", at: str = "") -> dict[str, Any]:
    """人复核满意则逐条关闭反馈；所挂批注同步记已采纳。"""
    ticket = next((t for t in sug["feedback"]
                   if t["ticket_id"] == ticket_id), None)
    if ticket is None:
        raise BenchError(f"UNKNOWN_FEEDBACK：{ticket_id!r}")
    if ticket["status"] != "returned":
        raise BenchError(
            f"BAD_FEEDBACK：须先回炉再关闭 status={ticket['status']!r}")
    ticket["status"] = "closed"
    for aid in ticket["annotation_ids"]:
        ann = next(a for a in sug["annotations"]
                   if a["annotation_id"] == aid)
        ann["status"] = "accepted"
    _progress(sug, "feedback_closed", by, at, ticket_id=ticket_id)
    return dict(ticket)


def decide(sug: dict[str, Any], *, decision: str, by: str,
           reason: str = "", at: str = "") -> dict[str, Any]:
    """裁决三选一：点头/发回/否决；发回与否决必须写理由，点头可留空。"""
    if decision not in DECISIONS:
        raise BenchError(f"BAD_STATUS：裁决词表外 decision={decision!r}")
    if decision in ("send_back", "reject") and not reason.strip():
        raise BenchError("REASON_REQUIRED：发回与否决必须写理由")
    if decision == "send_back":
        raise BenchError("BAD_STATUS：发回请用 send_back（须带反馈类型与正文）")
    if sug["status"] != "in_review":
        raise BenchError(
            f"GATE_BLOCKED：建议未到待审 status={sug['status']!r}")
    open_tickets = [t for t in sug["feedback"]
                    if t["status"] in ("open", "in_progress", "returned")]
    if open_tickets:
        raise BenchError("GATE_BLOCKED：尚有未结反馈 "
                         f"{[t['ticket_id'] for t in open_tickets]}")
    if decision == "approve":
        sug["status"] = "approved"
        sug["versions"][-1]["status"] = "approved"
        gate = {"decision": "approve", "by": by, "at": _now(at),
                "reason": reason or "同意建议（占位）"}
    else:
        sug["status"] = "rejected"
        sug["versions"][-1]["status"] = "rejected"
        gate = {"decision": "reject", "by": by, "at": _now(at),
                "reason": reason}
    sug["decisions"].append(gate)
    _progress(sug, decision, by, at)
    return dict(gate)


def outlet(suggestions: list[dict[str, Any]]) -> dict[str, Any]:
    """换路批准收件箱 出口包装：只放行已点头的建议；待审/发回中/已否决拦下。
    监督台只出推荐，不执行任何路由切换。"""
    passed, blocked = [], []
    for s in suggestions or []:
        if s.get("status") == "approved":
            passed.append(copy.deepcopy(s))
        else:
            blocked.append({"suggestion_id": s.get("suggestion_id"),
                            "status": s.get("status")})
    return {"schema": SCHEMA, "passed": passed, "blocked": blocked,
            "blocked_count": len(blocked)}


class SupervisionLedger:
    """监督留痕账本：JSONL append-only（路径注入），缺件拒绝入账。"""

    _REQUIRED = ("suggestion_id", "kind", "by", "at")

    def __init__(self, path: str | Path | None = None) -> None:
        self._records: list[dict[str, Any]] = []
        self._path = Path(path) if path else None

    def record(self, sug: dict[str, Any],
               entry: dict[str, Any]) -> dict[str, Any]:
        rec = {"schema": SCHEMA, "suggestion_id": sug.get("suggestion_id"),
               **copy.deepcopy(entry)}
        missing = [k for k in self._REQUIRED
                   if k not in rec or rec.get(k) in (None, "")]
        if missing:
            raise BenchError(f"GATE_BLOCKED：账目缺 {missing}")
        self._records.append(copy.deepcopy(rec))
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False,
                                    sort_keys=True) + "\n")
        return dict(rec)

    def entries(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._records]

    def latest_for(self, suggestion_id: str) -> dict[str, Any] | None:
        found = [r for r in self._records
                 if r["suggestion_id"] == suggestion_id]
        return dict(found[-1]) if found else None
