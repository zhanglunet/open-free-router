# PRD-201/T1：路由 trace 结构与看板信息架构设计

> trend issue #1005。对应 PRD-201（中央仓库 zhanglunet/trend，`adopted/PRD-201-ofr-routing-observability.md`）。
> 地位：本文档是 PRD-201 的"宪法"（沿用 `docs/plans/failover-routing-policy.md`、`docs/plans/2026-10-23-failover-audit-ledger.md` 的文档地位惯例）。T2（trace 采集落盘）、T3（实时看板）、T4（评估集与漂移）、T5（验收试运行）均以本文为准；实现不得擅自增删 trace 字段、改动落盘口径或看板信息架构，变更须回本文修订。
> 红线：trace **默认只存元数据，永不存提示词/响应正文/工具参数/密钥**（PRD 风险项 + 仓库既有脱敏约定，见 §3）；看板**只读聚合表，不实时扫全量**（PRD 风险项，见 §4）；本文不替代、不改写 PRD-149 审计账本（边界见 §6，写死）。
> 借鉴声明：形态借鉴 langfuse 的"trace + 评估集"思路。**langfuse 许可证本轮未核实，只借鉴思路，不复用其任何代码**；本文及后续实现均为自研表述。

## 0. 现状衔接：trace 是补面，不是另起炉灶

仓库里已有的相关事实（本文所有设计都落在这些真实结构上，2026-10-26 主分支核对）：

| 已有结构 | 现状 | 与本 PRD 的关系 |
|---|---|---|
| `telemetry.py:RouteDecisionStore` | 内存环形缓冲（最近 200 条），每请求一条决策记录：`request_id / requested_model / strategy / status / provider / model / attempts / rejected[] / scores[] / attempt_results[] / ttfb_ms / total_latency_ms / error_kind`；仅非成功请求经 `_persist_analytics` 落 `usage_events` | trace 的**字段事实源**：§2 的 trace 结构以它为骨架补齐落盘缺口，不另发明一套字段 |
| `analytics.py` 表 `usage_events` | 每请求一条用量事件（`provider/model/status/status_code/ttfb/total_latency/input_tokens/output_tokens/fallback_attempts/error_kind/circuit_breaker_events`），0600 / WAL / 30 天保留 | trace 与它**同库共存、按 `request_id`/`event_id` 可关联**；不合并、不改其语义（见 §2.4） |
| `analytics.py` 表 `failover_decisions` + `failover_outcomes`（PRD-149） | 只记**发生换路**的请求：决策行 + 结果行成对，枚举白名单 + `SENSITIVE_PATTERN` 扫描拒收，0600 / WAL / 30 天保留；已有查询 `analytics.failover_decisions(...)` 与 `/api/failover-decisions` | 审计账本，**本 PRD 只读关联、不写、不改**（§6 边界） |
| `executor.py:_log_failover` | 换路时同步写决策行 | T2 的 trace 写入挂点参考（trace 挂点在请求落定处，见 §3.1） |
| `costing.py`（PRD-059/T2） | `cost_factor` 相对成本因子 + 成本表（`provider-profiles.json` 的 free_tier / 成本配置） | trace 成本字段的**计算口径源**（§2.2），trace 只存快照值，不重算 |
| `latency.py`（PRD-059/T3） | 滑动窗口 p50/p95 延迟统计 | trace 只存本请求的实测延迟；窗口统计仍归 latency 模块，看板不重复实现 |
| 本地 UI 服务（`proxy.py` 报告 `ui` 地址，端口 9057）+ `/api/failover-decisions` | 已有本地仪表盘与决策查询接口 | 看板**挂在同一本地 UI**，新增"路由可观测"页（§5），不另起服务、不上公网站点 `site/`（`site/` 是对外静态站，只放脱敏后的公开数据，与本看板无关） |

现状的三个缺口，即本 PRD 要补的面：

