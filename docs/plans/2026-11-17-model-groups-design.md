# PRD-470/T1：统一模型分组与命名回退链设计

> PRD：adopted/PRD-470-ofr-model-groups-fallback-profiles.md（issue #2270）
> 借鉴源：freellmapi（MIT）——只借鉴思路，零代码复用。
> 日期：2026-11-17

## §1 分组合并（T2）

同一逻辑模型在多个提供方都有（upstream_id 相同）时合并为一条分组：

```json
{"schema": "model-groups/v1",
 "groups": [{"model_id": "llama-3.3-70b",
   "members": [{"platform": "groq", "model_id": "llama-3.3-70b",
                "priority": 1},
               {"platform": "openrouter", "model_id": "llama-3.3-70b",
                "priority": 2}]}]}
```

- 分组键 = upstream_model_id（与 ResilienceManager 键口径一致）；
- 组内故障转移严格按 priority 递增；转移不改变模型身份——用户请求的模型不变；
- 合并后模型列表无重复（同 model_id 只出现一次）。

## §2 命名配置档（T3）

回退链可点名引用：

```json
{"profiles": {
  "coding": ["deepseek-chat", "claude-sonnet"],
  "vision": ["glm-5.2", "gpt-4o"],
  "default": ["deepseek-chat"]}}
```

- 配置档按序排列模型 id，路由器按序尝试；
- 按请求指定（request header `x-ofr-profile: coding`）优先于默认档；
- 默认档行为不变（不指定时走全量路由）。

## §3 与 PRD-202 换模型打通（T4）

- 换模型目标可指定配置档（target_profile=coding → 路由限制在 coding 档内）；
- 切换留 trace（audit.py 既有 trace 加 target_profile 字段）；
- 未确认不切换：换模型操作未获用户确认时零路由变更。

## §4 与 PRD-059 故障转移的边界

059 管单次请求失败后的备选提供方选择（executor 路径内）；本条管**跨请求的分组配置**（提供方集合的静态重组）。059 在分组内选择，分组在 059 外定义。

## §5 非目标

不改路由评分权重；不做能力分（intelligenceRank）；不做实时可用性探测。
