# -*- coding: utf-8 -*-
"""PRD-294/T2 测试（trend #1472）：@后端引用解析最小实现。

运行：.venv/bin/python -m pytest tests/test_prd294_t2_backend_ref.py -q

覆盖（T1 设计 §3/§4/§5/§6，验收「样例可解析到确定 id，审计可查，有测试」）：
  parse 单形态与禁止形态（策略后缀/权重修饰/组合引用/双写）；resolve 精确
  匹配→确定 id、候选 0/多裁决、health 报告字段（只读注入零探活、unknown
  合法显形、非词表值不透传）；二值返回；模块零换路导入 AST 红线（R1）；
  RefAudit append-only 带 checked_at 时点快照。
零网络；全合成占位。
"""
import ast
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "open_free_router"))

from backend_ref import (  # noqa: E402
    HEALTH_STATES,
    RefAudit,
    RefSyntaxError,
    parse,
    resolve,
)

BACKENDS = [{"name": "provider-x（占位）", "backend_id": "provider-x",
             "models": ["model-x（占位）"], "byok": True}]


def health_fn(backend_id):
    return {"provider-x": "degraded"}.get(backend_id, "unknown")


class TestParse(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(parse("@provider-x（占位）"), "provider-x（占位）")

    def test_prohibited_forms(self):
        for bad in ("@provider-x（占位）#failover", "@provider-x（占位）+2",
                    "@a,@b", "@@双写", "@provider x（占位）"):
            with self.assertRaises(RefSyntaxError):
                parse(bad)


class TestResolve(unittest.TestCase):
    def test_resolves_to_id_plus_health_report(self):
        out = resolve("@provider-x（占位）", BACKENDS, health_lookup=health_fn)
        self.assertEqual(out["resolution"]["status"], "resolved")
        self.assertEqual(out["backend_id"], "provider-x")
        self.assertEqual(out["health"]["status"], "degraded")     # 报告字段
        self.assertEqual(out["meta_summary"]["byok"], True)

    def test_health_readonly_no_probe(self):
        """health 只读注入：lookup 未覆盖的后端 → unknown 显形（不探活不猜）。"""
        out = resolve("@provider-x（占位）", BACKENDS, health_lookup=lambda b: None)
        self.assertEqual(out["health"]["status"], "unknown")
        out2 = resolve("@provider-x（占位）", BACKENDS,
                       health_lookup=lambda b: "garbage")
        self.assertEqual(out2["health"]["status"], "unknown")     # 非词表值不透传

    def test_health_vocabulary_closed(self):
        self.assertEqual(set(HEALTH_STATES),
                         {"healthy", "degraded", "down", "unknown"})

    def test_not_found_and_ambiguous(self):
        out = resolve("@provider-y（占位）", BACKENDS)
        self.assertEqual(out["resolution"]["error_code"], "REF_NOT_FOUND")
        dup = BACKENDS + [{"name": "provider-x（占位）", "backend_id": "provider-x-2"}]
        out2 = resolve("@provider-x（占位）", dup)
        self.assertEqual(out2["resolution"]["error_code"], "REF_AMBIGUOUS")

    def test_ast_redline_no_auto_switch(self):
        """R1：解析器模块零 failover/executor/hot_swap 导入（换路物理不可达）。"""
        banned = {"failover", "executor", "hot_swap", "auto_rollback"}
        tree = ast.parse((REPO / "src" / "open_free_router" / "backend_ref.py")
                         .read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module.split(".")[0]]
            for mod in mods:
                self.assertNotIn(mod, banned, f"解析器导入了换路模块 {mod}")


class TestRefAudit(unittest.TestCase):
    def test_success_logged_with_health_snapshot(self):
        audit = RefAudit()
        out = audit.resolve_logged("@provider-x（占位）", BACKENDS,
                                   at="2026-11-04T09:00:00Z", health_lookup=health_fn)
        self.assertFalse(out["refused"])
        record = audit.records[0]
        self.assertEqual(record["health_status"], "degraded")
        self.assertEqual(record["checked_at"], "2026-11-04T09:00:00Z")  # 时点快照

    def test_refused_and_syntax_logged(self):
        audit = RefAudit()
        audit.resolve_logged("@provider-y（占位）", BACKENDS, at="2026-11-04T09:01:00Z")
        audit.resolve_logged("@provider-x（占位）#failover", BACKENDS,
                             at="2026-11-04T09:02:00Z")
        self.assertEqual([r["error_code"] for r in audit.records],
                         ["REF_NOT_FOUND", "REF_SYNTAX_ERROR"])

    def test_append_only(self):
        audit = RefAudit()
        audit.resolve_logged("@provider-x（占位）", BACKENDS, at="2026-11-04T09:00:00Z")
        before = list(audit.records)
        audit.resolve_logged("@provider-y（占位）", BACKENDS, at="2026-11-04T09:01:00Z")
        self.assertEqual(audit.records[:len(before)], before)


if __name__ == "__main__":
    unittest.main()
