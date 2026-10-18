"""Response normalization tests (PRD-060/T3).

Covers the translation layer's *response* side: table-driven JSON response
normalization, canonical SSE event parsing (JSON + SSE, >=3 representative
upstreams), event-name flattening, and wire-equivalence with the pre-T3
implementation (diff tests).
"""
from __future__ import annotations

import copy
import json
import logging
import re

import pytest

from open_free_router import translate
from open_free_router.translate import (
    DoneEvent,
    ReasoningDelta,
    ResponseTable,
    StreamingTable,
    StreamErrorEvent,
    TextDelta,
    ToolCallDelta,
    TranslationTable,
    UsageEvent,
    extract_usage,
    get_table,
    parse_response,
    parse_sse_events,
    render_chat_chunk,
)
from open_free_router.anthropic import AnthropicStreamAdapter
from open_free_router.responses import ResponsesStreamAdapter


def _sse(*lines: str) -> list[bytes]:
    return [(line + "\n").encode() for line in lines]


# ---------------------------------------------------------------------------
# Reference: the pre-T3 hardcoded implementation (proxy._normalise_reasoning_content).
# Kept here as the oracle for the wire-equivalence diff test.
# ---------------------------------------------------------------------------

def _reference_normalise_reasoning_content(body: bytes) -> bytes:
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
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


# ---------------------------------------------------------------------------
# Fixtures: representative upstream response shapes.
# ---------------------------------------------------------------------------

DEEPSEEK_REASONING = {
    "id": "chatcmpl-ds-1",
    "model": "deepseek-reasoner",
    "choices": [{
        "index": 0,
        "message": {
            "role": "assistant",
            "content": None,
            "reasoning_content": "Let me think step by step.",
        },
        "finish_reason": "stop",
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
}

NOUS_STANDARD = {
    "id": "chatcmpl-nous-1",
    "model": "nous-hermes",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "Hello!"},
        "finish_reason": "stop",
    }],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}

OPENROUTER_TOOLS = {
    "id": "gen-or-1",
    "model": "openai/gpt-oss-20b:free",
    "choices": [{
        "index": 0,
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_abc",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":"SF"}'},
            }],
        },
        "finish_reason": "tool_calls",
    }],
    "usage": {"prompt_tokens": 42, "completion_tokens": 17, "total_tokens": 59},
}

NULL_NO_REASONING = {
    "id": "x",
    "model": "m",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": None},
                 "finish_reason": "stop"}],
}


def _new_path(table, fixture: dict) -> bytes:
    """The T3 pipeline for a buffered response, as wired in proxy.py."""
    raw = json.loads(json.dumps(fixture).encode())
    fixed, changed = parse_response(
        table, raw, provider_name="p", request_id="r")
    return json.dumps(fixed).encode() if changed else json.dumps(fixture).encode()


# ---------------------------------------------------------------------------
# parse_response
# ---------------------------------------------------------------------------

def test_parse_response_copies_reasoning_deepseek():
    before = copy.deepcopy(DEEPSEEK_REASONING)
    fixed, changed = parse_response(
        get_table("deepseek"), copy.deepcopy(DEEPSEEK_REASONING),
        provider_name="deepseek", request_id="r1")
    assert changed is True
    msg = fixed["choices"][0]["message"]
    assert msg["content"] == "Let me think step by step."
    # reasoning_content is preserved, not consumed
    assert msg["reasoning_content"] == "Let me think step by step."
    # input never mutated
    assert DEEPSEEK_REASONING == before


def test_parse_response_standard_untouched_nous():
    fixed, changed = parse_response(
        get_table("nous"), copy.deepcopy(NOUS_STANDARD),
        provider_name="nous", request_id="r2")
    assert changed is False
    assert fixed == NOUS_STANDARD


def test_parse_response_empty_string_policy():
    table = TranslationTable(
        provider_id="x",
        response=ResponseTable(null_content_policy="empty_string"),
    )
    fixed, changed = parse_response(
        table, copy.deepcopy(NULL_NO_REASONING),
        provider_name="x", request_id="r3")
    assert changed is True
    assert fixed["choices"][0]["message"]["content"] == ""


