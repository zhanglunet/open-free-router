"""PRD-079/T4: resource limits for the local sandbox backend (Linux).

Uses the stdlib ``resource`` module (``RLIMIT_CPU`` / ``RLIMIT_AS``) via a
``preexec_fn`` that runs in the child *before* exec:

- ``RLIMIT_CPU``: CPU-seconds budget. The soft limit is set to ``cpu_s``
  and the hard limit a few seconds later, so exhaustion is delivered as
  ``SIGXCPU`` (deterministic, detectable) rather than SIGKILL.
- ``RLIMIT_AS``: virtual-address-space cap. ``malloc`` failures surface in
  the child as ``MemoryError`` (exit 1), which the parent detects.

The ``preexec_fn`` also calls ``os.setsid()`` itself -- CPython forbids
combining ``start_new_session=True`` with a custom ``preexec_fn``, and
``setsid()`` gives the same new-session/new-process-group semantics, so
the timeout path's ``os.killpg()`` keeps working unchanged.

Kill classification is best-effort and documented as such:
- returncode ``-SIGXCPU`` -> ``"cpu"``
- ``"MemoryError"`` in stderr (and not a timeout kill) -> ``"memory"``
- anything else -> ``None`` (unknown / not a resource kill)

Honest limits of this layer (T1 "accidents, not adversaries"):
- ``RLIMIT_AS`` caps *virtual* address space, not RSS; a fork bomb is
  bounded by the timeout kill, not by these limits.
- A child that re-``setsid()``s itself escapes the process-group kill,
  same as the T2 timeout path.
"""
from __future__ import annotations

import os
import signal
from dataclasses import dataclass

try:
    import resource as _resource
except ImportError:  # non-POSIX (e.g. Windows): no rlimits available
    _resource = None

#: Grace period between the CPU soft limit (SIGXCPU) and the hard limit
#: (SIGKILL), so exhaustion is always observed as SIGXCPU first.
CPU_HARD_GRACE_S = 5.0


class ResourceLimitUnsupportedError(Exception):
    """Resource limits were configured but the platform cannot enforce
    them (needs the POSIX ``resource`` module / ``os.setsid``)."""


@dataclass(frozen=True)
class ResourceLimits:
    """CPU/memory budgets for one sandboxed execution.

    ``None`` disables that dimension. Values must be positive when set.
    """

    cpu_s: float | None = None
    memory_mb: float | None = None

    def validate(self) -> None:
        if _resource is None or not hasattr(os, "setsid"):
            raise ResourceLimitUnsupportedError(
                "resource limits need a POSIX platform (resource/os.setsid)")
        for name, value in (("cpu_s", self.cpu_s), ("memory_mb", self.memory_mb)):
            if value is not None and (not isinstance(value, (int, float))
                                      or value <= 0):
                raise ValueError(f"{name} must be a positive number or None, "
                                 f"got {value!r}")

    @property
    def enabled(self) -> bool:
        return self.cpu_s is not None or self.memory_mb is not None


def make_preexec(limits: ResourceLimits):
    """Build the child-side ``preexec_fn``: new session + rlimits.

    Raises ``ResourceLimitUnsupportedError`` on platforms without
    ``resource``/``setsid``.
    """
    limits.validate()
    cpu_s = limits.cpu_s
    mem_bytes = int(limits.memory_mb * 1024 * 1024) if limits.memory_mb else None

    def _preexec() -> None:
        # New session => new process group (replaces start_new_session=True;
        # CPython forbids combining it with a custom preexec_fn).
        os.setsid()
        if cpu_s is not None:
            # Soft limit first: deterministic SIGXCPU, detectable by the
            # parent. The hard limit follows after a short grace period.
            _resource.setrlimit(_resource.RLIMIT_CPU,
                                (int(cpu_s), int(cpu_s + CPU_HARD_GRACE_S)))
        if mem_bytes is not None:
            _resource.setrlimit(_resource.RLIMIT_AS, (mem_bytes, mem_bytes))

    return _preexec


def classify_resource_kill(returncode: int | None, stderr: str,
                           timed_out: bool) -> str | None:
    """Best-effort classification of a resource-limit kill.

    Returns ``"cpu"`` / ``"memory"`` / ``None``. ``timed_out`` kills are
    the runner's own timeout path, never a resource kill.
    """
    if timed_out or returncode is None:
        return None
    if returncode == -signal.SIGXCPU:
        return "cpu"
    if "MemoryError" in (stderr or ""):
        return "memory"
    return None
