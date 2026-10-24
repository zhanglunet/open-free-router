# PRD-149/T1：failover 审计记录 schema

> trend issue #745。对应 PRD-149（中央仓库 zhanglunet/trend）。
> 地位：本文档是 PRD-149 的"宪法"（沿用 `docs/plans/failover-routing-policy.md` 的文档地位惯例）。T2（写入钩子）、T3（doctor 字段对齐）、T4（查询汇总）、T5（验收）均以本文为准；实现不得擅自增删字段。
> 红线：账本绝不写 API key / prompt / 响应正文 / 工具参数（PRD-149 非目标 + 仓库既有脱敏约定）；纯加法设计，不改变 PRD-059 已定型的换路策略与 `failover_decisions` 既有 10 列语义。
> 借鉴声明：审计形态借鉴 CopilotKit/OpenBot（MIT）的「事前决策 + 事后记录」模式，版权声明见中央 PRD-149 引用；本文及后续实现均为自研表述。

## 1. 现状衔接：账本是扩展，不是另起炉灶

仓库里已有的相关事实（本文所有设计都落在这些真实结构上）：

| 已有模块 | 现状 | 与本账本的关系 |
|---|---|---|
| `analytics.py` 表 `failover_decisions` | PRD-059/T4 已实现：`decision_id, at, request_id, from_candidate, to_candidate, reason, attempt, elapsed_ms, ttfb_ms, circuit`；`FAILOVER_REASONS` 枚举白名单；`SENSITIVE_PATTERN` 扫描 + `ExportSecurityError` 拒收；0600 / WAL / 30 天保留 | 账本的**决策行**直接复用，语义零改动 |
| `executor.py:_log_failover` | 换路时同步写一行决策（`analytics.record_failover_decision`） | T2 钩子的现成挂点 |
| `analytics.failover_decisions(days, model, reason, limit)` + `/api/failover-decisions` + 仪表盘 | 已有按时间/模型/原因的查询 | T4 在其上补 outcome JOIN 与汇总 |
| `quota.py:QuotaObservation` / `quota_view.py` | 额度档位 `sufficient/tight/exhausted/unknown/stale`、来源 `upstream_headers/event/never_measured`、新鲜度 1800s | 账本配额字段的事实源 |
| `cli.py:cmd_doctor --json` | 检查项形状 `{ok, severity, label, detail}`，覆盖 config/registry/协议兼容/路由配置/免费证据/客户端配置 | T3 doctor 结论字段的事实源 |
| `sandbox/audit.py` + `sandbox/redact.py`（PRD-079/T4） | JSONL 追加、0600/0700、轮转、`assert_no_secrets` 拒收、写失败不中断 | 脱敏与"审计永不中断主流程"哲学的仓库内先例 |
| PRD-131（有序后端 + doctor + 自动换路） | **待开工**，仓库内无对应模块 | 钩子先对 stub（§6） |

差距即本 PRD 要补的三件事：**只有决策没有结果**（换路后新候选表现如何，账本不知道）、**没有 doctor 结论与配额档位**（"为何换"缺上下文）、**没有成对完整性与汇总视图**（T5 验收"3 次换路账本完整"无从断言）。

## 2. 成对审计记录 schema：决策行 + 结果行

### 2.1 形状总则

- **一对记录 = 一行决策 + 一行结果**，以既有 `decision_id`（uuid hex）为配对键：决策行在"切往下一候选**之前**"写入（现有 `_log_failover` 调用点），结果行在"新候选**落定**"时写入。借鉴形态即此：事前记决策，事后记结果。
- **决策行**：复用 `failover_decisions` 表，新增 8 列（§2.2）。
- **结果行**：新建表 `failover_outcomes`（§2.3），与决策行同库 `usage.db`、同 0600/WAL/保留策略，不新增配置项（沿用 `AnalyticsStore` 的启用与 30 天保留）。
- **迁移**：`PRAGMA user_version` 1→2。存量库走 `ALTER TABLE failover_decisions ADD COLUMN ...`（SQLite 加列不损行）；新库直接按新形状建表。**实现警示（给 T2/T3）**：现有 `_initialize()` 对列集合做严格相等校验、不符即整体 `enabled=False`，迁移必须发生在校验之前，否则存量用户的分析存储会被整体禁用。
- 首字节边界（宪法原则 1）不变：已向客户端发出首字节后的失败**不产生任何账本记录**，只透传。

