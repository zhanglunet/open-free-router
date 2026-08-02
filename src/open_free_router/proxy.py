"""Free model proxy — single port, routes by model ID to upstream provider.

GET  /v1/models           → all free models from registry
POST /v1/chat/completions → forward to the correct upstream by model ID
POST /v1/completions      → same routing, legacy completions endpoint
POST /v1/embeddings       → same routing, embeddings endpoint
POST /v1/responses        → Codex Responses API over Chat Completions
POST /v1/messages         → Anthropic Messages API over Chat Completions
POST /v1/messages/count_tokens → local input-token estimate
"""
from __future__ import annotations

import json
import re
import sys
import threading
import weakref
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import ClassVar

from open_free_router.registry import Registry, codex_model_alias
from open_free_router.routing import RoutePlanner, RoutingConfig
from open_free_router.executor import OpenedRoute, RouteFailure, UpstreamExecutor
from open_free_router.resilience import ResilienceManager
from open_free_router.telemetry import RouteDecisionStore
from open_free_router.auth import check_auth
from open_free_router.analytics import AnalyticsStore, ExportSecurityError
from open_free_router.anthropic import (
    AnthropicConversionError,
    AnthropicStreamAdapter,
    anthropic_error,
    chat_to_messages,
    count_tokens_estimate,
    messages_to_chat,
)
from open_free_router.responses import (
    ResponsesConversionError,
    ResponsesStreamAdapter,
    chat_to_response,
    parse_chat_sse,
    responses_to_chat,
    sse_event,
)


_ACTIVE_HANDLERS: weakref.WeakSet[type] = weakref.WeakSet()
_ACTIVE_HANDLERS_LOCK = threading.Lock()


def _safe_header_value(value: str, limit: int = 256) -> str:
    """Return an ASCII routing label safe for an HTTP response header."""
    return re.sub(r"[^A-Za-z0-9._:/-]+", "-", str(value)).strip("-")[:limit]


def _normalise_reasoning_content(body: bytes) -> bytes:
    """Fix reasoning-model responses where message.content is null.

    Many reasoning models return:
        {"content": null, "reasoning_content": "actual output..."}
    Agents that only read message.content get null and crash. When content
    is null, copy reasoning_content (or fall back to empty string) so every
    response has a string content field.
    """
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body  # not JSON, pass through unchanged
    changed = False
    for choice in obj.get("choices", []):
        msg = choice.get("message")
        if not msg or msg.get("content") is not None:
            continue
        rc = msg.get("reasoning_content")
        if rc is not None:
            msg["content"] = rc
        else:
            msg["content"] = ""
        changed = True
    return json.dumps(obj).encode() if changed else body


def _normalise_error(body: bytes) -> tuple[dict, str]:
    """Return a standard OpenAI error and a plain message for other envelopes.

    Vendor trace IDs, account fields and arbitrary nested extensions must not
    cross protocol boundaries.  Only the four standard OpenAI error members
    are retained.
    """
    text = body.decode("utf-8", errors="replace")[:2048]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or "upstream request failed")[:2048]
        normalized = {"message": message}
        for key in ("type", "param", "code"):
            value = error.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                if key in error:
                    normalized[key] = value
        return {"error": normalized}, message
    message = text or "upstream request failed"
    return {"error": {"message": message, "type": "upstream_error"}}, message


