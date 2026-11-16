# PRD-470/T5 开发记录：统一模型分组验收试运行（2026-11-17）

> trend issue #2274。本链收官单（T1 #210 / T2+T3 #211 / T4 #212 / T5 本单）。

## 交付

- `tests/test_prd470_t5_model_groups_acceptance.py`：8 例全绿（本链合计 28 例：T2+T3 12+T4 8+T5 8）。
  - 全链两遍逐字节一致：多提供方分组（groq+openrouter 同模型合并、priority 递增）→ 配置档 resolve（默认/coding/vision 按请求指定）→ 切换不改默认档行为。
  - 失败演练：PROFILE_NOT_FOUND / 未知提供方静默跳过（读操作不报错）。
  - 链复跑：T2+T3/T4 子进程复跑 exit 0。文档无密钥（needle 排除设计文档合法引用）。

## PR 门槛合规核对（repos.yaml default_pr_gate）

| 门槛 | 落实 |
|---|---|
| require_integration_point | 接入点全链演练：分组→配置档→按请求指定→切换→默认档不变 |
| no_missing_channel | 全链零网络；唯一子进程是链复跑测试自身 |
| no_duplicate_module | 只消费本链 T2+T3 产物，零新模块 |
| impl_requires_devlog | 本文件即开发记录 |

## 合规核对

- 零网络零子进程；示例全合成占位。只开 PR 不合并。
