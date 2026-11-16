# PRD-471/T5 开发记录：粘性会话验收试运行（2026-11-17）

> trend issue #2279。本链收官单（T1 #214 / T2+T3 #215 / T4 #216 / T5 本单）。

## 交付

- `tests/test_prd471_t5_sticky_acceptance.py`：10 例全绿（本链合计 28 例：T2+T3 10+T4 8+T5 10）。
  - 真实场景全链两遍逐字节一致：绑定粘性 → 窗口内粘住（T1） → 批注触发发回 → HandoffGenerator 生成交接说明（key_topics 连贯性）→ 切换到新模型 → 新粘性绑定。
  - 失败演练：超时重选/不可用立即失效/交接零密钥原文/空 topics 拒绝。
  - 链复跑：T2+T3/T4 子进程复跑 exit 0。文档无密钥（needle 排除设计文档合法引用——只借鉴思路口径）。

## PR 门槛合规核对（repos.yaml default_pr_gate）

| 门槛 | 落实 |
|---|---|
| require_integration_point | 接入点全链演练：绑定→粘住→批注发回→交接→切换→新粘性端到端验证 |
| no_missing_channel | 全链零网络；唯一子进程是链复跑测试自身 |
| no_duplicate_module | 只消费本链 T2+T3 产物，零新模块 |
| impl_requires_devlog | 本文件即开发记录 |

## 合规核对

- 交接说明不含用户原文与密钥（测试钉值）；粘性不可用立即失效（安全优先于粘性，测试钉值）。
- 示例全合成占位。只开 PR 不合并。
