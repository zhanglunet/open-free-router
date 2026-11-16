# -*- coding: utf-8 -*-
"""PRD-471/T4 测试（trend #2278 的 T4 环节）：粘性会话划界断言。
运行：.venv/bin/python -m pytest tests/test_prd471_t4_sticky_boundary.py -q
覆盖（T1 §3/§5 边界）：routing/failover/probe/executor/translate/BYOS 零导入
+freellmapi 零符号+标准库-only+AST 零网络+交接零原文密钥+粘性立即失效+
schema 封闭。零网络；全合成占位。
"""
import ast
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/open_free_router"))
from sticky_sessions import (  # noqa: E402
    STICKY_WINDOW_SECONDS, StickyError, StickySessionManager, HandoffGenerator)

NOW = 1000000.0
STICKY = "groq/llama（占位）"

def _imports_of(path: Path) -> set:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


def _code_names(path: Path) -> set:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


MODULES = ["sticky_sessions.py"]
SRC_ROUTER = Path(__file__).resolve().parents[1] / "src/open_free_router"


class TestBoundary(unittest.TestCase):
    def test_no_downstream_imports(self):
        """routing/failover/probe/executor/translate/byos/quota 零导入——边界只读。"""
        for mod in MODULES:
            imported = set()
            for node in ast.walk(ast.parse(
                    (SRC_ROUTER / mod).read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            for banned in ("routing", "failover", "probe", "executor",
                           "translate", "byos", "quota", "model_groups"):
                self.assertNotIn(banned, imported, f"{mod} 导入 {banned}")

    def test_borrowed_zero_symbols(self):
        """借鉴仓（freellmapi，MIT 实测）零符号——只借思路。"""
        for mod in MODULES:
            names = {n.lower() for n in _code_names(SRC_ROUTER / mod)}
            for word in ("freellmapi", "freellm"):
                self.assertNotIn(word, names, f"{mod} 出现 {word}")

    def test_stdlib_only_and_ast_redline(self):
        allowed = {"__future__", "copy", "typing"}
        for mod in MODULES:
            imported = _imports_of(SRC_ROUTER / mod)
            self.assertLessEqual(imported - allowed, set(), mod)
            self.assertFalse(imported & {"urllib", "requests", "socket",
                                         "http", "subprocess"}, mod)

    def test_no_compression(self):
        """非目标：不做长对话压缩（freellmapi 有但本条不借鉴）。"""
        for mod in MODULES:
            names = {n.lower() for n in _code_names(SRC_ROUTER / mod)}
            for word in ("compress", "summarize_history"):
                self.assertNotIn(word, names, f"{mod} 出现 {word}")


class TestBehaviorPins(unittest.TestCase):
    def test_sticky_invalidates_immediately_on_unavailable(self):
        """粘性导致赖在坏模型上 → 模型不可用时粘性立即失效。"""
        led = StickySessionManager(now=NOW)
        led.bind(session_id="s-001", model=STICKY)
        out = led.resolve(session_id="s-001",
                          model_available_fn=lambda m: False)
        self.assertIsNone(out)

    def test_handoff_never_contains_original_text(self):
        """交接说明不含用户原文与密钥（T4/T5 钉值）。"""
        gen = HandoffGenerator()
        out = gen.generate(from_model=STICKY, to_model="deepseek（占位）",
                           summary="摘要（占位）",
                           key_topics=["topic1（占位）", "topic2（占位）"],
                           turn_count=3)
        dumped = json.dumps(out).lower()
        for word in ("api_key", "password", "sk-", "secret"):
            self.assertNotIn(word, dumped, word)

    def test_sticky_window_closed(self):
        self.assertEqual(STICKY_WINDOW_SECONDS, 1800)

    def test_error_type_closed(self):
        self.assertTrue(issubclass(StickyError, ValueError))


if __name__ == "__main__":
    unittest.main()
