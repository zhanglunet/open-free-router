"""Read-only availability queries over the probe evidence (PRD-014/T2).

Implements T1 tools 1 (``list_available_models``) and 3 (``get_live_status``)
per ``docs/plans/2026-10-17-mcp-tools-schema.md``. Tool 2 (``get_model_score``)
is T3 and deliberately not implemented here.

Data-source contract (T1 acceptance item): availability is derived from the
*same* probe snapshot the dashboard persists and loads
(``data_dir/probe-results.json``, legacy ``probe-status.json``), aged with the
*same* freshness clock the dashboard uses (``probe.available_model_ids``,
45-minute evidence window). The models enumerated are the *same* registry the
proxy routes from, and virtual-model membership is resolved with the *same*
``RoutePlanner`` the proxy executor uses.

Strictly read-only: no registry / probe / config writes, no network I/O.
``probed_at`` is the real probe batch time and is never synthesized — a model
or batch with no evidence reports ``null`` instead of "now".
"""
from __future__ import annotations

from pathlib import Path

from open_free_router.probe import (
    available_model_ids,
    load_probe_snapshot,
    load_status_as_probe_snapshot,
)
from open_free_router.registry import Registry
from open_free_router.routing import BUILTIN_ALIASES, RoutePlanner


# ── T1 error shapes ──────────────────────────────────────────────────────

def error_model_not_found(model_id: str) -> dict:
    """T1: ``{"error": "MODEL_NOT_FOUND", "model_id": "..."}``."""
    return {"error": "MODEL_NOT_FOUND", "model_id": model_id}


def error_unauthorized() -> dict:
    """T1: ``{"error": "UNAUTHORIZED"}``.

    Placeholder for T4: returned once tool-level authentication exists.
    T2 performs no auth check.
    """
    return {"error": "UNAUTHORIZED"}


def error_rate_limited(retry_after_s: int = 60) -> dict:
    """T1: ``{"error": "RATE_LIMITED", "retry_after_s": 60}``.

    Placeholder for T4: returned once per-caller rate limiting exists.
    T2 performs no rate limiting.
    """
    return {"error": "RATE_LIMITED", "retry_after_s": retry_after_s}


# ── probe data loading ───────────────────────────────────────────────────

def load_probe_data(data_dir: Path) -> dict | None:
    """Load the probe snapshot the dashboard persists.

    Mirrors ``DashboardHandler._api_probe_get``: prefer ``probe-results.json``,
    fall back to the legacy aggregated ``probe-status.json``. Returns ``None``
    when no probe batch has ever been persisted.
    """
    snapshot = load_probe_snapshot(data_dir / "probe-results.json")
    if snapshot is None:
        snapshot = load_status_as_probe_snapshot(data_dir / "probe-status.json")
    return snapshot


def probe_batch_time(snapshot: dict | None) -> str | None:
    """Real batch time of the probe run (``finished_at`` or ``started_at``).

    Returns ``None`` when no batch ever ran. The timestamps come from
    ``probe._now()`` (timezone-aware ISO-8601) and are passed through
    untouched — never replaced with the current time.
    """
    if not snapshot:
        return None
    return snapshot.get("finished_at") or snapshot.get("started_at") or None


def _alias_membership(planner: RoutePlanner) -> dict[tuple[str, str], str]:
    """Map ``(provider_name, model_id)`` → first virtual model covering it.

    Custom aliases are preferred over the builtin catch-alls (``auto``,
    ``auto/coding``, ``auto/fast``, ``auto/free``) so the hint stays
    informative; within each group the planner's canonical order wins.
    Membership is computed with the real ``RoutePlanner``, the same logic
    the proxy executor routes with.
    """
    ordered = [a for a in planner.virtual_model_ids() if a not in BUILTIN_ALIASES]
    ordered += [a for a in planner.virtual_model_ids() if a in BUILTIN_ALIASES]
    membership: dict[tuple[str, str], str] = {}
    for alias in ordered:
        for target in planner.plan(alias).candidates:
            membership.setdefault((target.provider_name, target.model.id), alias)
    return membership


# ── T1 tool 1 ────────────────────────────────────────────────────────────

def list_available_models(
    registry: Registry,
    routing_config,
    snapshot: dict | None,
    provider: str = "",
) -> dict:
    """T1 tool 1: registry models with availability from probe evidence.

    ``available`` is true only for models with *fresh* successful probe
    evidence (same ``available_model_ids`` clock the dashboard uses).
    No evidence — never probed, stale, or failed — means ``False``
    (fail-closed).
    """
    fresh, _stale = available_model_ids(snapshot)
    planner = RoutePlanner(registry, routing_config)
    membership = _alias_membership(planner)
    models = []
    for name, p in registry.providers.items():
        if provider and name != provider:
            continue
        for m in p.models:
            entry = {
                "model_id": f"{p.model_prefix}/{m.id}",
                "provider": name,
                "available": f"{name}/{m.id}" in fresh,
            }
            virtual = membership.get((name, m.id))
            if virtual:
                entry["virtual_model"] = virtual
            models.append(entry)
    return {"probed_at": probe_batch_time(snapshot), "models": models}


# ── T1 tool 3 ────────────────────────────────────────────────────────────

def _status_for(
    provider_name: str,
    model_id: str,
    canonical_id: str,
    results: dict,
) -> dict:
    """One T1 status row. ``error_rate`` is derived from the latest probe
    result only (the snapshot keeps one result per model): 0.0 on success,
    1.0 on failure or when there is no evidence at all (fail-closed)."""
    result = results.get(f"{provider_name}/{model_id}") or {}
    ok = result.get("ok") is True
    latency = result.get("latency_ms")
    return {
        "model_id": canonical_id,
        "last_probe_at": result.get("checked_at") or None,
        "latency_ms": latency if isinstance(latency, (int, float)) else None,
        "error_rate": 0.0 if ok else 1.0,
    }


def get_live_status(
    registry: Registry,
    routing_config,
    snapshot: dict | None,
    model_id: str = "",
) -> dict:
    """T1 tool 3: per-model live probe status.

    ``model_id`` accepts any form the proxy routes (bare ID, ``prefix/id``,
    upstream ID, Codex alias) and is resolved with the same
    ``RoutePlanner.resolve_explicit`` the proxy uses. Unknown IDs return the
    T1 ``MODEL_NOT_FOUND`` shape instead of a status list.
    """
    canonical = ""
    if model_id:
        target = RoutePlanner(registry, routing_config).resolve_explicit(model_id)
        if target is None:
            return error_model_not_found(model_id)
        canonical = target.canonical_id
    results = (snapshot or {}).get("results")
    if not isinstance(results, dict):
        results = {}
    statuses = []
    for name, p in registry.providers.items():
        for m in p.models:
            model_canonical = f"{p.model_prefix}/{m.id}"
            if canonical and model_canonical != canonical:
                continue
            statuses.append(_status_for(name, m.id, model_canonical, results))
    return {"statuses": statuses}
