"""Bounded, redacted runtime route-decision history."""
from __future__ import annotations

import threading
import time
import math
from collections import deque


class RouteDecisionStore:
    """Keep recent request routing facts without prompts, outputs or secrets."""

    def __init__(self, max_entries: int = 200, analytics=None):
        self.max_entries = max(1, int(max_entries))
        self.analytics = analytics
        self._items: deque[dict] = deque(maxlen=self.max_entries)
        self._lock = threading.RLock()

    def record(
        self,
        *,
        request_id: str,
        requested_model: str,
        strategy: str,
        status: str,
        attempts: int,
        rejected: tuple[dict[str, str], ...] | list[dict[str, str]],
        provider: str = "",
        model: str = "",
        status_code: int = 0,
        ttfb_ms: float | None = None,
        total_latency_ms: float | None = None,
        scores=(),
        attempt_results=(),
        error_kind: str = "",
    ) -> dict:
        def safe(value, limit=256):
            return str(value)[:limit]

        item = {
            "request_id": safe(request_id, 128),
            "timestamp": time.time(),
            "requested_model": safe(requested_model),
            "strategy": safe(strategy, 64),
            "status": safe(status, 32),
            "status_code": int(status_code),
            "provider": safe(provider),
            "model": safe(model),
            "attempts": max(0, int(attempts)),
            "fallback_attempts": max(0, int(attempts) - 1),
            "rejected": [
                {"model": safe(row.get("model", "")), "reason": safe(row.get("reason", ""), 96)}
                for row in rejected
            ],
            "ttfb_ms": self._safe_latency(ttfb_ms),
            "total_latency_ms": self._safe_latency(total_latency_ms),
            "scores": [self._safe_score(score) for score in scores],
            "attempt_results": [self._safe_attempt(row) for row in attempt_results],
            "input_tokens": None,
            "output_tokens": None,
            "error_kind": safe(error_kind, 64),
            "circuit_breaker_events": sum(
                row.get("reason") == "provider_circuit_open" for row in rejected
            ),
        }
        with self._lock:
            self._items.append(item)
        if item["status"] != "success":
            self._persist_analytics(item)
        return dict(item)

    def _persist_analytics(self, item: dict) -> None:
        if not self.analytics:
            return
        try:
            self.analytics.record(dict(item))
        except Exception:
            # Analytics is never allowed to break inference.
            pass

    @staticmethod
    def _safe_latency(value):
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return round(value, 3) if math.isfinite(value) and value >= 0 else None

    @staticmethod
    def _safe_score(score):
        raw = score.to_dict() if hasattr(score, "to_dict") else {}
        factors = {}
        for name in ("health", "success_rate", "latency", "quota", "capability", "free_evidence"):
            item = (raw.get("factors") or {}).get(name, {})
            factors[name] = {
                "value": RouteDecisionStore._safe_latency(item.get("value")),
                "weight": RouteDecisionStore._safe_latency(item.get("weight")),
                "contribution": RouteDecisionStore._safe_latency(item.get("contribution")),
                "source": str(item.get("source", ""))[:64],
            }
        return {
            "model": str(raw.get("model", ""))[:256],
            "total": RouteDecisionStore._safe_latency(raw.get("total")),
            "factors": factors,
        }

    @staticmethod
    def _safe_attempt(row):
        return {
            "provider": str(row.get("provider", ""))[:256],
            "model": str(row.get("model", ""))[:256],
            "status": "success" if row.get("status") == "success" else "failed",
            "status_code": int(row.get("status_code", 0)),
            "ttfb_ms": RouteDecisionStore._safe_latency(row.get("ttfb_ms")),
            "total_latency_ms": RouteDecisionStore._safe_latency(row.get("total_latency_ms")),
        }

    def complete(self, request_id: str, total_latency_ms: float) -> None:
        """Attach total latency after a buffered or streaming response closes."""
        value = self._safe_latency(total_latency_ms)
        if value is None:
            return
        with self._lock:
            for item in reversed(self._items):
                if item["request_id"] == request_id:
                    item["total_latency_ms"] = value
                    for attempt in reversed(item.get("attempt_results", [])):
                        if attempt.get("status") == "success":
                            attempt["total_latency_ms"] = value
                            break
                    self._persist_analytics(item)
                    return

    def set_usage(self, request_id: str, usage: dict | None) -> None:
        """Attach normalized token counts; ignore all other upstream usage fields."""
        usage = usage if isinstance(usage, dict) else {}
        input_value = usage.get("prompt_tokens", usage.get("input_tokens"))
        output_value = usage.get("completion_tokens", usage.get("output_tokens"))

        def safe_token(value):
            try:
                return max(0, int(value)) if value is not None else None
            except (TypeError, ValueError, OverflowError):
                return None

        with self._lock:
            for item in reversed(self._items):
                if item["request_id"] == request_id:
                    item["input_tokens"] = safe_token(input_value)
                    item["output_tokens"] = safe_token(output_value)
                    return

    def mark_failure(self, request_id: str, status_code: int, error_kind: str) -> None:
        """Correct an accepted route when its upstream payload is unusable."""
        with self._lock:
            for item in reversed(self._items):
                if item["request_id"] == request_id:
                    item["status"] = "failed"
                    item["status_code"] = int(status_code)
                    item["error_kind"] = str(error_kind)[:64]
                    for attempt in reversed(item.get("attempt_results", [])):
                        if attempt.get("status") == "success":
                            attempt["status"] = "failed"
                            attempt["status_code"] = int(status_code)
                            break
                    return

    @staticmethod
    def _p95(values: list[float]) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]

    def scoring_signals(self, limit: int = 200) -> dict[str, dict[str, float]]:
        """Aggregate bounded recent outcomes into model-level scoring inputs."""
        rows = self.recent(min(limit, self.max_entries))
        groups: dict[str, list[dict]] = {}
        for row in rows:
            attempts = row.get("attempt_results") or []
            if not attempts and row.get("model"):
                attempts = [row]
            for attempt in attempts:
                model = attempt.get("model")
                if model:
                    groups.setdefault(model, []).append(attempt)
        result = {}
        for model, items in groups.items():
            signals = {
                "success_rate": sum(item.get("status") == "success" for item in items) / len(items)
            }
            ttfb = [item["ttfb_ms"] for item in items if item.get("ttfb_ms") is not None]
            total = [item["total_latency_ms"] for item in items
                     if item.get("total_latency_ms") is not None]
            if ttfb:
                signals["ttfb_p95_ms"] = self._p95(ttfb)
            if total:
                signals["total_p95_ms"] = self._p95(total)
            result[model] = signals
        return result

    def recent(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), self.max_entries))
        with self._lock:
            return [dict(item) for item in list(self._items)[-limit:][::-1]]

    def get(self, request_id: str) -> dict | None:
        with self._lock:
            for item in reversed(self._items):
                if item["request_id"] == request_id:
                    return dict(item)
        return None

    def snapshot(self, limit: int = 50) -> dict:
        with self._lock:
            total = len(self._items)
        return {"total": total, "max_entries": self.max_entries, "items": self.recent(limit)}
