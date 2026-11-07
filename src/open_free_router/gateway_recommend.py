#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRD-229/T3（trend #1147）：学习网关——策略推荐生成（推荐值 + 证据 + 预期收益）。

按 T1 设计（docs/plans/2026-10-26-learning-gateway-design.md）§2.3/§4/§5 实现，
只消费 T2 聚合集（gateway-aggregate/v1，含 §3 扩展列）：

  - **无证据不给推荐**（§5）：证据链字段缺失或 §2.3 硬规则（R1~R6）任一不满足时，
    只产观察记录（kind=observation，含触发的规则编号）——「为什么没推荐」可见，
    不静默无输出；
  - **R1~R6 写死不得绕过**（§2.3）：R1 分组 n≥30 / R2 对比双方各 n≥30 /
    R3 时间覆盖 ≥3 个自然日 / R4 效应量（成功率差 ≥5pp 或 TTFB p95 差 ≥20% 或
    单位成功成本差 ≥15%）/ R5 成本型推荐要求 token_coverage_rate ≥0.5 /
    R6 分类覆盖率 ≥0.5；
  - **参数白名单**（§4）：推荐只指向 whitelist 内参数；本模块产出
    parameter="candidate_first"（候选首选，prompt_class 级）；白名单外一律不产出；
  - **推荐格式**（§5 固定字段缺一不可）：recommendation_id / kind / generated_at /
    status=pending / scope / parameter / current_value / recommended_value /
    evidence（window+classifier_version+metric_version+groups+rules_passed+source）/
    expected_benefit（窗口流量静态重放估算，basis 写明，不给无依据百分比）/
    expires_at（generated_at+7 天）；
  - **一条推荐只动一个参数**；多参数组合拆条（本模块单参数）；
  - **不推荐面**（§4 排除项）：凭据/熔断/超时/注册表/沙盒永不推荐——本模块
    参数面物理上只有 candidate_first，排除项不可达；
  - 确定性：recommendation_id 为内容寻址 hex 16（T1 口径为 uuid——为 T5 回放
    可复现改为内容寻址，格式一致，变更随本 PR 记录）；generated_at/expires_at
    由调用方注入（时间注入约定），模块不取墙钟。

零网络；全程零自动变更（status 恒 pending，应用归 T4 人工确认流程）。
测试：tests/test_prd229_t3_recommend.py
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

METRIC_VERSION = "gateway-recommend/v1"
#: §2.3 硬规则阈值（写死，不得绕过）
R1_MIN_N = 30
R3_MIN_DAYS = 3
R4_SUCCESS_DIFF_PP = 0.05          # 5 个百分点
R4_TTFB_DIFF_RATIO = 0.20          # 20%
R4_COST_DIFF_RATIO = 0.15          # 15%
R5_MIN_TOKEN_COVERAGE = 0.5
R6_MIN_CLASS_COVERAGE = 0.5
#: §5 推荐保质期（天）
RECOMMENDATION_TTL_DAYS = 7
#: §4 参数白名单（穷举；推荐只允许指向其中参数）
PARAMETER_WHITELIST = (
    "policy_weights.quality", "policy_weights.cost", "policy_weights.latency",
    "candidate_first", "cost_downgrade_threshold", "cost_downgrade_factor",
    "max_attempts",
)
#: §4 明确排除面（永不推荐；本模块参数面物理不含，列出以供 T4 应用器复核）
EXCLUDED_PARAMETERS = ("credentials", "resilience", "timeout", "registry", "sandbox")

EVIDENCE_GROUP_FIELDS = ("provider", "model", "n", "success_rate", "ttfb_p95_ms",
                         "tokens_per_success", "wasted_attempt_ratio",
                         "token_coverage_rate", "days_covered")


class RecommendError(ValueError):
    """推荐生成输入不合法（非 gateway-aggregate/v1 / 时间注入缺失等）。"""


