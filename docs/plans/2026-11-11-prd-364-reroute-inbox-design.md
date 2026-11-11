# PRD-364/T1：换路批准收件箱设计（推荐→生效之间的人工环节一等公民）

> trend issue #1643。对应 PRD-364（2026-11-11 日报第 7 条，用户「继续」按既定流水线立项）。
> 设计日：2026-11-11 · 目标仓库：zhanglunet/open-free-router
> 状态：T1 设计草案，待评审。仅设计文档（纯加法 .md）；收件箱内核（T2）、批阅闸门（T3）、划界断言（T4）、全链验收（T5）按本设计后续落地。
> 示例全部虚构占位，不出现真实主机名、IP、账号、密钥。

## 0. 红线

| # | 红线 | 落地方式 |
|---|---|---|
| R1 | **收件箱零自动执行** | approve/reject 只改条目状态留痕——approved **零待生效配置写**：人批只是记录，配置生效走既有显式 reload 流程（PRD-336/T3 settle 语义同族）；INBOX_AUTO_EXEC_ATTEMPT 拒绝配置载荷 |
| R2 | **只读汇聚** | 学习网关推荐换路项（带当时健康/配额快照）统一进待批清单——学习网关只出推荐红线不变（PRD-350 R2 同源） |
| R3 | **每项带证据** | 逐条 `{item_id, kind, source_ref, snapshot_digest（健康/配额快照）, recommendation, reason, state}` 缺一拒绝入箱——快照与 PRD-350 重放包指纹同源 |
| R4 | **账本 append-only + 拒绝留痕** | reject 留 by/at/reason（拒绝也留证据） |
| R5 | 占位符纪律 | T5 检查覆盖本文件 |
| R6 | 实现类 PR 只开不合并 | 等用户 review |

## 1. 借鉴对象、许可证与不复用红线

backnotprop/plannotator（Apache-2.0，实测）+ pingdotgg/t3code（MIT，实测）+ vercel-labs/agent-browser、agent-desktop（Apache-2.0，实测）——只借鉴「人留控制口」形态；条目 schema 按本仓路由域自研，零代码复用，不引入原仓依赖。

## 2. 与既有机制划界

| 维度 | PRD-364（本条） | PRD-201 看板 | PRD-350 重放包 | PRD-336 fallback_gate |
|---|---|---|---|---|
| 管什么 | 推荐→生效的**人工环节** | 看≠批 | 决策可重算 | 回填闸门 |
| 操作语义 | 人批/拒（零自动） | 只读 | 只读复算 | 结果回填 |
| 配置语义 | 零配置写（R1） | 零 | 零 | 账本回填 |

## 3. 收件箱设计

### 3.1 schema（T2 预埋）

```json
{"schema": "reroute-inbox/v1",
 "items": [{"item_id": "item-001",
   "kind": "reroute_recommendation",
   "source_ref": "routing-replay-pack/v1（占位）",
   "snapshot_digest": "sha16:…（当时健康/配额）",
   "recommendation": "backend-a → backend-b（占位）",
   "reason": "推荐理由（占位）",
   "state": "pending|approved|rejected", "decided_by": null, "decided_at": null}]}
```

### 3.2 状态机（确定性）

collect（学习网关推荐只读汇聚）→ pending；人 approve/reject 留痕；重复裁决/未知 id 显形拒绝。approved 是「人已批准」记录——配置写入与生效走既有显式流程。

## 4. 失败闸门（T3 落地）

| 错误码 | 触发 | 行为 |
|---|---|---|
| INBOX_ITEM_INCOMPLETE | 快照/推荐缺一 | 拒绝入箱 |
| INBOX_DOUBLE_DECIDE | 重复裁决 | 显形拒绝 |
| INBOX_AUTO_EXEC_ATTEMPT | 条目携带待生效配置载荷 | 拒绝（R1） |

- kind 首期封闭单项（reroute_recommendation），演进走 schema 版本 +1。

## 5. 衔接

T2 内核；T3 闸门（三码+R1 零配置写 AST）；T4 划界（snapshot_digest 与 PRD-350 同源断言+与 201/336 划界）；T5 样例回放+文档无密钥。

## 6. 开放问题

1. 推荐过期语义（快照过旧后 approve 是否仍有效——倾向过期显形需重新推荐，阈值待 T2 校准）；
2. 与 PRD-336 settle 的衔接（回填事件可否自动入箱——倾向只回填 failed/fallback_broken 项，待 T2）。
