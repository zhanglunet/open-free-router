"""Anthropic Messages API compatibility for Chat Completions upstreams.

Claude Code (and other Anthropic-native clients) speak the Messages API:
``POST /v1/messages`` with content blocks, ``tool_use``/``tool_result``
turns, and a typed SSE event stream. The router's upstreams speak OpenAI
Chat Completions, so this module performs the state-free translation that
a local coding session needs: system prompts, text turns, tool definitions,
tool calls, tool results, and streaming deltas.

Anthropic's SSE framing differs from OpenAI's: events are typed
(``message_start`` … ``message_stop``), content lives in indexed blocks
that open and close sequentially, and the stream ends after
``message_stop`` with no ``[DONE]`` sentinel.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field


class AnthropicConversionError(ValueError):
    """The request contains an input this compatibility layer cannot use."""


def _system_text(system) -> str:
    """Anthropic ``system`` is a string or a list of text blocks."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(p for p in parts if p)
    return str(system)


def _tool_result_text(content) -> str:
    """``tool_result.content`` is a string or a list of text/image blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, dict) and block.get("type") == "image":
                raise AnthropicConversionError(
                    "tool_result image content is not supported by this text-only upstream"
                )
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def messages_to_chat(request: dict) -> dict:
    """Convert a Messages API create request to a Chat Completions request."""
    messages: list[dict] = []
    system = request.get("system")
    if system:
        text = _system_text(system)
        if text:
            messages.append({"role": "system", "content": text})

    for msg in request.get("messages", []):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            messages.append({"role": role, "content": str(content)})
            continue

        text_parts: list[str] = []
        tool_calls: list[dict] = []
        tool_results: list[dict] = []
        for block in content:
            if isinstance(block, str):
                text_parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            kind = block.get("type", "")
            if kind == "text":
                text_parts.append(str(block.get("text", "")))
            elif kind == "tool_use":
                tool_calls.append({
                    "id": block.get("id") or f"call_{uuid.uuid4().hex}",
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(
                            block.get("input", {}) or {}, ensure_ascii=False
                        ),
                    },
                })
            elif kind == "tool_result":
                tool_results.append(block)
            elif kind in {"thinking", "redacted_thinking"}:
                continue  # thinking blocks are provider-internal; drop on replay
            elif kind in {"image", "document"}:
                raise AnthropicConversionError(
                    f"Messages input type '{kind}' is not supported by this text-only upstream"
                )
            # Unknown block types are dropped rather than rejected so newer
            # clients degrade to text instead of hard-failing the session.

        if role == "assistant":
            out: dict = {"role": "assistant"}
            out["content"] = "\n".join(p for p in text_parts if p) or None
            if tool_calls:
                out["tool_calls"] = tool_calls
            if out["content"] is None and not tool_calls:
                out["content"] = ""
            messages.append(out)
        else:
            # OpenAI expects each tool result as its own `tool` message,
            # placed before any accompanying user text.
            for tr in tool_results:
                text = _tool_result_text(tr.get("content", ""))
                if tr.get("is_error"):
                    text = f"[tool error] {text}" if text else "[tool error]"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tr.get("tool_use_id", ""),
                    "content": text,
                })
            joined = "\n".join(p for p in text_parts if p)
            if joined or not tool_results:
                messages.append({"role": "user", "content": joined})

    chat: dict = {
        "model": request.get("model", ""),
        "messages": messages,
        "stream": bool(request.get("stream", False)),
    }
    if request.get("max_tokens") is not None:
        chat["max_tokens"] = request["max_tokens"]

    tools: list[dict] = []
    for tool in request.get("tools", []):
        if not isinstance(tool, dict) or not tool.get("name"):
            continue  # server-side tools (web_search etc.) have no schema here
        tools.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get(
                    "input_schema", {"type": "object", "properties": {}}
                ),
            },
        })
    if tools:
        chat["tools"] = tools
        choice = request.get("tool_choice")
        if isinstance(choice, dict):
            kind = choice.get("type")
            if kind == "any":
                chat["tool_choice"] = "required"
            elif kind == "tool":
                chat["tool_choice"] = {
                    "type": "function",
                    "function": {"name": choice.get("name", "")},
                }
            elif kind == "none":
                chat["tool_choice"] = "none"
            else:
                chat["tool_choice"] = "auto"
            if choice.get("disable_parallel_tool_use"):
                chat["parallel_tool_calls"] = False

    for key in ("temperature", "top_p"):
        if request.get(key) is not None:
            chat[key] = request[key]
    stops = request.get("stop_sequences")
    if stops:
        chat["stop"] = stops
    return chat


def _stop_reason(finish_reason: str | None, has_tool_calls: bool) -> str:
    if has_tool_calls or finish_reason == "tool_calls":
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    if finish_reason == "content_filter":
        return "refusal"
    return "end_turn"


def _anthropic_usage(chat_usage: dict | None) -> dict:
    usage = chat_usage or {}
    return {
        "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "output_tokens": int(usage.get("completion_tokens", 0) or 0),
    }


def chat_to_messages(chat_response: dict, requested_model: str) -> dict:
    """Convert a buffered Chat Completions response to a Messages object."""
    choices = chat_response.get("choices", [])
    message = choices[0].get("message", {}) if choices else {}
    finish = choices[0].get("finish_reason") if choices else None

    content_blocks: list[dict] = []
    text = message.get("content")
    if text is None:
        # Reasoning models may put the entire output in reasoning_content.
        text = message.get("reasoning_content")
    if text:
        content_blocks.append({"type": "text", "text": str(text)})

    tool_calls = message.get("tool_calls") or []
    for call in tool_calls:
        fn = call.get("function", {}) or {}
        raw_args = fn.get("arguments") or "{}"
        try:
            input_obj = json.loads(raw_args)
        except json.JSONDecodeError:
            input_obj = {"_raw_arguments": raw_args}
        if not isinstance(input_obj, dict):
            input_obj = {"_value": input_obj}
        content_blocks.append({
            "type": "tool_use",
            "id": call.get("id") or f"toolu_{uuid.uuid4().hex}",
            "name": fn.get("name", ""),
            "input": input_obj,
        })

    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content_blocks,
        "stop_reason": _stop_reason(finish, bool(tool_calls)),
        "stop_sequence": None,
        "usage": _anthropic_usage(chat_response.get("usage")),
    }


def anthropic_error(status: int, message: str) -> dict:
    """Anthropic error envelope: {"type": "error", "error": {...}}."""
    error_types = {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        413: "request_too_large",
        429: "rate_limit_error",
        500: "api_error",
        529: "overloaded_error",
    }
    return {
        "type": "error",
        "error": {
            "type": error_types.get(status, "api_error"),
            "message": message,
        },
    }


def count_tokens_estimate(request: dict) -> int:
    """Rough input-token estimate for /v1/messages/count_tokens.

    Upstream Chat Completions providers have no token-counting endpoint, so
    approximate with the ~4-chars-per-token heuristic over everything the
    request would send. Clients only use this for context-window budgeting;
    an estimate keeps them working without a paid tokenizer dependency.
    """
    total_chars = 0
    system = request.get("system")
    if system:
        total_chars += len(_system_text(system))
    for msg in request.get("messages", []):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
            continue
        if isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    total_chars += len(block)
                elif isinstance(block, dict):
                    total_chars += len(str(block.get("text", "")))
                    if block.get("type") == "tool_use":
                        total_chars += len(json.dumps(block.get("input", {}) or {}))
                    elif block.get("type") == "tool_result":
                        try:
                            total_chars += len(_tool_result_text(block.get("content", "")))
                        except AnthropicConversionError:
                            total_chars += 1000  # image blocks: flat surcharge
    for tool in request.get("tools", []):
        if isinstance(tool, dict):
            total_chars += len(json.dumps(tool, ensure_ascii=False))
    return max(1, total_chars // 4)


@dataclass
class AnthropicStreamAdapter:
    """Turn Chat Completions SSE chunks into Messages API SSE events.

    Anthropic content blocks are strictly sequential: a block must emit
    ``content_block_stop`` before the next block's ``content_block_start``.
    OpenAI interleaves at most one text stream plus indexed tool calls, and
    in practice emits them in order, so we close the open block whenever a
    new one begins.
    """

    model: str
    message_id: str = field(default_factory=lambda: f"msg_{uuid.uuid4().hex}")
    next_index: int = 0
    open_index: int | None = None  # anthropic block index currently open
    open_kind: str = ""
    text_block_index: int | None = None
    tool_blocks: dict[int, int] = field(default_factory=dict)  # chat index → block index
    finish_reason: str | None = None
    saw_tool_call: bool = False
    usage: dict = field(default_factory=dict)

    def start(self) -> list[dict]:
        return [{
            "type": "message_start",
            "message": {
                "id": self.message_id,
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }]

    def _close_open_block(self) -> list[dict]:
        if self.open_index is None:
            return []
        event = {"type": "content_block_stop", "index": self.open_index}
        self.open_index = None
        self.open_kind = ""
        return [event]

    def feed(self, chat_chunk: dict) -> list[dict]:
        events: list[dict] = []
        if chat_chunk.get("usage"):
            self.usage = chat_chunk["usage"]
        choices = chat_chunk.get("choices", [])
        if not choices:
            return events
        choice = choices[0]
        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]
        delta = choice.get("delta", {}) or {}

        content = delta.get("content")
        if content:
            if self.open_kind != "text":
                # A block emits exactly one start and one stop; once text was
                # interrupted by a tool block its old index is closed for
                # good, so text resuming afterwards opens a fresh block.
                events.extend(self._close_open_block())
                self.text_block_index = self.next_index
                self.next_index += 1
                events.append({
                    "type": "content_block_start",
                    "index": self.text_block_index,
                    "content_block": {"type": "text", "text": ""},
                })
                self.open_index = self.text_block_index
                self.open_kind = "text"
            events.append({
                "type": "content_block_delta",
                "index": self.text_block_index,
                "delta": {"type": "text_delta", "text": str(content)},
            })

        for piece in delta.get("tool_calls", []) or []:
            self.saw_tool_call = True
            chat_index = int(piece.get("index", 0))
            fn = piece.get("function", {}) or {}
            if chat_index not in self.tool_blocks:
                events.extend(self._close_open_block())
                block_index = self.next_index
                self.next_index += 1
                self.tool_blocks[chat_index] = block_index
                events.append({
                    "type": "content_block_start",
                    "index": block_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": piece.get("id") or f"toolu_{uuid.uuid4().hex}",
                        "name": fn.get("name", ""),
                        "input": {},
                    },
                })
                self.open_index = block_index
                self.open_kind = "tool_use"
            arguments = fn.get("arguments", "")
            if arguments:
                events.append({
                    "type": "content_block_delta",
                    "index": self.tool_blocks[chat_index],
                    "delta": {"type": "input_json_delta", "partial_json": arguments},
                })
        return events

    def finish(self) -> list[dict]:
        events = self._close_open_block()
        events.append({
            "type": "message_delta",
            "delta": {
                "stop_reason": _stop_reason(self.finish_reason, self.saw_tool_call),
                "stop_sequence": None,
            },
            "usage": {
                "input_tokens": int((self.usage or {}).get("prompt_tokens", 0) or 0),
                "output_tokens": int((self.usage or {}).get("completion_tokens", 0) or 0),
            },
        })
        events.append({"type": "message_stop"})
        return events
