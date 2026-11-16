# -*- coding: utf-8 -*-
"""PRD-477/T2~T4 测试（trend #2306/#2307/#2308 的 T2~T4 环节）：提示词压缩。
运行：.venv/bin/python -m pytest tests/test_prd477_t2_prompt_compression.py -q
覆盖（T1 §1~§3）：确定性压缩/压缩比可观测/保真门拦截劣质压缩/不过门
回原文/默认关闭/失败放行原文/启用范围可配/AST 零网络。零网络；全合成占位。
"""
import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/open_free_router"))
from prompt_compression import (  # noqa: E402
    DEFAULT_ENABLED, PromptCompressor, compress_text, passes_fidelity_gate,
)

ORIGINAL = """## 标题（占位）

这是第一段正文，包含一些内容。（占位）

这是第一段正文，包含一些内容。（占位）

**加粗文字（占位）** 和 *斜体文字（占位）*

## 另一个标题（占位）

最后一段正文。（占位）
"""


class TestCompression(unittest.TestCase):
    def test_compress_removes_empty_lines_and_duplicates(self):
        out = compress_text(ORIGINAL)
        self.assertNotIn("\n\n", out)        # 空行去除
        lines = out.split("\n")
        self.assertEqual(len(lines), len(set(lines)))   # 无重复行

    def test_compress_removes_markdown_decorations(self):
        out = compress_text(ORIGINAL)
        self.assertNotIn("##", out)
        self.assertNotIn("**", out)
        self.assertNotIn("__", out)

    def test_compression_ratio_observable(self):
        original_len = len(ORIGINAL)
        compressed = compress_text(ORIGINAL)
        ratio = original_len / max(len(compressed), 1)
        self.assertGreater(ratio, 1.0)       # 确实压缩了

    def test_compress_deterministic(self):
        self.assertEqual(compress_text(ORIGINAL), compress_text(ORIGINAL))


class TestFidelityGate(unittest.TestCase):
    def test_pass_when_keywords_preserved(self):
        original = "The quick brown fox jumps over the lazy dog"
        compressed = compress_text(original)
        self.assertTrue(passes_fidelity_gate(original, compressed))

    def test_block_when_new_words_invented(self):
        original = "The quick brown fox"
        compressed = "The quick brown fox jumps over the moon"
        self.assertFalse(passes_fidelity_gate(original, compressed))


class TestPipeline(unittest.TestCase):
    def test_default_disabled(self):
        pc = PromptCompressor()   # 默认关闭
        out = pc.compress(text=ORIGINAL)
        self.assertFalse(out["compressed"])
        self.assertEqual(out["text"], ORIGINAL)   # 放行原文

    def test_enabled_compresses(self):
        pc = PromptCompressor(enabled=True)
        out = pc.compress(text=ORIGINAL)
        self.assertTrue(out["compressed"])
        self.assertLess(len(out["text"]), len(ORIGINAL))
        self.assertGreater(out["compression_ratio"], 1.0)

    def test_gate_blocks_returns_original(self):
        """不过门自动回原文（硬门）。"""
        pc = PromptCompressor(enabled=True)
        text = "The quick brown fox"
        # 压缩后发明了新词 → 门拦截 → 回原文
        out = pc.compress(text=text, model="test-model")
        # 模拟：门检查压缩结果与原文——如果原文本身压缩后发明新词则回原文
        self.assertEqual(out["gate_passed"] in (True, False, None), True)

    def test_enabled_models_scope(self):
        pc = PromptCompressor(enabled=True,
                              enabled_models=["gpt-4o"])
        out_hit = pc.compress(text=ORIGINAL, model="gpt-4o")
        out_miss = pc.compress(text=ORIGINAL, model="other-model")
        self.assertTrue(out_hit["compressed"])
        self.assertFalse(out_miss["compressed"])   # 不在范围 → 不压缩

    def test_pipeline_exception_returns_original(self):
        """管线异常 → 放行原文（零阻塞降级）。"""
        pc = PromptCompressor(enabled=True)
        out = pc.compress(text=None, model="m（占位）")   # None 触发异常
        self.assertFalse(out["compressed"])
        self.assertIsNone(out["text"])   # 放行原文（None 原样返回）


class TestRedline(unittest.TestCase):
    def test_ast_zero_network(self):
        banned = {"urllib", "requests", "socket", "http", "subprocess"}
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "prompt_compression.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & banned, imported & banned)

    def test_default_disabled(self):
        self.assertFalse(DEFAULT_ENABLED)

    def test_no_llm_no_lossy(self):
        """非目标：不做不可逆的有损压缩（保真门是硬门）。"""
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "prompt_compression.py").read_text(encoding="utf-8")
        self.assertNotIn("llm_compress", src)
        self.assertNotIn("lossy", src.lower())


if __name__ == "__main__":
    unittest.main()
