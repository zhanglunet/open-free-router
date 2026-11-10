# -*- coding: utf-8 -*-
"""PRD-350/T2（trend issue #1629 的 T2 环节）：路由决策重放包最小实现。

按 T1 设计（docs/plans/2026-11-11-prd-350-routing-replay-design.md）
§3.1/§3.2/§4 实现：

  - make_pack(recommendations, snapshots, *, rules_version)：从路由决策记录
    构建重放包（R1 三件套）——input_fingerprint（请求特征 + 健康快照
    digest + 配额快照 digest + rules_version）、decisions（选的后端 + 理由
    逐条）、output（延迟/成本预期汇总）；缺任一 PACK_INCOMPLETE；
  - replay(pack, snapshots, recommend_fn, *, rules_version)：重放三步
    （§3.2）——①指纹复核（健康/配额快照 digest 或版本不符 →
    REPLAY_INPUT_DRIFT 拒绝，防当前状态冒充当时）②注入式 recommend_fn
    深拷贝复算选路判定（判定层重演，零路由切换 R2）③decisions 逐字节
    比对 → REPLAY_OK / REPLAY_MISMATCH（首分歧条显形，R3 零静默换新路由）；
  - ReplayLedger：append-only（R4）；
  - **零路由切换**（R2）：本模块零待生效配置写零路由执行（T4 AST 预埋：
    零配置写导入零网络）。

零网络；示例全虚构占位。
测试：tests/test_prd350_t2_routing_replay.py
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Callable

PACK_SCHEMA = "routing-replay-pack/v1"
ERROR_CODES = ("REPLAY_INPUT_DRIFT", "REPLAY_MISMATCH", "PACK_INCOMPLETE")
_DECISION_KEYS = ("backend", "why")


def _digest(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return "sha16:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def snapshots_digest(health: dict[str, Any], quota: dict[str, Any]) -> str:
    return _digest({"health": health, "quota": quota})


def make_pack(recommendations: list[dict[str, Any]],
              health: dict[str, Any], quota: dict[str, Any], *,
              rules_version: str) -> dict[str, Any]:
    """从路由决策记录构建重放包（R1 三件套）。"""
    clean = []
    for r in recommendations or []:
        if not all(str(r.get(k) or "").strip() for k in _DECISION_KEYS):
            return {"schema": PACK_SCHEMA, "refused": "PACK_INCOMPLETE",
                    "detail": f"决策缺 {_DECISION_KEYS} 任一——拒绝入账"}
        clean.append({k: r.get(k) for k in
                      ("backend", "why", "latency_ms_expected",
                       "cost_expected")})
    if not clean or not health or not quota or not rules_version:
        return {"schema": PACK_SCHEMA, "refused": "PACK_INCOMPLETE",
                "detail": "recommendations/snapshots/rules_version 缺失"}
    return {
        "schema": PACK_SCHEMA,
        "input_fingerprint": {
            "request_traits": _digest({"health": health, "quota": quota}),
            "health_quota_digest": snapshots_digest(health, quota),
            "rules_version": rules_version,
        },
        "decisions": clean,
        "output": {"backends": [c["backend"] for c in clean],
                   "latency_total": sum(c.get("latency_ms_expected") or 0
                                        for c in clean)},
    }


def _refusal(code: str, detail: str) -> dict[str, Any]:
    return {"schema": PACK_SCHEMA, "refused": code, "detail": detail,
            "verdict": None}


def replay(pack: dict[str, Any], health: dict[str, Any],
           quota: dict[str, Any],
           recommend_fn: Callable[[dict, dict], list[dict[str, Any]]], *,
           rules_version: str) -> dict[str, Any]:
    """重放三步（§3.2）：判定层复算（R2 零路由切换），对不上显形（R3）。"""
    fp = pack.get("input_fingerprint") or {}
    if (not fp or pack.get("refused") or not pack.get("decisions")
            or not pack.get("output")):
        return _refusal("PACK_INCOMPLETE", "重放包三件套不齐")
    if (snapshots_digest(health, quota) != fp.get("health_quota_digest")
            or rules_version != fp.get("rules_version")):
        return _refusal("REPLAY_INPUT_DRIFT",
                        "健康/配额快照指纹或规则版本与包不符——拒绝重放"
                        "（防当前状态冒充当时）")
    rerun = recommend_fn(copy.deepcopy(health), copy.deepcopy(quota)) or []
    norm = [{k: r.get(k) for k in ("backend", "why", "latency_ms_expected",
                                   "cost_expected")} for r in rerun]
    for i, want in enumerate(pack["decisions"]):
        got = norm[i] if i < len(norm) else None
        if got != want:
            return {"schema": PACK_SCHEMA, "refused": None,
                    "verdict": "REPLAY_MISMATCH",
                    "evidence": {"first_divergence_index": i,
                                 "want": want, "got": got},
                    "rerun_count": len(norm)}
    if len(norm) != len(pack["decisions"]):
        return {"schema": PACK_SCHEMA, "refused": None,
                "verdict": "REPLAY_MISMATCH",
                "evidence": {"first_divergence_index": len(norm),
                             "why": "决策条数不一致"},
                "rerun_count": len(norm)}
    return {"schema": PACK_SCHEMA, "refused": None, "verdict": "REPLAY_OK",
            "evidence": None, "rerun_count": len(norm)}


class ReplayLedger:
    """重放账本（R4）：append-only 只增不删。"""

    def __init__(self) -> None:
        self._entries: list[dict[str, Any]] = []

    def record(self, pack: dict[str, Any], verdict: dict[str, Any], *,
               at: str) -> dict[str, Any]:
        entry = {"at": at,
                 "pack_fingerprint": copy.deepcopy(pack.get("input_fingerprint")),
                 "verdict": verdict.get("verdict") or verdict.get("refused"),
                 "evidence": copy.deepcopy(verdict.get("evidence"))}
        self._entries.append(entry)
        return dict(entry)

    @property
    def entries(self) -> list[dict[str, Any]]:
        return [dict(e) for e in self._entries]
