"""Upstream request translation layer (PRD-060/T2).

All upstream outbound requests are built through translation tables:
canonical request -> per-provider fields. Design: docs/plans/protocol-unify.md.

T2 implements the *request* side only (table loader + outbound request
builder). Response/streaming/error sections follow the §3.1 template shape
and will be consumed by T3/T4.
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
class TranslationTable:
    """Mirrors the §3.1 YAML template shape. T2 consumes the request section;
    response/streaming/error are data placeholders for T3/T4."""
    provider_id: str
    auth: AuthTable = field(default_factory=AuthTable)
    request: RequestTable = field(default_factory=RequestTable)
    response: dict = field(default_factory=dict)
    streaming: dict = field(default_factory=dict)
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
