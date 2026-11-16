# -*- coding: utf-8 -*-
"""PRD-469/T2+T3 测试（trend #2266/#2267 的 T2+T3 环节）：Retry-After 深度解析。
运行：.venv/bin/python -m pytest tests/test_prd469_t2_retry_parse.py -q
覆盖（T1 §2/§3）：响应头两形式/正文结构化深度封顶/锚定短语/合并优先级/
24h 钳制/正文原文零保留/词表封闭/AST 零网络。零网络；全合成占位。
"""
import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/open_free_router"))
from retry_after_deep import (  # noqa: E402
    CLAMP_MAX, DEEP_SCHEMA, MAX_DEPTH, parse_retry_after_deep,
    parse_retry_after_header,
)

NOW = 1700000000.0


class TestHeader(unittest.TestCase):
    def test_delta_seconds(self):
        out = parse_retry_after_deep(status=429,
                                     headers={"Retry-After": "120"}, now=NOW)
        self.assertEqual((out["retry_after_seconds"], out["source"]),
                         (120, "header"))

    def test_http_date(self):
        import email.utils
        future = email.utils.formatdate(NOW + 300, usegmt=True)
        out = parse_retry_after_deep(status=429,
                                     headers={"Retry-After": future}, now=NOW)
        self.assertEqual(out["retry_after_seconds"], 300)

    def test_shared_parser_called_once(self):
        self.assertEqual(parse_retry_after_header("90", NOW), 90.0)
        self.assertIsNone(parse_retry_after_header("", NOW))


class TestStructured(unittest.TestCase):
    def test_nested_field_found(self):
        body = json_dumps({"error": {"details": {"retry_after": 120}}})
        out = parse_retry_after_deep(status=429, body_text=body, now=NOW)
        self.assertEqual((out["retry_after_seconds"], out["source"]),
                         (120, "structured"))

    def test_depth_cap_6(self):
        body = json_dumps(nest(8, {"retry_after": 120}))
        out = parse_retry_after_deep(status=429, body_text=body, now=NOW)
        self.assertIsNone(out)               # 超深不进

    def test_non_numeric_skipped(self):
        body = json_dumps({"retry_after": "not-a-number（占位）"})
        out = parse_retry_after_deep(status=429, body_text=body, now=NOW)
        self.assertIsNone(out)


class TestText(unittest.TestCase):
    def test_anchored_phrases(self):
        for text, expected in [
            ("Please try again in 30 seconds", 30.0),
            ("try again in 2 minutes", 120.0),
            ("retry after 45 seconds", 45.0),
            ("wait 10 seconds and try again", 10.0),
        ]:
            out = parse_retry_after_deep(status=429, body_text=text, now=NOW)
            self.assertEqual(out["retry_after_seconds"], expected, text)
            self.assertEqual(out["source"], "text")

    def test_unrelated_numbers_not_matched(self):
        body = "The model has 100 billion parameters and 42 layers"
        out = parse_retry_after_deep(status=429, body_text=body, now=NOW)
        self.assertIsNone(out)


class TestClampAndMerge(unittest.TestCase):
    def test_clamp_24h(self):
        out = parse_retry_after_deep(status=429,
                                     headers={"Retry-After": "999999"}, now=NOW)
        self.assertEqual(out["retry_after_seconds"], 86400)
        self.assertTrue(out["clamped"])

    def test_negative_clamped_to_zero(self):
        out = parse_retry_after_deep(status=429,
                                     headers={"Retry-After": "-5"}, now=NOW)
        self.assertEqual(out["retry_after_seconds"], 0)

    def test_header_wins_over_body(self):
        body = json_dumps({"retry_after": 120})
        out = parse_retry_after_deep(status=429, headers={"Retry-After": "60"},
                                     body_text=body, now=NOW)
        self.assertEqual((out["retry_after_seconds"], out["source"]),
                         (60, "header"))

    def test_structured_wins_over_text(self):
        body = json_dumps({"retry_after": 60, "message": "try again in 300 seconds（占位）"})
        out = parse_retry_after_deep(status=429, body_text=body, now=NOW)
        self.assertEqual(out["source"], "structured")

    def test_no_sources_returns_none(self):
        self.assertIsNone(parse_retry_after_deep(status=429, now=NOW))
        self.assertIsNone(parse_retry_after_deep(status=429,
                                                 body_text="no retry info",
                                                 now=NOW))


class TestRedline(unittest.TestCase):
    def test_body_text_never_in_result(self):
        """§3 脱敏：结果只含数字+来源，正文原文从不保留。"""
        body = "try again in 30 seconds with error message body content"
        out = parse_retry_after_deep(status=429, body_text=body, now=NOW)
        self.assertEqual(set(out.keys()),
                         {"schema", "retry_after_seconds", "source", "clamped"})

    def test_ast_zero_network(self):
        banned = {"urllib", "requests", "socket", "http", "subprocess"}
        src = (Path(__file__).resolve().parents[1] / "src/open_free_router"
               / "retry_after_deep.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & banned, imported & banned)

    def test_schema_closed(self):
        self.assertEqual(DEEP_SCHEMA, "retry-after-deep/v1")
        self.assertEqual(CLAMP_MAX, 86400)
        self.assertEqual(MAX_DEPTH, 6)


def json_dumps(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)


def nest(depth, leaf):
    result = leaf
    for _ in range(depth):
        result = {"inner": result}
    return result


if __name__ == "__main__":
    unittest.main()
