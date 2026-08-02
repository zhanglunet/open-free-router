import http.client
import json

from open_free_router.proxy import run_proxy
from open_free_router.registry import Registry
from open_free_router.telemetry import RouteDecisionStore


def test_route_history_is_bounded_and_redacted():
    store = RouteDecisionStore(max_entries=2)
    for index in range(3):
        store.record(
            request_id=f"ofr_{index}", requested_model="auto", strategy="virtual",
            status="success", status_code=200, provider="provider", model="p/model",
            attempts=2, rejected=[{"model": "x", "reason": "provider_circuit_open"}],
        )
    snapshot = store.snapshot()
    assert snapshot["total"] == 2
    assert [item["request_id"] for item in snapshot["items"]] == ["ofr_2", "ofr_1"]
    assert store.get("ofr_0") is None
    serialized = json.dumps(snapshot).lower()
    assert "prompt" not in serialized
    assert "authorization" not in serialized
    assert "api_key" not in serialized


def test_route_history_api_requires_token_and_supports_request_lookup():
    store = RouteDecisionStore()
    store.record(
        request_id="ofr_lookup", requested_model="auto", strategy="virtual",
        status="failed", status_code=503, attempts=0, rejected=[],
    )
    server, _ = run_proxy(
        Registry({}), host="127.0.0.1", port=0,
        auth_token="proxy-placeholder", decisions=store,
    )
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("GET", "/api/routes")
        response = conn.getresponse()
        response.read()
        assert response.status == 401
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("GET", "/api/routes/ofr_lookup", headers={"Authorization": "Bearer proxy-placeholder"})
        response = conn.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200
        assert payload["request_id"] == "ofr_lookup"
        assert payload["status"] == "failed"
    finally:
        server.shutdown()
