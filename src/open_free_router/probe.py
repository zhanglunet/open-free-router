"""Live availability probing — real minimal requests per registry model.

The dashboard's "availability" numbers must come from evidence, not the
catalog: a model counts as available only after a real 1-token Chat
Completions request against its upstream succeeded. Probes use each
provider's own credential from the registry and never store response
content — only status, latency, and a short error reason.
"""
from __future__ import annotations

import json
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
    if not key:
        return {"ok": False, "status": "no_key", "latency_ms": None,
                "error": "no API key configured", "checked_at": checked_at}
    body = json.dumps({
        "model": model.effective_upstream_id,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }).encode()
    request = Request(
        f"{base}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "open-free-router/0.1",
        },
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
            detail = (err.get("message", "") if isinstance(err, dict) else str(err))[:200]
        except Exception:
            pass
        return {"ok": False, "status": f"http_{e.code}", "latency_ms": latency,
                "error": detail or f"HTTP {e.code}", "checked_at": checked_at}
    except URLError as e:
        return {"ok": False, "status": "network_error", "latency_ms": None,
                "error": str(getattr(e, "reason", e))[:200], "checked_at": checked_at}
    except Exception as e:
        return {"ok": False, "status": "error", "latency_ms": None,
                "error": str(e)[:200], "checked_at": checked_at}


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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    return status
