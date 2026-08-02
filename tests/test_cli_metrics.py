import json
import stat
from types import SimpleNamespace

from open_free_router.analytics import AnalyticsStore
from open_free_router.cli import cmd_metrics
from open_free_router.proxy import run_proxy
from open_free_router.registry import Registry
from open_free_router.telemetry import RouteDecisionStore


def test_metrics_cli_reads_and_exports_redacted_local_analytics(tmp_path, monkeypatch, capsys):
    analytics = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    decisions = RouteDecisionStore(analytics=analytics)
    decisions.record(
        request_id="local", requested_model="auto", strategy="priority",
        status="failed", status_code=503, attempts=1, rejected=[],
        provider="provider", model="p/model", error_kind="provider_unavailable",
    )
    proxy, _ = run_proxy(
        Registry({}), host="127.0.0.1", port=0, auth_token="local-token",
        decisions=decisions, analytics=analytics,
    )
    try:
        config = tmp_path / "config.yaml"
        config.write_text(
            f"proxy:\n  host: 127.0.0.1\n  port: {proxy.server_address[1]}\n"
        )
        (tmp_path / "proxy.token").write_text("local-token\n")
        monkeypatch.setenv("OPEN_FREE_ROUTER_CONFIG", str(config))

        capsys.readouterr()
        cmd_metrics(SimpleNamespace(days=7, json=True, export=None, output=None))
        payload = json.loads(capsys.readouterr().out)
        assert payload["overall"]["requests"] == 1

        output = tmp_path / "metrics.csv"
        cmd_metrics(SimpleNamespace(days=7, json=False, export="csv", output=str(output)))
        assert "timestamp,provider,model" in output.read_text()
        assert "local-token" not in output.read_text()
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
    finally:
        proxy.shutdown()
        analytics.close()