def test_parse_response_no_choices_embeddings_shape():
    emb = {"data": [{"embedding": [0.1], "index": 0}], "model": "e"}
    fixed, changed = parse_response(
        get_table("nous"), copy.deepcopy(emb),
        provider_name="nous", request_id="r4")
    assert changed is False
    assert fixed == emb


def test_extract_usage_openrouter():
    usage = extract_usage(get_table("openrouter"), OPENROUTER_TOOLS)
    assert usage == {"prompt_tokens": 42, "completion_tokens": 17,
                     "total_tokens": 59}


def test_wire_equivalence_with_pre_t3_implementation():
    """Diff test: new table-driven path == old hardcoded function, byte for byte."""
    for fixture in (DEEPSEEK_REASONING, NOUS_STANDARD, OPENROUTER_TOOLS,
                    NULL_NO_REASONING):
        body = json.dumps(fixture).encode()
        assert _new_path(get_table("deepseek"), fixture) == \
            _reference_normalise_reasoning_content(body)


# ---------------------------------------------------------------------------
# parse_sse_events
# ---------------------------------------------------------------------------

MIXED_STREAM = _sse(
    ": ping",  # SSE comment -> skipped silently
    "",
    'data: {"id":"1","choices":[{"index":0,"delta":{"role":"assistant"}}]}',
    "",
    'data: {"choices":[{"index":0,"delta":{"content":"Hello"}}]}',
    "",
    'data: {"choices":[{"index":0,"delta":{"reasoning_content":"thinking"}}]}',
    "",
    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"f","arguments":"{\\"a\\": 1}"},"extra_content":{"google":{"thought_signature":"sig"}}}]}}]}',
    "",
    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":9,"total_tokens":12}}',
    "",
    "data: [DONE]",
    "",
)


def test_parse_sse_mixed_stream():
    events = list(parse_sse_events(
        get_table("nous"), MIXED_STREAM, provider_name="nous", request_id="s1"))
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["TextDelta", "ReasoningDelta", "ToolCallDelta",
                     "UsageEvent", "DoneEvent"]
    assert events[0].delta == "Hello"
    assert events[1].delta == "thinking"
    tc = events[2]
    assert isinstance(tc, ToolCallDelta)
    assert (tc.index, tc.id, tc.name, tc.arguments_delta) == \
        (0, "call_1", "f", '{"a": 1}')
    # provider extension survives on the raw piece for adapter consumption
    assert tc.raw["extra_content"]["google"]["thought_signature"] == "sig"
    assert events[3] == UsageEvent(prompt_tokens=3, completion_tokens=9,
                                   total_tokens=12)
    assert events[4] == DoneEvent(finish_reason="stop")


def test_parse_sse_lazy_generator():
    gen = parse_sse_events(get_table("nous"), MIXED_STREAM,
                           provider_name="nous", request_id="s2")
    assert next(gen) == TextDelta(delta="Hello")


def test_parse_sse_malformed_payload_warned_once(caplog):
    lines = _sse('data: {"ok": true}', "", "data: not-json{", "", "data: nope[", "")
    with caplog.at_level(logging.WARNING, logger="open_free_router.translate"):
        events = list(parse_sse_events(
            get_table("groq"), lines, provider_name="groq", request_id="s3"))
    assert events == []  # valid-but-foreign JSON yields nothing; bad JSON skipped
    warnings = [r for r in caplog.records if "unparseable" in r.getMessage()]
    assert len(warnings) == 1  # one warning per stream, not per line


