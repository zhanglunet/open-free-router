#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRD-258/T2（trend #1291）：后端插件安检适配最小实现——B 四维静态扫描。

按 PRD-258/T1 设计（docs/plans/2026-10-28-prd-258-plugin-gate.md）实现 T2 范围：
  扫描对象：第三方后端适配器（代码文件）/ MCP 配置片段（JSON 工具定义）；
  B-1 工具投毒（工具描述内嵌指令/外发要求；与内建近名伪装）
  B-2 凭据访问（明文密钥模式 floor HIGH；凭证路径触碰）
  B-3 过度授权（请求域超出声明 provider 官方域）
  B-4 清单一致性（license 声明与实测不符直接 HIGH；name/version 缺失）
  0-100 安全分 + 风险值 risk=100-score 四档严重度；HIGH+ blocked（不接流量）；
  plugin-gate-report-v1 JSON 报告；evidence/location 占位符化。
  零网络（license 实测由调用方传入；漏洞信息不在本层）。示例全虚构。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SCHEMA = "plugin-gate-report-v1"
SCAN_VERSION = "0.1.0"

RULES: dict[str, dict[str, Any]] = {
    "B-1-01": {"dimension": "B-1", "deduct": 35, "floor": "HIGH", "desc": "工具描述内嵌指令/外发要求"},
    "B-1-02": {"dimension": "B-1", "deduct": 25, "floor": "HIGH", "desc": "工具名与内建近名伪装"},
    "B-2-01": {"dimension": "B-2", "deduct": 30, "floor": "HIGH", "desc": "明文密钥模式"},
    "B-2-02": {"dimension": "B-2", "deduct": 25, "floor": "HIGH", "desc": "凭证路径触碰"},
    "B-3-01": {"dimension": "B-3", "deduct": 25, "floor": "HIGH", "desc": "请求域超出声明官方域"},
    "B-4-01": {"dimension": "B-4", "deduct": 10, "desc": "name/version 缺失"},
    "B-4-02": {"dimension": "B-4", "deduct": 15, "floor": "HIGH", "desc": "license 声明与实测不符"},
}

_BUILTIN_TOOL_NAMES = ("list_models", "list_backends", "health_check", "route_status")
_POISON_RE = re.compile(
    r"(读取|发送|上报|外传|上传).{0,12}(api[_ ]?key|token|密钥|凭据)"
    r"|(api[_ ]?key|token|密钥).{0,12}(读取|发送|上报|外传|上传)"
    r"|(POST|GET)\s+https?://"
    r"|(忽略|无视).{0,8}(规则|指令)", re.I)
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
)
_CRED_PATH_RE = re.compile(r"\.env|\.ssh|credentials", re.I)
_URL_RE = re.compile(r"https?://([^/\s\"']+)")
_LOCAL_RE = re.compile(r"(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}")


def _redact(text: str) -> str:
    text = _SECRET_PATTERNS and re.sub(
        r"sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|Bearer\s+[A-Za-z0-9._-]{16,}|gh[pousr]_[A-Za-z0-9]{30,}",
        "<REDACTED>", text)
    return _LOCAL_RE.sub("<REDACTED>", text)


def _finding(rule_id: str, location: str, detail: str) -> dict[str, Any]:
    rule = RULES[rule_id]
    return {
        "rule_id": rule_id,
        "dimension": rule["dimension"],
        "severity_suggest": rule.get("floor", "MEDIUM"),
        "evidence": _redact(detail),
        "location": location,
        "deduct": rule["deduct"],
    }