1. **成功首选请求没有落盘 trace**：内存缓冲只留 200 条且重启即失，`usage_events` 经 telemetry 落盘的只有非成功请求——"路由分布""成本曲线"无从统计全量。
2. **候选集与评分不落盘**：`scores[]` 只在内存里，看不了"当时为什么选它、次选是谁、差多少"。
3. **没有聚合层与质量回归**：看板若直查明细表即违 PRD 性能风险项；固定评估集与漂移报告不存在。

## 1. 名词与总则

- **trace**：一个客户端请求在网关内的一次完整路由经历 = 一条 `route_traces` 记录（§2）。粒度 = **每请求一条**，不是每次尝试一条；尝试级细节收进 trace 的 `attempts_detail` 子结构。
- **候选集（candidates）**：本次选路时 RoutePlanner 实际排序过的候选及其评分快照（含被排除者及排除原因枚举）。
- **聚合（rollup）**：按时间桶预先算好的统计行（§4），看板唯一数据源（明细点查除外，见 §5.3）。
- **评估集（eval set）**：一组固定的提示词用例（§7），定期经本地代理重跑，比对路由结果与基线，产出漂移报告。评估流量在 trace 里以 `origin=eval` 标记，与真实流量分开统计。
- 总则：trace 是**可观测数据**，不是审计证据——可采样、可按保留期删除、聚合可由明细重建；审计语义只属于 PRD-149（§6）。

## 2. 路由 trace 结构

### 2.1 落盘决策：新建 `route_traces` 表，不扩展既有表

| 选项 | 结论 | 理由 |
|---|---|---|
| 扩展 `failover_decisions` | ✗ | 该表只覆盖"发生换路"的请求，且是 PRD-149 审计账本：列集合有严格相等校验、字段增删受 149 宪法约束；塞全量 trace 会污染审计语义并撞迁移红线 |
| 扩展 `usage_events` | ✗ | 该表是 用量事件（一行一请求但只有计数/时延列），候选集/评分是变长结构，硬塞列会把两类生命周期（用量 30 天 vs trace 明细短保留）绑死 |
| **新建 `route_traces` + `route_trace_rollups`** | ✓ | 与既有表同库（`usage.db`）、同 `AnalyticsStore`、同 0600/WAL 约定；独立保留期（§3.3）；经 `request_id` 与 `usage_events`/`failover_decisions` 关联，互不改写 |

### 2.2 字段表

类型为 SQLite 存储类型；"来源"指写入时的事实源；"落盘"指是否进 `route_traces`（全部字段均落盘，内存态字段单列说明）。

