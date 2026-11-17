# -*- coding: utf-8 -*-
"""PRD-468/T5 测试（trend #2264 的 T5 环节）：冷却阶梯与探测恢复验收试运行。
运行：.venv/bin/python -m pytest tests/test_prd468_t5_cooldown_acceptance.py -q
验收（PRD 任务拆分 T5 行，freellmapi 回退清单四场景）：真实监督回路全链
两遍逐字节一致——连续 429 走阶梯逐级升 → 探测成功提前解除 → 成功清零
→ 按新起点重升；失败演练：402 实锤不被探测/一键清除留痕/封顶不越界；
链复跑 T2/T3/T4 子进程 exit 0；文档无密钥。零网络（probe_fn 注入）。
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/open_free_router"))
from cooldown_ladder import LADDER_STEPS, CooldownLadder  # noqa: E402
from cooldown_ops import clear_all_cooldowns  # noqa: E402
from cooldown_probe import probe_round  # noqa: E402

NOW = 1000000.0
TESTS = Path(__file__).resolve().parent


def full_chain():
    """全链：连续 429 → 探测成功提前解除 → 成功清零 → 重升。"""
    lad = CooldownLadder(now=NOW)
    cooldowns = []
    for _ in range(3):                          # 连续 429 三次 → 逐级升
        out = lad.on_rate_limited(platform="groq", model_id="llama", slot=0)
        cooldowns.append(out["cooldown_seconds"])
    probe = probe_round(lad, now=NOW + 5, probe_fn=lambda key: True)
    lad.on_success(platform="groq", model_id="llama", slot=0)
    re_limited = lad.on_rate_limited(platform="groq", model_id="llama", slot=0)
    return {"cooldowns": cooldowns, "probe_released": probe["released"],
            "post_success_cooldown": re_limited["cooldown_seconds"],
            "log": lad.entries()}


class TestAcceptance(unittest.TestCase):
    def test_chain_twice_byte_identical(self):
        runs = [full_chain(), full_chain()]
        self.assertEqual(json.dumps(runs[0], sort_keys=True),
                         json.dumps(runs[1], sort_keys=True))

    def test_ladder_ascending(self):
        run = full_chain()
        self.assertEqual(run["cooldowns"], [90, 120, 600])   # 逐级升

    def test_probe_recovered_early(self):
        run = full_chain()
        self.assertEqual(run["probe_released"], ["groq:llama:slot-0"])

    def test_success_resets_then_re_ascends(self):
        run = full_chain()
        self.assertEqual(run["post_success_cooldown"], 90)   # 清零后回起点

    def test_ops_clear_all_leaves_trail(self):
        lad = CooldownLadder(now=NOW)
        lad.on_rate_limited(platform="groq", model_id="llama", slot=0)
        lad.on_rate_limited(platform="groq", model_id="llama", slot=1)
        out = clear_all_cooldowns(lad, by="ops（占位）", at=NOW + 5)
        self.assertEqual(out["count"], 2)
        self.assertEqual(lad.entries()[-1]["kind"], "ops_clear_all")

    def test_402_goes_straight_to_day(self):
        lad = CooldownLadder(now=NOW)
        lad.on_rate_limited(platform="groq", model_id="llama", slot=0)
        lad.on_rate_limited(platform="groq", model_id="llama", slot=0)
        out = lad.on_payment_required(platform="groq", model_id="llama",
                                      slot=0)
        self.assertEqual(out["cooldown_seconds"], 86400)     # 不走阶梯

    def test_speculative_flag(self):
        """上游没说恢复时间 → 猜测性=True；说了 → False（实锤）。"""
        lad = CooldownLadder(now=NOW)
        out_spec = lad.on_rate_limited(platform="groq", model_id="llama",
                                       slot=0)
        out_conf = lad.on_rate_limited(platform="groq", model_id="llama",
                                       slot=1, retry_after=3600)
        self.assertTrue(out_spec["speculative"])
        self.assertFalse(out_conf["speculative"])


class TestChainRerun(unittest.TestCase):
    def test_t2_t3_t4_rerun_green(self):
        """链复跑：T2/T3/T4 测试子进程复跑 exit 0。"""
        for name in ("test_prd468_t2_cooldown_ladder.py",
                     "test_prd468_t3_cooldown_probe.py",
                     "test_prd468_t4_cooldown_ops.py"):
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q",
                 str(TESTS / name)],
                capture_output=True, timeout=600)
            self.assertEqual(proc.returncode, 0,
                             f"{name} 复跑失败：{proc.stdout[-400:]}{proc.stderr[-400:]}")


class TestDocNoSecrets(unittest.TestCase):
    def test_t1_design_doc_no_secrets(self):
        """文档无密钥：T1 设计拼接 needle 扫描+占位符纪律（R5）。"""
        doc = next(Path(__file__).resolve().parents[1].glob(
            "docs/plans/2026-11-17-cooldown-ladder-design.md"))
        text = doc.read_text(encoding="utf-8").lower()
        for needle in ("sk-" + "live", "passw" + "ord =",
                       "BEGIN PRIVATE", "AKIA" + "x"):
            self.assertNotIn(needle, text, f"设计文档出现密钥样式 {needle}")
        self.assertIn("占位", text)


if __name__ == "__main__":
    unittest.main()
