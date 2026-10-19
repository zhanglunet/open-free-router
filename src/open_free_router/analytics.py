"""Privacy-minimized local SQLite usage analytics."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid


EXPORT_FIELDS = (
    "timestamp", "provider", "model", "status", "status_code",
    "ttfb_ms", "total_latency_ms", "input_tokens", "output_tokens",
    "fallback_attempts", "error_kind", "circuit_breaker_events",
)
# PRD-059/T4: T1 §5 decision-log columns. Only enum kinds, candidate IDs and
# timings are ever stored -- never headers, keys, prompts or bodies.
FAILOVER_DECISION_FIELDS = (
    "at", "request_id", "from_candidate", "to_candidate", "reason",
    "attempt", "elapsed_ms", "ttfb_ms", "circuit",
)
# FailureDecision.kind values the failover policy may emit (T1 §1.3). Anything
# else is coerced to "unknown" at write time so the table can never carry
# free-form error prose.
FAILOVER_REASONS = frozenset({
    "provider_unavailable", "rate_limited", "quota_exhausted", "transport_error",
    "model_unavailable", "context_overflow", "invalid_request",
    "credential_invalid", "credits_exhausted", "model_forbidden",
})
SENSITIVE_PATTERN = re.compile(
    r"authorization|api[_-]?key|bearer\s+|sk-[A-Za-z0-9_-]{8,}|prompt|response[_-]?body|tool[_-]?arguments",
    re.IGNORECASE,
)


class ExportSecurityError(ValueError):
    pass


def _safe_number(value, integer: bool = False):
    if value is None:
        return None
    try:
        number = int(value) if integer else float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not integer and not math.isfinite(number):
        return None
    return max(0, number)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return round(ordered[index], 3)


class AnalyticsStore:
    """Thread-safe SQLite store; failures never interrupt inference."""

    def __init__(self, path: str | Path | None, retention_days: int = 30):
        self.path = Path(path).expanduser() if path and retention_days > 0 else None
        self.retention_days = max(0, int(retention_days))
        self.enabled = self.path is not None and self.retention_days > 0
        self.last_error = ""
        self._last_purge_at = 0.0
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        if self.enabled:
            self._initialize()

    def _initialize(self) -> None:
        connection = None
        try:
            assert self.path is not None
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            connection = sqlite3.connect(self.path, check_same_thread=False)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA busy_timeout=3000")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS usage_events (
                    event_id TEXT PRIMARY KEY,
                    timestamp REAL NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    ttfb_ms REAL,
                    total_latency_ms REAL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    fallback_attempts INTEGER NOT NULL,
                    error_kind TEXT NOT NULL,
                    circuit_breaker_events INTEGER NOT NULL
                )
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(usage_events)")}
            expected = {"event_id", *EXPORT_FIELDS}
            if columns != expected:
                raise sqlite3.DatabaseError("unsupported analytics schema")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS usage_events_timestamp ON usage_events(timestamp)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS usage_events_model ON usage_events(provider, model)"
            )
            # PRD-059/T4: failover decision log (T1 §5). Same 0600/retention
            # conventions as usage_events.
            connection.execute("""
                CREATE TABLE IF NOT EXISTS failover_decisions (
                    decision_id TEXT PRIMARY KEY,
                    at TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    from_candidate TEXT,
                    to_candidate TEXT,
                    reason TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    elapsed_ms REAL,
                    ttfb_ms REAL,
                    circuit TEXT
                )
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(failover_decisions)")}
            expected_failover = {"decision_id", *FAILOVER_DECISION_FIELDS}
            if columns != expected_failover:
                raise sqlite3.DatabaseError("unsupported analytics schema")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS failover_decisions_at ON failover_decisions(at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS failover_decisions_request ON failover_decisions(request_id)"
            )
            connection.execute("PRAGMA user_version=1")
            connection.commit()
            self._connection = connection
            self._secure_files()
            self.purge()
            self.last_error = ""
        except (OSError, sqlite3.Error, ValueError) as exc:
            if connection is not None:
                connection.close()
            self._connection = None
            self.enabled = False
            self.last_error = exc.__class__.__name__

    def _secure_files(self) -> None:
        if not self.path:
            return
        for candidate in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            if candidate.exists():
                os.chmod(candidate, 0o600)

    def close(self) -> None:
        with self._lock:
            if self._connection:
                self._connection.close()
                self._connection = None

    def purge(self, now: float | None = None) -> int:
        if not self.enabled or not self._connection:
            return 0
        cutoff = (time.time() if now is None else now) - self.retention_days * 86400
        with self._lock:
            try:
                cursor = self._connection.execute(
                    "DELETE FROM usage_events WHERE timestamp < ?", (cutoff,)
                )
                removed = max(0, cursor.rowcount)
                iso_cutoff = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
                cursor = self._connection.execute(
                    "DELETE FROM failover_decisions WHERE at < ?", (iso_cutoff,)
                )
                removed += max(0, cursor.rowcount)
                self._connection.commit()
                self._secure_files()
                self._last_purge_at = time.time() if now is None else now
                return removed
            except (OSError, sqlite3.Error) as exc:
                self.last_error = exc.__class__.__name__
                return 0

    def record(self, item: dict) -> None:
        if not self.enabled or not self._connection:
            return
        request_id = str(item.get("request_id", ""))[:128]
        if not request_id:
            return
        row = (
            hashlib.sha256(request_id.encode()).hexdigest(),
            float(item.get("timestamp", time.time())),
            str(item.get("provider", ""))[:256],
            str(item.get("model", ""))[:256],
            "success" if item.get("status") == "success" else "failed",
            int(item.get("status_code", 0)),
            _safe_number(item.get("ttfb_ms")),
            _safe_number(item.get("total_latency_ms")),
            _safe_number(item.get("input_tokens"), integer=True),
            _safe_number(item.get("output_tokens"), integer=True),
            _safe_number(item.get("fallback_attempts"), integer=True) or 0,
            str(item.get("error_kind", ""))[:64],
            _safe_number(item.get("circuit_breaker_events"), integer=True) or 0,
        )
        with self._lock:
            try:
                if time.time() - self._last_purge_at >= 3600:
                    self.purge()
                self._connection.execute(
                    """INSERT INTO usage_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(event_id) DO UPDATE SET
                         timestamp=excluded.timestamp, provider=excluded.provider,
                         model=excluded.model, status=excluded.status,
                         status_code=excluded.status_code, ttfb_ms=excluded.ttfb_ms,
                         total_latency_ms=excluded.total_latency_ms,
                         input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,
                         fallback_attempts=excluded.fallback_attempts,
                         error_kind=excluded.error_kind,
                         circuit_breaker_events=excluded.circuit_breaker_events""",
                    row,
                )
                self._connection.commit()
                self._secure_files()
                self.last_error = ""
            except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
                self.last_error = exc.__class__.__name__

    def record_failover_decision(
        self,
        *,
        request_id: str,
        from_candidate: str | None,
        to_candidate: str | None,
        reason: str,
        attempt: int,
        elapsed_ms: float | None = None,
        ttfb_ms: float | None = None,
        circuit: str | None = None,
        at: str | None = None,
    ) -> None:
        """Write one T1 §5 failover decision row (PRD-059/T4).

        Desensitized by construction: ``reason`` is coerced to a known
        FailureDecision.kind enum and every free-form field is length-bounded;
        the encoded row is additionally scanned for secret patterns and
        rejected rather than stored.
        """
        if not self.enabled or not self._connection:
            return
        request_id = str(request_id or "")[:128]
        if not request_id:
            return
        reason = str(reason or "")
        if reason not in FAILOVER_REASONS:
            reason = "unknown"
        circuit = str(circuit or "")
        if circuit not in ("provider", "credential", "model", ""):
            circuit = ""
        row = (
            uuid.uuid4().hex,
            at or datetime.now(timezone.utc).isoformat(),
            request_id,
            str(from_candidate or "")[:256] or None,
            str(to_candidate or "")[:256] or None,
            reason,
            max(0, int(attempt)),
            _safe_number(elapsed_ms),
            _safe_number(ttfb_ms),
            circuit or None,
        )
        encoded = json.dumps(row, ensure_ascii=False)
        if SENSITIVE_PATTERN.search(encoded):
            raise ExportSecurityError("sensitive failover decision content rejected")
        with self._lock:
            try:
                if time.time() - self._last_purge_at >= 3600:
                    self.purge()
                self._connection.execute(
                    """INSERT INTO failover_decisions
                       (decision_id, at, request_id, from_candidate, to_candidate,
                        reason, attempt, elapsed_ms, ttfb_ms, circuit)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    row,
                )
                self._connection.commit()
                self._secure_files()
                self.last_error = ""
            except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
                self.last_error = exc.__class__.__name__

    def failover_decisions(
        self,
        days: int = 30,
        model: str | None = None,
        reason: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Query the T1 §5 decision log for the dashboard (PRD-059/T4)."""
        if not self.enabled or not self._connection:
            return []
        days = max(1, min(int(days), self.retention_days))
        # ISO8601 UTC strings sort lexicographically, so the window cutoff
        # can be compared as text.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        limit = max(1, min(int(limit), 10000))
        conditions = ["at >= ?"]
        params: list = [cutoff]
        if model:
            conditions.append("(from_candidate = ? OR to_candidate = ?)")
            params.extend([str(model)[:256], str(model)[:256]])
        if reason:
            conditions.append("reason = ?")
            params.append(str(reason)[:64])
        sql = (
            f"SELECT {', '.join(FAILOVER_DECISION_FIELDS)} FROM failover_decisions "
            f"WHERE {' AND '.join(conditions)} ORDER BY at DESC LIMIT ?"
        )
        params.append(limit)
        with self._lock:
            cursor = self._connection.execute(sql, tuple(params))
            return [dict(zip(FAILOVER_DECISION_FIELDS, row)) for row in cursor.fetchall()]

    def _rows(self, days: int = 30, limit: int | None = None) -> list[dict]:
        if not self.enabled or not self._connection:
            return []
        days = max(1, min(int(days), self.retention_days))
        cutoff = time.time() - days * 86400
        with self._lock:
            sql = (f"SELECT {', '.join(EXPORT_FIELDS)} FROM usage_events "
                   "WHERE timestamp >= ? ORDER BY timestamp DESC")
            params: tuple = (cutoff,)
            if limit is not None:
                limit = max(1, min(int(limit), 10000))
                sql += " LIMIT ?"
                params = (cutoff, limit)
            cursor = self._connection.execute(sql, params)
            return [dict(zip(EXPORT_FIELDS, row)) for row in cursor.fetchall()]

    def summary(self, days: int = 30) -> dict:
        rows = self._rows(days)
        groups: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            if row["provider"] or row["model"]:
                groups.setdefault((row["provider"], row["model"]), []).append(row)

        def aggregate(items: list[dict]) -> dict:
            count = len(items)
            ttfb = [row["ttfb_ms"] for row in items if row["ttfb_ms"] is not None]
            total = [row["total_latency_ms"] for row in items if row["total_latency_ms"] is not None]
            token_rows = [row for row in items
                          if row["input_tokens"] is not None or row["output_tokens"] is not None]
            return {
                "requests": count,
                "successes": sum(row["status"] == "success" for row in items),
                "success_rate": round(sum(row["status"] == "success" for row in items) / count, 6) if count else 0,
                "fallback_requests": sum(row["fallback_attempts"] > 0 for row in items),
                "fallback_rate": round(sum(row["fallback_attempts"] > 0 for row in items) / count, 6) if count else 0,
                "saved_failed_requests": sum(row["fallback_attempts"] for row in items if row["status"] == "success"),
                "circuit_breaker_events": sum(row["circuit_breaker_events"] for row in items),
                "input_tokens": sum(row["input_tokens"] or 0 for row in items),
                "output_tokens": sum(row["output_tokens"] or 0 for row in items),
                "token_coverage_rate": round(len(token_rows) / count, 6) if count else 0,
                "ttfb_p50_ms": _percentile(ttfb, 0.50),
                "ttfb_p95_ms": _percentile(ttfb, 0.95),
                "total_p50_ms": _percentile(total, 0.50),
                "total_p95_ms": _percentile(total, 0.95),
            }

        errors = {}
        for row in rows:
            if row["error_kind"]:
                errors[row["error_kind"]] = errors.get(row["error_kind"], 0) + 1
        return {
            "enabled": self.enabled,
            "healthy": not self.last_error,
            "error": self.last_error,
            "retention_days": self.retention_days,
            "period_days": max(1, min(int(days), self.retention_days)) if self.enabled else 0,
            "overall": aggregate(rows),
            "groups": [
                {"provider": provider, "model": model, **aggregate(items)}
                for (provider, model), items in sorted(groups.items())
            ],
            "errors": errors,
            "quota_usage_is_estimated": True,
            "quota_estimate_notice_zh": "仅按本机观测到的请求和上游返回 Token 统计估算，不代表厂商账单或完整账号额度。",
        }

    @staticmethod
    def _assert_safe(rows: list[dict]) -> None:
        for row in rows:
            if set(row) != set(EXPORT_FIELDS):
                raise ExportSecurityError("export field allowlist violation")
            encoded = json.dumps(row, ensure_ascii=False)
            if SENSITIVE_PATTERN.search(encoded):
                raise ExportSecurityError("sensitive export content rejected")

    def export(self, format_name: str, days: int = 30) -> tuple[str, str]:
        rows = self._rows(days)
        self._assert_safe(rows)
        if format_name == "json":
            return json.dumps(rows, ensure_ascii=False, indent=2), "application/json"
        if format_name != "csv":
            raise ValueError("format must be json or csv")
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=EXPORT_FIELDS)
        writer.writeheader()
        for row in rows:
            safe = dict(row)
            for key in ("provider", "model", "status", "error_kind"):
                if str(safe[key]).startswith(("=", "+", "-", "@")):
                    safe[key] = "'" + str(safe[key])
            writer.writerow(safe)
        return output.getvalue(), "text/csv; charset=utf-8"

