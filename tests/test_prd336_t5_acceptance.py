# -*- coding: utf-8 -*-
"""PRD-336/T5 测试（trend #1615 的 T5 环节）：GPU 资源编排验收试运行。

运行：.venv/bin/python -m pytest tests/test_prd336_t5_acceptance.py -q

覆盖（T1 §5 验收「合成账本全链回放（直接换入/先卸后换/INFEASIBLE/回退）
+ 文档无密钥」）：① 直接换入路（free 足够 hot_swap）② 先卸后换路（LRU
victim 卸后满足）③ INFEASIBLE 演练（需求超单卡容量显形不硬换）④ 回退演练
（rolled_back 底座恢复可再规划/failed 双失败 fallback_broken 零伪装供人工
介入）——四路全链 settle 回填账本全留痕；回放可复现；⑤ 文档无密钥。
零网络；全合成占位。
"""
import copy
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# T1 设计文档已随 PR #154 合并到 main，直接读仓内文件。
# 此前用 git show origin/trend/prd-336-t1-design:... 跨分支取文档，
# CI checkout 里没有该远端分支引用，exit 128，main 常年红在这一例。
sys.path.insert(0, str(REPO / "src" / "open_free_router"))

from gpu_fallback_gate import settle  # noqa: E402
from gpu_orchestrator import new_ledger, plan, reserve  # noqa: E402

NOW = "2026-11-10T09:00:00Z"


def _busy_ledger():
    led = new_ledger([{"gpu_id": "gpu-0", "total_vram_mb": 16000}])
    reserve(led, "chat-large（占位）", "gpu-0", 8000)
    reserve(led, "ocr-stale（占位）", "gpu-0", 8000)
    return led


def _last_used():
    return {"ocr-stale（占位）": "2026-11-01T00:00:00Z",
            "chat-large（占位）": "2026-11-10T08:59:00Z"}


class TestFourPaths(unittest.TestCase):
    def test_path1_hot_swap_direct(self):
        """① 直接换入：free 足够 → hot_swap 建议（账本只读零变更）。"""
        led = new_ledger([{"gpu_id": "gpu-0", "total_vram_mb": 16000}])
        before = copy.deepcopy(led)
        p = plan("chat-small（占位）", 4000, led)
        self.assertEqual(p["action"], "hot_swap")
        self.assertEqual(led, before)   # plan 只读（R1 账本不是驱动）

    def test_path2_unload_then_swap_lru(self):
        """② 先卸后换：LRU victim（ocr-stale）被选，在服底座保留。"""
        led = _busy_ledger()
        p = plan("chat-small（占位）", 8000, led, current_model="chat-large（占位）",
                 last_used=_last_used())
        self.assertEqual(p["action"], "unload_then_swap")
        self.assertEqual([v["model"] for v in p["victims"]],
                         ["ocr-stale（占位）"])

    def test_path3_infeasible_visible(self):
        """③ INFEASIBLE 演练：需求超单卡 → 显形不硬换。"""
        led = new_ledger([{"gpu_id": "gpu-0", "total_vram_mb": 8000}])
        p = plan("超大模型（占位）", 99999, led)
        self.assertEqual(p["action"], "infeasible")
        with self.assertRaises(Exception):
            settle(p, "healthy", led, at=NOW)   # 不可 settle（零硬换）

    def test_path4_rollback_restores_and_replannable(self):
        """④ 回退演练：rolled_back → 底座恢复，账本与初始等价可再规划。"""
        led = _busy_ledger()
        snapshot = copy.deepcopy(led["gpus"][0]["reserved"])
        p = plan("chat-small（占位）", 8000, led,
                 current_model="chat-large（占位）", last_used=_last_used())
        settle(p, "rolled_back", led, at=NOW)
        self.assertEqual(led["gpus"][0]["reserved"], snapshot)
        p2 = plan("chat-small（占位）", 8000, led,
                  current_model="chat-large（占位）", last_used=_last_used())
        self.assertEqual(p2["action"], "unload_then_swap")   # 可再规划

    def test_path4_failed_marks_broken_visibly(self):
        """④ failed 演练：换入与回退双失败 → fallback_broken 零伪装供人工。"""
        led = _busy_ledger()
        p = plan("chat-small（占位）", 8000, led,
                 current_model="chat-large（占位）", last_used=_last_used())
        out = settle(p, "failed", led, at=NOW, detail="双失败（占位）")
        self.assertTrue(out["fallback_broken"])
        self.assertIn("人工介入", out["note"])
        self.assertEqual([e["outcome"] for e in led["events"]], ["failed"])

    def test_replay_reproducible(self):
        """回放可复现：四路连跑两次逐字节一致。"""
        def run_all():
            led = _busy_ledger()
            p = plan("chat-small（占位）", 8000, led,
                     current_model="chat-large（占位）", last_used=_last_used())
            settle(p, "rolled_back", led, at=NOW)
            settle(p, "healthy", led, at="2026-11-10T09:05:00Z")
            return copy.deepcopy(led)
        self.assertEqual(run_all(), run_all())


class TestDocNoSecrets(unittest.TestCase):
    def test_design_doc_no_secrets(self):
        """⑤ 文档无密钥：T1 设计文档（仓内已合并版）零密钥/零 IP/外链仅
        github 与 example 域。"""
        doc = (REPO / "docs" / "plans" / "2026-11-09-prd-336-t1-design.md"
               ).read_text(encoding="utf-8")
        for token in ("password", "secret", "api_key", "AKIA", "Bearer ",
                      "BEGIN RSA"):
            self.assertNotIn(token.lower(), doc.lower(), token)
        self.assertEqual(re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", doc), [])
        for m in re.findall(r"https?://[^\s）)】]+", doc):
            self.assertTrue(any(ok in m for ok in
                                ("github.com", "example.invalid",
                                 "example.com")), m)


if __name__ == "__main__":
    unittest.main()
