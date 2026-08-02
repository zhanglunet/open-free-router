"""Bounded, redacted runtime route-decision history."""
from __future__ import annotations

import threading
import time
from collections import deque


class RouteDecisionStore:
    """Keep recent request routing facts without prompts, outputs or secrets."""

    def __init__(self, max_entries: int = 200):
        self.max_entries = max(1, int(max_entries))
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
        }
        with self._lock:
            self._items.append(item)
        return dict(item)

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
