import json

import yaml

from open_free_router.analytics import AnalyticsStore
from open_free_router.config import Config
from open_free_router.mcp_server import McpServer
from open_free_router.proxy import run_proxy
from open_free_router.quota import parse_quota_headers
from open_free_router.registry import Registry
from open_free_router.resilience import ResilienceManager
from open_free_router.telemetry import RouteDecisionStore


def _call(server, name, arguments=None):
    response = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    })
    result = response["result"]
    return json.loads(result["content"][0]["text"]), result.get("isError", False)


def test_mcp_read_only_tools_use_authenticated_redacted_proxy_apis(tmp_path):
    registry_data = {
        "provider": {
            "prefix": "p", "api_key": "upstream-secret",
            "upstream_url": "https://example.com/v1",
            "free_tier": {
                "type": "recurring_quota", "limit": 100, "unit": "requests/day",
                "reset_period": "daily", "evidence_url": "https://example.com/free",
                "verified_at": "2026-08-01T00:00:00Z",
                "expires_at": "2099-08-01T00:00:00Z",
            },
            "models": [{"id": "model"}],
        }
    }
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(yaml.safe_dump(registry_data))
    registry = Registry(registry_data)
    resilience = ResilienceManager()
    resilience.record_quota(
        "provider", 0,
        parse_quota_headers({"X-RateLimit-Limit-Requests": "100",
                             "X-RateLimit-Remaining-Requests": "60"}, 200),
    )
    analytics = AnalyticsStore(tmp_path / "usage.db", retention_days=30)
    decisions = RouteDecisionStore(analytics=analytics)
    decisions.record(
        request_id="local-request", requested_model="auto", strategy="priority",
        status="failed", status_code=503, attempts=1, rejected=[],
        provider="provider", model="p/model", error_kind="provider_unavailable",
    )
    proxy, _ = run_proxy(
        registry, host="127.0.0.1", port=0, auth_token="proxy-token",
        resilience=resilience, decisions=decisions, analytics=analytics,
    )
    try:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.safe_dump({
            "registry": str(registry_path),
            "proxy": {"host": "127.0.0.1", "port": proxy.server_address[1]},
        }))
        (tmp_path / "proxy.token").write_text("proxy-token\n")
        server = McpServer(Config(config_path))

        resilience_payload, error = _call(server, "get_resilience")
        assert not error and "provider:slot-0" in resilience_payload["credentials"]
        quota, error = _call(server, "check_quota", {"provider": "provider"})
        assert not error
        assert quota["runtime"]["credential_slots"][0]["quota"]["requests"]["remaining"] == 60
        metrics, error = _call(server, "get_metrics", {"days": 7})
        assert not error and metrics["overall"]["requests"] == 1

        serialized = json.dumps([resilience_payload, quota, metrics])
        assert "upstream-secret" not in serialized
        assert "proxy-token" not in serialized
        assert "local-request" not in serialized
    finally:
        proxy.shutdown()
        analytics.close()
