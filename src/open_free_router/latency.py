"""Latency dimension for PRD-059/T3: realtime TTFB sliding windows.

Implements ``docs/plans/failover-routing-policy.md`` §4 (latency dimension):

- :class:`LatencyStore` — in-memory per-(provider, model) sliding window of
  TTFB samples (default last 100, tunable, switchable); computes p50/p95.
  Only attempts that actually received a first byte are recorded: a failed
  connection has no "first byte", and the resilience manager already owns
  failure punishment (§1.1–§1.2) — recording fast failures as "fast" would
  reward dead providers with good latency scores.
- :func:`latency_factor` — p50 (ms) → [0, 1] reusing the existing gauge
  ``latency_good_ms`` / ``latency_bad_ms`` (``ScoringConfig``); lower latency
  scores higher.  No data scores ``missing_default`` without punishment (§4.3).
- :func:`latency_dimension` — planner hook: realtime p50 when sampling is
  enabled and data exists, otherwise falls back to the probe-snapshot
  latency factor (T1 §4.2: "默认关闭实时采样时退化为探测快照延迟").

Everything here is read-only with respect to business state: the store is a
pure runtime heuristic, never touches disk, and is rebuilt empty on every
process start.
"""
from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class LatencyConfig:
    """T1 §4.1 sampling policy (all tunable via routing.policy).

    ``good_ms`` / ``bad_ms`` / ``missing_default`` are taken from
    ``ScoringConfig`` so the gauge stays identical to the existing latency
    factor — T1 §4.1: "口径 latency_good_ms=500 / latency_bad_ms=10000 沿用现有".
    """

    enabled: bool = True
    window_size: int = 100
    good_ms: float = 500.0
    bad_ms: float = 10000.0
    missing_default: float = 0.5

    @classmethod
    def from_dict(cls, raw: object) -> "LatencyConfig":
        raw = raw if isinstance(raw, dict) else {}

        def _num(key: str, default: float) -> float:
            try:
                value = float(raw.get(key, default))  # type: ignore[union-attr]
            except (TypeError, ValueError, OverflowError):
                return default
            return value if math.isfinite(value) and value >= 0 else default

        enabled = raw.get("latency_enabled", True)
        if not isinstance(enabled, bool):
            enabled = True
        try:
            window = int(raw.get("latency_window_size", 100))  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            window = 100
        window = min(10000, max(1, window))
        return cls(
            enabled=enabled,
            window_size=window,
            good_ms=_num("latency_good_ms", 500.0),
            bad_ms=_num("latency_bad_ms", 10000.0),
            missing_default=_num("missing_default", 0.5),
        )

    @classmethod
    def from_routing(cls, routing_config) -> "LatencyConfig":
        """Build from a validated ``RoutingConfig`` (gauge from its scoring)."""
        scoring = routing_config.scoring
        return cls(
            enabled=bool(routing_config.latency_enabled),
            window_size=max(1, int(routing_config.latency_window_size)),
            good_ms=scoring.latency_good_ms,
            bad_ms=scoring.latency_bad_ms,
            missing_default=scoring.missing_default,
        )


class LatencyStore:
    """In-memory sliding-window TTFB samples, keyed by (provider, model).

    Thread-safe for the proxy's threaded request handling; a slightly stale
    percentile snapshot is acceptable for a routing heuristic.
    """

    def __init__(self, window_size: int = 100):
        self.window_size = max(1, int(window_size))
        self._samples: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def record(self, provider_name: str, upstream_model_id: str, ttfb_ms: float) -> None:
        """Append one first-byte observation; junk values are ignored."""
        try:
            value = float(ttfb_ms)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            return
        if not math.isfinite(value) or value < 0:
            return
        key = (str(provider_name), str(upstream_model_id))
        with self._lock:
            samples = self._samples.get(key)
            if samples is None:
                samples = self._samples[key] = deque(maxlen=self.window_size)
            samples.append(value)

    def _values(self, provider_name: str, upstream_model_id: str | None) -> list[float]:
        """Exact (provider, model) samples; provider-wide aggregate as fallback."""
        with self._lock:
            if upstream_model_id is not None:
                values = list(self._samples.get((provider_name, upstream_model_id), ()))
            else:
                values = []
            if not values:
                values = [
                    value
                    for (name, _), samples in self._samples.items()
                    if name == provider_name
                    for value in samples
                ]
            return values

    def percentile(
        self, provider_name: str, upstream_model_id: str | None = None, q: float = 50.0
    ) -> float | None:
        """Nearest-rank percentile over the sliding window; None when empty."""
        values = self._values(provider_name, upstream_model_id)
        if not values:
            return None
        ordered = sorted(values)
        if q <= 0:
            return ordered[0]
        if q >= 100:
            return ordered[-1]
        rank = math.ceil(q / 100.0 * len(ordered))
        return ordered[max(0, min(len(ordered) - 1, rank - 1))]

    def p50(self, provider_name: str, upstream_model_id: str | None = None) -> float | None:
        return self.percentile(provider_name, upstream_model_id, 50.0)

    def p95(self, provider_name: str, upstream_model_id: str | None = None) -> float | None:
        return self.percentile(provider_name, upstream_model_id, 95.0)

    def snapshot(self) -> dict[tuple[str, str], dict[str, float | int]]:
        """Per-key {count, p50_ms, p95_ms} for dashboards / T5 explainability."""
        with self._lock:
            keys = list(self._samples.keys())
        report: dict[tuple[str, str], dict[str, float | int]] = {}
        for key in keys:
            provider_name, upstream_model_id = key
            values = self._values(provider_name, upstream_model_id)
            if not values:
                continue
            ordered = sorted(values)
            report[key] = {
                "count": len(ordered),
                "p50_ms": round(self.percentile(provider_name, upstream_model_id, 50.0) or 0.0, 3),
                "p95_ms": round(self.percentile(provider_name, upstream_model_id, 95.0) or 0.0, 3),
            }
        return report


def latency_factor(p50_ms: object, config: LatencyConfig) -> tuple[float, str]:
    """Realtime latency dimension value in [0, 1]; lower latency scores higher.

    ``p50_ms`` is the sliding-window p50 for the candidate, or None when no
    realtime data exists.  T1 §4.3: no data scores ``missing_default`` without
    punishment.  The gauge mirrors ``scoring._latency_score`` (good → 1.0,
    bad → 0.0, linear between).
    """
    try:
        observed = float(p50_ms)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return config.missing_default, "missing_default"
    if not math.isfinite(observed) or observed < 0:
        return config.missing_default, "missing_default"
    span = config.bad_ms - config.good_ms
    if span <= 0:
        return config.missing_default, "missing_default"
    normalized = 1.0 - (observed - config.good_ms) / span
    return round(min(1.0, max(0.0, normalized)), 6), "latency_realtime_p50"


def latency_dimension(
    target,
    store: LatencyStore | None,
    config: LatencyConfig,
    fallback_value: float,
    fallback_source: str,
) -> tuple[float, str]:
    """Latency dimension for the planner (T1 §4.1–§4.2).

    Realtime sliding-window p50 when sampling is enabled and the store has
    data for this provider/model; otherwise the caller-supplied fallback
    (the probe-snapshot latency factor from ``score_candidate``).
    """
    if store is not None and config.enabled:
        p50 = store.p50(target.provider_name, target.upstream_model_id)
        if p50 is not None:
            return latency_factor(p50, config)
    return fallback_value, fallback_source