| 字段 | 类型 | 来源 | 说明 |
|---|---|---|---|
| `trace_id` | TEXT PK | 写入时生成（uuid hex） | trace 主键 |
| `request_id` | TEXT NOT NULL，索引 | proxy 请求上下文 | 与 `usage_events.event_id`、`failover_decisions.request_id` 的关联键 |
| `at` | TEXT NOT NULL（ISO8601 UTC），索引 | 请求落定时钟 | trace 时间（看板时间轴） |
| `origin` | TEXT 枚举 `live`/`eval` | 代理入口标记（§7.2） | 评估重跑流量与真实流量分开；默认 `live` |
| `requested_model` | TEXT ≤256 | 客户端请求 | 客户端点名的模型 ID（可能是别名/`auto`） |
| `strategy` | TEXT ≤64 | RoutePlanner | 本次选路策略名（与 telemetry/149 同源同值域） |
| `selected_provider` | TEXT | 最终落定候选 | 实际执行请求的 provider |
| `selected_model` | TEXT | 最终落定候选 | 实际执行的模型 canonical_id |
| `status` | TEXT 枚举 | 执行结果 | `success` / `failed` / `rejected`（入口拒绝，如鉴权/参数） |
| `status_code` | INTEGER | 上游/网关响应 | 最终返回给客户端的状态码 |
| `error_kind` | TEXT 枚举 | `translate.py` 归一错误码 / `FailureDecision.kind` | 失败原因归一枚举（与 060/T4 归一表同值域）；成功为 `""`。**只存枚举，永不存上游错误原文** |
| `attempts` | INTEGER | executor | 总尝试次数（含首选） |
| `failover_count` | INTEGER | executor | 换路次数 = `attempts - 1`（成功首选为 0） |
| `ttfb_ms` | REAL 可空 | executor 计时 | 最终成功尝试的首字节延迟；无首字节为 NULL（不记 0 冒充，沿用 PRD-059/T3 口径） |
| `total_latency_ms` | REAL 可空 | executor 计时 | 请求总耗时（入口→落定） |
| `input_tokens` / `output_tokens` | INTEGER 可空 | 上游 usage 回报 | 上游未回报为 NULL，不估算冒充 |
| `cost_factor` | REAL 可空 | `costing.cost_factor` 写入时快照 | 选中模型当时的相对成本因子 |
| `cost_units` | REAL 可空 | 写入时计算：`cost_factor × (input_tokens + output_tokens)` 的口径在 T2 定死并回填本文 | 相对成本单位（**不是货币金额**；免费额度网关无真实账单，看板文案不得写成金额） |
| `candidates` | TEXT（JSON）≤8KB | RoutePlanner 评分快照 | 候选集与评分，结构见 §2.3；超限截断为 Top-N + 计数（见 §2.3 上限规则） |
| `attempts_detail` | TEXT（JSON）≤4KB | executor 尝试序列 | 每次尝试 `{candidate, error_kind, ttfb_ms, elapsed_ms}`，只存枚举与数值 |
| `circuit_events` | INTEGER | resilience | 本请求内熔断开/合事件计数（与 `usage_events.circuit_breaker_events` 同口径） |
| `quota_band` | TEXT 枚举 可空 | quota_view 快照 | 选中候选当时的额度档位（与 149 决策行同值域）；仅作可观测上下文，**审计口径以 149 为准** |
| `prompt_meta` | TEXT（JSON）≤512B | 请求信封统计 | **只有计数与长度分桶**：`{messages: n, input_chars_bucket: "0-1k｜1k-4k｜4k-16k｜16k+", tools: n, stream: bool}`。无任何内容、无哈希（见 §3.2） |

### 2.3 `candidates` 子结构

```json
[
  {"candidate": "<provider>/<model>", "rank": 1, "selected": true,
   "score": 0.83, "quality": 0.9, "cost": 0.6, "latency": 0.8,
   "excluded": null},
  {"candidate": "<provider>/<model>", "rank": 2, "selected": false,
   "score": 0.71, "quality": 0.85, "cost": 0.9, "latency": 0.5,
   "excluded": null},
  {"candidate": "<provider>/<model>", "rank": null, "selected": false,
   "score": null, "quality": null, "cost": null, "latency": null,
   "excluded": "circuit_open"}
]
```

- 分项权重口径沿用 PRD-059/T2 定型值（质量 0.5 / 成本 0.3 / 延迟 0.2），trace 只存**当时快照**，权重变更后不回改历史 trace。
- `excluded` 为枚举：`circuit_open` / `quota_exhausted` / `cost_excluded` / `context_too_small` / `unavailable` / `not_in_strategy`；枚举外的值写入时折叠为 `other`。
- 上限规则：最多存 **Top 10** 个已评分候选 + 最多 **10** 个被排除候选（各取排序最靠前者），其余只在尾部记 `{"truncated": true, "scored_total": n, "excluded_total": m}`；整个 JSON 超 8KB 时先减被排除候选，再减已评分候选，直至达标——保证任何单条 trace 大小有界。
- 子结构里只允许 ID、数值、枚举：写入前整行过 §3.2 的敏感扫描，命中即整行拒收（与 149 同门禁）。

### 2.4 与既有表的关系（一句话版）

`route_traces` 记**每个请求的全景**（可观测），`failover_decisions`/`failover_outcomes` 记**换路事件的审计对**（PRD-149），`usage_events` 记**用量计数**；三者同库、经 `request_id` 关联、互不改写。完整边界见 §6。

## 3. 采集与落盘口径

### 3.1 采集点与时机