### 2.2 决策行新增列（`failover_decisions`，全部可空、写入时经 allowlist + 脱敏门禁）

| 列 | 类型 | 来源 | 说明 |
|---|---|---|---|
| `requested_model` | TEXT ≤256 | `RouteDecisionStore.record(requested_model)` 同源 | 客户端请求的模型 ID（请求上下文摘要之一） |
| `strategy` | TEXT ≤64 | 同上 `strategy` | 选路策略名 |
| `quota_band` | TEXT 枚举 | `quota_view` 的 `band` | 失败候选当时的额度档位：`sufficient/tight/exhausted/unknown/stale` |
| `quota_source` | TEXT 枚举 | `quota_view` 的 `source` | `upstream_headers/event/never_measured` |
| `quota_classification` | TEXT 枚举 | `QuotaObservation.classification`，仅取 `rate_limited/quota_exhausted/credits_exhausted/credential_invalid`，否则空 | 失败候选 credential 的事件分类 |
| `doctor_verdict` | TEXT 枚举 | T3 前恒 `never_run` | 最近一次 doctor 结论：`pass/warn/fail/never_run` |
| `doctor_failed_checks` | INTEGER | T3 前恒 NULL | 最近一次 doctor 的 error 级检查计数 |
| `doctor_at` | TEXT | T3 前恒 NULL | 最近一次 doctor 运行时间（ISO8601 UTC） |

既有 10 列语义零改动；`from_candidate`/`to_candidate`（`provider/model` 的 canonical_id）即 PRD 所说的"前/后后端"。

### 2.3 结果行新表 `failover_outcomes`

| 列 | 类型 | 说明 |
|---|---|---|
| `outcome_id` | TEXT PK | uuid hex |
| `decision_id` | TEXT NOT NULL | 配对键 → `failover_decisions.decision_id` |
| `settled_at` | TEXT NOT NULL | 结果落定时间（ISO8601 UTC） |
| `outcome` | TEXT NOT NULL 枚举 | `success`（新候选收到首字节）/ `failed`（新候选也失败，链上后续决策另起一对）/ `no_candidate`（`to_candidate` 为空，无路可换）/ `budget_exhausted`（总预算 300s 耗尽，宪法 §2.3） |
| `status_code` | INTEGER | 新候选的 HTTP 状态码；无则为 0 |
| `error_kind` | TEXT | 失败时的 `FailureDecision.kind` 枚举，白名单同 `FAILOVER_REASONS`，越界一律折叠为 `unknown`（不存错误原文） |
| `ttfb_ms` | REAL | 新候选首字节耗时 |
| `total_latency_ms` | REAL | 完成时回填（沿用 `RouteDecisionStore.complete` 的回填模式） |
| `attempt` | INTEGER | 新候选是第几次尝试（通常 = 决策行 `attempt + 1`） |

Token 用量**不重复入账本**：`usage_events` 已按 request_id 记录 input/output tokens，汇总时 JOIN 取用，符合最小化原则。

### 2.4 成对完整性规则（T2 实现契约、T5 验收锚点）

| 场景 | 决策行 | 结果行 |
|---|---|---|
| 换候选成功 | 切前写入 | 新候选首字节到达写 `success`，完成时回填 `total_latency_ms` |
| 换候选后新候选又失败 | 切前写入 | 写 `failed` + `error_kind`；下一次转移生成新的一对 |
| 无可用候选 | 切前写入（`to_candidate` 为空） | 写 `no_candidate` |
| 总预算耗尽 | 切前写入 | 写 `budget_exhausted` |
| 决策行超时无结果 | — | 汇总视图按 `total_budget_seconds(300s) + 60s` 宽限判 `incomplete`，**不回填、不编造** |

### 2.5 示例（占位符，不含真实主机名/账号/密钥）

