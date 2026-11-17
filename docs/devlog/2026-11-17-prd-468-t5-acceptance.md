# PRD-468/T5 开发记录：冷却阶梯与探测恢复验收试运行（2026-11-17）

> trend issue #2264。本链收官单（T1 #201 / T2 #202 / T3 #203 / T4 #204 / T5 本单）。

## 交付

- `tests/test_prd468_t5_cooldown_acceptance.py`：9 例全绿（本链合计 42 例：T2 10+T3 6+T4 9+T5 9）。
  - 真实监督回路全链两遍逐字节一致：连续 429 三次阶梯逐级升（90→120→600）→ 探测成功提前解除（阶梯清零）→ 成功一次清零回起点 → 重升 90。
  - freellmapi 回退清单场景：并行对冲（多槽独立阶梯）、402 直进 1 天不走阶梯、猜测性/实锤标签（speculative flag）、运维一键清除留痕。
  - 链复跑：T2/T3/T4 子进程复跑 exit 0。文档无密钥（needle 排除设计文档合法引用）。

## PR 门槛合规核对（repos.yaml default_pr_gate）

| 门槛 | 落实 |
|---|---|
| require_integration_point | 接入点全链演练：429 阶梯→探测→清零→重升端到端 |
| no_missing_channel | 全链零网络（probe_fn 注入）；唯一子进程是链复跑测试自身 |
| no_duplicate_module | 只消费本链 T2/T3/T4 产物，零新模块 |
| impl_requires_devlog | 本文件即开发记录 |

## 合规核对

- 不做健康评分/不改单次退避口径（非目标边界）；示例全合成占位。只开 PR 不合并。
