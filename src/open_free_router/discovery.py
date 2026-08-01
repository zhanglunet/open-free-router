"""Continuous discovery of candidate free-model providers.

Discovery is intentionally separated from the trusted registry: public catalog
metadata is evidence for review, not authorization to route traffic or store a
new provider endpoint automatically.
"""
from __future__ import annotations

import json
import ipaddress
import os
import re
import socket
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

from open_free_router.registry import ModelInfo, ProviderConfig, Registry

MODELS_DEV_URL = "https://models.dev/api.json"
USER_AGENT = "open-free-router/0.2 discovery"


def _protocol(provider: dict) -> str:
    package = str(provider.get("npm") or "")
    return "openai-compatible" if package == "@ai-sdk/openai-compatible" else "unsupported"


def _dedicated_credential_env(provider_id: str) -> str:
    safe = re.sub(r"[^A-Z0-9]+", "_", provider_id.upper()).strip("_")
    return f"OFR_{safe}_API_KEY"


def _is_zero_cost(model: dict) -> bool:
    cost = model.get("cost") or {}
    return cost.get("input") == 0 and cost.get("output") == 0


def _safe_https_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return value.rstrip("/")


def normalize_models_dev(data: dict, registered: Registry) -> list[dict]:
    """Return review-only candidates with explicit zero-cost evidence."""
    registered_names = {name.lower() for name in registered.providers}
    registered_hosts = {
        urlsplit(p.upstream_url or p.base_url).hostname
        for p in registered.providers.values()
        if (p.upstream_url or p.base_url)
    }
    candidates = []
    for provider_id, provider in data.items():
        if not isinstance(provider, dict) or provider_id.lower() in registered_names:
            continue
        api = _safe_https_url(str(provider.get("api") or ""))
        if not api or urlsplit(api).hostname in registered_hosts:
            continue
        free_models = []
        for model_id, model in (provider.get("models") or {}).items():
            if not isinstance(model, dict) or model.get("status") == "deprecated":
                continue
            if not _is_zero_cost(model):
                continue
            limit = model.get("limit") or {}
            free_models.append({
                "id": str(model_id),
                "name": str(model.get("name") or model_id),
                "context_window": int(limit.get("context") or 0),
                "max_tokens": int(limit.get("output") or 0),
                "reasoning": bool(model.get("reasoning")),
                "tool_calling": bool(model.get("tool_call")),
                "modalities": list((model.get("modalities") or {}).get("input") or ["text"]),
                "last_updated": str(model.get("last_updated") or ""),
            })
        if not free_models:
            continue
        free_models.sort(key=lambda item: (-item["context_window"], item["id"]))
        candidates.append({
            "id": str(provider_id),
            "name": str(provider.get("name") or provider_id),
            "api": api,
            "documentation": _safe_https_url(str(provider.get("doc") or "")),
            "auth_env": [str(item) for item in (provider.get("env") or []) if str(item)],
            "credential_env": _dedicated_credential_env(str(provider_id)),
            "protocol": _protocol(provider),
            "status": "candidate",
            "evidence": "models.dev lists zero unit price; free eligibility still requires verification",
            "model_count": len(free_models),
            "models": free_models[:50],
        })
    candidates.sort(key=lambda item: (-item["model_count"], item["id"]))
    return candidates


