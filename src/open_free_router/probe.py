"""Live availability probing — real minimal requests per registry model.

The dashboard's "availability" numbers must come from evidence, not the
catalog: a model counts as available only after a real 1-token Chat
Completions request against its upstream succeeded. Probes use each
provider's own credential from the registry and never store response
content — only status, latency, and a short error reason.
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from open_free_router.registry import ModelInfo, ProviderConfig, Registry

PROBE_TIMEOUT = 45
PROBE_MAX_WORKERS = 4
# How long a successful probe stays usable as proof a model is available now.
# Mirrors STATUS_STALE_MS in worker/probe.js so the local and published views
# age evidence on the same clock; anything older is a historical record, not a
# statement about the present.
EVIDENCE_MAX_AGE_SECONDS = 45 * 60

# Upstream error bodies occasionally echo the credential they rejected.
# Probe results flow to the unauthenticated GET /api/probe and into the
# exported public status snapshot, so scrub anything key-shaped.
_CREDENTIAL_RE = re.compile(
    r"(?:Bearer\s+)?\b(?:sk|nvapi|gsk|xai|pplx|or)[-_][A-Za-z0-9._\-]{6,}",
    re.IGNORECASE,
)


def _scrub(text: str) -> str:
    return _CREDENTIAL_RE.sub("[redacted-credential]", text or "")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def probe_model(provider: ProviderConfig, model: ModelInfo, timeout: int = PROBE_TIMEOUT) -> dict:
    """One minimal live Chat Completions request. Returns a redacted result."""
    checked_at = _now()
    base = (provider.upstream_url or provider.base_url).rstrip("/")
    if not base:
        return {"ok": False, "status": "no_endpoint", "latency_ms": None,
                "error": "provider has no upstream URL", "checked_at": checked_at}
    key = provider.effective_key
    if not key and provider.auth_mode != "none":
        return {"ok": False, "status": "no_key", "latency_ms": None,
                "error": "no API key configured", "checked_at": checked_at}
    body = json.dumps({
        "model": model.effective_upstream_id,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "open-free-router/0.1",
    }
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = Request(
        f"{base}/chat/completions",
        data=body,
        headers=headers,
        method="POST",
    )
    start = time.monotonic()
    try:
        with urlopen(request, timeout=timeout) as response:
            response.read()
            latency = round((time.monotonic() - start) * 1000)
            return {"ok": True, "status": f"http_{response.status}",
                    "latency_ms": latency, "error": "", "checked_at": checked_at}
    except HTTPError as e:
        latency = round((time.monotonic() - start) * 1000)
        detail = ""
        try:
            raw = e.read()
            parsed = json.loads(raw)
            err = parsed.get("error")
            detail = _scrub(err.get("message", "") if isinstance(err, dict) else str(err))[:200]
        except Exception:
            pass
        return {"ok": False, "status": f"http_{e.code}", "latency_ms": latency,
                "error": detail or f"HTTP {e.code}", "checked_at": checked_at}
    except URLError as e:
        return {"ok": False, "status": "network_error", "latency_ms": None,
                "error": _scrub(str(getattr(e, "reason", e)))[:200], "checked_at": checked_at}
    except Exception as e:
        return {"ok": False, "status": "error", "latency_ms": None,
                "error": _scrub(str(e))[:200], "checked_at": checked_at}


class ProbeRunner:
    """Background probe executor with a queryable snapshot."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state: dict = {
            "running": False,
            "started_at": None,
            "finished_at": None,
            "total": 0,
            "done": 0,
            "results": {},  # "provider/model_id" → probe result
        }

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self.state))

    def start(
        self,
        reg: Registry,
        provider: str = "",
        model: str = "",
        on_finish=None,
        timeout: int = PROBE_TIMEOUT,
    ) -> bool:
        """Kick off a probe run. Returns False if one is already running."""
        with self._lock:
            if self.state["running"]:
                return False
            targets = []
            for name, p in reg.providers.items():
                if provider and name != provider:
                    continue
                for m in p.models:
                    if model and m.id != model and f"{p.model_prefix}/{m.id}" != model:
                        continue
                    targets.append((p, m))
            self.state["running"] = True
            self.state["started_at"] = _now()
            self.state["finished_at"] = None
            self.state["total"] = len(targets)
            self.state["done"] = 0
            # Drop results for models no longer in the registry so removed
            # models don't linger in snapshots and the exported status file;
            # scoped runs still accumulate across invocations.
            valid = {
                f"{p.name}/{m.id}" for p in reg.providers.values() for m in p.models
            }
            self.state["results"] = {
                key: value for key, value in self.state["results"].items() if key in valid
            }

        def worker():
            def probe_one(pair):
                p, m = pair
                result = probe_model(p, m, timeout=timeout)
                with self._lock:
                    self.state["results"][f"{p.name}/{m.id}"] = {
                        "provider": p.name,
                        "model": m.id,
                        "display_id": f"{p.model_prefix}/{m.id}",
                        **result,
                    }
                    self.state["done"] += 1

            try:
                with ThreadPoolExecutor(max_workers=PROBE_MAX_WORKERS) as pool:
                    list(pool.map(probe_one, targets))
            finally:
                with self._lock:
                    self.state["running"] = False
                    self.state["finished_at"] = _now()
                if on_finish:
                    try:
                        on_finish(self.snapshot())
                    except Exception:
                        pass

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()
        return True


