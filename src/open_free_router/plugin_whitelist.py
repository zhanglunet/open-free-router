#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRD-258/T4（trend #1293）：误报白名单与复核——可审计。

按 PRD-258/T1 设计（docs/plans/2026-10-28-prd-258-plugin-gate.md）T4 范围：
  WhitelistManager：误报复核白名单——add 必须附 reason（误报依据）+ reviewer
    （复核人），缺失 ValueError；expires_days 默认 90（复核有保质期，过期自动
    失效）；check(rule_id, scope, now) 过期条目不再命中；audit_log 记每次复核
    登记（谁、何时、为何、豁免哪条规则哪个范围）——可审计。
  PluginGate（T3 闸门）补 whitelist 参数：blocked 报告的 findings 命中白名单
    单条规则 → 该条被豁免，剩余发现重 derive severity/decision；全部 blocked 级
    发现均被豁免才放行，且报告 whitelist_applied 留痕（谁复核的、豁免了什么）；
    有未豁免发现 → 仍拦（白名单豁免的是被复核的具体误报，不是整个报告）。

红线不减：HIGH 及以上默认不接流量——白名单是显式人工复核的例外通道，非默认；
安检仍在接流量前（闸门五道校验原样）；豁免不替代执行时隔离。零网络；示例全虚构。
"""
from __future__ import annotations

import time
from typing import Any

# 复核白名单条目有效期默认（天）：复核有保质期，过期必须重新复核
DEFAULT_EXPIRES_DAYS = 90
_DAY_S = 24 * 60 * 60


class WhitelistManager:
    """误报复核白名单：登记（必须带 reason/reviewer）+ 过期检查 + 审计日志。

    内存态；audit_log 为追加式列表（谁在何时豁免了哪个范围的哪条规则、为何），
    供机读导出/复核回看——可审计是本层验收口径。
    """

    def __init__(self) -> None:
        self._entries: list[dict[str, Any]] = []
        self.audit_log: list[dict[str, Any]] = []

    def add(self, rule_id: str, scope: str, *, reason: str, reviewer: str,
            expires_days: int = DEFAULT_EXPIRES_DAYS,
            now: float | None = None) -> dict[str, Any]:
        """登记一条误报复核豁免：rule_id（如 B-2-01）+ scope（插件名）。

        reason/reviewer 必填（缺失 ValueError）——没有复核依据与人，就没有豁免；
        expires_days 默认 90 天，到期自动失效（check 不再命中）。
        """
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise ValueError("白名单必须指明 rule_id（豁免哪条安检规则）")
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("白名单必须指明 scope（豁免哪个插件范围）")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("白名单必须附 reason（误报复核依据）——缺失无法审计")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError("白名单必须附 reviewer（复核人）——无人复核不得豁免")
        if not isinstance(expires_days, int) or isinstance(expires_days, bool) or expires_days <= 0:
            raise ValueError(f"expires_days 必须为正整数，得到 {expires_days!r}")
        added_at = time.time() if now is None else now
        entry = {
            "rule_id": rule_id, "scope": scope,
            "reason": reason, "reviewer": reviewer,
            "added_at": added_at, "expires_at": added_at + expires_days * _DAY_S,
        }
        self._entries.append(entry)
        self.audit_log.append({"action": "add", "ts": added_at, **{
            k: entry[k] for k in ("rule_id", "scope", "reason", "reviewer", "expires_at")}})
        return entry

    def _hit(self, rule_id: str, scope: str, now: float) -> dict[str, Any] | None:
        for entry in self._entries:
            if (entry["rule_id"] == rule_id and entry["scope"] == scope
                    and entry["expires_at"] > now):
                return entry
        return None

    def check(self, rule_id: str, scope: str, now: float | None = None) -> bool:
        """(rule_id, scope) 是否在有效豁免期内——过期条目一律 False（自动失效）。"""
        return self._hit(rule_id, scope, time.time() if now is None else now) is not None

    def audit_log_view(self) -> list[dict[str, Any]]:
        """审计日志只读视图（机读）。"""
        return [dict(row) for row in self.audit_log]


def apply_whitelist(report: dict[str, Any], scope: str, whitelist: WhitelistManager,
                    now: float | None = None) -> dict[str, Any]:
    """blocked 报告过白名单复核（纯决策，不改传入报告）。

    findings 逐条对 (rule_id, scope) 查白名单：命中的被豁免；对剩余发现重
    derive score/severity/decision（口径与 scan_plugin 一致：风险分 = 100 -
    剩余扣分；剩余含 floor 规则或风险 ≥60 → HIGH，≥80 → CRITICAL）。
    命中记录进 whitelist_applied（含 reason/reviewer/expires_at——留痕）。
    非 blocked 报告原样返回（白名单只救被拦的，不给 admitted 加戏）。
    """
    if report.get("decision") != "blocked":
        return report
    from open_free_router.plugin_gate import RULES

    ts = time.time() if now is None else now
    kept: list[dict[str, Any]] = []
    waived: list[dict[str, Any]] = []
    for finding in report.get("findings", []):
        entry = whitelist._hit(finding.get("rule_id", ""), scope, ts)
        if entry is None:
            kept.append(finding)
        else:
            waived.append({**finding, "waived_by": entry["reviewer"],
                           "waive_reason": entry["reason"],
                           "expires_at": entry["expires_at"]})

    out = dict(report)
    out["whitelist_applied"] = waived
    if not waived:  # 一条未命中 → 报告原样（decision 不变）
        return out

    score = max(0, 100 - sum(f.get("deduct", 0) for f in kept))
    risk = 100 - score
    floor = any(RULES.get(f.get("rule_id", ""), {}).get("floor") for f in kept)
    if risk >= 80:
        severity = "CRITICAL"
    elif risk >= 60 or floor:
        severity = "HIGH"
    elif risk >= 30:
        severity = "MEDIUM"
    else:
        severity = "LOW"
    out["score"], out["severity"] = score, severity
    out["decision"] = "blocked" if severity in ("CRITICAL", "HIGH") else "admitted"
    return out
