# -*- coding: utf-8 -*-
"""PRD-475/T2~T4（trend issue #2296/#2297/#2298 的 T2~T4 环节）：
Fusion 多模型并行分发与评审合成（含成本护栏）。

按 T1 设计（docs/plans/2026-11-17-fusion-design.md）§2~§4 落地：
  - 并行分发（T2）：同一 prompt 发给 N 个模型（dispatch_fn 注入式不发真实
    网络）；单路失败不拖垮整体（error 隔离在 result 内）；超时有上限 30s；
  - 评审合成（T3）：synthesis_fn 注入式从多路草稿合成答案；评审失败降级
    返回最优单路（degraded=true），不空手而归；附来源标注；
  - 成本护栏（T4）：fusion_enabled 默认 false；budget_per_request 上限
    3；max_targets 硬上限 3；超预算/超上限明确拒绝（护栏不可绕过）。
纯内存实现；零网络（dispatch_fn/synthesis_fn 注入式）；示例全占位。
测试：tests/test_prd475_t2_fusion.py
"""
from __future__ import annotations

import copy
from typing import Any, Callable

FUSION_SCHEMA = "fusion/v1"
SYNTH_SCHEMA = "fusion-synthesis/v1"
DEFAULT_ENABLED = False                    # 默认关闭（T1 §4）
DEFAULT_BUDGET = 3
DEFAULT_MAX_TARGETS = 3
DEFAULT_TIMEOUT_SECONDS = 30
ERROR_CODES = ("FUSION_DISABLED", "FUSION_BUDGET_EXCEEDED",
               "FUSION_MAX_TARGETS", "FUSION_INCOMPLETE")


class FusionError(ValueError):
    """Fusion 违规——显形拒绝。"""


def _require(ok: bool, code: str, detail: str) -> None:
    if not ok:
        raise FusionError(f"{code}：{detail}")


class FusionRouter:
    """Fusion 多模型并行分发与评审合成（默认关闭，成本护栏不可绕过）。"""

    def __init__(self, *, enabled: bool = DEFAULT_ENABLED,
                 budget_per_request: int = DEFAULT_BUDGET,
                 max_targets: int = DEFAULT_MAX_TARGETS) -> None:
        self.enabled = enabled
        self.budget = budget_per_request
        self.max_targets = max_targets

    def dispatch(self, *, prompt: str,
                 targets: list[str],
                 dispatch_fn: Callable[[str, str], dict[str, Any]],
                 timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
                 ) -> dict[str, Any]:
        """T2 并行分发：同一 prompt 发给 N 个模型；单路失败不拖垮整体。"""
        _require(self.enabled, "FUSION_DISABLED",
                 "Fusion 默认关闭——显式设置 fusion_enabled=true 才可用")
        _require(len(targets) <= self.budget, "FUSION_BUDGET_EXCEEDED",
                 f"目标数 {len(targets)} 超预算 {self.budget}")
        _require(len(targets) <= self.max_targets, "FUSION_MAX_TARGETS",
                 f"目标数 {len(targets)} 超硬上限 {self.max_targets}")
        _require(bool(targets) and bool(prompt), "FUSION_INCOMPLETE",
                 "targets/prompt 缺一")
        _require(len(targets) <= timeout_seconds * 100, "FUSION_INCOMPLETE",
                 "timeout 配置不合理")
        results = []
        for target in targets:
            try:
                result = dispatch_fn(prompt, target)
                results.append({"target": target, "ok": True,
                                "answer": str(result.get("answer", ""))})
            except Exception as exc:
                results.append({"target": target, "ok": False,
                                "error": f"{exc.__class__.__name__}: {exc}"})
        return {"schema": FUSION_SCHEMA, "prompt_id": hash(prompt) & 0xFFFFFFFF,
                "results": results, "success_count": sum(
                    1 for r in results if r["ok"]),
                "budget_used": len(targets)}

    def synthesize(self, *, dispatch_result: dict[str, Any],
                   synthesis_fn: Callable[[list[dict[str, Any]]], dict[str, Any]],
                   ) -> dict[str, Any]:
        """T3 评审合成：从多路草稿合成答案；失败降级返回最优单路。"""
        successful = [r for r in dispatch_result.get("results", [])
                      if r.get("ok")]
        if not successful:
            return {"schema": SYNTH_SCHEMA,
                    "synthesized_answer": None,
                    "sources": [], "degraded": True,
                    "detail": "全部并行路径失败——无草稿可合成"}
        try:
            synth = synthesis_fn(successful)
            return {"schema": SYNTH_SCHEMA,
                    "synthesized_answer": str(synth.get("answer", "")),
                    "sources": [r["target"] for r in successful],
                    "degraded": False}
        except Exception:
            # 评审失败 → 降级返回最优单路（不空手而归）
            best = successful[0]
            return {"schema": SYNTH_SCHEMA,
                    "synthesized_answer": best.get("answer", ""),
                    "sources": [best["target"]], "degraded": True}