def _public_https_host(url: str) -> bool:
    """Reject endpoints resolving to private/special networks before validation."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        }
    except OSError:
        return False
    if not addresses:
        return False
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            return False
    return True


def _credential_env(provider: dict, environment: dict[str, str]) -> str:
    name = str(provider.get("credential_env") or _dedicated_credential_env(str(provider.get("id") or "")))
    return name if environment.get(name) else ""


def validate_candidates(
    snapshot: dict,
    *,
    environment: dict[str, str] | None = None,
    timeout: int = 20,
    max_providers: int = 5,
    max_models: int = 3,
) -> dict:
    """Run minimal authenticated requests without retaining credential values."""
    environment = dict(os.environ if environment is None else environment)
    checked = 0
    for provider in snapshot.get("providers", []):
        result = {
            "state": "not_tested",
            "reason": "Not selected in this validation cycle",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "credential_env": "",
            "latency_ms": None,
            "tested_models": [],
            "successful_models": [],
        }
        provider["validation"] = result
        if provider.get("protocol") != "openai-compatible":
            result.update(state="unsupported", reason="Provider is not declared OpenAI-compatible")
            continue
        credential_name = _credential_env(provider, environment)
        if not credential_name:
            result.update(
                state="needs_credentials",
                reason=f"Set dedicated environment variable {provider.get('credential_env', '')}",
            )
            continue
        if checked >= max(0, max_providers):
            continue
        if not _public_https_host(str(provider.get("api") or "")):
            result.update(state="blocked", reason="Endpoint failed public HTTPS network policy")
            continue
        checked += 1
        result["credential_env"] = credential_name
        session = requests.Session()
        session.trust_env = False
        headers = {
            "Authorization": f"Bearer {environment[credential_name]}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        latencies = []
        failures = []
        models = provider.get("models", [])[: max(0, max_models)]
        for model in models:
            model_id = str(model.get("id") or "")
            if not model_id:
                continue
            result["tested_models"].append(model_id)
            started = time.monotonic()
            try:
                response = session.post(
                    f"{provider['api'].rstrip('/')}/chat/completions",
                    headers=headers,
                    json={
                        "model": model_id,
                        "messages": [{"role": "user", "content": "Reply with OK."}],
                        "max_tokens": 8,
                        "temperature": 0,
                    },
                    timeout=timeout,
                    allow_redirects=False,
                )
                latency = round((time.monotonic() - started) * 1000)
                body = response.json() if response.status_code == 200 else {}
                choices = body.get("choices") if isinstance(body, dict) else None
                if response.status_code == 200 and isinstance(choices, list) and choices:
                    result["successful_models"].append(model_id)
                    latencies.append(latency)
                else:
                    failures.append({"model": model_id, "http_status": response.status_code})
            except (requests.RequestException, ValueError):
                failures.append({"model": model_id, "http_status": None})
        result["failed_models"] = failures
        if result["successful_models"]:
            result.update(
                state="ready",
                reason="Authenticated Chat Completions smoke test succeeded",
                latency_ms=round(sum(latencies) / len(latencies)),
            )
        else:
            result.update(state="failed", reason="No tested model completed a valid response")
    snapshot["validation"] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "tested_provider_count": checked,
        "ready_provider_count": sum(
            item.get("validation", {}).get("state") == "ready"
            for item in snapshot.get("providers", [])
        ),
        "policy": "public HTTPS + declared credential env + OpenAI-compatible chat response",
    }
    return snapshot


def adopt_validated(snapshot: dict, registry: Registry) -> list[str]:
    """Add only successful models, referencing credentials by environment name."""
    adopted = []
    used_prefixes = {provider.model_prefix for provider in registry.providers.values()}
    for candidate in snapshot.get("providers", []):
        validation = candidate.get("validation") or {}
        if validation.get("state") != "ready" or candidate.get("id") in registry.providers:
            continue
        successful = set(validation.get("successful_models") or [])
        models = [
            ModelInfo(
                id=str(model["id"]),
                name=str(model.get("name") or model["id"]),
                context_window=int(model.get("context_window") or 131072),
                max_tokens=int(model.get("max_tokens") or 8192),
                reasoning=bool(model.get("reasoning")),
                tool_calling=False,
            )
            for model in candidate.get("models", [])
            if model.get("id") in successful
        ]
        if not models:
            continue
        base_prefix = re.sub(r"[^a-z0-9]+", "", str(candidate["id"]).lower())[:8] or "auto"
        prefix = base_prefix
        suffix = 2
        while prefix in used_prefixes:
            prefix = f"{base_prefix[:6]}{suffix}"
            suffix += 1
        used_prefixes.add(prefix)
        registry.add_provider(ProviderConfig(
            name=str(candidate["id"]),
            upstream_url=str(candidate["api"]),
            api_key_env=str(validation["credential_env"]),
            models=models,
            auto_refresh=False,
            refresh_method="discovery_verified",
            prefix=prefix,
        ))
        adopted.append(str(candidate["id"]))
    return adopted


def discover(registry: Registry, timeout: int = 30) -> dict:
    response = requests.get(
        MODELS_DEV_URL,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("models.dev returned a non-object catalog")
    candidates = normalize_models_dev(data, registry)
    return {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": MODELS_DEV_URL,
        "trust": "candidate-only; manual verification required before registry import",
        "candidate_provider_count": len(candidates),
        "candidate_model_count": sum(item["model_count"] for item in candidates),
        "providers": candidates,
    }


def save_discovery(snapshot: dict, path: Path) -> None:
    """Atomically write a private local discovery snapshot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
