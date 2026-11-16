# PRD-469/T1：Retry-After 深度解析设计

> PRD：adopted/PRD-469-ofr-retry-after-deep-parse.md（issue #2265）
> 借鉴源：freellmapi（MIT）——只借鉴思路，零代码复用。
> 日期：2026-11-17

## §1 与 060/T4 的关系

060/T4 已归一化 Retry-After **响应头**（delta-seconds 与 HTTP-date 两种形式）为秒。本 PRD 在此之上补**正文三路**：响应头（060 已有）、正文结构化字段、正文锚定自然语言短语。三路合并后取**第一个有值的结果**（响应头优先——最权威），正文只在响应头缺失时才解析。

## §2 三路解析

### 2.1 响应头（060/T4 已有，共享解析器）

delta-seconds（纯数字）与 HTTP-date（RFC 7231 格式）共用 `parse_retry_after_header(value, now)` 一个入口；HTTP-date 转秒（`max(0, date - now)`）；解析只跑一次（结果缓存）。

### 2.2 正文结构化字段（深度封顶遍历）

从响应正文 JSON 中递归搜索以下字段名（大小写不敏感）：
`retry_after` / `retry-after` / `retry_after_seconds` / `wait_seconds`
值必须可转 int/float；非数字跳过。**深度封顶**：递归最多 6 层，超深不进。
数组只取第一个匹配元素。

### 2.3 正文锚定自然语言短语

白名单短语表（只认固定形态，不做通用 NLP）：

| 短语模式 | 提取 |
|---|---|
| `try again in N seconds` | N 秒 |
| `try again in N minutes` | N×60 秒 |
| `retry after N seconds` | N 秒 |
| `wait N seconds and try again` | N 秒 |

正则：`(?:try again in|retry after|wait)\s+(\d+)\s+(seconds?|minutes?)`；
只认前 3 个匹配的数字（防恶意提示塞满数字）；`minutes` 自动 ×60。

### 2.4 合并规则

优先级：响应头 > 正文结构化 > 正文锚定短语。前一路有值直接返回，不走后路。

## §3 钳制与脱敏（§4 写死）

- 钳制：所有来源的等待时间一律钳制在 **0 ≤ seconds ≤ 86400**（24 小时）内——畸形或恶意提示无法永久停用密钥。
- 脱敏：解析结果只保留**数字**（秒数）。正文原文、匹配到的短语上下文、结构化字段的原始值**从不保留**在日志或 trace 里——日志只记 `retry_after_seconds=N, source=header|structured|text`。

## §4 返回类型

```json
{"schema": "retry-after-deep/v1",
 "retry_after_seconds": 120,
 "source": "header|structured|text",
 "clamped": false}
```
`clamped=true` 表示原始值超出 24h 被钳制；三路全无 → 返回 None。

## §5 落点

`src/open_free_router/retry_after_deep.py`：parse_retry_after_deep(status, headers, body_text, now)——供 resilience.py 的 429/402/401 处理路径调用（替换裸 retry_after 读取）。不改 060/T4 归一错误码表。
