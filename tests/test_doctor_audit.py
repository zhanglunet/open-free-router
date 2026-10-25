"""PRD-149/T3: doctor conclusions land in the failover audit ledger.

T1 §3.1 is the alignment contract under test: a ``cmd_doctor`` run reduces
to ``doctor_verdict`` (pass/warn/fail) plus the error-level check count, is
persisted as the latest doctor run in ``usage.db``, and stamps the
``doctor_*`` columns of every later failover decision row whose caller did
not pass its own doctor context. Labels, details and client paths never
enter the ledger -- asserted against the raw database bytes, not just the
API surface.
"""
import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest

from open_free_router.analytics import AnalyticsStore
from open_free_router.cli import cmd_doctor
from open_free_router.failover_audit import (
    AnalyticsAuditHook,
    doctor_summary_from_report,
    make_audit_hook,
)
from open_free_router.telemetry import RouteDecisionStore


DECISION_COLUMNS = (
    "at", "request_id", "from_candidate", "to_candidate", "reason",
    "attempt", "requested_model", "strategy", "quota_band",
    "doctor_verdict", "doctor_failed_checks", "doctor_at",
)


def _check(severity, label="a check", detail=""):
    return {
        "ok": severity == "ok",
        "severity": severity,
        "label": label,
        "detail": detail,
    }


# ----------------------------------------------- T1 §3.1 pure mapping ---

def test_doctor_summary_maps_pass_warn_fail():
    assert doctor_summary_from_report({"checks": [_check("ok")]}) == {
        "doctor_verdict": "pass", "doctor_failed_checks": 0,
    }
    assert doctor_summary_from_report(
        {"checks": [_check("ok"), _check("warning")]}
    ) == {"doctor_verdict": "warn", "doctor_failed_checks": 0}
    assert doctor_summary_from_report(
        {"checks": [_check("error"), _check("warning")]}
    ) == {"doctor_verdict": "fail", "doctor_failed_checks": 1}


def test_doctor_summary_counts_errors_across_report_groups():
    report = {
        "checks": [_check("error"), _check("warning")],
        "routing": [{"severity": "error"}, {"severity": "warning"}],
        "free_tier": {"summary": {}, "issues": [{"severity": "warning"}]},
        "protocol_matrix": {"issues": [{"severity": "error"}]},
        "clients": [{"name": "Codex profile", "configured": False, "path": "x"}],
    }
    assert doctor_summary_from_report(report) == {
        # checks + routing + protocol_matrix errors; warnings never count.
        "doctor_verdict": "fail", "doctor_failed_checks": 3,
    }


def test_doctor_summary_keeps_no_plaintext():
    report = {
        "checks": [{
            "ok": False, "severity": "error", "label": "config.yaml found",
            "detail": "/home/example-user/.config/open-free-router/config.yaml",
        }],
        "routing": [],
        "free_tier": {"issues": []},
        "protocol_matrix": {"issues": []},
        "clients": [{"name": "Codex profile", "configured": False,
                     "path": "/home/example-user/.codex/profile.toml"}],
    }
    summary = doctor_summary_from_report(report)
    assert set(summary) == {"doctor_verdict", "doctor_failed_checks"}
    blob = json.dumps(summary)
    for banned in (
        "config.yaml found", "/home/example-user", "profile.toml",
        "label", "detail", "Codex",
    ):
        assert banned not in blob


def test_doctor_summary_refuses_to_fabricate_without_check_groups():
    # A payload without any recognized severity group must not yield a
    # "pass" the ledger could stamp onto later decisions.
    assert doctor_summary_from_report({}) is None
    assert doctor_summary_from_report(None) is None
    assert doctor_summary_from_report(
        {"clients": [{"path": "x"}]}
    ) is None
    assert doctor_summary_from_report(
        {"ok": True, "problem_count": 0}
    ) is None


# --------------------------------------------------- latest-run store ---

