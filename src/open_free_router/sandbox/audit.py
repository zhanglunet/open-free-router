"""PRD-079/T4: append-only audit log for sandbox executions (T1 section 6).

One JSON object per line. Every record carries the *hash* of the executed
code (sha256), never the code itself, and goes through the secret
desensitization gate before it is written.

Schema (T1 section 6 + audit fields):
- ``event``: ``"executed"`` | ``"rejected"`` (pre-execution gate)
- ``at``: ISO-8601 UTC timestamp
- ``model``: model that answered (executed only)
- ``code_sha256``: sha256 of the executed code (never the code)
- ``exit_code`` / ``duration_ms`` / ``timed_out`` (executed only)
- ``model_switched``: whether a model failover happened (executed only)
- ``resource_exhausted``: ``"cpu"`` | ``"memory"`` | null
- ``reason``: rejection reason, desensitized (rejected only)

Conventions (aligned with the gateway's analytics store):
- file ``0600``, parent dir ``0700`` -- the log never leaves the machine;
- rotation: ``audit.log`` -> ``audit.log.1`` .. ``.N`` at ``max_mb``;
- failures never interrupt execution: write errors are swallowed with a
  ``logging.warning`` (same philosophy as ``AnalyticsStore``:
  "failures never interrupt inference");
- a record that trips the secret gate is *dropped*, never written.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from open_free_router.sandbox.redact import (
    SensitiveContentError,
    assert_no_secrets,
    redact_secrets,
)

log = logging.getLogger(__name__)

DEFAULT_AUDIT_PATH = "~/.open-free-router/sandbox/audit.log"
DEFAULT_MAX_MB = 5.0
DEFAULT_KEEP = 3


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditLog:
    """Thread-safe, append-only JSONL audit log. Never raises into callers."""

    def __init__(self, path: str | Path | None = DEFAULT_AUDIT_PATH,
                 max_mb: float = DEFAULT_MAX_MB, keep: int = DEFAULT_KEEP,
                 enabled: bool = True) -> None:
        self.enabled = bool(enabled)
        self.path = Path(os.path.expanduser(str(path))).resolve() \
            if path else None
        self.max_bytes = int(max_mb * 1024 * 1024)
        self.keep = max(0, int(keep))
        self._lock = threading.Lock()

    # -- public API ----------------------------------------------------

    def record_execution(self, *, model: str | None, code_sha256: str,
                         exit_code: int | None, duration_ms: float,
                         timed_out: bool, model_switched: bool,
                         resource_exhausted: str | None = None) -> None:
        self._write({
            "event": "executed",
            "at": _utc_now(),
            "model": model,
            "code_sha256": code_sha256,
            "exit_code": exit_code,
            "duration_ms": round(duration_ms, 1),
            "timed_out": bool(timed_out),
            "model_switched": bool(model_switched),
            "resource_exhausted": resource_exhausted,
        })

    def record_rejection(self, *, code_sha256: str, reason: str) -> None:
        """A pre-execution gate (blacklist / path jail / env) refused the
        code. T1 section 6: rejections are audited too."""
        self._write({
            "event": "rejected",
            "at": _utc_now(),
            "code_sha256": code_sha256,
            # The reason is our own gate label; still desensitized in case
            # a pattern label ever echoes user content.
            "reason": redact_secrets(reason),
        })

    # -- internals -----------------------------------------------------

    def _write(self, record: dict) -> None:
        if not self.enabled or self.path is None:
            return
        try:
            assert_no_secrets(record)
        except SensitiveContentError:
            # Privacy fail-safe: drop the record rather than risk writing
            # a secret. This is a bug signal, so it gets a warning.
            log.warning("audit record dropped: sensitive content detected")
            return
        line = json.dumps(record, ensure_ascii=False)
        try:
            with self._lock:
                self._ensure_parent()
                self._rotate_if_needed()
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                os.chmod(self.path, 0o600)
        except OSError as e:
            # The audit log must never break an execution.
            log.warning("audit log write failed (%s); continuing", e)

    def _ensure_parent(self) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)

    def _rotate_if_needed(self) -> None:
        assert self.path is not None
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
        except FileNotFoundError:
            return
        # audit.log -> .1 -> .2 ... drop the oldest beyond `keep`.
        oldest = self.path.with_name(self.path.name + f".{self.keep}")
        if oldest.exists():
            oldest.unlink()
        for i in range(self.keep - 1, 0, -1):
            src = self.path.with_name(self.path.name + f".{i}")
            if src.exists():
                src.rename(self.path.with_name(self.path.name + f".{i + 1}"))
        if self.keep > 0:
            self.path.rename(self.path.with_name(self.path.name + ".1"))
