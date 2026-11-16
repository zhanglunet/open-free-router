# PRD-467/T5 开发记录：按密钥额度跟踪验收试运行（2026-11-17）

> trend issue #2259。本链收官单（T1 #192 已合 / T2 #197 / T3 #198 / T4 #199 / T5 本单）。

## 交付

- `tests/test_prd467_t5_quota_acceptance.py`：9 例全绿（本链合计 41 例：T2 19+T3 6+T4 9+T5 9）。
  - 真实计量+守门+租约全链两遍逐字节一致：限额 2 RPM，3 个并行请求领租约——派 2 拦 1（进行中也算数），落定释放按实际 usage 记账（80/120，超预留观测 0）。
  - 恢复场景：checkpoint→重启恢复计数不归零（2 请求/150 token）；恢复缺口保守（counter_lost 拦下）。
  - 失败演练：无上限只计数不编数字拦 / reset 提示不复位（30 秒后计数仍在） / observed 头 30 分钟过期降档。
  - 链复跑：T2/T3/T4 子进程复跑 exit 0。文档无密钥（needle 排除设计文档合法引用）。

## PR 门槛合规核对（repos.yaml default_pr_gate）

| 门槛 | 落实 |
|---|---|
| require_integration_point | 接入点全链演练：派发前领租约/落定释放/守门放行——并行不越线场景端到端验证 |
| no_missing_channel | 全链零网络（注入时钟+无 fetch）；唯一子进程是链复跑测试自身 |
| no_duplicate_module | 只消费本链 T2/T3 产物，零新模块；不碰 BYOS/冷却/解析/权重 |
| impl_requires_devlog | 本文件即开发记录 |

## 合规核对

- 上限未知只计数不守门（没有证据不定罪）；丢计数保守（安全不变量）；示例全合成占位。只开 PR 不合并。
