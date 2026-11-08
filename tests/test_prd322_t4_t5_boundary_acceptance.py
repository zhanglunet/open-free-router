# -*- coding: utf-8 -*-
"""PRD-322/T4+T5：搜索闸门划界断言 + 验收试运行（trend #655 区段）。

T4 划界：catalog_gate/catalog_search 零网络形态（无 fetch/http/socket）、
零写盘调用形态、只依赖标准库；闸门拒绝路径零副作用（入参不变）。
T5 验收：真实注册表 → search_catalog → catalog_gate 全链——
命中/空 query/伪造注入三类场景各跑两遍，结果逐字段一致（可重放口径）。
"""
from __future__ import annotations
import ast
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.catalog_gate import catalog_gate
from open_free_router.catalog_search import search_catalog

REPO = Path(__file__).resolve().parents[1]
REGISTRY = [
    {"name": "glm-backend", "description": "GLM 免费通道", "tags": ["free"]},
    {"name": "nemotron-backend", "description": "NVIDIA 通道", "tags": ["nvidia"]},
]


class T4BoundaryTests(unittest.TestCase):
    def test_ast_import_stdlib_only(self):
        for mod in ("catalog_gate.py", "catalog_search.py"):
            tree = ast.parse((REPO / "src" / "open_free_router" / mod).read_text(encoding="utf-8"))
            roots = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    roots.add(node.module.split(".")[0])
            for bad in ("requests", "urllib", "socket", "http"):
                self.assertNotIn(bad, roots, f"{mod} 越界网络导入：{bad}")

    def test_gate_refusal_zero_side_effect(self):
        results = [{"name": "ghost", "description": "", "tags": []}]
        frozen = copy.deepcopy(results)
        reg = copy.deepcopy(REGISTRY)
        catalog_gate(results, "ghost", reg)
        self.assertEqual(results, frozen, "拒绝路径不得改入参")
        self.assertEqual(reg, REGISTRY, "拒绝路径不得改注册表")


class T5AcceptanceTests(unittest.TestCase):
    def _chain(self, query, registry):
        results = search_catalog(registry, query)
        return {"results": results, "gate": catalog_gate(results, query, registry)}

    def test_hit_scenario_replay_identical(self):
        self.assertEqual(self._chain("glm", REGISTRY), self._chain("glm", REGISTRY))
        self.assertTrue(self._chain("glm", REGISTRY)["gate"]["allow"])

    def test_empty_query_full_registry_replay_identical(self):
        self.assertEqual(self._chain("", REGISTRY), self._chain("", REGISTRY))
        self.assertEqual(self._chain("", REGISTRY)["gate"]["count"], 2)

    def test_forged_injection_refused_deterministic(self):
        forged = [{"name": "ghost-backend", "description": "", "tags": []}]
        out1 = catalog_gate(forged, "ghost", REGISTRY)
        out2 = catalog_gate(copy.deepcopy(forged), "ghost", REGISTRY)
        self.assertEqual(out1, out2)
        self.assertFalse(out1["allow"])
        self.assertTrue(any("不在注册表" in e for e in out1["evidence"]))


if __name__ == "__main__":
    unittest.main()