def snapshot_to_status(snapshot: dict) -> dict:
    """Aggregate per-model probe results into the provider-status shape
    consumed by scripts/export-public-catalog.py (superset: adds models)."""
    providers: dict[str, dict] = {}
    for result in snapshot.get("results", {}).values():
        name = result["provider"]
        entry = providers.setdefault(name, {
            "availability": "unverified", "latency_ms": None,
            "reason": "", "checked_at": result.get("checked_at", ""),
            "models": {},
        })
        entry["models"][result["model"]] = {
            "available": bool(result.get("ok")),
            "status": result.get("status", ""),
            "latency_ms": result.get("latency_ms"),
            "reason": result.get("error", ""),
            "checked_at": result.get("checked_at", ""),
        }
    for name, entry in providers.items():
        model_results = list(entry["models"].values())
        ok_latencies = [m["latency_ms"] for m in model_results if m["available"] and m["latency_ms"]]
        if any(m["available"] for m in model_results):
            entry["availability"] = "available"
            entry["latency_ms"] = min(ok_latencies) if ok_latencies else None
            entry["reason"] = "Live 1-token Chat Completions probe succeeded"
        elif all(m["status"] == "no_key" for m in model_results):
            entry["availability"] = "unverified"
            entry["reason"] = "No API key configured; not probed"
        else:
            entry["availability"] = "unavailable"
            failing = next((m for m in model_results if not m["available"]), {})
            entry["reason"] = failing.get("reason") or failing.get("status") or "probe failed"
        entry["checked_at"] = max(
            (m["checked_at"] for m in model_results if m["checked_at"]), default=""
        )
    return {
        "as_of": snapshot.get("finished_at") or snapshot.get("started_at") or _now(),
        "method": (
            "One minimal 1-token Chat Completions request per model against its "
            "upstream; no credential values or response content retained."
        ),
        "providers": providers,
    }


def write_probe_status(snapshot: dict, path: Path) -> dict:
    """Persist the aggregated redacted status snapshot; returns it."""
    status = snapshot_to_status(snapshot)
    _write_private_json(path, status)
    return status


