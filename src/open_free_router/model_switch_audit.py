#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRD-202/T3（trend 前区间）：一句话换模型——回滚 + 审计事件。

按 PRD-202/T1 设计 §3.3（回滚）+ §3.4（审计事件）实现 T3 最小范围：
  undo_switch(switch_event) → 回滚到切换前路由 + 回滚事件；
  export_events() → 机读审计导出（switch/rollback 事件按序）；
  与 PRD-149 审计账本划界（149 事后审计、202 实时事件流）。
零网络；示例全虚构。
"""
from __future__ import annotations

import json
from typing import Any


class ModelSwitchAudit:
    """审计事件流：switch/rollback 按序记录，export 可序列化。"""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    def record_switch(self, from_route: str, to_route: str, actor: str, at: str) -> None:
        self._events.append({"event": "switch", "from": from_route, "to": to_route,
                              "actor": actor, "at": at})

    def record_rollback(self, from_route: str, to_route: str, actor: str, at: str) -> None:
        self._events.append({"event": "rollback", "from": from_route, "to": to_route,
                              "actor": actor, "at": at})

    def export_jsonl(self) -> str:
        return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in self._events)

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)


def undo_switch(current_route: str, previous_route: str,
                audit: ModelSwitchAudit, actor: str, at: str) -> dict[str, Any]:
    """回滚到切换前路由 + 留审计。"""
    audit.record_rollback(from_route=current_route, to_route=previous_route,
                           actor=actor, at=at)
    return {"ok": True, "route": previous_route, "previous": current_route}
