# PRD-472/T1：健康检查节流设计

> PRD：adopted/PRD-472-ofr-health-check-throttling.md（issue #2280）
> 借鉴源：freellmapi（MIT）——只借鉴思路，零代码复用。
> 日期：2026-11-17

## §1 目标

让健康检查只花该花的额度：该验的验，不该重复验的不验。

## §2 节奏与抖动（T2）

- 探测间隔：默认 300 秒（5 分钟），可配（health_check_interval_seconds）；
- 抖动：每次间隔加 ±10% 随机抖动（防 N 个提供方同时探测扎堆）；
- 同提供方探测间隔：两个提供方的探测至少间隔 30 秒（提供方间错开）；
- 3.5 分钟内验过的跳过（`last_probe_at + 210 > now` → skip）。

## §3 成本分级（T3）

校验本身是否烧额度按提供方分级：

| 级别 | 含义 | 节奏倍率 |
|---|---|---|
| free_verify | 校验端点免费（如 /models 列表） | 1.0（默认节奏） |
| costs_quota | 校验端点消耗额度（真实推理请求） | 3.0（降频三倍） |

分级表可扩展（新提供方添加条目即可）；分级表不认识的提供方按 costs_quota（保守——宁可降频不烧额度）。

## §4 预算与开关（T4）

- 每轮探测预算上限：probe_budget_per_round（默认 5 个提供方）；
- 并发上限：max_concurrent_probes（默认 2）；
- 总开关：OFR_HEALTH_PROBE_DISABLED=1 杀死探测任务（关闭后零探测零网络）；
- 探测不污染健康账本：探测结果写独立 probe_log.jsonl，不改 AnalyticsStore 正式表。

## §5 与 probe.py 的边界

probe.py 管探测逻辑本身（怎么发、怎么判健康/不健康）；本条管探测的调度策略（什么时候发、发几个、烧不烧额度）。probe.py 零改动。

## §6 非目标

不降低故障发现的及时性（分级只降重复校验，首次与异常后的校验节奏不变）；不做 freellmapi 式的密钥健康评分。
