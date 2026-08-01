"""Responses API compatibility for Chat Completions upstreams.

The router's providers largely expose the older OpenAI-compatible Chat
Completions API. Codex uses the Responses API, so this module performs the
small, state-free subset of translation Codex needs for local coding turns:
messages, function tools, function calls, tool outputs, text, and SSE deltas.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Iterable, Iterator


class ResponsesConversionError(ValueError):
    """The request contains an input type this compatibility layer cannot use."""


# Gemini 3's OpenAI-compatible Chat Completions endpoint returns an encrypted
# thought signature on function calls and requires the exact value on the next
# request. Responses function-call items have no vendor-extension field that
# Codex will round-trip, so retain the signature server-side by call id. Keep
# the cache bounded: signatures are opaque routing metadata, needed only for
# active tool loops, and must never be persisted or logged.
_THOUGHT_SIGNATURES: OrderedDict[str, str] = OrderedDict()
_THOUGHT_SIGNATURES_LOCK = threading.Lock()
_THOUGHT_SIGNATURES_MAX = 4096


def _extract_thought_signature(tool_call: dict) -> str:
    extra = tool_call.get("extra_content") or {}
    google = extra.get("google") if isinstance(extra, dict) else {}
    signature = google.get("thought_signature") if isinstance(google, dict) else None
    return signature if isinstance(signature, str) else ""


def _remember_thought_signature(call_id: str, signature: str) -> None:
    if not call_id or not signature:
        return
    with _THOUGHT_SIGNATURES_LOCK:
        _THOUGHT_SIGNATURES[call_id] = signature
        _THOUGHT_SIGNATURES.move_to_end(call_id)
        while len(_THOUGHT_SIGNATURES) > _THOUGHT_SIGNATURES_MAX:
            _THOUGHT_SIGNATURES.popitem(last=False)


def _thought_signature_for(call_id: str) -> str:
    if not call_id:
        return ""
    with _THOUGHT_SIGNATURES_LOCK:
        signature = _THOUGHT_SIGNATURES.get(call_id, "")
        if signature:
            _THOUGHT_SIGNATURES.move_to_end(call_id)
        return signature


def _text_content(content) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type", "")
        if kind in {"input_text", "output_text", "text"}:
            parts.append(str(part.get("text", "")))
        elif kind in {"input_image", "input_file", "output_image"}:
            raise ResponsesConversionError(
                f"Responses input type '{kind}' is not supported by this text-only upstream"
            )
    return "\n".join(p for p in parts if p)


def _tool_output_text(output) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return _text_content(output)
    return json.dumps(output, ensure_ascii=False)


def responses_to_chat(request: dict) -> dict:
    """Convert a Responses create request to a Chat Completions request."""
    messages: list[dict] = []
    instructions = request.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": _text_content(instructions)})

    input_items = request.get("input", [])
    if isinstance(input_items, str):
        messages.append({"role": "user", "content": input_items})
    elif isinstance(input_items, list):
        for item in input_items:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue
            kind = item.get("type", "message")
            if kind == "message":
                role = item.get("role", "user")
                if role == "developer":
                    role = "system"
                messages.append({"role": role, "content": _text_content(item.get("content", ""))})
            elif kind == "function_call":
                call_id = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}"
                tool_call = {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    },
                }
                signature = _thought_signature_for(call_id)
                if signature:
                    tool_call["extra_content"] = {
                        "google": {"thought_signature": signature}
                    }
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool_call],
                })
            elif kind == "function_call_output":
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id", ""),
                    "content": _tool_output_text(item.get("output", "")),
                })
            elif kind in {"reasoning", "item_reference"}:
                continue
            else:
                raise ResponsesConversionError(f"Responses input item '{kind}' is not supported")

    tools: list[dict] = []
    for tool in request.get("tools", []):
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        function = {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
        }
        if "strict" in tool:
            function["strict"] = bool(tool["strict"])
        tools.append({"type": "function", "function": function})

    chat = {
        "model": request.get("model", ""),
        "messages": messages,
        "stream": bool(request.get("stream", False)),
    }
    if tools:
        chat["tools"] = tools
        choice = request.get("tool_choice", "auto")
        if isinstance(choice, dict) and choice.get("type") == "function":
            choice = {"type": "function", "function": {"name": choice.get("name", "")}}
        if choice in {"auto", "none", "required"} or isinstance(choice, dict):
            chat["tool_choice"] = choice
        chat["parallel_tool_calls"] = bool(request.get("parallel_tool_calls", True))
    if request.get("max_output_tokens") is not None:
        chat["max_tokens"] = request["max_output_tokens"]
    for key in ("temperature", "top_p"):
        if request.get(key) is not None:
            chat[key] = request[key]
    return chat


def _usage(chat_usage: dict | None) -> dict:
    usage = chat_usage or {}
    input_tokens = int(usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(usage.get("completion_tokens", 0) or 0)
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": int(usage.get("total_tokens", input_tokens + output_tokens) or 0),
    }


def _base_response(response_id: str, model: str, status: str, output: list, usage=None) -> dict:
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": _usage(usage),
    }


def chat_to_response(chat_response: dict, requested_model: str) -> dict:
    """Convert a buffered Chat Completions response to a Responses object."""
    response_id = f"resp_{uuid.uuid4().hex}"
    choices = chat_response.get("choices", [])
    message = choices[0].get("message", {}) if choices else {}
    output: list[dict] = []
    content = message.get("content")
    if content is not None:
        output.append({
            "type": "message",
            "id": f"msg_{uuid.uuid4().hex}",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": str(content), "annotations": []}],
        })
    for call in message.get("tool_calls", []) or []:
        call_id = call.get("id") or f"call_{uuid.uuid4().hex}"
        _remember_thought_signature(call_id, _extract_thought_signature(call))
        fn = call.get("function", {})
        output.append({
            "type": "function_call",
            "id": f"fc_{uuid.uuid4().hex}",
            "call_id": call_id,
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments", "{}"),
            "status": "completed",
        })
    return _base_response(
        response_id,
        requested_model,
        "completed",
        output,
        chat_response.get("usage"),
    )


def sse_event(event: dict) -> bytes:
    event_type = event.get("type", "message")
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {payload}\n\n".encode()


@dataclass
class ResponsesStreamAdapter:
    """Turn Chat Completions SSE JSON chunks into Responses SSE events."""

    model: str
    response_id: str = field(default_factory=lambda: f"resp_{uuid.uuid4().hex}")
    message_id: str = field(default_factory=lambda: f"msg_{uuid.uuid4().hex}")
    sequence: int = 0
    text: str = ""
    text_started: bool = False
    calls: dict[int, dict] = field(default_factory=dict)
    usage: dict = field(default_factory=dict)

    def _event(self, event_type: str, **values) -> dict:
        self.sequence += 1
        return {"type": event_type, **values, "sequence_number": self.sequence}

    def start(self) -> list[dict]:
        response = _base_response(self.response_id, self.model, "in_progress", [], {})
        return [self._event("response.created", response=response)]

    def _start_text(self) -> list[dict]:
        if self.text_started:
            return []
        self.text_started = True
        item = {
            "type": "message", "id": self.message_id, "status": "in_progress",
            "role": "assistant", "content": [],
        }
        return [
            self._event("response.output_item.added", output_index=0, item=item),
            self._event(
                "response.content_part.added",
                item_id=self.message_id,
                output_index=0,
                content_index=0,
                part={"type": "output_text", "text": "", "annotations": []},
            ),
        ]

    def feed(self, chat_chunk: dict) -> list[dict]:
        events: list[dict] = []
        if chat_chunk.get("usage"):
            self.usage = chat_chunk["usage"]
        choices = chat_chunk.get("choices", [])
        if not choices:
            return events
        delta = choices[0].get("delta", {}) or {}
        content = delta.get("content")
        if content:
            events.extend(self._start_text())
            self.text += str(content)
            events.append(self._event(
                "response.output_text.delta",
                item_id=self.message_id,
                output_index=0,
                content_index=0,
                delta=str(content),
                logprobs=[],
            ))
        for piece in delta.get("tool_calls", []) or []:
            index = int(piece.get("index", 0))
            call = self.calls.get(index)
            new_call = call is None
            fn = piece.get("function", {}) or {}
            if new_call:
                output_index = (1 if self.text_started else 0) + len(self.calls)
                call = {
                    "id": f"fc_{uuid.uuid4().hex}",
                    "call_id": piece.get("id") or f"call_{uuid.uuid4().hex}",
                    "name": fn.get("name", ""),
                    "arguments": "",
                    "output_index": output_index,
                    "thought_signature": "",
                }
                self.calls[index] = call
                item = {
                    "type": "function_call",
                    "id": call["id"],
                    "call_id": call["call_id"],
                    "name": call["name"],
                    "arguments": "",
                    "status": "in_progress",
                }
                events.append(self._event(
                    "response.output_item.added",
                    output_index=output_index,
                    item=item,
                ))
            if piece.get("id"):
                call["call_id"] = piece["id"]
            signature = _extract_thought_signature(piece)
            if signature:
                call["thought_signature"] = signature
            if fn.get("name") and not new_call:
                call["name"] += fn["name"] if call["name"] else fn["name"]
            arguments = fn.get("arguments", "")
            if arguments:
                call["arguments"] += arguments
                events.append(self._event(
                    "response.function_call_arguments.delta",
                    item_id=call["id"],
                    output_index=call["output_index"],
                    delta=arguments,
                ))
        return events

    def finish(self) -> list[dict]:
        events: list[dict] = []
        output: list[dict] = []
        if self.text_started:
            part = {"type": "output_text", "text": self.text, "annotations": []}
            item = {
                "type": "message", "id": self.message_id, "status": "completed",
                "role": "assistant", "content": [part],
            }
            events.extend([
                self._event(
                    "response.output_text.done", item_id=self.message_id,
                    output_index=0, content_index=0, text=self.text, logprobs=[],
                ),
                self._event(
                    "response.content_part.done", item_id=self.message_id,
                    output_index=0, content_index=0, part=part,
                ),
                self._event("response.output_item.done", output_index=0, item=item),
            ])
            output.append(item)
        for index in sorted(self.calls):
            call = self.calls[index]
            _remember_thought_signature(
                call["call_id"], call.get("thought_signature", "")
            )
            item = {
                "type": "function_call",
                "id": call["id"],
                "call_id": call["call_id"],
                "name": call["name"],
                "arguments": call["arguments"],
                "status": "completed",
            }
            events.extend([
                self._event(
                    "response.function_call_arguments.done",
                    item_id=call["id"], output_index=call["output_index"],
                    name=call["name"], arguments=call["arguments"],
                ),
                self._event(
                    "response.output_item.done",
                    output_index=call["output_index"], item=item,
                ),
            ])
            output.append(item)
        response = _base_response(
            self.response_id, self.model, "completed", output, self.usage
        )
        events.append(self._event("response.completed", response=response))
        return events


def parse_chat_sse(lines: Iterable[bytes]) -> Iterator[dict]:
    """Yield JSON objects from an OpenAI-compatible Chat SSE byte stream."""
    for raw in lines:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue
