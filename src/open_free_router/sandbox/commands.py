"""CLI commands for ``open-free-router sandbox ...`` (PRD-079/T3).

Stdlib-only at import time. The sandbox runner/config/providers modules
(PRD-079/T2) are imported lazily inside functions, so this module stays
importable before T2 lands. This module never imports gateway internals
(routing / executor / translate / resilience / failover): the gateway is
only ever touched over HTTP, and the proxy token is read as a file.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn, Optional

from open_free_router.sandbox import caller as _caller_mod

GATEWAY_BASE_URL = _caller_mod.GATEWAY_BASE_URL

# Fallback defaults mirroring T1 §4 (used only while PRD-079/T2's config
# module is not importable). T2's config.py is canonical once it lands.
_FALLBACK_DEFAULTS = {
    "provider": "none",  # opt-in: disabled until configured (T1)
    "default_models": [],
    "timeout_s": 120,
    "max_model_retries": 3,
    "total_budget_s": 300,
    "workdir_parent": "~/.open-free-router/sandbox",
    "network": False,
    "env_allowlist": ["PATH", "HOME", "LANG"],
    "retain_workdir_on_failure": True,
}


class SandboxCLIError(Exception):
    """User-facing sandbox CLI failure (message -> stderr, exit 2)."""


# -- gateway access (HTTP only, token read as a file) --------------------

def config_dir() -> Path:
    """Locate the gateway config dir (read-only replica of its lookup).

    Mirrors the gateway's own resolution without importing it:
    OPEN_FREE_ROUTER_CONFIG override, then ~/.config/open-free-router,
    then ./config.yaml. Only the *location* is depended upon (T1 §7).
    """
    override = os.environ.get("OPEN_FREE_ROUTER_CONFIG", "")
    if override:
        return Path(override).expanduser().parent
    p = Path.home() / ".config" / "open-free-router" / "config.yaml"
    if p.exists():
        return p.parent
    p = Path.cwd() / "config.yaml"
    if p.exists():
        return p.parent
    return Path.home() / ".config" / "open-free-router"


def read_proxy_token(config_dir_path: Optional[Path] = None) -> str:
    """Read the gateway's proxy.token file (read-only, never written)."""
    d = config_dir_path or config_dir()
    token_file = d / "proxy.token"
    if not token_file.exists():
        raise SandboxCLIError(
            f"proxy token not found at {token_file}; is the gateway installed? "
            "(run `open-free-router serve` once to generate it)")
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise SandboxCLIError(f"proxy token file {token_file} is empty")
    return token


def _gateway_request(method: str, path: str, token: str, payload=None,
                     timeout_s: float = 30.0):
    url = f"{GATEWAY_BASE_URL}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise SandboxCLIError(f"gateway {method} {path} -> HTTP {e.code}: {body[:300]}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SandboxCLIError(
            f"cannot reach gateway at {GATEWAY_BASE_URL} ({e}); "
            "is `open-free-router serve` running?")


def gateway_get(path: str, token: str, timeout_s: float = 30.0):
    return _gateway_request("GET", path, token, None, timeout_s)


def gateway_post(path: str, token: str, payload: dict, timeout_s: float = 120.0):
    return _gateway_request("POST", path, token, payload, timeout_s)


# -- sandbox.toml (T2's config.py is canonical; local fallback) -------------

def _fallback_config() -> dict:
    """Minimal sandbox.toml reader used only before T2's config lands."""
    cfg = dict(_FALLBACK_DEFAULTS)
    cfg["env_allowlist"] = list(_FALLBACK_DEFAULTS["env_allowlist"])
    for candidate in (Path("sandbox.toml"),
                      Path.home() / ".open-free-router" / "sandbox.toml"):
        if candidate.is_file():
            import tomllib

            with open(candidate, "rb") as f:
                raw = tomllib.load(f).get("sandbox", {})
            if isinstance(raw, dict):
                for key in cfg:
                    if key in raw:
                        cfg[key] = raw[key]
            break
    cfg["workdir_parent"] = str(Path(str(cfg["workdir_parent"])).expanduser())
    return cfg


def load_sandbox_config(path: Optional[Path] = None) -> dict:
    """Effective sandbox config as a plain dict.

    Delegates to PRD-079/T2's ``config.load_sandbox_config`` when available;
    otherwise falls back to the local minimal reader. ``provider`` defaults
    to ``"none"`` (opt-in, T1).
    """
    try:
        from open_free_router.sandbox import config as t2_config
    except ImportError:
        return _fallback_config()
    import dataclasses

    loaded = t2_config.load_sandbox_config(path)
    cfg = dataclasses.asdict(loaded)
    cfg["workdir_parent"] = str(Path(str(cfg["workdir_parent"])).expanduser())
    return cfg


def _scrub(value):
    """Drop token/secret/password keys recursively (T1 §8: 脱敏不打 token)."""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()
                if not any(s in k.lower() for s in ("token", "secret", "password"))}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


# -- T2 provider (lazy; clear error before T2 lands) -------------------------

def get_provider(cfg: dict):
    """Build the T2 sandbox backend named by ``cfg["provider"]``.

    Raises SandboxCLIError with a clear message while PRD-079/T2 is not
    installed, or when the backend refuses (e.g. local + network=false,
    which T2 honestly rejects instead of pretending to isolate).
    """
    try:
        from open_free_router.sandbox import config as t2_config
        from open_free_router.sandbox import providers as t2_providers
    except ImportError as e:
        raise SandboxCLIError(
            "sandbox runner is not available (PRD-079/T2 not installed); "
            "`sandbox run` needs it") from e
    fields = {f.name for f in t2_config.SandboxConfig.__dataclass_fields__.values()}
    t2_cfg = t2_config.SandboxConfig(
        **{k: v for k, v in cfg.items() if k in fields})
    try:
        return t2_providers.create_provider(t2_cfg)
    except t2_providers.SandboxError as e:
        raise SandboxCLIError(str(e)) from e


