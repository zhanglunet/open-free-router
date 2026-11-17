# -*- coding: utf-8 -*-
"""PRD-468/T3 测试（trend #2262 的 T3 环节）：探测恢复任务。
运行：.venv/bin/python -m pytest tests/test_prd468_t3_cooldown_probe.py -q
覆盖（T1 §3）：猜测性可提前解除/失败不延长/实锤绝不被探测/预算上限/
落账。零网络（probe_fn 注入）；全合成占位。
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/open_free_router"))
from cooldown_ladder import CooldownLadder  # noqa: E402
from cooldown_probe import DEFAULT_PROBE_BUDGET, probe_round  # noqa: E402

NOW = 1000000.0


def mk_with_429(slot=0):
    lad = CooldownLadder(now=NOW)
    lad.on_rate_limited(platform="groq", model_id="llama", slot=slot)
    return lad


class TestProbe(unittest.TestCase):
    def test_speculative_recovered_early(self):
        lad = mk_with_429()
        out = probe_round(lad, now=NOW + 5,
                          probe_fn=lambda key: True)   # 探测成功
        self.assertEqual(out["released"], ["groq:llama:slot-0"])
        self.assertFalse(lad.is_cooled_down("groq", "llama", 0))  # 提前解除
        self.assertEqual(lad.slots["groq:llama:slot-0"]["step"], 0)  # 清零

    def test_failed_probe_does_not_extend(self):
        lad = mk_with_429()
        until_before = lad.slots["groq:llama:slot-0"]["cooldown_until"]
        out = probe_round(lad, now=NOW + 5, probe_fn=lambda key: False)
        self.assertEqual(out["released"], [])
        self.assertEqual(lad.slots["groq:llama:slot-0"]["cooldown_until"],
                         until_before)                    # 不延长

    def test_hammered_cooldown_never_probed(self):
        lad = CooldownLadder(now=NOW)
        lad.on_payment_required(platform="groq", model_id="llama", slot=0)
        lad.on_auth_failed(platform="groq", model_id="llama", slot=1)
        out = probe_round(lad, now=NOW + 5, probe_fn=lambda key: True)
        self.assertEqual(out["probed"], [])              # 实锤绝不被探测

    def test_budget_caps_probe_count(self):
        lad = CooldownLadder(now=NOW)
        for slot in range(5):
            lad.on_rate_limited(platform="groq", model_id="llama", slot=slot)
        out = probe_round(lad, now=NOW + 5, probe_fn=lambda key: False,
                          budget=2)
        self.assertEqual(len(out["probed"]), 2)          # 每轮预算上限
        self.assertEqual(out["budget_exceeded"], 3)

    def test_budget_default_is_3(self):
        self.assertEqual(DEFAULT_PROBE_BUDGET, 3)

    def test_probe_fn_required(self):
        lad = mk_with_429()
        with self.assertRaises(Exception):
            probe_round(lad, now=NOW + 5, probe_fn=None)


if __name__ == "__main__":
    unittest.main()
