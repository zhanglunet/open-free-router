# PRD-475/T1：Fusion 多模型并行研究与设计

> PRD：adopted/PRD-475-ofr-fusion-multi-model.md（issue #2295）
> 借鉴源：freellmapi（MIT）——只借鉴思路，零代码复用。
> 日期：2026-11-17

## §1 可行性研究结论

做，但默认关闭且成本护栏先于实现定稿。理由：
- 多模型并行分发+评审合成在某些场景（复杂推理/代码审查）有质量提升潜力；
- N 倍额度消耗是硬代价，必须默认关闭、显式开启、预算上限；
- 实现本身不复杂（并行分发+合成两步），但成本护栏的设计比实现更关键。

## §2 并行分发（T2）

```json
{"schema": "fusion-dispatch/v1",
 "request_id": "req-001",
 "targets": ["groq/llama-3.3-70b（占位）", "deepseek/deepseek-chat（占位）"],
 "results": [{"target": "groq/…", "ok": true, "answer": "…（占位）"},
             {"target": "deepseek/…", "ok": false, "error": "timeout"}],
 "budget_used": 1}
```

- 并行分发多模型（同一 prompt 发给 N 个模型）；
- 单路失败不拖垮整体（error 隔离在 result 内，不传播）；
- 超时有上限（per_target_timeout_seconds，T2 定 30s）。

## §3 评审合成（T3）

```json
{"schema": "fusion-synthesis/v1",
 "request_id": "req-001",
 "synthesized_answer": "评审模型合成结果（占位）",
 "sources": ["groq/llama-3.3-70b（占位）", "deepseek/deepseek-chat（占位）"],
 "degraded": false}
```

- 评审模型从多路草稿合成答案，附来源标注；
- 评审失败 → 降级返回最优单路（degraded=true），不空手而归。

## §4 成本护栏（T4）

| 护栏 | 规则 |
|---|---|
| 总开关 | fusion_enabled 默认 **false**——不显式开启就零 Fusion 调用 |
| 预算上限 | fusion_budget_per_request（默认 3 模型，可配） |
| 单请求模型数上限 | fusion_max_targets（默认 3，硬上限）——超过拒绝 |
| 超预算拒绝 | 超预算/超上限 → 明确拒绝（FUSION_BUDGET_EXCEEDED / FUSION_MAX_TARGETS） |

护栏不可绕过：disable fusion ≠ 超预算拒绝——前者是开关，后者是运行时硬限制。

## §5 验收标准（T5）

并行分发可用；单路失败有隔离；评审合成可用且来源可溯；评审失败降级返回最优单路；默认关闭；超预算明确拒绝。
