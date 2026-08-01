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

import http.client
import json
import socket
import sys
import threading
import weakref
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import URLError

from open_free_router.registry import Registry, codex_model_alias
from open_free_router.auth import check_auth
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

    def _find_provider(self, model_id: str) -> str | None:
        with self._index_lock:
            return self._model_index.get(model_id)

    def _send_json(self, code: int, obj: dict, retry_after: str | None = None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if retry_after:
            self.send_header("Retry-After", retry_after)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        if path == "/":
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

        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header) if length_header is not None else None
        except ValueError:
            length = None
        if length is None:
            self._send_json(411, {"error": "Content-Length required"})
            return
        if length > self.MAX_BODY_BYTES:
            self._send_json(413, {"error": f"request body too large (> {self.MAX_BODY_BYTES} bytes)"})
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
        # Codex probes the same endpoint and expects a top-level `models`
        # field. It can safely use fallback metadata for custom model IDs.
        self._send_json(200, {"object": "list", "data": items, "models": []})

    def _resolve_model(self, model_id: str):
        provider_name = self._find_provider(model_id)
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
        p, upstream_model_id = self._resolve_model(model_id)
        if not p:
            self._send_json(403, {
                "error": {
                    "message": f"Model '{model_id}' not in free whitelist.",
                    "type": "proxy_error",
                }
            })
            return

        if not (p.upstream_url or p.base_url):
            self._send_json(502, {"error": "provider not configured"})
            return
        req["model"] = upstream_model_id
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
        data = json.dumps(req).encode()

        upstream = (p.upstream_url or p.base_url).rstrip("/")
        key = p.effective_key
        url = f"{upstream}/{endpoint_suffix}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "open-free-router/0.1",
        }
        timeout = getattr(self, "_upstream_timeout", 120)

        if is_stream:
            self._forward_streaming(url, data, headers, timeout)
            return

        self._forward_buffered(url, data, headers, timeout)

    def _forward_responses(self, body: str):
        try:
            request = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": {"message": "invalid json", "type": "invalid_request_error"}})
            return

        requested_model = request.get("model", "")
        p, upstream_model = self._resolve_model(requested_model)
        if not p:
            self._send_json(403, {"error": {
                "message": f"Model '{requested_model}' not in free whitelist.",
                "type": "proxy_error",
            }})
            return
        if not (p.upstream_url or p.base_url):
            self._send_json(502, {"error": {"message": "provider not configured", "type": "proxy_error"}})
            return
        try:
            chat_request = responses_to_chat(request)
        except ResponsesConversionError as e:
            self._send_json(400, {"error": {"message": str(e), "type": "invalid_request_error"}})
            return
        chat_request["model"] = upstream_model
        streaming = bool(request.get("stream"))
        chat_request["stream"] = streaming
        if streaming:
            chat_request["stream_options"] = {"include_usage": True}

        upstream = (p.upstream_url or p.base_url).rstrip("/")
        url = f"{upstream}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {p.effective_key}",
            "User-Agent": "open-free-router/0.1",
        }
        data = json.dumps(chat_request).encode()
        timeout = getattr(self, "_upstream_timeout", 120)
        if streaming:
            self._forward_responses_streaming(url, data, headers, timeout, requested_model)
            return
        try:
            req_out = Request(url, data=data, headers=headers, method="POST")
            with urlopen(req_out, timeout=timeout) as upstream_response:
                raw = upstream_response.read()
                chat_response = json.loads(raw)
                self._send_json(
                    upstream_response.status,
                    chat_to_response(chat_response, requested_model),
                )
        except URLError as e:
            code = getattr(e, "code", 502)
            raw = getattr(e, "read", lambda: b"")()
            retry_after = e.headers.get("Retry-After") if getattr(e, "headers", None) else None
            try:
                error = json.loads(raw) if raw else {"error": {"message": str(e), "type": "upstream_error"}}
            except json.JSONDecodeError:
                error = {"error": {"message": raw.decode(errors="replace"), "type": "upstream_error"}}
            self._send_json(code, error, retry_after=retry_after)
        except Exception as e:
            self._send_json(502, {"error": {"message": str(e), "type": "proxy_error"}})

    def _forward_responses_streaming(
        self, url: str, data: bytes, headers: dict, timeout: int, requested_model: str
    ):
        parts = urlsplit(url)
        conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(parts.hostname, parts.port, timeout=timeout)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        started = False
        try:
            conn.connect()
            conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.request("POST", path, body=data, headers=headers)
            upstream_response = conn.getresponse()
            if upstream_response.status >= 400:
                error_body = upstream_response.read()
                retry_after = upstream_response.getheader("Retry-After")
                try:
                    error = json.loads(error_body)
                except json.JSONDecodeError:
                    error = {"error": error_body.decode(errors="replace")}
                self._send_json(upstream_response.status, error, retry_after=retry_after)
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
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
            for chat_chunk in parse_chat_sse(iter(upstream_response.readline, b"")):
                for event in adapter.feed(chat_chunk):
                    write_event(event)
            for event in adapter.finish():
                write_event(event)
            done = b"data: [DONE]\n\n"
            self.wfile.write(f"{len(done):x}\r\n".encode("ascii"))
            self.wfile.write(done)
            self.wfile.write(b"\r\n0\r\n\r\n")
            self.wfile.flush()
        except Exception as e:
            if not started:
                self._send_json(502, {"error": {"message": str(e), "type": "proxy_error"}})
            else:
                print(f"  ⚠ Responses stream ended early: {e}", file=sys.stderr)
        finally:
            conn.close()

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
        p, upstream_model = self._resolve_model(requested_model)
        if not p:
            self._send_json(404, anthropic_error(
                404, f"Model '{requested_model}' not in free whitelist."
            ))
            return
        if not (p.upstream_url or p.base_url):
            self._send_json(502, anthropic_error(502, "provider not configured"))
            return
        try:
            chat_request = messages_to_chat(request)
        except AnthropicConversionError as e:
            self._send_json(400, anthropic_error(400, str(e)))
            return
        chat_request["model"] = upstream_model
        streaming = bool(request.get("stream"))
        chat_request["stream"] = streaming
        if streaming:
            chat_request["stream_options"] = {"include_usage": True}

        upstream = (p.upstream_url or p.base_url).rstrip("/")
        url = f"{upstream}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {p.effective_key}",
            "User-Agent": "open-free-router/0.1",
        }
        data = json.dumps(chat_request).encode()
        timeout = getattr(self, "_upstream_timeout", 120)
        if streaming:
            self._forward_messages_streaming(url, data, headers, timeout, requested_model)
            return
        try:
            req_out = Request(url, data=data, headers=headers, method="POST")
            with urlopen(req_out, timeout=timeout) as upstream_response:
                raw = upstream_response.read()
                chat_response = json.loads(raw)
                self._send_json(
                    upstream_response.status,
                    chat_to_messages(chat_response, requested_model),
                )
        except URLError as e:
            code = getattr(e, "code", 502)
            raw = getattr(e, "read", lambda: b"")()
            retry_after = e.headers.get("Retry-After") if getattr(e, "headers", None) else None
            message = raw.decode(errors="replace") if raw else str(e)
            self._send_json(code, anthropic_error(code, message), retry_after=retry_after)
        except Exception as e:
            self._send_json(502, anthropic_error(502, str(e)))

    def _forward_messages_streaming(
        self, url: str, data: bytes, headers: dict, timeout: int, requested_model: str
    ):
        """Relay upstream Chat SSE as Anthropic Messages SSE events.

        Unlike the OpenAI-style stream, an Anthropic stream ends after
        ``message_stop`` — there is no ``data: [DONE]`` sentinel.
        """
        parts = urlsplit(url)
        conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(parts.hostname, parts.port, timeout=timeout)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        started = False
        try:
            conn.connect()
            conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.request("POST", path, body=data, headers=headers)
            upstream_response = conn.getresponse()
            if upstream_response.status >= 400:
                error_body = upstream_response.read()
                retry_after = upstream_response.getheader("Retry-After")
                self._send_json(
                    upstream_response.status,
                    anthropic_error(
                        upstream_response.status,
                        error_body.decode(errors="replace"),
                    ),
                    retry_after=retry_after,
                )
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
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
            for chat_chunk in parse_chat_sse(iter(upstream_response.readline, b"")):
                for event in adapter.feed(chat_chunk):
                    write_event(event)
            for event in adapter.finish():
                write_event(event)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception as e:
            if not started:
                self._send_json(502, anthropic_error(502, str(e)))
            else:
                print(f"  ⚠ Messages stream ended early: {e}", file=sys.stderr)
        finally:
            conn.close()

    def _forward_buffered(self, url: str, data: bytes, headers: dict, timeout: int):
        try:
            req_out = Request(url, data=data, headers=headers, method="POST")
            with urlopen(req_out, timeout=timeout) as r:
                resp = r.read()
                # Normalise reasoning-model responses where content is null.
                # Many reasoning models (e.g. step-3.7-flash, deepseek-r1)
                # return {"content": null, "reasoning_content": "..."} — the
                # actual output is in reasoning_content. Agents that only
                # read message.content get null and crash. Fix: copy
                # reasoning_content into content when content is null.
                ct = r.headers.get("Content-Type", "")
                if "json" in ct.lower():
                    resp = _normalise_reasoning_content(resp)
                self.send_response(r.status)
                for k, v in r.headers.items():
                    if k.lower() in ("content-type", "retry-after"):
                        self.send_header(k, v)
                # Always send our own Content-Length (may differ after normalisation)
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
        except URLError as e:
            code = getattr(e, "code", 502)
            raw = getattr(e, "read", lambda: b"")()
            retry_after = e.headers.get("Retry-After") if getattr(e, "headers", None) else None
            if raw:
                try:
                    self._send_json(code, json.loads(raw), retry_after=retry_after)
                except json.JSONDecodeError:
                    self._send_json(code, {"error": raw.decode("utf-8", errors="replace")}, retry_after=retry_after)
            else:
                self._send_json(code, {"error": str(e.reason)}, retry_after=retry_after)
        except Exception as e:
            self._send_json(502, {"error": str(e)})

    def _forward_streaming(self, url: str, data: bytes, headers: dict, timeout: int):
        """Forward a `stream: true` chat completion, relaying upstream SSE
        chunks to the client as they arrive instead of buffering the whole
        response (which is what plain urlopen + one wfile.write() would do).

        If the upstream call fails or returns an error status *before* any
        body has been sent to the client, we still reply with a normal
        buffered JSON error, matching the non-streaming path. Once we've
        started relaying chunks, headers are already flushed, so a later
        upstream failure just ends the stream (mirrors a dropped connection
        mid-stream rather than a JSON error body).
        """
        parts = urlsplit(url)
        conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(parts.hostname, parts.port, timeout=timeout)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        started = False
        try:
            conn.connect()
            conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.request("POST", path, body=data, headers=headers)
            resp = conn.getresponse()

            if resp.status >= 400:
                body = resp.read()
                retry_after = resp.getheader("Retry-After")
                try:
                    self._send_json(resp.status, json.loads(body), retry_after=retry_after)
                except json.JSONDecodeError:
                    self._send_json(resp.status, {"error": body.decode("utf-8", errors="replace")}, retry_after=retry_after)
                return

            self.send_response(resp.status)
            self.send_header("Content-Type", resp.getheader("Content-Type", "text/event-stream"))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            started = True

            # NOTE: deliberately resp.readline(), not resp.read(N). SSE is
            # line-oriented, and http.client's chunked-aware read(N) keeps
            # pulling *subsequent* upstream HTTP chunks from the socket
            # (blocking on each) until it has N bytes buffered — so
            # read(4096) on a stream of many small SSE events silently
            # blocks until the entire response has arrived, defeating
            # streaming. readline() returns as soon as one line is
            # available, which is exactly the granularity SSE needs and
            # keeps latency-to-first-byte low without going byte-at-a-time.
            while True:
                line = resp.readline()
                if not line:
                    break
                self.wfile.write(f"{len(line):x}\r\n".encode("ascii"))
                self.wfile.write(line)
                self.wfile.write(b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        except Exception as e:
            if not started:
                self._send_json(502, {"error": str(e)})
            # else: client already received a 200 + partial stream; stop
            # writing and let the connection close, like an upstream drop.
        finally:
            conn.close()

    def log_message(self, format, *args):
        pass


def run_proxy(
    registry: Registry,
    host: str = "127.0.0.1",
    port: int = 8337,
    upstream_timeout: int = 120,
    auth_token: str = "",
):
    handler = type("Handler", (_ProxyHandler,), {
        "registry": registry,
        "_upstream_timeout": upstream_timeout,
        "auth_token": auth_token,
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
