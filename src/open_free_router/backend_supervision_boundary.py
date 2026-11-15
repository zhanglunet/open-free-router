# -*- coding: utf-8 -*-
"""PRD-447/T4（trend issue #2158 的 T4 环节）：后端监督台 划界断言。

按 T1 设计（docs/plans/2026-11-15-prd-447-backend-supervision-bench-design.md）
§2.7 划界 + §5 红线落地：监督回路只管换路建议出炉的全程（人看没看、
批没批、agent 改没改），五条边界写死成断言，越界即显形拒绝：

  - 不管配置评审过没有：配置评审是 PRD-424 的事（REVIEW_CONFIG）；
  - 不验异常真假：异常真假验证是 PRD-378 的事（VERIFY_ANOMALY）；
  - 不管待办收发：收件箱的待办收发是 PRD-364 的事（INBOX_DISPATCH）；
  - 人不下场改建议：改建议是 agent 的事，监督台不提供直接编辑建议
    内容的入口（EDIT_SUGGESTION）；
  - 不执行换路：学习网关只出推荐不自动执行，换路建议无论点头与否
    都不由监督台执行（EXECUTE_REROUTE）。

为什么 T2/T3 还不够：T2 的 bench 与 T3 的 gate 只保证"未点头不外流"，
却没说"点头能证明什么"。一条已点头的建议若被下游拿去当"配置已评审"、
"异常已验真"、"待办已收发"的凭证，或被拿去直接切换路由，监督台就
悄悄越了界——点头只证明"人在监督台上看过、批过、裁决过"，除此之外
什么都不证明。

T4 的做法（只加断言，不改 T2 的设计与状态机、不改 T3 的闸门）：
  - classify_action：纯函数，给一个待办动作定界——监督回路内的四种
    动作（看进度/批注/发回反馈/点头裁决）放行，其余一律越界；未知或
    残缺动作按越界处理（fail-closed），并逐条给出越界原因
    （code + target + detail），指明该动作归哪个 PRD 管。
  - assert_boundary：越界即显形拒绝（BenchError，OUT_OF_SCOPE）。
  - check_suggestion_scope：纯函数，查一条换路建议有没有带邻仓的
    "外来认领"（config_reviewed / anomaly_verified / inbox_dispatched /
    suggestion_edited / route_switched 等旗子）：监督台从不产生这类
    认领，带了就是越界冒充，一律拦下。
  - guard_admission：下游放行的第二道断言——先过界（动作须在监督
    回路内），再过 T3 的 check_gate（建议须已点头）；两项缺一不可，
    放行/拦截都可记进 SupervisionLedger 留痕。

本模块不管配置评审、不验异常真假、不管待办收发、不改建议内容、不触发
路由切换、不授予点头——只有划界断言，没有越界的后门；学习网关只出推
荐不自动执行。
零网络零子进程；示例全虚构占位。
测试：tests/test_backend_supervision_boundary.py
运行：.venv/bin/python -m pytest tests/test_backend_supervision_boundary.py -q
"""
from __future__ import annotations

import copy
from typing import Any

from backend_supervision_bench import BenchError
from backend_supervision_bench import SCHEMA as BENCH_SCHEMA
from backend_supervision_gate import check_gate

BOUNDARY_SCHEMA = "backend-supervision-boundary/v1"
BOUNDARY_LOOP = "supervision-loop"
IN_SCOPE_ACTIONS = ("watch_progress", "add_annotation",
                    "send_back_feedback", "decide_approval")
# 越界动作 →（原因码，归属边界，说明）：动作归谁管，断言就指去谁。
OUT_OF_SCOPE_ACTIONS = {
    "review_config": ("REVIEW_CONFIG", "prd-424",
                      "配置评审归 PRD-424 评审流水线，监督台不管配置评审"),
    "verify_anomaly": ("VERIFY_ANOMALY", "prd-378",
                       "验异常真假归 PRD-378 后端异常验证台，监督台不验真假"),
    "inbox_dispatch": ("INBOX_DISPATCH", "prd-364",
                       "待办收发归 PRD-364 收件箱，监督台不管待办收发"),
    "edit_suggestion": ("EDIT_SUGGESTION", "human-does-not-edit",
                        "人不下场改建议：改建议是 agent 的事，监督台不提供"
                        "直接编辑建议内容的入口"),
    "execute_reroute": ("EXECUTE_REROUTE", "gateway-recommendation-only",
                        "学习网关只出推荐不自动执行，监督台不触发路由切换"),
}
BOUNDARY_CODES = ("REVIEW_CONFIG", "VERIFY_ANOMALY", "INBOX_DISPATCH",
                  "EDIT_SUGGESTION", "EXECUTE_REROUTE", "UNKNOWN_ACTION",
                  "FOREIGN_CLAIM", "BAD_SUGGESTION")
BOUNDARY_AUDIT_KINDS = ("boundary_passed", "boundary_blocked")
# 外来认领 →（原因码，归属边界）：监督台从不产生这类旗子，带了即冒充。
FOREIGN_CLAIM_KEYS = {
    "config_reviewed": ("REVIEW_CONFIG", "prd-424"),
    "anomaly_verified": ("VERIFY_ANOMALY", "prd-378"),
    "inbox_dispatched": ("INBOX_DISPATCH", "prd-364"),
    "suggestion_edited": ("EDIT_SUGGESTION", "human-does-not-edit"),
    "route_switched": ("EXECUTE_REROUTE", "gateway-recommendation-only"),
}


def _now(at: str) -> str:
    return at or "1970-01-01T00:00:00Z"


