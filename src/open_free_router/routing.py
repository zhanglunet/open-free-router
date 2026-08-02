"""Deterministic route planning for explicit and virtual model IDs.

This module deliberately contains no network I/O.  It turns a requested model
into an ordered, bounded candidate list that the proxy executor can consume.
Keeping planning separate makes route decisions explainable and lets every wire
protocol share the same behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Iterable

from open_free_router.registry import ModelInfo, ProviderConfig, Registry, codex_model_alias
from open_free_router.scoring import CandidateScore, ScoringConfig, score_candidate


BUILTIN_ALIASES = ("auto", "auto/coding", "auto/fast", "auto/free")


@dataclass(frozen=True)
class RoutingDiagnostic:
    severity: str
    code: str
    path: str
    message: str
    fix: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "fix": self.fix,
        }


@dataclass(frozen=True)
class AliasRule:
    candidates: tuple[str, ...] = ()
    require_tool_calling: bool = False


@dataclass(frozen=True)
class RoutingConfig:
    """Validated routing settings with compatibility-safe defaults."""

    aliases: dict[str, AliasRule] = field(default_factory=dict)
    fallback_enabled: bool = True
    max_attempts: int = 3
    explicit_model_fallback: bool = False
    scoring: ScoringConfig = field(default_factory=ScoringConfig)

    @classmethod
    def from_dict(cls, raw: dict | None) -> "RoutingConfig":
        raw = raw if isinstance(raw, dict) else {}
        aliases: dict[str, AliasRule] = {}
        aliases_raw = raw.get("aliases") or {}
        if not isinstance(aliases_raw, dict):
            aliases_raw = {}
        for name, value in aliases_raw.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(value, dict):
                continue
            candidates = tuple(
                item.strip()
                for item in (value.get("candidates") or [])
                if isinstance(item, str) and item.strip()
            )
            require = value.get("require") or {}
            aliases[name.strip()] = AliasRule(
                candidates=candidates,
                require_tool_calling=bool(
                    isinstance(require, dict) and require.get("tool_calling", False)
                ),
            )

        fallback = raw.get("fallback") or {}
        if not isinstance(fallback, dict):
            fallback = {}
        try:
            max_attempts = int(fallback.get("max_attempts", 3))
        except (TypeError, ValueError):
            max_attempts = 3
        max_attempts = min(10, max(1, max_attempts))
        return cls(
            aliases=aliases,
            fallback_enabled=bool(fallback.get("enabled", True)),
            max_attempts=max_attempts,
            explicit_model_fallback=bool(fallback.get("explicit_model", False)),
            scoring=ScoringConfig.from_dict(raw.get("scoring", {})),
        )


@dataclass(frozen=True)
class RouteTarget:
    provider_name: str
    provider: ProviderConfig
    model: ModelInfo

    @property
    def canonical_id(self) -> str:
        return f"{self.provider.model_prefix}/{self.model.id}"

    @property
    def upstream_model_id(self) -> str:
        return self.model.effective_upstream_id


@dataclass(frozen=True)
class RoutePlan:
    requested_model: str
    strategy: str
    candidates: tuple[RouteTarget, ...]
    rejected: tuple[dict[str, str], ...] = ()
    scores: tuple[CandidateScore, ...] = ()

    @property
    def selected(self) -> RouteTarget | None:
        return self.candidates[0] if self.candidates else None

    def explain(self) -> dict:
        return {
            "requested_model": self.requested_model,
            "strategy": self.strategy,
            "candidates": [target.canonical_id for target in self.candidates],
            "selected": self.selected.canonical_id if self.selected else None,
            "rejected": list(self.rejected),
            "scoring": {
                "enabled": bool(self.scores),
                "candidates": [score.to_dict() for score in self.scores],
            },
        }


class RoutePlanner:
    """Build deterministic plans from registry order and explicit rules."""

    def __init__(self, registry: Registry, config: RoutingConfig | dict | None = None):
        self.registry = registry
        self.config = config if isinstance(config, RoutingConfig) else RoutingConfig.from_dict(config)

    def virtual_model_ids(self) -> tuple[str, ...]:
        custom = tuple(name for name in self.config.aliases if name not in BUILTIN_ALIASES)
        return BUILTIN_ALIASES + custom

    def _targets(self) -> Iterable[RouteTarget]:
        for provider_name, provider in self.registry.providers.items():
            for model in provider.models:
                yield RouteTarget(provider_name, provider, model)

    @staticmethod
    def _matches(target: RouteTarget, model_id: str) -> bool:
        model = target.model
        provider = target.provider
        return model_id in {
            model.id,
            f"{provider.model_prefix}/{model.id}",
            model.effective_upstream_id,
            f"{provider.name}/{model.effective_upstream_id}",
            codex_model_alias(provider.model_prefix, model.id),
        }

    def resolve_explicit(self, model_id: str) -> RouteTarget | None:
        for target in self._targets():
            if self._matches(target, model_id):
                return target
        return None

    def plan(self, requested_model: str, signals: dict[str, dict] | None = None) -> RoutePlan:
        if requested_model not in self.virtual_model_ids():
            target = self.resolve_explicit(requested_model)
            return RoutePlan(
                requested_model=requested_model,
                strategy="explicit",
                candidates=(target,) if target else (),
            )

        rule = self.config.aliases.get(requested_model)
        require_tools = requested_model == "auto/coding"
        if rule and rule.candidates:
            require_tools = require_tools or rule.require_tool_calling
            source: list[RouteTarget] = []
            rejected: list[dict[str, str]] = []
            for reference in rule.candidates:
                target = self.resolve_explicit(reference)
                if target:
                    source.append(target)
                else:
                    rejected.append({"model": reference, "reason": "not_registered"})
        else:
            if rule:
                require_tools = require_tools or rule.require_tool_calling
            source = list(self._targets())
            rejected = []

        candidates: list[RouteTarget] = []
        seen: set[tuple[str, str]] = set()
        for target in source:
            if require_tools and not target.model.tool_calling:
                rejected.append({"model": target.canonical_id, "reason": "tool_calling_required"})
                continue
            if requested_model == "auto/free":
                evidence_status = target.provider.free_tier_for(target.model).status()
                if evidence_status != "verified":
                    rejected.append({
                        "model": target.canonical_id,
                        "reason": f"free_evidence_{evidence_status}",
                    })
                    continue
            identity = (target.provider_name, target.upstream_model_id)
            if identity in seen:
                continue
            seen.add(identity)
            candidates.append(target)

        scores: tuple[CandidateScore, ...] = ()
        if self.config.scoring.enabled:
            scored = []
            for order, target in enumerate(candidates):
                target_signals = dict((signals or {}).get(target.provider_name, {}))
                target_signals.update((signals or {}).get(target.canonical_id, {}))
                score = score_candidate(
                    target, requested_model, self.config.scoring, target_signals, require_tools
                )
                scored.append((score.total, order, target, score))
            scored.sort(key=lambda row: (-row[0], row[1]))
            candidates = [row[2] for row in scored]
            score_by_model = {row[3].model: row[3] for row in scored}
            scores = tuple(score_by_model[target.canonical_id]
                           for target in candidates[:self.config.max_attempts])

        return RoutePlan(
            requested_model=requested_model,
            strategy="scored" if self.config.scoring.enabled else "priority",
            candidates=tuple(candidates[:self.config.max_attempts]),
            rejected=tuple(rejected),
            scores=scores,
        )


def diagnose_routing_config(raw: object, registry: Registry) -> list[RoutingDiagnostic]:
    """Return actionable diagnostics for values the compatibility parser normalizes."""
    issues: list[RoutingDiagnostic] = []

    def add(severity: str, code: str, path: str, message: str, fix: str = ""):
        issues.append(RoutingDiagnostic(severity, code, path, message, fix))

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        add(
            "error", "routing_not_mapping", "routing",
            "routing 必须是 YAML 对象，当前配置会被忽略。",
            "将 routing 改为对象；运行 `open-free-router route explain auto` 复查",
        )
        return issues

    fallback = raw.get("fallback", {})
    if not isinstance(fallback, dict):
        add("error", "fallback_not_mapping", "routing.fallback", "fallback 必须是 YAML 对象。")
    else:
        attempts = fallback.get("max_attempts", 3)
        parsed_attempts = attempts if type(attempts) is int else 0
        if not 1 <= parsed_attempts <= 10:
            add(
                "error", "invalid_max_attempts", "routing.fallback.max_attempts",
                "max_attempts 必须是 1 到 10 的整数；当前运行时会回退到安全范围。",
                "把 routing.fallback.max_attempts 设置为 3",
            )
        for key in ("enabled", "explicit_model"):
            if key in fallback and not isinstance(fallback[key], bool):
                add(
                    "error", "invalid_boolean", f"routing.fallback.{key}",
                    f"{key} 必须写为 true 或 false，不能使用字符串。",
                    f"把 routing.fallback.{key} 改为 true 或 false",
                )
        if fallback.get("explicit_model") is True:
            add(
                "warning", "explicit_fallback_enabled", "routing.fallback.explicit_model",
                "显式模型允许静默换模，响应能力和数据处理方可能发生变化。",
                "如不需要显式换模，将 routing.fallback.explicit_model 设置为 false",
            )

    scoring = raw.get("scoring", {})
    if not isinstance(scoring, dict):
        add("error", "scoring_not_mapping", "routing.scoring", "scoring 必须是 YAML 对象。")
    else:
        if "enabled" in scoring and not isinstance(scoring["enabled"], bool):
            add(
                "error", "invalid_boolean", "routing.scoring.enabled",
                "enabled 必须写为 true 或 false；无效值会保持关闭。",
                "把 routing.scoring.enabled 设置为 true 或 false",
            )
        weights = scoring.get("weights", {})
        if not isinstance(weights, dict):
            add("error", "weights_not_mapping", "routing.scoring.weights", "weights 必须是 YAML 对象。")
        else:
            defaults = ScoringConfig().weights
            total = sum(value for name, value in defaults.items() if name not in weights)
            for name, value in weights.items():
                path = f"routing.scoring.weights.{name}"
                if name not in defaults:
                    add("warning", "unknown_score_factor", path, f"未知评分因子 {name} 会被忽略。")
                    continue
                try:
                    parsed = float(value)
                except (TypeError, ValueError, OverflowError):
                    parsed = -1.0
                if not math.isfinite(parsed) or parsed < 0:
                    add("error", "invalid_score_weight", path, "评分权重必须是非负有限数字。")
                else:
                    total += parsed
            if weights and total <= 0:
                add(
                    "error", "zero_score_weights", "routing.scoring.weights",
                    "已配置评分权重的总和必须大于 0。",
                )
        if "missing_default" in scoring:
            try:
                missing = float(scoring["missing_default"])
            except (TypeError, ValueError, OverflowError):
                missing = -1.0
            if not math.isfinite(missing) or not 0 <= missing <= 1:
                add(
                    "error", "invalid_missing_default", "routing.scoring.missing_default",
                    "缺失值默认分必须在 0 到 1 之间。",
                )
        try:
            good = float(scoring.get("latency_good_ms", 500))
            bad = float(scoring.get("latency_bad_ms", 10000))
        except (TypeError, ValueError, OverflowError):
            good, bad = -1.0, -1.0
        if not all(math.isfinite(value) for value in (good, bad)) or good < 0 or bad <= good:
            add(
                "error", "invalid_latency_bounds", "routing.scoring",
                "latency_good_ms 必须非负，latency_bad_ms 必须更大。",
            )

    aliases = raw.get("aliases", {})
    if not isinstance(aliases, dict):
        add(
            "error", "aliases_not_mapping", "routing.aliases",
            "aliases 必须是按别名命名的 YAML 对象，当前内容会被忽略。",
            "运行 `open-free-router models` 查看可用模型 ID",
        )
        return issues

    planner = RoutePlanner(registry, raw)
    for name, value in aliases.items():
        path = f"routing.aliases.{name}"
        if not isinstance(name, str) or not name.strip():
            add("error", "invalid_alias_name", path, "路由别名不能为空。")
            continue
        if not isinstance(value, dict):
            add("error", "alias_not_mapping", path, "别名规则必须是 YAML 对象。")
            continue
        candidates = value.get("candidates", [])
        if candidates is not None and not isinstance(candidates, list):
            add(
                "error", "candidates_not_list", f"{path}.candidates",
                "candidates 必须是模型 ID 数组。", "运行 `open-free-router models` 查看有效 ID",
            )
            continue
        candidates = candidates or []
        seen: set[str] = set()
        for index, candidate in enumerate(candidates):
            candidate_path = f"{path}.candidates[{index}]"
            if not isinstance(candidate, str) or not candidate.strip():
                add("error", "invalid_candidate", candidate_path, "候选模型 ID 必须是非空字符串。")
                continue
            candidate = candidate.strip()
            if candidate in seen:
                add("warning", "duplicate_candidate", candidate_path, f"候选 {candidate} 重复，将被去重。")
            seen.add(candidate)
            if candidate == name or candidate in aliases:
                add(
                    "error", "alias_candidate_reference", candidate_path,
                    "候选必须引用真实注册表模型，不能引用路由别名。",
                    "运行 `open-free-router models` 查看有效 ID",
                )
            elif planner.resolve_explicit(candidate) is None:
                add(
                    "error", "candidate_not_registered", candidate_path,
                    f"候选 {candidate} 未在 registry.yaml 中注册。",
                    "运行 `open-free-router models` 查看有效 ID",
                )
        require = value.get("require", {})
        if require is not None and not isinstance(require, dict):
            add("error", "require_not_mapping", f"{path}.require", "require 必须是 YAML 对象。")
        elif isinstance(require, dict) and "tool_calling" in require and not isinstance(require["tool_calling"], bool):
            add(
                "error", "invalid_boolean", f"{path}.require.tool_calling",
                "tool_calling 必须写为 true 或 false。",
            )

    for alias in planner.virtual_model_ids():
        plan = planner.plan(alias)
        if not plan.candidates:
            severity = "error" if alias in aliases else "warning"
            add(
                severity, "no_eligible_candidates", f"routing.aliases.{alias}",
                f"路由 {alias} 没有可用候选。",
                f"运行 `open-free-router route explain {alias}` 查看过滤原因",
            )
            continue
        missing = sorted({
            target.provider_name for target in plan.candidates
            if target.provider.auth_mode != "none" and not target.provider.effective_key
        })
        ready = [
            target for target in plan.candidates
            if target.provider.auth_mode == "none" or target.provider.effective_key
        ]
        if missing:
            severity = "warning" if ready or alias not in aliases else "error"
            code = "candidates_missing_credentials" if ready else "no_credential_candidates"
            add(
                severity, code, f"routing.aliases.{alias}",
                f"路由候选包含未配置凭据的提供商：{', '.join(missing)}。",
                "运行 `open-free-router setup` 配置凭据",
            )
    return issues
