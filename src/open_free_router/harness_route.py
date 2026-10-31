#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRD-257/T2+T3（trend #1286/#1287）：后端统一路由协议——backend-route-v1。

按 PRD-257/T1 设计（docs/plans/2026-10-28-prd-257-backend-routing.md）实现：
T2 范围（#1286）：
  协议对象：ChatSession / Request / StreamChunk / Response / StructuredFailure（七类封闭枚举）；
  routes 配置驱动：客户端只认 route_id——同一客户端调用在双后端间切换零代码改动；
  适配器接口 probe/open_session/send/cancel + capabilities 如实声明；
  双后端：stub（零网络，协议基座）+ local-echo（能力子集，synthetic_stream 如实标注）。
  审计切换事件对齐既有 failover_audit 纪律（T4 深化）。
T3 合同深化（#1287，§3.3 会话/流式/取消/结构化失败合同）：
  会话：ChatSession.expires_at 检查——persistent 会话过期返回
    StructuredFailure(class: protocol, code: SESSION_EXPIRED) 并提示重建；
  流式：assert_monotonic_seq——同一请求内 seq 单调正整数，乱序/非法抛 ValueError，
    路由边界（send/stream 返回前）强制校验；
  取消：取消是协议操作（有 ack 语义），cancel(request_id) 后下一次 send 返回
    cancelled chunk（payload.ack=true）+ status=cancelled；cancel_ack 校验
    cancelled 类失败必须带 ack 语义；
  失败：assert_structured_failure 校验器——七类封闭枚举 + 字段类型；
    路由边界对适配器返回的 failure 一律过校验（合同一致性）。

