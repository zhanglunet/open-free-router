"""Normalize common upstream rate-limit headers without retaining raw values."""
from __future__ import annotations

import email.utils
import math
import re
import time
from dataclasses import asdict, dataclass
from datetime import timezone


@dataclass(frozen=True)
class RateLimitDimension:
    limit: float | None = None
    remaining: float | None = None
    reset_at: float = 0.0


@dataclass(frozen=True)
class QuotaObservation:
    classification: str = "unknown"
    observed_at: float = 0.0
    retry_at: float = 0.0
    requests: RateLimitDimension = RateLimitDimension()
    tokens: RateLimitDimension = RateLimitDimension()
    has_headers: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def next_reset_at(self) -> float:
        candidates = (
            self.retry_at, self.requests.reset_at, self.tokens.reset_at,
        )
        future = [value for value in candidates if value > self.observed_at]
        return min(future) if future else 0.0


def _headers(headers) -> dict[str, str]:
    try:
        items = headers.items()
    except AttributeError:
        items = headers or []
    result = {}
    for name, value in items:
        key = str(name).strip().lower()
        if key and key not in result:
            result[key] = str(value).strip()[:256]
    return result


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)", value)
    if not match:
        return None
    number = float(match.group(1))
    return number if math.isfinite(number) else None


def _first_number(values: dict[str, str], names: tuple[str, ...]) -> float | None:
    for name in names:
        parsed = _number(values.get(name))
        if parsed is not None:
            return parsed
    return None


def _reset_at(value: str | None, now: float) -> float:
    if not value:
        return 0.0
    stripped = value.strip().lower()
    duration = re.match(r"^([0-9]+(?:\.[0-9]+)?)(ms|s|m|h|d|w)?$", stripped)
    if duration:
        number = float(duration.group(1))
        unit = duration.group(2) or ""
        if not math.isfinite(number):
            return 0.0
        if unit == "ms":
            return now + min(number / 1000, 86400 * 366)
        if unit == "m":
            return now + min(number * 60, 86400 * 366)
        if unit == "h":
            return now + min(number * 3600, 86400 * 366)
        if unit == "d":
            return now + min(number * 86400, 86400 * 366)
        if unit == "w":
            return now + min(number * 604800, 86400 * 366)
        if unit == "s" or number < 1_000_000_000:
            return now + min(number, 86400 * 366)
        return min(number, now + 86400 * 366)
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return min(max(0.0, parsed.timestamp()), now + 86400 * 366)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _first_reset(values: dict[str, str], names: tuple[str, ...], now: float) -> float:
    for name in names:
        parsed = _reset_at(values.get(name), now)
        if parsed:
            return parsed
    return 0.0


def parse_quota_headers(
    headers,
    status: int,
    classification: str = "unknown",
    now: float | None = None,
) -> QuotaObservation:
    """Parse only documented numeric/reset fields; never infer remaining quota from text."""
    now = time.time() if now is None else now
    values = _headers(headers)
    request_limit = _first_number(values, (
        "ratelimit-limit", "x-ratelimit-limit-requests", "x-ratelimit-limit",
    ))
    request_remaining = _first_number(values, (
        "ratelimit-remaining", "x-ratelimit-remaining-requests", "x-ratelimit-remaining",
    ))
    request_reset = _first_reset(values, (
        "ratelimit-reset", "x-ratelimit-reset-requests", "x-ratelimit-reset",
    ), now)
    token_limit = _first_number(values, ("x-ratelimit-limit-tokens",))
    token_remaining = _first_number(values, ("x-ratelimit-remaining-tokens",))
    token_reset = _first_reset(values, ("x-ratelimit-reset-tokens",), now)
    retry_at = _reset_at(values.get("retry-after"), now)
    recognized = any(value is not None for value in (
        request_limit, request_remaining, token_limit, token_remaining,
    )) or bool(request_reset or token_reset or retry_at)
    if status == 402:
        normalized = "credits_exhausted"
    elif classification in {
        "rate_limited", "quota_exhausted", "credits_exhausted", "credential_invalid",
    }:
        normalized = classification
    elif 200 <= status < 300 and recognized:
        normalized = "available"
    else:
        normalized = "unknown"
    return QuotaObservation(
        classification=normalized,
        observed_at=now,
        retry_at=retry_at,
        requests=RateLimitDimension(request_limit, request_remaining, request_reset),
        tokens=RateLimitDimension(token_limit, token_remaining, token_reset),
        has_headers=recognized,
    )
