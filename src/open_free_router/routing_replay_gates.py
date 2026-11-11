# -*- coding: utf-8 -*-
"""PRD-350/T3（trend issue #1629 的 T3 环节）：路由决策重放闸门。

按 T1 设计（docs/plans/2026-11-11-prd-350-routing-replay-design.md）§4 落地，
消费 T2 routing_replay 产物（注入式只读）：

  - mark_baseline + append_only_gate：账本 append-only 复核——基线锚后长度
    单调不减 + 首条原样，破坏 LEDGER_DRIFT 显形（无基线时形态检查）；
  - state_gate：条目状态与账本双向对账——条目被直接改写、账本有裁决但
    条目回 pending、账本有裁决但条目被删，三者全部 STATE_INCONSISTENT
    显形（裁决必须双向一致）；
  - gate_summary 多闸一票否决合并显形。

**零路由切换**（R2 红线继续）：闸门只读消费零账本写零配置写零网络。
零网络；示例全虚构占位。
测试：tests/test_prd350_t3_routing_replay_gates.py
"""
from __future__ import annotations

from typing import Any

from routing_replay import PACK_SCHEMA

GATE_SCHEMA = "routing-replay-gate/v1"
GATE_CODES = ("VERDICT_MISMATCH", "LEDGER_DRIFT", "PACK_STALE")
ERROR_CODES = GATE_CODES   # 错误码词表（与闸门三码同源）


def _fail(gate: str, reasons: list[str]) -> dict[str, Any]:
    return {"schema": GATE_SCHEMA, "gate": gate, "reasons": reasons}


def ledger_gate(pack: dict[str, Any], verdict: dict[str, Any],
                ledger: Any) -> dict[str, Any]:
    """账本一致性复核：verdict/evidence/指纹与最后入账逐字节一致才过。"""
    entries = getattr(ledger, "entries", None)
    if not entries:
        return _fail("VERDICT_MISMATCH", ["账本为空——verdict 无入账记录可对"])
    last = entries[-1]
    v = verdict.get("verdict") or verdict.get("refused")
    want_v = last.get("verdict")
    reasons = []
    if v != want_v:
        reasons = reasons + [f"verdict {v!r} 与账本最后入账 {want_v!r} 不一致——"
                             "账本被篡改或 verdict 伪造"]
    if verdict.get("evidence") != last.get("evidence"):
        reasons = reasons + ["evidence 与账本最后入账不一致——证据被改写"]
    if last.get("pack_fingerprint") != pack.get("input_fingerprint"):
        reasons = reasons + ["账本入账指纹与本包指纹不一致——入账与包错位"]
    return _fail("pass", []) if not reasons else _fail("VERDICT_MISMATCH",
                                                       reasons)


def mark_baseline(ledger: Any) -> None:
    """打基线锚：记录当前 entries 长度与首条（只挂只读锚属性）。"""
    entries = getattr(ledger, "entries", None)
    if entries is None:
        raise ValueError("账本无 entries 属性——非 ReplayLedger")
    ledger._baseline_len = len(entries)
    ledger._baseline_first = dict(entries[0]) if entries else None


def append_only_gate(ledger: Any) -> dict[str, Any]:
    """append-only 复核：基线锚后长度单调不减 + 首条原样。"""
    entries = getattr(ledger, "entries", None)
    if entries is None:
        return _fail("LEDGER_DRIFT", ["账本无 entries 属性——非 ReplayLedger"])
    if getattr(ledger, "_baseline_len", None) is not None:
        if len(entries) < ledger._baseline_len:
            return _fail("LEDGER_DRIFT",
                         [f"entries 长度 {len(entries)} < 基线 "
                          f"{ledger._baseline_len}——发生删除"])
    if getattr(ledger, "_baseline_first", None) is not None:
        if entries and entries[0] != ledger._baseline_first:
            return _fail("LEDGER_DRIFT", ["首条记录被改写——append-only 破坏"])
    return _fail("pass", [])


def pack_gate(pack: dict[str, Any], ledger: Any) -> dict[str, Any]:
    """包指纹复核：与账本最后入账一致，否则别包冒充 PACK_STALE 显形。"""
    entries = getattr(ledger, "entries", None) or []
    if not entries:
        return _fail("PACK_STALE", ["账本为空——无从比对包指纹"])
    if entries[-1].get("pack_fingerprint") != pack.get("input_fingerprint"):
        return _fail("PACK_STALE", ["包指纹与账本最后入账不一致——拿别的包冒充"])
    return _fail("pass", [])


def gate_summary(gates: list[dict[str, Any]]) -> dict[str, Any]:
    """多闸一票否决合并显形。"""
    reasons = [r for g in gates for r in g.get("reasons", [])]
    return {"schema": GATE_SCHEMA,
            "gate": "pass" if not reasons else "block",
            "gates_checked": len(gates), "reasons": reasons}