零网络；本模块不触碰凭据（base_url_ref 只透传引用，不解析密钥）；
StructuredFailure.backend_detail 生成时即经 redact 占位符化。示例全虚构。
"""
from __future__ import annotations

import re
import time
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


_FAILURE_FIELD_TYPES = {"code": str, "message": str, "retryable": bool, "backend_detail": str}


def assert_structured_failure(obj: Any) -> dict[str, Any]:
    """T3 失败合同校验器（§3.1/§3.3）：StructuredFailure 封闭七类 + 字段类型。

    非法（非 dict / 缺字段 / class 出枚举 / 字段类型不符）→ ValueError。
    通过则原样返回，供调用方链式断言。
    """
    if not isinstance(obj, dict):
        raise ValueError(f"StructuredFailure 必须为 dict，得到 {type(obj).__name__}")
    for key in ("class", *_FAILURE_FIELD_TYPES):
        if key not in obj:
            raise ValueError(f"StructuredFailure 缺字段: {key}")
    if obj["class"] not in FAILURE_CLASSES:
        raise ValueError(f"失败类越界: {obj['class']!r}（封闭枚举 {'/'.join(FAILURE_CLASSES)}）")
    for key, typ in _FAILURE_FIELD_TYPES.items():
        if not isinstance(obj[key], typ) or (typ is str and key == "code" and not obj[key]):
            raise ValueError(f"字段 {key} 类型必须为 {typ.__name__}，得到 {type(obj[key]).__name__}")
    return obj


def assert_monotonic_seq(chunks: Any) -> list[Any]:
    """T3 流式合同（§3.3）：同一请求内 seq 单调正整数（严格递增，从 1 起）。

    乱序 / 重复 / 非正整数 → ValueError。通过则原样返回 chunks。
    """
    last = 0
    if not isinstance(chunks, (list, tuple)):
        raise ValueError(f"chunks 必须为列表，得到 {type(chunks).__name__}")
    for i, chunk in enumerate(chunks):
        seq = chunk.get("seq") if isinstance(chunk, dict) else None
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
            raise ValueError(f"chunk[{i}] seq 必须为正整数，得到 {seq!r}")
        if seq <= last:
            raise ValueError(f"chunk[{i}] seq 乱序: {seq} 不大于前值 {last}（单调合同）")
        last = seq
    return chunks


DEFAULT_SESSION_TTL_S = 30 * 60  # persistent 会话默认 30 分钟


@dataclass
class ChatSession:
    """§3.1 ChatSession：persistent 会话跨请求复用；expires_at 过期检查（T3 合同）。"""

    session_id: str
    backend_id: str
    model: str
    created_at: float
    persistent: bool = True
    ttl_s: int = DEFAULT_SESSION_TTL_S

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl_s

    def expired(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "backend_id": self.backend_id,
                "model": self.model, "created_at": self.created_at,
                "persistent": self.persistent, "expires_at": self.expires_at}


def open_session(backend_id: str, model: str, *, session_id: str = "sess-1",
                 now: float | None = None, ttl_s: int = DEFAULT_SESSION_TTL_S,
                 persistent: bool = True) -> ChatSession:
    """开 persistent 会话（后端支持时）；now/ttl_s 可注入以便测试与策略收紧。"""
    return ChatSession(session_id=session_id, backend_id=backend_id, model=model,
                       created_at=(time.time() if now is None else now),
                       persistent=persistent, ttl_s=ttl_s)


def _is_session_expired(session: Any, now: float | None = None) -> bool:
    """ChatSession / dict（含 expires_at）两形态统一过期检查。"""
    if isinstance(session, ChatSession):
        return session.expired(now)
    if isinstance(session, dict):
        expires_at = session.get("expires_at")
        return isinstance(expires_at, (int, float)) and (now if now is not None else time.time()) >= expires_at
    return False


# ── 适配器 ──

class BaseAdapter:
    capabilities: tuple[str, ...] = ()

    def __init__(self, backend_id: str) -> None:
        self.backend_id = backend_id
        self._cancelled: set[str] = set()

    def probe(self) -> dict[str, Any]:
        return {"ok": True, "backend_id": self.backend_id,
                "capabilities": list(self.capabilities)}

    def cancel(self, request_id: str) -> dict[str, Any]:
        """取消是协议操作（§3.1），不是客户端单方面断连：登记后下一次
        send(request_id) 返回带 ack 语义的 cancelled chunk（T3 §3.3 取消合同）。"""
        self._cancelled.add(request_id)
        return {"request_id": request_id, "backend": self.backend_id, "requested": True}

    def _cancelled_ack(self, request_id: str) -> dict[str, Any] | None:
        """已登记取消 → cancelled ack 信封（一次性；未登记 → None 走正常路径）。"""
        if request_id not in self._cancelled:
            return None
        self._cancelled.discard(request_id)
        chunks = [{"request_id": request_id, "seq": 1, "type": "cancelled",
                   "payload": {"ack": True, "reason": "client_cancel"}}]
        return {"ok": True, "chunks": chunks,
                "response": {"request_id": request_id, "status": "cancelled", "content": None,
                             "usage": {}, "synthetic_stream": False,
                             "failure": structured_failure("cancelled", "CANCEL_ACKED",
                                                           "客户端取消，后端已确认（ack 语义）")}}

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class StubAdapter(BaseAdapter):
    """零网络协议基座：全能力。会话计数持久化在内存。"""

    capabilities = ("stream", "persistent", "cancel")

    def __init__(self) -> None:
        super().__init__("stub")
        self.sessions: dict[str, int] = {}

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        ack = self._cancelled_ack(request["request_id"])
        if ack is not None:
            return ack
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
        ack = self._cancelled_ack(request["request_id"])
        if ack is not None:
            return ack
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


def cancel_ack(request_id: str, adapter: BaseAdapter) -> dict[str, Any]:
    """T3 取消合同（§3.3）：cancelled 类失败必须带 ack 语义。

    cancel(request_id) 后下一次 send 必须返回：
      cancelled chunk（payload.ack=true） + response.status=cancelled
      + failure.class=cancelled（过 assert_structured_failure）；
    缺任一 → ValueError（合同违约，如实暴露而非静默吞掉）。
    """
    adapter.cancel(request_id)
    out = adapter.send({"request_id": request_id,
                        "messages": [{"role": "user", "content": "<cancel-probe 虚构>"}],
                        "session": {}})
    if not isinstance(out, dict):
        raise ValueError(f"后端 {adapter.backend_id} 取消响应非信封 dict")
    chunks = out.get("chunks", [])
    assert_monotonic_seq(chunks)
    resp = out.get("response") or {}
    cancelled_chunks = [c for c in chunks if isinstance(c, dict) and c.get("type") == "cancelled"]
    failure = resp.get("failure") or {}
    if not cancelled_chunks:
        raise ValueError(f"取消无 cancelled chunk（backend={adapter.backend_id}）——ack 语义缺失")
    if not cancelled_chunks[0].get("payload", {}).get("ack"):
        raise ValueError(f"cancelled chunk 缺 payload.ack（backend={adapter.backend_id}）")
    if failure.get("class") != "cancelled":
        raise ValueError(f"取消路径 failure.class 必须为 cancelled（backend={adapter.backend_id}）")
    if resp.get("status") != "cancelled":
        raise ValueError(f"取消路径 response.status 必须为 cancelled（backend={adapter.backend_id}）")
    assert_structured_failure(failure)
    return {"request_id": request_id, "backend": adapter.backend_id, "acked": True,
            "chunk_seq": cancelled_chunks[0]["seq"], "failure_code": failure.get("code")}


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
             request_id: str = "req-0",
             session: "ChatSession | dict[str, Any] | None" = None) -> dict[str, Any]:
        failure = self._session_failure(session, route_id)
        if failure is not None:
            return {"request_id": request_id, "status": "failed", "content": None,
                    "usage": {}, "synthetic_stream": False, "failure": failure}
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
            "session": self._session_payload(session, route),
            "base_url_ref": route.get("base_url_ref", ""),  # 只透传引用
            "policy_ref": route.get("policy_ref", ""),
        }
        out = adapter.send(request)
        self._enforce_contracts(out)  # 路由边界强制：seq 单调 + 失败结构 + cancelled ack
        # 客户端唯一可见面 = Response dict（ok/chunks 是适配器信封，不下发）
        return out.get("response", out) if isinstance(out, dict) else out

    def stream(self, route_id: str | None, messages: list[dict[str, str]],
               request_id: str = "req-0",
               session: "ChatSession | dict[str, Any] | None" = None) -> list[dict[str, Any]]:
        """T3 流式面（§3.1）：Request → StreamChunk*；seq 单调在路由层强制。

        解析失败/会话过期 → 单条 failed chunk（payload 为 StructuredFailure），
        事件形态与正常流一致（合同一致性）。
        """
        failure = self._session_failure(session, route_id)
        if failure is not None:
            return [{"request_id": request_id, "seq": 1, "type": "failed",
                     "payload": failure}]
        resolved = self.resolve(route_id)
        if not resolved["ok"]:
            return [{"request_id": request_id, "seq": 1, "type": "failed",
                     "payload": resolved["response"]["failure"]}]
        route, adapter = resolved["route"], resolved["adapter"]
        request = {
            "request_id": request_id,
            "messages": messages,
            "session": self._session_payload(session, route),
            "base_url_ref": route.get("base_url_ref", ""),
            "policy_ref": route.get("policy_ref", ""),
        }
        out = adapter.send(request)
        self._enforce_contracts(out)
        return out.get("chunks", [])

    # ── T3 合同内部件 ──

    @staticmethod
    def _session_payload(session: "ChatSession | dict[str, Any] | None",
                         route: dict[str, Any]) -> Any:
        if session is None:
            return route.get("session")
        return session.to_dict() if isinstance(session, ChatSession) else session

    def _session_failure(self, session: "ChatSession | dict[str, Any] | None",
                         route_id: str | None) -> dict[str, Any] | None:
        """§3.3 会话合同：persistent 会话过期 → StructuredFailure(protocol, SESSION_EXPIRED)
        并提示重建（retryable：重建会话后同一客户端调用可重试）。"""
        if session is None:
            return None
        if not _is_session_expired(session):
            return None
        return structured_failure("protocol", "SESSION_EXPIRED",
                                  "persistent 会话已过期，请重建会话（open_session）后重试",
                                  retryable=True)

    @staticmethod
    def _enforce_contracts(out: Any) -> None:
        """路由边界合同强制（§3.3）：适配器信封必须过 seq 单调 / 失败结构 / cancelled ack。"""
        if not isinstance(out, dict):
            return
        chunks = out.get("chunks") or []
        assert_monotonic_seq(chunks)
        resp = out.get("response") or {}
        failure = resp.get("failure")
        if failure is not None:
            assert_structured_failure(failure)
            if failure.get("class") == "cancelled":
                acked = any(isinstance(c, dict) and c.get("type") == "cancelled"
                            and c.get("payload", {}).get("ack") for c in chunks)
                if not acked:
                    raise ValueError("cancelled 类失败必须带 ack 语义的 cancelled chunk（§3.3）")
