# -*- coding: utf-8 -*-
"""PRD-477/T2~T4（trend issue #2306/#2307/#2308 的 T2~T4 环节）：
提示词压缩管线（确定性压缩+保真门+开关降级）。

按 T1 设计（docs/plans/2026-11-17-prompt-compression-design.md）§1~§3 落地：
  - 压缩管线（T2）：确定性压缩（去空行/去重复空白/去 markdown 装饰/
    合并连续相同行，不用 LLM 保证可复算）；压缩比可观测；
  - 保真门（T3）：原文关键词集合 ⊇ 压缩后关键词集合（不发明新词）才
    通过——不过门自动回原文（硬门）；
  - 开关与降级（T4）：prompt_compression_enabled 默认 false；失败即
    放行原文；enabled_models 列表可配（不在列表中的模型不压缩）。
纯内存实现；零网络零子进程；示例全占位。
测试：tests/test_prd477_t2_prompt_compression.py
"""
from __future__ import annotations

import copy
import re
from typing import Any

COMPRESSION_SCHEMA = "prompt-compression/v1"
DEFAULT_ENABLED = False                    # 默认关闭（T1 §3）
ERROR_CODES = ("COMPRESSION_INCOMPLETE",)


class CompressionError(ValueError):
    """压缩违规——显形拒绝。"""


def _require(ok: bool, code: str, detail: str) -> None:
    if not ok:
        raise CompressionError(f"{code}：{detail}")


def compress_text(text: str) -> str:
    """确定性压缩（§1）：去空行/去重复空白/去 markdown 装饰/合并连续相同行。"""
    lines = text.split("\n")
    cleaned = []
    prev = None
    for line in lines:
        line = line.strip()
        line = re.sub(r"\s+", " ", line)     # 多空格合并
        line = re.sub(r"^#{1,6}\s*", "", line)  # 去 markdown 标题符号
        line = re.sub(r"\*\*|__|~~", "", line)  # 去 markdown 装饰
        if not line:
            continue                          # 去空行
        if line == prev:
            continue                          # 合并连续相同行
        cleaned.append(line)
        prev = line
    return "\n".join(cleaned)


def _keyword_set(text: str) -> set[str]:
    """关键词集合 = 长度 > 2 的词（去停用词前的词干集）。"""
    return {w for w in re.findall(r"[\w]+", text) if len(w) > 2}


def passes_fidelity_gate(original: str, compressed: str) -> bool:
    """保真门（§2）：原文关键词集合 ⊇ 压缩后关键词集合才通过。"""
    orig_set = _keyword_set(original)
    comp_set = _keyword_set(compressed)
    return comp_set <= orig_set


class PromptCompressor:
    """提示词压缩管线：压缩→保真门→回退原文（硬门，不是可选项）。"""

    def __init__(self, *, enabled: bool = DEFAULT_ENABLED,
                 enabled_models: list[str] | None = None) -> None:
        self.enabled = enabled
        self.enabled_models = set(enabled_models or [])

    def compress(self, *, text: str, model: str = "") -> dict[str, Any]:
        """压缩管线（T2+T3+T4 合并）：压缩→保真门→回原文。

        返回 {text, compressed, compression_ratio, gate_passed}。
        未启用/模型不在范围/管线异常 → 放行原文（零阻塞降级）。
        """
        if not self.enabled:
            return {"text": text, "compressed": False,
                    "compression_ratio": 1.0, "gate_passed": None}
        if self.enabled_models and model not in self.enabled_models:
            return {"text": text, "compressed": False,
                    "compression_ratio": 1.0, "gate_passed": None}
        try:
            compressed = compress_text(text)
            gate = passes_fidelity_gate(text, compressed)
            if not gate:
                # 保真门不过 → 自动回原文（硬门）
                return {"text": text, "compressed": False,
                        "compression_ratio": 1.0, "gate_passed": False}
            ratio = (len(text) / len(compressed)) if compressed else 1.0
            return {"text": compressed, "compressed": True,
                    "compression_ratio": round(ratio, 3),
                    "gate_passed": True}
        except Exception:
            # 管线任何异常 → 放行原文（零阻塞降级）
            return {"text": text, "compressed": False,
                    "compression_ratio": 1.0, "gate_passed": None}
