"""Structured, non-secret evidence for provider and model free-tier claims."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from urllib.parse import parse_qsl, urlsplit


FREE_TIER_TYPES = frozenset({
    "unlimited_claimed", "recurring_quota", "trial", "keyless", "unknown",
})


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class FreeTierEvidence:
    tier_type: str = "unknown"
    limit: float | None = None
    unit: str = ""
    reset_period: str = ""
    regions: tuple[str, ...] = ()
    evidence_url: str = ""
    verified_at: str = ""
    expires_at: str = ""
    terms_warning_zh: str = ""
    requires_payment_method: bool = False
    validation_errors: tuple[str, ...] = field(default=(), repr=False, compare=False)
    raw_payload: dict | None = field(default=None, repr=False, compare=False)

    @property
    def configured(self) -> bool:
        return any((
            self.tier_type != "unknown", self.limit is not None, self.unit,
            self.reset_period, self.regions, self.evidence_url, self.verified_at,
            self.expires_at, self.terms_warning_zh, self.requires_payment_method,
            self.validation_errors,
        ))

    @classmethod
    def from_dict(cls, raw: object) -> "FreeTierEvidence":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            return cls(validation_errors=("free_tier must be a mapping",))
        errors: list[str] = []
        tier_type = raw.get("type", "unknown")
        if not isinstance(tier_type, str) or tier_type not in FREE_TIER_TYPES:
            errors.append(f"unsupported type: {tier_type}")
            tier_type = "unknown"

        limit = raw.get("limit")
        if limit is not None:
            if (
                isinstance(limit, bool) or not isinstance(limit, (int, float))
                or not math.isfinite(limit) or limit < 0
            ):
                errors.append("limit must be a non-negative number")
                limit = None
            else:
                limit = float(limit)

        regions_raw = raw.get("regions", [])
        if not isinstance(regions_raw, list) or any(
            not isinstance(item, str) or not item.strip() for item in regions_raw
        ):
            errors.append("regions must be a list of non-empty strings")
            regions = ()
        else:
            regions = tuple(dict.fromkeys(item.strip() for item in regions_raw))

        evidence_url = raw.get("evidence_url", "")
        if not isinstance(evidence_url, str):
            errors.append("evidence_url must be a string")
            evidence_url = ""
        elif evidence_url:
            parts = urlsplit(evidence_url)
            sensitive_query = any(
                name.lower() in {"key", "api_key", "token", "access_token", "secret", "signature"}
                for name, _ in parse_qsl(parts.query, keep_blank_values=True)
            )
            if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
                errors.append("evidence_url must be public HTTPS")
            elif sensitive_query:
                errors.append("evidence_url must not contain credential query parameters")

        verified_at = raw.get("verified_at", "")
        expires_at = raw.get("expires_at", "")
        for name, value in (("verified_at", verified_at), ("expires_at", expires_at)):
            if not isinstance(value, str) or (value and _parse_time(value) is None):
                errors.append(f"{name} must be an ISO-8601 timestamp with timezone")
        verified_at = verified_at if isinstance(verified_at, str) else ""
        expires_at = expires_at if isinstance(expires_at, str) else ""
        verified = _parse_time(verified_at)
        expires = _parse_time(expires_at)
        if verified and expires and expires <= verified:
            errors.append("expires_at must be later than verified_at")

        payment = raw.get("requires_payment_method", False)
        if not isinstance(payment, bool):
            errors.append("requires_payment_method must be true or false")
            payment = False

        def text(name: str) -> str:
            value = raw.get(name, "")
            if not isinstance(value, str):
                errors.append(f"{name} must be a string")
                return ""
            return value.strip()

        return cls(
            tier_type=str(tier_type), limit=limit, unit=text("unit"),
            reset_period=text("reset_period"), regions=regions,
            evidence_url=evidence_url, verified_at=verified_at, expires_at=expires_at,
            terms_warning_zh=text("terms_warning_zh"),
            requires_payment_method=payment, validation_errors=tuple(errors),
            raw_payload=dict(raw),
        )

    def status(self, now: datetime | None = None) -> str:
        if self.validation_errors:
            return "invalid"
        if not self.configured or self.tier_type == "unknown":
            return "unknown"
        verified = _parse_time(self.verified_at)
        expires = _parse_time(self.expires_at)
        if not self.evidence_url or not verified or not expires:
            return "unverified"
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return "expired" if expires <= now.astimezone(timezone.utc) else "verified"

    def to_dict(
        self,
        include_status: bool = False,
        now: datetime | None = None,
        preserve_invalid: bool = False,
    ) -> dict:
        if preserve_invalid and self.validation_errors and self.raw_payload is not None:
            return dict(self.raw_payload)
        result: dict = {"type": self.tier_type}
        if self.limit is not None:
            number = float(self.limit)
            result["limit"] = int(number) if number.is_integer() else number
        for name in ("unit", "reset_period", "evidence_url", "verified_at", "expires_at", "terms_warning_zh"):
            value = getattr(self, name)
            if value:
                result[name] = value
        if self.regions:
            result["regions"] = list(self.regions)
        if self.requires_payment_method:
            result["requires_payment_method"] = True
        if include_status:
            result["status"] = self.status(now)
        return result


def summarize_evidence(items: list[FreeTierEvidence], now: datetime | None = None) -> dict[str, int]:
    summary = {name: 0 for name in ("verified", "expired", "unverified", "unknown", "invalid")}
    for item in items:
        summary[item.status(now)] += 1
    return summary
