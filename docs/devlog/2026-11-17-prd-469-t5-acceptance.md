# PRD-469/T5 开发记录：Retry-After 深度解析验收试运行（2026-11-17）

> trend issue #2269。本链收官单（T1 #206 / T2+T3 #207 / T4 #208 / T5 本单）。

## 交付

- `tests/test_prd469_t5_retry_acceptance.py`：6 例全绿（本链合计 31 例：T2+T3 16+T4 9+T5 6）。
  - 三路场景两遍逐字节一致：响应头（120s）/正文结构化（300s）/锚定短语（30s）逐一过 parse_retry_after_deep。
  - 恶意钳制：999999999→86400+clamped=true。
  - 正文原文零保留：结果不含 "sensitive"/"internal error" 任何正文片段。
  - 链复跑：T2+T3/T4 子进程复跑 exit 0。文档无密钥（needle 排除设计文档合法引用——占位字面改为只借鉴思路口径）。

## PR 门槛合规核对（repos.yaml default_pr_gate）

| 门槛 | 落实 |
|---|---|
| require_integration_point | 接入点全链演练：三路场景逐一过 parse_retry_after_deep |
| no_missing_channel | 全链本地零网络；唯一子进程是链复跑测试自身 |
| no_duplicate_module | 只消费本链 T2+T3 产物，零新模块 |
| impl_requires_devlog | 本文件即开发记录 |

## 合规核对

- 畸形/恶意提示无法永久停用密钥（24h 钳制，测试钉值）。
- 正文原文从不保留进日志/trace（结果键集封闭，测试钉值）。
- 示例全合成占位。只开 PR 不合并。
