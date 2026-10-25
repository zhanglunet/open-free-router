"""PRD-174/T3: 写入 PRD-149 failover 审计账本（trend issue #872）.

Contract: T1 §4 of ``docs/plans/2026-10-25-doctor-honest-labeling.md`` — a
purely additive schema extension (``failover_decisions.honesty`` and
``doctor_state.honesty_summary``, ``user_version`` 3→4), the three-layer
write gate (allowlist → sensitive-pattern rejection → never interrupt), the
conservative derivation default, and the folding rule for out-of-range
labels. Everything runs locally against tmp SQLite files; fixtures carry
placeholder candidate IDs only — no hosts, no accounts, no keys.
"""
import json
import logging
import sqlite3

import pytest

from open_free_router.analytics import AnalyticsStore, ExportSecurityError
from open_free_router.failover_audit import (
    HONESTY_SUMMARY_MAX_CHARS,
    AnalyticsAuditHook,
    derive_decision_honesty,
    honesty_summary_to_compact,
)


def _decision_honesty(store, decision_id) -> str | None:
    connection = sqlite3.connect(str(store.path))
    try:
        row = connection.execute(
            "SELECT honesty FROM failover_decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else row[0]


def _doctor_state_row(store):
    connection = sqlite3.connect(str(store.path))
    try:
        return connection.execute(
            "SELECT at, verdict, failed_checks, honesty_summary FROM doctor_state"
        ).fetchone()
    finally:
        connection.close()


def _decision(store, **fields) -> str:
    fields.setdefault("request_id", "ofr_example")
    fields.setdefault("from_candidate", "<provider-a>/<model-x>")
    fields.setdefault("to_candidate", "<byos-oc>/<model-y>")
    fields.setdefault("reason", "rate_limited")
    fields.setdefault("attempt", 1)
    decision_id = store.record_failover_decision(**fields)
    assert decision_id
    return decision_id


def test_fresh_store_has_honesty_columns_at_v4(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        assert store.enabled is True
        connection = sqlite3.connect(str(store.path))
        try:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
            decisions = {row[1] for row in connection.execute(
                "PRAGMA table_info(failover_decisions)")}
            doctor = {row[1] for row in connection.execute(
                "PRAGMA table_info(doctor_state)")}
        finally:
            connection.close()
        # 账本字段齐全（T1 §4 rows 1–2）。
        assert "honesty" in decisions
        assert "honesty_summary" in doctor
    finally:
        store.close()


def _legacy_v3_database(path):
    """A store exactly as PRD-149/T3 created it: no honesty columns, version 3."""
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("""
            CREATE TABLE usage_events (
                event_id TEXT PRIMARY KEY, timestamp REAL NOT NULL,
                provider TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
                status_code INTEGER NOT NULL, ttfb_ms REAL, total_latency_ms REAL,
                input_tokens INTEGER, output_tokens INTEGER,
                fallback_attempts INTEGER NOT NULL, error_kind TEXT NOT NULL,
                circuit_breaker_events INTEGER NOT NULL)
        """)
        connection.execute("""
            CREATE TABLE failover_decisions (
                decision_id TEXT PRIMARY KEY, at TEXT NOT NULL,
                request_id TEXT NOT NULL, from_candidate TEXT, to_candidate TEXT,
                reason TEXT NOT NULL, attempt INTEGER NOT NULL, elapsed_ms REAL,
                ttfb_ms REAL, circuit TEXT, requested_model TEXT, strategy TEXT,
                quota_band TEXT, quota_source TEXT, quota_classification TEXT,
                doctor_verdict TEXT, doctor_failed_checks INTEGER, doctor_at TEXT)
        """)
        connection.execute("""
            CREATE TABLE failover_outcomes (
                outcome_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL,
                settled_at TEXT NOT NULL, outcome TEXT NOT NULL,
                status_code INTEGER, error_kind TEXT, ttfb_ms REAL,
                total_latency_ms REAL, attempt INTEGER)
        """)
        connection.execute("""
            CREATE TABLE doctor_state (
                id INTEGER PRIMARY KEY CHECK (id = 1), at TEXT NOT NULL,
                verdict TEXT NOT NULL, failed_checks INTEGER NOT NULL)
        """)
        connection.execute(
            "INSERT INTO failover_decisions VALUES "
            "('v3row1', '2026-10-01T00:00:00+00:00', 'ofr_v3_legacy', "
            "'a/model', 'b/model', 'rate_limited', 1, 12.0, 5.0, NULL, "
            "'auto', 'priority', 'unknown', NULL, NULL, 'never_run', NULL, NULL)"
        )
        connection.execute(
            "INSERT INTO doctor_state VALUES "
            "(1, '2026-10-01T00:00:00+00:00', 'warn', 0)"
        )
        connection.execute("PRAGMA user_version=3")
        connection.commit()
    finally:
        connection.close()


def test_v3_store_migrates_in_place_and_keeps_rows_unlabeled(tmp_path):
    """T1 §4: declarative ADD COLUMN migration 3→4, idempotent, no data loss.

    Old rows stay NULL (shown as ``unlabeled``) and are never backfilled or
    defaulted into any honesty state (T1 §2.1).
    """
    path = tmp_path / "usage.db"
    _legacy_v3_database(path)
    store = AnalyticsStore(path, retention_days=30)
    try:
        assert store.enabled is True
        assert store.last_error == ""
        connection = sqlite3.connect(str(path))
        try:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
            legacy = connection.execute(
                "SELECT request_id, honesty FROM failover_decisions"
            ).fetchone()
            doctor = connection.execute(
                "SELECT verdict, honesty_summary FROM doctor_state"
            ).fetchone()
        finally:
            connection.close()
        assert legacy == ("ofr_v3_legacy", None)
        assert doctor == ("warn", None)
        # The ledger keeps working through the unchanged API.
        decision_id = _decision(store)
        assert _decision_honesty(store, decision_id) == "ambiguous"
    finally:
        store.close()


def test_v3_migration_is_idempotent(tmp_path):
    path = tmp_path / "usage.db"
    _legacy_v3_database(path)
    first = AnalyticsStore(path, retention_days=30)
    first.close()
    second = AnalyticsStore(path, retention_days=30)
    try:
        assert second.enabled is True
        assert second.last_error == ""
    finally:
        second.close()


def test_decision_honesty_explicit_value_is_stored(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        decision_id = _decision(store, honesty="inferred")
        assert _decision_honesty(store, decision_id) == "inferred"
    finally:
        store.close()


def test_decision_honesty_out_of_range_folds_to_ambiguous(tmp_path, caplog):
    """T1 §6 anchor 5: uppercase/whitespace labels fold, the row survives."""
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        with caplog.at_level(logging.WARNING, logger="open_free_router.analytics"):
            decision_id = _decision(store, honesty="EXTRACTED ")
        assert _decision_honesty(store, decision_id) == "ambiguous"
        # The warning records the fact and the fold target, never the
        # offending value (T1 §4: 记录原值类别，不含原值).
        assert any("honesty" in message and "ambiguous" in message
                   for message in caplog.messages)
        assert all("EXTRACTED" not in message for message in caplog.messages)
    finally:
        store.close()


def test_decision_honesty_derived_conservatively(tmp_path):
    """T1 §4 row 1 + §6 sample: doctor-stamped rows are inferred; without a
    real signal the row can only be ambiguous (T1 §2.3 rule 1)."""
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        # The T1 §6 sample row: doctor context stamped, quota never measured
        # → inferred (the doctor derivation carries the row).
        sample = _decision(
            store, request_id="ofr_example_002",
            doctor_verdict="warn", doctor_failed_checks=0,
            doctor_at="2026-10-25T11:58:00+00:00",
            quota_band="unknown", quota_source="never_measured",
        )
        assert _decision_honesty(store, sample) == "inferred"
        # Quota numbers actually parsed this round carry the row without a
        # doctor run.
        headers = _decision(
            store, request_id="ofr_example_003",
            quota_band="tight", quota_source="upstream_headers",
        )
        assert _decision_honesty(store, headers) == "inferred"
        # No doctor run, no measured source → ambiguous.
        dark = _decision(store, request_id="ofr_example_004")
        assert _decision_honesty(store, dark) == "ambiguous"
        # ``never_run`` is a ledger placeholder, not a run → ambiguous.
        never = _decision(
            store, request_id="ofr_example_005", doctor_verdict="never_run",
        )
        assert _decision_honesty(store, never) == "ambiguous"
        assert _decision_honesty(store, sample) != "extracted"
    finally:
        store.close()


def test_derive_decision_honesty_is_never_extracted():
    """A decision row is composed/derived context; ``extracted`` is
    unreachable by construction (T1 §2.2, T1 §6 anchor 3)."""
    for verdict in (None, "", "never_run", "pass", "warn", "fail", "bogus"):
        for source in (None, "", "never_measured", "event", "upstream_headers"):
            assert derive_decision_honesty(verdict, source) in {"inferred", "ambiguous"}


def test_honesty_gate_keeps_sensitive_pattern_rejection(tmp_path):
    """Adding the honesty column must not weaken the PRD-149 redline: a row
    carrying credential-shaped material is still rejected, never masked."""
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        with pytest.raises(ExportSecurityError):
            _decision(store, from_candidate="a/model sk-abcdefghijklmnop1234")
        connection = sqlite3.connect(str(store.path))
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM failover_decisions"
            ).fetchone()[0]
        finally:
            connection.close()
        assert count == 0
    finally:
        store.close()


def test_audit_hook_passes_honesty_through_the_allowlist(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        hook = AnalyticsAuditHook(store)
        decision_id = hook.on_failover_decision(
            request_id="ofr_hook", from_candidate="a/model",
            to_candidate="b/model", reason="rate_limited", attempt=1,
            honesty="inferred",
            not_in_allowlist="dropped-before-the-store",
        )
        assert decision_id
        assert _decision_honesty(store, decision_id) == "inferred"
    finally:
        store.close()


def test_doctor_run_honesty_summary_roundtrip(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        store.record_doctor_run(
            doctor_verdict="warn", doctor_failed_checks=0,
            honesty_summary={"extracted": 2, "inferred": 3, "ambiguous": 1},
        )
        row = _doctor_state_row(store)
        assert row[1] == "warn"
        assert json.loads(row[3]) == {"extracted": 2, "inferred": 3, "ambiguous": 1}
        assert row[3] == '{"extracted":2,"inferred":3,"ambiguous":1}'
        assert len(row[3]) <= HONESTY_SUMMARY_MAX_CHARS
        # Same-row update (singleton ON CONFLICT) rewrites the summary.
        store.record_doctor_run(
            doctor_verdict="pass", doctor_failed_checks=0,
            honesty_summary={"extracted": 5, "inferred": 0, "ambiguous": 0},
        )
        assert json.loads(_doctor_state_row(store)[3])["extracted"] == 5
    finally:
        store.close()


def test_doctor_run_malformed_summary_stores_null(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        store.record_doctor_run(
            doctor_verdict="pass", doctor_failed_checks=0,
            honesty_summary="extracted=99",
        )
        assert _doctor_state_row(store)[3] is None
        # A non-integer count is invalid, not zero-folded.
        store.record_doctor_run(
            doctor_verdict="pass", doctor_failed_checks=0,
            honesty_summary={"extracted": "many", "inferred": 0, "ambiguous": 0},
        )
        assert _doctor_state_row(store)[3] is None
        # Counts beyond the 128-char cap store NULL, never a truncated JSON.
        store.record_doctor_run(
            doctor_verdict="pass", doctor_failed_checks=0,
            honesty_summary={"extracted": 10 ** 200, "inferred": 0, "ambiguous": 0},
        )
        assert _doctor_state_row(store)[3] is None
        # A run without a labeled report keeps the column NULL.
        store.record_doctor_run(doctor_verdict="fail", doctor_failed_checks=2)
        assert _doctor_state_row(store)[3] is None
        assert _doctor_state_row(store)[1] == "fail"
    finally:
        store.close()


def test_honesty_summary_to_compact_edges():
    assert honesty_summary_to_compact(None) is None
    assert honesty_summary_to_compact({"extracted": 1}) == '{"extracted":1,"inferred":0,"ambiguous":0}'
    # Booleans are ints in Python but not counts.
    assert honesty_summary_to_compact({"extracted": True}) is None


def test_cli_doctor_conclusion_lands_summary_in_the_ledger(tmp_path):
    """The T2 report's ``honesty_summary`` block reaches doctor_state via the
    same conclusion-recording path PRD-149/T3 wired (T1 §4 row 2)."""
    from open_free_router.config import Config
    from open_free_router.cli import _record_doctor_conclusion

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"data_dir: {tmp_path / 'data'}\nregistry: registry.yaml\n", encoding="utf-8"
    )
    cfg = Config(config_path=config_file)
    report = {
        "ok": False,
        "problem_count": 1,
        "honesty": "inferred",
        "honesty_summary": {"extracted": 4, "inferred": 2, "ambiguous": 1},
        "checks": [
            {"ok": True, "severity": "ok", "label": "registry.yaml found",
             "detail": "registry.yaml", "honesty": "extracted"},
            {"ok": False, "severity": "warning", "label": "proxy reachable",
             "detail": "not running", "honesty": "ambiguous"},
        ],
    }
    _record_doctor_conclusion(cfg, report)
    store = AnalyticsStore(cfg.analytics_path, retention_days=cfg.analytics_retention_days)
    try:
        row = _doctor_state_row(store)
        assert row[1] == "warn"  # warnings only → warn
        assert json.loads(row[3]) == {"extracted": 4, "inferred": 2, "ambiguous": 1}
    finally:
        store.close()


def test_ledger_never_stores_free_text_labels(tmp_path):
    """``honesty`` is an enum word, not a free-text channel (T1 §4 redline):
    only the three labels ever appear in the column, whatever callers pass."""
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        for bad in ("实测", "looks legit", "inferred; drop table", ""):
            decision_id = _decision(store, request_id="ofr_enum", honesty=bad)
            assert _decision_honesty(store, decision_id) == "ambiguous"
    finally:
        store.close()
