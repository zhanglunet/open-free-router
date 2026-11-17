# -*- coding: utf-8 -*-
"""PRD-468/T2（trend issue #2261 的 T2 环节）：冷却阶梯与原因标签。

按 T1 设计（docs/plans/2026-11-17-cooldown-ladder-design.md）§2 落地：
  - 429 阶梯：90s → 2min → 10min → 1h → 1天（连续 429 逐级升）；
  - 402 欠费冷 1 天（实锤）；401 鉴权失败冷到下一健康周期（实锤）；
  - 成功一次 → 阶梯步数清零；
  - 原因标签随冷却落账（谁/何时/原因/阶梯步数/猜测性）进 CooldownLog
    （JSONL append-only 路径注入）；
  - 与 PRD-059/T4 边界：本模块只管凭证槽级冷却阶梯，不改单次退避口径。
零 LLM 零网络零子进程；示例全虚构占位。
测试：tests/test_prd468_t2_cooldown_ladder.py
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

LADDER_SCHEMA = "cooldown-ladder/v1"
LADDER_STEPS = (90, 120, 600, 3600, 86400)
DAY_SECONDS = 86400
REASON_429 = "429_limited"
REASON_402 = "402_payment"
REASON_401 = "401_auth"
REASON_SUCCESS = "success_reset"
ERROR_CODES = ("LADDER_INCOMPLETE", "LADDER_STATE_CONFLICT")


class LadderError(ValueError):
    """阶梯违规——显形拒绝。"""


def _require(ok: bool, code: str, detail: str) -> None:
    if not ok:
        raise LadderError(f"{code}：{detail}")


class CooldownLadder:
    """凭证槽级冷却阶梯（连续 429 逐级升、成功清零、402/401 实锤钉死）。"""

    def __init__(self, *, log_path: str | Path | None = None,
                 now: float | None = None) -> None:
        self.log_path = Path(log_path) if log_path else None
        self.now = now if now is not None else 0.0
        self.slots: dict[str, dict[str, Any]] = {}
        self.log: list[dict[str, Any]] = []

    def _key(self, platform: str, model_id: str, slot: int) -> str:
        return f"{platform}:{model_id}:slot-{slot}"

    def _log(self, kind: str, key: str, payload: dict[str, Any]) -> None:
        entry = {"schema": LADDER_SCHEMA, "kind": kind, "key": key,
                 "at": self.now, **copy.deepcopy(payload)}
        self.log.append(entry)
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False,
                                    sort_keys=True) + "\n")

    def on_rate_limited(self, *, platform: str, model_id: str, slot: int,
                        retry_after: float | None = None) -> dict[str, Any]:
        """429 限流：阶梯逐级升。上游说了 retry_after 则以实锤优先。"""
        key = self._key(platform, model_id, slot)
        state = self.slots.setdefault(key, {"step": 0, "reason": None,
                                            "cooldown_until": 0.0})
        step = min(state["step"] + 1, len(LADDER_STEPS))
        cooldown = LADDER_STEPS[step - 1]
        if retry_after is not None:
            cooldown = max(cooldown, float(retry_after))   # 实锤优先不缩短
        state["step"] = step
        state["reason"] = REASON_429
        state["cooldown_until"] = self.now + cooldown
        out = {"key": key, "reason": REASON_429, "step": step,
               "cooldown_seconds": cooldown, "cooldown_until":
                   state["cooldown_until"], "speculative": retry_after is None}
        self._log("cooldown", key, out)
        return out

    def on_payment_required(self, *, platform: str, model_id: str,
                            slot: int) -> dict[str, Any]:
        """402 欠费：冷 1 天（实锤，不走阶梯）。"""
        key = self._key(platform, model_id, slot)
        state = self.slots.setdefault(key, {"step": 0, "reason": None,
                                            "cooldown_until": 0.0})
        state["reason"] = REASON_402
        state["cooldown_until"] = self.now + DAY_SECONDS
        out = {"key": key, "reason": REASON_402, "step": 0,
               "cooldown_seconds": DAY_SECONDS,
               "cooldown_until": state["cooldown_until"], "speculative": False}
        self._log("cooldown", key, out)
        return out

    def on_auth_failed(self, *, platform: str, model_id: str,
                       slot: int) -> dict[str, Any]:
        """401 鉴权失败：冷到下一健康周期（实锤）。"""
        key = self._key(platform, model_id, slot)
        state = self.slots.setdefault(key, {"step": 0, "reason": None,
                                            "cooldown_until": 0.0})
        state["reason"] = REASON_401
        state["cooldown_until"] = float("inf")   # 到人工换钥（下一健康周期）
        out = {"key": key, "reason": REASON_401, "step": 0,
               "cooldown_seconds": None, "cooldown_until": "inf",
               "speculative": False}
        self._log("cooldown", key, out)
        return out

    def on_success(self, *, platform: str, model_id: str, slot: int) -> dict[str, Any]:
        """成功一次 → 阶梯步数清零（回 90s 起点）。"""
        key = self._key(platform, model_id, slot)
        state = self.slots.setdefault(key, {"step": 0, "reason": None,
                                            "cooldown_until": 0.0})
        prev_step = state["step"]
        state["step"] = 0
        state["reason"] = REASON_SUCCESS
        out = {"key": key, "reason": REASON_SUCCESS, "prev_step": prev_step,
               "step": 0}
        self._log("success_reset", key, out)
        return out

    def is_cooled_down(self, platform: str, model_id: str, slot: int) -> bool:
        key = self._key(platform, model_id, slot)
        state = self.slots.get(key)
        if state is None:
            return False
        return state["cooldown_until"] > self.now or \
            state["cooldown_until"] == float("inf")

    def entries(self) -> list[dict[str, Any]]:
        return [dict(e) for e in self.log]
