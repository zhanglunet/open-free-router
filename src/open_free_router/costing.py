"""Cost dimension for PRD-059/T2: quota-aware routing without new state.

Implements ``docs/plans/failover-routing-policy.md`` §4 (cost dimension):

- :func:`cost_factor` — remaining/limit ratio with the §4.4 downgrade rule.
- :func:`excluded_by_cost` — candidate exclusion through
  ``ResilienceManager.can_attempt`` (§4.4); no new classification logic.
- :func:`load_cost_table` — documented per-provider free-tier reference
  (``docs/provider-profiles.json`` ``free_tier``).  Purely documentary at
  runtime: live quota signals always take precedence (§4.1), and unknown
  numbers are never invented (§4.3, "no evidence, no conviction").

Everything here is read-only: it never writes business state.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path


def _bounded_ratio(value: object, default: float) -> float | None:
    """Return value clamped to [0, 1], or None when it is not a real number."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return min(1.0, max(0.0, number))


@dataclass(frozen=True)
class CostConfig:
    """T1 §4.4 downgrade policy values (all tunable via routing.policy)."""

    downgrade_threshold: float = 0.10
    downgrade_factor: float = 0.5
    missing_default: float = 0.5

    @classmethod
    def from_dict(cls, raw: object) -> "CostConfig":
        raw = raw if isinstance(raw, dict) else {}

        def _num(key: str, default: float) -> float:
            try:
                value = float(raw.get(key, default))  # type: ignore[union-attr]
            except (TypeError, ValueError, OverflowError):
                return default
            return value if math.isfinite(value) and 0.0 <= value <= 1.0 else default

        return cls(
            downgrade_threshold=_num("cost_downgrade_threshold", 0.10),
            downgrade_factor=_num("cost_downgrade_factor", 0.5),
            missing_default=_num("missing_default", 0.5),
        )


def cost_factor(quota_ratio: object, config: CostConfig) -> tuple[float, str]:
    """Cost dimension value in [0, 1]; higher means more quota headroom.

    ``quota_ratio`` is remaining/limit from live quota signals
    (``ResilienceManager.scoring_signals``), or None when no live data exists.
    T1 §4.4: ratio below threshold is downgraded, never cut off; no data
    scores ``missing_default`` without punishment.
    """
    ratio = _bounded_ratio(quota_ratio, config.missing_default)
    if ratio is None:
        return config.missing_default, "missing_default"
    if ratio < config.downgrade_threshold:
        return round(ratio * config.downgrade_factor, 6), "quota_runtime_downgraded"
    return round(ratio, 6), "quota_runtime"


def credential_slots(provider) -> list[int]:
    """Enumerate configured credential slots without exposing key material.

    Mirrors the slot logic in ``executor._credentials`` so the planner and the
    executor agree on which slots exist.  Auth-less providers get slot 0;
    providers with several api_keys get one slot per non-empty key.
    """
    if provider.auth_mode == "none":
        return [0]
    api_keys = getattr(provider, "api_keys", None) or []
    slots = [index for index, value in enumerate(api_keys) if value]
    if slots:
        return slots
    return [0] if provider.effective_key else []


def excluded_by_cost(target, resilience, now: float | None = None) -> tuple[bool, str]:
    """True when every credential slot is blocked in the resilience manager.

    T1 §4.4: ``credits_exhausted`` (terminal) and 429-cooldown candidates are
    excluded.  Both already surface through ``can_attempt`` as not-allowed, so
    this reuses that verdict instead of inventing new classification logic.
    Providers with no configured credentials are left alone here — the
    executor already rejects those as ``credential_missing``.
    """
    slots = credential_slots(target.provider)
    if not slots:
        return False, ""
    reasons: list[str] = []
    for slot in slots:
        allowed, reason = resilience.can_attempt(
            target.provider_name, target.upstream_model_id, slot, now
        )
        if allowed:
            return False, ""
        reasons.append(reason)
    return True, reasons[0] if reasons else "unknown"


def load_cost_table(path: str | Path | None = None) -> dict:
    """Load the documented per-provider cost table.

    Defaults to ``docs/provider-profiles.json`` next to the package tree and
    returns ``{}`` when the file is absent or unreadable — it is a docs
    artifact, so routing must never depend on it being present.
    """
    if path is None:
        path = Path(__file__).resolve().parents[2] / "docs" / "provider-profiles.json"
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    table = {}
    for name, entry in data.items():
        if not isinstance(entry, dict):
            continue
        free_tier = entry.get("free_tier")
        if isinstance(free_tier, dict):
            table[name] = free_tier
    return table
