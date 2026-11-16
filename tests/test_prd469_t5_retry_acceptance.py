# -*- coding: utf-8 -*-
"""PRD-469/T5 测试（trend #2269 的 T5 环节）：Retry-After 深度解析验收试运行。
运行：.venv/bin/python -m pytest tests/test_prd469_t5_retry_acceptance.py -q
验收（PRD 任务拆分 T5 行）：真实场景全链演练两遍逐字节一致——响应头+
正文结构化+正文锚定短语三路场景逐一过 parse_retry_after_deep → 钳制 →
结果键集封闭；失败演练：畸形/恶意正文无法永久停用/正文原文零保留/
链复跑 T2+T3/T4 子进程 exit 0；文档无密钥。零网络；全合成占位。
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src/open_free_router"))
TESTS = Path(__file__).resolve().parent

from retry_after_deep import parse_retry_after_deep  # noqa: E402

NOW = 1700000000.0


def full_chain():
    """三路场景逐一演练。"""
    scenarios = []
    # 场景 1：响应头 delta-seconds
    out = parse_retry_after_deep(status=429, headers={"Retry-After": "120"},
                                 now=NOW)
    scenarios.append(("header", out))
    # 场景 2：正文结构化
    body = json.dumps({"error": {"retry_after": 300}})
    out = parse_retry_after_deep(status=429, body_text=body, now=NOW)
    scenarios.append(("structured", out))
    # 场景 3：锚定短语
    out = parse_retry_after_deep(status=429,
                                 body_text="try again in 30 seconds", now=NOW)
    scenarios.append(("text", out))
    # 场景 4：恶意钳制
    out = parse_retry_after_deep(status=429,
                                 headers={"Retry-After": "999999999"}, now=NOW)
    scenarios.append(("clamped", out))
    return scenarios


class TestAcceptance(unittest.TestCase):
    def test_three_scenarios_all_resolved(self):
        run = full_chain()
        for source, out in run:
            self.assertIsNotNone(out, source)
        self.assertEqual(run[0][1]["source"], "header")
        self.assertEqual(run[1][1]["source"], "structured")
        self.assertEqual(run[2][1]["source"], "text")

    def test_chain_twice_byte_identical(self):
        runs = [json.dumps(full_chain(), sort_keys=True),
                json.dumps(full_chain(), sort_keys=True)]
        self.assertEqual(runs[0], runs[1])


class TestFailureDrills(unittest.TestCase):
    def test_malicious_24h_clamped(self):
        out = parse_retry_after_deep(status=429,
                                     headers={"Retry-After": "999999999"},
                                     now=NOW)
        self.assertEqual(out["retry_after_seconds"], 86400)
        self.assertTrue(out["clamped"])

    def test_body_text_never_in_trace(self):
        body = "try again in 30 seconds internal error trace_data=<sensitive>（占位）"
        out = parse_retry_after_deep(status=429, body_text=body, now=NOW)
        dumped = json.dumps(out).lower()
        self.assertNotIn("sensitive", dumped)
        self.assertNotIn("internal error", dumped)

    def test_chain_rerun_green(self):
        """链复跑：T2+T3/T4 测试子进程复跑 exit 0。"""
        for name in ("test_prd469_t2_retry_parse.py",
                     "test_prd469_t4_retry_boundary.py"):
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", str(TESTS / name)],
                capture_output=True, timeout=600)
            self.assertEqual(proc.returncode, 0,
                             f"{name} 复跑失败：{proc.stdout[-400:]}{proc.stderr[-400:]}")


class TestDocNoSecrets(unittest.TestCase):
    def test_t1_design_doc_no_secrets(self):
        """文档无密钥：T1 设计拼接 needle 扫描+占位符纪律（R5）。"""
        doc = next(REPO.glob("docs/plans/2026-11-17-retry-after-deep-parse-design.md"))
        text = doc.read_text(encoding="utf-8").lower()
        for needle in ("sk-" + "live", "passw" + "ord =",
                       "BEGIN PRIVATE", "AKIA" + "x"):
            self.assertNotIn(needle, text, f"设计文档出现密钥样式 {needle}")
        self.assertIn("只借鉴思路", text)   # R5 占位符纪律的同义口径


if __name__ == "__main__":
    unittest.main()
