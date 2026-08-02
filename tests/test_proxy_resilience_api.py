import http.client
import json

from open_free_router.proxy import run_proxy
from open_free_router.registry import Registry
from open_free_router.resilience import ResilienceManager, classify_failure


def test_resilience_snapshot_requires_proxy_token_and_is_redacted():
    manager = ResilienceManager()
    manager.record_failure(
        "provider", "model", classify_failure(401), credential_slot=2
    )
    server, _ = run_proxy(
        Registry({}), host="127.0.0.1", port=0,
        auth_token="local-proxy-placeholder", resilience=manager,
    )
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("GET", "/api/resilience")
        response = conn.getresponse()
        response.read()
        assert response.status == 401

        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request(
            "GET", "/api/resilience",
            headers={"Authorization": "Bearer local-proxy-placeholder"},
        )
        response = conn.getresponse()
        body = response.read()
        payload = json.loads(body)
        assert response.status == 200
        assert "provider:slot-2" in payload["credentials"]
        lowered = body.lower()
        assert b"api_key" not in lowered
        assert b"authorization" not in lowered
        assert b"local-proxy-placeholder" not in body
    finally:
        server.shutdown()


def test_resilience_reset_is_authenticated_and_precise():
    manager = ResilienceManager(model_cooldown=100)
    manager.record_failure("provider", "m1", classify_failure(404), now=10)
    manager.record_failure("provider", "m2", classify_failure(404), now=10)
    server, _ = run_proxy(
        Registry({}), host="127.0.0.1", port=0,
        auth_token="local-proxy-placeholder", resilience=manager,
    )
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        body = json.dumps({"provider": "provider", "model": "m1"})
        conn.request(
            "POST", "/api/resilience/reset", body,
            {"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        response.read()
        assert response.status == 401

        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request(
            "POST", "/api/resilience/reset", body,
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer local-proxy-placeholder",
            },
        )
        response = conn.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["ok"] is True
        snapshot = manager.snapshot()
        assert "provider/m1" not in snapshot["models"]
        assert "provider/m2" in snapshot["models"]
    finally:
        server.shutdown()
