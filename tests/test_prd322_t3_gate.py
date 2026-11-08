# -*- coding: utf-8 -*-
"""PRD-322/T3 后端注册表搜索闸门测试（trend #655 区段）。"""
import unittest
from pathlib import Path

from open_free_router.catalog_gate import catalog_gate, verify_search_result
from open_free_router.catalog_search import search_catalog

REGISTRY = [
    {"name": "glm-backend", "description": "GLM 免费通道", "tags": ["free", "glm"]},
    {"name": "nemotron-backend", "description": "NVIDIA 通道", "tags": ["nvidia"]},
    {"name": "kimi-backend", "description": "Kimi 长文本", "tags": ["long-context"]},
]


class GateTests(unittest.TestCase):
    def test_real_search_output_passes(self):
        # 真实 T2 搜索产物必须过闸（不变量成立）
        results = search_catalog(REGISTRY, "glm")
        out = catalog_gate(results, "glm", REGISTRY)
        self.assertTrue(out["allow"])
        self.assertEqual(out["count"], 1)

    def test_empty_query_full_registry_passes(self):
        results = search_catalog(REGISTRY, "")
        out = catalog_gate(results, "", REGISTRY)
        self.assertTrue(out["allow"])
        self.assertEqual(out["count"], 3)

    def test_forged_entry_refused(self):
        # 伪造条目（不在注册表）→ 逐条点名
        forged = [{"name": "ghost-backend", "description": "x", "tags": []}]
        out = catalog_gate(forged, "ghost", REGISTRY)
        self.assertFalse(out["allow"])
        self.assertTrue(any("不在注册表" in e for e in out["evidence"]))

    def test_non_matching_hit_refused(self):
        # 结果与查询谓词不符（搜索实现回归的护栏）
        out = catalog_gate([REGISTRY[0]], "kimi", REGISTRY)
        self.assertFalse(out["allow"])
        self.assertTrue(any("不满足匹配谓词" in e for e in out["evidence"]))

    def test_mixed_valid_and_forged_refused(self):
        mixed = [REGISTRY[0], {"name": "fake"}]
        out = catalog_gate(mixed, "glm", REGISTRY)
        self.assertFalse(out["allow"])
        self.assertEqual(len(out["evidence"]), 1)

    def test_non_list_refused(self):
        out = catalog_gate("not-a-list", "glm", REGISTRY)
        self.assertFalse(out["allow"])

    def test_verify_pure_deterministic(self):
        results = search_catalog(REGISTRY, "backend")
        self.assertEqual(verify_search_result(results, "backend", REGISTRY),
                         verify_search_result(results, "backend", REGISTRY))


if __name__ == "__main__":
    unittest.main()