def test_parse_sse_named_event_flattening(caplog):
    # Declared native events are dropped quietly; undeclared ones warn+drop (§5 rule 4).
    table = TranslationTable(
        provider_id="mock",
        streaming=StreamingTable(drop_with_log=("ping",)),
    )
    lines = _sse(
        "event: ping", 'data: {"t":1}', "",
        "event: mystery", 'data: {"t":2}', "",
        'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}', "",
    )
    with caplog.at_level(logging.WARNING, logger="open_free_router.translate"):
        events = list(parse_sse_events(
            table, lines, provider_name="mock", request_id="s4"))
    assert events == [TextDelta(delta="hi")]
    warnings = [r for r in caplog.records if "mystery" in r.getMessage()]
    assert len(warnings) == 1
    assert not [r for r in caplog.records if "ping" in r.getMessage()]


def test_parse_sse_stream_error_envelope():
    lines = _sse('data: {"error":{"message":"boom","type":"server_error"}}', "")
    events = list(parse_sse_events(
        get_table("nous"), lines, provider_name="nous", request_id="s5"))
    assert len(events) == 1
    assert isinstance(events[0], StreamErrorEvent)
    assert events[0].message == "boom"


# ---------------------------------------------------------------------------
# render_chat_chunk + adapter wire-equivalence (diff test)
# ---------------------------------------------------------------------------

def _raw_chunks(lines):
    for raw in lines:
        text = raw.decode().strip()
        if not text or text.startswith(":"):
            continue
        assert text.startswith("data:")
        data = text[5:].strip()
        if data and data != "[DONE]":
            yield json.loads(data)


def test_adapter_equivalence_responses():
    table = get_table("nous")
    raw = list(_raw_chunks(MIXED_STREAM))
    canonical = list(parse_sse_events(
        table, MIXED_STREAM, provider_name="nous", request_id="s6"))
    rendered = [render_chat_chunk(e) for e in canonical]

    a1, a2 = ResponsesStreamAdapter("m"), ResponsesStreamAdapter("m")
    out_raw, out_new = [], []
    for c in raw:
        out_raw.extend(a1.feed(c))
    for c in rendered:
        out_new.extend(a2.feed(c))
    out_raw.extend(a1.finish())
    out_new.extend(a2.finish())

    def norm(events):
        # uuid-generated ids (resp_/msg_/fc_/call_/toolu_) are
        # non-deterministic; scrub them and compare structure only.
        text = json.dumps(events, sort_keys=True, default=str)
        return re.sub(r"\b(resp|msg|fc|call|toolu)_[0-9a-f]{6,}\b",
                      r"\1_ID", text)
    assert norm(out_new) == norm(out_raw)
    assert any(e.get("type") == "response.output_text.delta" for e in out_new)
    assert a2.usage == {"prompt_tokens": 3, "completion_tokens": 9, "total_tokens": 12}


def test_adapter_equivalence_anthropic():
    table = get_table("groq")
    lines = _sse(
        'data: {"choices":[{"index":0,"delta":{"content":"Hi"}}]}', "",
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_9","function":{"name":"g","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}', "",
        'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}', "",
        "data: [DONE]", "",
    )
    raw = list(_raw_chunks(lines))
    rendered = [render_chat_chunk(e) for e in parse_sse_events(
        table, lines, provider_name="groq", request_id="s7")]

    a1, a2 = AnthropicStreamAdapter("m"), AnthropicStreamAdapter("m")
    out_raw, out_new = [], []
    for c in raw:
        out_raw.extend(a1.feed(c))
    for c in rendered:
        out_new.extend(a2.feed(c))
    out_raw.extend(a1.finish())
    out_new.extend(a2.finish())

    assert json.dumps(out_new, sort_keys=True, default=str) == \
        json.dumps(out_raw, sort_keys=True, default=str)
    assert any(e.get("type") == "content_block_delta" for e in out_new)


def test_unknown_provider_falls_back_to_default_tables():
    table = get_table("some-brand-new-upstream")
    fixed, changed = parse_response(
        table, copy.deepcopy(NOUS_STANDARD),
        provider_name="some-brand-new-upstream", request_id="s8")
    assert changed is False
    events = list(parse_sse_events(
        table, _sse('data: {"choices":[{"index":0,"delta":{"content":"x"}}]}', ""),
        provider_name="some-brand-new-upstream", request_id="s9"))
    assert events == [TextDelta(delta="x")]
