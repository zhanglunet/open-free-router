"""Upstream translation layer (PRD-060).

All upstream traffic goes through translation tables, both directions:
canonical request -> per-provider fields (T2), and per-provider responses ->
canonical internal representation (T3). Design: docs/plans/protocol-unify.md.

T2 implemented the *request* side (table loader + outbound request builder).
T3 implements the *response* side: translation-table response/streaming
sections, a canonical SSE event model (§2.3), and table-driven parsers.
Error tables stay placeholders for T4.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

USER_AGENT = "open-free-router/0.1"

# Default field whitelists, moved verbatim from proxy.py::_forward_request.
# Per protocol-unify.md §7 they are the *default translation-table behaviour*:
# standard OpenAI fields pass; anything else is dropped per provider table,
# with a warning (never silently, §5).
DEFAULT_ALLOWED_TOP_KEYS = frozenset({
    "model", "messages", "max_tokens", "max_completion_tokens",
    "temperature", "top_p", "n", "stream", "stream_options",
    "stop", "frequency_penalty", "presence_penalty",
    "logit_bias", "logprobs", "top_logprobs", "response_format",
    "seed", "tools", "tool_choice", "parallel_tool_calls",
    "user", "functions", "function_call",
})
DEFAULT_ALLOWED_MSG_KEYS = frozenset({
    "role", "content", "name", "tool_calls", "tool_call_id",
    "function_call", "refusal",
})


@dataclass(frozen=True)
class AuthTable:
    mode: str = "bearer"  # bearer | none


@dataclass(frozen=True)
class RequestTable:
    model_field: str = "model"
    max_tokens_field: str = "max_tokens"
    role_map: dict = field(default_factory=lambda: {"developer": "system"})
    drop_fields: tuple = ()       # explicitly-declared top-level drops
    passthrough: tuple = ()       # declared dotted paths, e.g. "metadata.user_id"
    allowed_top_keys: frozenset = DEFAULT_ALLOWED_TOP_KEYS
    allowed_msg_keys: frozenset = DEFAULT_ALLOWED_MSG_KEYS


@dataclass(frozen=True)
class ResponseTable:
    """Mirrors the §3.1 template ``response`` section (consumed by T3)."""
    content_field: str = "message.content"
    reasoning_field: str = "message.reasoning_content"
    null_content_policy: str = "copy_reasoning"  # copy_reasoning | empty_string
    usage_fields: dict = field(default_factory=lambda: {
        "prompt_tokens": "usage.prompt_tokens",
        "completion_tokens": "usage.completion_tokens",
        "total_tokens": "usage.total_tokens",
    })


@dataclass(frozen=True)
class StreamingTable:
    """Mirrors the §3.1 template ``streaming`` section (consumed by T3)."""
    event_format: str = "openai_sse"   # openai_sse | anthropic_sse | custom
    done_marker: str = "[DONE]"
    usage_in_stream: bool = False
    # Named upstream-native events explicitly allowed to be dropped
    # (protocol-unify.md §5 rule 4). Anything else unknown is warn+drop.
    drop_with_log: tuple = ()


@dataclass(frozen=True)
class TranslationTable:
    """Mirrors the §3.1 YAML template shape. T2 consumes the request section,
    T3 the response/streaming sections; error stays a data placeholder for T4."""
    provider_id: str
    auth: AuthTable = field(default_factory=AuthTable)
    request: RequestTable = field(default_factory=RequestTable)
    response: ResponseTable = field(default_factory=ResponseTable)
    streaming: StreamingTable = field(default_factory=StreamingTable)
    error: dict = field(default_factory=dict)


def _default_table(provider_id: str) -> TranslationTable:
    return TranslationTable(provider_id=provider_id)


# Built-in providers. All 10 walk OpenAI-compatible Chat Completions today
# (protocol-unify.md §0); per-provider deltas live here. Unknown provider ids
# (user-added via `open-free-router add`, the "11th upstream") fall back to
# the default table.
TABLES: dict[str, TranslationTable] = {
    # The only keyless upstream: never attach an Authorization header.
    "opencode-zen-free": TranslationTable(
        provider_id="opencode-zen-free",
        auth=AuthTable(mode="none"),
    ),
}


def get_table(provider_id: str) -> TranslationTable:
    """Return the translation table for a provider id (registry key).

    Unknown ids get the default table, so newly added upstreams keep working
    with standard behaviour until a dedicated table is written (T5).
    """
    return TABLES.get(provider_id, _default_table(provider_id))


_MISSING = object()


def _get_path(obj: dict, dotted: str):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _set_path(obj: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = obj
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def canonicalize_request(payload: dict) -> dict:
    """Provider-agnostic canonical normalization (previously inline in
    proxy.py::_forward_request, now table-driven default behaviour):

    - ``max_completion_tokens`` -> ``max_tokens``
    - ``developer`` role -> ``system``

    Returns a new dict; the input is never mutated.
    """
    req = copy.deepcopy(payload)
    if "max_completion_tokens" in req:
        if "max_tokens" not in req:
            req["max_tokens"] = req.pop("max_completion_tokens")
        else:
            del req["max_completion_tokens"]
    for msg in req.get("messages", []) or []:
        if isinstance(msg, dict) and msg.get("role") == "developer":
            msg["role"] = "system"
    return req


def normalize_request(
    table: TranslationTable,
    payload: dict,
    *,
    provider_name: str,
    request_id: str,
) -> tuple[dict, list[str]]:
    """Apply a provider's request translation table to a canonical payload.

    Returns ``(body, dropped_fields)``. Fields that are neither whitelisted
    nor declared in ``passthrough`` are dropped with a *warning* log --
    never silently (protocol-unify.md §5). Only field *names* are logged,
    never values.
    """
    req = copy.deepcopy(payload)
    rt = table.request
    dropped: list[str] = []

    # 1. Token-limit alias -> provider's max-tokens field.
    if "max_completion_tokens" in req and rt.max_tokens_field == "max_tokens":
        if "max_tokens" not in req:
            req["max_tokens"] = req.pop("max_completion_tokens")
        else:
            del req["max_completion_tokens"]
    elif rt.max_tokens_field != "max_tokens" and "max_tokens" in req:
        req[rt.max_tokens_field] = req.pop("max_tokens")

    # 2. Messages: role map, then per-message key whitelist.
    for msg in req.get("messages", []) or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role in rt.role_map:
            msg["role"] = rt.role_map[role]
        for key in list(msg.keys()):
            if key not in rt.allowed_msg_keys:
                dropped.append(f"messages[].{key}")
                del msg[key]

    # 3. Passthrough: re-plant declared dotted paths from the ORIGINAL
    # payload (they must survive the whitelist below).
    passthrough_tops: set[str] = set()
    for path in rt.passthrough:
        passthrough_tops.add(path.split(".")[0])
        value = _get_path(payload, path)
        if value is not _MISSING:
            # Prune the top-level container first so undeclared siblings
            # cannot ride along; then plant only the declared path.
            top = path.split(".")[0]
            if top in req and isinstance(req[top], dict) and "." in path:
                req[top] = {}
            _set_path(req, path, copy.deepcopy(value))

    # 4. Top-level whitelist (+ explicitly declared per-provider drops).
    allowed = set(rt.allowed_top_keys) | passthrough_tops
    for key in list(req.keys()):
        if key not in allowed:
            dropped.append(key)
            del req[key]
    for key in rt.drop_fields:
        if key in req:
            dropped.append(key)
            del req[key]

    if dropped:
        seen = list(dict.fromkeys(dropped))
        log.warning(
            "translate: provider=%s request_id=%s dropped undeclared "
            "request fields: %s",
            provider_name,
            request_id,
            ",".join(seen),
        )
    return req, dropped


@dataclass(frozen=True)
class OutboundRequest:
    url: str
    headers: dict
    body: dict
    dropped_fields: tuple


def build_outbound_request(
    table: TranslationTable,
    *,
    target,
    payload: dict,
    credential: str,
    endpoint_suffix: str,
    request_id: str,
) -> OutboundRequest:
    """Build the complete upstream HTTP request for one route target.

    ``target`` is a routing.RouteTarget (duck-typed: needs ``provider`` with
    ``upstream_url``/``base_url``, ``provider_name`` and ``upstream_model_id``).
    Raises OSError for unconfigured/invalid upstream URLs, mirroring the
    previous executor behaviour.
    """
    provider = target.provider
    upstream = (provider.upstream_url or provider.base_url or "").rstrip("/")
    if not upstream:
        raise OSError("provider not configured")
    url = f"{upstream}/{endpoint_suffix.lstrip('/')}"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise OSError("invalid provider upstream URL")

    body, dropped = normalize_request(
        table, payload, provider_name=target.provider_name, request_id=request_id
    )
    body[table.request.model_field] = target.upstream_model_id

    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    # auth.mode == "none" (opencode-zen-free): never attach Authorization,
    # even if a credential string happens to be present.
    if table.auth.mode == "bearer" and credential:
        headers["Authorization"] = f"Bearer {credential}"

    return OutboundRequest(
        url=url,
        headers=headers,
        body=body,
        dropped_fields=tuple(dropped),
    )


# ---------------------------------------------------------------------------
# T3: response normalization. Upstream responses are parsed into the canonical
# internal representation (protocol-unify.md §2.2/§2.3) via the translation
# table's response/streaming sections, then rendered back to the client
# protocol surface. Client behaviour is unchanged: normalization is internal
# plumbing, not a protocol change (error semantics stay untouched for T4).
# ---------------------------------------------------------------------------


def parse_response(
    table: TranslationTable,
    obj: dict,
    *,
    provider_name: str,
    request_id: str,
) -> tuple[dict, bool]:
    """Apply the table's response section to a parsed upstream JSON response.

    Returns ``(fixed, changed)``. ``fixed`` is a deep copy with every choice's
    message content fixed per ``null_content_policy``; the input is never
    mutated. Callers should re-serialize only when ``changed`` is True so
    untouched responses keep their original bytes.

    This replaces the hardcoded ``proxy._normalise_reasoning_content`` with
    table-driven behaviour: ``copy_reasoning`` copies ``reasoning_content``
    into a null ``content`` (falling back to ``""``), ``empty_string`` writes
    ``""`` directly.
    """
    rt = table.response
    fixed = copy.deepcopy(obj)
    changed = False
    choices = fixed.get("choices")
    if not isinstance(choices, list):
        return fixed, False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        content = _get_path(choice, rt.content_field)
        if content is not None:
            continue
        if rt.null_content_policy == "copy_reasoning":
            rc = _get_path(choice, rt.reasoning_field)
            new_content = rc if rc is not None and rc is not _MISSING else ""
        else:
            new_content = ""
        _set_path(choice, rt.content_field, new_content)
        changed = True
    if changed:
        log.debug(
            "translate: provider=%s request_id=%s normalized null content "
            "(policy=%s)",
            provider_name,
            request_id,
            rt.null_content_policy,
        )
    return fixed, changed


def extract_usage(table: TranslationTable, obj: dict) -> dict | None:
    """Pull the canonical usage object out of a parsed upstream response."""
    usage = {}
    for canonical_key, dotted in table.response.usage_fields.items():
        value = _get_path(obj, dotted)
        if value is not _MISSING:
            usage[canonical_key] = value
    return usage or None


# --- Canonical SSE events (protocol-unify.md §2.3) --------------------------
# Upstream-native SSE is parsed into exactly these events. Downstream client
# adapters (Responses/Messages) and the OpenAI passthrough only ever see this
# vocabulary; per-provider event-name/field dialects are flattened here.


@dataclass(frozen=True)
class TextDelta:
    delta: str


@dataclass(frozen=True)
class ReasoningDelta:
    delta: str


@dataclass(frozen=True)
class ToolCallDelta:
    index: int
    id: str | None
    name: str | None
    arguments_delta: str
    # Original tool-call piece, re-embedded on render so provider extensions
    # (e.g. extra_content.google.thought_signature) survive the round trip.
    raw: dict | None = None


@dataclass(frozen=True)
class UsageEvent:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class DoneEvent:
    finish_reason: str | None = None


@dataclass(frozen=True)
class StreamErrorEvent:
    message: str
    raw: dict | None = None


StreamEvent = TextDelta | ReasoningDelta | ToolCallDelta | UsageEvent | DoneEvent | StreamErrorEvent


def _warned_once(seen: set, key: str, provider_name: str, request_id: str, text: str) -> None:
    if key in seen:
        return
    seen.add(key)
    log.warning(
        "translate: provider=%s request_id=%s %s",
        provider_name,
        request_id,
        text,
    )


def parse_sse_events(
    table: TranslationTable,
    lines,
    *,
    provider_name: str,
    request_id: str,
):
    """Yield canonical stream events (§2.3) from raw upstream SSE byte lines.

    A generator: lines are consumed lazily so long streams never buffer.
    Behaviour for the ``openai_sse`` event format (all 10 built-in upstreams
    today):

    - ``data: {...}`` JSON chunks -> TextDelta / ReasoningDelta /
      ToolCallDelta / UsageEvent
    - ``data: [DONE]`` -> DoneEvent (carries the last seen finish_reason)
    - SSE comments (``: ...``) -> skipped silently (protocol keepalives)
    - Named ``event: <name>`` lines -> flattened when the table knows them,
      otherwise warn+drop per protocol-unify.md §5 rule 4 (never silently)
    - Unparseable ``data:`` payloads -> one warning per stream, then skipped

    Error envelopes inside the stream become StreamErrorEvent; error-code
    semantics stay untouched (T4).
    """
    import json as _json

    st = table.streaming
    warned: set[str] = set()
    pending_event: str | None = None
    data_buf: list[str] = []
    last_finish_reason: str | None = None

    def flush_data():
        nonlocal pending_event, last_finish_reason
        if not data_buf:
            pending_event = None
            return
        raw_text = "\n".join(data_buf)
        data_buf.clear()
        name = pending_event
        pending_event = None
        data = raw_text.strip()
        if not data:
            return
        if data == st.done_marker:
            yield DoneEvent(finish_reason=last_finish_reason)
            return
        try:
            obj = _json.loads(data)
        except (_json.JSONDecodeError, UnicodeDecodeError):
            _warned_once(
                warned, "bad-json", provider_name, request_id,
                "dropped unparseable SSE data payload",
            )
            return
        if name is not None and name not in ("", "message"):
            # Named native event: only the table decides its fate (§5 rule 4).
            if name in st.drop_with_log:
                log.debug(
                    "translate: provider=%s request_id=%s dropped declared "
                    "stream event %r",
                    provider_name, request_id, name,
                )
                return
            _warned_once(
                warned, f"event:{name}", provider_name, request_id,
                f"dropped undeclared upstream stream event {name!r}",
            )
            return
        if not isinstance(obj, dict):
            _warned_once(
                warned, "non-dict", provider_name, request_id,
                "dropped non-object SSE data payload",
            )
            return
        if isinstance(obj.get("error"), dict):
            err = obj["error"]
            yield StreamErrorEvent(
                message=str(err.get("message") or "upstream stream error"),
                raw=obj,
            )
            return
        usage = extract_usage(table, obj)
        if usage:
            yield UsageEvent(
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
            )
        choices = obj.get("choices")
        if not isinstance(choices, list) or not choices:
            return
        choice = choices[0] if isinstance(choices[0], dict) else {}
        fr = choice.get("finish_reason")
        if isinstance(fr, str):
            last_finish_reason = fr
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            return
        content = delta.get("content")
        if content:
            yield TextDelta(delta=str(content))
        reasoning = delta.get("reasoning_content")
        if reasoning:
            yield ReasoningDelta(delta=str(reasoning))
        for piece in delta.get("tool_calls", []) or []:
            if not isinstance(piece, dict):
                continue
            fn = piece.get("function") or {}
            if not isinstance(fn, dict):
                fn = {}
            try:
                index = int(piece.get("index", 0))
            except (TypeError, ValueError):
                index = 0
            yield ToolCallDelta(
                index=index,
                id=piece.get("id"),
                name=fn.get("name"),
                arguments_delta=str(fn.get("arguments") or ""),
                raw=piece,
            )

    for raw in lines:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        line = line.rstrip("\r\n")
        if not line:
            yield from flush_data()
            continue
        if line.startswith(":"):
            continue  # SSE comment / keepalive, not an event
        if line.startswith("event:"):
            yield from flush_data()
            pending_event = line[6:].strip()
            continue
        if line.startswith("data:"):
            data_buf.append(line[5:].lstrip(" "))
            continue
        # A bare line with no field name: per the SSE spec it is ignored.
        # (Only appended when it continues a multi-line data field.)
        if data_buf:
            data_buf.append(line)
    yield from flush_data()


def render_chat_chunk(event: StreamEvent) -> dict:
    """Render a canonical stream event back to an OpenAI chat-completion chunk.

    Downstream client adapters (Responses/Messages) keep consuming these chunk
    shapes unchanged; the canonical layer sits *between* the upstream wire and
    the adapters, flattening per-provider dialects into one vocabulary.
    """
    if isinstance(event, TextDelta):
        return {"choices": [{"index": 0, "delta": {"content": event.delta}}]}
    if isinstance(event, ReasoningDelta):
        return {"choices": [{"index": 0, "delta": {"reasoning_content": event.delta}}]}
    if isinstance(event, ToolCallDelta):
        piece = dict(event.raw) if isinstance(event.raw, dict) else {}
        piece["index"] = event.index
        fn = piece.get("function")
        if not isinstance(fn, dict):
            fn = {}
            piece["function"] = fn
        if event.id is not None:
            piece["id"] = event.id
        if event.name is not None:
            fn["name"] = event.name
        if event.arguments_delta:
            fn["arguments"] = event.arguments_delta
        return {"choices": [{"index": 0, "delta": {"tool_calls": [piece]}}]}
    if isinstance(event, UsageEvent):
        usage = {}
        if event.prompt_tokens is not None:
            usage["prompt_tokens"] = event.prompt_tokens
        if event.completion_tokens is not None:
            usage["completion_tokens"] = event.completion_tokens
        if event.total_tokens is not None:
            usage["total_tokens"] = event.total_tokens
        return {"usage": usage}
    if isinstance(event, DoneEvent):
        return {"choices": [{"index": 0, "delta": {}, "finish_reason": event.finish_reason}]}
    if isinstance(event, StreamErrorEvent):
        return dict(event.raw) if isinstance(event.raw, dict) else {"error": {"message": event.message}}
    raise TypeError(f"unknown stream event: {type(event).__name__}")
