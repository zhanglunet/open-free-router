"""Explainable, bounded candidate scoring with explicit missing-value policy."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field


DEFAULT_WEIGHTS = {
    "health": 0.25,
    "success_rate": 0.20,
    "latency": 0.20,
    "quota": 0.15,
    "capability": 0.10,
    "free_evidence": 0.10,
}


def _bounded(value, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number):
        return default
    return min(1.0, max(0.0, number))


@dataclass(frozen=True)
class ScoringConfig:
    enabled: bool = False
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    missing_default: float = 0.5
    latency_good_ms: float = 500.0
    latency_bad_ms: float = 10000.0

    @classmethod
    def from_dict(cls, raw: object) -> "ScoringConfig":
        raw = raw if isinstance(raw, dict) else {}
        missing = _bounded(raw.get("missing_default", 0.5))
        weights_raw = raw.get("weights", {})
        weights = {}
        for name, default in DEFAULT_WEIGHTS.items():
            value = weights_raw.get(name, default) if isinstance(weights_raw, dict) else default
            try:
                parsed = float(value)
            except (TypeError, ValueError, OverflowError):
                parsed = default
            weights[name] = parsed if math.isfinite(parsed) and parsed >= 0 else default
        total = sum(weights.values())
        weights = ({name: value / total for name, value in weights.items()}
                   if total > 0 else dict(DEFAULT_WEIGHTS))
        try:
            good = max(0.0, float(raw.get("latency_good_ms", 500)))
            bad = float(raw.get("latency_bad_ms", 10000))
        except (TypeError, ValueError, OverflowError):
            good, bad = 500.0, 10000.0
        if not math.isfinite(good) or not math.isfinite(bad) or bad <= good:
            good, bad = 500.0, 10000.0
        return cls(raw.get("enabled") is True, weights, missing, good, bad)


@dataclass(frozen=True)
class FactorScore:
    value: float
    weight: float
    contribution: float
    source: str


@dataclass(frozen=True)
class CandidateScore:
    model: str
    total: float
    factors: dict[str, FactorScore]

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "total": self.total,
            "factors": {name: asdict(value) for name, value in self.factors.items()},
        }


def _latency_score(signals: dict, config: ScoringConfig) -> tuple[float, str]:
    observed = []
    labels = []
    for key in ("ttfb_p95_ms", "total_p95_ms"):
        value = signals.get(key)
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(value) or value < 0:
            continue
        normalized = 1.0 - (value - config.latency_good_ms) / (
            config.latency_bad_ms - config.latency_good_ms
        )
        observed.append(_bounded(normalized))
        labels.append(key)
    if not observed:
        return config.missing_default, "missing_default"
    return sum(observed) / len(observed), "+".join(labels)


def score_candidate(target, requested_model: str, config: ScoringConfig,
                    signals: dict | None = None, require_tools: bool = False) -> CandidateScore:
    """Score a route target without network I/O or hidden non-determinism."""
    signals = signals if isinstance(signals, dict) else {}
    values: dict[str, tuple[float, str]] = {}
    for name in ("health", "success_rate", "quota"):
        if signals.get(name) is None:
            values[name] = (config.missing_default, "missing_default")
        else:
            values[name] = (_bounded(signals[name], config.missing_default), "runtime")
    values["latency"] = _latency_score(signals, config)
    capability = 1.0 if not require_tools or target.model.tool_calling else 0.0
    values["capability"] = (capability, "registry")
    evidence = target.provider.free_tier_for(target.model).status()
    values["free_evidence"] = (
        1.0 if evidence == "verified" else (0.0 if evidence in {"expired", "invalid"}
                                             else config.missing_default),
        f"evidence_{evidence}",
    )
    factors = {}
    for name, (value, source) in values.items():
        weight = config.weights[name]
        factors[name] = FactorScore(
            round(value, 6), round(weight, 6), round(value * weight, 6), source
        )
    return CandidateScore(
        target.canonical_id,
        round(sum(factor.contribution for factor in factors.values()), 6),
        factors,
    )
