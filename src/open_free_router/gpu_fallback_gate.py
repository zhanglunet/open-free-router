# -*- coding: utf-8 -*-
"""PRD-336/T3（trend issue #1615 的 T3 环节）：GPU 回退结果回填闸门。

按 T1 设计（docs/plans/2026-11-09-prd-336-gpu-orchestra-design.md）§4.2/§4.3
落地，消费 T2 gpu_orchestrator 的 plan/账本原语（复用 reserve/release 不绕行）：

  - OUTCOME_CODES 词表封闭：healthy / rolled_back / failed / fallback_broken；
  - settle(plan, outcome, ledger, *, at, detail="")：执行方回填闸门——
    healthy（换入成功）：victim 释放生效，target 建议由执行方按 hot_swap
    实际结果自行 reserve（本闸门只核不代写——R1 编排零执行）；
    rolled_back（换入失败自动回滚成功）：**victim 回到 reserved**（复用
    T2 reserve——回退底座恢复，R2）；failed（回退自身失败）：零伪装显形 +
    账本打 fallback_broken 标记供人工介入；未知 outcome 词表外显形拒绝；
  - 每次 settle 追加 events[]（append-only 全留痕，§4.3「执行结果回填账本
    全留痕」）；
  - gate_check(plan)：建议可执行性前置核——infeasible 建议不可 settle
    （显形拒绝不硬换）；回退判据表随建议在位（§4.2 判据表不缺席）。

零设备调用零网络（R1，AST 同表预埋）；示例全虚构占位。
测试：tests/test_prd336_t3_fallback_gate.py
"""
from __future__ import annotations

from typing import Any

from gpu_orchestrator import OrchestratorError, release, reserve

GATE_SCHEMA = "gpu-fallback-gate/v1"
OUTCOME_CODES = ("healthy", "rolled_back", "failed", "fallback_broken")


def gate_check(plan: dict[str, Any]) -> list[str]:
    """建议可执行性前置核（§4.2/§4.3）：判据表在位、infeasible 拒绝执行。"""
    reasons: list[str] = []
    if plan.get("action") not in ("hot_swap", "unload_then_swap"):
        reasons.append(f"action={plan.get('action')!r} 不可执行（GPU_INFEASIBLE "
                       "显形不硬换）")
    if not plan.get("fallback_criteria"):
        reasons.append("回退判据表缺席——§4.2 判据表必须随建议输出")
    if plan.get("action") == "unload_then_swap" and not plan.get("victims"):
        reasons.append("unload_then_swap 缺 victims——卸载对象显形")
    return reasons


def settle(plan: dict[str, Any], outcome: str, ledger: dict[str, Any], *,
           at: str, detail: str = "") -> dict[str, Any]:
    """执行结果回填闸门（§4.3）：核词表、核建议，按回退判据恢复底座。"""
    if outcome not in OUTCOME_CODES:
        raise OrchestratorError(
            f"outcome 词表外：{outcome!r}（须为 {OUTCOME_CODES}）")
    pre = gate_check(plan)
    if pre:
        raise OrchestratorError("；".join(pre))
    gpu_id = plan["gpu_id"]
    victims = plan.get("victims") or []
    restored: list[str] = []
    if outcome in ("rolled_back", "fallback_broken"):
        for v in victims:   # victim 回到 reserved（回退底座恢复，复用 T2 原语）
            reserve(ledger, v["model"], gpu_id, v["vram_mb"])
            restored.append(v["model"])
    if outcome == "healthy" and victims:
        for v in victims:
            release(ledger, v["model"], gpu_id)   # 换入成功：卸载生效
    entry = {"schema": GATE_SCHEMA, "at": at, "action": plan["action"],
             "gpu_id": gpu_id, "target_model": plan.get("target_model"),
             "outcome": outcome, "restored_victims": restored,
             "detail": detail}
    if outcome == "failed":
        entry["fallback_broken"] = True
        entry["note"] = "换入与回退双失败——零伪装显形，供人工介入（§4.2）"
    ledger.setdefault("events", []).append(entry)   # append-only 全留痕
    return dict(entry)


def events_of(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """回填事件只读取回（append-only 可回放）。"""
    return [dict(e) for e in ledger.get("events", [])]
