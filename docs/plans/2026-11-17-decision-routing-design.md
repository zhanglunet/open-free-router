# PRD-476/T1：决策模型选路由设计

> PRD：adopted/PRD-476-ofr-decision-model-routing.md（issue #2300）
> 借鉴源：freellmapi（MIT）——只借鉴思路，零代码复用。
> 日期：2026-11-17

## §1 决策调用（T2）

小模型读请求 → 输出档位与置信度：

```json
{"schema": "decision-routing/v1",
 "decision_id": "d-001",
 "suggested_tier": "coding|vision|default",
 "confidence": 0.85,
 "reason": "请求包含代码关键词（占位）"}
```

决策只重加权本次请求（不改全局策略）；decision_fn 注入式（不发真实网络）。

## §2 按轮缓存（T3）

同一轮（同一 request_id）只决策一次；低置信（< 0.6）/超时（> 2s）/不可用均降级均衡（走默认全量路由，不偏科）。

## §3 成本护栏与降级（T4）

- 决策调用成本上限：decision_budget_per_hour（默认 100 次/小时，超限降级均衡）；
- 不可用时全链路降级均衡；
- 开关：decision_routing_enabled 默认 false——显式开启才用（同 Fusion 模式）。

## §4 与 PRD-202 边界

202 是固定规则意图解析（@引用/列选择等，规则先行）；本条是 LLM 决策模型选路（模型后判）。零交集。
