# -*- coding: utf-8 -*-
"""PRD-470/T4 测试（trend #2273 的 T4 环节）：统一模型分组划界断言。
运行：.venv/bin/python -m pytest tests/test_prd470_t4_model_groups_boundary.py -q
覆盖（T1 §4 边界）：routing/failover/probe/resilience/costing/executor 零导入
+freellmapi 零符号+标准库-only+AST 零网络+零权重词面+schema 封闭。零网络。
"""
import ast
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/open_free_router"))
from model_groups import (  # noqa: E402
    GROUPS_SCHEMA, GroupsError, FallbackProfiles, merge_groups)

PROVIDERS = {"groq": {"models": {"a": {"upstream_id": "a", "enabled": True}}},
             "ofr2": {"models": {"a": {"upstream_id": "a", "enabled": True}}}}


class TestBoundary(unittest.TestCase):
    def test_no_downstream_imports(self):
        """routing/failover/probe/resilience/costing/executor 零导入——边界只读不改。"""
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "model_groups.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for banned in ("routing", "failover", "probe", "resilience",
                       "costing", "executor", "translate"):
            self.assertNotIn(banned, imported, f"model_groups 导入 {banned}")

    def test_borrowed_zero_symbols(self):
        """借鉴仓（freellmapi，MIT 实测）零符号——只借思路。"""
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "model_groups.py").read_text(encoding="utf-8")
        names = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Name):
                names.add(node.id.lower())
            elif isinstance(node, ast.Attribute):
                names.add(node.attr.lower())
        for word in ("freellmapi", "freellm", "intelligencerank"):
            self.assertNotIn(word, names, word)

    def test_stdlib_only_and_ast_redline(self):
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "model_groups.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        allowed = {"__future__", "copy", "typing"}
        self.assertLessEqual(imported - allowed, set(), imported)
        self.assertFalse(imported & {"urllib", "requests", "socket", "http",
                                     "subprocess"}, imported)

    def test_schema_closed(self):
        self.assertEqual(GROUPS_SCHEMA, "model-groups/v1")


class TestBehaviorPins(unittest.TestCase):
    def test_no_weight_change(self):
        """非目标：不改路由评分权重。"""
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "model_groups.py").read_text(encoding="utf-8")
        for word in ("quality_weight", "cost_weight", "latency_weight"):
            self.assertNotIn(word, src, word)

    def test_merge_output_deterministic(self):
        out1 = merge_groups(PROVIDERS)
        out2 = merge_groups(PROVIDERS)
        self.assertEqual(json.dumps(out1, sort_keys=True),
                         json.dumps(out2, sort_keys=True))

    def test_profile_not_found_error_type(self):
        fp = FallbackProfiles({"default": ["x（占位）"]})
        with self.assertRaises(GroupsError):
            fp.resolve("ghost")

    def test_unknown_provider_skipped_silently(self):
        """分组只对已知提供方合并——未知提供方零导入零报错。"""
        extra = dict(PROVIDERS)
        extra["unknown-platform（占位）"] = {"models": {
            "a": {"upstream_id": "a", "enabled": True}}}
        out = merge_groups(extra)   # 不报错（与 T3 apply 不同——分组是读操作）
        self.assertGreaterEqual(len(out["groups"]), 1)


if __name__ == "__main__":
    unittest.main()
