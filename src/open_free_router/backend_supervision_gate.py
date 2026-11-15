# -*- coding: utf-8 -*-
"""PRD-447/T3（trend issue #2157 的 T3 环节）：后端监督台 监督回路闸门。

按 T1 设计（docs/plans/2026-11-15-prd-447-backend-supervision-bench-design.md）
§2.3/§5 落地：把"未点头不外流"做成换路批准收件箱入口前的强制检查。

为什么 T2 还不够：T2 的 outlet 只认建议的 status == "approved" 这一面
旗子。建议点头之后若又 send_back（"换路/保路"），建议回到 sent_back、还
挂着未结反馈——但只要有人手写回 status，outlet 就会放行；更直接的口子
是：点头后发回再改回的新版本重新待审，可 decisions 里还留着上一轮的
approve 记录，只看"曾经点头过"而不看当前状态、当前版本与未结反馈，未
点头的建议就溜进了换路批准收件箱。

T3 的做法（只加检查，不改 T2 的设计与状态机）：
  - check_gate：纯函数，不改建议；从原始状态重新推导点头结论——建议
    status 已点头 + 有一条 approve 裁决记录 + 当前版本已点头 + 无未结
    反馈，四项缺一不可；只认旗子或只认"曾经点头"的一律拦下，并逐条给出
    拦截原因（code + target + detail）。
  - enforce_gate：不通过即显形拒绝（BenchError，GATE_BLOCKED）。
  - admit_to_downstream：换路批准收件箱唯一允许的入口——先过闸，再把
    放行/拦截的审计条目写进 SupervisionLedger（拦截也留痕，绕关尝试有据
    可查）。
  - gate_outlet：加固版批量出口，逐条建议过闸；只放行 check_gate 通过
    的，未知/残缺建议按拦截处理，永不抛错漏放。

本模块不授予点头、不改建议内容、不触发路由切换——只有检查与审计，没有
放行的后门；点头仍只能由 T2 的 decide 给出；学习网关只出推荐不自动执行，
换路建议无论点头与否都不由监督台执行。
零网络零子进程；示例全虚构占位。
测试：tests/test_backend_supervision_gate.py
运行：.venv/bin/python -m pytest tests/test_backend_supervision_gate.py -q
"""
from __future__ import annotations

import copy
from typing import Any

from backend_supervision_bench import BenchError
from backend_supervision_bench import SCHEMA as BENCH_SCHEMA

GATE_SCHEMA = "backend-supervision-gate/v1"
OPEN_FEEDBACK_STATUSES = ("open", "in_progress", "returned")
BLOCK_REASON_CODES = ("BAD_SUGGESTION", "NOT_APPROVED", "NO_DECISION",
                      "NO_VERSION", "VERSION_NOT_APPROVED", "OPEN_FEEDBACK")
GATE_AUDIT_KINDS = ("gate_passed", "gate_blocked")


def _now(at: str) -> str:
    return at or "1970-01-01T00:00:00Z"


def _reason(code: str, target: Any, detail: str) -> dict[str, Any]:
    return {"code": code, "target": target, "detail": detail}


def check_gate(sug: dict[str, Any] | None) -> dict[str, Any]:
    """强制复核：从原始状态重新推导"该建议是否已点头"，不只认 status 旗子。

    返回 {"allowed": bool, "reasons": [...]}；永不抛错，残缺输入也只拦下。
    """
    reasons: list[dict[str, Any]] = []
    if not isinstance(sug, dict):
        return {"allowed": False,
                "reasons": [_reason("BAD_SUGGESTION", None, "建议不是 dict")]}
    if sug.get("schema") != BENCH_SCHEMA:
        reasons.append(_reason("BAD_SUGGESTION", sug.get("suggestion_id"),
                               f"schema 非 {BENCH_SCHEMA}"))
    if sug.get("status") != "approved":
        reasons.append(_reason("NOT_APPROVED", sug.get("suggestion_id"),
                               f"建议未点头 status={sug.get('status')!r}"))
    decisions = sug.get("decisions") or []
    if not any(isinstance(d, dict) and d.get("decision") == "approve"
               for d in decisions):
        reasons.append(_reason("NO_DECISION", sug.get("suggestion_id"),
                               "无 approve 裁决记录（点头不可手写 status 冒充）"))
    versions = sug.get("versions") or []
    if not versions:
        reasons.append(_reason("NO_VERSION", sug.get("suggestion_id"),
                               "建议无版本，无法推导点头结论"))
    else:
        current = versions[-1]
        if not isinstance(current, dict):
            reasons.append(_reason("BAD_SUGGESTION", sug.get("suggestion_id"),
                                   "当前版本记录不是 dict"))
        elif current.get("status") != "approved":
            reasons.append(_reason(
                "VERSION_NOT_APPROVED", current.get("version"),
                f"当前版本 {current.get('version')!r} 未点头 "
                f"status={current.get('status')!r}"))
    for t in sug.get("feedback") or []:
        if isinstance(t, dict) and t.get("status") in OPEN_FEEDBACK_STATUSES:
            reasons.append(_reason(
                "OPEN_FEEDBACK", t.get("ticket_id"),
                f"反馈 {t.get('ticket_id')!r} 未结 status={t.get('status')!r}"))
    return {"allowed": not reasons, "reasons": reasons}