class _ProxyHandler(BaseHTTPRequestHandler):
    # Nagle's algorithm + delayed ACK otherwise coalesces the small
    # writes _forward_streaming does per SSE chunk, adding tens of ms of
    # jitter per hop and defeating the point of streaming at all.
    disable_nagle_algorithm = True
    protocol_version = "HTTP/1.1"

    registry: Registry | None = None
    auth_token: str = ""
    _model_index: ClassVar[dict[str, str]] = {}  # model_id → provider_name
    _index_lock: ClassVar[threading.Lock] = threading.Lock()
    routing_config: ClassVar[RoutingConfig] = RoutingConfig()
    route_planner: ClassVar[RoutePlanner | None] = None
    resilience_manager: ClassVar[ResilienceManager] = ResilienceManager()
    decision_store: ClassVar[RouteDecisionStore] = RouteDecisionStore()
    analytics_store: ClassVar[AnalyticsStore | None] = None

    def handle(self):
        """Ignore normal client disconnects without hiding server failures."""
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            # Codex may close an idle HTTP/1.1 keep-alive socket after the
            # response completes. socketserver otherwise prints a misleading
            # traceback even though the request succeeded.
            pass

    @classmethod
    def rebuild_index(cls):
        idx = {}
        if cls.registry:
            for name, p in cls.registry.providers.items():
                prefix = p.model_prefix
                for m in p.models:
                    # Register all forms of the model ID so agents can use
                    # whichever format they prefer:
                    #   1. bare id       (e.g. glm-5.2)
                    #   2. prefix/id     (e.g. nv/glm-5.2)
                    #   3. upstream_id   (e.g. z-ai/glm-5.2) — matches what
                    #      Hermes and other agents send when they show the
                    #      "provider/model" label to users
                    #   4. provider/upstream_id (e.g. openrouter/gpt-oss-20b:free)
                    #      OMP and other agents use this format
                    #   5. Codex-safe alias (e.g. ofr-or-gpt-oss-20b-free)
                    idx[m.id] = name
                    prefixed = f"{prefix}/{m.id}"
                    if prefixed not in idx:
                        idx[prefixed] = name
                    uid = m.effective_upstream_id
                    if uid != m.id:
                        idx[uid] = name
                        provider_prefixed = f"{name}/{uid}"
                        if provider_prefixed not in idx:
                            idx[provider_prefixed] = name
                    idx[codex_model_alias(prefix, m.id)] = name
        with cls._index_lock:
            cls._model_index = idx
            cls.route_planner = RoutePlanner(cls.registry, cls.routing_config) if cls.registry else None

    def _find_provider(self, model_id: str) -> str | None:
        with self._index_lock:
            return self._model_index.get(model_id)

    def _send_json(
        self,
        code: int,
        obj: dict,
        retry_after: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if retry_after:
            self.send_header("Retry-After", retry_after)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_content(self, code: int, content: str, content_type: str,
                      filename: str | None = None):
        body = content.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def _record_usage(self, opened: OpenedRoute, usage: dict | None) -> None:
        self.decision_store.set_usage(opened.request_id, usage)

    def _quota_estimates(self, days: int) -> list[dict]:
        """Compare local observations with verified registry claims; never call billing APIs."""
        if not self.analytics_store or not self.registry:
            return []
        windows = {"daily": 1, "weekly": 7, "monthly": 30}
        summaries = {}
        estimates = []
        for provider_name, provider in self.registry.providers.items():
            for model in provider.models:
                evidence = provider.free_tier_for(model)
                if evidence.status() != "verified" or evidence.limit is None:
                    continue
                period_days = windows.get(evidence.reset_period.lower(), max(1, days))
                if period_days not in summaries:
                    summary = self.analytics_store.summary(period_days)
                    summaries[period_days] = {
                        (row["provider"], row["model"]): row for row in summary.get("groups", [])
                    }
                canonical = f"{provider.model_prefix}/{model.id}"
                observed = summaries[period_days].get((provider_name, canonical), {})
                token_unit = "token" in evidence.unit.lower()
                used = (
                    int(observed.get("input_tokens", 0)) + int(observed.get("output_tokens", 0))
                    if token_unit else int(observed.get("requests", 0))
                )
                estimates.append({
                    "provider": provider_name,
                    "model": canonical,
                    "limit": evidence.limit,
                    "unit": evidence.unit,
                    "reset_period": evidence.reset_period,
                    "period_days": period_days,
                    "observed_usage": used,
                    "estimated_ratio": round(used / evidence.limit, 6) if evidence.limit > 0 else None,
                    "estimated": True,
                })
        return estimates

    @staticmethod
    def _route_headers(result: OpenedRoute | RouteFailure) -> dict[str, str]:
        headers = {
            "X-OFR-Request-Id": result.request_id,
            "X-OFR-Fallback-Attempts": str(result.fallback_attempts),
        }
        target = result.target if isinstance(result, OpenedRoute) else result.last_target
        if target:
            headers["X-OFR-Provider"] = _safe_header_value(target.provider_name)
            headers["X-OFR-Model"] = _safe_header_value(target.canonical_id)
        return headers

    def _execute_route(self, model_id: str, endpoint_suffix: str, payload: dict):
        planner = self.route_planner
        if not planner:
            return RouteFailure(
                "ofr_unavailable", 503,
                b'{"error":{"message":"router is not initialized"}}',
                "application/json", None, 0, (),
            )
        timeout = getattr(self, "_upstream_timeout", 120)
        return UpstreamExecutor(
            planner, self.resilience_manager, timeout=timeout, decisions=self.decision_store
        ).execute(model_id, endpoint_suffix, payload)

    def _send_route_failure(self, failure: RouteFailure, anthropic: bool = False):
        code = failure.status
        payload, message = _normalise_error(failure.body)
        if anthropic:
            if failure.last_target is None and code == 403:
                code = 404
            payload = anthropic_error(code, message)
        self._send_json(
            code,
            payload,
            retry_after=failure.retry_after,
            extra_headers=self._route_headers(failure),
        )

    def _send_opened_buffered(
        self,
        opened: OpenedRoute,
        transform=None,
        normalize_reasoning: bool = False,
        error_factory=None,
    ):
        try:
            try:
                body = opened.response.read()
                parsed = None
                if transform or "json" in opened.response.getheader("Content-Type", "").lower():
                    parsed = json.loads(body)
                    self._record_usage(opened, parsed.get("usage"))
                if transform:
                    body = json.dumps(transform(parsed)).encode()
                elif normalize_reasoning and "json" in opened.response.getheader("Content-Type", "").lower():
                    body = _normalise_reasoning_content(body)
            except Exception as exc:
                self.decision_store.mark_failure(opened.request_id, 502, "invalid_upstream_response")
                message = f"Invalid upstream response: {exc}"
                self._send_json(
                    502,
                    error_factory(502, message) if error_factory else {
                        "error": {"message": message, "type": "upstream_error"}
                    },
                    extra_headers=self._route_headers(opened),
                )
                return
            self.send_response(opened.response.status)
            self.send_header(
                "Content-Type",
                opened.response.getheader("Content-Type", "application/json"),
            )
            for name, value in self._route_headers(opened).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        finally:
            opened.close()

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query = parse_qs(parsed_url.query)
        if path == "/api/resilience":
            if not self.auth_token or not check_auth(self.headers, self.auth_token):
                self._send_json(401, {"error": "unauthorized"})
                return
            self._send_json(200, self.resilience_manager.snapshot())
            return
        if path == "/api/routes":
            if not self.auth_token or not check_auth(self.headers, self.auth_token):
                self._send_json(401, {"error": "unauthorized"})
                return
            self._send_json(200, self.decision_store.snapshot())
            return
        if path in ("/api/metrics", "/api/metrics/export"):
            if not self.auth_token or not check_auth(self.headers, self.auth_token):
                self._send_json(401, {"error": "unauthorized"})
                return
            if not self.analytics_store:
                self._send_json(200, {"enabled": False, "retention_days": 0})
                return
            try:
                days = int((query.get("days") or [30])[0])
            except (TypeError, ValueError):
                days = 30
            if path == "/api/metrics":
                summary = self.analytics_store.summary(days)
                summary["quota_estimates"] = self._quota_estimates(days)
                self._send_json(200, summary)
                return
            format_name = str((query.get("format") or ["json"])[0]).lower()
            try:
                content, content_type = self.analytics_store.export(format_name, days)
            except (ValueError, ExportSecurityError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_content(
                200, content, content_type,
                f"open-free-router-usage.{format_name}",
            )
            return
        if path.startswith("/api/routes/"):
            if not self.auth_token or not check_auth(self.headers, self.auth_token):
                self._send_json(401, {"error": "unauthorized"})
                return
            item = self.decision_store.get(path.removeprefix("/api/routes/"))
            self._send_json(200, item) if item else self._send_json(404, {"error": "not found"})
            return
        if path in ("/", "/health"):
            self._send_json(200, {
                "service": "open-free-router",
                "version": "0.1",
                "endpoints": {
                    "models": "/v1/models",
                    "chat": "/v1/chat/completions",
                    "responses": "/v1/responses",
                    "messages": "/v1/messages",
                    "count_tokens": "/v1/messages/count_tokens",
                    "completions": "/v1/completions",
                    "embeddings": "/v1/embeddings",
                    "ui": f"http://{self.server.server_address[0]}:9057",
                },
                "docs": "https://github.com/NoelJudeNoel/open-free-router",
            })
            return
        if path == "/v1/models":
            self._handle_list_models()
            return
        self._send_json(404, {"error": "not found"})

    # Generous but bounded — protects the proxy process from a client (or a
    # buggy/malicious one, if this ever ends up reachable beyond localhost)
    # sending an unbounded body. 25MB comfortably covers long-context chat
    # payloads; nothing in this proxy's supported endpoints needs more.
    MAX_BODY_BYTES: ClassVar[int] = 25 * 1024 * 1024

    def do_POST(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path

        if path == "/api/resilience/reset":
            self._handle_resilience_reset()
            return

        endpoint_map = {
            "/v1/chat/completions": "chat/completions",
            "/v1/completions": "completions",
            "/v1/embeddings": "embeddings",
            "/v1/responses": "responses",
            "/v1/messages": "messages",
            "/v1/messages/count_tokens": "count_tokens",
        }
        upstream_suffix = endpoint_map.get(path)
        if not upstream_suffix:
            # Drain and discard so the connection can be reused/closed
            # cleanly, but don't bother enforcing the size limit on a
            # request we're rejecting anyway.
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                self.rfile.read(min(length, self.MAX_BODY_BYTES))
            self._send_json(404, {"error": "not found"})
            return

        if self.auth_token and not check_auth(self.headers, self.auth_token):
            if upstream_suffix in ("messages", "count_tokens"):
                self._send_json(
                    401, anthropic_error(401, "Missing or invalid proxy token.")
                )
            else:
                self._send_json(401, {
                    "error": {
                        "message": "Missing or invalid proxy bearer token.",
                        "type": "authentication_error",
                    }
                })
            self.close_connection = True
            return

        anthropic_endpoint = upstream_suffix in ("messages", "count_tokens")
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header) if length_header is not None else None
        except ValueError:
            length = None
        if length is None:
            message = "Content-Length required"
            self._send_json(
                411,
                anthropic_error(411, message) if anthropic_endpoint else {"error": message},
            )
            return
        if length > self.MAX_BODY_BYTES:
            message = f"request body too large (> {self.MAX_BODY_BYTES} bytes)"
            self._send_json(
                413,
                anthropic_error(413, message) if anthropic_endpoint else {"error": message},
            )
            self.close_connection = True
            return

        body = self.rfile.read(length).decode("utf-8", errors="replace")
        if upstream_suffix == "responses":
            self._forward_responses(body)
        elif upstream_suffix == "messages":
            self._forward_messages(body)
        elif upstream_suffix == "count_tokens":
            self._handle_count_tokens(body)
        else:
            self._forward_request(upstream_suffix, body)

    def _handle_resilience_reset(self):
        if not self.auth_token or not check_auth(self.headers, self.auth_token):
            self._send_json(401, {"error": "unauthorized"})
            # Do not leave an unread request body on a reusable HTTP/1.1
            # connection; the next request would parse those bytes as a new
            # request line/body. Closing matches the inference auth path.
            self.close_connection = True
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send_json(411, {"error": "Content-Length required"})
            return
        if length < 0 or length > 64 * 1024:
            self._send_json(413, {"error": "request body too large"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid json"})
            return
        provider = payload.get("provider") if isinstance(payload, dict) else None
        model = payload.get("model") if isinstance(payload, dict) else None
        if not isinstance(provider, str) or not provider.strip():
            self._send_json(400, {"error": "provider is required"})
            return
        if model is not None and not isinstance(model, str):
            self._send_json(400, {"error": "model must be a string"})
            return
        self.resilience_manager.reset(provider.strip(), model.strip() if model else None)
        self._send_json(200, {"ok": True, "provider": provider.strip(), "model": model or None})

    def _handle_list_models(self):
        if not self.registry:
            self._send_json(200, {"object": "list", "data": []})
            return
        items = []
        for name, p in self.registry.providers.items():
            for m in p.models:
                # Show provider-prefixed ID so users can distinguish upstreams
                items.append({
                    "id": f"{p.model_prefix}/{m.id}",
                    "object": "model",
                    "created": 0,
                    "owned_by": name,
                })
        if self.route_planner:
            items.extend({
                "id": model_id,
                "object": "model",
                "created": 0,
                "owned_by": "open-free-router",
            } for model_id in self.route_planner.virtual_model_ids())
        # Codex probes the same endpoint and expects a top-level `models`
        # field. It can safely use fallback metadata for custom model IDs.
        self._send_json(200, {"object": "list", "data": items, "models": []})

    def _resolve_model(self, model_id: str):
        provider_name = self._find_provider(model_id)
        if not provider_name and self.route_planner:
            selected = self.route_planner.plan(model_id).selected
            if selected:
                return selected.provider, selected.upstream_model_id
        if not provider_name:
            return None, None
        p = self.registry.get(provider_name) if self.registry else None
        if not p:
            return None, None
        upstream_model_id = model_id
        for m in p.models:
            display = f"{p.model_prefix}/{m.id}"
            prov_upstream = f"{p.name}/{m.effective_upstream_id}"
            codex_alias = codex_model_alias(p.model_prefix, m.id)
            if (display == model_id or m.id == model_id
                    or m.effective_upstream_id == model_id
                    or prov_upstream == model_id or codex_alias == model_id):
                upstream_model_id = m.effective_upstream_id
                break
        return p, upstream_model_id

    def _forward_request(self, endpoint_suffix: str, body: str):
        """Route + forward a POST to /v1/chat/completions, /v1/completions,
        or /v1/embeddings — model lookup and upstream routing are the same
        for all three; only the upstream path suffix and (for chat/
        completions) streaming support differ."""
        try:
            req = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid json"})
            return

        model_id = req.get("model", "")
        # Strip non-standard fields before forwarding. Clients (agents,
        # IDEs) often attach fields their own model supports but the
        # upstream we forward to does not — e.g. prompt_cache_key,
        # reasoning_content in message history, thinking, reasoning_effort.
        # These cause 400 errors on strict upstreams (Groq, DeepSeek, etc).
        # Use a whitelist of standard OpenAI API fields so we don't have to
        # chase every new non-standard field.
        _ALLOWED_TOP_KEYS = {
            "model", "messages", "max_tokens", "max_completion_tokens",
            "temperature", "top_p", "n", "stream", "stream_options",
            "stop", "frequency_penalty", "presence_penalty",
            "logit_bias", "logprobs", "top_logprobs", "response_format",
            "seed", "tools", "tool_choice", "parallel_tool_calls",
            "user", "functions", "function_call",
        }
        for k in list(req.keys()):
            if k not in _ALLOWED_TOP_KEYS:
                del req[k]
        # Convert max_completion_tokens → max_tokens. Many newer OpenAI models
        # use max_completion_tokens, but most upstream providers (OpenRouter
        # free models, Groq, etc.) only accept max_tokens. Without this, a
        # request carrying max_completion_tokens causes a 422 on those upstreams.
        if "max_completion_tokens" in req:
            if "max_tokens" not in req:
                req["max_tokens"] = req.pop("max_completion_tokens")
            else:
                del req["max_completion_tokens"]
        # Normalize messages:
        # 1. "developer" role → "system" (older OpenAI-compatible APIs)
        # 2. Strip non-standard message fields (reasoning_content, etc.)
        _ALLOWED_MSG_KEYS = {
            "role", "content", "name", "tool_calls", "tool_call_id",
            "function_call", "refusal",
        }
        for msg in req.get("messages", []):
            if msg.get("role") == "developer":
                msg["role"] = "system"
            for k in list(msg.keys()):
                if k not in _ALLOWED_MSG_KEYS:
                    del msg[k]
        is_stream = endpoint_suffix != "embeddings" and bool(req.get("stream"))
        routed = self._execute_route(model_id, endpoint_suffix, req)
        if isinstance(routed, RouteFailure):
            self._send_route_failure(routed)
            return
        if is_stream:
            self._relay_openai_stream(routed)
        else:
            self._send_opened_buffered(routed, normalize_reasoning=True)

    def _forward_responses(self, body: str):
        try:
            request = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": {"message": "invalid json", "type": "invalid_request_error"}})
            return

        requested_model = request.get("model", "")
        try:
            chat_request = responses_to_chat(request)
        except ResponsesConversionError as e:
            self._send_json(400, {"error": {"message": str(e), "type": "invalid_request_error"}})
            return
        streaming = bool(request.get("stream"))
        chat_request["stream"] = streaming
        if streaming:
            chat_request["stream_options"] = {"include_usage": True}

        routed = self._execute_route(requested_model, "chat/completions", chat_request)
        if isinstance(routed, RouteFailure):
            self._send_route_failure(routed)
            return
        if streaming:
            self._relay_responses_streaming(routed, requested_model)
            return
        self._send_opened_buffered(
            routed,
            transform=lambda response: chat_to_response(response, requested_model),
        )

    def _relay_responses_streaming(self, opened: OpenedRoute, requested_model: str):
        started = False
        adapter = None
        try:
            self.send_response(opened.response.status)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            for name, value in self._route_headers(opened).items():
                self.send_header(name, value)
            self.end_headers()
            started = True
            adapter = ResponsesStreamAdapter(requested_model)

            def write_event(event: dict):
                chunk = sse_event(event)
                self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii"))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

            for event in adapter.start():
                write_event(event)
            for chat_chunk in parse_chat_sse(iter(opened.response.readline, b"")):
                for event in adapter.feed(chat_chunk):
                    write_event(event)
            for event in adapter.finish():
                write_event(event)
            done = b"data: [DONE]\n\n"
            self.wfile.write(f"{len(done):x}\r\n".encode("ascii"))
            self.wfile.write(done)
            self.wfile.write(b"\r\n0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            if started:
                print(f"  ⚠ Responses stream ended early: {exc}", file=sys.stderr)
        finally:
            if adapter is not None:
                self._record_usage(opened, adapter.usage)
            opened.close()

    def _handle_count_tokens(self, body: str):
        """Local estimate — upstreams have no Anthropic token counter."""
        try:
            request = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, anthropic_error(400, "invalid json"))
            return
        self._send_json(200, {"input_tokens": count_tokens_estimate(request)})

    def _forward_messages(self, body: str):
        """Anthropic Messages API endpoint used by Claude Code."""
        try:
            request = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, anthropic_error(400, "invalid json"))
            return

        requested_model = request.get("model", "")
        try:
            chat_request = messages_to_chat(request)
        except AnthropicConversionError as e:
            self._send_json(400, anthropic_error(400, str(e)))
            return
        streaming = bool(request.get("stream"))
        chat_request["stream"] = streaming
        if streaming:
            chat_request["stream_options"] = {"include_usage": True}

        routed = self._execute_route(requested_model, "chat/completions", chat_request)
        if isinstance(routed, RouteFailure):
            self._send_route_failure(routed, anthropic=True)
            return
        if streaming:
            self._relay_messages_streaming(routed, requested_model)
            return
        self._send_opened_buffered(
            routed,
            transform=lambda response: chat_to_messages(response, requested_model),
            error_factory=anthropic_error,
        )

    def _relay_messages_streaming(self, opened: OpenedRoute, requested_model: str):
        """Translate an accepted Chat SSE response to Anthropic Messages SSE."""
        started = False
        adapter = None
        try:
            self.send_response(opened.response.status)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            for name, value in self._route_headers(opened).items():
                self.send_header(name, value)
            self.end_headers()
            started = True
            adapter = AnthropicStreamAdapter(requested_model)

            def write_event(event: dict):
                chunk = sse_event(event)
                self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii"))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

            for event in adapter.start():
                write_event(event)
            for chat_chunk in parse_chat_sse(iter(opened.response.readline, b"")):
                for event in adapter.feed(chat_chunk):
                    write_event(event)
            for event in adapter.finish():
                write_event(event)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            if started:
                print(f"  ⚠ Messages stream ended early: {exc}", file=sys.stderr)
        finally:
            if adapter is not None:
                self._record_usage(opened, adapter.usage)
            opened.close()

    def _relay_openai_stream(self, opened: OpenedRoute):
        """Relay an already accepted upstream SSE response.

        Candidate switching has finished before this method sends response
        headers. Once headers are sent, an upstream/client disconnect only ends
        this stream and can never replay the request to another model.
        """
        response = opened.response
        started = False
        usage = None
        try:
            self.send_response(response.status)
            self.send_header(
                "Content-Type", response.getheader("Content-Type", "text/event-stream")
            )
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            for name, value in self._route_headers(opened).items():
                self.send_header(name, value)
            self.end_headers()
            started = True
            while True:
                line = response.readline()
                if not line:
                    break
                if line.startswith(b"data:"):
                    try:
                        chunk = json.loads(line[5:].strip())
                        if isinstance(chunk.get("usage"), dict):
                            usage = chunk["usage"]
                    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                        pass
                self.wfile.write(f"{len(line):x}\r\n".encode("ascii"))
                self.wfile.write(line)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            if started:
                print(f"  ⚠ Upstream stream ended early: {exc}", file=sys.stderr)
        finally:
            self._record_usage(opened, usage)
            opened.close()

    def log_message(self, format, *args):
        pass


def run_proxy(
    registry: Registry,
    host: str = "127.0.0.1",
    port: int = 8337,
    upstream_timeout: int = 120,
    auth_token: str = "",
    routing: RoutingConfig | dict | None = None,
    resilience: ResilienceManager | None = None,
    decisions: RouteDecisionStore | None = None,
    analytics: AnalyticsStore | None = None,
):
    decision_store = decisions or RouteDecisionStore(analytics=analytics)
    if analytics is not None:
        decision_store.analytics = analytics
    handler = type("Handler", (_ProxyHandler,), {
        "registry": registry,
        "_upstream_timeout": upstream_timeout,
        "auth_token": auth_token,
        "routing_config": routing if isinstance(routing, RoutingConfig) else RoutingConfig.from_dict(routing),
        "resilience_manager": resilience or ResilienceManager(),
        "decision_store": decision_store,
        "analytics_store": analytics,
    })
    handler.rebuild_index()
    with _ACTIVE_HANDLERS_LOCK:
        _ACTIVE_HANDLERS.add(handler)
    srv = ThreadingHTTPServer((host, port), handler)
    print(f"  Proxy  : {host}:{port} (single-port, model-ID routing)")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, handler


def rebuild_proxy_index():
    """Rebuild the model-ID → provider reverse index."""
    with _ACTIVE_HANDLERS_LOCK:
        handlers = list(_ACTIVE_HANDLERS)
    for handler in handlers:
        handler.rebuild_index()
