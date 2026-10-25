"""PRD-173/T4: BYOS 与 PRD-096/131/149 额度与审计通道对接（trend issue #868）。

T1 契约（``docs/plans/2026-10-25-byos-access.md``）§5：账本不加列不改枚举、
不新开表；BYOS 上下文由既有 ``requested_model``/``strategy``/quota 列与
``from/to_candidate`` 前缀自明；无额度头的 BYOS 上游诚实落在
``unknown``/``never_measured``，给了 ``RateLimit-*`` 头则照常归一化。
所有端点均为 127.0.0.1 沙盒；所有密钥均为本测试自造的假密钥。
"""
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from open_free_router.analytics import AnalyticsStore
from open_free_router.byos.wiring import (
    ENV_MISSING,
    doctor_checks,
    register_byos_providers,
)
from open_free_router.config import Config
from open_free_router.executor import OpenedRoute, UpstreamExecutor
from open_free_router.failover_audit import doctor_summary_from_report
from open_free_router.quota_view import quota_view
from open_free_router.refresh import refresh
from open_free_router.registry import Registry
from open_free_router.resilience import ResilienceManager
from open_free_router.routing import RoutePlanner
from open_free_router.telemetry import RouteDecisionStore

FAKE_KEY = "test-key-0123456789abcdef"  # 沙盒假密钥，绝不代表任何真实凭据
KEY_ENV = "OFR_BYOS_OPENAI_COMPAT_API_KEY"
BASE_URL_ENV = "OFR_BYOS_OPENAI_COMPAT_BASE_URL"
MODEL_ENV = "OFR_BYOS_OPENAI_COMPAT_MODEL"
BYOS_NAME = "byos-openai-compat"


class _ByosConfig:
    """Minimal stand-in for the parsed config.yaml ``byos`` block (T1 §3.4)."""

    def __init__(self, enabled=True, adapters=(BYOS_NAME,)):
        self.byos_enabled = enabled
        self.byos_adapters = list(adapters)


# ---------------------------------------------------------------- fixtures ---

