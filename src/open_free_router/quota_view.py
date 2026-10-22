"""Read-only quota presentation; never infer account balances from local usage."""
from __future__ import annotations

import time

from open_free_router.resilience import QUOTA_SIGNAL_MAX_AGE_SECONDS, _safe_float


def quota_view(snapshot: dict, providers=(), now: float | None = None) -> dict:
    now = time.time() if now is None else now
    rows = []
    seen = set()
    for name, state in snapshot.get("credentials", {}).items():
        provider, _, slot = name.rpartition(":slot-")
        seen.add(provider)
        quota = state.get("quota") or {}
        observed = _safe_float(quota.get("observed_at"))
        measured = bool(quota.get("has_headers"))
        event = quota.get("classification") in {"rate_limited", "quota_exhausted", "credits_exhausted", "credential_invalid"}
        observed_evidence = measured or event
        fresh = observed_evidence and 0 < observed <= now and now - observed <= QUOTA_SIGNAL_MAX_AGE_SECONDS
        ratios = []
        dimensions = {}
        resets = []
        for dimension in ("requests", "tokens"):
            raw = quota.get(dimension) or {}
            limit = _safe_float(raw.get("limit"), -1)
            remaining = _safe_float(raw.get("remaining"), -1)
            reset = _safe_float(raw.get("reset_at"))
            dimensions[dimension] = {
                "limit": limit if limit >= 0 else None,
                "used": None,  # Upstream does not report used; do not fabricate subtraction.
                "remaining": remaining if remaining >= 0 else None,
                "reset_at": reset if reset > 0 else None,
            }
            if limit > 0 and remaining >= 0:
                ratios.append(min(1, remaining / limit))
            if reset > 0:
                resets.append(reset)
        ratio = min(ratios) if ratios else None
        band = "unknown"
        if observed_evidence and not fresh:
            band = "stale"
        elif fresh and ratio is not None:
            band = "exhausted" if ratio == 0 else ("tight" if ratio < 0.10 else "sufficient")
        elif fresh and quota.get("classification") == "credits_exhausted":
            # Structural 402 evidence; quota_exhausted may be only a wording
            # hint and cannot establish a numeric balance, even without headers.
            band = "exhausted"
        rows.append({"provider": provider, "slot": slot, "band": band,
                     "event_classification": quota.get("classification") if event else None,
                     "classification_source": ("failure_classification_hint" if quota.get("classification") == "quota_exhausted" else "failure_classification") if event else None,
                     "classification_conflict": bool(quota.get("classification") == "quota_exhausted" and ratio is not None and ratio > 0),
                     "source": "upstream_headers" if measured else ("event" if event else "never_measured"),
                     "observed_at": observed if observed > 0 else None,
                     "freshness": "fresh" if fresh else ("stale" if observed_evidence else "unknown"),
                     "valid_until": observed + QUOTA_SIGNAL_MAX_AGE_SECONDS if observed > 0 else None,
                     "reset_at": min(resets) if resets else None,
                     "state": state.get("state", "ready"), "dimensions": dimensions})
    for provider in sorted(set(providers) - seen):
        rows.append({"provider": provider, "slot": None, "band": "unknown",
                     "source": "never_measured", "observed_at": None,
                     "freshness": "unknown", "valid_until": None, "reset_at": None,
                     "state": "unknown", "dimensions": {}})
    return {"generated_at": now, "max_age_seconds": QUOTA_SIGNAL_MAX_AGE_SECONDS, "items": rows}


def public_quota_bands(view: dict) -> list[dict]:
    """Allowlist projection: public output never includes per-slot numbers/times."""
    grouped = {}
    for row in view["items"]:
        grouped.setdefault(row["provider"], []).append(row)
    result = []
    for provider, rows in sorted(grouped.items()):
        bands = {row["band"] for row in rows if row["state"] not in {"terminal", "cooldown"}}
        band = next((item for item in ("sufficient", "tight", "unknown", "stale", "exhausted") if item in bands), "unknown")
        result.append({"provider": provider, "band": band})
    return result