def test_record_and_latest_doctor_run_roundtrip_three_states(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        assert store.latest_doctor_run() is None
        store.record_doctor_run(
            doctor_verdict="warn", doctor_failed_checks=0,
            doctor_at="2026-10-23T12:00:00+00:00",
        )
        assert store.latest_doctor_run() == {
            "doctor_verdict": "warn", "doctor_failed_checks": 0,
            "doctor_at": "2026-10-23T12:00:00+00:00",
        }
        # Singleton upsert: the newest run replaces the previous one.
        store.record_doctor_run(
            doctor_verdict="fail", doctor_failed_checks=2,
            doctor_at="2026-10-23T12:05:00+00:00",
        )
        assert store.latest_doctor_run()["doctor_verdict"] == "fail"
        store.record_doctor_run(doctor_verdict="pass", doctor_failed_checks=0)
        latest = store.latest_doctor_run()
        assert latest["doctor_verdict"] == "pass"
        assert latest["doctor_failed_checks"] == 0
        # Default doctor_at is a real ISO8601 UTC stamp.
        assert datetime.fromisoformat(latest["doctor_at"])
    finally:
        store.close()


def test_record_doctor_run_ignores_invalid_verdict_and_disabled_store(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        store.record_doctor_run(
            doctor_verdict="bogus", doctor_failed_checks=1,
            doctor_at="2026-10-23T12:00:00+00:00",
        )
        assert store.latest_doctor_run() is None
    finally:
        store.close()
    disabled = AnalyticsStore(None, retention_days=0)
    disabled.record_doctor_run(doctor_verdict="pass")  # no-op, never raises
    assert disabled.latest_doctor_run() is None


def test_existing_store_upgrades_in_place_with_doctor_state(tmp_path):
    # A pre-T3 store (T2 tables, audit rows, user_version=2) gains
    # doctor_state on the next open -- in place, without losing rows or
    # being disabled by the strict column checks.
    path = tmp_path / "usage.db"
    store = AnalyticsStore(path, retention_days=30)
    seed = store.record_failover_decision(
        request_id="ofr_example_seed", from_candidate="a/model",
        to_candidate="b/model", reason="rate_limited", attempt=1,
    )
    store.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE doctor_state")
        connection.execute("PRAGMA user_version=2")
        connection.commit()
    finally:
        connection.close()

    store = AnalyticsStore(path, retention_days=30)
    try:
        assert store.enabled
        assert store.latest_doctor_run() is None
        store.record_doctor_run(doctor_verdict="fail", doctor_failed_checks=1)
        assert store.latest_doctor_run()["doctor_verdict"] == "fail"
        connection = sqlite3.connect(path)
        try:
            # PRD-174/T3 bumps the schema generation to 4 (honesty columns).
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
            kept = connection.execute(
                "SELECT COUNT(*) FROM failover_decisions WHERE decision_id = ?",
                (seed,),
            ).fetchone()[0]
            assert kept == 1
        finally:
            connection.close()
    finally:
        store.close()


# ------------------------------------------------------ hook stamping ---

def _decision_row(store, decision_id):
    connection = sqlite3.connect(str(store.path))
    try:
        row = connection.execute(
            f"SELECT {', '.join(DECISION_COLUMNS)} FROM failover_decisions "
            "WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        return dict(zip(DECISION_COLUMNS, row))
    finally:
        connection.close()


def _write_decision(hook, **extra):
    fields = {
        "request_id": "ofr_example_001",
        "from_candidate": "<provider-a>/<model-x>",
        "to_candidate": "<provider-b>/<model-y>",
        "reason": "rate_limited",
        "attempt": 1,
    }
    fields.update(extra)
    return hook.on_failover_decision(**fields)


def test_hook_stamps_latest_doctor_conclusion(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        store.record_doctor_run(
            doctor_verdict="fail", doctor_failed_checks=2,
            doctor_at="2026-10-23T12:00:00+00:00",
        )
        # Production wiring: the factory hook built from the decision store.
        hook = make_audit_hook(RouteDecisionStore(analytics=store))
        decision_id = _write_decision(hook)
        row = _decision_row(store, decision_id)
        assert row["doctor_verdict"] == "fail"
        assert row["doctor_failed_checks"] == 2
        assert row["doctor_at"] == "2026-10-23T12:00:00+00:00"
    finally:
        store.close()


def test_hook_defaults_without_doctor_run_and_respects_caller_context(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        hook = AnalyticsAuditHook(store)
        # No doctor run recorded: the T2 defaults hold (never_run / NULL).
        row = _decision_row(store, _write_decision(hook))
        assert row["doctor_verdict"] == "never_run"
        assert row["doctor_failed_checks"] is None
        assert row["doctor_at"] is None
        # Caller-supplied doctor context wins over the store's latest run.
        store.record_doctor_run(
            doctor_verdict="fail", doctor_failed_checks=9,
            doctor_at="2026-10-23T12:00:00+00:00",
        )
        row = _decision_row(store, _write_decision(
            hook, doctor_verdict="pass", doctor_failed_checks=0,
            doctor_at="2026-10-23T00:00:00+00:00",
        ))
        assert row["doctor_verdict"] == "pass"
        assert row["doctor_failed_checks"] == 0
        assert row["doctor_at"] == "2026-10-23T00:00:00+00:00"
    finally:
        store.close()


def test_hook_stamping_never_interrupts_on_lookup_failure(tmp_path):
    store = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    try:
        def _boom():
            raise sqlite3.OperationalError("doctor lookup exploded")

        store.latest_doctor_run = _boom
        hook = AnalyticsAuditHook(store)
        decision_id = _write_decision(hook)
        assert decision_id  # recorded, with defaults
        assert _decision_row(store, decision_id)["doctor_verdict"] == "never_run"
    finally:
        store.close()


# ------------------------------------------------ cmd_doctor 落账接线 ---

_CLEAN_REGISTRY = (
    "provider:\n"
    "  prefix: p\n"
    "  api_key: test-placeholder\n"
    "  upstream_url: https://upstream.example.test/v1\n"
    "  free_tier:\n"
    "    type: recurring_quota\n"
    "    limit: 1000\n"
    "    unit: requests/day\n"
    "    reset_period: daily\n"
    "    evidence_url: https://example.com/docs/free-tier\n"
    '    verified_at: "2026-08-01T00:00:00+00:00"\n'
    '    expires_at: "2027-01-01T00:00:00+00:00"\n'
    "  models:\n"
    "    - id: coder\n"
    "      tool_calling: true\n"
)


def _doctor_env(tmp_path, monkeypatch, routing="", reachable=True):
    """Temp config + registry with an isolated data_dir; reachable proxy/ui."""
    (tmp_path / "registry.yaml").write_text(_CLEAN_REGISTRY)
    config = tmp_path / "config.yaml"
    config.write_text(
        "registry: registry.yaml\n"
        f"data_dir: {tmp_path / 'data'}\n"
        + routing
    )
    monkeypatch.setenv("OPEN_FREE_ROUTER_CONFIG", str(config))
    monkeypatch.setattr(
        "open_free_router.cli._status_payload",
        lambda cfg: {
            "providers": 1, "providers_with_key": 1, "models": 1,
            "proxy_reachable": reachable, "ui_reachable": reachable,
            "proxy_url": "http://127.0.0.1:8337",
            "ui_url": "http://127.0.0.1:9057",
        },
    )
    return tmp_path / "data" / "usage.db"


def _data_dir_bytes(db_path):
    return b"".join(
        path.read_bytes() for path in sorted(db_path.parent.iterdir())
    )


def test_cmd_doctor_records_conclusion_pass_warn_fail(tmp_path, monkeypatch, capsys):
    db_path = _doctor_env(tmp_path, monkeypatch, reachable=True)

    # pass: every check ok, no issues anywhere.
    cmd_doctor(SimpleNamespace(json=True))
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    store = AnalyticsStore(db_path, retention_days=30)
    try:
        latest = store.latest_doctor_run()
        assert latest["doctor_verdict"] == "pass"
        assert latest["doctor_failed_checks"] == 0
        assert datetime.fromisoformat(latest["doctor_at"])
    finally:
        store.close()

    # warn: proxy/ui unreachable are warn_only checks -> verdict warn, 0 errors.
    _doctor_env(tmp_path, monkeypatch, reachable=False)
    cmd_doctor(SimpleNamespace(json=True))
    capsys.readouterr()
    store = AnalyticsStore(db_path, retention_days=30)
    try:
        latest = store.latest_doctor_run()
        assert latest["doctor_verdict"] == "warn"
        assert latest["doctor_failed_checks"] == 0
        assert datetime.fromisoformat(latest["doctor_at"])
    finally:
        store.close()

    # fail: a routing error (unregistered alias candidate) -> verdict fail,
    # exit code 1, and the conclusion is still recorded before the exit.
    _doctor_env(
        tmp_path, monkeypatch, reachable=True,
        routing=(
            "routing:\n"
            "  aliases:\n"
            "    preferred:\n"
            "      candidates: [missing]\n"
        ),
    )
    with pytest.raises(SystemExit) as stopped:
        cmd_doctor(SimpleNamespace(json=True))
    assert stopped.value.code == 1
    report = json.loads(capsys.readouterr().out)
    store = AnalyticsStore(db_path, retention_days=30)
    try:
        latest = store.latest_doctor_run()
        assert latest["doctor_verdict"] == "fail"
        # Only error-level items count, and the count matches the report's
        # own problem_count (both are the error-level total).
        assert latest["doctor_failed_checks"] == report["problem_count"]
        assert latest["doctor_failed_checks"] >= 1
    finally:
        store.close()


def test_cmd_doctor_records_conclusion_in_human_mode(tmp_path, monkeypatch, capsys):
    db_path = _doctor_env(tmp_path, monkeypatch, reachable=False)
    cmd_doctor(SimpleNamespace())  # human mode, no --json
    capsys.readouterr()
    store = AnalyticsStore(db_path, retention_days=30)
    try:
        latest = store.latest_doctor_run()
        assert latest is not None
        assert latest["doctor_verdict"] == "warn"
    finally:
        store.close()


def test_cmd_doctor_ledger_carries_no_plaintext(tmp_path, monkeypatch, capsys):
    db_path = _doctor_env(
        tmp_path, monkeypatch, reachable=True,
        routing=(
            "routing:\n"
            "  aliases:\n"
            "    preferred:\n"
            "      candidates: [missing]\n"
        ),
    )
    with pytest.raises(SystemExit):
        cmd_doctor(SimpleNamespace(json=True))
    report = json.loads(capsys.readouterr().out)

    # A failover decision written after the doctor run must carry the
    # conclusion but none of the report's prose.
    store = AnalyticsStore(db_path, retention_days=30)
    try:
        hook = make_audit_hook(RouteDecisionStore(analytics=store))
        decision_id = hook.on_failover_decision(
            request_id="ofr_example_002",
            from_candidate="<provider-a>/<model-x>",
            to_candidate="<provider-b>/<model-y>",
            reason="quota_exhausted",
            attempt=1,
        )
        row = _decision_row(store, decision_id)
        assert row["doctor_verdict"] == "fail"
        assert row["doctor_failed_checks"] == report["problem_count"]
        assert datetime.fromisoformat(row["doctor_at"])
    finally:
        store.close()

    # Raw bytes of every file in the data dir: no labels, details, issue
    # codes, fix texts or local paths from the doctor report may appear.
    blob = _data_dir_bytes(db_path)
    for banned in (
        "config.yaml found", "providers registered", "dashboard reachable",
        "candidate_not_registered", "no_eligible_candidates",
        "未在 registry.yaml 中注册",
        str(tmp_path / "registry.yaml"),
        "upstream.example.test",
    ):
        assert banned.encode() not in blob, banned


def test_cmd_doctor_conclusion_recording_survives_store_failure(
        tmp_path, monkeypatch, capsys):
    db_path = _doctor_env(tmp_path, monkeypatch, reachable=True)
    monkeypatch.setattr(
        "open_free_router.analytics.AnalyticsStore.record_doctor_run",
        lambda self, **kw: (_ for _ in ()).throw(RuntimeError("ledger down")),
    )
    cmd_doctor(SimpleNamespace(json=True))  # must not raise, must not exit 1
    out = capsys.readouterr().out
    report = json.loads(out)
    assert report["ok"] is True
    assert db_path.exists()
