# PRD-096 本轮开发记录（2026-10-21）

## 基线与恢复

读取 AGENTS.md、CLAUDE.md、中央 trend PRD-096、#56 全部设计评论、#58、#59 和 failover-routing-policy。刷新 origin 后基线为 `3610c3c`（#61 已合并）；未发现重复面板/策略开放 PR。原始工作区有用户未提交改动，本轮只在独立工作树和自己的分支工作。quota 维度采用中央 PRD 和 #61 owner 裁决：只进入 cost。

父任务确认 Mac 恢复 connected 后核对分支、提交和未提交 diff：基础 `4510132`（#66）、面板 `aa7fd6e`（#67）均保留，继续原策略改动，未重建或覆盖。父任务提供 CI 运行 `39644201412` 与基线运行 `39423455650` 的五项同源失败；本机全套复现相同五项。

## 可验证交付

1. #66 基础：过期/未来额度不输出 quota 数值，保留 stale；cost 为中性 0.5。固定时间测试覆盖 1800 秒含端点，槽内最小/eligible slots 最大、全耗尽/一个可用、terminal/cooldown/health；保留 cost=0 回归。回退修复后两个测试红，恢复后相关 49 项绿。
2. #67 面板：本地网关响应头/事件来源、观测时间、新鲜度、有效期、请求与 Token 重置时间。未知值 null/unknown，used 不做减法。五档合成数据本机浏览器渲染核查通过。公开投影只保留 provider/band，未把运行时数据接入公开静态构建。脚本 `scripts/render-quota-fixture.py` 可复现预览。
3. #59 策略：宪法 §9 统一故障/额度表；本机三 provider harness 跑 503→quota 429→200，并验证新额度不能解除 circuit/cooldown、480s 恢复、过期与缺失中性。Retry-After 0/120/121、无效值边界：旧代码在 0 秒测试失败，修复后通过。

## 五项基线失败的原因与处理

- 两项 executor 429 测试仍按 PRD-059 之前“立即切换/立即返回”计数；与现行默认同候选重试一次冲突。保留默认策略，改为精确断言 3 次/2 次以及两次同 slot 请求，并注入不等待的 sleeper。不是把次数改成宽松范围。
- 两项 Retry-After 透传测试：客户端 5s 超时，小于上游声明等待时间。透传测试显式关闭同候选重试，保留原状态码/头断言；默认重试语义由单独 policy tests 验证，未增大超时掩盖问题。
- `_timeout_phase` 文字启发式 helper 已被结构化 `TimeoutPhase` 替代，旧测试仍调用删除接口。改为经过 executor 的 connect/ttfb→TIMEOUT 及 refused/纯错误文案→TRANSPORT_ERROR 精确断言，不恢复按文字猜测。

相关 48 项已通过；策略/额度五套相关回归先通过 86 项（后增两项面板事件/API 合成测试单独 4 项通过）。

## 验证边界

首次全套：583 passed、16 failed。其中上述五项基线失败已修正验证；另 11 项来自既有 sandbox runner/hardening/acceptance 的平台限制。普通子进程诊断定位到 macOS `resource.setrlimit(RLIMIT_AS, ...)` 抛 `ValueError: current limit exceeds maximum limit`，代码注明该实现针对 Linux；本轮未修改该安全边界、未跳过或放宽其断言。后续非该平台限制相关的全套结果在 PR 描述记录。

测试连接守卫仅允许 loopback，不调用真实服务商；全程串行，无大规模并行构建、删除清理、凭据配置、版本发布、合并或部署。所有数据均为合成观测；#60 真实调用和耗尽验收仍需另行授权，本轮未执行。

最终复跑（仅排除原有 sandbox runner/hardening/acceptance 三个文件，未改它们的测试）：**527 passed in 90.77s**，覆盖其余全部 Python 套件，使用仅回环连接守卫。相关基线修复套件 **48 passed**，观测/面板/成本/韧性 **53 passed**。`git diff --check` 与 JS 语法检查通过。沙盒 11 项需在支持其 RLIMIT_AS 的 Linux 环境验证，不在 macOS 上绕过限制冒充通过。