def enforce_gate(sug: dict[str, Any] | None) -> dict[str, Any]:
    """过闸：不通过即显形拒绝；通过返回 check_gate 的裁决。"""
    verdict = check_gate(sug)
    if not verdict["allowed"]:
        codes = "; ".join(
            f"{r['code']}({r['target']})" for r in verdict["reasons"])
        raise BenchError(f"GATE_BLOCKED：未点头不外流——{codes}")
    return verdict


def admit_to_downstream(sug: dict[str, Any] | None, *,
                        ledger: Any = None, by: str,
                        at: str = "") -> dict[str, Any]:
    """换路批准收件箱唯一允许的入口：先过闸，再把裁决写进账本留痕。

    放行返回 {"admitted": 建议深拷贝, "verdict": 裁决}；拦截时先记一条
    gate_blocked 审计（绕关尝试有据可查），再显形拒绝。ledger 给了就
    必须给 by（审计须署名），否则拒绝入账。
    """
    verdict = check_gate(sug)
    if ledger is not None:
        if not by:
            raise BenchError("GATE_BLOCKED：审计留痕须 by（裁决人必填）")
        entry = {"kind": "gate_passed" if verdict["allowed"]
                 else "gate_blocked", "by": by, "at": _now(at),
                 "reasons": [r["code"] for r in verdict["reasons"]],
                 "targets": [r["target"] for r in verdict["reasons"]]}
        ledger.record(sug if isinstance(sug, dict) else
                      {"suggestion_id": None}, entry)
    if not verdict["allowed"]:
        codes = "; ".join(
            f"{r['code']}({r['target']})" for r in verdict["reasons"])
        raise BenchError(f"GATE_BLOCKED：未点头不外流——{codes}")
    return {"admitted": copy.deepcopy(sug), "verdict": verdict}


def gate_outlet(suggestions: list[dict[str, Any] | None] | None, *,
                ledger: Any = None, by: str,
                at: str = "") -> dict[str, Any]:
    """加固版批量出口：逐条建议过闸，只放行 check_gate 通过的。

    blocked 条目带逐条拦截原因；残缺建议按拦截处理，永不抛错漏放。
    给了 ledger 则放行与拦截各记一条审计（kind=gate_passed/gate_blocked）。
    """
    passed, blocked = [], []
    for s in suggestions or []:
        verdict = check_gate(s)
        suggestion_id = s.get("suggestion_id") if isinstance(s, dict) else None
        if ledger is not None:
            if not by:
                raise BenchError("GATE_BLOCKED：审计留痕须 by（裁决人必填）")
            ledger.record(
                s if isinstance(s, dict) else {"suggestion_id": None},
                {"kind": "gate_passed" if verdict["allowed"]
                 else "gate_blocked", "by": by, "at": _now(at),
                 "reasons": [r["code"] for r in verdict["reasons"]],
                 "targets": [r["target"] for r in verdict["reasons"]]})
        if verdict["allowed"]:
            passed.append(copy.deepcopy(s))
        else:
            blocked.append({"suggestion_id": suggestion_id,
                            "status": s.get("status")
                            if isinstance(s, dict) else None,
                            "reasons": verdict["reasons"]})
    return {"schema": GATE_SCHEMA, "passed": passed, "blocked": blocked,
            "blocked_count": len(blocked)}
