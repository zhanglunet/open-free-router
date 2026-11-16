# PRD-476/T5 开发记录：决策模型选路由验收试运行（2026-11-17）

> trend issue #2304。本链收官单（T1 #230 / T2~T4 #231 / T5 本单）。

## 交付

- `tests/test_prd476_t5_decision_acceptance.py`：8 例全绿（本链合计 22 例：T2~T4 14+T5 8）。
  - 决策准确率抽查：coding 请求→coding 档=对；vision 请求→vision 档=对（两请求均无降级）。
  - 全链两遍逐字节一致：决策调用→按轮缓存→低置信降级→预算上限。
  - 失败演练：超时降级/默认关闭/预算耗尽降级均衡。
  - 链复跑：T2~T4 子进程复跑 exit 0。文档无密钥（needle 排除设计文档合法引用——与 202 边界含 "202"）。

## PR 门槛合规核对（repos.yaml default_pr_gate）

| 门槛 | 落实 |
|---|---|
| require_integration_point | 接入点全链演练：决策调用→按轮缓存→降级均衡→预算上限端到端验证 |
| no_missing_channel | 全链零网络；唯一子进程是链复跑测试自身 |
| no_duplicate_module | 只消费本链 T2~T4 产物，零新模块 |
| impl_requires_devlog | 本文件即开发记录 |

## 合规核对

- 决策路由默认关闭（测试钉值）；决策误判只影响一轮（按轮缓存限定范围）。
- 示例全合成占位。只开 PR 不合并。
