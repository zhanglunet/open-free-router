# PRD-475/T5 开发记录：Fusion 多模型验收试运行（2026-11-17）

> trend issue #2299。本链收官单（T1 #227 / T2~T4 #228 / T5 本单）。

## 交付

- `tests/test_prd475_t5_fusion_acceptance.py`：9 例全绿（本链合计 22 例：T2~T4 13+T5 9）。
  - 全链两遍逐字节一致：启用 Fusion → 并行分发 3 模型 → 评审合成 → 成本实测（budget_used=3 可量化）→ 默认关闭验证 → 超预算拒绝。
  - 失败演练：评审失败降级返回最优单路（不空手而归）/单路失败隔离不传播/护栏不可绕过（budget/max_targets 双保险）。
  - 链复跑：T2~T4 子进程复跑 exit 0。文档无密钥（needle 排除设计文档合法引用——可行性结论含「做」字）。

## PR 门槛合规核对（repos.yaml default_pr_gate）

| 门槛 | 落实 |
|---|---|
| require_integration_point | 接入点全链演练：启用→并行分发→评审合成→成本实测，端到端验证 |
| no_missing_channel | 全链零网络（dispatch_fn/synthesis_fn 注入）；唯一子进程是链复跑测试自身 |
| no_duplicate_module | 只消费本链 T2~T4 产物，零新模块 |
| impl_requires_devlog | 本文件即开发记录 |

## 合规核对

- Fusion 默认关闭（不显式开启就零调用，测试钉值）；成本护栏不可绕过（预算+硬上限双保险）。
- 不承诺质量一定优于单模型（以实测为准）；不做能力分（intelligenceRank 零出现）。
- 示例全合成占位。只开 PR 不合并。
