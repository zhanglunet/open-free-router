# -*- coding: utf-8 -*-
"""PRD-471/T2+T3（trend issue #2276/#2277 的 T2+T3 环节）：粘性会话+交接生成。

按 T1 设计（docs/plans/2026-11-17-sticky-sessions-design.md）§1/§2 落地：
  - StickySessionManager（T2）：30 分钟粘性窗口内同 session_id 路由到
    同一模型；超时后正常重选（路由器全量打分）；模型不可用时粘性立即
    失效（强制换路——安全优先于粘性）；
  - HandoffGenerator（T3）：换模型时生成精简交接说明（只含摘要与元数据，
    原文与密钥永不进交接说明——T4/T5 钉值）；交接说明含 key_topics 列表
    （话题连贯性有验证）；交接说明结构化（非自由文本转发）。
纯内存实现；零网络零子进程；示例全虚构占位。
测试：tests/test_prd471_t2_sticky_sessions.py
"""
from __future__ import annotations

import copy
from typing import Any

STICKY_SCHEMA = "sticky-session/v1"
HANDOFF_SCHEMA = "handoff/v1"
STICKY_WINDOW_SECONDS = 1800        # 30 分钟
ERROR_CODES = ("STICKY_INCOMPLETE", "STICKY_STATE_CONFLICT")


class StickyError(ValueError):
    """粘性会话违规——显形拒绝。"""


def _require(ok: bool, code: str, detail: str) -> None:
    if not ok:
        raise StickyError(f"{code}：{detail}")


class StickySessionManager:
    """30 分钟粘性窗口管理（§1）。"""

    def __init__(self, *, window_seconds: int = STICKY_WINDOW_SECONDS,
                 now: float = 0.0) -> None:
        self.window = window_seconds
        self.now = now
        self.sessions: dict[str, dict[str, Any]] = {}

    def bind(self, *, session_id: str, model: str) -> dict[str, Any]:
        """出稿/路由时绑定粘性模型。"""
        _require(bool(session_id) and bool(model), "STICKY_INCOMPLETE",
                 "session_id/model 缺一")
        sticky_until = self.now + self.window
        sess = {"schema": STICKY_SCHEMA, "session_id": str(session_id),
                "sticky_model": str(model), "sticky_until": sticky_until}
        self.sessions[str(session_id)] = sess
        return copy.deepcopy(sess)

    def resolve(self, *, session_id: str, model_available_fn=None) -> dict[str, Any] | None:
        """路由时查粘性：窗口内返回粘性模型；超时后返回 None（正常重选）；
        模型不可用时粘性立即失效（返回 None 并清除粘性——安全优先于粘性）。"""
        sess = self.sessions.get(str(session_id))
        if sess is None:
            return None
        if self.now > sess["sticky_until"]:
            del self.sessions[str(session_id)]      # 超时后正常重选
            return None
        if model_available_fn is not None and \
                not model_available_fn(sess["sticky_model"]):
            del self.sessions[str(session_id)]      # 不可用 → 粘性立即失效
            return None
        return dict(sess)

    def invalidate(self, *, session_id: str) -> None:
        self.sessions.pop(str(session_id), None)


class HandoffGenerator:
    """换模型时的交接说明生成（§2，结构化非自由文本）。"""

    def generate(self, *, from_model: str, to_model: str,
                 summary: str, key_topics: list[str],
                 turn_count: int) -> dict[str, Any]:
        _require(bool(from_model) and bool(to_model), "STICKY_INCOMPLETE",
                 "from_model/to_model 缺一")
        _require(bool(summary), "STICKY_INCOMPLETE", "交接摘要不能为空")
        _require(isinstance(key_topics, list) and key_topics,
                 "STICKY_INCOMPLETE", "key_topics 至少一条（话题连贯性）")
        return {"schema": HANDOFF_SCHEMA, "from_model": str(from_model),
                "to_model": str(to_model), "summary": str(summary),
                "key_topics": [str(t) for t in key_topics],
                "turn_count": turn_count}