def _reason(code: str, target: Any, detail: str) -> dict[str, Any]:
    return {"code": code, "target": target, "detail": detail}


def classify_action(action: dict[str, Any] | None) -> dict[str, Any]:
    """给待办动作定界：监督回路内放行，其余越界；永不抛错。

    返回 {"in_scope": bool, "boundary": str, "reasons": [...]}；
    boundary 为 "supervision-loop"（回路内）或越界动作归属的 PRD。
    """
    if not isinstance(action, dict):
        return {"in_scope": False, "boundary": "unknown",
                "reasons": [_reason("UNKNOWN_ACTION", None, "动作不是 dict")]}
    kind = action.get("kind")
    if not isinstance(kind, str) or not kind:
        return {"in_scope": False, "boundary": "unknown",
                "reasons": [_reason("UNKNOWN_ACTION", None,
                                    "动作无 kind（须为非空字符串）")]}
    if kind in IN_SCOPE_ACTIONS:
        return {"in_scope": True, "boundary": BOUNDARY_LOOP, "reasons": []}
    if kind in OUT_OF_SCOPE_ACTIONS:
        code, boundary, detail = OUT_OF_SCOPE_ACTIONS[kind]
        return {"in_scope": False, "boundary": boundary,
                "reasons": [_reason(code, kind, detail)]}
    return {"in_scope": False, "boundary": "unknown",
            "reasons": [_reason("UNKNOWN_ACTION", kind,
                                f"动作 kind 不在监督回路词表内 kind={kind!r}")]}  # noqa: E501


def assert_boundary(action: dict[str, Any] | None) -> dict[str, Any]:
    """定界：越界即显形拒绝；回路内返回 classify_action 的定界。"""
    verdict = classify_action(action)
    if not verdict["in_scope"]:
        codes = "; ".join(
            f"{r['code']}({r['target']})" for r in verdict["reasons"])
        raise BenchError(f"OUT_OF_SCOPE：监督台不管这件事——{codes}")
    return verdict


def check_suggestion_scope(sug: dict[str, Any] | None) -> dict[str, Any]:
    """查换路建议的认领范围：带邻仓的外来认领即越界；永不抛错。

    返回 {"in_scope": bool, "reasons": [...]}；只有真值（truthy）的
    外来认领才算冒充——写了 config_reviewed=False 不是认领，是没认领。
    """
    reasons: list[dict[str, Any]] = []
    if not isinstance(sug, dict):
        return {"in_scope": False,
                "reasons": [_reason("BAD_SUGGESTION", None, "建议不是 dict")]}
    if sug.get("schema") != BENCH_SCHEMA:
        reasons.append(_reason("BAD_SUGGESTION", sug.get("suggestion_id"),
                               f"schema 非 {BENCH_SCHEMA}"))
    claim_sources = [sug]
    claims = sug.get("claims")
    if isinstance(claims, dict):
        claim_sources.append(claims)
    for source in claim_sources:
        for key, (code, boundary) in FOREIGN_CLAIM_KEYS.items():
            if source.get(key):
                reasons.append(_reason(
                    code, key,
                    f"建议带外来认领 {key}（归 {boundary} 管），"
                    "监督台从不产生这类认领"))
    return {"in_scope": not reasons, "reasons": reasons}


def guard_admission(action: dict[str, Any] | None,
                    sug: dict[str, Any] | None, *,
                    ledger: Any = None, by: str,
                    at: str = "") -> dict[str, Any]:
    """下游放行的第二道断言：先过界，再过 T3 的闸。

    动作须在监督回路内（否则 OUT_OF_SCOPE），且建议须过 T3 check_gate
    （否则 GATE_BLOCKED）；两项缺一不可。放行返回 {"admitted": 建议深
    拷贝, "verdict": 闸门裁决, "boundary": "supervision-loop"}；拦截时
    先记一条 boundary_blocked 审计（越界尝试有据可查），再显形拒绝。
    ledger 给了就必须给 by（审计须署名），否则拒绝入账。
    """
    boundary = classify_action(action)
    scope = check_suggestion_scope(sug)
    gate = check_gate(sug)
    reasons = list(boundary["reasons"]) + list(scope["reasons"])
    if boundary["in_scope"] and scope["in_scope"]:
        reasons = list(gate["reasons"])
    allowed = boundary["in_scope"] and scope["in_scope"] and gate["allowed"]
    if ledger is not None:
        if not by:
            raise BenchError("GATE_BLOCKED：审计留痕须 by（裁决人必填）")
        entry = {"kind": "boundary_passed" if allowed
                 else "boundary_blocked", "by": by, "at": _now(at),
                 "boundary": boundary["boundary"],
                 "reasons": [r["code"] for r in reasons],
                 "targets": [r["target"] for r in reasons]}
        ledger.record(sug if isinstance(sug, dict) else
                      {"suggestion_id": None}, entry)
    if not boundary["in_scope"]:
        codes = "; ".join(
            f"{r['code']}({r['target']})" for r in boundary["reasons"])
        raise BenchError(f"OUT_OF_SCOPE：监督台不管这件事——{codes}")
    if not scope["in_scope"]:
        codes = "; ".join(
            f"{r['code']}({r['target']})" for r in scope["reasons"])
        raise BenchError(f"OUT_OF_SCOPE：建议带外来认领——{codes}")
    if not gate["allowed"]:
        codes = "; ".join(
            f"{r['code']}({r['target']})" for r in gate["reasons"])
        raise BenchError(f"GATE_BLOCKED：未点头不外流——{codes}")
    return {"admitted": copy.deepcopy(sug), "verdict": gate,
            "boundary": BOUNDARY_LOOP}
