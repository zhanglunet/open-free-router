"""Read-only route query for the openfree-skill (PRD-016/T2).

Answers "which upstream would this request be routed to" without any
network I/O and without changing any state.

- Explicit model IDs reuse ``RoutePlanner.resolve_explicit``.
- Virtual model names reuse ``RoutePlanner.plan`` (scored or priority,
  exactly as the proxy would).
- Empty input ("find me a usable model") ranks every registered target
  with ``score_candidate`` from scoring.py -- the same explainable scoring
  the model radar uses -- instead of inventing a new one.

Query results carry ``from: None`` (nothing was switched away from);
``resolved_to`` holds the answer, following the structured-reason shape
from the PRD-016/T1 skill draft.
"""
from __future__ import annotations

from datetime import datetime

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


def _rank_all_scored(planner: RoutePlanner) -> list[tuple[RouteTarget, dict]]:
    """Rank every registered target by explainable score.

    Candidate enumeration mirrors RoutePlanner._targets (dedup by provider +
    upstream model); ordering comes from scoring.score_candidate, not from
    registry priority.
    """
    seen: set[tuple[str, str]] = set()
    ranked: list[tuple[float, int, RouteTarget, dict]] = []
    order = 0
    for provider_name, provider in planner.registry.providers.items():
        for model in provider.models:
            target = RouteTarget(provider_name, provider, model)
            identity = (target.provider_name, target.upstream_model_id)
            if identity in seen:
                continue
            seen.add(identity)
            score = score_candidate(target, "auto", planner.config.scoring)
            ranked.append((score.total, order, target, score.to_dict()))
            order += 1
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [(row[2], row[3]) for row in ranked]


def _base(requested: str | None) -> dict:
    return {"from": None, "requested": requested, "at": _now_iso()}


def query_route(planner: RoutePlanner, requested: str | None) -> dict:
    """Resolve where a request would be routed, read-only.

    ``requested`` is an explicit model ID, a virtual model name (e.g.
    "auto", "free-smart"), or None/"" for "find me a usable model".
    """
    name = (requested or "").strip()

    if not name:
        ranked = _rank_all_scored(planner)
        result = _base(None)
        result["strategy"] = "best-scored"
        if not ranked:
            result["resolved_to"] = None
            result["reason"] = "注册表中没有可用模型（registry 为空）"
            return result
        target, score = ranked[0]
        payload = _target_payload(target)
        result.update(payload)
        result["resolved_to"] = payload["canonical_id"]
        result["reason"] = (
            f"未指定模型，按可解释评分选择最优可用模型：{payload['canonical_id']}"
            f"（总分 {score['total']:.3f}，共 {len(ranked)} 个候选）"
        )
        result["candidates"] = [target.canonical_id for target, _ in ranked]
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
