# -*- coding: utf-8 -*-
"""PRD-468/T4 测试（trend #2263 的 T4 环节）：运维上限与一键清除。
运行：.venv/bin/python -m pytest tests/test_prd468_t4_cooldown_ops.py -q
覆盖（T1 §4）：封顶/retry_after 不缩短/一键清除留痕/精细重置/AST 零网络。
"""
import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/open_free_router"))
from cooldown_ladder import CooldownLadder  # noqa: E402
from cooldown_ops import (  # noqa: E402
    OPS_SCHEMA, clamp_cooldown, clear_all_cooldowns, reset_slot)

NOW = 1000000.0


class TestClamp(unittest.TestCase):
    def test_cap_1_day(self):
        self.assertEqual(clamp_cooldown(999999), 86400)

    def test_retry_after_not_shortened(self):
        self.assertEqual(clamp_cooldown(90, retry_after=999999), 999999)

    def test_normal_passes(self):
        self.assertEqual(clamp_cooldown(90), 90)


class TestOps(unittest.TestCase):
    def test_clear_all(self):
        lad = CooldownLadder(now=NOW)
        lad.on_rate_limited(platform="groq", model_id="llama", slot=0)
        lad.on_rate_limited(platform="groq", model_id="llama", slot=1)
        out = clear_all_cooldowns(lad, by="ops（占位）", at=NOW + 5)
        self.assertEqual(out["count"], 2)
        self.assertFalse(lad.is_cooled_down("groq", "llama", 0))
        self.assertFalse(lad.is_cooled_down("groq", "llama", 1))
        self.assertEqual(lad.entries()[-1]["kind"], "ops_clear_all")   # 留痕

    def test_clear_all_requires_operator(self):
        lad = CooldownLadder(now=NOW)
        with self.assertRaisesRegex(Exception, "OPS_INCOMPLETE"):
            clear_all_cooldowns(lad, by="", at=NOW)

    def test_reset_slot_precision(self):
        lad = CooldownLadder(now=NOW)
        lad.on_rate_limited(platform="groq", model_id="llama", slot=0)
        lad.on_rate_limited(platform="groq", model_id="llama", slot=1)
        out = reset_slot(lad, platform="groq", model_id="llama", slot=0,
                         by="ops（占位）", at=NOW + 5)
        self.assertTrue(out["was_active"])
        self.assertFalse(lad.is_cooled_down("groq", "llama", 0))
        self.assertTrue(lad.is_cooled_down("groq", "llama", 1))   # 只解指定槽

    def test_reset_inactive_noop(self):
        lad = CooldownLadder(now=NOW)
        out = reset_slot(lad, platform="groq", model_id="llama", slot=0,
                         by="ops（占位）", at=NOW)
        self.assertFalse(out["was_active"])

    def test_retry_after_survives_cap(self):
        """提供方明说的恢复时间不被缩短（retry_after > 封顶以实锤为准）。"""
        lad = CooldownLadder(now=NOW)
        lad.on_rate_limited(platform="groq", model_id="llama", slot=0,
                            retry_after=999999)
        self.assertTrue(lad.is_cooled_down("groq", "llama", 0))
        self.assertEqual(clamp_cooldown(90, retry_after=999999), 999999)

    def test_ast_zero_network(self):
        banned = {"urllib", "requests", "socket", "http", "subprocess"}
        src = (Path(__file__).resolve().parents[1] / "src/open_free_router"
               / "cooldown_ops.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & banned, imported & banned)
        self.assertEqual(OPS_SCHEMA, "cooldown-ops/v1")


if __name__ == "__main__":
    unittest.main()
