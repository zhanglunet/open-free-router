# -*- coding: utf-8 -*-
"""PRD-467/T3（trend issue #2257 的 T3 环节）：额度窗口持久化镜像与重启恢复。

按 T1 设计 §2.7 落地：热路径是内存环（T2 QuotaLedger），本模块是
SQLite 持久镜像——落定时写、检查点、重启时恢复：
  - 表 quota_windows(key, window, bucket_ts, requests, tokens_est)，
    主键 (key, window, bucket_ts)，索引 (key, window)；放 usage.db 同侧
    （0600 权限、WAL——沿用 AnalyticsStore 约定）；
  - 过期桶随检查点一起清（分钟桶超 2 小时、日桶超 48 小时即死数据）；
  - 重启恢复：从镜像重建窗口计数——T2 验收口径「重启后计数不归零」；
  - 恢复缺口处理（§2.5）：检查点之后、落定之前的一小段丢失按保守处理
    （调用方对恢复出的键标 counter_lost），不静默放行。
纯 stdlib sqlite3；零网络零子进程；示例全虚构占位。
测试：tests/test_prd467_t3_quota_ledger_store.py
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

STORE_SCHEMA = "quota-ledger-store/v1"
ERROR_CODES = ("STORE_INCOMPLETE",)
MINUTE_BUCKETS = {"rpm", "tpm"}
HOUR_BUCKETS = {"rpd", "tpd"}
PERMS = 0o600


class QuotaLedgerStore:
    """额度窗口 SQLite 镜像（同目录 usage.db；WAL；0600）。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS quota_windows (
                key TEXT, window TEXT, bucket_ts INTEGER,
                requests INTEGER, tokens_est INTEGER,
                PRIMARY KEY (key, window, bucket_ts))""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_kw "
                          "ON quota_windows(key, window)")
        self.conn.commit()
        try:
            os_chmod = self.db_path.chmod
            os_chmod(PERMS)
        except OSError:
            pass

    def checkpoint(self, windows: dict[str, Any], now: float) -> int:
        """把内存窗口镜像进 SQLite，并清死数据桶（分钟桶>2h、日桶>48h）。"""
        written = 0
        for key, win in windows.items():
            for ts, bucket in win.buckets.items():
                age = now - ts * win.bucket_sec
                dead = (win.window in MINUTE_BUCKETS and age >= 7200) or \
                       (win.window in HOUR_BUCKETS and age >= 172800)
                if dead or (bucket["requests"] == 0 and bucket["tokens_est"] == 0):
                    self.conn.execute(
                        "DELETE FROM quota_windows WHERE key=? AND window=? "
                        "AND bucket_ts=?", (key, win.window, ts))
                    continue
                self.conn.execute(
                    "INSERT INTO quota_windows(key, window, bucket_ts, "
                    "requests, tokens_est) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(key, window, bucket_ts) DO UPDATE SET "
                    "requests=excluded.requests, tokens_est=excluded.tokens_est",
                    (key, win.window, ts, bucket["requests"],
                     bucket["tokens_est"]))
                written += 1
        self.conn.commit()
        return written

    def restore_into(self, ledger: Any) -> int:
        """重启恢复：从镜像重建账本窗口计数（重启后计数不归零）。"""
        rows = self.conn.execute(
            "SELECT key, window, bucket_ts, requests, tokens_est "
            "FROM quota_windows").fetchall()
        restored = 0
        for key, window, ts, requests, tokens in rows:
            win = ledger._win(key, window)
            win.buckets[ts] = {"requests": requests, "tokens_est": tokens}
            restored += 1
        return restored

    def purge_dead(self, now: float) -> int:
        dead_total = 0
        cur = self.conn.execute("SELECT DISTINCT key, window FROM quota_windows")
        for key, window in cur.fetchall():
            bucket_sec = 1 if window in MINUTE_BUCKETS else 3600
            limit_age = 7200 if window in MINUTE_BUCKETS else 172800
            dead = self.conn.execute(
                "DELETE FROM quota_windows WHERE key=? AND window=? AND "
                "? - bucket_ts * ? >= ?",
                (key, window, now, bucket_sec, limit_age))
            dead_total += dead.rowcount
        self.conn.commit()
        return dead_total

    def close(self) -> None:
        self.conn.close()