```json
{"record":"decision","decision_id":"0f1e2d3c4b5a","at":"2026-10-23T12:00:00+00:00",
 "request_id":"ofr_example_001","from_candidate":"<provider-a>/<model-x>",
 "to_candidate":"<provider-b>/<model-y>","reason":"quota_exhausted","attempt":1,
 "elapsed_ms":812.4,"ttfb_ms":null,"circuit":null,
 "requested_model":"<model-x>","strategy":"quality",
 "quota_band":"exhausted","quota_source":"event","quota_classification":"quota_exhausted",
 "doctor_verdict":"never_run","doctor_failed_checks":null,"doctor_at":null}

{"record":"outcome","outcome_id":"9a8b7c6d5e4f","decision_id":"0f1e2d3c4b5a",
 "settled_at":"2026-10-23T12:00:01+00:00","outcome":"success","status_code":200,
 "error_kind":"","ttfb_ms":640.1,"total_latency_ms":2103.7,"attempt":2}
```

## 3. doctor 结论与配额状态字段对照表

### 3.1 doctor 输出 → 账本字段（T3 的对齐契约）

现状 `cmd_doctor` 的检查项形状 `{ok, severity, label, detail}` 是唯一契约；账本只存**汇总**，不存 label/detail 明文（detail 可能含本机路径等敏感信息，且自由文本是脱敏红名单的天敌）。

| doctor 现有检查组（cli.py `cmd_doctor`） | severity 归类 | 落入账本字段 |
|---|---|---|
| config.yaml / registry.yaml / providers / keys / models / proxy / ui 可达性 | error → 计入失败数；warn_only → 不计入 | `doctor_failed_checks`（error 计数） |
| 协议兼容矩阵 `protocol_matrix` 的 issues | error/warning | 同上，按 severity 计入 |
| 路由配置诊断 `diagnose_routing_config` | error/warning | 同上 |
| 免费证据 `free_tier` 状态 `invalid/expired/unverified` | error/warning | 同上 |
| 客户端配置存在性 | 提示性 | 不入账本 |
| 汇总 | 任一 error → `fail`；仅 warning → `warn`；全过 → `pass` | `doctor_verdict` + `doctor_at` |
| PRD-131 新增的「有序后端 + doctor」检查 | 沿用 `{ok, severity, label, detail}` 形状追加 | **字段不变**，只增检查项不改账本列（§6 第 3 条） |

`doctor_failed_checks` 只存 error 计数、不存明细列表：明细留在 `doctor --json` 输出里，账本按时间戳关联即可复查。

### 3.2 quota 模块 → 账本字段

| quota 模块字段（`quota.py` / `quota_view.py`） | 账本字段 | 入/不入及理由 |
|---|---|---|
| `band`（sufficient/tight/exhausted/unknown/stale，tight = 比率 <0.10，与成本降权边界一致） | `quota_band` | 入：档位足以解释"为何换路" |
| `source`（upstream_headers/event/never_measured） | `quota_source` | 入：区分"有证据"与"措辞提示"（宪法原则 2：不按文案猜） |
| `classification`（事件分类） | `quota_classification` | 入：枚举折叠 |
| `dimensions.requests/tokens` 的 limit/remaining/reset_at 数字 | — | **不入**：数字留在本地面板（`/api/resilience` → `quota_view`）；账本只存档位，与 `public_quota_bands` 的最小化投影同一口径 |
| 新鲜度（`QUOTA_SIGNAL_MAX_AGE_SECONDS=1800` 派生 stale） | — | 不重复入：由 `quota_band=stale` 表达 |
| `Retry-After` 解析秒数 | — | 不单独设列：已体现在 `reason=rate_limited` 与结果行时序中，避免再开自由数字口子 |

## 4. 密钥字段红名单与写入门禁

### 4.1 红名单（撞线即拒收，绝不入账本）

| 红名单项 | 依据 |
|---|---|
| `authorization` 头及任何取值、`api_key`/`api-key` 字段名与值、`Bearer` 令牌、`sk-` 前缀字符串 | 既有 `analytics.SENSITIVE_PATTERN`（与 `sandbox/redact.py` 手动同步的既有约定） |
| prompt / 消息正文、response body、tool arguments | 同上；PRD-149 非目标 |
| 原始响应头（只允许归一化后的枚举与数字档位） | 宪法 §5 脱敏要求 |
| 错误原文（只允许 `FailureDecision.kind` 枚举） | 同上 |
| schema allowlist 之外的任何字段 | 新增：未在 §2.2/§2.3 列出的键一律丢弃，杜绝"顺手多记一个字段" |

