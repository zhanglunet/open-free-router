# -*- coding: utf-8 -*-
"""PRD-471/T2+T3 测试（trend #2276/#2277 的 T2+T3 环节）：粘性会话+交接生成。
运行：.venv/bin/python -m pytest tests/test_prd471_t2_sticky_sessions.py -q
覆盖（T1 §1/§2）：30 分钟内同 session 同模型/超时重选/不可用立即失效/
交接说明结构化/不含敏感原文/话题连贯性 key_topics/AST 零网络。零网络。
"""
import ast
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/open_free_router"))
from sticky_sessions import (  # noqa: E402
    STICKY_SCHEMA, STICKY_WINDOW_SECONDS, StickyError, StickySessionManager,
    HandoffGenerator,
)

NOW = 1000000.0
STICKY_MODEL = "groq/llama-3.3-70b（占位）"
NEW_MODEL = "deepseek/deepseek-chat（占位）"


class TestStickySessions(unittest.TestCase):
    def test_bind_and_resolve_within_window(self):
        """30 分钟内同 session 不换模型。"""
        led = StickySessionManager(now=NOW)
        led.bind(session_id="s-001", model=STICKY_MODEL)
        out = led.resolve(session_id="s-001")
        self.assertIsNotNone(out)
        self.assertEqual(out["sticky_model"], STICKY_MODEL)

    def test_timeout_returns_none(self):
        """超时后正常重选（路由器全量打分）。"""
        led = StickySessionManager(now=NOW)
        led.bind(session_id="s-001", model=STICKY_MODEL)
        led.now = NOW + STICKY_WINDOW_SECONDS + 1
        self.assertIsNone(led.resolve(session_id="s-001"))

    def test_model_unavailable_invalidates_immediately(self):
        """模型不可用时粘性立即失效（安全优先于粘性）。"""
        led = StickySessionManager(now=NOW)
        led.bind(session_id="s-001", model=STICKY_MODEL)
        out = led.resolve(session_id="s-001",
                          model_available_fn=lambda m: m != STICKY_MODEL)
        self.assertIsNone(out)      # 不可用 → 失效
        self.assertNotIn("s-001", led.sessions)

    def test_sticky_window_is_30min(self):
        self.assertEqual(STICKY_WINDOW_SECONDS, 1800)


class TestHandoffGenerator(unittest.TestCase):
    def test_generate_structured(self):
        gen = HandoffGenerator()
        out = gen.generate(from_model=STICKY_MODEL, to_model=NEW_MODEL,
                           summary="用户讨论 X 方案（占位摘要）",
                           key_topics=["X（占位）", "Y（占位）"],
                           turn_count=5)
        self.assertEqual(out["schema"], "handoff/v1")
        self.assertEqual(out["from_model"], STICKY_MODEL)
        self.assertEqual(out["to_model"], NEW_MODEL)
        self.assertEqual(out["key_topics"], ["X（占位）", "Y（占位）"])

    def test_no_secret_material_in_handoff(self):
        """交接说明不含用户原文与密钥（T4/T5 钉值）。"""
        gen = HandoffGenerator()
        out = gen.generate(from_model=STICKY_MODEL, to_model=NEW_MODEL,
                           summary="摘要（占位）", key_topics=["topic（占位）"],
                           turn_count=3)
        dumped = json.dumps(out).lower()
        for word in ("api_key", "password", "secret", "sk-"):
            self.assertNotIn(word, dumped, word)

    def test_key_topics_required_for_coherence(self):
        """话题连贯性：key_topics 至少一条。"""
        gen = HandoffGenerator()
        with self.assertRaisesRegex(StickyError, "STICKY_INCOMPLETE"):
            gen.generate(from_model=STICKY_MODEL, to_model=NEW_MODEL,
                         summary="s（占位）", key_topics=[], turn_count=3)

    def test_empty_summary_refused(self):
        gen = HandoffGenerator()
        with self.assertRaisesRegex(StickyError, "STICKY_INCOMPLETE"):
            gen.generate(from_model=STICKY_MODEL, to_model=NEW_MODEL,
                         summary="", key_topics=["t（占位）"], turn_count=1)


class TestRedline(unittest.TestCase):
    def test_ast_zero_network(self):
        banned = {"urllib", "requests", "socket", "http", "subprocess"}
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "sticky_sessions.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & banned, imported & banned)

    def test_schema_closed(self):
        self.assertEqual(STICKY_SCHEMA, "sticky-session/v1")
        self.assertIn("handoff/v1",
                      (Path(__file__).resolve().parents[1] / "src"
                       / "open_free_router" / "sticky_sessions.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
