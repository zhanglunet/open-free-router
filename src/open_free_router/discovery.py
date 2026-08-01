"""Continuous discovery of candidate free-model providers.

Discovery is intentionally separated from the trusted registry: public catalog
metadata is evidence for review, not authorization to route traffic or store a
new provider endpoint automatically.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

from open_free_router.registry import Registry

MODELS_DEV_URL = "https://models.dev/api.json"
USER_AGENT = "open-free-router/0.2 discovery"


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
            "status": "candidate",
            "evidence": "models.dev lists zero unit price; free eligibility still requires verification",
            "model_count": len(free_models),
            "models": free_models[:50],
        })
    candidates.sort(key=lambda item: (-item["model_count"], item["id"]))
    return candidates


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
        "schema_version": 1,
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
