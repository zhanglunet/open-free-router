"""PRD-079/T2: sandbox backend providers (``SANDBOX_PROVIDER``).

- ``local``: subprocess on this machine -- workdir isolation, env
  allowlist, static path jail + dangerous-pattern blacklist as the first
  line of defense (the "accidents, not adversaries" threat model from T1).
- ``none``: sandbox disabled; ``run()`` refuses. The gateway behaves
  exactly as without the sandbox (opt-in design).
- ``docker`` / ``e2b`` / ``daytona``: reserved names that raise
  ``SandboxNotImplementedError`` -- configuring them is an explicit
  error, never a silent downgrade.

Honest limitation, stated up front (T1 section 4 fix): the local backend
runs a plain subprocess and **cannot isolate network**. ``network=false``
is therefore a *declaration*, not an enforcement: ``LocalProvider``
refuses to start with ``network=false`` and tells the operator to either
acknowledge the lack of isolation (``network=true``) or wait for the
docker backend. The full audit log / desensitization / resource limits
are T4's scope; the blacklist here is the minimal backstop from T1
section 6.
"""
from __future__ import annotations

import ast
import logging
import os
import re
import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import replace
from pathlib import Path

from open_free_router.sandbox.config import (
    FUTURE_PROVIDERS,
    SandboxConfig,
    SandboxConfigError,
)
from open_free_router.sandbox.runner import SandboxRequest, SandboxResult, execute_local

log = logging.getLogger(__name__)


class SandboxError(Exception):
    """Base class for all sandbox errors."""


class SandboxNotImplementedError(SandboxError):
    """A reserved backend (docker/e2b/daytona) was configured."""


class SandboxUnsupportedError(SandboxError):
    """A valid configuration the local backend cannot honor
    (e.g. network=false, which it cannot enforce)."""


class SandboxRejectedError(SandboxError):
    """The code was rejected *before* execution (path jail, blacklist,
    language, env allowlist)."""


class SandboxDisabledError(SandboxError):
    """``run()`` was called on the ``none`` backend."""


class SandboxProvider(ABC):
    """A place where sandboxed code can run."""

    name: str

    @abstractmethod
    def run(self, request: SandboxRequest) -> SandboxResult:
        """Execute ``request`` and return the result."""

    def close(self) -> None:
        """Release backend resources. Default: nothing to do."""


def create_provider(config: SandboxConfig) -> SandboxProvider:
    """Build the backend named by ``config.provider``."""
    if config.provider == "local":
        return LocalProvider(config)
    if config.provider == "none":
        return NoneProvider(config)
    if config.provider in FUTURE_PROVIDERS + ("docker",):
        raise SandboxNotImplementedError(
            f"SANDBOX_PROVIDER={config.provider!r} is reserved but not implemented "
            "in this release; refusing to silently downgrade to another backend."
        )
    raise SandboxConfigError(f"unknown SANDBOX_PROVIDER={config.provider!r}")


class NoneProvider(SandboxProvider):
    """The ``none`` backend: the sandbox is disabled entirely."""

    name = "none"

    def __init__(self, config: SandboxConfig) -> None:
        self._config = config

    def run(self, request: SandboxRequest) -> SandboxResult:
        raise SandboxDisabledError(
            "sandbox is disabled (provider=none); refusing to run code"
        )


# ---------------------------------------------------------------------------
# Dangerous-pattern blacklist (T1 section 6: backstop, not the primary
# defense -- isolation is. T4 adds audit logging and desensitization
# around rejections; the pattern list itself stays minimal here.)
# ---------------------------------------------------------------------------
_DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    # rm -rf / style path destruction (rm -rf /, rm -r /tmp, ...)
    (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*\b", "destructive 'rm -r' invocation"),
    # reverse shell via /dev/tcp
    (r"/dev/tcp/", "reverse shell via /dev/tcp"),
    # interactive bash reverse shell
    (r"\bbash\s+-i\b", "interactive reverse shell"),
    # curl|bash / wget|bash pipe execution
    (r"\bcurl\b[^|\n]*\|\s*(ba)?sh\b", "curl-piped-to-shell execution"),
    (r"\bwget\b[^|\n]*\|\s*(ba)?sh\b", "wget-piped-to-shell execution"),
)
_DANGEROUS_COMPILED = [(re.compile(p), label) for p, label in _DANGEROUS_PATTERNS]


def check_dangerous_patterns(code: str) -> None:
    """Reject code matching the dangerous-pattern backstop list."""
    for pattern, label in _DANGEROUS_COMPILED:
        if pattern.search(code):
            raise SandboxRejectedError(
                f"code rejected by dangerous-pattern blacklist: {label}"
            )


