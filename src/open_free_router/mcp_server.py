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

from open_free_router import __version__
from open_free_router.auth import get_or_create_proxy_token
from open_free_router.config import Config
from open_free_router.registry import Registry

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
    },
    {
        "name": "list_providers",
        "description": "List upstream providers with model counts and key status (never key values).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_status",
        "description": "Router health: config paths, registry stats, and whether the local proxy/UI are reachable.",
        "inputSchema": {"type": "object", "properties": {}},
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
    },
]


def _text_result(payload, is_error: bool = False) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False, indent=2
    )
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class McpServer:
    """Stateless-ish MCP tool server over a config + registry pair."""

    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config()

    # ── registry helpers ──

    def _registry(self) -> Registry:
        return Registry.load(self.cfg.registry_path)

    def _proxy_base(self) -> str:
        return f"http://{self.cfg.proxy_host}:{self.cfg.proxy_port}"

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
                "upstream_url": p.upstream_url or p.base_url,
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
        except urllib.error.URLError as e:
            raw = getattr(e, "read", lambda: b"")()
            detail = raw.decode(errors="replace") if raw else str(e)
            hint = (
                " (is `open-free-router serve` running?)"
                if "refused" in str(e).lower() else ""
            )
            return _text_result(f"proxy request failed: {detail}{hint}", is_error=True)
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
            return ok({"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name", "")
            handler = getattr(self, f"tool_{name}", None)
            if not handler or not any(t["name"] == name for t in TOOLS):
                return err(-32602, f"Unknown tool: {name}")
            try:
                return ok(handler(params.get("arguments") or {}))
            except Exception as e:
                return ok(_text_result(f"{type(e).__name__}: {e}", is_error=True))
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