def _write_private_json(path: Path, payload: dict) -> None:
    """Write dashboard evidence without making it readable by other users."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _safe_probe_snapshot(snapshot: dict) -> dict:
    """Return the small, redacted subset safe to expose from ``/api/probe``."""
    safe = {
        "running": False,
        "started_at": snapshot.get("started_at"),
        "finished_at": snapshot.get("finished_at"),
        "total": int(snapshot.get("total") or 0),
        "done": int(snapshot.get("done") or 0),
        "results": {},
    }
    raw_results = snapshot.get("results")
    if not isinstance(raw_results, dict):
        return safe
    for key, result in raw_results.items():
        if not isinstance(key, str) or not isinstance(result, dict):
            continue
        safe["results"][key] = {
            "provider": str(result.get("provider") or ""),
            "model": str(result.get("model") or ""),
            "display_id": str(result.get("display_id") or ""),
            "ok": bool(result.get("ok")),
            "status": str(result.get("status") or ""),
            "latency_ms": result.get("latency_ms") if isinstance(result.get("latency_ms"), int) else None,
            "error": _scrub(str(result.get("error") or ""))[:200],
            "checked_at": str(result.get("checked_at") or ""),
        }
    return safe


def write_probe_snapshot(snapshot: dict, path: Path) -> dict:
    """Persist per-model probe evidence so a dashboard restart keeps history."""
    safe = _safe_probe_snapshot(snapshot)
    _write_private_json(path, safe)
    return safe


def load_probe_snapshot(path: Path) -> dict | None:
    """Load a previously persisted probe snapshot, rejecting invalid files."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("results"), dict):
        return None
    return _safe_probe_snapshot(raw)


def available_model_ids(
    snapshot: dict | None,
    now: float | None = None,
    max_age: float = EVIDENCE_MAX_AGE_SECONDS,
) -> tuple[set[str], int]:
    """Return ``provider/model`` IDs whose latest probe succeeded *and* is fresh.

    A probe result is a measurement with a timestamp, not a standing fact. The
    dashboard's availability view already ages evidence out; callers that write
    client configs from the same file must do so too, or a probe from last week
    becomes a claim about right now.

    Returns ``(fresh_ok, stale_ok)`` so a caller can distinguish "never probed"
    from "probed too long ago" -- those need different advice. A result whose
    ``checked_at`` is missing or unparseable counts as stale, since freshness
    cannot be shown.
    """
    if not isinstance(snapshot, dict):
        return set(), 0
    results = snapshot.get("results")
    if not isinstance(results, dict):
        return set(), 0
    now = time.time() if now is None else now
    fresh: set[str] = set()
    stale = 0
    for key, result in results.items():
        if not isinstance(key, str) or not isinstance(result, dict):
            continue
        if result.get("ok") is not True:
            continue
        try:
            checked = datetime.fromisoformat(str(result.get("checked_at") or ""))
        except ValueError:
            stale += 1
            continue
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        if 0 <= now - checked.timestamp() <= max_age:
            fresh.add(key)
        else:
            # Also counts a timestamp from the future, which cannot be trusted
            # to describe the present either.
            stale += 1
    return fresh, stale


def load_status_as_probe_snapshot(path: Path) -> dict | None:
    """Convert the older aggregated status file into dashboard model rows."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return None
    providers = raw.get("providers") if isinstance(raw, dict) else None
    if not isinstance(providers, dict):
        return None
    results = {}
    for provider_name, provider in providers.items():
        models = provider.get("models") if isinstance(provider, dict) else None
        if not isinstance(models, dict):
            continue
        for model_id, model in models.items():
            if not isinstance(model, dict):
                continue
            results[f"{provider_name}/{model_id}"] = {
                "provider": str(provider_name),
                "model": str(model_id),
                "display_id": f"{provider_name}/{model_id}",
                "ok": bool(model.get("available")),
                "status": str(model.get("status") or ""),
                "latency_ms": model.get("latency_ms"),
                "error": str(model.get("reason") or ""),
                "checked_at": str(model.get("checked_at") or ""),
            }
    return _safe_probe_snapshot({
        "running": False,
        "started_at": None,
        "finished_at": raw.get("as_of"),
        "total": len(results),
        "done": len(results),
        "results": results,
    })
