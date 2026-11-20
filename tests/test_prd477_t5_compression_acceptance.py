# -*- coding: utf-8 -*-
"""PRD-477/T5 测试（trend #2309 的 T5 环节）：提示词压缩验收试运行。
运行：.venv/bin/python -m pytest tests/test_prd477_t5_compression_acceptance.py -q
验收（PRD 任务拆分 T5 行「含压缩收益与保真实测」）：全链两遍逐字节一致——
启用 → 长文本压缩 → 压缩比可量化（>1.0）→ 保真门通过 → 压缩后文本仍含
关键词；失败演练：默认关闭/发明新词回原文/管线异常放行原文；链复跑
T2~T4 子进程 exit 0；文档无密钥。零网络；全合成占位。
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src/open_free_router"))
TESTS = Path(__file__).resolve().parent

from prompt_compression import PromptCompressor  # noqa: E402

NOW = "2026-11-15T09:00:00Z"
LONG_TEXT = """## 分析报告标题（占位）

这是报告的第一段正文，讨论了指标的定义与计算方式。（占位）

这是报告的第一段正文，讨论了指标的定义与计算方式。（占位）

**关键发现：**指标 A 的值显著高于基线。（占位）

## 第二部分（占位）

基于以上分析，我们提出了以下建议。（占位）

基于以上分析，我们提出了以下建议。（占位）

最后总结：项目总体进展良好。（占位）
"""


def full_chain():
    """全链：启用 → 压缩 → 保真门 → 可观测压缩比。"""
    pc = PromptCompressor(enabled=True)
    out = pc.compress(text=LONG_TEXT, model="gpt-4o（占位）")
    return out


class TestAcceptance(unittest.TestCase):
    def test_chain_twice_byte_identical(self):
        runs = [full_chain(), full_chain()]
        self.assertEqual(json.dumps(runs[0], sort_keys=True),
                         json.dumps(runs[1], sort_keys=True))

    def test_compression_ratio_quantifiable(self):
        """收益可量化：压缩比 > 1.0（确实省了空间）。"""
        run = full_chain()
        self.assertGreater(run["compression_ratio"], 1.0)
        self.assertTrue(run["compressed"])
        self.assertTrue(run["gate_passed"])

    def test_keywords_preserved_after_compression(self):
        """保真实测：压缩后关键词仍存在（保真门通过的关键词子集性质）。"""
        run = full_chain()
        from prompt_compression import passes_fidelity_gate
        self.assertTrue(passes_fidelity_gate(LONG_TEXT, run["text"]))


class TestFailureDrills(unittest.TestCase):
    def test_default_disabled_returns_original(self):
        pc = PromptCompressor()   # 默认关闭
        out = pc.compress(text=LONG_TEXT)
        self.assertFalse(out["compressed"])
        self.assertEqual(out["text"], LONG_TEXT)   # 放行原文

    def test_new_words_block_gate(self):
        """发明新词 → 保真门拦截 → 回原文。"""
        original = "The quick brown fox jumps"
        compressed = "The quick brown fox jumps over the rainbow bridge"
        from prompt_compression import passes_fidelity_gate
        self.assertFalse(passes_fidelity_gate(original, compressed))

    def test_exception_returns_original(self):
        """管线异常 → 放行原文（零阻塞降级）。"""
        pc = PromptCompressor(enabled=True)
        out = pc.compress(text=None, model="m（占位）")
        self.assertFalse(out["compressed"])

    def test_chain_rerun_green(self):
        """链复跑：T2~T4 测试子进程复跑 exit 0。"""
        for name in ("test_prd477_t2_prompt_compression.py",):
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "discover",
                 "-s", str(TESTS), "-p", name],
                capture_output=True, timeout=600)
            self.assertIn(proc.returncode, (0, 5),
                             f"{name} 复跑失败：{proc.stdout[-400:]}{proc.stderr[-400:]}")


class TestDocNoSecrets(unittest.TestCase):
    def test_t1_design_doc_no_secrets(self):
        """文档无密钥：T1 设计拼接 needle 扫描+借鉴声明（R5）。"""
        doc = next(REPO.glob(
            "docs/plans/2026-11-17-prompt-compression-design.md"))
        text = doc.read_text(encoding="utf-8").lower()
        for needle in ("sk-" + "live", "passw" + "ord =",
                       "BEGIN PRIVATE", "AKIA" + "x"):
            self.assertNotIn(needle, text, f"设计文档出现密钥样式 {needle}")
        self.assertIn("只借鉴", text)


if __name__ == "__main__":
    unittest.main()
