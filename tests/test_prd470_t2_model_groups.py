# -*- coding: utf-8 -*-
"""PRD-470/T2+T3 测试（trend #2271/#2272 的 T2+T3 环节）：分组合并+命名配置档。
运行：.venv/bin/python -m pytest tests/test_prd470_t2_model_groups.py -q
覆盖（T1 §1/§2）：分组合并无重复/组内 priority 递增/配置档切换/按请求
指定/默认档行为不变/PROFILE_NOT_FOUND/AST 零网络。零网络；全合成占位。
"""
import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/open_free_router"))
from model_groups import (  # noqa: E402
    GROUPS_SCHEMA, GroupsError, FallbackProfiles, merge_groups,
)

PROVIDERS = {
    "groq": {"models": {
        "llama-3.3-70b": {"upstream_id": "llama-3.3-70b-versatile",
                          "enabled": True}}},
    "openrouter": {"models": {
        "llama-3.3-versatile": {"upstream_id": "llama-3.3-70b-versatile",
                                "enabled": True}}},
    "deepseek": {"models": {
        "deepseek-chat": {"upstream_id": "deepseek-chat", "enabled": True}}},
}


class TestMergeGroups(unittest.TestCase):
    def test_same_upstream_grouped(self):
        out = merge_groups(PROVIDERS)
        groups = {g["model_id"]: g for g in out["groups"]}
        llama = groups["llama-3.3-70b-versatile"]
        self.assertEqual(len(llama["members"]), 2)   # groq + openrouter
        self.assertEqual(llama["members"][0]["priority"], 1)
        self.assertEqual(llama["members"][1]["priority"], 2)

    def test_no_duplicate_models(self):
        """合并后模型列表无重复（同 model_id 只出现一次）。"""
        out = merge_groups(PROVIDERS)
        ids = [g["model_id"] for g in out["groups"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_schema(self):
        self.assertEqual(merge_groups(PROVIDERS)["schema"], GROUPS_SCHEMA)

    def test_incomplete_refused(self):
        with self.assertRaisesRegex(GroupsError, "GROUPS_INCOMPLETE"):
            merge_groups("not-a-dict")


class TestFallbackProfiles(unittest.TestCase):
    PROFILES = {"coding": ["deepseek-chat", "claude-sonnet（占位）"],
                "vision": ["glm-5.2", "gpt-4o（占位）"],
                "default": ["deepseek-chat"]}

    def test_default_used_when_no_request_profile(self):
        fp = FallbackProfiles(self.PROFILES)
        self.assertEqual(fp.resolve(), ["deepseek-chat"])

    def test_request_profile_overrides_default(self):
        fp = FallbackProfiles(self.PROFILES)
        self.assertEqual(fp.resolve("coding"),
                         ["deepseek-chat", "claude-sonnet（占位）"])

    def test_switch_does_not_change_default(self):
        fp = FallbackProfiles(self.PROFILES)
        fp.switch("vision")
        self.assertEqual(fp.resolve(), ["deepseek-chat"])   # 默认档行为不变

    def test_profile_not_found(self):
        fp = FallbackProfiles(self.PROFILES)
        with self.assertRaisesRegex(GroupsError, "PROFILE_NOT_FOUND"):
            fp.resolve("ghost")

    def test_empty_profiles_refused(self):
        with self.assertRaisesRegex(GroupsError, "GROUPS_INCOMPLETE"):
            FallbackProfiles({})

    def test_default_not_in_profiles_refused(self):
        with self.assertRaisesRegex(GroupsError, "GROUPS_INCOMPLETE"):
            FallbackProfiles({"coding": ["x（占位）"]}, default_profile="ghost")


class TestRedline(unittest.TestCase):
    def test_ast_zero_network(self):
        banned = {"urllib", "requests", "socket", "http", "subprocess"}
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "model_groups.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & banned, imported & banned)

    def test_no_weight_change(self):
        """非目标：不改路由评分权重。"""
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "model_groups.py").read_text(encoding="utf-8")
        for word in ("quality_weight", "cost_weight", "latency_weight"):
            self.assertNotIn(word, src, word)


if __name__ == "__main__":
    unittest.main()
