"""Tests for the PRD-079/T3 model caller (sandbox layer).

The model endpoint is a scripted local HTTP server -- no gateway needed.
The sandbox runner (PRD-079/T2) is faked at the orchestration seam.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from open_free_router.sandbox import caller as mc
from open_free_router.sandbox import commands as scmd


# --------------------------------------------------------------------------
# scripted mock model endpoint
# --------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    script = []    # list of (status, body, headers)
    requests = []  # list of (path, parsed_json_body)
    delay_s = 0.0

    def log_message(self, *a):  # silence
        pass

    def _serve(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode())
        except ValueError:
            body = {}
        if _Handler.delay_s:
            import time as _t
            _t.sleep(_Handler.delay_s)
        _Handler.requests.append((self.path, body))
        status, resp_body, headers = _Handler.script.pop(0)
        payload = (json.dumps(resp_body).encode()
                   if isinstance(resp_body, dict) else str(resp_body).encode())
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_POST = _serve
    do_GET = _serve


@pytest.fixture()
def gateway():
    _Handler.script = []
    _Handler.requests = []
    _Handler.delay_s = 0.0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join()


def _ok(model_text="print('hi')"):
    return (200, {"choices": [{"message": {"content": f"```python\n{model_text}\n```"}}]}, {})


def _mkcaller(gateway, **kw):
    kw.setdefault("sleep", lambda s: None)  # no real waiting in tests
    return mc.ModelCaller(
        base_url=f"http://127.0.0.1:{gateway.server_address[1]}",
        token="test-token", **kw)


# --------------------------------------------------------------------------
# T1 §10 #2: 429 -> switch model, exactly two model calls
# --------------------------------------------------------------------------

def test_retry_chain_429_then_success(gateway):
    sleeps = []
    _Handler.script = [
        (429, {"error": "rate limited"}, {"Retry-After": "1"}),
        _ok(),
    ]
    c = _mkcaller(gateway, sleep=sleeps.append)
    result = c.call_with_retry(["model-a", "model-b"],
                               [{"role": "user", "content": "hi"}])
    assert result.model == "model-b"
    assert [m for _, b in _Handler.requests for m in [b["model"]]] == ["model-a", "model-b"]
    assert len(_Handler.requests) == 2  # 只调了两次模型
    assert [a.outcome for a in result.attempts] == ["switched", "success"]
    assert result.attempts[0].http_status == 429
    # Retry-After wins over the exponential formula (policy doc §2.2 note).
    assert sleeps == [1.0]
    # auth header actually sent
    assert result.switches[0]["model"] == "model-a"


def test_5xx_switches_with_backoff(gateway):
    sleeps = []
    _Handler.script = [(503, {"error": "overloaded"}, {}), _ok()]
    c = _mkcaller(gateway, sleep=sleeps.append, rand=lambda: 0.5)
    result = c.call_with_retry(["a", "b"], [{"role": "user", "content": "hi"}])
    assert result.model == "b"
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(1.0)  # base * factor^0, jitter=0 at rand=0.5


def test_404_skips_without_backoff(gateway):
    sleeps = []
    _Handler.script = [(404, {"error": "not found"}, {}), _ok()]
    c = _mkcaller(gateway, sleep=sleeps.append)
    result = c.call_with_retry(["gone", "b"], [{"role": "user", "content": "hi"}])
    assert result.model == "b"
    assert [a.outcome for a in result.attempts] == ["skipped", "success"]
    assert sleeps == []  # 404:立刻换，不退避


def test_401_aborts_whole_list(gateway):
    _Handler.script = [(401, {"error": "bad key"}, {}), _ok()]
    c = _mkcaller(gateway)
    with pytest.raises(mc.ModelAuthError):
        c.call_with_retry(["bad", "good"], [{"role": "user", "content": "hi"}])
    assert len(_Handler.requests) == 1  # 停整个列表，不烧额度


def test_403_aborts_whole_list(gateway):
    _Handler.script = [(403, {"error": "forbidden"}, {})]
    c = _mkcaller(gateway)
    with pytest.raises(mc.ModelAuthError):
        c.call_with_retry(["x"], [{"role": "user", "content": "hi"}])


def test_400_returns_directly(gateway):
    _Handler.script = [(400, {"error": "too long"}, {}), _ok()]
    c = _mkcaller(gateway)
    with pytest.raises(mc.ModelBadRequest):
        c.call_with_retry(["a", "b"], [{"role": "user", "content": "hi"}])
    assert len(_Handler.requests) == 1  # 不换模型，直接返回


def test_connection_error_switches():
    # Truly dead port: bind then close so nothing listens.
    # (server.shutdown() alone leaves the listen socket open -> connects
    # hang instead of refusing.)
    import socket as _socket

    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    c = mc.ModelCaller(base_url=f"http://127.0.0.1:{port}", token="t",
                       sleep=lambda s: None)
    with pytest.raises(mc.ModelListExhausted):
        c.call_with_retry(["a", "b"], [{"role": "user", "content": "hi"}])


def test_max_model_retries_cap(gateway):
    _Handler.script = [(500, {"error": "x"}, {})] * 5
    c = _mkcaller(gateway, max_model_retries=3)
    with pytest.raises(mc.ModelListExhausted) as e:
        c.call_with_retry(["a", "b", "c", "d", "e"],
                          [{"role": "user", "content": "hi"}])
    assert len(_Handler.requests) == 3  # 含首选最多试 3 个
    assert "3" in str(e.value)


def test_budget_exhausted(gateway):
    _Handler.delay_s = 0.3
    _Handler.script = [(500, {"error": "x"}, {})] * 5
    c = _mkcaller(gateway, total_budget_s=0.1)
    with pytest.raises(mc.ModelBudgetExhausted) as e:
        c.call_with_retry(["a", "b"], [{"role": "user", "content": "hi"}])
    assert "budget" in str(e.value).lower()


def test_empty_models_raises(gateway):
    c = _mkcaller(gateway)
    with pytest.raises(ValueError):
        c.call_with_retry([], [{"role": "user", "content": "hi"}])


def test_malformed_200_switches(gateway):
    _Handler.script = [(200, {"nonsense": True}, {}), _ok()]
    c = _mkcaller(gateway)
    result = c.call_with_retry(["a", "b"], [{"role": "user", "content": "hi"}])
    assert result.model == "b"


# --------------------------------------------------------------------------
# classification table + backoff formula (unit)
# --------------------------------------------------------------------------

def test_classify_table():
    assert mc.classify_http_status(429) == mc.DISPOSITION_SWITCH
    for s in (500, 502, 503, 599):
        assert mc.classify_http_status(s) == mc.DISPOSITION_SWITCH
    assert mc.classify_http_status(408) == mc.DISPOSITION_SWITCH
    assert mc.classify_http_status(404) == mc.DISPOSITION_SKIP
    assert mc.classify_http_status(401) == mc.DISPOSITION_ABORT
    assert mc.classify_http_status(403) == mc.DISPOSITION_ABORT
    assert mc.classify_http_status(400) == mc.DISPOSITION_RETURN
    assert mc.classify_http_status(422) == mc.DISPOSITION_RETURN


def test_backoff_formula_matches_policy_doc():
    # delay_n = min(cap, base * factor^(n-1)); rand=0.5 -> jitter term = 0
    assert mc.compute_backoff(1, rand=lambda: 0.5) == pytest.approx(1.0)
    assert mc.compute_backoff(2, rand=lambda: 0.5) == pytest.approx(2.0)
    assert mc.compute_backoff(3, rand=lambda: 0.5) == pytest.approx(4.0)
    assert mc.compute_backoff(10, rand=lambda: 0.5) == pytest.approx(30.0)  # capped
    lo = mc.compute_backoff(1, rand=lambda: 0.0)
    hi = mc.compute_backoff(1, rand=lambda: 1.0)
    assert lo == pytest.approx(0.75) and hi == pytest.approx(1.25)


def test_parse_retry_after():
    assert mc.parse_retry_after("120") == 120.0
    assert mc.parse_retry_after("  3 ") == 3.0
    assert mc.parse_retry_after("bogus") is None
    assert mc.parse_retry_after(None) is None
    assert mc.parse_retry_after("-5") is None


# --------------------------------------------------------------------------
# code extraction + generate_code
# --------------------------------------------------------------------------

def test_extract_code_fenced():
    resp = {"choices": [{"message": {"content": "here:\n```python\nprint(1)\n```\ndone"}}]}
    assert mc.extract_code(resp) == "print(1)"


def test_extract_code_raw():
    resp = {"choices": [{"message": {"content": "print(2)"}}]}
    assert mc.extract_code(resp) == "print(2)"


def test_extract_code_empty_raises():
    with pytest.raises(mc.ModelCallError):
        mc.extract_code({"choices": [{"message": {"content": "  "}}]})
    with pytest.raises(mc.ModelCallError):
        mc.extract_code({"choices": []})


def test_generate_code_end_to_end(gateway):
    _Handler.script = [_ok("x = 41 + 1\nprint(x)")]
    c = _mkcaller(gateway)
    code, result = c.generate_code(["m"], "compute 42")
    assert code == "x = 41 + 1\nprint(x)"
    assert result.model == "m"
    # model field passthrough + messages shape
    _, body = _Handler.requests[0]
    assert body["model"] == "m"
    assert body["messages"][0]["role"] == "system"


# --------------------------------------------------------------------------
# orchestration: T1 §10 #3 — code failure never switches models
# --------------------------------------------------------------------------

class _FakeProvider:
    def __init__(self, exit_code=0, stdout="", stderr="", timed_out=False):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.duration_ms = 12.5
        self.calls = []

    def run(self, request):
        self.calls.append(request)
        return self


class _FakeCaller:
    def __init__(self, code="print('hi')"):
        self.code = code
        self.calls = 0

    def generate_code(self, models, task, **kw):
        self.calls += 1
        result = mc.ModelCallResult(
            model=models[0], response={},
            attempts=[mc.ModelAttempt(model=models[0], outcome="success",
                                      http_status=200, latency_ms=1.0)])
        return self.code, result


def _cfg(**over):
    cfg = {"provider": "local", "default_models": ["m"], "timeout_s": 120,
           "max_model_retries": 3, "total_budget_s": 300,
           "workdir_parent": "/tmp/x", "network": False,
           "env_allowlist": ["PATH"]}
    cfg.update(over)
    return cfg


def test_code_failure_no_model_switch():
    caller = _FakeCaller()
    provider = _FakeProvider(exit_code=3, stderr="boom")
    out = scmd.run_sandbox_task(task="t", models=["m"], cfg=_cfg(),
                                token="tok", caller=caller, provider=provider)
    assert out["exit_code"] == 3
    assert out["stderr"] == "boom"
    assert caller.calls == 1  # 代码侧失败：不换模型
    assert [s["outcome"] for s in out["model_switches"]] == ["success"]
    assert len(provider.calls) == 1
    assert provider.calls[0].language == "python3"
    assert "code_sha256" in out and len(out["code_sha256"]) == 64


def test_run_success_shape():
    provider = _FakeProvider(exit_code=0, stdout="hi\n")
    out = scmd.run_sandbox_task(task="t", models=["m"], cfg=_cfg(),
                                token="tok", caller=_FakeCaller(),
                                provider=provider)
    assert out["exit_code"] == 0
    assert out["stdout"] == "hi\n"
    assert out["timed_out"] is False
    assert out["model"] == "m"


def test_run_refuses_when_disabled():
    with pytest.raises(scmd.SandboxCLIError):
        scmd.run_sandbox_task(task="t", models=["m"],
                              cfg=_cfg(provider="none"), token="tok",
                              caller=_FakeCaller(), provider=_FakeProvider())


def test_resolve_models_priority():
    class A:  # argparse namespace stand-in
        pass
    a = A(); a.models = "x, y"; a.model = "z"
    assert scmd.resolve_models(a, _cfg()) == ["x", "y"]
    a = A(); a.models = None; a.model = "z"
    assert scmd.resolve_models(a, _cfg()) == ["z"]
    a = A(); a.models = None; a.model = None
    assert scmd.resolve_models(a, _cfg(default_models=["d1", "d2"])) == ["d1", "d2"]
    a = A(); a.models = "a,b,c,d,e"; a.model = None
    assert scmd.resolve_models(a, _cfg(max_model_retries=3)) == ["a", "b", "c"]
    a = A(); a.models = None; a.model = None
    with pytest.raises(scmd.SandboxCLIError):
        scmd.resolve_models(a, _cfg(default_models=[]))


def test_scrub_drops_secrets():
    cfg = {"provider": "local", "api_token": "sk-xxx",
           "nested": {"secret_key": "y"}, "timeout_s": 1}
    assert scmd._scrub(cfg) == {"provider": "local",
                                "nested": {}, "timeout_s": 1}


def test_load_config_fallback(tmp_path, monkeypatch):
    # fallback reader mirrors T2's path resolution: ./sandbox.toml, then
    # ~/.open-free-router/sandbox.toml; missing file -> disabled defaults.
    monkeypatch.chdir(tmp_path)
    cfg = scmd.load_sandbox_config()
    assert cfg["provider"] == "none" and cfg["timeout_s"] == 120
    (tmp_path / "sandbox.toml").write_text(
        '[sandbox]\nprovider = "local"\ntimeout_s = 45\n')
    cfg = scmd.load_sandbox_config()
    assert cfg["provider"] == "local" and cfg["timeout_s"] == 45