- 写入点：请求**落定后**（成功响应完成 / 失败终结 / 入口拒绝）同步组装一条 trace，经 `AnalyticsStore` 的新方法 `record_route_trace(...)` 落盘——与 `record_failover_decision` 同一条"永不中断主流程"纪律：**写失败只记日志计数，绝不影响推理请求**（沿用仓库先例）。
- 数据来源全部是现成事实：telemetry 的 `RouteDecisionStore.record(...)` 入参 + executor 尝试序列 + costing 快照，T2 不新增任何推理路径上的计算（评分快照在选路时顺手留存，不重算）。
- `origin=eval` 的请求（§7）同样写 trace，但看板默认视图与聚合默认**排除** eval（可切换包含），避免评估流量污染真实分布。

### 3.2 隐私口径与脱敏规则（写死）

1. **永不落盘**：提示词正文、响应正文、工具参数、请求/响应头、API key、Authorization 值、客户端 IP。本节为红线，T2–T5 不得增设"可选存正文"开关。
2. **只存元数据**：`prompt_meta` 只含消息条数、字符数分桶（分桶边界见 §2.2）、工具个数、是否流式；不存内容、不存内容哈希（哈希在小空间提示词上可被字典反推，不作为脱敏手段）。
3. **枚举折叠**：`error_kind`、`excluded`、`quota_band`、`status` 等自由度字段一律白名单枚举，枚举外折叠为 `unknown`/`other`/`""`，杜绝上游错误原文夹带信息进库（沿用 149 的 `FAILOVER_REASONS` 折叠先例）。
4. **长度上限**：所有 TEXT 字段写入时截断到 §2.2 的上限；JSON 子结构按 §2.3 上限规则截断。
5. **整行敏感扫描**：整行 JSON 编码后过 `analytics.SENSITIVE_PATTERN`（现值含 `authorization|api[_-]?key|bearer|sk-…|prompt|response[_-]?body|tool[_-]?arguments`），命中即**整行拒收**并计数告警（与 149 的 `ExportSecurityError` 同门禁，不做"打码后照存"）。
6. **文件权限与位置**：trace 与既有表同在 `usage.db`（0600、目录 0700），不新增文件、不新增配置项开关以外的路径。

### 3.3 保留与体量

| 数据 | 保留 | 说明 |
|---|---|---|
| `route_traces` 明细 | **7 天** | 明细只服务近期点查与回放（T5 一周回放正好覆盖）；到期由 `AnalyticsStore.purge` 同事务删除 |
| `route_trace_rollups` 聚合 | **90 天** | 聚合行小、是看板与漂移对比的历史基线 |
| 漂移报告文件 | 长期（随仓库外状态目录） | 报告是结论性文档，不随 trace 过期（§7.4） |

保留期与 `usage_events`/149 账本的 30 天**有意不同**：trace 明细量最大、审计价值最低，故最短；任何一方调整保留期不联动其他方（§6）。

## 4. 聚合落盘（看板性能口径）

### 4.1 `route_trace_rollups` 表

按 **1 小时桶**聚合，维度键：`(bucket_at, origin, selected_provider, selected_model, status, error_kind)`；度量列：

| 列 | 含义 |
|---|---|
| `requests` | 桶内请求数 |
| `successes` / `failovers` | 成功数 / 发生换路的请求数 |
| `ttfb_p50_ms` / `ttfb_p95_ms` | 桶内首字节延迟分位（口径同 `latency.py` 的 `_percentile`） |
| `total_latency_p95_ms` | 总耗时 p95 |
| `input_tokens` / `output_tokens` | 桶内 token 合计（NULL 按未回报不计入，不按 0 估算——合计列另记 `token_reported` 计数以示覆盖率） |
| `cost_units_sum` | 桶内相对成本单位合计 |
| `failover_reasons` | JSON：本桶换路原因 Top 计数（枚举→次数，最多 10 项）——供"失败原因 Top"跨桶合并 |

### 4.2 聚合时机：增量滚动，不实时扫全量

