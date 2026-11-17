# PRD-468/T1：冷却阶梯与探测恢复设计

> PRD：adopted/PRD-468-ofr-cooldown-ladder-probe.md（issue #2260）
> 借鉴源：freellmapi（MIT）quotas-and-cooldowns 机制——只借鉴思路，零代码复用（TypeScript→Python）。
> 日期：2026-11-17

## §0 现状

resilience.py 的 ResilienceManager 429/402 后按 quota_cooldown_scale=8.0 一把梭指数退避：无分级、无原因标签、无提前恢复。好密钥撞一次限流就关 8 倍时长的小黑屋。

## §1 目标与非目标

目标：冷却分级（阶梯）、原因打标签（实锤 vs 猜测）、探测恢复（只提前解除猜测性）、运维上限与一键清除。

非目标（写死）：不改单次退避计算口径（059/T4）；不做密钥健康评分；不碰 quota.py 解析（PRD-469）；不做 UI。

## §2 冷却阶梯（T2）

| 触发 | 冷却 | 原因标签 | 猜测性 |
|---|---|---|---|
| 429 限流 | 阶梯：90s → 2min → 10min → 1h → 1天（连续 429 逐级升） | reason=429_limited, step=N | 是（上游没说恢复时间） |
| 402 欠费 | 冷 1 天 | reason=402_payment | 否（实锤——欠费不会自己好） |
| 401 鉴权失败 | 冷到下一健康周期 | reason=401_auth | 否（实锤——需人工换钥） |
| 成功响应 | **清零**：阶梯回到 90s 起点 | reason=success_reset | — |

规则写死：
1. 阶梯只对 429 逐级升：连续 429 次数 N → 冷却 = ladder[min(N, len(ladder)-1)]。成功一次 → N 清零。
2. 402/401 不走阶梯，直接钉死时长；它们的恢复靠 T3 探测或人工。
3. 原因标签随冷却落账（谁/何时/原因/阶梯步数/猜测性），可查。

## §3 探测恢复任务（T3）

- 每分钟扫一遍处于冷却中的凭证槽，挑出**猜测性冷却**（429_limited）发起探测。
- 探测 = 发一个最小请求（占位描述，T3 用注入式 probe_fn，不发真实网络）。
- 探测成功 → 提前解除冷却（阶梯步数清零）；探测失败 → **不延长**（等阶梯自然到点）；实锤冷却（402/401）绝不被探测。
- 每轮探测有预算（默认每轮最多 probe_budget=3 个槽），防止探测风暴。

## §4 运维上限与一键清除（T4）

- 冷却封顶：单槽冷却时长不超过 max_cooldown_seconds（默认 1 天，可配）；提供方明说 retry_after 的恢复时间不被缩短（retry_after 是实锤，优先于阶梯）。
- 一键清除：clear_all_cooldowns() 解除全部冷却（运维动作，落账留痕）。
- 按密钥精细重置：reset_slot(platform, model_id, slot) 只解指定槽。

## §5 与 PRD-059/T4 的边界

059/T4 管单次请求失败后的重试退避（毫秒级，executor 路径内）；本条管凭证槽级别的冷却阶梯（秒/分钟/小时级，跨请求状态）。两者叠加：059 先重试，重试仍 429 才进本条阶梯。

## §6 事务与留痕

- 冷却状态进 ResilienceManager 既有字段（cooldown_until/quota），阶梯步数与原因标签进新增 SupervisionLedger-式 JSONL 账本（cooldown_log，路径注入 append-only）。
- 零 LLM 零网络（probe_fn 注入式）；示例全占位。

## §7 T2~T5 衔接

T2 阶梯与原因标签（§2）；T3 探测恢复（§3）；T4 运维上限与一键清除（§4）；T5 验收（freellmapi 回退清单：并行对冲/客户端取消/账号被停/单方向限流四场景）。
