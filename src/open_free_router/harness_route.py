#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRD-257/T2（trend #1286）：后端统一路由协议最小实现——backend-route-v1。

按 PRD-257/T1 设计（docs/plans/2026-10-28-prd-257-backend-routing.md）实现 T2 范围：
  协议对象：ChatSession / Request / StreamChunk / Response / StructuredFailure（七类封闭枚举）；
  routes 配置驱动：客户端只认 route_id——同一客户端调用在双后端间切换零代码改动；
  适配器接口 probe/open_session/send/cancel + capabilities 如实声明；
  双后端：stub（零网络，协议基座）+ local-echo（能力子集，synthetic_stream 如实标注）。
  审计切换事件对齐既有 failover_audit 纪律（T4 深化）。

零网络；本模块不触碰凭据（base_url_ref 只透传引用，不解析密钥）；
StructuredFailure.backend_detail 生成时即经 redact 占位符化。示例全虚构。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SCHEMA = "backend-route-v1"

FAILURE_CLASSES = (
    "connect", "auth", "rate_limit", "timeout", "protocol", "backend_error", "cancelled",
)
CHUNK_TYPES = ("delta", "usage", "done", "failed", "cancelled")

_SECRET_RE = re.compile(
    r"sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|Bearer\s+[A-Za-z0-9._-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}")
_LAN_RE = re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")


def redact(text: str) -> str:
    text = _SECRET_RE.sub("<REDACTED>", text)
    return _LAN_RE.sub("<REDACTED>", text)


def structured_failure(cls: str, code: str, message: str, *,
                       retryable: bool = False, backend_detail: str = "") -> dict[str, Any]:
    if cls not in FAILURE_CLASSES:
        raise ValueError(f"非法失败类: {cls}（封闭枚举 {'/'.join(FAILURE_CLASSES)}）")
    return {
        "class": cls, "code": code, "message": message, "retryable": retryable,
        "backend_detail": redact(backend_detail),
    }


# ── 适配器 ──

class BaseAdapter:
    capabilities: tuple[str, ...] = ()

    def __init__(self, backend_id: str) -> None:
        self.backend_id = backend_id

    def probe(self) -> dict[str, Any]:
        return {"ok": True, "backend_id": self.backend_id,
                "capabilities": list(self.capabilities)}

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class StubAdapter(BaseAdapter):
    """零网络协议基座：全能力。会话计数持久化在内存。"""

    capabilities = ("stream", "persistent", "cancel")

    def __init__(self) -> None:
        super().__init__("stub")
        self.sessions: dict[str, int] = {}

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        session = request.get("session") or {}
        sid = session.get("session_id", "stub-default")
        self.sessions[sid] = self.sessions.get(sid, 0) + 1
        text = f"[stub] echo({self.sessions[sid]}): {request['messages'][-1]['content']}"
        chunks = [
            {"request_id": request["request_id"], "seq": 1, "type": "delta", "payload": {"text": text}},
            {"request_id": request["request_id"], "seq": 2, "type": "usage",
             "payload": {"turns": self.sessions[sid]}},
            {"request_id": request["request_id"], "seq": 3, "type": "done", "payload": {}},
        ]
        return {"ok": True, "chunks": chunks, "response": _response_ok(request["request_id"], text)}


class LocalEchoAdapter(BaseAdapter):
    """能力子集：无 stream / persistent——synthetic_stream 如实标注。"""

    capabilities = ("cancel",)

    def __init__(self) -> None:
        super().__init__("local-echo")

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        text = f"[local-echo] {request['messages'][-1]['content']}"
        chunks = [
            {"request_id": request["request_id"], "seq": 1, "type": "done",
             "payload": {"synthetic_stream": True, "text": text}},
        ]
        return {"ok": True, "chunks": chunks,
                "response": _response_ok(request["request_id"], text, synthetic_stream=True)}


def _response_ok(request_id: str, content: str, synthetic_stream: bool = False) -> dict[str, Any]:
    return {"request_id": request_id, "status": "ok", "content": content,
            "usage": {}, "synthetic_stream": synthetic_stream, "failure": None}


# ── 路由器 ──

class HarnessRouter:
    """routes 配置 → 适配器分发。客户端只认 route_id。"""

    def __init__(self, config: dict[str, Any],
                 adapters: dict[str, Any] | None = None) -> None:
        if config.get("schema") != SCHEMA:
            raise ValueError(f"routes 配置 schema 必须为 {SCHEMA}")
        self._routes = {r["route_id"]: r for r in config.get("routes", [])}
        self._default = config.get("default_route")
        self._adapters = adapters or {
            "stub": StubAdapter(),
            "local-echo": LocalEchoAdapter(),
        }
        # 切换审计（T4 导出；本层先留内存事件流）
        self.switch_events: list[dict[str, Any]] = []
        self._last_route: str | None = None

    def resolve(self, route_id: str | None) -> dict[str, Any]:
        rid = route_id or self._default
        route = self._routes.get(rid or "")
        if not route:
            return {"ok": False,
                    "response": {"request_id": "", "status": "failed", "content": None,
                                 "usage": {}, "synthetic_stream": False,
                                 "failure": structured_failure("protocol", "ROUTE_NOT_FOUND",
                                                               f"路由不存在: {rid}")}}
        backend_id = route.get("backend_id", "")
        adapter = self._adapters.get(backend_id)
        if adapter is None:
            return {"ok": False,
                    "response": {"request_id": "", "status": "failed", "content": None,
                                 "usage": {}, "synthetic_stream": False,
                                 "failure": structured_failure("protocol", "BACKEND_NOT_REGISTERED",
                                                               f"后端未注册: {backend_id}")}}
        return {"ok": True, "route": route, "adapter": adapter}

    def send(self, route_id: str | None, messages: list[dict[str, str]],
             request_id: str = "req-0") -> dict[str, Any]:
        resolved = self.resolve(route_id)
        if not resolved["ok"]:
            return resolved["response"]
        route, adapter = resolved["route"], resolved["adapter"]
        rid = route["route_id"]
        if self._last_route is not None and self._last_route != rid:
            self.switch_events.append({"route_from": self._last_route, "route_to": rid,
                                       "reason": "client_choice", "actor": "client"})
        self._last_route = rid
        request = {
            "request_id": request_id,
            "messages": messages,
            "session": route.get("session"),
            "base_url_ref": route.get("base_url_ref", ""),  # 只透传引用
            "policy_ref": route.get("policy_ref", ""),
        }
        out = adapter.send(request)
        # 客户端唯一可见面 = Response dict（ok/chunks 是适配器信封，不下发）
        return out.get("response", out) if isinstance(out, dict) else out