- 每条 trace 落盘后**不**立即更新聚合（避免热路径写放大）；由 T2 实现一个**滚动聚合器**：每 60 秒把上次水位之后新增的 trace 增量并入当前/历史桶（水位 = 已聚合的最大 `at`+`trace_id`，存 `doctor_state` 式单行状态或独立 `rollup_state` 单行表，T2 二选一并回填本文）。
- 聚合可**全量重建**：删 rollups 后按明细重跑聚合器即恢复（明细保留 7 天内可重建；更早的桶以已聚合值为准，不可重建——这正是 trace"可重建"与审计"不可改写"的分界，§6）。
- 看板查询**只许**打 `route_trace_rollups` 与带时间窗+`LIMIT` 的 `route_traces` 点查（§5.3）；禁止任何对 `route_traces` 的无界全表扫描接口。

## 5. 看板信息架构

### 5.1 挂载位置

本地 UI 服务（端口 9057，与现有仪表盘同服务、同本地访问口径）新增页面 **「路由可观测」**，与已有 failover 决策视图并列（后者是 149 的查询面，本页不复制它，只在 trace 详情里给出跳转链接）。页面只读，无任何写操作、无配置修改入口。

### 5.2 区块划分

页面自上而下四个区块 + 一个明细抽屉：

| 区块 | 内容 | 数据源 | 刷新口径 |
|---|---|---|---|
| A. 路由分布 | 时间窗内各 `selected_provider/model` 的请求占比（条形/环形）+ 成功率 + 换路率；可切 `origin`（默认只看 `live`）、时间窗（1h/24h/7d） | `route_trace_rollups` 按维度汇总 | 页面打开时拉取 + 每 60 秒轮询（与聚合周期对齐，更快无意义） |
| B. 失败原因 Top | `error_kind` Top 10（次数 + 占比 + 环比上一窗涨跌），含换路原因 Top（来自 `failover_reasons` 合并） | `route_trace_rollups` | 同 A |
| C. 成本曲线 | 按小时桶的 `cost_units_sum` 曲线 + token 合计曲线（双轴）；标注"相对成本单位，非金额" | `route_trace_rollups` | 同 A |
| D. 质量漂移报告入口 | 最近漂移报告列表（时间、评估集版本、结论 `no_drift`/`drift`、漂移项数），点击进报告详情（§7.4 的落盘报告渲染）；无报告时显示"尚未运行评估"空态 | T4 产出的报告索引（状态目录下的报告文件清单，接口只读目录元数据） | 页面打开时拉取，不轮询 |
| E. trace 明细抽屉 | 由 A/B 区块点击某模型/原因下钻：时间窗内该维度最近 N 条 trace（默认 50，`LIMIT` 写死上限 200），展开单条看 `candidates` 评分表与 `attempts_detail`；含跳 149 决策视图的链接（带 `request_id`） | `route_traces` 带索引点查（`at` + 维度过滤） | 点击时拉取，不轮询 |

### 5.3 接口契约（T3 按此实现，路径为建议名，T3 可微调但须回填本文）

- `GET /api/route-observability/summary?window=24h&origin=live` → A+B+C 一次返回（全部来自 rollups）。
- `GET /api/route-observability/traces?window=24h&model=<id>&error_kind=<enum>&limit=50` → E 的点查；参数白名单校验，`limit` 超 200 按 200 截断。
- `GET /api/route-observability/drift-reports` → D 的报告索引；`GET /api/route-observability/drift-reports/<report_id>` → 单份报告内容。
- 全部接口只读、只查本地库/状态目录，不触发任何上游请求。

## 6. 与 PRD-149 审计账本的边界（写死）

> 本节是 PRD-201 验收的硬性条款（trend #1005）。后续任务与评审均以本节为准；与本节冲突的实现视为越界。

