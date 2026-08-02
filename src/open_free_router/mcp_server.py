"""MCP (Model Context Protocol) stdio server for open-free-router.

Exposes the router's registry, refresh, sync, and inference abilities as
MCP tools so agent clients (Claude Code, Codex, Kimi CLI, and any other
MCP host) can inspect and drive the router without shelling out.

Transport: stdio, one JSON-RPC 2.0 message per line (the MCP stdio
framing). Zero dependencies beyond the stdlib, matching the rest of the
project.

Register with Claude Code:
    claude mcp add --scope user open-free-router -- open-free-router mcp
Or print a ready-to-paste config snippet:
    open-free-router mcp --print-config
"""
from __future__ import annotations

import contextlib
import json
import sys
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from open_free_router import __version__
from open_free_router.auth import get_or_create_proxy_token
from open_free_router.config import Config
from open_free_router.registry import Registry
from open_free_router.routing import RoutePlanner

PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "list_models",
        "description": (
            "List free models from the router registry. Model IDs are in "
            "prefix/id form and can be used directly against the local proxy."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "description": "only this provider"},
                "tool_calling_only": {"type": "boolean", "description": "only models verified for tool calling"},
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "list_providers",
        "description": "List upstream providers with model counts and key status (never key values).",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "get_status",
        "description": "Router health: config paths, registry stats, and whether the local proxy/UI are reachable.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "explain_route",
        "description": "Explain an explicit or virtual route, including candidate filters and configured score factors, without inference.",
        "inputSchema": {
            "type": "object",
            "properties": {"model": {"type": "string", "description": "model or virtual model ID"}},
            "required": ["model"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "get_resilience",
        "description": "Read redacted provider circuit, anonymous credential-slot and model lockout state from the local proxy.",
        "inputSchema": {
            "type": "object",
            "properties": {"provider": {"type": "string", "description": "optional provider filter"}},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "check_quota",
        "description": "Read verified free-tier claims and normalized runtime quota for anonymous credential slots; never returns key values.",
        "inputSchema": {
            "type": "object",
            "properties": {"provider": {"type": "string", "description": "optional provider filter"}},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "get_metrics",
        "description": "Read aggregated privacy-minimized local usage metrics; excludes prompts, responses, headers and request IDs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30}
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "chat",
        "description": (
            "Send a prompt to a free model through the local proxy "
            "(requires `open-free-router serve` to be running)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "model ID (any accepted form, e.g. gq/gpt-oss-120b)"},
                "prompt": {"type": "string"},
                "system": {"type": "string"},
                "max_tokens": {"type": "integer"},
            },
            "required": ["model", "prompt"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    },
    {
        "name": "refresh_models",
        "description": "Refresh free model lists from provider APIs and save the registry.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "only refresh this provider"},
            },
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "sync_clients",
        "description": (
            "Write router models into client configs (pi, omp, opencode, hermes, "
            "codex, claude, kimi, openclaw, workbuddy)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agents": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "agent names; omit to sync all detected clients",
                },
            },
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
]

WRITE_TOOL_NAMES = frozenset({"refresh_models", "sync_clients"})


def _text_result(payload, is_error: bool = False) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False, indent=2
    )
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _safe_endpoint(value: str) -> str:
    """Drop userinfo, query and fragment before placing an endpoint in MCP context."""
    parsed = urlsplit(value or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        return ""
    netloc = f"{host}:{port}" if port else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


class McpServer:
    """Stateless-ish MCP tool server over a config + registry pair."""

    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config()

    # ── registry helpers ──

    def _registry(self) -> Registry:
        return Registry.load(self.cfg.registry_path)

    def _proxy_base(self) -> str:
        host = "127.0.0.1" if self.cfg.proxy_host in ("0.0.0.0", "::") else self.cfg.proxy_host
        return f"http://{host}:{self.cfg.proxy_port}"

    def available_tools(self) -> list[dict]:
        return [tool for tool in TOOLS if (
            self.cfg.mcp_allow_write_tools or tool["name"] not in WRITE_TOOL_NAMES
        )]

    def _proxy_json(self, path: str) -> dict:
        token = get_or_create_proxy_token(self.cfg.config_dir)
        request = urllib.request.Request(
            f"{self._proxy_base()}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "open-free-router/mcp",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"local proxy returned HTTP {exc.code}") from None
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise RuntimeError(f"local proxy unavailable: {exc.__class__.__name__}") from None
        if not isinstance(payload, dict):
            raise RuntimeError("local proxy returned an invalid payload")
        return payload

    # ── tool implementations ──

    def tool_list_models(self, args: dict) -> dict:
        reg = self._registry()
        provider_filter = args.get("provider", "")
        tool_only = bool(args.get("tool_calling_only"))
        models = []
        for name, p in reg.providers.items():
            if provider_filter and name != provider_filter:
                continue
            for m in p.models:
                if tool_only and not m.tool_calling:
                    continue
                models.append({
                    "id": f"{p.model_prefix}/{m.id}",
                    "name": m.name or m.id,
                    "provider": name,
                    "context_window": m.context_window,
                    "max_tokens": m.max_tokens,
                    "reasoning": m.reasoning,
                    "tool_calling": m.tool_calling,
                })
        return _text_result({"count": len(models), "models": models})

    def tool_list_providers(self, args: dict) -> dict:
        reg = self._registry()
        providers = [
            {
                "name": name,
                "upstream_url": _safe_endpoint(p.upstream_url or p.base_url),
                "prefix": p.model_prefix,
                "models": len(p.models),
                "auto_refresh": p.auto_refresh,
                "has_key": bool(p.effective_key),
                "key_env": p.api_key_env or None,
            }
            for name, p in reg.providers.items()
        ]
        return _text_result({"count": len(providers), "providers": providers})

    def _http_ok(self, url: str, timeout: float = 2.0):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "open-free-router/mcp"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status < 500
        except Exception:
            return False

    def tool_get_status(self, args: dict) -> dict:
        reg = self._registry()
        model_count = sum(len(p.models) for p in reg.providers.values())
        keyed = sum(1 for p in reg.providers.values() if p.effective_key)
        return _text_result({
            "version": __version__,
            "config_path": str(self.cfg.path) if self.cfg.path else None,
            "registry_path": str(self.cfg.registry_path),
            "providers": len(reg.providers),
            "providers_with_key": keyed,
            "models": model_count,
            "proxy_url": f"{self._proxy_base()}/v1",
            "proxy_reachable": self._http_ok(f"{self._proxy_base()}/v1/models"),
            "ui_url": f"http://{self.cfg.ui_host}:{self.cfg.ui_port}",
            "ui_reachable": self._http_ok(f"http://{self.cfg.ui_host}:{self.cfg.ui_port}/api/status"),
        })

    def tool_explain_route(self, args: dict) -> dict:
        model = str(args.get("model", "")).strip()
        if not model:
            return _text_result("model is required", is_error=True)
        explanation = RoutePlanner(
            self._registry(), self.cfg.routing
        ).plan(model).explain()
        explanation["runtime_signals_included"] = False
        explanation["note"] = "Offline explanation uses registry/config only; runtime execution re-evaluates local health, quota and metrics."
        return _text_result(explanation)

    def tool_get_resilience(self, args: dict) -> dict:
        try:
            state = self._proxy_json("/api/resilience")
        except RuntimeError as exc:
            return _text_result(str(exc), is_error=True)
        provider = str(args.get("provider", "")).strip()
        if not provider:
            return _text_result(state)
        return _text_result({
            "provider": provider,
            "providers": {name: item for name, item in state.get("providers", {}).items()
                          if name == provider},
            "credentials": {name: item for name, item in state.get("credentials", {}).items()
                            if name.split(":slot-", 1)[0] == provider},
            "models": {name: item for name, item in state.get("models", {}).items()
                       if name.split("/", 1)[0] == provider},
            "persistence": state.get("persistence", {}),
        })

    def tool_check_quota(self, args: dict) -> dict:
        provider_filter = str(args.get("provider", "")).strip()
        reg = self._registry()
        claims = []
        for provider_name, provider in reg.providers.items():
            if provider_filter and provider_name != provider_filter:
                continue
            for model in provider.models:
                evidence = provider.free_tier_for(model)
                claims.append({
                    "provider": provider_name,
                    "model": f"{provider.model_prefix}/{model.id}",
                    "free_tier": evidence.to_dict(include_status=True),
                })
        try:
            state = self._proxy_json("/api/resilience")
            runtime_available = True
            runtime_error = ""
        except RuntimeError as exc:
            state = {}
            runtime_available = False
            runtime_error = str(exc)
        slots = []
        for name, item in state.get("credentials", {}).items():
            provider_name = name.split(":slot-", 1)[0]
            if provider_filter and provider_name != provider_filter:
                continue
            if item.get("quota") or item.get("state") != "ready":
                slots.append({
                    "provider": provider_name,
                    "slot": name.split(":", 1)[-1],
                    "state": item.get("state", "ready"),
                    "reason": item.get("reason", ""),
                    "quota": item.get("quota", {}),
                })
        return _text_result({
            "claims": claims,
            "runtime": {
                "available": runtime_available,
                "error": runtime_error,
                "credential_slots": slots,
            },
            "notice": "Runtime quota contains normalized numbers/timestamps for anonymous slots only; unknown means no supported header was observed.",
        })

    def tool_get_metrics(self, args: dict) -> dict:
        try:
            days = max(1, min(365, int(args.get("days", 30))))
        except (TypeError, ValueError):
            return _text_result("days must be an integer from 1 to 365", is_error=True)
        try:
            return _text_result(self._proxy_json(f"/api/metrics?days={days}"))
        except RuntimeError as exc:
            return _text_result(str(exc), is_error=True)

    def tool_chat(self, args: dict) -> dict:
        model = args.get("model", "")
        prompt = args.get("prompt", "")
        if not model or not prompt:
            return _text_result("model and prompt are required", is_error=True)
        messages = []
        if args.get("system"):
            messages.append({"role": "system", "content": args["system"]})
        messages.append({"role": "user", "content": prompt})
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": int(args.get("max_tokens") or 2048),
        }
        token = get_or_create_proxy_token(self.cfg.config_dir)
        req = urllib.request.Request(
            f"{self._proxy_base()}/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "open-free-router/mcp",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.upstream_timeout) as r:
                response = json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return _text_result(f"proxy request failed: HTTP {exc.code}", is_error=True)
        except urllib.error.URLError as e:
            hint = (
                " (is `open-free-router serve` running?)"
                if "refused" in str(e).lower() else ""
            )
            return _text_result(f"proxy request failed: {e.__class__.__name__}{hint}", is_error=True)
        choices = response.get("choices", [])
        message = choices[0].get("message", {}) if choices else {}
        return _text_result({
            "model": model,
            "content": message.get("content", ""),
            "usage": response.get("usage", {}),
        })

    def tool_refresh_models(self, args: dict) -> dict:
        from open_free_router.refresh import refresh

        reg = self._registry()
        # refresh() prints progress; on the MCP transport stdout IS the
        # protocol stream, so route the chatter to stderr (host log).
        with contextlib.redirect_stdout(sys.stderr):
            results = refresh(reg, provider_name=args.get("source") or None)
        changed = {k: v for k, v in results.items() if v}
        if changed:
            reg.save(self.cfg.registry_path)
        return _text_result({
            "changed": changed,
            "unchanged": sorted(set(results) - set(changed)),
            "registry_saved": bool(changed),
        })

    def tool_sync_clients(self, args: dict) -> dict:
        from open_free_router.sync import sync_all

        reg = self._registry()
        agents = args.get("agents") or None
        if agents is not None:
            agents = [str(a).strip() for a in agents if str(a).strip()]
        token = get_or_create_proxy_token(self.cfg.config_dir)
        proxy_url = f"{self._proxy_base()}/v1"
        with contextlib.redirect_stdout(sys.stderr):
            results = sync_all(reg, agents=agents, proxy_url=proxy_url, proxy_token=token)
        return _text_result({"synced": results})

    # ── JSON-RPC dispatch ──

    def handle(self, message: dict) -> dict | None:
        """Handle one JSON-RPC message. Returns a response dict or None
        (for notifications, which get no response)."""
        msg_id = message.get("id")
        method = message.get("method", "")
        params = message.get("params") or {}

        # JSON-RPC notifications are id-less requests; they never get a
        # response. This subsumes the "notifications/" method prefix.
        if "id" not in message:
            return None
        if not isinstance(method, str):
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32600, "message": "Invalid Request: method must be a string"},
            }

        def ok(result) -> dict:
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}

        def err(code: int, text: str) -> dict:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": text}}

        if method == "initialize":
            requested = params.get("protocolVersion", DEFAULT_PROTOCOL_VERSION)
            version = requested if requested in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
            return ok({
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "open-free-router", "version": __version__},
            })
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": self.available_tools()})
        if method == "tools/call":
            name = params.get("name", "")
            handler = getattr(self, f"tool_{name}", None)
            if not handler or not any(t["name"] == name for t in self.available_tools()):
                return err(-32602, f"Unknown tool: {name}")
            try:
                return ok(handler(params.get("arguments") or {}))
            except Exception as e:
                return ok(_text_result(f"{type(e).__name__}: tool failed", is_error=True))
        return err(-32601, f"Method not found: {method}")

    def serve_stdio(self, stdin=None, stdout=None):
        """Blocking loop: one JSON-RPC message per stdin line.

        ``tools/call`` runs on a worker thread so a slow tool (``chat`` can
        legitimately take up to the upstream timeout) doesn't block pings
        and other requests; stdout writes are serialized with a lock.
        """
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        write_lock = threading.Lock()

        def write(response: dict):
            with write_lock:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()

        def respond(message: dict):
            response = self.handle(message)
            if response is not None:
                write(response)

        workers: list[threading.Thread] = []
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                write({
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                })
                continue
            if not isinstance(message, dict):
                write({
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600, "message": "Invalid Request: expected a JSON object"},
                })
                continue
            if message.get("method") == "tools/call":
                worker = threading.Thread(target=respond, args=(message,), daemon=True)
                worker.start()
                workers.append(worker)
            else:
                respond(message)
        for worker in workers:
            worker.join(timeout=5)


def print_client_config():
    """Print MCP registration snippets for common hosts."""
    entry = {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "open_free_router.cli", "mcp"],
    }
    print("# Claude Code (user scope):")
    print("#   claude mcp add --scope user open-free-router -- "
          f"{sys.executable} -m open_free_router.cli mcp")
    print("# Or merge into ~/.claude.json / .mcp.json:")
    print(json.dumps({"mcpServers": {"open-free-router": entry}}, indent=2))


def main(print_config: bool = False):
    if print_config:
        print_client_config()
        return
    McpServer().serve_stdio()