credential slot 标识（`<provider>:slot-N`）**不在**红名单：不含密钥材料，且三层归因（provider/credential/model）需要它。

### 4.2 写入门禁（三层，全部沿用仓库既有机制）

1. **字段 allowlist**：写入前按 schema 白名单取键，多余键丢弃。
2. **模式扫描拒收**：序列化整行走 `SENSITIVE_PATTERN`，命中即抛 `ExportSecurityError` 拒收（沿用 `record_failover_decision` 的"拒收不掩码"——账本行本不该有自由文本，掩码反而掩盖 bug）。
3. **永不中断推理**：拒收/写失败只记 warning，主流程照常（`AnalyticsStore` 与 `sandbox/audit.py` 的共同哲学）。测试锚点：构造含 `api_key=<占位值>` 的行断言被拒、且请求不失败（T2 随钩子测试落地，T5 复验"无密钥入账"）。

## 5. 查询与汇总视图（CLI / 本地）

| 视图 | 形状 | 落点 |
|---|---|---|
| 明细查询 | `open-free-router failover-log --days N --model M --reason R --provider P --limit K --json`：decision LEFT JOIN outcome 按 `decision_id` 输出成对记录，缺结果行标 `incomplete` | `cli.py` 新子命令（`cmd_status`/`cmd_doctor` 同款 `--json` 风格）；查询 SQL 在 `analytics.py` 新增 `failover_pairs()`（复用 `failover_decisions()` 的过滤风格） |
| 汇总 | `open-free-router failover-summary --days N`：按 reason × provider/model 计数、配对完整率、`quota_band` 分布、切换耗时 `elapsed_ms` p50/p95 | 同上 |
| 仪表盘 | 既有 `/api/failover-decisions` 增补 outcome 列与 incomplete 标记 | `ui.py`（T4） |
| 公开边界 | 账本**只进本机**：不进 `catalog.json`、不进公开站点静态数据 | 沿用 PRD-096 额度面板的公开投影红线 |

过滤维度覆盖 PRD 验收"可按时间/后端过滤"：时间（`--days`）、后端（`--model` 命中 from/to candidate、`--provider` 取 candidate 的 provider 段）、原因（`--reason` 枚举）。

## 6. 与 PRD-131 换路实现的协同约定（钩子先对 stub）

1. **钩子接口（T2 定义，本文定型）**：`FailoverAuditHook` 两个方法——`on_failover_decision(**决策行字段)` 在 `classify_failure` 之后、切往下一候选之前调用（现挂点即 `executor.py:_log_failover`）；`on_failover_outcome(**结果行字段)` 在新候选落定时调用。
2. **stub 先行**：PRD-131 开工前，默认实现 `AnalyticsAuditHook` 直接写 `usage.db` 两表（现有换路路径即可产生完整成对记录，不必等 PRD-131）；另备 `NoopAuditHook` 供测试与关闭场景。工厂函数决定实现，调用点不随实现切换而改动。
3. **PRD-131 落地时的义务**：其有序后端换路实现必须调用同一钩子接口；`reason` 只准用 `FAILOVER_REASONS` 枚举（宪法 §1.3 口径不变，不新增自由文本）；其 doctor 新增检查项沿用 `{ok, severity, label, detail}` 形状，账本列不改。
4. **性能与安全契约**：钩子永不向推理路径抛异常；同步写路径软预算 <5ms（WAL 单行 INSERT）；先字节后永不补写任何账本记录（宪法原则 1）。
5. **保留策略**：结果行随决策行同一 30 天保留、同一 purge 事务清理（按 `settled_at`）。

## 下一步

确认本设计后进 T2（trend issue #746）：换路前决策 / 换路后结果写入钩子——按 §2.4 场景表实现 `on_failover_decision` / `on_failover_outcome`，含 §4.2 红名单门禁测试；T3（#747，doctor 字段对齐）与 T4（#748，查询汇总）按 §3 / §5 并行；T5（#749）以"模拟 3 次换路账本完整、无密钥入账"收口。
