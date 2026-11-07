# -*- coding: utf-8 -*-
"""PRD-294/T3 测试（trend #1473）：不健康先报状态，不自动换路闸门。

运行：.venv/bin/python -m pytest tests/test_prd294_t3_unhealthy.py -q

覆盖（T1 设计 §5，验收口径「不健康先报状态不自动换路 / 学习网关只推荐不
自动执行」）：① UNHEALTHY_REPORTED 语义——degraded/down 解析照常成功、
状态显形、报告不拦截；unknown 合法；② 决策面分离—— unhealthy 时不产
任何换路建议字段（产物键白名单机判）；③ AST 全链红线——解析模块与闸门
模块零 failover/executor/hot_swap/auto_rollback/learning_confirm 写导入；
④ 错误码封闭穷举 + 审计带 checked_at 时点快照。
零网络；全合成占位。
"""
import ast
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "open_free_router"))

from backend_ref import (  # noqa: E402
    ERROR_CODES,
    RefAudit,
    RefSyntaxError,
    resolve,
)

BACKENDS = [{"name": "provider-x（占位）", "backend_id": "provider-x",
             "models": ["model-x（占位）"], "byok": True}]

ALL_INPUTS = ["@provider-x（占位）", "@provider-y（占位）",
              "@provider-x（占位）#failover", "@@双写", "@a,@b"]


def health_fn(backend_id):
    return {"provider-x": "down"}.get(backend_id, "unknown")


class 不健康报告Tests(unittest.TestCase):
    def test_down_backend_resolves_with_report_not_block(self):
        """UNHEALTHY_REPORTED：down 解析照常成功，状态显形交人工决策。"""
        out = resolve("@provider-x（占位）", BACKENDS, health_lookup=health_fn)
        self.assertEqual(out["resolution"]["status"], "resolved")   # 不拦截
        self.assertEqual(out["health"]["status"], "down")           # 显形
        self.assertIsNone(out["resolution"].get("error_code"))

    def test_product_key_whitelist_no_switch_advice(self):
        """决策面分离：产物键白名单——零换路建议/零自动执行字段。"""
        out = resolve("@provider-x（占位）", BACKENDS, health_lookup=health_fn)
        allowed = {"schema", "raw", "kind", "backend_id", "health",
                   "meta_summary", "resolution"}
        self.assertTrue(set(out) <= allowed, set(out) - allowed)
        blob = json.dumps(out, ensure_ascii=False)
        for hint in ("failover", "switch_to", "auto_route", "recommended"):
            self.assertNotIn(hint, blob.lower(), hint)

    def test_unknown_state_legal(self):
        out = resolve("@provider-x（占位）", BACKENDS, health_lookup=lambda b: None)
        self.assertEqual(out["health"]["status"], "unknown")        # 显形不猜测


class AST全链红线Tests(unittest.TestCase):
    def test_no_auto_switch_imports_across_modules(self):
        """解析模块 AST 级零换路/零执行导入（R1：换路物理不可达）。"""
        banned = {"failover", "executor", "hot_swap", "auto_rollback",
                  "learning_confirm", "gateway_recommend", "urllib", "subprocess"}
        tree = ast.parse((REPO / "src" / "open_free_router" / "backend_ref.py")
                         .read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module.split(".")[0]]
            for mod in mods:
                self.assertNotIn(mod, banned, f"解析器导入了 {mod}")


class 词表封闭Tests(unittest.TestCase):
    def test_exhaustive_inputs_landed_in_vocabulary(self):
        audit = RefAudit()
        seen = set()
        for raw in ALL_INPUTS:
            try:
                out = resolve(raw, BACKENDS, health_lookup=health_fn)
            except RefSyntaxError:
                seen.add("REF_SYNTAX_ERROR")
                continue
            status = out["resolution"]["status"]
            if status == "refused":
                code = out["resolution"]["error_code"]
                self.assertIn(code, ERROR_CODES, raw)
                seen.add(code)
        self.assertEqual(seen, {"REF_NOT_FOUND", "REF_SYNTAX_ERROR"})


class 审计时点Tests(unittest.TestCase):
    def test_health_snapshot_with_checked_at(self):
        audit = RefAudit()
        audit.resolve_logged("@provider-x（占位）", BACKENDS, at="2026-11-04T10:00:00Z",
                             health_lookup=health_fn)
        record = audit.records[0]
        self.assertEqual(record["health_status"], "down")
        self.assertEqual(record["checked_at"], "2026-11-04T10:00:00Z")  # 回放可区分当时/现在


if __name__ == "__main__":
    unittest.main()
