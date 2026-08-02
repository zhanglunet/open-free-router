import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from open_free_router.executor import OpenedRoute, RouteFailure, UpstreamExecutor
from open_free_router.registry import Registry
from open_free_router.resilience import ResilienceManager
from open_free_router.routing import RoutePlanner
from open_free_router.telemetry import RouteDecisionStore


class _ConfiguredHandler(BaseHTTPRequestHandler):
    status = 200
    seen_auth = []

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length))
        type(self).seen_auth.append(self.headers.get("Authorization"))
        if self.status >= 400:
            body = json.dumps({"error": {"message": "rate limited"}}).encode()
        else:
            body = json.dumps({"model": request["model"], "ok": True}).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.status == 429:
            self.send_header("Retry-After", "9")
        self.end_headers()
        self.wfile.write(body)


def _handler(status):
    return type(f"Handler{status}", (_ConfiguredHandler,), {"status": status, "seen_auth": []})


def _start(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _provider(port, prefix, keys=None):
    value = {
        "prefix": prefix,
        "upstream_url": f"http://127.0.0.1:{port}/v1",
        "models": [{"id": "model", "tool_calling": True}],
    }
    if keys is None:
        value["api_key"] = f"placeholder-{prefix}"
    else:
        value["api_keys"] = keys
    return value


def test_virtual_route_falls_back_before_returning_response():
    limited_handler = _handler(429)
    healthy_handler = _handler(200)
    limited = _start(limited_handler)
    healthy = _start(healthy_handler)
    try:
        registry = Registry({
            "limited": _provider(limited.server_address[1], "a"),
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        decisions = RouteDecisionStore()
        result = UpstreamExecutor(
            RoutePlanner(registry), ResilienceManager(), timeout=5, decisions=decisions
        ).execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        try:
            assert result.target.provider_name == "healthy"
            assert result.attempts == 2
            assert limited_handler.seen_auth == ["Bearer placeholder-a"]
            assert healthy_handler.seen_auth == ["Bearer placeholder-b"]
            decision = decisions.get(result.request_id)
            assert decision["provider"] == "healthy"
            assert decision["fallback_attempts"] == 1
            assert decision["requested_model"] == "auto"
            assert json.loads(result.response.read())["model"] == "model"
        finally:
            result.close()
    finally:
        limited.shutdown()
        healthy.shutdown()


def test_explicit_model_does_not_switch_provider():
    limited_handler = _handler(429)
    healthy_handler = _handler(200)
    limited = _start(limited_handler)
    healthy = _start(healthy_handler)
    try:
        registry = Registry({
            "limited": _provider(limited.server_address[1], "a"),
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        result = UpstreamExecutor(
            RoutePlanner(registry), ResilienceManager(), timeout=5
        ).execute("a/model", "chat/completions", {"messages": []})
        assert isinstance(result, RouteFailure)
        assert result.status == 429
        assert result.attempts == 1
        assert healthy_handler.seen_auth == []
    finally:
        limited.shutdown()
        healthy.shutdown()


def test_bad_credential_switches_slot_without_switching_model():
    class CredentialHandler(_ConfiguredHandler):
        seen_auth = []

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            request = json.loads(self.rfile.read(length))
            auth = self.headers.get("Authorization")
            type(self).seen_auth.append(auth)
            status = 401 if auth == "Bearer bad-placeholder" else 200
            body = json.dumps(
                {"error": {"message": "invalid credential"}}
                if status == 401 else {"model": request["model"], "ok": True}
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = _start(CredentialHandler)
    try:
        registry = Registry({
            "provider": _provider(
                server.server_address[1], "p", ["bad-placeholder", "good-placeholder"]
            )
        })
        result = UpstreamExecutor(
            RoutePlanner(registry), ResilienceManager(), timeout=5
        ).execute("p/model", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        try:
            assert result.credential_slot == 1
            assert result.attempts == 2
            assert CredentialHandler.seen_auth == [
                "Bearer bad-placeholder", "Bearer good-placeholder"
            ]
            result.response.read()
        finally:
            result.close()
    finally:
        server.shutdown()


def test_quota_headers_are_recorded_and_resettable_exhaustion_cools_down():
    class QuotaHandler(_ConfiguredHandler):
        seen_auth = []

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            type(self).seen_auth.append(self.headers.get("Authorization"))
            body = json.dumps({"error": {"message": "daily quota exhausted"}}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-RateLimit-Limit-Requests", "100")
            self.send_header("X-RateLimit-Remaining-Requests", "0")
            self.send_header("X-RateLimit-Reset-Requests", "1d")
            self.end_headers()
            self.wfile.write(body)

    server = _start(QuotaHandler)
    try:
        registry = Registry({
            "provider": _provider(server.server_address[1], "p", ["placeholder"])
        })
        manager = ResilienceManager()
        result = UpstreamExecutor(
            RoutePlanner(registry), manager, timeout=5
        ).execute("p/model", "chat/completions", {"messages": []})
        assert isinstance(result, RouteFailure)
        state = manager.snapshot()["credentials"]["provider:slot-0"]
        assert state["state"] == "cooldown"
        assert state["reason"] == "quota_exhausted"
        assert state["quota"]["requests"]["remaining"] == 0
        assert state["quota"]["requests"]["reset_at"] > state["quota"]["observed_at"]
    finally:
        server.shutdown()


def test_invalid_request_never_falls_back():
    invalid_handler = _handler(400)
    healthy_handler = _handler(200)
    invalid = _start(invalid_handler)
    healthy = _start(healthy_handler)
    try:
        registry = Registry({
            "invalid": _provider(invalid.server_address[1], "a"),
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        result = UpstreamExecutor(
            RoutePlanner(registry), ResilienceManager(), timeout=5
        ).execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, RouteFailure)
        assert result.status == 400
        assert healthy_handler.seen_auth == []
    finally:
        invalid.shutdown()
        healthy.shutdown()


def test_virtual_route_skips_provider_without_credentials_before_network():
    unused_handler = _handler(200)
    healthy_handler = _handler(200)
    unused = _start(unused_handler)
    healthy = _start(healthy_handler)
    try:
        missing = _provider(unused.server_address[1], "a")
        missing["api_key"] = ""
        registry = Registry({
            "missing": missing,
            "healthy": _provider(healthy.server_address[1], "b"),
        })
        result = UpstreamExecutor(
            RoutePlanner(registry), ResilienceManager(), timeout=5
        ).execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        try:
            assert result.target.provider_name == "healthy"
            assert result.attempts == 1
            assert unused_handler.seen_auth == []
            result.response.read()
        finally:
            result.close()
    finally:
        unused.shutdown()
        healthy.shutdown()


def test_explicit_keyless_route_omits_authorization_header():
    handler = _handler(200)
    server = _start(handler)
    try:
        provider = _provider(server.server_address[1], "free")
        provider["api_key"] = ""
        provider["auth_mode"] = "none"
        result = UpstreamExecutor(
            RoutePlanner(Registry({"keyless": provider})), ResilienceManager(), timeout=5
        ).execute("free/model", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        try:
            result.response.read()
            assert handler.seen_auth == [None]
        finally:
            result.close()
    finally:
        server.shutdown()
