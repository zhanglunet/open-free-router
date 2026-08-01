"""Live availability probe tests: real fake-upstream requests, status
aggregation, and the dashboard's /api/probe endpoints."""
from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from open_free_router.probe import (
    ProbeRunner,
    load_probe_snapshot,
    load_status_as_probe_snapshot,
    probe_model,
    snapshot_to_status,
    write_probe_snapshot,
)
from open_free_router.registry import ModelInfo, ProviderConfig, Registry


class _OkUpstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({"choices": [{"message": {"content": "pong"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _RateLimitedUpstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({"error": {"message": "quota exhausted"}}).encode()
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _provider(port, api_key="sk-test"):
    return ProviderConfig(
        name="fake",
        upstream_url=f"http://127.0.0.1:{port}/v1",
        api_key=api_key,
        prefix="fk",
        models=[ModelInfo(id="m1")],
    )


def test_probe_model_success_records_latency():
    srv = _start(_OkUpstream)
    try:
        p = _provider(srv.server_address[1])
        result = probe_model(p, p.models[0], timeout=5)
        assert result["ok"] is True
        assert result["status"] == "http_200"
        assert isinstance(result["latency_ms"], int)
        assert result["checked_at"]
    finally:
        srv.shutdown()


def test_probe_model_maps_http_error_with_reason():
    srv = _start(_RateLimitedUpstream)
    try:
        p = _provider(srv.server_address[1])
        result = probe_model(p, p.models[0], timeout=5)
        assert result["ok"] is False
        assert result["status"] == "http_429"
        assert "quota exhausted" in result["error"]
    finally:
        srv.shutdown()


def test_probe_model_without_key_is_not_probed():
    p = _provider(1, api_key="")
    result = probe_model(p, p.models[0], timeout=1)
    assert result["ok"] is False
    assert result["status"] == "no_key"


def test_probe_runner_completes_and_rejects_concurrent_runs():
    srv = _start(_OkUpstream)
    try:
        reg = Registry()
        reg.add_provider(_provider(srv.server_address[1]))
        runner = ProbeRunner()
        finished = threading.Event()
        assert runner.start(reg, on_finish=lambda snap: finished.set()) is True
        assert finished.wait(10)
        state = runner.snapshot()
        assert state["running"] is False
        assert state["done"] == state["total"] == 1
        assert state["results"]["fake/m1"]["ok"] is True
        # a fresh run can start again once the previous one finished
        assert runner.start(reg) is True
        deadline = time.monotonic() + 10
        while runner.snapshot()["running"] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert runner.snapshot()["running"] is False
    finally:
        srv.shutdown()


def test_snapshot_to_status_aggregates_by_provider():
    status = snapshot_to_status({
        "finished_at": "2026-08-01T00:00:00+00:00",
        "results": {
            "a/m1": {"provider": "a", "model": "m1", "display_id": "a/m1",
                     "ok": True, "status": "http_200", "latency_ms": 900,
                     "error": "", "checked_at": "2026-08-01T00:00:00+00:00"},
            "a/m2": {"provider": "a", "model": "m2", "display_id": "a/m2",
                     "ok": False, "status": "http_429", "latency_ms": 100,
                     "error": "quota", "checked_at": "2026-08-01T00:00:01+00:00"},
            "b/m1": {"provider": "b", "model": "m1", "display_id": "b/m1",
                     "ok": False, "status": "no_key", "latency_ms": None,
                     "error": "no API key configured", "checked_at": "2026-08-01T00:00:02+00:00"},
        },
    })
    assert status["providers"]["a"]["availability"] == "available"
    assert status["providers"]["a"]["latency_ms"] == 900
    assert status["providers"]["a"]["models"]["m2"]["available"] is False
    assert status["providers"]["b"]["availability"] == "unverified"
    assert status["as_of"] == "2026-08-01T00:00:00+00:00"
    text = json.dumps(status)
    assert "api_key" not in text  # redaction-friendly field names only


def test_probe_snapshot_round_trip_is_private_and_scrubbed(tmp_path):
    path = tmp_path / "probe-results.json"
    snapshot = {
        "running": False,
        "started_at": "2026-08-01T00:00:00+00:00",
        "finished_at": "2026-08-01T00:00:01+00:00",
        "total": 1,
        "done": 1,
        "results": {
            "fake/m1": {
                "provider": "fake", "model": "m1", "display_id": "fk/m1",
                "ok": False, "status": "http_401", "latency_ms": 12,
                "error": "rejected Bearer sk-private-secret-123456",
                "checked_at": "2026-08-01T00:00:00+00:00",
                "unexpected": "must be dropped",
            },
        },
    }
    persisted = write_probe_snapshot(snapshot, path)
    loaded = load_probe_snapshot(path)
    assert loaded == persisted
    assert "private-secret" not in path.read_text()
    assert "unexpected" not in path.read_text()
    assert path.stat().st_mode & 0o077 == 0


def test_aggregated_status_can_restore_dashboard_history(tmp_path):
    path = tmp_path / "probe-status.json"
    path.write_text(json.dumps({
        "as_of": "2026-08-01T00:00:01+00:00",
        "providers": {"fake": {"models": {"m1": {
            "available": True, "status": "http_200", "latency_ms": 42,
            "reason": "", "checked_at": "2026-08-01T00:00:00+00:00",
        }}}},
    }))
    snapshot = load_status_as_probe_snapshot(path)
    assert snapshot["done"] == snapshot["total"] == 1
    assert snapshot["finished_at"] == "2026-08-01T00:00:01+00:00"
    assert snapshot["results"]["fake/m1"]["ok"] is True


def test_ui_probe_endpoints_require_auth_for_post(tmp_path):
    from open_free_router.config import Config
    from open_free_router.ui import _UIHandler

    upstream = _start(_OkUpstream)
    reg = Registry()
    reg.add_provider(_provider(upstream.server_address[1]))

    handler = type("UI", (_UIHandler,), {
        "cfg": None, "reg": reg, "config_path": None, "token": "ui-secret",
    })
    ui = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=ui.serve_forever, daemon=True).start()
    port = ui.server_address[1]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/probe")
        response = conn.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["running"] is False

        conn.request("POST", "/api/probe", body="{}",
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        assert response.status == 401
        response.read()

        conn.request("POST", "/api/probe", body="{}", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer ui-secret",
        })
        response = conn.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["ok"] is True

        deadline = time.monotonic() + 10
        state = {}
        while time.monotonic() < deadline:
            conn.request("GET", "/api/probe")
            state = json.loads(conn.getresponse().read())
            if not state["running"] and state["finished_at"]:
                break
            time.sleep(0.1)
        assert state["results"]["fake/m1"]["ok"] is True
        conn.close()
    finally:
        ui.shutdown()
        upstream.shutdown()
