"""Tests for auth.py and the UI's enforcement of it on write endpoints."""
import http.client
import http.server
import json
import stat
import threading
import time

from open_free_router import ui
from open_free_router.auth import check_auth, get_or_create_proxy_token, get_or_create_token
from open_free_router.config import Config
from open_free_router.registry import ModelInfo, ProviderConfig, Registry
from open_free_router.proxy import run_proxy
from open_free_router.resilience import ResilienceManager, classify_failure
from open_free_router.telemetry import RouteDecisionStore


class _FakeHeaders(dict):
    def get(self, key, default=""):
        return super().get(key, default)


def test_get_or_create_token_creates_file_once(tmp_path):
    tok1 = get_or_create_token(tmp_path)
    assert tok1
    path = tmp_path / "ui.token"
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600

    # Second call must return the same token, not regenerate it.
    tok2 = get_or_create_token(tmp_path)
    assert tok1 == tok2


def test_proxy_token_is_distinct_and_owner_only(tmp_path):
    ui_token = get_or_create_token(tmp_path)
    proxy_token = get_or_create_proxy_token(tmp_path)
    assert proxy_token != ui_token
    assert stat.S_IMODE((tmp_path / "proxy.token").stat().st_mode) == 0o600


def test_check_auth_accepts_valid_bearer_token():
    headers = _FakeHeaders({"Authorization": "Bearer secret123"})
    assert check_auth(headers, "secret123") is True


def test_check_auth_rejects_missing_or_wrong_token():
    assert check_auth(_FakeHeaders({}), "secret123") is False
    assert check_auth(_FakeHeaders({"Authorization": "Bearer wrong"}), "secret123") is False
    assert check_auth(_FakeHeaders({"Authorization": "secret123"}), "secret123") is False  # no "Bearer "


def test_check_auth_false_when_server_has_no_token():
    headers = _FakeHeaders({"Authorization": "Bearer whatever"})
    assert check_auth(headers, "") is False


# ---------------------------------------------------------------------------
# End-to-end: real HTTP server, real requests, exercising do_POST's auth gate.
# ---------------------------------------------------------------------------

def _start_ui_server(tmp_path, token="test-token"):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("proxy:\n  host: 127.0.0.1\n  port: 8337\nui:\n  host: 127.0.0.1\n  port: 0\n")
    cfg = Config(config_path=config_path)
    reg = Registry({})

    ui._UIHandler.cfg = cfg
    ui._UIHandler.reg = reg
    ui._UIHandler.config_path = cfg.path
    ui._UIHandler.token = token

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ui._UIHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    return srv, srv.server_address[1]


def test_post_without_token_is_rejected(tmp_path):
    srv, port = _start_ui_server(tmp_path)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/api/refresh", body="{}",
                      headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 401
        body = json.loads(resp.read())
        assert body["error"] == "unauthorized"
    finally:
        srv.shutdown()


def test_post_with_wrong_token_is_rejected(tmp_path):
    srv, port = _start_ui_server(tmp_path)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/api/refresh", body="{}",
                      headers={"Content-Type": "application/json",
                                "Authorization": "Bearer nope"})
        resp = conn.getresponse()
        assert resp.status == 401
        resp.read()
    finally:
        srv.shutdown()


def test_post_with_correct_token_is_accepted(tmp_path):
    srv, port = _start_ui_server(tmp_path, token="right-token")
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/api/providers", body=json.dumps({
            "name": "test-provider",
            "base_url": "http://127.0.0.1:8337/v1",
            "upstream_url": "https://example.com/v1",
            "api_key": "sk-test",
            "models": ["m1"],
        }), headers={"Content-Type": "application/json",
                     "Authorization": "Bearer right-token"})
        resp = conn.getresponse()
        body = json.loads(resp.read())
        assert resp.status == 200
        assert body["ok"] is True
    finally:
        srv.shutdown()


def test_get_endpoints_do_not_require_auth(tmp_path):
    """Read-only endpoints stay open — only POST is gated."""
    srv, port = _start_ui_server(tmp_path)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/status")
        resp = conn.getresponse()
        assert resp.status == 200
        resp.read()
    finally:
        srv.shutdown()


def test_status_exposes_dashboard_summary_without_credentials(tmp_path):
    srv, port = _start_ui_server(tmp_path)
    try:
        ui._UIHandler.reg.add_provider(ProviderConfig(
            name="secret-provider",
            upstream_url="https://example.test/v1",
            api_key="sk-must-never-reach-dashboard",
            prefix="sp",
            auto_refresh=True,
            models=[ModelInfo(id="free-model", reasoning=True, tool_calling=True)],
        ))
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/status")
        resp = conn.getresponse()
        assert resp.status == 200
        payload = json.loads(resp.read())
        assert payload["summary"] == {
            "provider_count": 1,
            "model_count": 1,
            "credential_count": 1,
            "auto_refresh_count": 1,
        }
        assert payload["providers"][0]["credential_configured"] is True
        assert payload["providers"][0]["prefix"] == "sp"
        assert "must-never-reach-dashboard" not in json.dumps(payload)
        assert "codex" in payload["clients"]
        assert "Anthropic Messages" in payload["service"]["protocols"]
    finally:
        srv.shutdown()


def test_dashboard_routing_api_reads_proxy_and_forwards_authenticated_reset(tmp_path):
    proxy_token = get_or_create_proxy_token(tmp_path)
    manager = ResilienceManager(model_cooldown=300)
    manager.record_failure("provider", "model", classify_failure(404))
    decisions = RouteDecisionStore()
    decisions.record(
        request_id="ofr_ui", requested_model="auto", strategy="virtual",
        status="failed", status_code=503, attempts=1, rejected=[],
    )
    proxy, _ = run_proxy(
        Registry({}), host="127.0.0.1", port=0, auth_token=proxy_token,
        resilience=manager, decisions=decisions,
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"proxy:\n  host: 127.0.0.1\n  port: {proxy.server_address[1]}\n"
        "ui:\n  host: 127.0.0.1\n  port: 0\n"
    )
    cfg = Config(config_path=config_path)
    ui._UIHandler.cfg = cfg
    ui._UIHandler.reg = Registry({})
    ui._UIHandler.config_path = cfg.path
    ui._UIHandler.token = "ui-token"
    dashboard = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ui._UIHandler)
    threading.Thread(target=dashboard.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", dashboard.server_address[1], timeout=5)
        conn.request("GET", "/api/routing")
        response = conn.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200
        assert payload["routes"]["items"][0]["request_id"] == "ofr_ui"
        assert "provider/model" in payload["resilience"]["models"]

        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", dashboard.server_address[1], timeout=5)
        conn.request(
            "POST", "/api/routing/reset", json.dumps({"provider": "provider", "model": "model"}),
            {"Content-Type": "application/json", "Authorization": "Bearer ui-token"},
        )
        response = conn.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["ok"] is True
        assert manager.snapshot()["models"] == {}
    finally:
        dashboard.shutdown()
        proxy.shutdown()
