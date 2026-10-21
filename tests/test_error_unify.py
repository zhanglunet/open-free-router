"""PRD-060/T4: error normalization (translate error section + executor wiring).

Semantics are owned by PRD-059/T1 (docs/plans/failover-routing-policy.md
§1.3); classification is resilience.classify_failure's. These tests pin the
mapping, not the policy.
"""
import json
import pytest
import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from open_free_router import translate
from open_free_router.executor import UpstreamExecutor
from open_free_router.failover import TimeoutPhase
from open_free_router.registry import Registry
from open_free_router.resilience import ResilienceManager
from open_free_router.routing import RoutePlanner
from open_free_router.telemetry import RouteDecisionStore


def _norm(status, body=None, retry_after=None, phase=None, provider_id="nous",
          model="m", table=None):
    return translate.normalize_error(
        table or translate.get_table(provider_id),
        http_status=status,
        body=body,
        retry_after=retry_after,
        provider_name=provider_id,
        model_id=model,
        timeout_phase=phase,
    )


def _openai_envelope(message, code=None, type_="invalid_request_error"):
    err = {"message": message, "type": type_}
    if code is not None:
        err["code"] = code
    return json.dumps({"error": err}).encode()


def test_401_maps_to_auth_invalid():
    n = _norm(401, _openai_envelope("Incorrect API key provided", "invalid_api_key"))
    assert n.code == "AUTH_INVALID"
    assert n.decision_kind == "credential_invalid"
    assert n.http_status == 401
    assert n.retry_after_s is None
    assert "Incorrect API key" in n.message


def test_429_plain_maps_to_rate_limited():
    n = _norm(429, _openai_envelope("Rate limit reached for gpt-4"), retry_after="120")
    assert n.code == "RATE_LIMITED"
    assert n.decision_kind == "rate_limited"
    assert n.retry_after_s == 120.0


def test_429_http_date_retry_after_normalized_to_seconds():
    header = formatdate(time.time() + 60, usegmt=True)
    n = _norm(429, _openai_envelope("Rate limit reached"), retry_after=header)
    assert n.code == "RATE_LIMITED"
    assert 55.0 < n.retry_after_s <= 60.0


def test_429_quota_wording_maps_to_quota_exhausted():
    n = _norm(429, _openai_envelope("You exceeded your current quota, please check your plan"))
    assert n.code == "QUOTA_EXHAUSTED"
    assert n.decision_kind == "quota_exhausted"
    assert n.retry_after_s is None  # no header -> no wait


def test_retry_after_capped_at_3600():
    n = _norm(429, _openai_envelope("slow down"), retry_after="99999")
    assert n.retry_after_s == 3600.0


def test_5xx_family_maps_to_upstream_5xx():
    for status in (408, 500, 502, 503, 504):
        n = _norm(status, _openai_envelope("bad gateway"))
        assert n.code == "UPSTREAM_5XX", status
        assert n.decision_kind == "provider_unavailable", status


def test_402_403_404_mapping():
    assert _norm(402, _openai_envelope("Not enough credits")).code == "CREDITS_EXHAUSTED"
    assert _norm(403, _openai_envelope("Model not entitled")).code == "MODEL_FORBIDDEN"
    n = _norm(404, _openai_envelope("Model not found"), model="deepseek-r1")
    assert n.code == "MODEL_NOT_FOUND"
    assert n.model == "deepseek-r1"


def test_400_context_overflow_is_bad_request():
    n = _norm(400, _openai_envelope(
        "This model's maximum context length is 8192 tokens, however you requested 9000"))
    assert n.code == "BAD_REQUEST"
    assert n.decision_kind == "context_overflow"
    assert n.context_limit is None  # no built-in declares a machine-readable limit


def test_400_other_is_bad_request():
    n = _norm(400, _openai_envelope("Unrecognized request argument supplied: foo"))
    assert n.code == "BAD_REQUEST"
    assert n.decision_kind == "invalid_request"


def test_connect_and_ttfb_timeouts():
    for phase in ("connect", "ttfb", "total"):
        n = _norm(0, None, phase=phase)
        assert n.code == "TIMEOUT", phase
        assert n.decision_kind == "transport_error", phase
        assert n.timeout_phase == phase, phase
        assert n.http_status == 0


def test_connection_refused_is_not_a_timeout():
    n = _norm(0, None, phase=None)
    # §1.3 defines TIMEOUT for the timeout branch only: a refused
    # connection must not wear its name.
    assert n.code == "TRANSPORT_ERROR"
    assert n.decision_kind == "transport_error"
    assert n.timeout_phase is None


def test_timeout_phase_cleared_for_non_timeout_codes():
    n = _norm(500, _openai_envelope("boom"), phase="connect")
    assert n.code == "UPSTREAM_5XX"
    assert n.timeout_phase is None


