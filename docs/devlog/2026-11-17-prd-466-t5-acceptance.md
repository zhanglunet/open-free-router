# PRD-466/T5 开发记录：签名目录自更新验收试运行（2026-11-17）

> trend issue #2254。本链收官单（T1 #191 已合 / T2 #193 / T3 #194 / T4 #195 / T5 本单）。

## 交付

- `tests/test_prd466_t5_catalog_acceptance.py`：11 例全绿（本链合计 46 例：T2 20+T3 14+T4 8+T5 11）。
  - 验收点①签名伪造拒绝：错钥签发→整份丢弃大声报错（outcome=failed）。
  - 验收点②旧版本不回滚：先应用 2026.11.20 再拉 2026.11.18 → skipped_older（**T2 补回滚守卫：version < applied 即使 ≥ MIN 也跳过**——PRD 验收点②落地时发现的 T1 设计未覆盖区间，按 PRD 口径补齐）。
  - 验收点③用户改动保留：user 行 context_window 不被收编（4096 保持）、用户关闭的 catalog-model 不被重开。
  - 全链两遍逐字节一致：真钥签名目录 → run_sync（apply_fn=T3 apply_catalog）→ 状态文件落账（applied_version/applied_json）→ 差异报告落盘（changed 含 context_window 更新）。
  - 失败演练：篡改签名大声丢弃 / 墓碑复活拦截 / 降级不断线（本地目录继续服务）。
  - 链复跑：T2/T3/T4 子进程复跑 exit 0。文档无密钥（needle 排除合法字段名 api_key_env——R5 口径注明）。

## 链上修复（随 T5 落库）

- T2 version_decision 补回滚守卫（version < applied → skipped_older，设计未覆盖区间按 PRD 验收点②补齐）。
- T3 apply_catalog 元数据跟目录走适用于 baseline 行（设计 §5 只豁免 user 行）——全链演练时发现 changed 报告为空（baseline 行元数据没跟目录走）。

## PR 门槛合规核对（repos.yaml default_pr_gate）

| 门槛 | 落实 |
|---|---|
| require_integration_point | 接入点全链演练：签名目录→验签→事务应用→状态落账→差异报告，三个验收点逐一钉值 |
| no_missing_channel | 全链零真实网络（fetch_fn 注入）；唯一子进程是链复跑测试自身 |
| no_duplicate_module | 只消费本链 T2/T3/T4 产物，零新模块 |
| impl_requires_devlog | 本文件即开发记录 |

## 合规核对

- 私钥从不离源站（仓库只有公钥+环境覆盖）；示例全合成占位。只开 PR 不合并。
