"""PRD-059/T4: policy-driven failover configuration.

The strategy itself is fixed by docs/plans/failover-routing-policy.md (the
"constitution" for PRD-059). This module only turns its section 7 YAML shape
into validated runtime values:

- ``routing.policy.*``: retry/backoff/timeout knobs (parsed by RoutingConfig,
  defaults from T1 section 2).
- ``failover.*``: circuit-breaker thresholds (FailoverThresholds below,
  defaults from T1 section 3).

An out-of-range value falls back to the documented default and logs a
warning -- it is never silently clamped into a surprising policy.
"""
from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass

log = logging.getLogger(__name__)


class TimeoutPhase(Exception):
    """An upstream attempt timed out; carries which phase blew the budget."""

    def __init__(self, phase: str):
        super().__init__(f"upstream timeout during {phase}")
        self.phase = phase  # "connect" | "ttfb"


def bounded_number(value: object, default: float, low: float, high: float, name: str) -> float:
    if value is None:
        return default
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        log.warning("failover: %s 不是有效数字，回退默认值 %s", name, default)
        return default
    if not math.isfinite(number) or not low <= number <= high:
        log.warning(
            "failover: %s=%r 超出范围 [%s, %s]，回退默认值 %s",
            name, value, low, high, default,
        )
        return default
    return number


def bounded_int(value: object, default: int, low: int, high: int, name: str) -> int:
    if value is None:
        return default
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        log.warning("failover: %s 不是有效整数，回退默认值 %s", name, default)
        return default
    if not low <= number <= high:
        log.warning(
            "failover: %s=%r 超出范围 [%s, %s]，回退默认值 %s",
            name, value, low, high, default,
        )
        return default
    return number


def bounded_bool(value: object, default: bool, name: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    log.warning("failover: %s 必须写为 true 或 false，回退默认值 %s", name, default)
    return default


@dataclass(frozen=True)
class FailoverThresholds:
    """The ``failover:`` YAML section (T1 section 7) driving ResilienceManager.

    Every default below is the value fixed by T1 section 3; changing the
    strategy means revising the T1 document, not the code.
    """

    provider_threshold: int = 3
    provider_cooldown_seconds: float = 30.0
    credential_cooldown_seconds: float = 60.0
    quota_cooldown_scale: float = 8.0
    model_cooldown_seconds: float = 120.0
    credential_terminal_probe_seconds: float = 900.0
    credential_terminal_probe_max_scale: int = 8
    credential_forbidden_threshold: int = 3

    @classmethod
    def from_dict(cls, raw: object) -> "FailoverThresholds":
        raw = raw if isinstance(raw, dict) else {}
        return cls(
            provider_threshold=bounded_int(
                raw.get("provider_threshold"), 3, 1, 100, "failover.provider_threshold"),
            provider_cooldown_seconds=bounded_number(
                raw.get("provider_cooldown_seconds"), 30.0, 0, 3600,
                "failover.provider_cooldown_seconds"),
            credential_cooldown_seconds=bounded_number(
                raw.get("credential_cooldown_seconds"), 60.0, 0, 86400,
                "failover.credential_cooldown_seconds"),
            quota_cooldown_scale=bounded_number(
                raw.get("quota_cooldown_scale"), 8.0, 1, 100,
                "failover.quota_cooldown_scale"),
            model_cooldown_seconds=bounded_number(
                raw.get("model_cooldown_seconds"), 120.0, 0, 86400,
                "failover.model_cooldown_seconds"),
            credential_terminal_probe_seconds=bounded_number(
                raw.get("credential_terminal_probe_seconds"), 900.0, 0, 86400,
                "failover.credential_terminal_probe_seconds"),
            credential_terminal_probe_max_scale=bounded_int(
                raw.get("credential_terminal_probe_max_scale"), 8, 1, 32,
                "failover.credential_terminal_probe_max_scale"),
            credential_forbidden_threshold=bounded_int(
                raw.get("credential_forbidden_threshold"), 3, 1, 10,
                "failover.credential_forbidden_threshold"),
        )

    def to_resilience_kwargs(self) -> dict:
        """Map onto ResilienceManager.__init__ parameter names."""
        return {
            "provider_threshold": self.provider_threshold,
            "provider_cooldown": self.provider_cooldown_seconds,
            "credential_cooldown": self.credential_cooldown_seconds,
            "model_cooldown": self.model_cooldown_seconds,
            "credential_terminal_probe": self.credential_terminal_probe_seconds,
            "credential_terminal_probe_max_scale": self.credential_terminal_probe_max_scale,
            "credential_forbidden_threshold": self.credential_forbidden_threshold,
            "quota_cooldown_scale": self.quota_cooldown_scale,
        }


@dataclass(frozen=True)
class TimeoutPolicy:
    """The three timeouts plus the total failover budget (T1 section 2.3)."""

    connect: float = 10.0
    ttfb: float = 30.0
    request: float = 120.0
    total: float = 300.0


def compute_backoff(
    n: int,
    base_seconds: float,
    factor: float,
    cap_seconds: float,
    jitter: float,
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff with jitter between candidate switches (T1 section 2.2).

    ``n`` is the 1-based switch count: delay_n = min(cap, base * factor^(n-1)),
    actual wait = delay_n * (1 +/- jitter), jitter drawn uniformly.
    """
    n = max(1, int(n))
    delay = min(cap_seconds, base_seconds * (factor ** (n - 1)))
    if jitter > 0:
        source = rng if rng is not None else random
        delay *= 1 + (source.random() * 2 - 1) * jitter
    return max(0.0, delay)
