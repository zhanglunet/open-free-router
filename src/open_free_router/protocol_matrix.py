"""Declared provider × client-protocol compatibility matrix.

The matrix describes the router surface and configuration readiness.  It is
deliberately not a live availability claim: credentials, regional access and
upstream model behaviour are verified separately by probes.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from open_free_router.registry import ProviderConfig, Registry


PROTOCOLS = (
    {
        "id": "chat_completions",
        "endpoint": "/v1/chat/completions",
        "envelope": "openai",
    },
    {
        "id": "responses",
        "endpoint": "/v1/responses",
        "envelope": "openai_responses",
    },
    {
        "id": "anthropic_messages",
        "endpoint": "/v1/messages",
        "envelope": "anthropic",
    },
)


def _upstream_issue(name: str, provider: ProviderConfig) -> dict | None:
    value = provider.upstream_url or provider.base_url
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return {
            "severity": "warning",
            "code": "invalid_upstream_url",
            "provider": name,
            "message": "上游地址不是有效的 HTTP(S) URL。",
            "fix": f"修正 registry.yaml 中 {name}.upstream_url。",
        }
    if parsed.hostname == "generativelanguage.googleapis.com":
        path = parsed.path.rstrip("/")
        if not path.endswith("/openai"):
            return {
                "severity": "error",
                "code": "google_openai_compatibility_path_missing",
                "provider": name,
                "message": "Google AI Studio 地址缺少 OpenAI 兼容路径 /openai。",
                "fix": (
                    f"将 {name}.upstream_url 改为 "
                    "https://generativelanguage.googleapis.com/v1beta/openai"
                ),
            }
    return None


def protocol_matrix(registry: Registry) -> dict:
    """Return declared compatibility without performing network requests."""
    rows = []
    issues = []
    for name, provider in registry.providers.items():
        issue = _upstream_issue(name, provider)
        if issue:
            issues.append(issue)
        tool_models = sum(bool(model.tool_calling) for model in provider.models)
        reasoning_models = sum(bool(model.reasoning) for model in provider.models)
        for protocol in PROTOCOLS:
            rows.append({
                "provider": name,
                "protocol": protocol["id"],
                "endpoint": protocol["endpoint"],
                "envelope": protocol["envelope"],
                "configured": issue is None and bool(provider.models),
                "credential_ready": bool(
                    provider.auth_mode == "none" or provider.api_keys or provider.effective_key
                ),
                "model_count": len(provider.models),
                "tool_calling_models": tool_models,
                "reasoning_models": reasoning_models,
                "capabilities": {
                    "buffered_text": True,
                    "streaming_text": True,
                    "tool_loop": tool_models > 0,
                    "usage": True,
                    "finish_reason": True,
                    "retry_after": True,
                    "pre_byte_fallback": True,
                    "post_byte_fallback": False,
                },
                "verification": "declared_not_live_tested",
            })
    return {
        "schema_version": 1,
        "provider_count": len(registry.providers),
        "protocol_count": len(PROTOCOLS),
        "row_count": len(rows),
        "rows": rows,
        "issues": issues,
        "notice_zh": "这是配置与协议适配矩阵，不代表服务器实时可用；实时结果请运行 probe。",
    }
