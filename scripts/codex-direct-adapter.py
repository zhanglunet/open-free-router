#!/usr/bin/env python3
"""Expose a local Responses endpoint backed by one official Chat API.

This adapter is intentionally provider-pinned: it does not load the router's
model registry for routing or fallback. It only reads the selected provider's
official endpoint and API key, then translates Codex Responses requests.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from open_free_router.responses import (
    ResponsesConversionError,
    ResponsesStreamAdapter,
    chat_to_response,
    parse_chat_sse,
    responses_to_chat,
    sse_event,
)


HOME = Path.home()
CONFIG_DIR = HOME / ".config" / "open-free-router"
REGISTRY = CONFIG_DIR / "registry.yaml"
PROVIDERS = {
    "deepseek": {
        "registry_name": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "models": {
            "deepseek-v4-flash": "deepseek-v4-flash",
            "deepseek-v4-pro": "deepseek-v4-pro",
        },
    },
    "glm": {
        "registry_name": "bigmodel-glm-coding",
        "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
        "models": {
            "glm-4.5": "glm-4.5",
            "glm-4.5-air": "glm-4.5-air",
            "glm-4.6": "glm-4.6",
            "glm-4.7": "glm-4.7",
            "glm-5": "glm-5",
            "glm-5-turbo": "glm-5-turbo",
            "glm-5.1": "glm-5.1",
            "glm-5.2": "glm-5.2",
        },
    },
}


def token_path(provider: str) -> Path:
    return CONFIG_DIR / f"codex-direct-{provider}.token"


def local_token(provider: str) -> str:
    path = token_path(provider)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(32) + "\n")
        path.chmod(0o600)
    return path.read_text().strip()


def provider_key(provider: str) -> str:
    cfg = PROVIDERS[provider]
    data = yaml.safe_load(REGISTRY.read_text()) or {}
    key = str(data.get(cfg["registry_name"], {}).get("api_key", "")).strip()
    if not key:
        raise RuntimeError(f"missing API key for {provider} in {REGISTRY}")
    return key


def upstream_model(provider: str, model: str) -> str:
    models = PROVIDERS[provider]["models"]
    if model in models:
        return models[model]
    raise ValueError(f"model {model!r} is not allowed for profile {provider}")


class Handler(BaseHTTPRequestHandler):
    server_version = "codex-direct-adapter/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[codex-direct] " + (fmt % args) + "\n")

    @property
    def provider(self) -> str:
        return self.server.provider  # type: ignore[attr-defined]

    def send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        expected = local_token(self.provider)
        return self.headers.get("Authorization", "") == f"Bearer {expected}"

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {"ok": True, "provider": self.provider, "mode": "official-upstream"})
            return
        if self.path == "/v1/models":
            if not self.authorized():
                self.send_json(401, {"error": {"message": "unauthorized", "type": "authentication_error"}})
                return
            data = [
                {"id": model, "object": "model", "created": 0, "owned_by": self.provider}
                for model in PROVIDERS[self.provider]["models"]
            ]
            self.send_json(200, {"object": "list", "data": data})
            return
        self.send_json(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

    def do_POST(self):
        if self.path != "/v1/responses":
            self.send_json(404, {"error": {"message": "only /v1/responses is supported", "type": "invalid_request_error"}})
            return
        if not self.authorized():
            self.send_json(401, {"error": {"message": "unauthorized", "type": "authentication_error"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16 * 1024 * 1024:
                raise ValueError("invalid request body size")
            request = json.loads(self.rfile.read(length))
            requested_model = str(request.get("model", ""))
            chat = responses_to_chat(request)
            chat["model"] = upstream_model(self.provider, requested_model)
            body = json.dumps(chat, ensure_ascii=False).encode()
        except (ValueError, TypeError, json.JSONDecodeError, ResponsesConversionError) as exc:
            self.send_json(400, {"error": {"message": str(exc)[:500], "type": "invalid_request_error"}})
            return

        request_headers = {
            "Authorization": f"Bearer {provider_key(self.provider)}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if chat.get("stream") else "application/json",
        }
        upstream = PROVIDERS[self.provider]["base_url"].rstrip("/") + "/chat/completions"
        upstream_request = urllib.request.Request(upstream, data=body, headers=request_headers, method="POST")
        started = time.time()
        try:
            with urllib.request.urlopen(upstream_request, timeout=180) as response:
                if chat.get("stream"):
                    self._stream(response, requested_model)
                else:
                    payload = json.loads(response.read().decode("utf-8", "replace"))
                    self._send_response(chat_to_response(payload, requested_model))
            self.log_message("provider=%s model=%s status=200 latency_ms=%d", self.provider, requested_model, int((time.time() - started) * 1000))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                upstream_error = json.loads(raw).get("error", {})
                message = upstream_error.get("message", "upstream request failed") if isinstance(upstream_error, dict) else str(upstream_error)
            except json.JSONDecodeError:
                message = "upstream request failed"
            self.send_json(exc.code, {"error": {"message": str(message)[:1000], "type": "upstream_error"}})
        except Exception as exc:
            self.send_json(502, {"error": {"message": str(exc)[:1000], "type": "upstream_error"}})

    def _send_response(self, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, response, model: str):
        adapter = ResponsesStreamAdapter(model)
        events = adapter.start()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def emit(event: dict):
            self.wfile.write(sse_event(event))
            self.wfile.flush()

        for event in events:
            emit(event)
        for chunk in parse_chat_sse(response):
            for event in adapter.feed(chunk):
                emit(event)
        for event in adapter.finish():
            emit(event)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=sorted(PROVIDERS), required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=False)
    parser.add_argument("--token", action="store_true")
    args = parser.parse_args()
    if args.token:
        print(local_token(args.provider))
        return
    port = args.port or {"deepseek": 8341, "glm": 8342}[args.provider]
    server = ThreadingHTTPServer((args.host, port), Handler)
    server.provider = args.provider  # type: ignore[attr-defined]
    print(f"codex-direct-adapter provider={args.provider} listen={args.host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
