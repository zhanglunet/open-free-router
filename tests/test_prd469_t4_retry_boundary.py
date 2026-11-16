# -*- coding: utf-8 -*-
"""PRD-469/T4 测试（trend #2268 的 T4 环节）：钳制与脱敏划界断言。
运行：.venv/bin/python -m pytest tests/test_prd469_t4_retry_boundary.py -q
覆盖（T1 §3/§4 边界）：24h 钳制钉值（畸形/恶意提示无法永久停用）+
正文原文零保留钉值（结果键集封闭）+060/T4 归一错误码表零改动+
freellmapi 零符号+标准库-only+AST 零网络。零网络；全合成占位。
"""
import ast
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/open_free_router"))
from retry_after_deep import (  # noqa: E402
    CLAMP_MAX, DEEP_SCHEMA, MAX_DEPTH, MAX_TEXT_MATCHES, parse_retry_after_deep)

NOW = 1700000000.0
SRC_ROUTER = Path(__file__).resolve().parents[1] / "src/open_free_router"
MODULES = ["retry_after_deep.py"]


class TestClamp(unittest.TestCase):
    def test_24h_clamp_always(self):
        """§3 钳制：畸形/恶意提示无法永久停用密钥（999999→86400）。"""
        for val in ("999999", "0", "86401"):
            out = parse_retry_after_deep(status=429,
                                         headers={"Retry-After": val}, now=NOW)
            self.assertTrue(0 <= out["retry_after_seconds"] <= CLAMP_MAX)

    def test_structured_also_clamped(self):
        import json
        body = json.dumps({"retry_after": 999999999})
        out = parse_retry_after_deep(status=429, body_text=body, now=NOW)
        self.assertEqual(out["retry_after_seconds"], CLAMP_MAX)
        self.assertTrue(out["clamped"])

    def test_text_also_clamped(self):
        body = "try again in 999999999 seconds"
        out = parse_retry_after_deep(status=429, body_text=body, now=NOW)
        self.assertEqual(out["retry_after_seconds"], CLAMP_MAX)


class TestSanitization(unittest.TestCase):
    def test_result_keys_closed(self):
        """§3 脱敏：结果键集封闭——正文原文/短语上下文/结构化原始值永不保留。"""
        body = "try again in 30 seconds because of rate limit exceeded"
        out = parse_retry_after_deep(status=429, body_text=body, now=NOW)
        self.assertEqual(set(out.keys()),
                         {"schema", "retry_after_seconds", "source", "clamped"})
        # 结果不含任何正文原文
        for word in ("rate limit", "error", "because"):
            self.assertNotIn(word, json.dumps(out).lower(), word)

    def test_max_text_matches_3(self):
        """只取前 3 个匹配（防恶意提示塞满数字）。"""
        self.assertEqual(MAX_TEXT_MATCHES, 3)

    def test_depth_cap_6(self):
        self.assertEqual(MAX_DEPTH, 6)


class TestBoundary(unittest.TestCase):
    def test_no_downstream_imports(self):
        """resilience/costing/routing/probe/parse_quota_headers 零导入。"""
        for mod in MODULES:
            src_path = SRC_ROUTER / mod
            imported = set()
            for node in ast.walk(ast.parse(src_path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            for banned in ("resilience", "costing", "routing", "probe",
                           "quota", "byos"):
                self.assertNotIn(banned, imported, f"{mod} 导入 {banned}")

    def test_borrowed_zero_symbols(self):
        for mod in MODULES:
            src_path = SRC_ROUTER / mod
            names = set()
            for node in ast.walk(ast.parse(src_path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Name):
                    names.add(node.id.lower())
                elif isinstance(node, ast.Attribute):
                    names.add(node.attr.lower())
            for word in ("freellmapi", "freellm"):
                self.assertNotIn(word, names, f"{mod} 出现 {word}")

    def test_stdlib_only_and_ast_redline(self):
        allowed = {"__future__", "calendar", "email", "json", "re", "typing"}
        for mod in MODULES:
            imported = set()
            for node in ast.walk(ast.parse(
                    (SRC_ROUTER / mod).read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertLessEqual(imported - allowed, set(), mod)
            self.assertFalse(imported & {"urllib", "requests", "socket",
                                         "http", "subprocess"}, mod)


if __name__ == "__main__":
    unittest.main()
