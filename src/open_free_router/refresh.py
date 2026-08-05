#!/usr/bin/env python3
"""Refresh free model lists from provider APIs via pluggable sources."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

from open_free_router.probe import probe_model
from open_free_router.registry import ModelInfo, ProviderConfig, Registry

from open_free_router.refresh_sources import (
    openrouter,
    nvidia_nim,
    google_ai_studio,
    groq,
    deepseek,
    poolside,
    nous,
    sensenova,
    stepfun,
    opencode_zen,
)

# Map registry provider name -> refresh source module
SOURCE_MAP = {
    "openrouter": openrouter,
    "nvidia-nim": nvidia_nim,
    "google-ai-studio": google_ai_studio,
    "groq": groq,
    "deepseek": deepseek,
    "poolside": poolside,
    "nous": nous,
    "sensenova": sensenova,
    "stepfun": stepfun,
    "opencode-zen-free": opencode_zen,
}


PROBE_MAX_WORKERS = 4

# A probe that never reached the upstream proves nothing about the model, so
# these outcomes adopt the candidate rather than rejecting it.
_UNVALIDATABLE = ("no_key", "no_endpoint")


def _validate_new_models(
    provider: ProviderConfig, name: str, candidates: List[ModelInfo], timeout: int
) -> List[ModelInfo]:
    """Drop newly-offered models that fail a real 1-token request.

    A provider listing a model under ``GET /models`` does not mean
    ``/chat/completions`` accepts it, so opting into this trades refresh time
    and a little free-tier quota for a registry without dead routes.

    Skipping is not a permanent verdict: a model that is merely rate-limited
    right now simply is not adopted this round, and the next refresh offers it
    again. That keeps a transient 429 from costing more than one cycle -- the
    same reasoning that stops a transient 429 from stopping a credential.
    """
    if not candidates:
        return []
    print(f"  probing {len(candidates)} new {name} model(s)...")
    with ThreadPoolExecutor(max_workers=PROBE_MAX_WORKERS) as pool:
        results = list(pool.map(
            lambda model: (model, probe_model(provider, model, timeout=timeout)), candidates,
        ))
    kept = []
    for model, result in results:
        if result.get("ok"):
            kept.append(model)
        elif result.get("status") in _UNVALIDATABLE:
            print(f"    ? {model.id} adopted unvalidated ({result.get('status')})")
            kept.append(model)
        else:
            reason = result.get("status") or "failed"
            print(f"    ✗ {model.id} skipped ({reason}); will be offered again next refresh")
    return kept


def refresh(
    reg: Registry,
    provider_name: str | None = None,
    probe_new: bool = False,
    probe_timeout: int = 30,
) -> Dict[str, bool]:
    """Refresh one or all providers' model lists.

    ``probe_new`` validates models that are not already in the registry with a
    real 1-token request before adopting them, and is off by default: it costs
    free-tier quota on every call, so the scheduler in serve.py never enables
    it. Pre-existing models are never re-probed, keeping the diff
    non-destructive.

    Returns a dict of provider name -> **did this provider's model list
    actually change**. This is deliberately *not* "did the fetch succeed" —
    callers (serve.py's scheduler, ui.py's /api/refresh, cli.py's
    `refresh` command) all use this dict via ``any(results.values())`` to
    decide whether a registry save + agent-config sync is warranted. A
    provider that fetched successfully but returned the same model list
    it already had must report False here, otherwise every scheduled
    refresh triggers a full registry backup + rewrite of every agent's
    synced config file even when nothing changed.
    """
    results: Dict[str, bool] = {}

    providers = [provider_name] if provider_name else list(reg.providers.keys())
    for name in providers:
        p = reg.get(name)
        if not p:
            results[name] = False
            continue

        source = SOURCE_MAP.get(name)
        if not source:
            print(f"  ⚠ No refresh source for provider: {name}")
            results[name] = False
            continue

        print(f"Refreshing {name}...")
        new_models = source.fetch(
            provider_base_url=p.upstream_url or p.base_url,
            api_key=p.effective_key,
        )

        if not new_models:
            print(f"  ⚠ {name} fetch returned no models")
            results[name] = False
            continue

        if probe_new:
            known = {m.id for m in p.models}
            candidates = [m for m in new_models if m.id not in known]
            if candidates:
                kept = {m.id for m in _validate_new_models(p, name, candidates, probe_timeout)}
                new_models = [m for m in new_models if m.id in known or m.id in kept]
                if not new_models:
                    print(f"  ⚠ {name} has no models left after probing")
                    results[name] = False
                    continue

        def fingerprint(model):
            return (
                model.id,
                model.effective_upstream_id,
                model.name or model.id,
                model.context_window,
                model.max_tokens,
                model.reasoning,
                model.tool_calling,
            )

        current_models = [fingerprint(m) for m in p.models]
        new_model_data = [fingerprint(m) for m in new_models]
        changed = current_models != new_model_data
        if changed:
            reg.update_models(name, new_models)
            print(f"  ✓ {name} updated: {len(new_models)} models")
        else:
            print(f"  ✓ {name} unchanged: {len(new_models)} models")
        results[name] = changed

    return results
