# -*- coding: utf-8 -*-
"""PRD-294/T5 测试（trend #1475）：验收试运行——样例全流程回放 + 歧义/失败
演练 + 文档无密钥。

运行：.venv/bin/python -m pytest tests/test_prd294_t5_acceptance.py -q

验收标准（trend #1475）：**引用解析全链通过；红线断言全过；文档无密钥**。

回放：路由指令含 @provider-x（占位） → 解析（health 报告显形）→ 审计
留痕（checked_at 时点）→ 歧义/失败演练（NOT_FOUND/AMBIGUOUS/SYNTAX_
ERROR 全触发；不健康 down/degraded 解析成功报告不换路）→ 文档无密钥。
零网络；全合成占位。
"""
import ast
import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "open_free_router"))

from backend_ref import RefAudit, resolve  # noqa: E402

BACKENDS = [{"name": "provider-x（占位）", "backend_id": "provider-x",
             "models": ["model-x（占位）"], "byok": True}]


def health_fn(backend_id):
    return {"provider-x": "down"}.get(backend_id, "unknown")


class 全链回放Tests(unittest.TestCase):
    def test_full_chain_replay_with_health_report(self):
        """样例全流程：解析（health 显形）→ 审计留痕 → 决策面分离。"""
        audit = RefAudit()
        for i, raw in enumerate(("@provider-x（占位）",)):
            out = audit.resolve_logged(raw, BACKENDS, at=f"2026-11-04T09:0{i}:00Z",
                                       health_lookup=health_fn)
            self.assertFalse(out["refused"], raw)
        record = audit.records[0]
        self.assertEqual(record["health_status"], "down")          # 报告显形
        self.assertEqual(record["checked_at"], "2026-11-04T09:00:00Z")
        blob = json.dumps(audit.records, ensure_ascii=False)
        for hint in ("failover", "switch_to", "auto_route"):
            self.assertNotIn(hint, blob.lower())                   # 零换路字段

    def test_replay_deterministic(self):
        def run():
            audit = RefAudit()
            audit.resolve_logged("@provider-x（占位）", BACKENDS,
                                 at="2026-11-04T09:00:00Z", health_lookup=health_fn)
            return str(audit.records)
        self.assertEqual(run(), run())


class 歧义失败演练Tests(unittest.TestCase):
    def test_all_failures_block(self):
        """三码全触发（NOT_FOUND/AMBIGUOUS/SYNTAX_ERROR）。"""
        audit = RefAudit()
        for raw, want in (("@provider-y（占位）", "REF_NOT_FOUND"),
                          ("@provider-x（占位）#failover", "REF_SYNTAX_ERROR"),
                          ("@a,@b", "REF_SYNTAX_ERROR")):
            out = audit.resolve_logged(raw, BACKENDS, at="2026-11-04T09:00:00Z")
            self.assertTrue(out["refused"], raw)
            self.assertEqual(out["error_code"], want, raw)
        dup = BACKENDS + [{"name": "provider-x（占位）", "backend_id": "provider-x-2"}]
        out = audit.resolve_logged("@provider-x（占位）", dup, at="2026-11-04T09:01:00Z")
        self.assertEqual(out["error_code"], "REF_AMBIGUOUS")


class 文档无密钥Tests(unittest.TestCase):
    def test_design_doc_no_secrets(self):
        secret_re = re.compile(r"(sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{8,}|"
                               r"AKIA[0-9A-Z]{16}|password\s*[:=]\s*['\"][^'\"]{4,})",
                               re.IGNORECASE)
        docs = list((REPO / "docs" / "plans").glob("2026-11-04-prd-294-*.md"))
        self.assertTrue(docs, "T1 设计文档在位")
        for doc in docs:
            blob = doc.read_text(encoding="utf-8")
            self.assertFalse(secret_re.findall(blob), doc.name)
            for host in re.findall(r"https?://([a-z0-9.-]+)", blob, re.IGNORECASE):
                self.assertTrue(host.endswith(("github.com", "example.com",
                                               "example.invalid", "synthetic",
                                               "local")), f"{doc.name} {host}")

    def test_ast_zero_auto_switch_final(self):
        """红线终检：解析器零 failover/executor/hot_swap 导入（不自动换路）。"""
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
                self.assertNotIn(mod, banned)


if __name__ == "__main__":
    unittest.main()