def scan_mcp_config(mcp: dict[str, Any], *, builtin_names: tuple[str, ...] = _BUILTIN_TOOL_NAMES,
                    declared_domains: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """B-1：MCP 工具定义扫描。"""
    findings: list[dict[str, Any]] = []
    tools = mcp.get("tools", [])
    if isinstance(tools, dict):
        tools = list(tools.values())
    for i, tool in enumerate(tools):
        name = str(tool.get("name", ""))
        desc = str(tool.get("description", ""))
        loc = f"mcp.json:tools[{i}].description"
        if _POISON_RE.search(desc):
            findings.append(_finding("B-1-01", loc, f"工具 {name} 描述内嵌指令/外发要求"))
        for builtin in builtin_names:
            if name != builtin and _near_name(name, builtin):
                findings.append(_finding("B-1-02", f"mcp.json:tools[{i}].name",
                                         f"工具名 {name} 与内建 {builtin} 近名伪装"))
                break
        for m in _URL_RE.finditer(desc):
            domain = m.group(1).lower()
            if declared_domains and not any(domain == d or domain.endswith("." + d) for d in declared_domains):
                findings.append(_finding("B-3-01", loc,
                                         f"描述内 URL 域 {domain} 超出声明官方域 {'/'.join(declared_domains)}"))
    return findings


def _near_name(a: str, b: str) -> bool:
    """近名判定：去分隔符小写后编辑距离 1（长度≥8 才算伪装，避免短名误报）。"""
    a, b = re.sub(r"[-_.]", "", a).lower(), re.sub(r"[-_.]", "", b).lower()
    if abs(len(a) - len(b)) > 1 or len(b) < 8:
        return False
    if a == b:
        return False
    # 简化编辑距离：等长=差一字符；差一=插入/删除
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    small, big = (a, b) if len(a) < len(b) else (b, a)
    for i in range(len(big)):
        if big[:i] + big[i + 1:] == small:
            return True
    return False


def scan_adapter_code(code: str, rel: str = "adapter.py",
                      declared_domains: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """B-2/B-3：适配器代码扫描。"""
    findings: list[dict[str, Any]] = []
    for pat in _SECRET_PATTERNS:
        m = pat.search(code)
        if m:
            findings.append(_finding("B-2-01", rel, f"检出明文密钥模式（命中样例 {_redact(m.group(0))}）"))
            break
    if _CRED_PATH_RE.search(code):
        findings.append(_finding("B-2-02", rel, "检出凭证路径触碰（.env/ssh/credentials）"))
    for m in _URL_RE.finditer(code):
        domain = m.group(1).lower()
        if declared_domains and not any(domain == d or domain.endswith("." + d) for d in declared_domains):
            findings.append(_finding("B-3-01", rel, f"请求域 {domain} 超出声明官方域 {'/'.join(declared_domains)}"))
            break
    return findings


def scan_plugin(*, kind: str, name: str = "", version: str = "",
                mcp: dict[str, Any] | None = None, code: str | None = None,
                declared_license: str | None = None, verified_license: str | None = None,
                declared_domains: tuple[str, ...] = ()) -> dict[str, Any]:
    """汇总扫描 → plugin-gate-report-v1。kind: backend-adapter|mcp-config。"""
    findings: list[dict[str, Any]] = []
    if mcp is not None:
        findings += scan_mcp_config(mcp, declared_domains=declared_domains)
    if code is not None:
        findings += scan_adapter_code(code, declared_domains=declared_domains)

    if not name or not version:
        findings.append(_finding("B-4-01", "manifest", "name/version 缺失"))
    lic_declared = declared_license or "MISSING"
    lic_verified = verified_license or "MISSING"
    if lic_declared == "MISSING" or lic_verified == "MISSING" or lic_declared != lic_verified:
        findings.append(_finding("B-4-02", "manifest",
                                 f"license 声明({lic_declared})与实测({lic_verified})不符或缺失"))

    score = 100
    floor = None
    for f in findings:
        score = max(0, score - f["deduct"])
        if RULES.get(f["rule_id"], {}).get("floor"):
            floor = "HIGH"
    risk = 100 - score
    if risk >= 80:
        severity = "CRITICAL"
    elif risk >= 60 or floor:
        severity = "HIGH"
    elif risk >= 30:
        severity = "MEDIUM"
    else:
        severity = "LOW"
    decision = "blocked" if severity in ("CRITICAL", "HIGH") else "admitted"

    return {
        "schema": SCHEMA,
        "scanned_at": "2026-10-28T00:00:00Z",
        "plugin": {"name": name or "<unnamed>", "version": version or "0.1.0",
                   "kind": kind,
                   "license_declared": lic_declared, "license_verified": lic_verified},
        "score": score, "severity": severity, "decision": decision,
        "findings": findings,
        "whitelist_applied": [],
        "scan_version": SCAN_VERSION,
    }
