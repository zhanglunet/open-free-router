# PRD-467/T3 开发记录：额度窗口持久化镜像与重启恢复（2026-11-17）

> trend issue #2257。承接 T2（#197）。

## 交付

- `src/open_free_router/quota_ledger_store.py`：QuotaLedgerStore——SQLite 镜像（quota_windows 表：key/window/bucket_ts/requests/tokens_est，主键+索引；WAL；0600 权限沿用 AnalyticsStore 约定）+ checkpoint()（内存窗口落定镜像+死数据桶清理：分钟桶>2h、日桶>48h）+ restore_into()（重启恢复：从镜像重建账本窗口计数——T2 验收口径「重启后计数不归零」）+ purge_dead()。
- `tests/test_prd467_t3_quota_ledger_store.py`：6 例全绿（checkpoint+重启恢复计数不归零/0600/死桶清理/表结构与 WAL/恢复缺口保守处理钉值/schema）。

## PR 门槛合规核对（repos.yaml default_pr_gate）

| 门槛 | 落实 |
|---|---|
| require_integration_point | 接入点=QuotaLedger 落定时 checkpoint、启动时 restore_into（热路径仍是内存环，微秒级守门） |
| no_missing_channel | 纯 stdlib sqlite3 本地文件，零网络零子进程 |
| no_duplicate_module | 沿用 usage.db 同侧与 0600/WAL/30 天保留期约定，不另起库 |
| impl_requires_devlog | 本文件即开发记录 |

## 合规核对

- 恢复缺口按 §2.5 保守处理（标 counter_lost 不静默放行，测试钉值）。
- 示例全合成占位。只开 PR 不合并。