| 维度 | PRD-149 审计账本 | PRD-201 路由可观测（本条） |
|---|---|---|
| 定位 | **事后审计**：回答"这次换路为何发生、换得怎么样"，供事后复核与追责 | **实时可观测 + 质量回归**：回答"现在流量在怎么走、成本延迟如何、路由质量有没有漂移" |
| 覆盖范围 | 只记**发生换路**的请求（决策行 + 结果行成对） | 记**每个请求**一条 trace（含成功首选、无换路的大多数请求） |
| 真相源 | `failover_decisions` + `failover_outcomes` 是换路事实的**唯一真相源**；trace 里出现的换路相关字段（`failover_count`、`attempts_detail`、`quota_band`）只是可观测副本，**与账本不一致时以账本为准**，差异记为可观测侧 bug，不回改账本 | `route_traces` + `route_trace_rollups` 是流量/成本/漂移观测的真相源；账本不承担观测口径 |
| 写入纪律 | 追加写、成对完整性校验、字段受 149 宪法冻结 | 追加写但**可采样、可过期删除、聚合可重建**；字段受本文冻结 |
| 数据共用 | 两条线**共用同一数据库文件**（`usage.db`）与同一 `AnalyticsStore` 的权限/保留基础设施，**不共用表**；关联只经 `request_id` 只读 JOIN，任一线都不写对方的表 | 同左 |
| 保留期 | 30 天（审计口径，独立决定） | 明细 7 天 / 聚合 90 天（观测口径，独立决定）；互不联动 |
| 脱敏 | `SENSITIVE_PATTERN` + 枚举白名单（既有） | 复用同一 `SENSITIVE_PATTERN` 与枚举折叠纪律（§3.2），标准不降低、不另立一套 |
| 互不替代 | 账本不做实时看板数据源（其 30 天全量明细不是为看板聚合设计的） | 看板与漂移报告**不构成审计证据**；任何"审计/追责"用途必须回到 149 账本数据 |

一句话：**149 是账本（慢、准、不可改），201 是仪表盘（快、全、可丢弃重建）；共用一间库房，各记各的账。**

## 7. 固定评估集与漂移标注（为 T4 定契约）

### 7.1 评估集格式

- 载体：仓库内文件 `docs/eval/routing-eval-set.jsonl`（随仓库版本管理，变更走 PR——评估集是基线的一部分，不许运行时静默改）。每行一个用例：

```json
{"case_id": "eval-001", "version": 1, "tags": ["chat", "short"],
 "prompt": "<占位提示词：评估集内的示例文本，不含真实用户数据>",
 "expect": {"selected_in": ["<provider>/<model>", "<provider>/<model>"],
            "max_ttfb_ms": 8000, "must_succeed": true}}
```

- `expect.selected_in`：可接受的选中模型集合（路由是多候选权衡，断言"必须选 X"过脆，断言"落在可接受集合内"）；`must_succeed`：是否要求最终成功；`max_ttfb_ms` 可空。
- 评估集规模下限：首版 ≥20 条，覆盖短对话/长上下文/工具调用/代码四类标签（T4 落数并回填本文）。

### 7.2 重跑节奏与流量标记

- 触发：CLI 手动命令（T4 定名，建议 `eval-routing`）+ 可选定时（每日一次，由部署侧 cron/systemd 调用同一 CLI，本 PRD 不内置调度器）。
- 重跑经**本地代理正常入口**发请求（走真实选路，不走捷径直调 provider），请求带内部标记使 trace 落 `origin=eval`；标记方式（内部请求头/保留模型后缀）由 T4 定死并回填本文，红线：标记不得透传给上游。
- 每次重跑的并发与速率受限（顺序或低并发执行，避免评估流量挤占真实请求与上游额度），T4 定数值并回填。

### 7.3 基线与漂移判定

- **基线**：评估集每个 `version` 首次重跑的结果即为该版本基线（`baseline` 报告落盘，§7.4）；评估集或选路权重变更后 version+1，旧基线保留可查。
- 单用例漂移：`selected` 落出 `selected_in` 集合，或 `must_succeed` 用例失败，或 `ttfb_ms` 超 `max_ttfb_ms`。
- 整轮漂移判定（满足其一即 `drift`）：① 用例漂移率 > 10%；② 全集 p95 首字节延迟较基线上升 > 20%；③ 首选命中率（selected == 基线 selected 的比例）下降 > 5 个百分点。阈值是**默认口径**，T4 可在实现时附理由调整并回填本文，不许散落在代码里无文档。
- 漂移结论只标注、不自动改任何选路参数（与 PRD-229"只出推荐不自动执行"同精神：本 PRD 只负责看见漂移，处置归人）。

