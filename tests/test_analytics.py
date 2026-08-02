import json
import stat
import threading
import time
from pathlib import Path

import pytest
import sqlite3

from open_free_router.analytics import AnalyticsStore, ExportSecurityError


def _event(request_id, **overrides):
    event = {
        "request_id": request_id,
        "timestamp": time.time(),
        "provider": "provider",
        "model": "p/model",
        "status": "success",
        "status_code": 200,
        "ttfb_ms": 100,
        "total_latency_ms": 300,
        "input_tokens": 10,
        "output_tokens": 5,
        "fallback_attempts": 0,
        "error_kind": "",
        "circuit_breaker_events": 0,
    }
    event.update(overrides)
    return event


def test_sqlite_store_is_owner_only_and_contains_only_allowlisted_columns(tmp_path):
    path = tmp_path / "data" / "usage.db"
    store = AnalyticsStore(path, retention_days=30)
    event = _event("one")
    event.update({"authorization": "Bearer super-secret", "prompt": "private prompt",
                  "response_body": "private response", "tool_arguments": {"secret": True}})
    store.record(event)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    columns = [row[1] for row in store._connection.execute("PRAGMA table_info(usage_events)")]
    assert "prompt" not in columns
    assert "response" not in columns
    assert "api_key" not in columns
    assert "request_id" not in columns
    assert "event_id" in columns and "input_tokens" in columns
    wal = Path(f"{path}-wal")
    database_bytes = path.read_bytes() + (wal.read_bytes() if wal.exists() else b"")
    assert b"super-secret" not in database_bytes
    assert b"private prompt" not in database_bytes
    store.close()


def test_summary_reports_rates_latencies_tokens_and_saved_failures(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    store.record(_event("success", fallback_attempts=2))
    store.record(_event(
        "failed", status="failed", status_code=429, ttfb_ms=500,
        total_latency_ms=700, input_tokens=None, output_tokens=None,
        error_kind="rate_limited", circuit_breaker_events=1,
    ))
    summary = store.summary(7)
    overall = summary["overall"]
    assert overall["requests"] == 2
    assert overall["success_rate"] == 0.5
    assert overall["fallback_rate"] == 0.5
    assert overall["saved_failed_requests"] == 2
    assert overall["circuit_breaker_events"] == 1
    assert overall["input_tokens"] == 10 and overall["output_tokens"] == 5
    assert overall["token_coverage_rate"] == 0.5
    assert overall["ttfb_p50_ms"] == 100
    assert overall["ttfb_p95_ms"] == 500
    assert summary["quota_usage_is_estimated"] is True


def test_retention_zero_disables_database_and_old_rows_are_purged(tmp_path):
    disabled_path = tmp_path / "disabled.db"
    disabled = AnalyticsStore(disabled_path, retention_days=0)
    disabled.record(_event("ignored"))
    assert disabled.enabled is False
    assert not disabled_path.exists()

    store = AnalyticsStore(tmp_path / "usage.db", retention_days=2)
    store.record(_event("old", timestamp=time.time() - 3 * 86400))
    store.record(_event("new"))
    assert store.purge() == 1
    assert store.summary()["overall"]["requests"] == 1


def test_json_and_csv_exports_are_allowlisted_and_formula_safe(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    store.record(_event("formula", provider="+provider", model="@model"))
    payload, content_type = store.export("json")
    assert content_type == "application/json"
    assert set(json.loads(payload)[0]) == {
        "timestamp", "provider", "model", "status", "status_code",
        "ttfb_ms", "total_latency_ms", "input_tokens", "output_tokens",
        "fallback_attempts", "error_kind", "circuit_breaker_events",
    }
    csv_payload, _ = store.export("csv")
    assert "'+provider" in csv_payload and "'@model" in csv_payload


def test_export_rejects_secret_like_values(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    store.record(_event("secret", model="sk-secretvalue123"))
    with pytest.raises(ExportSecurityError):
        store.export("json")


def test_concurrent_upserts_remain_consistent(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    threads = [
        threading.Thread(target=store.record, args=(_event(f"request-{index}"),))
        for index in range(50)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.summary()["overall"]["requests"] == 50


def test_incompatible_schema_disables_analytics_without_breaking_calls(tmp_path):
    path = tmp_path / "usage.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE usage_events (wrong TEXT)")
    connection.commit()
    connection.close()
    store = AnalyticsStore(path, retention_days=30)
    assert store.enabled is False
    assert store.last_error == "DatabaseError"
    store.record(_event("ignored"))