def _parse_generated_at(generated_at: str) -> datetime:
    if not isinstance(generated_at, str) or not generated_at.strip():
        raise RecommendError("generated_at 必填（时间注入约定，ISO-8601 UTC）")
    try:
        moment = datetime.fromisoformat(generated_at)
    except ValueError as exc:
        raise RecommendError(f"generated_at 不是合法 ISO-8601：{generated_at!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _evidence_group(row: dict[str, Any]) -> dict[str, Any]:
    group = {}
    for field in EVIDENCE_GROUP_FIELDS:
        group[field] = row.get(field)
    return group


def _rule_checks(current: dict[str, Any], candidate: dict[str, Any],
                 class_coverage: float) -> dict[str, tuple[bool, str]]:
    """R1~R6 逐条判定：返回 {规则: (通过?, 未过原因码)}。"""
    checks: dict[str, tuple[bool, str]] = {}
    checks["R1"] = (current["n"] >= R1_MIN_N, "insufficient_samples")
    checks["R2"] = (candidate["n"] >= R1_MIN_N, "insufficient_samples")
    checks["R3"] = (current["days_covered"] >= R3_MIN_DAYS
                    and candidate["days_covered"] >= R3_MIN_DAYS,
                    "insufficient_day_coverage")
    # R4 效应量（三选一即过，同时记录触发指标供 R5 判成本型）
    success_diff = None
    if current["success_rate"] is not None and candidate["success_rate"] is not None:
        success_diff = candidate["success_rate"] - current["success_rate"]
    ttfb_ratio = None
    if current["ttfb_p95_ms"] and candidate["ttfb_p95_ms"]:
        ttfb_ratio = (current["ttfb_p95_ms"] - candidate["ttfb_p95_ms"]) / current["ttfb_p95_ms"]
    cost_ratio = None
    if current["tokens_per_success"] and candidate["tokens_per_success"]:
        cost_ratio = ((current["tokens_per_success"] - candidate["tokens_per_success"])
                      / current["tokens_per_success"])
    triggered = []
    if success_diff is not None and success_diff >= R4_SUCCESS_DIFF_PP:
        triggered.append(("success_rate", success_diff))
    if ttfb_ratio is not None and ttfb_ratio >= R4_TTFB_DIFF_RATIO:
        triggered.append(("ttfb_p95_ms", ttfb_ratio))
    if cost_ratio is not None and cost_ratio >= R4_COST_DIFF_RATIO:
        triggered.append(("tokens_per_success", cost_ratio))
    checks["R4"] = (bool(triggered), "effect_below_threshold")
    # R5 成本型（触发指标含单位成功成本）要求双方 token 覆盖率 ≥0.5
    cost_type = any(metric == "tokens_per_success" for metric, _ in triggered)
    if cost_type:
        checks["R5"] = (
            current["token_coverage_rate"] >= R5_MIN_TOKEN_COVERAGE
            and candidate["token_coverage_rate"] >= R5_MIN_TOKEN_COVERAGE,
            "insufficient_token_coverage")
    else:
        checks["R5"] = (True, "not_cost_type")
    checks["R6"] = (class_coverage >= R6_MIN_CLASS_COVERAGE, "insufficient_class_coverage")
    return checks, triggered


def recommend(aggregate: dict[str, Any], *, generated_at: str) -> dict[str, Any]:
    """T2 聚合集 → 推荐集（recommendation-set/v1，确定性纯函数）。

    对每个 prompt_class：现任首选 = n 最大的分组（并列取排序名小者，确定性）；
    挑战者 = 除现任外按 (success_rate desc, ttfb_p95 asc, tokens_per_success asc,
    provider, model) 排序的第一名。现任=挑战者或挑战者缺失 → 无候选，不产记录。
    规则全过 → kind=recommendation；任一不过 → kind=observation（含 reason）。
    """
    moment = _parse_generated_at(generated_at)
    if not isinstance(aggregate, dict) or aggregate.get("schema") != "gateway-aggregate/v1" \
            or not isinstance(aggregate.get("aggregate"), list):
        raise RecommendError("输入必须是 T2 聚合集（gateway-aggregate/v1）")

    rows = aggregate["aggregate"]
    total_events = sum(r["n"] for r in rows) or 0
    by_class: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_class.setdefault(row["prompt_class"], []).append(row)

    window = aggregate["window"]
    records: list[dict[str, Any]] = []
    for prompt_class in sorted(by_class):
        groups = by_class[prompt_class]
        class_coverage = (sum(g["n"] for g in groups) / total_events) if total_events else 0.0
        ranked = sorted(groups, key=lambda g: (-g["n"], g["provider"], g["model"]))
        current = ranked[0]
        challengers = sorted(
            [g for g in groups if g is not current],
            key=lambda g: (-(g["success_rate"] if g["success_rate"] is not None else -1),
                           g["ttfb_p95_ms"] if g["ttfb_p95_ms"] is not None else float("inf"),
                           g["tokens_per_success"] if g["tokens_per_success"] is not None else float("inf"),
                           g["provider"], g["model"]))
        if not challengers:
            continue  # 单后端无对比对象：无候选也无分歧，不产记录
        candidate = challengers[0]
        checks, triggered = _rule_checks(current, candidate, class_coverage)
        rules_passed = sorted(rule for rule, (ok, _) in checks.items() if ok)
        failed = {rule: reason for rule, (ok, reason) in checks.items() if not ok}
        evidence = {
            "window": {"from": window["start"], "to": window["end_exclusive"],
                       "days_covered": max(current["days_covered"], candidate["days_covered"])},
            "classifier_version": aggregate["classifier_version"],
            "metric_version": METRIC_VERSION,
            "groups": [_evidence_group(current), _evidence_group(candidate)],
            "rules_passed": rules_passed,
            "source": "usage.db: usage_events（窗口内全量，gateway-aggregate/v1 event_ids 可逐条对回）",
        }
        payload = {
            "scope": {"prompt_class": prompt_class, "virtual_model": None},
            "parameter": "candidate_first",
            "current_value": {"provider": current["provider"], "model": current["model"]},
            "recommended_value": {"provider": candidate["provider"], "model": candidate["model"]},
            "evidence": evidence,
        }
        if failed:
            records.append({
                "recommendation_id": _content_id(payload),
                "kind": "observation",
                "generated_at": moment.isoformat(),
                "scope": payload["scope"],
                "parameter": payload["parameter"],
                "current_value": payload["current_value"],
                "observed_value": payload["recommended_value"],
                "evidence": evidence,
                "rules_failed": failed,
                "note": "未达推荐门槛：不产推荐只产观察（§5 无证据不给推荐）；"
                        "规则阈值见 docs/plans/2026-10-26-learning-gateway-design.md §2.3",
            })
            continue
        metric, effect = triggered[0]
        benefit = _expected_benefit(metric, effect, current, candidate)
        records.append({
            "recommendation_id": _content_id(payload),
            "kind": "recommendation",
            "generated_at": moment.isoformat(),
            "status": "pending",
            **payload,
            "expected_benefit": benefit,
            "expires_at": (moment + timedelta(days=RECOMMENDATION_TTL_DAYS)).isoformat(),
        })

    recommendations = [r for r in records if r["kind"] == "recommendation"]
    return {
        "schema": "recommendation-set/v1",
        "generated_at": moment.isoformat(),
        "window": {"start": window["start"], "end_exclusive": window["end_exclusive"]},
        "metric_version": METRIC_VERSION,
        "parameter_whitelist": list(PARAMETER_WHITELIST),
        "excluded_parameters": list(EXCLUDED_PARAMETERS),
        "records": records,
        "summary": {
            "recommendations_total": len(recommendations),
            "observations_total": len(records) - len(recommendations),
            "classes_evaluated": len(by_class),
            "auto_apply": False,
            "note": "全部记录 status=pending（观察记录无 status 字段）；确认后才改策略，"
                    "零自动变更（T4 人工确认流程）",
        },
    }


def _expected_benefit(metric: str, effect: float,
                      current: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """预期收益：按窗口流量静态重放估算（§5：不是承诺，不给无依据百分比）。"""
    if metric == "success_rate":
        return {
            "metric": "success_rate",
            "from": current["success_rate"],
            "to": candidate["success_rate"],
            "basis": "按窗口内该 scope 流量静态重放估算（成功率差 "
                     f"{effect:.4f} 个小数位值）；不是承诺，实际效果以应用后观察为准",
        }
    if metric == "ttfb_p95_ms":
        return {
            "metric": "ttfb_p95_ms",
            "from": current["ttfb_p95_ms"],
            "to": candidate["ttfb_p95_ms"],
            "basis": "按窗口内该 scope 流量静态重放估算（TTFB p95 相对差 "
                     f"{effect:.4f}）；不是承诺，实际效果以应用后观察为准",
        }
    return {
        "metric": "tokens_per_success",
        "from": current["tokens_per_success"],
        "to": candidate["tokens_per_success"],
        "basis": "按窗口内该 scope 流量静态重放估算（单位成功成本相对差 "
                 f"{effect:.4f}）；不是承诺，实际效果以应用后观察为准",
    }


def _content_id(payload: dict[str, Any]) -> str:
    """内容寻址 id（sha256 前 16 hex；T1 口径为 uuid，改为确定性以支 T5 回放）。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def validate_record(record: dict[str, Any]) -> list[str]:
    """单条记录契约校验：recommendation 固定字段缺一不可（§5）。"""
    errors: list[str] = []
    if record.get("kind") == "recommendation":
        for field in ("recommendation_id", "kind", "generated_at", "status", "scope",
                      "parameter", "current_value", "recommended_value", "evidence",
                      "expected_benefit", "expires_at"):
            if field not in record:
                errors.append(f"缺必填字段 {field}（§5 固定格式）")
        if record.get("status") != "pending":
            errors.append("status 必须为 pending（确认后才改策略，零自动变更）")
        if record.get("parameter") not in PARAMETER_WHITELIST:
            errors.append(f"parameter 不在白名单（§4）：{record.get('parameter')!r}")
        evidence = record.get("evidence") or {}
        for field in ("window", "classifier_version", "metric_version", "groups",
                      "rules_passed", "source"):
            if field not in evidence:
                errors.append(f"evidence.{field} 缺失——无证据不给推荐（§5）")
        if len((evidence or {}).get("groups") or []) < 2:
            errors.append("evidence.groups 必须 ≥2 组（对比双方）")
    return errors
