"""Deterministic route planning for explicit and virtual model IDs.

This module deliberately contains no network I/O.  It turns a requested model
into an ordered, bounded candidate list that the proxy executor can consume.
Keeping planning separate makes route decisions explainable and lets every wire
protocol share the same behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from open_free_router.registry import ModelInfo, ProviderConfig, Registry, codex_model_alias


BUILTIN_ALIASES = ("auto", "auto/coding", "auto/fast", "auto/free")


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

    def plan(self, requested_model: str) -> RoutePlan:
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
            identity = (target.provider_name, target.upstream_model_id)
            if identity in seen:
                continue
            seen.add(identity)
            candidates.append(target)
            if len(candidates) >= self.config.max_attempts:
                break

        return RoutePlan(
            requested_model=requested_model,
            strategy="priority",
            candidates=tuple(candidates),
            rejected=tuple(rejected),
        )
