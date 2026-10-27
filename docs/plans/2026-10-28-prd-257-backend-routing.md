# PRD-257/T1：HarnessRouter 式后端统一路由协议设计

- 日期：2026-10-28
- 来源：2026-10-27 趋势日报 open-free-router 第 1 条（⭐），用户当日拍板采纳
- trend issue：[zhanglunet/trend#1285](https://github.com/zhanglunet/trend/issues/1285)
- 中央 PRD：`trend/adopted/PRD-257-ofr-harnessrouter-backend-routing.md`
- 状态：T1 设计稿，待评审；实现类任务（T2–T4）只开 PR 不合并

## 1. 目标与非目标

**目标**：模型后端（既有 provider 系 + BYOS 本地系 + 未来新增后端）之上建**统一协议层**：会话、流式、取消、结构化失败同一套合同；客户端（CLI/MCP/站点）只面向协议，**不随后端换而重写**；后端切换走配置，切换前后留对比记录。

**非目标**：
- 不改各 provider 的协议适配器本身（`anthropic.py` 等保留）——路由层在它们之上，是翻译层不是替代层；
- 不做执行隔离与凭据管理（沿用既有 auth/config 纪律）；
- 不做计量计费变更——usage 透传给既有 costing/llmusage。

## 2. 与既有能力的划界

| 既有能力 | 关系 |
|---|---|
| PRD-173 BYOS 接入 | 173 解决"接得进来"（后端注册）；257 解决"接进来之后统一用"（协议层），BYOS 后端是路由表的一种 backend |
| PRD-230 本地热切换 | 230 管本地模型不断线的切换可用性；257 的切换语义包含它（本地后端同走统一合同） |
| PRD-258 插件安检 | 安检是接流量前准入；257 是准入后的统一使用层，互为上下游 |
| 既有 failover/failover_audit | failover 是故障切换；257 路由是常态选择 + 切换留痕——failover 审计事件格式对齐复用 |

## 3. 方案设计

### 3.1 协议核心（`backend-route-v1`）

```
RouteConfig     := { route_id, backend_id, model, base_url_ref, timeout_ms, retry }
ChatSession     := { session_id, backend_id, model, created_at, persistent }
Request         := { request_id, session?, messages, stream: bool, route: RouteConfig, cancel_token? }
StreamChunk     := { request_id, seq, type: delta|usage|done|failed|cancelled, payload }
Response        := { request_id, status: ok|failed|cancelled, content?, usage?, failure?: StructuredFailure }
StructuredFailure := { class: connect|auth|rate_limit|timeout|protocol|backend_error|cancelled, code, message, retryable, backend_detail }
```

- 客户端唯一可见面 = `Request → StreamChunk* | Response`；后端差异全部被适配器吸收；
- `StructuredFailure.class` 封闭七类，`backend_detail` 过既有脱敏后透传；
- `cancel_token`：取消是协议操作（有 ack 语义），不是客户端单方面断连。

### 3.2 适配器与配置路由（T2）

- 每后端一个协议适配器：`probe() / open_session() / send(Request) → chunks / cancel(request_id)`；`probe()` 返回能力位（stream/persistent/cancel），缺失能力如实降级标注；
- `routes.json`（对齐既有 `provider-profiles.json` 纪律）：

```json
{
  "schema": "backend-route-v1",
  "routes": [
    { "route_id": "default",  "backend_id": "<既有 provider id>", "model": "<...>" },
    { "route_id": "local",    "backend_id": "byos-local", "model": "<...>" }
  ],
  "default_route": "default"
}
```

- 客户端（CLI/MCP/站点）只引用 `route_id`；T2 验收 = 同一客户端调用在两个后端间切换零代码改动。

### 3.3 会话/流式/取消/失败合同（T3）

- 会话：`persistent` 会话跨请求复用（后端支持时），session 过期/失联返回 `StructuredFailure(class: protocol)` 并提示重建；
- 流式：`seq` 单调；后端不支持流式时适配器合成单 delta + done（如实标注 `synthetic_stream: true`）；
- 取消：cancel 后必须收到 `cancelled` chunk 或 failure；超时未回记 `backend_error` 并清理本地句柄；
- 失败：六类+cancelled 全部走 `StructuredFailure`，客户端按 class 决定重试/换路——**重试策略在客户端，路由层不做隐藏重试**（可观测性优先）。

### 3.4 切换对比记录与审计（T4）

- 每次路由切换（配置生效/failover/手动）写审计事件（格式对齐 `failover_audit`）：`route_from/route_to/reason/actor/at`；
- 同一请求可 `compare([route_a, route_b])` 跑双路，结果（status/usage/延迟/失败类）存对比记录 JSONL，可机读导出；
- 对比记录只存元数据，不存请求正文（对话内容不进记录）。

## 4. 安全红线

1. 路由层不新增凭据面：base_url_ref 引用既有凭据/配置，不复制密钥进路由表；
2. `backend_detail` 过脱敏（无密钥/内网主机名）后才进失败对象与记录；
3. 对比记录/审计零请求正文（对话内容不落记录层）；
4. 配置与记录文件零密钥（示例占位符）；
5. stub 后端零网络，协议一致性测试基座。

## 5. T2–T5 任务拆分（按 PRD）

| 任务 | 内容 | 验收标准（PRD 原文口径） |
|---|---|---|
| T2 | 最小路由实现：≥2 后端（含 stub）配置切换 | 切换不改客户端代码，有测试 |
| T3 | 会话/流式/取消/结构化失败合同落地 | 合同一致性测试覆盖 |
| T4 | 切换前后对比记录与审计导出 | 记录可机读 |
| T5 | 验收试运行：双后端切换对比 | 对比记录齐全；文档无密钥 |

## 6. T5 验收标准

1. 同一客户端请求在两后端切换成功（零代码改动，仅 routes.json）；对比记录 status/usage/延迟/失败类齐全；
2. 流式 seq 单调；不支持流式的后端 synthetic_stream 标注；
3. cancel 有 ack；七类失败枚举封闭；backend_detail 脱敏扫描 0 命中；
4. 切换审计事件可机读导出，与 failover_audit 格式一致；
5. 记录层无请求正文（对话内容零落盘断言）；文档无内网主机名。

## 7. 借鉴划界

- [HarnessRouter/harnessrouter](https://github.com/HarnessRouter/harnessrouter)：License API 实测 **Apache-2.0**。仅借鉴"统一协议管全部后端、换后端改配置、切换留对比"的形态，不复制其协议定义与代码；本设计的事件模型/失败枚举/审计对齐为本仓原创（对齐既有 failover_audit 与 provider 纪律）。
- 与日报原文的差异修正：无。
