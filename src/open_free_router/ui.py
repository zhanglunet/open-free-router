#!/usr/bin/env python3
"""Web dashboard for open-free-router."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from open_free_router import __version__
from open_free_router.auth import check_auth, get_or_create_proxy_token, get_or_create_token
from open_free_router.config import Config
from open_free_router.probe import (
    ProbeRunner,
    load_probe_snapshot,
    load_status_as_probe_snapshot,
    write_probe_snapshot,
    write_probe_status,
)
from open_free_router.registry import ModelInfo, ProviderConfig, Registry
from open_free_router.proxy import rebuild_proxy_index
from open_free_router.sync import write_pi_models

# One probe runner per process; the dashboard polls its snapshot.
_PROBE_RUNNER = ProbeRunner()


class _UIHandler(BaseHTTPRequestHandler):
    cfg: Config | None = None
    reg: Registry | None = None
    config_path: Path | None = None
    # Local auth token for state-changing requests (POST). Empty string
    # disables the check — only used by tests that don't care about auth.
    token: str = ""

    def _require_auth(self) -> bool:
        """Enforce Authorization: Bearer <token> on state-changing requests.

        Returns True and lets the caller proceed if authorized; otherwise
        sends a 401 JSON response and returns False.
        """
        if not self.token:
            return True
        if check_auth(self.headers, self.token):
            return True
        self._send_json(401, {
            "error": "unauthorized",
            "hint": "send Authorization: Bearer <token> "
                     "(token file: ~/.config/open-free-router/ui.token)",
        })
        return False

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_file("templates/index.html", "text/html")
        elif self.path == "/static/style.css":
            self._serve_file("web_static/static/css/style.css", "text/css")
        elif self.path == "/static/app.js":
            self._serve_file("web_static/static/js/app.js", "application/javascript")
        elif self.path == "/api/status":
            self._api_status()
        elif self.path == "/api/models":
            self._api_models()
        elif self.path == "/api/config":
            self._api_config_get()
        elif self.path == "/api/providers":
            self._api_providers()
        elif self.path == "/api/probe":
            self._api_probe_get()
        elif self.path == "/api/discovery":
            self._api_discovery_get()
        elif self.path == "/api/routing":
            self._api_routing_get()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path not in (
            "/api/config", "/api/refresh", "/api/providers", "/api/probe",
            "/api/discovery", "/api/sync",
            "/api/routing/reset",
        ):
            self.send_error(404)
            return
        if not self._require_auth():
            return
        if self.path == "/api/config":
            self._api_config_post()
        elif self.path == "/api/refresh":
            self._api_refresh()
        elif self.path == "/api/providers":
            self._api_providers_post()
        elif self.path == "/api/probe":
            self._api_probe_post()
        elif self.path == "/api/discovery":
            self._api_discovery_post()
        elif self.path == "/api/sync":
            self._api_sync_post()
        elif self.path == "/api/routing/reset":
            self._api_routing_reset()

    def _proxy_request(self, path: str, method: str = "GET", payload: dict | None = None):
        if not self.cfg:
            raise RuntimeError("server not initialized")
        host = self.cfg.proxy_host
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        token = get_or_create_proxy_token(self.cfg.config_dir)
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"http://{host}:{self.cfg.proxy_port}{path}",
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read())

    def _api_routing_get(self):
        """Read redacted runtime routing data through the authenticated proxy API."""
        try:
            resilience = self._proxy_request("/api/resilience")
            routes = self._proxy_request("/api/routes")
        except (OSError, ValueError, urllib.error.URLError, RuntimeError) as exc:
            self._send_json(503, {"error": f"本地代理运行状态不可读取：{exc.__class__.__name__}"})
            return
        self._send_json(200, {"resilience": resilience, "routes": routes})

    def _api_routing_reset(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) if length else b"{}")
        except ValueError:
            self._send_json(400, {"error": "请求不是有效 JSON"})
            return
        provider = str(payload.get("provider", "")).strip() if isinstance(payload, dict) else ""
        model = str(payload.get("model", "")).strip() if isinstance(payload, dict) else ""
        if not provider:
            self._send_json(400, {"error": "必须指定提供商"})
            return
        clean = {"provider": provider}
        if model:
            clean["model"] = model
        try:
            result = self._proxy_request("/api/resilience/reset", "POST", clean)
        except urllib.error.HTTPError as exc:
            self._send_json(exc.code, {"error": "代理拒绝重置请求"})
            return
        except (OSError, ValueError, urllib.error.URLError, RuntimeError) as exc:
            self._send_json(503, {"error": f"本地代理不可用：{exc.__class__.__name__}"})
            return
        self._send_json(200, result)

    def _serve_file(self, rel: str, content_type: str):
        base = Path(__file__).parent
        fpath = base / rel
        if not fpath.exists():
            self.send_error(404)
            return
        content = fpath.read_text()
        body = content.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_status(self):
        providers = self.reg.providers if self.reg else {}
        model_count = sum(len(p.models) for p in providers.values())
        credential_count = sum(bool(p.effective_key) for p in providers.values())
        status = {
            "version": __version__,
            "service": {
                "proxy_url": f"http://{self.cfg.proxy_host}:{self.cfg.proxy_port}/v1" if self.cfg else "",
                "ui_url": f"http://{self.cfg.ui_host}:{self.cfg.ui_port}" if self.cfg else "",
                "protocols": ["OpenAI Chat Completions", "OpenAI Responses", "Anthropic Messages", "MCP"],
                "auth_enabled": bool(self.token),
            },
            "summary": {
                "provider_count": len(providers),
                "model_count": model_count,
                "credential_count": credential_count,
                "auto_refresh_count": sum(p.auto_refresh for p in providers.values()),
            },
            "discovery": {
                "enabled": self.cfg.discovery_enabled if self.cfg else False,
                "interval_hours": self.cfg.discovery_interval_hours if self.cfg else 0,
                "auto_test": self.cfg.discovery_auto_test if self.cfg else False,
                "auto_adopt": self.cfg.discovery_auto_adopt if self.cfg else False,
            },
            "clients": ["pi", "omp", "opencode", "hermes", "codex", "claude", "kimi", "openclaw", "workbuddy"],
            "providers": [],
        }
        for name, p in providers.items():
            status["providers"].append({
                "name": name,
                "base_url": p.base_url,
                "prefix": p.model_prefix,
                "credential_configured": bool(p.effective_key),
                "credential_env": p.api_key_env,
                "auto_refresh": p.auto_refresh,
                "refresh_method": p.refresh_method,
                "model_count": len(p.models),
                "models": [m.id for m in p.models],
            })
        self._send_json(200, status)

    def _api_models(self):
        models = {}
        for name, p in (self.reg.providers if self.reg else {}).items():
            models[name] = [m.to_dict() for m in p.models]
        self._send_json(200, models)

    def _api_config_get(self):
        if not self.config_path or not self.config_path.exists():
            self._send_json(200, {"yaml": ""})
            return
        content = self.config_path.read_text()
        self._send_json(200, {"yaml": content})

    def _api_config_post(self):
        if not self.config_path:
            self._send_json(400, {"error": "config path not set"})
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
            yaml_text = data.get("yaml", "")
            import yaml
            parsed = yaml.safe_load(yaml_text)
            if not isinstance(parsed, dict):
                raise ValueError("config root must be a mapping")
        except Exception as e:
            self._send_json(400, {"error": str(e)})
            return
        self.config_path.write_text(yaml_text)
        self._send_json(200, {"ok": True, "saved": str(self.config_path)})

    def _mask_key(self, key: str) -> str:
        if not key:
            return ""
        return f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "***"

    def _api_providers(self):
        if not self.reg:
            self._send_json(200, {"providers": []})
            return
        providers = []
        for name, p in self.reg.providers.items():
            providers.append({
                "name": name,
                "base_url": p.base_url,
                "upstream_url": p.upstream_url or "",
                "api_key": "***" if p.api_key_env and p.effective_key else self._mask_key(p.effective_key),
                "api_key_env": p.api_key_env,
                "auto_refresh": p.auto_refresh,
                "refresh_method": p.refresh_method,
                "model_count": len(p.models),
                "models": [m.to_dict() for m in p.models],
            })
        self._send_json(200, {"providers": providers})

    def _api_refresh(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body)
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        provider_name = data.get("provider")
        from open_free_router.refresh import refresh
        results = refresh(self.reg, provider_name=provider_name)
        changed = any(v for v in results.values())
        if changed:
            assert self.reg is not None
            assert self.cfg is not None
            self.reg.save(self.cfg.registry_path)
        rebuild_proxy_index()
        if self.cfg:
            proxy_url = f"http://{self.cfg.proxy_host}:{self.cfg.proxy_port}/v1"
            write_pi_models(self.reg, proxy_url=proxy_url)
        self._send_json(200, {"ok": True, "results": results, "saved": changed})

    def _api_providers_post(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except Exception as e:
            self._send_json(400, {"error": f"invalid json: {e}"})
            return
        if not self.reg or not self.cfg:
            self._send_json(500, {"error": "server not initialized"})
            return
        name = data.get("name", "").strip()
        if not name:
            self._send_json(400, {"error": "name is required"})
            return
        existing = self.reg.get(name)
        api_key = data.get("api_key", existing.api_key if existing else "")
        api_key_env = data.get("api_key_env", existing.api_key_env if existing else "")
        base_url = data.get("base_url", existing.base_url if existing else "")
        upstream_url = data.get("upstream_url", existing.upstream_url if existing else "")
        models_raw = data.get("models", [])
        models = []
        for m in models_raw:
            if isinstance(m, str):
                models.append(ModelInfo(id=m))
            elif isinstance(m, dict):
                models.append(ModelInfo(
                    id=m.get("id", ""),
                    upstream_id=m.get("upstream_id", ""),
                    name=m.get("name", m.get("id", "")),
                    context_window=int(m.get("context_window", 131072) or 131072),
                    max_tokens=int(m.get("max_tokens", 8192) or 8192),
                    reasoning=bool(m.get("reasoning", False)),
                    tool_calling=bool(m.get("tool_calling", False)),
                ))
        p = ProviderConfig(
            name=name,
            base_url=base_url,
            upstream_url=upstream_url,
            api_key=api_key,
            api_key_env=api_key_env,
            models=models,
            auto_refresh=bool(data.get("auto_refresh", False)),
            refresh_method=data.get("refresh_method", "api" if data.get("auto_refresh") else "manual"),
            prefix=data.get("prefix", existing.prefix if existing else ""),
        )
        self.reg.add_provider(p)
        self.reg.save(self.cfg.registry_path)
        rebuild_proxy_index()
        if self.cfg:
            proxy_url = f"http://{self.cfg.proxy_host}:{self.cfg.proxy_port}/v1"
            write_pi_models(self.reg, proxy_url=proxy_url)
        self._send_json(200, {"ok": True, "provider": name, "models": len(models)})

    def _api_probe_get(self):
        """Current live-availability snapshot (read-only, no auth)."""
        snapshot = _PROBE_RUNNER.snapshot()
        if not snapshot["running"] and not snapshot["results"] and self.cfg:
            persisted = load_probe_snapshot(self.cfg.data_dir / "probe-results.json")
            if not persisted:
                persisted = load_status_as_probe_snapshot(self.cfg.data_dir / "probe-status.json")
            if persisted:
                for result in persisted["results"].values():
                    provider = self.reg.get(result["provider"]) if self.reg else None
                    if provider:
                        result["display_id"] = f"{provider.model_prefix}/{result['model']}"
                snapshot = persisted
        self._send_json(200, snapshot)

    def _api_probe_post(self):
        """Start a live availability probe run (real 1-token requests)."""
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body)
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if not self.reg:
            self._send_json(500, {"error": "server not initialized"})
            return

        def on_finish(snapshot):
            if self.cfg:
                try:
                    write_probe_status(snapshot, self.cfg.data_dir / "probe-status.json")
                    write_probe_snapshot(snapshot, self.cfg.data_dir / "probe-results.json")
                except Exception as e:
                    print(f"  ⚠ failed to persist probe status: {e}")

        started = _PROBE_RUNNER.start(
            self.reg,
            provider=data.get("provider", "") or "",
            model=data.get("model", "") or "",
            on_finish=on_finish,
        )
        self._send_json(200 if started else 409, {
            "ok": started,
            "running": True,
            "hint": "" if started else "a probe run is already in progress",
        })

    def _api_discovery_get(self):
        """Return the latest review-only discovery snapshot."""
        if not self.cfg or not self.cfg.discovery_path.exists():
            self._send_json(200, {"providers": [], "candidate_provider_count": 0,
                                  "candidate_model_count": 0})
            return
        try:
            payload = json.loads(self.cfg.discovery_path.read_text())
        except (OSError, ValueError):
            self._send_json(500, {"error": "候选快照无法读取"})
            return
        self._send_json(200, payload if isinstance(payload, dict) else {"providers": []})

    def _api_discovery_post(self):
        """Refresh review-only candidates; never auto-adopt from the dashboard."""
        if not self.reg or not self.cfg:
            self._send_json(500, {"error": "server not initialized"})
            return
        try:
            from open_free_router.discovery import discover, save_discovery
            snapshot = discover(self.reg)
            save_discovery(snapshot, self.cfg.discovery_path)
        except Exception as e:
            self._send_json(502, {"error": str(e)[:240]})
            return
        self._send_json(200, {
            "ok": True,
            "candidate_provider_count": snapshot.get("candidate_provider_count", 0),
            "candidate_model_count": snapshot.get("candidate_model_count", 0),
        })

    def _api_sync_post(self):
        """Synchronize explicitly selected local clients with the proxy."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length) if length else b"{}")
        except Exception:
            data = {}
        allowed = {"pi", "omp", "opencode", "hermes", "codex", "claude", "kimi", "openclaw", "workbuddy"}
        requested = data.get("agents", []) if isinstance(data, dict) else []
        agents = [str(agent) for agent in requested if str(agent) in allowed]
        if not agents:
            self._send_json(400, {"error": "请至少选择一个受支持的客户端"})
            return
        if not self.reg or not self.cfg:
            self._send_json(500, {"error": "server not initialized"})
            return
        from open_free_router.sync import sync_all
        results = sync_all(
            self.reg,
            do_write=True,
            agents=agents,
            proxy_url=f"http://{self.cfg.proxy_host}:{self.cfg.proxy_port}/v1",
            proxy_token=get_or_create_proxy_token(self.cfg.config_dir),
            codex_model=self.cfg.codex_model,
            claude_model=self.cfg.claude_model,
        )
        self._send_json(200, {"ok": True, "results": results})

    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def run_ui(cfg: Config, port: int = 9527, reg: Registry | None = None):
    _UIHandler.cfg = cfg
    _UIHandler.reg = reg or Registry.load(cfg.registry_path)
    _UIHandler.config_path = cfg.path
    config_dir = cfg.path.parent if cfg.path else Path.home() / ".config" / "open-free-router"
    _UIHandler.token = get_or_create_token(config_dir)
    srv = ThreadingHTTPServer((cfg.ui_host, port), _UIHandler)
    print(f"🌐 Dashboard: http://{cfg.ui_host}:{port}")
    print(f"🔑 Auth token stored at: {config_dir / 'ui.token'} "
          f"(dashboard will prompt for it once)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv.server_close()