def test_unknown_envelope_still_normalizes():
    n = _norm(503, b"Service Unavailable")
    assert n.code == "UPSTREAM_5XX"
    assert "Service Unavailable" in n.message


def test_empty_body_falls_back():
    n = _norm(500, b"")
    assert n.code == "UPSTREAM_5XX"
    assert n.message == "upstream error (status 500)"


def test_message_is_sanitized_one_liner():
    n = _norm(400, _openai_envelope("bad\nrequest\r\nwith\x00control" + "x" * 500))
    assert "\n" not in n.message and "\r" not in n.message
    assert len(n.message) <= 300


def test_input_body_not_mutated():
    body = _openai_envelope("nope")
    before = bytes(body)
    _norm(401, body)
    assert bytes(body) == before


def test_vendor_code_remap():
    table = translate.TranslationTable(
        provider_id="custom",
        error=translate.ErrorTable(code_remap={"insufficient_quota": "QUOTA_EXHAUSTED"}),
    )
    n = translate.normalize_error(
        table, http_status=400,
        body=_openai_envelope("no quota left", code="insufficient_quota"),
        retry_after=None, provider_name="custom", model_id="m",
    )
    assert n.code == "QUOTA_EXHAUSTED"


def test_unknown_provider_falls_back_to_default_table():
    n = _norm(401, _openai_envelope("bad key"), provider_id="brand-new-upstream")
    assert n.code == "AUTH_INVALID"


@pytest.mark.parametrize('error, phase, code', [
    (TimeoutPhase('connect'), 'connect', 'TIMEOUT'),
    (TimeoutPhase('ttfb'), 'ttfb', 'TIMEOUT'),
    (ConnectionRefusedError('refused'), None, 'TRANSPORT_ERROR'),
    (OSError('connect timed out'), None, 'TRANSPORT_ERROR'),
])
def test_executor_preserves_structured_timeout_phase(monkeypatch, error, phase, code):
    # PRD-059 replaced the deleted prose heuristic with TimeoutPhase at the
    # actual connect/getresponse boundary. Pin the executor/normalizer contract.
    registry = Registry({'p': {'api_key': 'placeholder', 'upstream_url': 'http://127.0.0.1:1/v1', 'models': [{'id': 'm'}]}})
    executor = UpstreamExecutor(RoutePlanner(registry), ResilienceManager())
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(executor, '_open', fail)
    result = executor.execute('p/m', 'chat/completions', {'messages': []})
    assert result.status == 502
    assert result.attempts == 1
    assert result.timeout_phase == phase
    assert result.normalized.timeout_phase == phase
    assert result.normalized.code == code
    assert result.normalized.decision_kind == 'transport_error'


# --- executor wiring: the client-facing response is unchanged, the
# normalized error rides along internally ---

class _Handler(BaseHTTPRequestHandler):
    status = 429
    body = b""
    extra_headers = {}

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        for k, v in type(self).extra_headers.items():
            self.send_header(k, v)
        body = type(self).body
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _run_executor(status, body, extra_headers=None):
    cls = type("H", (_Handler,), {
        "status": status, "body": body, "extra_headers": extra_headers or {}})
    server = ThreadingHTTPServer(("127.0.0.1", 0), cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        registry = Registry({
            "p": {
                "prefix": "p",
                "upstream_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
                "models": [{"id": "model", "tool_calling": True}],
                "api_key": "placeholder",
            }
        })
        decisions = RouteDecisionStore()
        result = UpstreamExecutor(
            RoutePlanner(registry), ResilienceManager(), timeout=5,
            decisions=decisions, sleeper=lambda delay: None,
        ).execute("auto", "chat/completions", {"messages": []})
        return result
    finally:
        server.shutdown()


def test_executor_429_attaches_normalized_error():
    body = _openai_envelope("Rate limit reached for gpt-4")
    result = _run_executor(429, body, {"Retry-After": "9"})
    # client-facing: unchanged
    assert result.status == 429
    assert result.body == body
    # internal: normalized
    assert result.normalized is not None
    assert result.normalized.code == "RATE_LIMITED"
    assert result.normalized.decision_kind == "rate_limited"
    assert result.normalized.retry_after_s == 9.0
    assert result.normalized.provider == "p"


def test_executor_500_attaches_normalized_error():
    body = _openai_envelope("upstream exploded")
    result = _run_executor(500, body)
    assert result.status == 500
    assert result.body == body
    assert result.normalized.code == "UPSTREAM_5XX"
    assert result.normalized.retry_after_s is None


def test_executor_401_attaches_normalized_error():
    body = _openai_envelope("Incorrect API key provided")
    result = _run_executor(401, body)
    assert result.status == 401
    assert result.normalized.code == "AUTH_INVALID"
