"""PRD-173/T2: BYOS 适配可注册、缺 env 不注册、探活诚实、红名单拒收。

T1 契约（``docs/plans/2026-10-25-byos-access.md``，trend issue #865）：
§3.2 协议（resolve/probe）、§3.3 registry byos 元数据块、§3.5 失败语义、
§6 安全边界（密钥仅 env 变量名、值不进任何落盘字段）。所有端点均为
127.0.0.1 沙盒或 ``<byos-*-host>`` 占位符；所有密钥均为本测试自造的假密钥。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

from open_free_router.byos import ADAPTERS, TOS_NOTE_ZH, make_adapter
from open_free_router.byos.env import endpoint_is_clean, find_secret_leak
from open_free_router.registry import ByosMetadata, Registry

FAKE_KEY = "test-key-0123456789abcdef"  # 沙盒假密钥，不出现在任何真实请求中
OPENAI_ENV = {
    "OFR_BYOS_OPENAI_COMPAT_API_KEY": FAKE_KEY,
    "OFR_BYOS_OPENAI_COMPAT_BASE_URL": "https://<byos-openai-compat-host>/v1",
    "OFR_BYOS_OPENAI_COMPAT_MODEL": "test-model-large",
}
CUSTOM_ENV = {
    "OFR_BYOS_CUSTOM_API_KEY": FAKE_KEY,
    "OFR_BYOS_CUSTOM_BASE_URL": "https://<byos-custom-host>/v1",
}


# ------------------------------------------------ T1 §3.2 adapter surface ---

def test_t2_adapter_pair_is_registered_with_fixed_contract():
    assert set(ADAPTERS) == {"byos-openai-compat", "byos-custom"}
    for name in ADAPTERS:
        descriptor = make_adapter(name).descriptor
        assert descriptor.name == name
        assert descriptor.auth_mode == "bearer"
        assert descriptor.priority_class == "subscribed"
        # T1 §2 rule 3 / §6 redline 4: the ToS disclaimer is mandatory.
        assert descriptor.tos_note_zh == TOS_NOTE_ZH
        assert all(env == env.upper() for env in descriptor.key_envs)
    assert make_adapter("byos-unknown") is None


def test_resolve_registers_provider_with_env_names_only():
    provider = make_adapter("byos-openai-compat").resolve(OPENAI_ENV)
    assert provider is not None
    assert provider.name == "byos-openai-compat"
    assert provider.api_key == ""  # 值永不落盘（T1 §6 红线 1）
    assert provider.api_key_env == "OFR_BYOS_OPENAI_COMPAT_API_KEY"  # 只存名
    assert provider.auth_mode == "bearer"
    assert provider.refresh_method == "byos"
    assert provider.prefix == "byos-oc"
    assert [m.id for m in provider.models] == ["test-model-large"]
    assert provider.byos == ByosMetadata(
        kind="subscription_gateway", priority_class="subscribed",
        enabled=True, adopted_at=provider.byos.adopted_at,
    )
    assert provider.byos.adopted_at  # adopted_at 由 resolve 打上真实时间戳


def test_resolve_missing_env_returns_none_and_custom_requires_endpoint():
    # T1 §3.5: 必需 env 缺失 -> None，不注册，不抛异常。
    assert make_adapter("byos-openai-compat").resolve({}) is None
    assert make_adapter("byos-openai-compat").resolve(
        {"OFR_BYOS_OPENAI_COMPAT_API_KEY": ""}
    ) is None
    # T1 §2 row 3: byos-custom 的端点 env 必填。
    assert make_adapter("byos-custom").resolve(
        {"OFR_BYOS_CUSTOM_API_KEY": FAKE_KEY}
    ) is None
    assert make_adapter("byos-custom").resolve({}) is None


def test_resolve_openai_compat_falls_back_to_placeholder_endpoint():
    provider = make_adapter("byos-openai-compat").resolve(
        {"OFR_BYOS_OPENAI_COMPAT_API_KEY": FAKE_KEY}
    )
    assert provider is not None
    # 占位端点不可路由：未显式设置 env 就没有真实流量（T1 §2 row 1）。
    assert provider.base_url == "https://<byos-openai-compat-host>/v1"


def test_resolve_rejects_endpoint_carrying_credential_material():
    # T1 §3.5 红名单：端点字段携带密钥值 / 凭据形查询参数 -> 拒收。
    leaked = dict(CUSTOM_ENV, OFR_BYOS_CUSTOM_BASE_URL=f"https://gw.invalid/v1?token={FAKE_KEY}")
    assert make_adapter("byos-custom").resolve(leaked) is None
    # userinfo / path 里出现密钥值同样拒收（写前扫描不区分位置）。
    leaked = dict(CUSTOM_ENV, OFR_BYOS_CUSTOM_BASE_URL=f"https://{FAKE_KEY}@gw.invalid/v1")
    assert make_adapter("byos-custom").resolve(leaked) is None
    leaked = dict(CUSTOM_ENV, OFR_BYOS_CUSTOM_BASE_URL=f"https://gw.invalid/{FAKE_KEY}/v1")
    assert make_adapter("byos-custom").resolve(leaked) is None
    assert make_adapter("byos-custom").resolve(
        dict(CUSTOM_ENV, OFR_BYOS_CUSTOM_BASE_URL="https://gw.invalid/v1?api_key=x")
    ) is None


# --------------------------------- T1 §3.3 registry byos metadata block ---

def test_registry_roundtrip_preserves_byos_block_without_secret_values():
    registry = Registry({})
    registry.add_provider(make_adapter("byos-openai-compat").resolve(OPENAI_ENV))
    data = registry.to_dict()
    entry = data["byos-openai-compat"]
    assert entry["api_key_env"] == "OFR_BYOS_OPENAI_COMPAT_API_KEY"
    assert "api_key" not in entry and "api_keys" not in entry
    assert entry["refresh_method"] == "byos"
    assert entry["byos"] == {
        "kind": "subscription_gateway",
        "priority_class": "subscribed",
        "enabled": True,
        "adopted_at": entry["byos"]["adopted_at"],
    }
    # 落盘内容（yaml 序列化即 registry.yaml 写出路径）无密钥值。
    blob = yaml.dump(data, allow_unicode=True)
    assert FAKE_KEY not in blob
    assert "test-key-" not in blob

    reloaded = Registry(yaml.safe_load(blob))
    assert reloaded.get("byos-openai-compat").byos == registry.get("byos-openai-compat").byos


def test_registry_defaults_unchanged_without_byos_block():
    registry = Registry({"plain": {"base_url": "https://plain.invalid/v1"}})
    data = registry.to_dict()
    assert "byos" not in data["plain"]
    assert registry.get("plain").byos is None
    assert Registry(data).get("plain").byos is None


def test_registry_drops_malformed_byos_block():
    for bad in (
        "not-a-mapping",
        {"kind": "mystery"},
        {"priority_class": "premium"},
        {"enabled": "yes"},
        {"adopted_at": 12345},
    ):
        registry = Registry({"byos-openai-compat": {"base_url": "x", "byos": bad}})
        assert registry.get("byos-openai-compat").byos is None, bad


def test_registry_keeps_explicit_byos_disable_flag():
    registry = Registry({
        "byos-custom": {
            "base_url": "https://<byos-custom-host>/v1",
            "api_key_env": "OFR_BYOS_CUSTOM_API_KEY",
            "refresh_method": "byos",
            "byos": {"kind": "subscription_gateway", "priority_class": "subscribed",
                     "enabled": False, "adopted_at": "2026-10-25T00:00:00Z"},
        },
    })
    metadata = registry.get("byos-custom").byos
    assert metadata.enabled is False
    assert metadata.adopted_at == "2026-10-25T00:00:00Z"


# ------------------------------------------------ T1 §3.2 probe (sandbox) ---

class _SandboxUpstream(BaseHTTPRequestHandler):
    status_code = 200

    def log_message(self, *args):
        pass

    def do_POST(self):
        # 请求体只被丢弃：探活从不保留正文上下文（T1 §3.2）。
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        self.send_response(self.status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_sandbox(status_code):
    handler_cls = type("_Sandbox", (_SandboxUpstream,), {"status_code": status_code})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _sandbox_env(port):
    return {
        "OFR_BYOS_OPENAI_COMPAT_API_KEY": FAKE_KEY,
        "OFR_BYOS_OPENAI_COMPAT_BASE_URL": f"http://127.0.0.1:{port}/v1",
        "OFR_BYOS_OPENAI_COMPAT_MODEL": "test-model-large",
    }


def test_probe_success_and_failures_use_failover_taxonomy(monkeypatch):
    # 生产语义：密钥只存在于环境变量，provider 经 api_key_env 按名解析。
    monkeypatch.setenv("OFR_BYOS_OPENAI_COMPAT_API_KEY", FAKE_KEY)
    for status_code, expected_ok, expected_classification in (
        (200, True, None),
        (401, False, "credential_invalid"),
        (402, False, "credits_exhausted"),
        (429, False, "rate_limited"),  # 无正文可猜：429 只按结构归 rate_limited
        (500, False, "provider_unavailable"),
    ):
        server = _start_sandbox(status_code)
        try:
            adapter = make_adapter("byos-openai-compat")
            provider = adapter.resolve(_sandbox_env(server.server_address[1]))
            result = adapter.probe(provider, timeout=5)
        finally:
            server.shutdown()
            server.server_close()
        assert set(result) == {"ok", "status_code", "latency_ms", "classification"}
        assert result["ok"] is expected_ok
        assert result["status_code"] == status_code
        assert isinstance(result["latency_ms"], int)
        assert result["classification"] == expected_classification


def test_probe_connection_refused_is_transport_error(monkeypatch):
    monkeypatch.setenv("OFR_BYOS_OPENAI_COMPAT_API_KEY", FAKE_KEY)
    server = _start_sandbox(200)
    port = server.server_address[1]
    server.shutdown()
    server.server_close()
    adapter = make_adapter("byos-openai-compat")
    provider = adapter.resolve(_sandbox_env(port))
    result = adapter.probe(provider, timeout=5)
    assert result["ok"] is False
    assert result["status_code"] is None
    assert result["classification"] == "transport_error"


def test_probe_without_model_or_key_makes_no_claim(monkeypatch):
    monkeypatch.setenv("OFR_BYOS_OPENAI_COMPAT_API_KEY", FAKE_KEY)
    adapter = make_adapter("byos-openai-compat")
    # 无模型登记（resolve 未带 MODEL env）-> 不发明模型，不做任何断言。
    provider = adapter.resolve({"OFR_BYOS_OPENAI_COMPAT_API_KEY": FAKE_KEY})
    assert provider.models == []
    result = adapter.probe(provider, timeout=5)
    assert result == {"ok": False, "status_code": None, "latency_ms": None,
                      "classification": None}


# ---------------------------------------------------- env.py guard units ---

def test_env_guards_report_names_never_values():
    assert find_secret_leak("https://gw.invalid/x", {"K": FAKE_KEY}) == []
    assert find_secret_leak(f"path/{FAKE_KEY}/v1", {"OFR_BYOS_CUSTOM_API_KEY": FAKE_KEY}) == [
        "OFR_BYOS_CUSTOM_API_KEY"
    ]
    assert find_secret_leak("", {"K": FAKE_KEY}) == []
    assert endpoint_is_clean("https://<byos-custom-host>/v1") is True
    assert endpoint_is_clean("https://gw.invalid/v1?api_key=x") is False
    assert endpoint_is_clean("https://gw.invalid/v1?token=x") is False
    assert endpoint_is_clean("https://gw.invalid/v1?since=1") is True
