"""PRD-079/T2: local execution engine: run -> capture -> timeout.

The contract from T1 (docs/plans/2026-10-19-sandbox-design.md, section 4):

- code is written to ``main.py`` inside an already-created workdir,
- ``subprocess.Popen(["python3", "main.py"])`` with piped stdout/stderr,
- on timeout the **whole process tree** is killed (not just the parent),
- stdout/stderr are each truncated to 1MB,
- ``exit_code`` is None when the process was killed.

This module is deliberately dumb: validation (language, path jail,
dangerous-pattern blacklist, env allowlist) lives in providers.py. The
runner only executes what it is given inside the workdir it is given.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: Per-stream output cap (characters, ~1MB for ASCII). Prevents a runaway
#: print loop from exploding memory on the host.
OUTPUT_LIMIT = 1_000_000


@dataclass
class SandboxRequest:
    """Input contract (T1 section 4). ``workdir`` is set by the provider."""

    code: str
    language: str = "python3"
    workdir: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 120.0
    network: bool = False


@dataclass
class SandboxResult:
    """Output contract (T1 section 4)."""

    exit_code: int | None  # None means the process was killed (timeout)
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool
    workdir: str


def execute_local(request: SandboxRequest, *, python: str = "python3") -> SandboxResult:
    """Run ``request.code`` as ``main.py`` in ``request.workdir``.

    The child gets exactly ``request.env`` (already filtered by the
    provider), a fresh process group (so the timeout kill takes the whole
    tree), and no stdin.
    """
    if not request.workdir or not os.path.isdir(request.workdir):
        raise ValueError("provider must create request.workdir before execute_local")
    if request.timeout_s <= 0:
        raise ValueError(f"timeout_s must be > 0, got {request.timeout_s!r}")

    entry = os.path.join(request.workdir, "main.py")
    with open(entry, "w", encoding="utf-8") as fh:
        fh.write(request.code)

    started = time.monotonic()
    proc = subprocess.Popen(
        [python, "main.py"],
        cwd=request.workdir,
        env=dict(request.env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        # New session => new process group => os.killpg() below kills the
        # parent *and* every child/grandchild it spawned (T1 section 4:
        # "kill the whole process tree"). SIGKILL cannot be caught, so the
        # only escape is a child that re-setsids itself -- outside the
        # "accidents, not adversaries" threat model of the local backend.
        start_new_session=True,
        text=True,
        errors="replace",
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=request.timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc.pid)
        stdout, stderr = proc.communicate()  # reap; also drains pipes
    duration_ms = (time.monotonic() - started) * 1000
    return SandboxResult(
        exit_code=None if timed_out else proc.returncode,
        stdout=_truncate(stdout),
        stderr=_truncate(stderr),
        duration_ms=duration_ms,
        timed_out=timed_out,
        workdir=request.workdir,
    )


def _kill_tree(pid: int) -> None:
    """SIGKILL the whole process group, then the leader as a fallback."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _truncate(text: str) -> str:
    if len(text) > OUTPUT_LIMIT:
        log.warning("sandbox output truncated to %d chars", OUTPUT_LIMIT)
        return text[:OUTPUT_LIMIT]
    return text
