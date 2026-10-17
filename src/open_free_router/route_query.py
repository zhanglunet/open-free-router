"""Read-only route query for the openfree-skill (PRD-016/T2).

Answers "which upstream would this request be routed to" without any
network I/O and without changing any state.

- Explicit model IDs reuse ``RoutePlanner.resolve_explicit``.
- Virtual model names reuse ``RoutePlanner.plan`` (scored or priority,
  exactly as the proxy would).
- Empty input ("find me a usable model") ranks the *usable* registered
  targets with ``score_candidate`` from scoring.py -- the same explainable
  scoring the model radar uses -- instead of inventing a new one. A target
  is usable only when it is credential-ready (the same judgment
  ``UpstreamExecutor`` uses to skip ``credential_missing``) *and* has fresh
  successful probe evidence (the same ``probe.available_model_ids`` clock
  the dashboard uses). With no usable evidence the query reports
  "availability unknown" instead of guessing.

Query results carry ``from: None`` (nothing was switched away from);
``resolved_to`` holds the answer, following the structured-reason shape
from the PRD-016/T1 skill draft.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from open_free_router.probe import (
    available_model_ids,
    load_probe_snapshot,
    load_status_as_probe_snapshot,
)
from open_free_router.routing import RoutePlanner, RouteTarget
from open_free_router.scoring import score_candidate


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _target_payload(target: RouteTarget) -> dict:
    return {
        "canonical_id": target.canonical_id,
        "provider": target.provider_name,
        "upstream_model": target.upstream_model_id,
    }


def _has_credential(target: RouteTarget) -> bool:
    """Credential readiness, mirroring ``executor._credentials``.

    ``UpstreamExecutor`` skips a target as ``credential_missing`` exactly
    when ``_credentials(target)`` yields no slots (auth_mode "none" counts
    as ready; otherwise a configured api_key or effective key is needed).
    The query applies the same gate so it never recommends a model the
    proxy could not attempt. Kept inline (rather than importing the
    private helper) to avoid dragging executor's import chain into the
    query path; the rule is quoted here so the two stay in sync.
    """
    provider = target.provider
    if provider.auth_mode == "none":
        return True
    if provider.api_keys and any(provider.api_keys):
        return True
    return bool(provider.effective_key)


def _load_probe_snapshot(data_dir: Path | None) -> dict | None:
    """Load the probe snapshot the dashboard persists.

    Mirrors ``availability.load_probe_data`` (PRD-014/T2): prefer
    ``probe-results.json``, fall back to the legacy aggregated
    ``probe-status.json``. Returns ``None`` when no probe batch was ever
    persisted (or when no data dir is configured).
    """
    if data_dir is None:
        return None
    snapshot = load_probe_snapshot(Path(data_dir) / "probe-results.json")
    if snapshot is None:
        snapshot = load_status_as_probe_snapshot(Path(data_dir) / "probe-status.json")
    return snapshot


def _rank_usable_scored(
    planner: RoutePlanner, data_dir: Path | None = None
) -> tuple[list[tuple[RouteTarget, dict]], dict | None, dict]:
    """Rank usable registered targets by explainable score.

    Candidate enumeration mirrors RoutePlanner._targets (dedup by provider +
    upstream model); a target is eligible only with credential readiness
    *and* fresh successful probe evidence. Ordering comes from
    scoring.score_candidate, not from registry priority.

    Returns ``(ranked, snapshot, stats)`` where ``snapshot`` is ``None``
    when no probe batch was ever persisted, and ``stats`` carries the
    ``{"total", "credential_ready", "fresh_evidence"}`` counts for the
    reason text.
    """
    snapshot = _load_probe_snapshot(data_dir)
    fresh, _stale = (
        available_model_ids(snapshot) if snapshot is not None else (set(), 0)
    )
    seen: set[tuple[str, str]] = set()
    ranked: list[tuple[float, int, RouteTarget, dict]] = []
    stats = {"total": 0, "credential_ready": 0, "fresh_evidence": 0}
    order = 0
    for provider_name, provider in planner.registry.providers.items():
        for model in provider.models:
            target = RouteTarget(provider_name, provider, model)
            identity = (target.provider_name, target.upstream_model_id)
            if identity in seen:
                continue
            seen.add(identity)
            stats["total"] += 1
            if not _has_credential(target):
                continue
            stats["credential_ready"] += 1
            if f"{target.provider_name}/{target.model.id}" not in fresh:
                continue
            stats["fresh_evidence"] += 1
            score = score_candidate(target, "auto", planner.config.scoring)
            ranked.append((score.total, order, target, score.to_dict()))
            order += 1
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [(row[2], row[3]) for row in ranked], snapshot, stats


def _base(requested: str | None) -> dict:
    return {"from": None, "requested": requested, "at": _now_iso()}


def query_route(
    planner: RoutePlanner,
    requested: str | None,
    data_dir: Path | None = None,
) -> dict:
    """Resolve where a request would be routed, read-only.

    ``requested`` is an explicit model ID, a virtual model name (e.g.
    "auto", "free-smart"), or None/"" for "find me a usable model".
    ``data_dir`` locates the probe snapshot; without it (or without any
    persisted probe batch) availability is reported as unknown.
    """
    name = (requested or "").strip()

    if not name:
        ranked, snapshot, stats = _rank_usable_scored(planner, data_dir)
        result = _base(None)
        result["strategy"] = "best-scored"
        result["candidates"] = [target.canonical_id for target, _ in ranked]
        if snapshot is None:
            result["resolved_to"] = None
            result["reason"] = (
                "可用性未知：还没有任何探测数据（"
                f"注册表共 {stats['total']} 个候选）。"
                "先运行一次探测（面板 Live Status 页）后再查询，"
                "不要把“没测过”当成“能用”。"
            )
            return result
        if not ranked:
            result["resolved_to"] = None
            result["reason"] = (
                "可用性未知：探测数据存在，但当前没有“配好凭证且有新鲜成功"
                "探测证据”的模型"
                f"（注册表 {stats['total']} 个候选，凭证就绪 "
                f"{stats['credential_ready']} 个，有新鲜证据 "
                f"{stats['fresh_evidence']} 个）。"
                "检查各提供商的 API key 配置并重新探测后再查询。"
            )
            return result
        target, score = ranked[0]
        payload = _target_payload(target)
        result.update(payload)
        result["resolved_to"] = payload["canonical_id"]
        result["reason"] = (
            "未指定模型，在有可用证据的候选里按可解释评分选择最优："
            f"{payload['canonical_id']}（总分 {score['total']:.3f}，"
            f"{stats['fresh_evidence']} 个有新鲜证据的候选）"
        )
        result["score"] = score
        return result

    if name in planner.virtual_model_ids():
        plan = planner.plan(name)
        result = _base(name)
        result["strategy"] = plan.strategy
        result["candidates"] = [target.canonical_id for target in plan.candidates]
        result["rejected"] = list(plan.rejected)
        selected = plan.selected
        if selected is None:
            detail = "; ".join(
                f"{item['model']}（{item['reason']}）" for item in plan.rejected[:3]
            )
            result["resolved_to"] = None
            result["reason"] = (
                f"虚拟模型 {name} 没有可用候选" + (f"：{detail}" if detail else "")
            )
            return result
        payload = _target_payload(selected)
        result.update(payload)
        result["resolved_to"] = payload["canonical_id"]
        score = next(
            (item for item in plan.scores if item.model == payload["canonical_id"]),
            None,
        )
        if score is not None:
            score_dict = score.to_dict()
            result["score"] = score_dict
            top_factor = max(
                score_dict["factors"].items(),
                key=lambda item: item[1]["contribution"],
            )
            result["reason"] = (
                f"虚拟模型 {name} 按 {plan.strategy} 策略路由到 {payload['canonical_id']}"
                f"（可解释评分 {score_dict['total']:.3f}，主要因子 {top_factor[0]}）"
            )
        else:
            result["reason"] = (
                f"虚拟模型 {name} 按 {plan.strategy} 策略路由到 {payload['canonical_id']}"
                "（注册表优先级顺序）"
            )
        return result

    target = planner.resolve_explicit(name)
    result = _base(name)
    result["strategy"] = "explicit"
    if target is None:
        result["resolved_to"] = None
        result["reason"] = (
            f"MODEL_NOT_FOUND：注册表中没有 {name}"
            "（运行 `open-free-router models` 查看可用 ID）"
        )
        return result
    payload = _target_payload(target)
    result.update(payload)
    result["resolved_to"] = payload["canonical_id"]
    result["reason"] = (
        f"显式模型 {name} 命中注册表：{payload['canonical_id']}"
        f"（提供商 {payload['provider']}，上游模型 {payload['upstream_model']}）"
    )
    return result