# ---------------------------------------------------------------------------
# Static path jail (T1 section 4: first line of defense; OS permissions are
# the second). AST-based: only constant string paths in file-writing calls
# are checked. Dynamic paths (variables, f-strings built at runtime) cannot
# be checked statically -- that is the documented limit of this layer, and
# exactly why the threat model is "accidents, not adversaries".
# ---------------------------------------------------------------------------
_WRITE_CALLS = frozenset({
    "open",
    "write_text", "write_bytes",
    "mkdir", "makedirs",
    "remove", "unlink", "rmdir", "removedirs",
    "rename", "replace", "symlink", "link",
    "mkfifo", "mknod", "truncate", "chdir",
})
_READ_ONLY_OPEN_MODES = frozenset({"r", "rb", "rt", "r+", "rb+", "rt+"})


def _escapes_workdir(path_str: str, workdir: str) -> bool:
    candidate = os.path.expanduser(path_str)
    if os.path.isabs(candidate):
        base = os.path.normpath(workdir)
        target = os.path.normpath(candidate)
    elif ".." in Path(candidate).parts:
        base = os.path.normpath(workdir)
        target = os.path.normpath(os.path.join(base, candidate))
    else:
        return False
    try:
        return os.path.commonpath([base, target]) != base
    except ValueError:
        # e.g. different drives on Windows -- treat as escaping
        return True


def _check_literal(path_str: str, workdir: str, call_name: str) -> None:
    if _escapes_workdir(path_str, workdir):
        raise SandboxRejectedError(
            f"path jail: {call_name}() targets a path outside the sandbox "
            f"workdir: {path_str!r}"
        )


def check_path_jail(code: str, workdir: str) -> None:
    """Statically reject writes outside ``workdir``.

    Raises ``SandboxRejectedError`` before anything is executed.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # Not our problem: a syntax error fails at runtime like normal.
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            call_name = func.id
        elif isinstance(func, ast.Attribute):
            call_name = func.attr
        else:
            continue
        if call_name not in _WRITE_CALLS:
            continue
        if call_name == "open":
            mode = "r"
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value if isinstance(node.args[1].value, str) else "r"
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant) \
                        and isinstance(kw.value.value, str):
                    mode = kw.value.value
            # Fail closed: only a provably read-only open() skips the jail.
            if isinstance(mode, str) and mode in _READ_ONLY_OPEN_MODES:
                continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                _check_literal(arg.value, workdir, call_name)
        for kw in node.keywords:
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                _check_literal(kw.value.value, workdir, call_name)


class LocalProvider(SandboxProvider):
    """The ``local`` backend: ``subprocess`` on this machine."""

    name = "local"

    def __init__(self, config: SandboxConfig) -> None:
        if not config.network:
            # Honest declaration (T1 section 4 fix): a subprocess cannot be
            # network-isolated. Refuse instead of pretending.
            raise SandboxUnsupportedError(
                "local backend cannot isolate network: network=false is a "
                "declaration, not an enforcement. Set network=true to "
                "acknowledge the lack of isolation, or wait for the docker "
                "backend which will enforce it."
            )
        self._config = config
        self._workdir_parent = Path(os.path.expanduser(config.workdir_parent))
        self._workdir_parent.mkdir(parents=True, exist_ok=True)
        python = shutil.which("python3")
        if not python:
            raise SandboxUnsupportedError("python3 not found on PATH")
        self._python = python

    def run(self, request: SandboxRequest) -> SandboxResult:
        # Pre-execution gates (fail closed, before any workdir is created).
        if request.language != "python3":
            raise SandboxRejectedError(
                f"unsupported language {request.language!r}: only 'python3' "
                "is supported in this release"
            )
        check_dangerous_patterns(request.code)

        workdir = tempfile.mkdtemp(prefix="sbx-", dir=str(self._workdir_parent))
        try:
            check_path_jail(request.code, workdir)
            env = self._filtered_env(request.env)
            result = execute_local(
                replace(request, workdir=workdir, env=env),
                python=self._python,
            )
        except SandboxRejectedError:
            # Rejected before execution: nothing to debug, drop the dir.
            shutil.rmtree(workdir, ignore_errors=True)
            raise

        if result.exit_code == 0 and not result.timed_out:
            shutil.rmtree(workdir, ignore_errors=True)
        elif not self._config.retain_workdir_on_failure:
            shutil.rmtree(workdir, ignore_errors=True)
        # else: retained for debugging (T1 section 4); result.workdir
        # still points at it.
        return result

    def _filtered_env(self, env: dict[str, str]) -> dict[str, str]:
        allowlist = set(self._config.env_allowlist)
        rejected = sorted(k for k in env if k not in allowlist)
        if rejected:
            raise SandboxRejectedError(
                f"env keys not in allowlist {sorted(allowlist)}: {rejected}"
            )
        return dict(env)
