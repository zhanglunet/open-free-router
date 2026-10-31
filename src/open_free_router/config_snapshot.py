"""PRD-280/T2: route config snapshot minimal storage & rollback.

Implements the ``route-config-snapshot-v1`` minimal object from the T1 design
(``docs/plans/2026-10-29-prd-280-route-config-snapshot.md``, trend issue
#1402): take a snapshot of the routes dict *before* a config-apply action,
restore it verbatim, and record ``route_switch_failed`` audit events.

Scope discipline (T2 minimal slice):

- Pure stdlib (``json`` / ``hashlib`` / ``copy``), **zero network, zero IO,
  zero new dependencies**. Persistence into ``<数据目录>/routesnap/`` and the
  two-phase restore preflight land in later tasks; this module is the
  in-memory core those layers call.
- Single active slot: at most one live snapshot per config face at any time;
  a new ``take_snapshot`` overwrites the previous one (T1 design §3.1 #4).
- Fingerprints only, never contents, leave this module towards audit: the
  ``route_switch_failed`` event carries a placeholder-safe reason string, no
  provider names, no credentials, no raw error bodies (same discipline as
  ``classify_failure`` / the failover audit allowlist).
- Red line: the learning/recommendation plane only ever *recommends* -- this
  module exposes no automatic apply path and no network calls; the only
  permitted restore triggers are the health-check objective signal and a
  manual operator command (asserted structurally in T4).
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

SNAPSHOT_SCHEMA = "route-config-snapshot-v1"

# Length of the hex fingerprint prefix kept on the snapshot object. Full
# sha256 stays reproducible from the routes content; 16 hex chars are enough
# to detect drift/tamper without widening the audit surface.
_FINGERPRINT_HEX_CHARS = 16


class FingerprintMismatch(ValueError):
    """A snapshot's stored fingerprint does not match its routes content.

    Raised by :func:`restore` when either the ``fingerprint`` field or the
    ``routes`` payload was altered after the snapshot was taken. Restore is a
    write operation; refuse loudly rather than write back unverified bytes.
    """


# --- single active slot -----------------------------------------------------
# One slot per module (per config face in future wiring). Rebinding these in
# tests via monkeypatch is supported and is the documented reset path.
_active_snapshot: dict[str, Any] | None = None
_audit_events: list[dict[str, Any]] = []


def fingerprint_routes(routes_dict: Any) -> str:
    """Stable fingerprint of a routes dict: sha256(canonical json) prefix.

    Canonical form = sorted keys, no whitespace, UTF-8 -- so two dicts that
    are ``==`` always produce the same fingerprint regardless of insertion
    order, and any value change produces a different one.
    """
    canonical = json.dumps(
        routes_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest[:_FINGERPRINT_HEX_CHARS]


def take_snapshot(routes_dict: Any, taken_at: str) -> dict[str, Any]:
    """Snapshot the routes dict before a config-apply action (single slot).

    Returns and stores (deep copies both ways)::

        {
          "schema": "route-config-snapshot-v1",
          "routes": <deep copy of routes_dict>,
          "fingerprint": "<sha256(canonical json) first 16 hex>",
          "taken_at": taken_at,
        }

    The caller must not treat the returned dict as shared state with the slot:
    later mutations of either leave the other untouched, so the snapshot stays
    a trustworthy restore credential.
    """
    global _active_snapshot
    snapshot: dict[str, Any] = {
        "schema": SNAPSHOT_SCHEMA,
        "routes": copy.deepcopy(routes_dict),
        "fingerprint": fingerprint_routes(routes_dict),
        "taken_at": taken_at,
    }
    # 单槽覆盖：新变更使上一份快照失效（不做多版本历史，T1 §3.1 #4）。
    _active_snapshot = copy.deepcopy(snapshot)
    return snapshot


def active_snapshot() -> dict[str, Any] | None:
    """Return a deep copy of the current single-slot snapshot, or None."""
    if _active_snapshot is None:
        return None
    return copy.deepcopy(_active_snapshot)


def restore(snapshot: dict[str, Any]) -> Any:
    """Verify a snapshot's fingerprint and return its routes as a deep copy.

    The verification recomputes the fingerprint from the snapshot's own
    ``routes`` payload and compares it with the stored ``fingerprint`` field:
    a snapshot whose payload *or* fingerprint was tampered with is rejected
    with :class:`FingerprintMismatch` instead of being written back.
    """
    stored = snapshot.get("fingerprint")
    recomputed = fingerprint_routes(snapshot.get("routes"))
    if not isinstance(stored, str) or stored != recomputed:
        raise FingerprintMismatch(
            "snapshot fingerprint mismatch: "
            f"stored={stored!r} recomputed={recomputed!r} -- restore refused"
        )
    return copy.deepcopy(snapshot["routes"])


def record_switch_failed(reason: str, at: str) -> dict[str, Any]:
    """Append a ``route_switch_failed`` audit event (config-level switch).

    ``reason`` is a placeholder-safe failure class/summary (e.g. what
    ``classify_failure`` would produce); raw error bodies and provider
    credentials never belong here. ``at`` is the caller-provided timestamp.
    """
    event: dict[str, Any] = {
        "event": "route_switch_failed",
        "reason": reason,
        "at": at,
    }
    _audit_events.append(event)
    return copy.deepcopy(event)


def export_events() -> list[dict[str, Any]]:
    """Deep-copy export of the recorded audit events (JSON-serializable)."""
    return copy.deepcopy(_audit_events)
