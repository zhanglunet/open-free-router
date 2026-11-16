# PRD-472/T5 开发记录：健康检查节流验收试运行（2026-11-17）

> trend issue #2284。本链收官单（T1 #218 / T2~T4 #219 / T5 本单）。

## 交付

- `tests/test_prd472_t5_health_throttle_acceptance.py`：6 例全绿（本链合计 18 例：T2~T4 12+T5 6）。
  - 全链两遍逐字节一致：5 个提供方（2 免费/3 烧额度）× 10 轮探测 → 降频量化（costs_quota 探测次数显著低于 free_verify）。
  - 失败演练：总开关杀死探测（probed=0）/预算超限跳过/链复跑 exit 0。文档无密钥。

## PR 门槛合规核对（repos.yaml default_pr_gate）

| 门槛 | 落实 |
|---|---|
| require_integration_point | 接入点全链演练：多提供方多轮探测 → 成本分级降频量化对比 |
| no_missing_channel | 全链零网络；唯一子进程是链复跑测试自身 |
| no_duplicate_module | 只消费本链 T2~T4 产物，零新模块 |
| impl_requires_devlog | 本文件即开发记录 |

## 合规核对

- 降频只降重复校验（首次不变，测试钉值）；示例全合成占位。只开 PR 不合并。
