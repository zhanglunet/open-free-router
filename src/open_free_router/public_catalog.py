"""Redacted model catalog export for documentation and status pages."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from open_free_router.registry import Registry, codex_model_alias


def _capability_score(context: int, output: int, reasoning: bool, tools: bool) -> int:
    """Feature-density score, not an intelligence or quality benchmark."""
    context_points = min(35, round(max(0, math.log2(max(context, 1)) - 10) * 5))
    output_points = min(20, round(max(0, math.log2(max(output, 1)) - 8) * 4))
    return min(100, context_points + output_points + (20 if reasoning else 0) + (25 if tools else 0))


def _family_zh(model_id: str) -> str:
    value = model_id.lower()
    families = {
        "gemini": "Google Gemini",
        "gpt-oss": "OpenAI GPT-OSS 开放权重",
        "deepseek": "DeepSeek",
        "llama": "Meta Llama",
        "nemotron": "NVIDIA Nemotron",
        "glm": "智谱 GLM",
        "qwen": "阿里 Qwen",
        "gemma": "Google Gemma",
        "minimax": "MiniMax",
        "step": "阶跃 Step",
        "ernie": "百度 ERNIE",
        "kimi": "月之暗面 Kimi",
        "laguna": "Poolside Laguna",
        "ling": "Ling",
    }
    return next((label for marker, label in families.items() if marker in value), "通用大语言模型")


def _use_case(context: int, reasoning: bool, tools: bool) -> str:
    if reasoning and tools:
        return "复杂编码、Agent 工具链和多步分析"
    if tools:
        return "函数调用、自动化流程和轻量代码任务"
    if reasoning:
        return "规划、推理、数学和复杂问题拆解"
    if context >= 128_000:
        return "长文档理解、摘要和通用问答"
    return "日常问答、改写、分类和短文本生成"


def _speed_tier(availability: str, latency_ms: int | None) -> str:
    # Only "unavailable" may produce the affirmative claim. A model nobody has
    # probed is not down, and worker/probe.js copies this field onto the live
    # /api/catalog without recomputing it, so the claim would reach the board.
    if availability == "unavailable":
        return "当前不可用"
    if availability != "available" or latency_ms is None:
        return "未测"
    if latency_ms <= 1500:
        return "快"
    if latency_ms <= 3000:
        return "中等"
    return "较慢"


def build_public_catalog(
    registry: Registry,
    status_snapshot: dict,
    provider_profiles: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """Build the redacted public catalog.

    ``now`` pins ``generated_at`` (and the free-tier evidence clock derived from
    it) so CI can re-export with the committed timestamp and byte-diff the
    result. Without that the export is not reproducible and the freshness gate
    could be cleared by hand-editing a timestamp.
    """
    statuses = status_snapshot.get("providers") or {}
    # Per-model evidence, keyed "provider/model" exactly as the Cloudflare probe
    # keys it. Absent it, a model is unverified rather than inheriting the
    # provider's verdict — that inheritance is what published models as
    # available on the strength of one smoke test against a sibling.
    model_statuses = status_snapshot.get("models") or {}
    provider_profiles = provider_profiles or {}
    providers = []
    model_total = 0
    generated_at = now or datetime.now(timezone.utc)
    for name, provider in registry.providers.items():
        status = statuses.get(name) or {
            "availability": "unverified",
            "reason": "No recent smoke-test evidence",
            "latency_ms": None,
        }
        profile = provider_profiles.get(name) or {}
        provider_free_tier = provider.free_tier.to_dict(include_status=True, now=generated_at)
        models = []
        for model in provider.models:
            model_total += 1
            model_status = model_statuses.get(f"{name}/{model.id}") or {}
            free_tier = provider.free_tier_for(model)
            free_tier_payload = free_tier.to_dict(include_status=True, now=generated_at)
            free_status = free_tier_payload["status"]
            models.append({
                "id": model.id,
                "upstream_id": model.effective_upstream_id,
                "codex_alias": codex_model_alias(provider.model_prefix, model.id),
                "name": model.name or model.id,
                "context_window": model.context_window,
                "max_tokens": model.max_tokens,
                "reasoning": model.reasoning,
                "tool_calling": model.tool_calling,
                "input_modalities": ["text"],
                "availability": model_status.get("availability", "unverified"),
                "capability_score": _capability_score(
                    model.context_window, model.max_tokens, model.reasoning, model.tool_calling
                ),
                "family_zh": _family_zh(model.effective_upstream_id),
                "description_zh": (
                    f"{_family_zh(model.effective_upstream_id)} 系列文本模型；声明上下文 "
                    f"{model.context_window:,} tokens，最大输出 {model.max_tokens:,} tokens。"
                ),
                "recommended_for_zh": _use_case(
                    model.context_window, model.reasoning, model.tool_calling
                ),
                "speed_tier_zh": _speed_tier(
                    model_status.get("availability", "unverified"), model_status.get("latency_ms")
                ),
                "benchmark_note_zh": "暂无统一独立质量基准；功能指数不代表智力排名。",
                "free_tier": free_tier_payload,
                "free_availability": (
                    "verified_free" if free_status == "verified"
                    else "review_required" if free_status in ("expired", "invalid", "unverified")
                    else "unknown"
                ),
            })
        providers.append({
            "id": name,
            "name": name.replace("-", " ").title(),
            "prefix": provider.model_prefix,
            "api": provider.upstream_url or provider.base_url,
            "availability": status.get("availability", "unverified"),
            "reason": status.get("reason", "No recent smoke-test evidence"),
            "latency_ms": status.get("latency_ms"),
            "checked_at": status.get("checked_at", status_snapshot.get("as_of", "")),
            "model_count": len(models),
            "models": models,
            "profile": profile,
            "free_tier": provider_free_tier,
        })
    availability_order = {"available": 0, "unverified": 1, "unavailable": 2}
    providers.sort(key=lambda item: (availability_order.get(item["availability"], 9), item["id"]))
    return {
        "schema_version": 2,
        "generated_at": generated_at.isoformat(),
        "status_as_of": status_snapshot.get("as_of", ""),
        "status_note": (
            "Availability is the latest safe smoke-test snapshot, not an SLA. "
            "Capability score compares declared features only, not model intelligence."
            " Free-tier claims require unexpired evidence; expired evidence is review_required."
        ),
        "provider_count": len(providers),
        "model_count": model_total,
        "providers": providers,
    }


def write_public_catalog(catalog: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    forbidden = (
        "api_key", "api_keys", "proxy.token", "ui.token", "authorization",
        '"cookie"', '"token_value"', '"prompt"', '"messages"', '"tool_arguments"',
    )
    lowered = text.lower()
    if any(term in lowered for term in forbidden):
        raise ValueError("Public catalog contains a forbidden credential field")
    secret_patterns = (
        r"\bbearer\s+[a-z0-9._~+/=-]{8,}",
        r"\bsk-[a-z0-9_-]{8,}",
        r"\bAIza[0-9A-Za-z_-]{20,}",
    )
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in secret_patterns):
        raise ValueError("Public catalog contains a credential-like value")
    path.write_text(text, encoding="utf-8")