### 7.4 漂移报告

- 每轮重跑产出一份报告：Markdown（人读）+ 同名 JSON（机读，看板 D 区块消费），存本地状态目录 `drift-reports/`（不进仓库；报告含评估集内的提示词引用时只写 `case_id`，不复制提示词正文到报告外发面——仓库内归档报告时同样只写 case_id 与结论）。
- 报告字段：`report_id`、运行时间、评估集 version、基线指向（baseline report_id）、每用例结果（selected/ttfb/成败/是否漂移）、整轮判定（`no_drift`/`drift` + 触发的判定项）、与基线的 p50/p95 对比。

## 8. T2–T5 输入输出契约

### T2 trace 采集落盘（trend #1006）

- **输入**：本文 §2（字段表/子结构/上限规则）、§3（采集口径/隐私/保留）、§4（聚合表与滚动聚合器）。
- **输出**：`route_traces`、`route_trace_rollups` 两表与 `AnalyticsStore.record_route_trace(...)`；每请求一条 trace 落盘、字段完整（issue 验收）；trace 写入失败不影响请求（须有测试）；滚动聚合器把明细增量并入 rollups；`purge` 覆盖新表的 7 天/90 天保留。
- **不得做**：存任何正文/密钥（§3.2 红线）；改 `failover_decisions`/`usage_events` 既有列与语义；让看板式全表扫描接口提前出现。

### T3 实时看板（trend #1007）

- **输入**：本文 §5（信息架构/接口契约）+ T2 的 rollups 与点查能力。
- **输出**：本地 UI「路由可观测」页（A/B/C/D/E 五部分）与 §5.3 接口；看板数据与 trace 一致（issue 验收：同窗内看板聚合数与明细统计对得上，验收方法由 T5 落地）；成本区块明确标注相对单位。
- **不得做**：接口绕过 rollups 直扫明细全表；把页面挂到公网站点 `site/`；加任何写操作。

### T4 固定评估集定期重跑 + 漂移标注（trend #1008）

- **输入**：本文 §7（格式/节奏/判定/报告）+ T2 的 `origin=eval` trace。
- **输出**：首版评估集文件（≥20 条、四类标签）、重跑 CLI、基线与漂移报告（MD+JSON），漂移报告可生成（issue 验收）；§7.2/§7.3 中留给 T4 定死的数值（标记方式、并发、阈值如调整）回填本文。
- **不得做**：评估流量透传标记给上游；漂移结论触发任何自动调参；把真实用户提示词写进评估集。

### T5 验收试运行（trend #1009）

- **输入**：T2–T4 的全部产出 + 本文。
- **输出**：一周真实数据回放验收报告（沿用仓库验收报告惯例落 `docs/plans/`）：核对 trace 完整率（请求数 vs trace 数）、看板聚合与明细一致性、漂移报告可复核性（重跑一轮可复现同结论）、隐私抽查（全库敏感扫描零命中）；结论可复核（issue 验收）。
- **不得做**：为凑验收结论改判定阈值或评估集（变更须单独 PR 并使 version+1）。

## 9. 未决与诚实项

- `cost_units` 的精确公式（token 口径是否区分输入/输出权重）留 T2 定死回填；本文先定"相对单位、非金额"的文案红线。
- 聚合器水位状态的存放位置（独立单行表 vs 复用既有状态行）留 T2 二选一回填。
- §5.3 接口路径为建议名，T3 可微调但须回填本文，保持"summary/traces/drift-reports"三件套形状不变。
- 本文只做设计，不含任何实现代码；trace 字段的最终列名以 T2 落库时与本文 §2.2 的映射为准，如有出入 T2 须回填本文再开工。
