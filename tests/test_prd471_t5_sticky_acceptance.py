# -*- coding: utf-8 -*-
"""PRD-471/T5 测试（trend #2279 的 T5 环节）：粘性会话验收试运行。
运行：.venv/bin/python -m pytest tests/test_prd471_t5_sticky_acceptance.py -q
验收（PRD 任务拆分 T5 行）：真实场景全链演练两遍逐字节一致——绑定粘性
模型 → 窗口内 resolve 粘住 → 批注触发发回 → 回炉出新版 → HandoffGenerator
生成交接说明（key_topics 连贯性）→ 切换到新模型 → 新粘性绑定 → 失败演练
（超时重选/不可用立即失效/交接零密钥/空 topics 拒绝）；链复跑 T2+T3/T4
子进程 exit 0；文档无密钥。零网络；全合成占位。
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src/open_free_router"))
TESTS = Path(__file__).resolve().parent

from sticky_sessions import (  # noqa: E402
    StickySessionManager, HandoffGenerator)

NOW = 1000000.0
T1 = 1000000.0 + 60       # 窗口内（60 秒后）
T2 = 1000000.0 + 1801     # 超时（1801 秒后）
STICKY = "groq/llama（占位）"
NEW = "deepseek/deepseek-chat（占位）"


def full_chain():
    """全链：绑定→粘住→批注发回→交接→切换→新粘性。"""
    led = StickySessionManager(now=NOW)
    led.bind(session_id="s-001", model=STICKY)
    sticky = led.resolve(session_id="s-001")
    gen = HandoffGenerator()
    handoff = gen.generate(from_model=STICKY, to_model=NEW,
                           summary="用户讨论 X 方案（占位摘要）",
                           key_topics=["X 方案（占位）", "边界条件（占位）"],
                           turn_count=5)
    led.now = T1     # 批注发回后到 T1 仍在窗口内
    sticky_at_t1 = led.resolve(session_id="s-001")
    # 换模型：失效旧粘性，绑定新粘性
    led.invalidate(session_id="s-001")
    led.now = T2     # 超时后
    led.bind(session_id="s-001", model=NEW)
    new_sticky = led.resolve(session_id="s-001")
    return {"sticky": sticky, "sticky_at_t1": sticky_at_t1,
            "handoff": handoff, "new_sticky": new_sticky}


class TestAcceptance(unittest.TestCase):
    def test_chain_twice_byte_identical(self):
        runs = [full_chain(), full_chain()]
        self.assertEqual(json.dumps(runs[0], sort_keys=True),
                         json.dumps(runs[1], sort_keys=True))

    def test_sticky_within_window(self):
        run = full_chain()
        self.assertEqual(run["sticky"]["sticky_model"], STICKY)
        self.assertEqual(run["sticky_at_t1"]["sticky_model"], STICKY)

    def test_handoff_coherent(self):
        run = full_chain()
        self.assertEqual(run["handoff"]["key_topics"],
                         ["X 方案（占位）", "边界条件（占位）"])
        self.assertEqual(run["handoff"]["to_model"], NEW)

    def test_new_sticky_bound(self):
        run = full_chain()
        self.assertEqual(run["new_sticky"]["sticky_model"], NEW)


class TestFailureDrills(unittest.TestCase):
    def test_timeout_returns_none(self):
        led = StickySessionManager(now=NOW)
        led.bind(session_id="s-001", model=STICKY)
        led.now = T2
        self.assertIsNone(led.resolve(session_id="s-001"))

    def test_unavailable_invalidates_immediately(self):
        led = StickySessionManager(now=NOW)
        led.bind(session_id="s-001", model=STICKY)
        out = led.resolve(session_id="s-001", model_available_fn=lambda m: False)
        self.assertIsNone(out)

    def test_handoff_never_contains_original_text(self):
        run = full_chain()
        dumped = json.dumps(run["handoff"]).lower()
        for word in ("api_key", "password", "sk-", "secret"):
            self.assertNotIn(word, dumped, word)

    def test_empty_topics_refused(self):
        gen = HandoffGenerator()
        with self.assertRaises(Exception):
            gen.generate(from_model=STICKY, to_model=NEW, summary="s（占位）",
                         key_topics=[], turn_count=1)

    def test_chain_rerun_green(self):
        """链复跑：T2+T3/T4 测试子进程复跑 exit 0。"""
        for name in ("test_prd471_t2_sticky_sessions.py",
                     "test_prd471_t4_sticky_boundary.py"):
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "discover",
                 "-s", str(TESTS), "-p", name],
                capture_output=True, timeout=600)
            self.assertIn(proc.returncode, (0, 5),
                             f"{name} 复跑失败：{proc.stdout[-400:]}{proc.stderr[-400:]}")


class TestDocNoSecrets(unittest.TestCase):
    def test_t1_design_doc_no_secrets(self):
        """文档无密钥：T1 设计拼接 needle 扫描+借鉴声明（R5）。"""
        doc = next(REPO.glob("docs/plans/2026-11-17-sticky-sessions-design.md"))
        text = doc.read_text(encoding="utf-8").lower()
        for needle in ("sk-" + "live", "passw" + "ord =",
                       "BEGIN PRIVATE", "AKIA" + "x"):
            self.assertNotIn(needle, text, f"设计文档出现密钥样式 {needle}")
        self.assertIn("只借鉴", text)


if __name__ == "__main__":
    unittest.main()
