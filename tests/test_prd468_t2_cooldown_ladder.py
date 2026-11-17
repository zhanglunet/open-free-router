# -*- coding: utf-8 -*-
"""PRD-468/T2 测试（trend #2261 的 T2 环节）：冷却阶梯与原因标签。
运行：.venv/bin/python -m pytest tests/test_prd468_t2_cooldown_ladder.py -q
覆盖（T1 §2）：429 逐级升/402 冷 1 天/401 到下一健康周期/成功清零/
原因标签落账/retry_after 实锤优先不缩短/AST 零网络。零网络；全合成占位。
"""
import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/open_free_router"))
from cooldown_ladder import (  # noqa: E402
    DAY_SECONDS, ERROR_CODES, LADDER_SCHEMA, LADDER_STEPS, CooldownLadder,
)

NOW = 1000000.0


def mk(**kw):
    return CooldownLadder(now=NOW, **kw)


class TestLadder(unittest.TestCase):
    def test_429_ladder_ascending(self):
        lad = mk()
        expected = [90, 120, 600, 3600, 86400, 86400]   # 封顶在 1 天
        for i, exp in enumerate(expected):
            out = lad.on_rate_limited(platform="groq", model_id="llama",
                                      slot=0)
            self.assertEqual(out["cooldown_seconds"], exp, f"step {i}")
            self.assertEqual(out["step"], min(i + 1, len(LADDER_STEPS)))

    def test_success_resets_ladder(self):
        lad = mk()
        lad.on_rate_limited(platform="groq", model_id="llama", slot=0)
        lad.on_rate_limited(platform="groq", model_id="llama", slot=0)
        out = lad.on_success(platform="groq", model_id="llama", slot=0)
        self.assertEqual(out["prev_step"], 2)
        out2 = lad.on_rate_limited(platform="groq", model_id="llama", slot=0)
        self.assertEqual(out2["cooldown_seconds"], 90)   # 回起点

    def test_402_cools_one_day_not_ladder(self):
        lad = mk()
        lad.on_rate_limited(platform="groq", model_id="llama", slot=0)
        out = lad.on_payment_required(platform="groq", model_id="llama",
                                      slot=0)
        self.assertEqual((out["cooldown_seconds"], out["speculative"]),
                         (DAY_SECONDS, False))

    def test_401_cooldown_infinite(self):
        lad = mk()
        out = lad.on_auth_failed(platform="groq", model_id="llama", slot=0)
        self.assertEqual(out["cooldown_until"], "inf")
        self.assertTrue(lad.is_cooled_down("groq", "llama", 0))

    def test_retry_after_priority_not_shortened(self):
        lad = mk()
        out = lad.on_rate_limited(platform="groq", model_id="llama", slot=0,
                                  retry_after=3600)
        self.assertEqual(out["cooldown_seconds"], 3600)   # 实锤优先

    def test_is_cooled_down_expires(self):
        lad = mk()
        lad.on_rate_limited(platform="groq", model_id="llama", slot=0)
        self.assertTrue(lad.is_cooled_down("groq", "llama", 0))
        lad.now += 91
        self.assertFalse(lad.is_cooled_down("groq", "llama", 0))


class TestLedger(unittest.TestCase):
    def test_log_jsonl_append_only(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cooldown" / "log.jsonl"
            lad = mk(log_path=path)
            lad.on_rate_limited(platform="groq", model_id="llama", slot=0)
            lad.on_success(platform="groq", model_id="llama", slot=0)
            self.assertEqual(len(lad.entries()), 2)
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(json.loads(lines[1])["kind"], "success_reset")

    def test_log_causation_fields(self):
        lad = mk()
        out = lad.on_rate_limited(platform="groq", model_id="llama", slot=0)
        e = lad.entries()[-1]
        self.assertEqual(e["reason"], "429_limited")
        self.assertIn("speculative", e)              # 猜测性标签可查


class TestRedline(unittest.TestCase):
    def test_ast_zero_network(self):
        banned = {"urllib", "requests", "socket", "http", "subprocess"}
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "cooldown_ladder.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & banned, imported & banned)

    def test_vocab_closed(self):
        self.assertEqual(len(LADDER_STEPS), 5)
        self.assertEqual(LADDER_STEPS[0], 90)
        self.assertEqual(BENCH_SCHEMA := LADDER_SCHEMA,
                         "cooldown-ladder/v1")


if __name__ == "__main__":
    unittest.main()
