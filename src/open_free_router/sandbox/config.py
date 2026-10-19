"""PRD-079/T2: standalone ``sandbox.toml`` configuration.

The sandbox has its own config file and never touches the gateway's config.
Parsing uses the stdlib ``tomllib`` (read-only), so no new dependency.

``provider=none`` (or a missing file) means the sandbox is disabled: the
opt-in design from T1 -- existing gateway behavior is untouched.
"""
from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_WORKDIR_PARENT = "~/.open-free-router/sandbox"
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "HOME", "LANG")

#: Backends selected by SANDBOX_PROVIDER. docker/e2b/daytona are reserved
#: names; configuring them fails explicitly at provider creation time
#: (providers.create_provider) -- never a silent downgrade.
VALID_PROVIDERS = ("local", "docker", "none")
FUTURE_PROVIDERS = ("e2b", "daytona")


class SandboxConfigError(ValueError):
    """The sandbox configuration itself is invalid."""


@dataclass(frozen=True)
class SandboxConfig:
    provider: str = "none"
    default_models: tuple[str, ...] = ()
    timeout_s: float = 120.0
    max_model_retries: int = 3
    total_budget_s: float = 300.0
    workdir_parent: str = DEFAULT_WORKDIR_PARENT
    network: bool = False
    env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST
    retain_workdir_on_failure: bool = True
    # PRD-079/T4: resource budgets (None disables that dimension).
    cpu_limit_s: float | None = 600.0
    memory_limit_mb: float | None = 1024.0
    # PRD-079/T4: audit log.
    audit_enabled: bool = True
    audit_path: str = "~/.open-free-router/sandbox/audit.log"
    audit_max_mb: float = 5.0
    audit_keep: int = 3


def _bounded_number(value: object, default: float, low: float, high: float, name: str) -> float:
    """Out-of-range values fall back to the default with a warning (never
    silently clamped into a surprising policy)."""
    if value is None:
        return default
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        log.warning("sandbox.%s=%r is not a number; using default %r", name, value, default)
        return default
    if not (low <= number <= high):
        log.warning("sandbox.%s=%r out of range [%s, %s]; using default %r",
                    name, value, low, high, default)
        return default
    return number


def _optional_bounded_number(raw: dict, name: str, default: float | None,
                             low: float, high: float) -> float | None:
    """Like _bounded_number, but the limit can be disabled.

    Used for resource budgets (PRD-079/T4): missing key -> default;
    ``0`` -> None (disabled; TOML has no null, and 0 is meaningless as a
    budget); out-of-range/non-numeric -> default + warning.
    """
    if name not in raw:
        return default
    value = raw[name]
    if value is None:
        return None  # programmatic configs may still pass None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        log.warning("sandbox.%s=%r is not a number; using default %r", name, value, default)
        return default
    if number == 0:
        return None
    if not (low <= number <= high):
        log.warning("sandbox.%s=%r out of range [%s, %s]; using default %r",
                    name, value, low, high, default)
        return default
    return number


def _as_str_tuple(value: object, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        # comma-separated form is also accepted (T1 section 3)
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return tuple(value)
    raise SandboxConfigError(f"sandbox.{name} must be a string or list of strings, got {value!r}")


def _default_config_path() -> Path | None:
    for candidate in (Path("sandbox.toml"), Path(os.path.expanduser("~/.open-free-router/sandbox.toml"))):
        if candidate.is_file():
            return candidate
    return None


def load_sandbox_config(path: str | Path | None = None) -> SandboxConfig:
    """Load ``sandbox.toml``. Missing file -> defaults (provider=none,
    i.e. the sandbox stays disabled)."""
    cfg_path: Path | None = Path(path) if path is not None else _default_config_path()
    if cfg_path is not None and not cfg_path.is_file():
        raise SandboxConfigError(f"sandbox config not found: {cfg_path}")
    raw: dict = {}
    if cfg_path is not None:
        with open(cfg_path, "rb") as fh:
            data = tomllib.load(fh)
        raw = data.get("sandbox", {})
        if not isinstance(raw, dict):
            raise SandboxConfigError("sandbox.toml: [sandbox] must be a table")

    provider = raw.get("provider", "none")
    if provider not in VALID_PROVIDERS + FUTURE_PROVIDERS:
        raise SandboxConfigError(
            f"sandbox.provider must be one of {VALID_PROVIDERS + FUTURE_PROVIDERS}, got {provider!r}"
        )

    return SandboxConfig(
        provider=provider,
        default_models=_as_str_tuple(raw.get("default_models"), "default_models"),
        timeout_s=_bounded_number(raw.get("timeout_s"), 120.0, 0.1, 3600.0, "timeout_s"),
        max_model_retries=int(_bounded_number(raw.get("max_model_retries"), 3, 1, 100, "max_model_retries")),
        total_budget_s=_bounded_number(raw.get("total_budget_s"), 300.0, 1.0, 86400.0, "total_budget_s"),
        workdir_parent=str(raw.get("workdir_parent", DEFAULT_WORKDIR_PARENT)),
        network=bool(raw.get("network", False)),
        env_allowlist=_as_str_tuple(raw.get("env_allowlist"), "env_allowlist") or DEFAULT_ENV_ALLOWLIST,
        retain_workdir_on_failure=bool(raw.get("retain_workdir_on_failure", True)),
        # PRD-079/T4: resource budgets + audit log.
        cpu_limit_s=_optional_bounded_number(raw, "cpu_limit_s", 600.0, 0.1, 86400.0),
        memory_limit_mb=_optional_bounded_number(raw, "memory_limit_mb", 1024.0, 1.0, 1048576.0),
        audit_enabled=bool(raw.get("audit_enabled", True)),
        audit_path=str(raw.get("audit_path", "~/.open-free-router/sandbox/audit.log")),
        audit_max_mb=_bounded_number(raw.get("audit_max_mb"), 5.0, 0.1, 1024.0, "audit_max_mb"),
        audit_keep=int(_bounded_number(raw.get("audit_keep"), 3, 0, 100, "audit_keep")),
    )