class _Handler(BaseHTTPRequestHandler):
    """Sandbox upstream: fixed status, optional rate-limit headers."""

    status = 200
    ratelimit = {}

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = b'{"model": "sub-model", "ok": true}'
        if self.status >= 400:
            body = b'{"error": {"message": "boom"}}'
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in type(self).ratelimit.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def _start(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _free_provider(port, prefix="fb"):
    return {
        "prefix": prefix,
        "upstream_url": f"http://127.0.0.1:{port}/v1",
        "api_key": f"placeholder-{prefix}",
        "models": [{"id": "model"}],
    }


def _byos_registry(port, extra=None):
    """A registry holding only the env-resolved BYOS gateway (+ extras)."""
    registry = Registry({})
    register_byos_providers(registry, _ByosConfig(), env={
        KEY_ENV: FAKE_KEY,
        BASE_URL_ENV: f"http://127.0.0.1:{port}/v1",
        MODEL_ENV: "sub-model",
    })
    if extra:
        for name, cfg in extra.items():
            registry.add_provider(Registry({name: cfg}).providers[name])
    return registry


def _executor(registry, decisions, analytics):
    planner = RoutePlanner(
        registry,
        {"fallback": {"max_attempts": 5}, "policy": {"backoff_jitter": 0}},
        prefer_subscribed=True,
    )
    return UpstreamExecutor(
        planner,
        ResilienceManager(),
        decisions=decisions,
        timeout=5,
    )


DECISION_COLUMNS = (
    "decision_id", "at", "request_id", "from_candidate", "to_candidate",
    "reason", "attempt", "elapsed_ms", "ttfb_ms", "circuit",
    "requested_model", "strategy", "quota_band", "quota_source",
    "quota_classification", "doctor_verdict", "doctor_failed_checks", "doctor_at",
)
OUTCOME_COLUMNS = (
    "outcome_id", "decision_id", "settled_at", "outcome", "status_code",
    "error_kind", "ttfb_ms", "total_latency_ms", "attempt",
)


def _ledger(store):
    connection = sqlite3.connect(str(store.path))
    try:
        decisions = [
            dict(zip(DECISION_COLUMNS, row))
            for row in connection.execute(
                f"SELECT {', '.join(DECISION_COLUMNS)} FROM failover_decisions ORDER BY rowid"
            )
        ]
        outcomes = [
            dict(zip(OUTCOME_COLUMNS, row))
            for row in connection.execute(
                f"SELECT {', '.join(OUTCOME_COLUMNS)} FROM failover_outcomes ORDER BY rowid"
            )
        ]
        return decisions, outcomes
    finally:
        connection.close()


def _whole_database_text(store) -> str:
    connection = sqlite3.connect(str(store.path))
    try:
        chunks = []
        for (table,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ):
            for row in connection.execute(f"SELECT * FROM {table}"):
                chunks.extend(str(cell) for cell in row)
        return "\n".join(chunks)
    finally:
        connection.close()


# ------------------------------------------------- T1 §5: registration seam ---

def test_registration_env_gated_and_in_memory_only():
    registry = Registry({})
    # Missing env: adapter stays unregistered (T1 §3.5).
    assert register_byos_providers(registry, _ByosConfig(), env={}) == []
    assert BYOS_NAME not in registry.providers

    registered = register_byos_providers(registry, _ByosConfig(), env={
        KEY_ENV: FAKE_KEY,
        BASE_URL_ENV: "https://<byos-openai-compat-host>/v1",
        MODEL_ENV: "sub-model",
    })
    assert registered == [BYOS_NAME]
    provider = registry.providers[BYOS_NAME]
    assert provider.byos is not None and provider.byos.priority_class == "subscribed"
    # T1 §6 redline 1: the registry carries the variable NAME, never a value.
    assert provider.api_key == ""
    assert provider.api_key_env == KEY_ENV
    assert provider.refresh_method == "byos"


def test_registration_respects_config_switches():
    assert register_byos_providers(Registry({}), _ByosConfig(enabled=False), env={
        KEY_ENV: FAKE_KEY,
        BASE_URL_ENV: "https://<byos-openai-compat-host>/v1",
    }) == []
    assert register_byos_providers(
        Registry({}), _ByosConfig(adapters=[]), env={KEY_ENV: FAKE_KEY}
    ) == []


def test_registration_never_overwrites_registry_yaml_entry():
    manual = Registry({BYOS_NAME: {
        "prefix": "oc",
        "base_url": "https://<byos-openai-compat-host>/v1",
        "api_key_env": KEY_ENV,
        "models": [{"id": "manual-model"}],
    }})
    kept = manual.providers[BYOS_NAME]
    registered = register_byos_providers(manual, _ByosConfig(), env={
        KEY_ENV: FAKE_KEY,
        BASE_URL_ENV: "http://127.0.0.1:9/v1",
    })
    # registry.yaml is the single source of truth (T1 §5): same-name
    # entries are left exactly as the user wrote them.
    assert registered == []
    assert manual.providers[BYOS_NAME] is kept


def test_registration_swallows_adapter_errors(monkeypatch):
    from open_free_router.byos import wiring

    class _Broken:
        descriptor = None

        def resolve(self, env):
            raise RuntimeError("boom")

    monkeypatch.setattr(wiring, "make_adapter", lambda name: _Broken())
    registry = Registry({})
    assert register_byos_providers(registry, _ByosConfig(), env={KEY_ENV: FAKE_KEY}) == []
    assert BYOS_NAME not in registry.providers


def test_registration_works_from_a_real_config_file(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "registry: registry.yaml\n"
        "byos:\n"
        "  adapters:\n"
        f"    - {BYOS_NAME}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(KEY_ENV, FAKE_KEY)
    monkeypatch.setenv(BASE_URL_ENV, "https://<byos-openai-compat-host>/v1")
    monkeypatch.setenv(MODEL_ENV, "sub-model")
    cfg = Config(config_path=config_file)
    registry = Registry({})
    assert register_byos_providers(registry, cfg) == [BYOS_NAME]
    assert registry.providers[BYOS_NAME].models[0].id == "sub-model"


# --------------------------------------------- T1 §5 row 2: doctor ``byos`` ---

def test_doctor_checks_env_missing_warns_and_skips_probe():
    calls = []

    def probe(provider):  # pragma: no cover - must never run without env
        calls.append(provider)
        return {"ok": True}

    items = doctor_checks(_ByosConfig(), env={}, probe=probe)
    assert [item["label"] for item in items] == [
        "byos env present", "byos probe reachable",
    ]
    env_item, probe_item = items
    assert env_item["ok"] is False and env_item["severity"] == "warning"
    # T1 §5: detail carries adapter name + status enum + env NAME only.
    assert env_item["detail"] == f"{BYOS_NAME}: {ENV_MISSING} ({KEY_ENV})"
    assert probe_item["detail"] == f"{BYOS_NAME}: skipped"
    assert calls == []


def test_doctor_checks_env_ok_reports_each_probe_status():
    env = {
        KEY_ENV: FAKE_KEY,
        BASE_URL_ENV: "https://<byos-openai-compat-host>/v1",
        MODEL_ENV: "sub-model",
    }
    ok = doctor_checks(_ByosConfig(), env=env, probe=lambda p: {"ok": True})
    assert ok[1]["detail"] == f"{BYOS_NAME}: ok"
    assert ok[1]["severity"] == "ok"

    failed = doctor_checks(_ByosConfig(), env=env, probe=lambda p: {"ok": False})
    assert failed[1]["detail"] == f"{BYOS_NAME}: failed"
    assert failed[1]["severity"] == "warning"

    def boom(provider):
        raise RuntimeError("boom")

    raised = doctor_checks(_ByosConfig(), env=env, probe=boom)
    assert raised[1]["detail"] == f"{BYOS_NAME}: failed"

    # A custom gateway without its required base-url env resolves to None:
    # the probe is honestly skipped, never reported as failed.
    custom = _ByosConfig(adapters=["byos-custom"])
    skipped = doctor_checks(custom, env={"OFR_BYOS_CUSTOM_API_KEY": FAKE_KEY},
                            probe=lambda p: {"ok": True})
    assert skipped[0]["detail"] == "byos-custom: present"
    assert skipped[1]["detail"] == "byos-custom: skipped"
    assert skipped[1]["severity"] == "warning"


def test_doctor_items_never_carry_env_values():
    items = doctor_checks(_ByosConfig(), env={
        KEY_ENV: FAKE_KEY,
        BASE_URL_ENV: "https://<byos-openai-compat-host>/v1",
        MODEL_ENV: "sub-model",
    }, probe=lambda p: {"ok": True})
    assert FAKE_KEY not in json.dumps(items, ensure_ascii=False)
    assert BYOS_NAME in json.dumps(items, ensure_ascii=False)


def test_doctor_checks_empty_or_disabled_config_yields_nothing():
    assert doctor_checks(_ByosConfig(enabled=False), env={}) == []
    assert doctor_checks(_ByosConfig(adapters=[]), env={}) == []


def test_doctor_summary_counts_byos_group_without_new_code():
    ok = {"ok": True, "severity": "ok", "label": "byos env present",
          "detail": f"{BYOS_NAME}: present"}
    warn = {"ok": False, "severity": "warning", "label": "byos env present",
            "detail": f"{BYOS_NAME}: {ENV_MISSING}"}
    # A missing env degrades the verdict to warn at warning level...
    assert doctor_summary_from_report({"checks": [ok], "byos": [warn]}) == {
        "doctor_verdict": "warn", "doctor_failed_checks": 0,
    }
    # ...and a healthy BYOS block keeps the report a pass.
    assert doctor_summary_from_report({"checks": [ok], "byos": [ok]}) == {
        "doctor_verdict": "pass", "doctor_failed_checks": 0,
    }


def test_doctor_json_report_includes_byos_group(tmp_path, monkeypatch, capsys):
    """cmd_doctor --json carries the group and feeds the same summary."""
    from types import SimpleNamespace

    from open_free_router import cli

    (tmp_path / "registry.yaml").write_text(
        "free-a:\n"
        "  prefix: fa\n"
        "  api_key: test-placeholder\n"
        "  upstream_url: https://upstream.example.test/v1\n"
        "  models:\n"
        "    - id: m1\n"
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "registry: registry.yaml\n"
        f"data_dir: {tmp_path / 'data'}\n"
        "byos:\n"
        f"  adapters:\n    - {BYOS_NAME}\n"
    )
    monkeypatch.setenv("OPEN_FREE_ROUTER_CONFIG", str(config))
    monkeypatch.setenv(KEY_ENV, FAKE_KEY)
    monkeypatch.setattr(
        "open_free_router.cli._status_payload",
        lambda cfg: {
            "providers": 1, "providers_with_key": 1, "models": 1,
            "proxy_reachable": True, "ui_reachable": True,
            "proxy_url": "http://127.0.0.1:8337",
            "ui_url": "http://127.0.0.1:9057",
        },
    )
    # Warnings never fail doctor, so the run completes without SystemExit.
    cli.cmd_doctor(SimpleNamespace(json=True))
    report = json.loads(capsys.readouterr().out)
    labels = [item["label"] for item in report["byos"]]
    assert "byos env present" in labels
    assert "byos probe reachable" in labels
    # The sandbox endpoint is unreachable from the test run: warning-level
    # only, so the report overall stays a pass (0 errors).
    assert report["ok"] is True
    assert FAKE_KEY not in json.dumps(report, ensure_ascii=False)

# ---------------------------------------------------- T1 §5 sync/refresh row ---

def test_refresh_skips_byos_providers_without_warning(capsys):
    registry = _byos_registry(9, extra={
        "mystery-free": _free_provider(9),
    })
    results = refresh(registry)
    output = capsys.readouterr().out
    assert results[BYOS_NAME] is False
    # The env-derived gateway is silently skipped; the genuinely unknown
    # free provider still gets its usual warning.
    assert "No refresh source for provider: mystery-free" in output
    assert BYOS_NAME not in output
    assert refresh(registry, provider_name=BYOS_NAME) == {BYOS_NAME: False}
    assert BYOS_NAME not in capsys.readouterr().out


# ---------------------------- T1 §5 quota/ledger rows: 换路与用量可记账 ---

def test_byos_failover_writes_paired_audit_rows_with_honest_quota(tmp_path, monkeypatch):
    failing = _start(type("Failing", (_Handler,), {"status": 429, "ratelimit": {}}))
    healthy = _start(type("Healthy", (_Handler,), {"status": 200, "ratelimit": {}}))
    monkeypatch.setenv(KEY_ENV, FAKE_KEY)
    analytics = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    decisions = RouteDecisionStore(analytics=analytics)
    try:
        registry = _byos_registry(failing.server_address[1], extra={
            "free-b": _free_provider(healthy.server_address[1]),
        })
        result = _executor(registry, decisions, analytics).execute(
            "auto", "chat/completions", {"messages": []}
        )
        assert isinstance(result, OpenedRoute)
        assert result.target.provider_name == "free-b"

        decision_rows, outcome_rows = _ledger(analytics)
        assert len(decision_rows) == 1 and len(outcome_rows) == 1
        decision, outcome = decision_rows[0], outcome_rows[0]
        # The subscribed bucket went first, so the pair starts at the BYOS
        # gateway; no new columns or enums were needed to say so.
        assert decision["from_candidate"] == "byos-oc/sub-model"
        assert decision["to_candidate"] == "fb/model"
        assert decision["reason"] == "rate_limited"
        assert decision["strategy"] == "priority"
        # 429 without any rate-limit header: honest "event" evidence, no
        # invented balance (T1 §5: 不猜订阅余额).
        assert decision["quota_band"] == "unknown"
        assert decision["quota_source"] == "event"
        assert decision["quota_classification"] == "rate_limited"
        assert outcome["decision_id"] == decision["decision_id"]
        assert outcome["outcome"] == "success"
        assert outcome["attempt"] == 2
        # No key material anywhere in the ledger (T1 §6 redline 2).
        assert FAKE_KEY not in _whole_database_text(analytics)
        result.close()
    finally:
        analytics.close()
        failing.shutdown()
        healthy.shutdown()


def test_byos_rate_limit_headers_flow_through_quota_channel(tmp_path, monkeypatch):
    failing = _start(type("Failing", (_Handler,), {"status": 429, "ratelimit": {
        "x-ratelimit-limit": "100",
        "x-ratelimit-remaining": "0",
    }}))
    healthy = _start(type("Healthy", (_Handler,), {"status": 200, "ratelimit": {}}))
    monkeypatch.setenv(KEY_ENV, FAKE_KEY)
    analytics = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    decisions = RouteDecisionStore(analytics=analytics)
    try:
        registry = _byos_registry(failing.server_address[1], extra={
            "free-b": _free_provider(healthy.server_address[1]),
        })
        result = _executor(registry, decisions, analytics).execute(
            "auto", "chat/completions", {"messages": []}
        )
        assert isinstance(result, OpenedRoute)
        decision_rows, _ = _ledger(analytics)
        # Structured numeric headers land in the SAME PRD-096 channel: the
        # binding dimension is exhausted and the source is upstream_headers.
        assert decision_rows[0]["quota_band"] == "exhausted"
        assert decision_rows[0]["quota_source"] == "upstream_headers"
        result.close()
    finally:
        analytics.close()
        failing.shutdown()
        healthy.shutdown()


def test_byos_success_usage_lands_in_the_existing_ledger(tmp_path, monkeypatch):
    serving = _start(type("Serving", (_Handler,), {"status": 200, "ratelimit": {
        "x-ratelimit-limit": "100",
        "x-ratelimit-remaining": "50",
    }}))
    monkeypatch.setenv(KEY_ENV, FAKE_KEY)
    analytics = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    decisions = RouteDecisionStore(analytics=analytics)
    try:
        registry = _byos_registry(serving.server_address[1])
        result = _executor(registry, decisions, analytics).execute(
            "auto", "chat/completions", {"messages": []}
        )
        assert isinstance(result, OpenedRoute)
        assert result.target.provider_name == BYOS_NAME
        # The proxy always closes routes through ``complete``; the BYOS
        # request therefore lands in the same usage_events table.
        decisions.complete(result.request_id, 12.5)
        rows = analytics._rows()
        assert [row["provider"] for row in rows] == [BYOS_NAME]
        assert rows[0]["status"] == "success"
        assert rows[0]["model"] == "byos-oc/sub-model"
        # No key material anywhere in the ledger (T1 §6 redline 2).
        assert FAKE_KEY not in _whole_database_text(analytics)
        result.close()
    finally:
        analytics.close()
        serving.shutdown()


def test_byos_quota_panel_reflects_recorded_headers(tmp_path, monkeypatch):
    serving = _start(type("Serving", (_Handler,), {"status": 200, "ratelimit": {
        "x-ratelimit-limit": "100",
        "x-ratelimit-remaining": "50",
    }}))
    monkeypatch.setenv(KEY_ENV, FAKE_KEY)
    analytics = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    decisions = RouteDecisionStore(analytics=analytics)
    try:
        registry = _byos_registry(serving.server_address[1])
        resilience = ResilienceManager()
        planner = RoutePlanner(
            registry, {"fallback": {"max_attempts": 5}}, prefer_subscribed=True
        )
        result = UpstreamExecutor(
            planner, resilience, decisions=decisions, timeout=5,
        ).execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        panel = quota_view(resilience.snapshot(), providers=[BYOS_NAME])
        row = panel["items"][0]
        assert row["provider"] == BYOS_NAME
        assert row["band"] == "sufficient"
        assert row["source"] == "upstream_headers"
        result.close()
    finally:
        analytics.close()
        serving.shutdown()
