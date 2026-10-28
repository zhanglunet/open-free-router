#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRD-258/T3（trend #1292）：接流量闸门——HIGH 及以上不接流量（结构性拦截）。

按 PRD-258/T1 设计（docs/plans/2026-10-28-prd-258-plugin-gate.md）实现 T3 范围（§3.3）：
  路由表加载器前置安检校验：后端适配器 / MCP 配置注册前必须附**有效**安检报告——
    scan_version 匹配 + 内容哈希一致；
  报告缺失 / scan_version 不匹配 / 哈希不一致（改包）/ decision=blocked → 拒绝注册
    （结构性拒绝，不是"提示"）；被拦插件进隔离清单；
  内容哈希 = 对 plugin 内容 dict 的 canonical json sha256 前 16 位；
  哈希变更（版本变更/改包）→ 旧报告自动作废（hash_mismatch 拒绝，提示重扫）；
  通过 → 注册并记 gate 状态（gate: open/blocked），供 doctor 显示。

红线：HIGH+ 不接流量是结构性拦截；闸门只做准入决策，不做运行时隔离（归 PRD-181）。
零网络；示例全虚构。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from open_free_router.plugin_gate import SCAN_VERSION, SCHEMA

GATE_SCHEMA = "plugin-gate-v1"

# 机器可读拒绝原因（reason）
R_REPORT_MISSING = "report_missing"          # 报告缺失——接流量前必须先安检
R_SCHEMA_MISMATCH = "report_schema_mismatch"  # 报告 schema 不符（非 plugin-gate-report-v1）
R_SCAN_VERSION_MISMATCH = "scan_version_mismatch"  # 扫描器版本不匹配（旧报告作废）
R_HASH_MISMATCH = "hash_mismatch"            # 内容哈希不一致——改包/版本变更，旧报告作废待重扫
R_BLOCKED = "blocked"                        # decision=blocked（HIGH/CRITICAL 不接流量）
R_INVALID_DECISION = "invalid_decision"      # decision 值不在 admitted/blocked 封闭集


def plugin_hash(content: dict[str, Any]) -> str:
    """插件内容哈希：canonical json（键排序、紧凑分隔符）sha256 前 16 位。

    同一内容不同键序 → 同哈希；任何内容变更（改包）→ 哈希变。
    """
    blob = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def stamp_hash(report: dict[str, Any], content: dict[str, Any]) -> dict[str, Any]:
    """安检报告存档时对被扫内容落哈希章（扫描存档点调用；不改动传入报告）。"""
    out = dict(report)
    out["content_hash"] = plugin_hash(content)
    return out


def registration_gate(plugin: dict[str, Any], report: dict[str, Any] | None,
                      current_hash: str) -> dict[str, Any]:
    """接流量闸门（§3.3）：路由表加载器前置安检校验（纯决策，无副作用）。

    有效性 = 报告存在 + schema 匹配 + scan_version 匹配 + content_hash 与当前哈希一致
    且 decision=admitted。任一不满足 → register=False（结构性拦截）。
    """
    name = str(plugin.get("name", "<unnamed>"))
    decision = {"schema": GATE_SCHEMA, "plugin": name,
                "content_hash": current_hash, "scan_version": SCAN_VERSION}

    def denied(reason: str, message: str) -> dict[str, Any]:
        return {**decision, "register": False, "gate": "blocked",
                "reason": reason, "message": message}

    if not isinstance(report, dict):
        return denied(R_REPORT_MISSING, "缺安检报告——接流量前必须先过 B 四维安检（§3.3）")
    if report.get("schema") != SCHEMA:
        return denied(R_SCHEMA_MISMATCH, f"报告 schema 必须为 {SCHEMA}")
    if report.get("scan_version") != SCAN_VERSION:
        return denied(R_SCAN_VERSION_MISMATCH,
                      f"报告 scan_version={report.get('scan_version')!r} 与当前扫描器 "
                      f"{SCAN_VERSION!r} 不匹配，旧报告作废——请重扫")
    if report.get("content_hash") != current_hash:
        return denied(R_HASH_MISMATCH,
                      "内容哈希不一致（插件已改包/版本变更）——旧报告自动作废，请重扫后再注册")
    if report.get("decision") not in ("admitted", "blocked"):
        return denied(R_INVALID_DECISION,
                      f"报告 decision 非法: {report.get('decision')!r}（封闭集 admitted/blocked）")
    if report.get("decision") == "blocked":
        return denied(R_BLOCKED,
                      f"安检 decision=blocked（severity={report.get('severity')}）"
                      "——HIGH 及以上不接流量（结构性拦截，隔离清单留痕）")
    return {**decision, "register": True, "gate": "open", "reason": "admitted",
            "severity": report.get("severity"), "score": report.get("score"),
            "message": "安检报告有效（scan_version+哈希一致），准予注册接流量"}


class PluginGate:
    """路由表加载器侧闸门：注册决策 + gate 状态登记（供 doctor 显示）。

    通过 → 注册（gate: open）；被拒 → 进隔离清单（gate: blocked，含 reason）。
    doctor 视图 = gate_status(name) / gate_view()，口径对齐 honesty 纪律（如实标注）。
    """

    def __init__(self) -> None:
        self._state: dict[str, dict[str, Any]] = {}

    def admit(self, plugin: dict[str, Any], report: dict[str, Any] | None,
              current_hash: str) -> dict[str, Any]:
        """前置安检校验 + 登记结果。通过才可进路由表；被拒进隔离清单。"""
        name = str(plugin.get("name", "<unnamed>"))
        verdict = registration_gate(plugin, report, current_hash)
        entry = {"plugin": name,
                 "version": str((report or {}).get("plugin", {}).get("version", "")
                                or plugin.get("version", "")),
                 "kind": plugin.get("kind", ""),
                 "gate": verdict["gate"],
                 "registered": bool(verdict["register"]),
                 "reason": verdict["reason"],
                 "content_hash": current_hash,
                 "scan_version": verdict["scan_version"],
                 "scanned_at": (report or {}).get("scanned_at", ""),
                 "message": verdict["message"]}
        self._state[name] = entry
        return verdict

    def is_registered(self, name: str) -> bool:
        entry = self._state.get(name)
        return bool(entry and entry["registered"])

    def gate_status(self, name: str) -> dict[str, Any]:
        """单插件 gate 状态（doctor 显示用；未登记如实返回 unregistered）。"""
        entry = self._state.get(name)
        if entry is None:
            return {"plugin": name, "gate": "unregistered", "registered": False,
                    "reason": "not_seen", "message": "未经闸门——尚未尝试注册"}
        return dict(entry)

    def isolation_list(self) -> list[str]:
        """隔离清单：被闸门拦下（gate=blocked）的插件名。"""
        return sorted(n for n, e in self._state.items() if not e["registered"])

    def gate_view(self) -> dict[str, Any]:
        """doctor 全量视图：gate 状态 + 隔离清单（机读）。"""
        return {"schema": GATE_SCHEMA,
                "plugins": [self.gate_status(n) for n in sorted(self._state)],
                "isolation": self.isolation_list()}
