# -*- coding: utf-8 -*-
"""PRD-467/T3 测试（trend #2257 的 T3 环节）：额度窗口持久化镜像与重启恢复。
运行：.venv/bin/python -m pytest tests/test_prd467_t3_quota_ledger_store.py -q
覆盖（T1 §2.7）：checkpoint 镜像/重启恢复计数不归零（T2 验收口径）/
0600 权限/死数据桶清理/恢复缺口保守处理（标 counter_lost）/WAL。
零网络；全合成占位。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.quota_ledger import (  # noqa: E402
    QuotaLedger, count_key)
from open_free_router.quota_ledger_store import (  # noqa: E402
    QuotaLedgerStore, STORE_SCHEMA)

T0 = 1000000.0


class TestStore(unittest.TestCase):
    def test_checkpoint_and_restart_recovery(self):
        """重启后计数不归零（T1 §2.7 T2 验收口径）。"""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "usage.db"
            store = QuotaLedgerStore(db)
            led1 = QuotaLedger(now=T0)
            led1.set_limit("groq", "llama", 0, "rpm", 10, source="documented")
            led1.record_dispatch(platform="groq", model_id="llama", slot=0,
                                 tokens_est=100)
            led1.record_dispatch(platform="groq", model_id="llama", slot=0,
                                 tokens_est=50)
            store.checkpoint(led1.windows, now=T0)
            store.close()
            # 重启：新账本从镜像恢复
            store2 = QuotaLedgerStore(db)
            led2 = QuotaLedger(now=T0 + 5)
            restored = store2.restore_into(led2)
            self.assertEqual(restored, 4)        # 四窗口各 1 桶（rpm/rpd/tpm/tpd）
            r, t = led2._win(count_key("groq", "llama", 0, "rpm"),
                             "rpm").count(T0 + 5)
            self.assertEqual((r, t), (2, 150))   # 计数不归零
            lim = led2.get_limit("groq", "llama", 0, "rpm")
            self.assertIsNone(lim)               # 上限不入镜像（由来源重新注入）

    def test_perm_0600(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "usage.db"
            QuotaLedgerStore(db)
            self.assertEqual(oct(db.stat().st_mode & 0o777), "0o600")

    def test_dead_buckets_purged(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "usage.db"
            store = QuotaLedgerStore(db)
            led = QuotaLedger(now=T0)
            led.record_dispatch(platform="groq", model_id="llama", slot=0)
            store.checkpoint(led.windows, now=T0)
            dead = store.purge_dead(now=T0 + 7200)   # 分钟桶 2 小时后即死数据
            self.assertGreaterEqual(dead, 1)
            rows = store.conn.execute(
                "SELECT COUNT(*) FROM quota_windows WHERE window='rpm'").fetchone()
            self.assertEqual(rows[0], 0)

    def test_schema_and_wal(self):
        with tempfile.TemporaryDirectory() as td:
            store = QuotaLedgerStore(Path(td) / "usage.db")
            mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
            tables = store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            self.assertIn(("quota_windows",), tables)


class TestRecoveryGap(unittest.TestCase):
    def test_recovery_gap_conservative(self):
        """恢复缺口（检查点之后丢失的一段）按保守处理：标 counter_lost。"""
        led = QuotaLedger(now=T0)
        led.set_limit("groq", "llama", 0, "rpm", 10, source="documented")
        led.mark_counter_lost("groq", "llama", 0)   # 恢复缺口按 §2.5 保守
        ok, reason = led.can_dispatch(platform="groq", model_id="llama",
                                      slot=0)
        self.assertFalse(ok)
        self.assertEqual(reason, QUOTA_COUNTER_LOST := "quota_counter_lost")

    def test_store_schema(self):
        self.assertEqual(STORE_SCHEMA, "quota-ledger-store/v1")


if __name__ == "__main__":
    unittest.main()
