# -*- coding: utf-8 -*-
"""PRD-467/T5 测试（trend #2259 的 T5 环节）：按密钥额度跟踪验收试运行。
运行：.venv/bin/python -m pytest tests/test_prd467_t5_quota_acceptance.py -q
验收（PRD 任务拆分 T5 行）：真实计量+守门+租约全链演练两遍逐字节一致——
并行情景：N 个并行请求领租约不越线（进行中也算数）→ 落定释放 → 恢复
场景：checkpoint→重启恢复计数不归零→恢复缺口保守；失败演练：丢计数
保守/无上限只计数/reset 提示不复位；链复跑 T2/T3/T4 子进程 exit 0；
文档无密钥。零网络；全合成占位。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
TESTS = Path(__file__).resolve().parent

from open_free_router.quota_ledger import (  # noqa: E402
    QUOTA_COUNTER_LOST, QuotaLedger, QuotaLedgerError, count_key)
from open_free_router.quota_ledger_store import QuotaLedgerStore  # noqa: E402

NOW = 1000000.0


def full_chain():
    """真实计量+守门+租约全链：限额 2 RPM，3 个并行请求领租约。"""
    led = QuotaLedger(now=NOW)
    led.set_limit("groq", "llama", 0, "rpm", 2, source="documented")
    leases = []
    blocked = 0
    for _ in range(3):
        try:
            leases.append(led.acquire_lease(platform="groq",
                                            model_id="llama", slot=0))
        except QuotaLedgerError as exc:
            blocked += 1
            assert "QUOTA_EXHAUSTED" in str(exc)
    # 前两个落定
    led.release_lease(lease_id=leases[0]["lease_id"], actual_tokens=80)
    led.release_lease(lease_id=leases[1]["lease_id"], actual_tokens=120)
    return {"leases": leases, "blocked": blocked,
            "verdicts": [l.get("actual_tokens") for l in leases],
            "under_reserve": led.observations_under_reserve}


class TestFullChain(unittest.TestCase):
    def test_parallel_leases_never_exceed(self):
        """N 个并行请求领租约不越线：limit 2 → 派 2 拦 1（进行中也算数）。"""
        run = full_chain()
        self.assertEqual(len(run["leases"]), 2)
        self.assertEqual(run["blocked"], 1)
        self.assertEqual(run["verdicts"], [80, 120])
        self.assertEqual(run["under_reserve"], 0)   # 80/120 ≤ 512 预留

    def test_chain_twice_byte_identical(self):
        runs = [full_chain(), full_chain()]
        self.assertEqual(json.dumps(runs[0], sort_keys=True),
                         json.dumps(runs[1], sort_keys=True))


class TestRecoveryDrill(unittest.TestCase):
    def test_checkpoint_restart_recovery(self):
        """恢复场景：checkpoint→重启恢复计数不归零（T2 验收口径）。"""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "usage.db"
            store = QuotaLedgerStore(db)
            led = QuotaLedger(now=NOW)
            led.record_dispatch(platform="groq", model_id="llama", slot=0,
                                tokens_est=100)
            store.checkpoint(led.windows, now=NOW)
            store.close()
            store2 = QuotaLedgerStore(db)
            led2 = QuotaLedger(now=NOW + 5)
            store2.restore_into(led2)
            r, t = led2._win(count_key("groq", "llama", 0, "rpm"),
                             "rpm").count(NOW + 5)
            self.assertEqual((r, t), (1, 100))   # 计数不归零

    def test_recovery_gap_conservative(self):
        """恢复缺口保守：标 counter_lost → 拦下不静默放行。"""
        led = QuotaLedger(now=NOW)
        led.set_limit("groq", "llama", 0, "rpm", 10, source="documented")
        led.mark_counter_lost("groq", "llama", 0)
        ok, reason = led.can_dispatch(platform="groq", model_id="llama",
                                      slot=0)
        self.assertFalse(ok)
        self.assertEqual(reason, QUOTA_COUNTER_LOST)


class TestFailureDrills(unittest.TestCase):
    def test_no_limit_counts_only(self):
        led = QuotaLedger(now=NOW)
        for _ in range(100):
            led.record_dispatch(platform="groq", model_id="llama", slot=0)
        ok, _ = led.can_dispatch(platform="groq", model_id="llama", slot=0)
        self.assertTrue(ok)                      # 无上限只计数，不编数字拦

    def test_reset_hint_never_resets(self):
        """reset 提示不复位：advance 30 秒（不到 60 秒窗口）计数仍在。"""
        led = QuotaLedger(now=NOW)
        led.record_dispatch(platform="groq", model_id="llama", slot=0)
        led.advance(30)
        r, _ = led._win(count_key("groq", "llama", 0, "rpm"), "rpm").count(
            led.now)
        self.assertEqual(r, 1)                   # 窗口内不清零

    def test_observed_header_expiry_downgrades(self):
        led = QuotaLedger(now=NOW)
        led.set_limit("groq", "llama", 0, "rpm", 10, source="observed_header",
                      observed_at=NOW)
        led.advance(1801)
        lim = led.get_limit("groq", "llama", 0, "rpm")
        self.assertIsNone(lim)                   # 过期降档：视为无此档

    def test_chain_rerun_green(self):
        """链复跑：T2/T3/T4 测试子进程复跑 exit 0。"""
        for name in ("test_prd467_t2_quota_ledger.py",
                     "test_prd467_t3_quota_ledger_store.py",
                     "test_prd467_t4_quota_boundary.py"):
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "discover",
                 "-s", str(TESTS), "-p", name],
                capture_output=True, timeout=600)
            self.assertEqual(proc.returncode, 0,
                             f"{name} 复跑失败：{proc.stdout[-400:]}{proc.stderr[-400:]}")


class TestDocNoSecrets(unittest.TestCase):
    def test_t1_design_doc_no_secrets(self):
        """文档无密钥：T1 设计拼接 needle 扫描+占位符纪律（R5）。"""
        doc = next(REPO.glob("docs/plans/2026-11-17-per-key-quota-design.md"))
        text = doc.read_text(encoding="utf-8").lower()
        for needle in ("sk-" + "live", "passw" + "ord =",
                       "BEGIN PRIVATE", "AKIA" + "x"):
            self.assertNotIn(needle, text, f"设计文档出现密钥样式 {needle}")
        self.assertIn("占位", text)


if __name__ == "__main__":
    unittest.main()