# -- orchestration -------------------------------------------------------------

def resolve_models(args, cfg: dict) -> list:
    """--models a,b,c > --model ID > sandbox.toml default_models."""
    if getattr(args, "models", None):
        models = [m.strip() for m in str(args.models).split(",")]
    elif getattr(args, "model", None):
        models = [str(args.model).strip()]
    else:
        models = list(cfg.get("default_models") or [])
    models = [m for m in models if m]
    if not models:
        raise SandboxCLIError(
            "no model selected: pass --model ID or --models a,b,c, "
            "or set default_models in sandbox.toml")
    # Honor the retry cap early: never list more than we may try.
    return models[: int(cfg.get("max_model_retries", 3))]


def read_task_input(args) -> str:
    """Read the task from <file>, '-' or stdin (T1 §8: <code.py|stdin>)."""
    path = getattr(args, "file", None)
    if path and path != "-":
        p = Path(path)
        if not p.exists():
            raise SandboxCLIError(f"input file not found: {path}")
        return p.read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SandboxCLIError("no input: pass a file or pipe the task via stdin")


def run_sandbox_task(*, task: str, models: list, cfg: dict, token: str,
                     exec_timeout_s: Optional[int] = None,
                     allow_network: bool = False,
                     caller=None, provider=None) -> dict:
    """Generate code with the model, run it in the sandbox, return JSON-ready dict.

    Flow: model generates Python code for ``task`` -> sandbox executes it.
    Model-side failures switch models (T1 §5); code-side failures never do.
    ``caller``/``provider`` are injectable for tests; defaults are the real ones.
    """
    if cfg.get("provider", "none") == "none":
        raise SandboxCLIError(
            "sandbox is disabled (provider=none); set provider=local in "
            "sandbox.toml to enable it")

    # --network acknowledges the local backend cannot isolate network
    # (T2 honestly refuses network=false); it flips the effective config.
    effective = dict(cfg)
    if allow_network:
        effective["network"] = True

    model_caller = caller or _caller_mod.ModelCaller(
        token=token,
        max_model_retries=int(effective.get("max_model_retries", 3)),
        total_budget_s=float(effective.get("total_budget_s", 300)),
    )
    code, call_result = model_caller.generate_code(models, task)

    backend = provider if provider is not None else get_provider(effective)
    allowlist = effective.get("env_allowlist") or []
    request = _build_request(code, effective, exec_timeout_s, allowlist)
    try:
        exec_result = backend.run(request)
    except SandboxCLIError:
        raise
    except Exception as e:
        # T2's SandboxError family (disabled / rejected / unsupported):
        # surface as a clean CLI error, not a traceback.
        if type(e).__name__.startswith("Sandbox"):
            raise SandboxCLIError(str(e)) from e
        raise

    return {
        "model": call_result.model,
        "model_switches": call_result.switches,
        "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
        "exit_code": exec_result.exit_code,
        "stdout": exec_result.stdout,
        "stderr": exec_result.stderr,
        "duration_ms": exec_result.duration_ms,
        "timed_out": exec_result.timed_out,
    }


def _build_request(code: str, cfg: dict, exec_timeout_s: Optional[int],
                   allowlist: list):
    """Build T2's SandboxRequest; falls back to a stub when T2 is absent."""
    try:
        from open_free_router.sandbox import runner as t2_runner
        request_cls = t2_runner.SandboxRequest
    except ImportError:
        from dataclasses import dataclass

        @dataclass
        class _StubRequest:
            code: str
            language: str = "python3"
            workdir: object = None
            env: dict = None
            timeout_s: float = 120.0
            network: bool = False

        request_cls = _StubRequest
    env = {k: os.environ[k] for k in allowlist if k in os.environ}
    return request_cls(
        code=code,
        language="python3",
        workdir=None,  # the provider creates the isolated dir (T1 §4)
        env=env,       # values for allowlisted keys come from our env
        timeout_s=float(exec_timeout_s or cfg.get("timeout_s", 120)),
        network=bool(cfg.get("network", False)),
    )


# -- argparse entry points -------------------------------------------------------

def _fail(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(2)


def cmd_sandbox_run(args) -> None:
    try:
        cfg = load_sandbox_config()
        token = read_proxy_token()
        models = resolve_models(args, cfg)
        task = read_task_input(args)
        result = run_sandbox_task(
            task=task, models=models, cfg=cfg, token=token,
            exec_timeout_s=getattr(args, "timeout", None),
            allow_network=bool(getattr(args, "network", False)))
    except SandboxCLIError as e:
        _fail(str(e))
    except _caller_mod.ModelCallError as e:
        _fail(str(e))
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_sandbox_config(args) -> None:
    cfg = load_sandbox_config()
    print(json.dumps(_scrub(cfg), ensure_ascii=False, indent=2))


def cmd_sandbox_models(args) -> None:
    try:
        token = read_proxy_token()
        _, data = gateway_get("/v1/models", token)
    except SandboxCLIError as e:
        _fail(str(e))
    items = data.get("data", []) if isinstance(data, dict) else []
    if getattr(args, "json", False):
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    for m in items:
        if isinstance(m, dict):
            print(f"  {m.get('id', '?')}  ({m.get('owned_by', '?')})")
    print(f"\n{len(items)} models (pick one with `sandbox run --model <id>`)")
