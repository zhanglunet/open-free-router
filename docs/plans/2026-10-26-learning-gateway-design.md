# PRD-229/T1 学习网关设计

> 对应 PRD：trend `adopted/PRD-229-ofr-learning-gateway.md`（experiential 式 BYOK 学习网关）
> 对应 issue：zhanglunet/trend#1145（T1）；T2 #1146 / T3 #1147 / T4 #1148 / T5 #1149
> 状态：设计稿，只开 PR 不合并，等评审
> 借鉴声明：借鉴对象 experientiallabs/experiential 的许可证**未核实**。本设计只借鉴「从真实流量里学成本与质量、产出带证据的推荐」这一**形态**，不读其代码、不复用其任何实现；文中全部口径均锚定本仓库现有代码（`analytics.py` / `costing.py` / `latency.py` / `routing.py` / `scoring.py`）。

## 0. 一句话定位

学习网关是一条**离线**链路：周期性地读网关已经落盘的流量元数据（`usage.db`），按「提示词类型 × 后端」汇总成本与质量，产出**带证据链的路由策略推荐**；推荐永远只是建议，**人工确认后才由独立的应用步骤写入路由配置**，全程留痕、可回滚。

它回答的问题是「用得省」：哪类请求走哪个后端最划算、失败率最低。它不回答「接得进来」（那是 PRD-173 BYOS 的事，见 §8），也不做实时展示（那是 PRD-201 的事，见 §7）。

## 1. 总体架构与数据流

```
                 ┌─────────────────────── 在线热路径（本 PRD 零改动）──────────────────────┐
  请求 ──► proxy ──► routing.RoutePlanner ──► executor ──► 上游
                     │  ▲
                     │  └──── routing 配置（policy 权重 / 候选顺序 / 成本参数）
                     ▼ 只写元数据
              analytics.AnalyticsStore ──► usage.db（0600，保留 30 天）
                     │
                     ▼ 只读（T2）          ┌── 推荐存储（独立文件，与路由配置物理隔离）
              汇总管道 ──► 聚合集 ──► 推荐生成（T3） ──► 推荐记录 status=pending
                                                            │
                                              人工确认（T4）▼ 带 confirmation 记录
                                                        应用器 ──► 写 routing 配置 + 留回滚点
```

设计原则：

1. **热路径零改动**：学习链路全部离线、只读 `usage.db`；不 import、不修改 `routing.py` / `executor.py` / `translate.py` / `resilience` 的任何在线行为（与 PRD-079 沙盒层的红线同款）。
2. **读写分离**：推荐生成器对路由配置**只有读权限**（读当前值用于生成「当前值 → 推荐值」）；唯一能写路由配置的组件是 T4 的应用器，且它只接受带有效确认记录的推荐（见 §6）。
3. **确定性**：同一份聚合集 + 同一份当前配置，推荐生成必须产出逐项一致的推荐（无随机、无墙钟依赖之外的不确定性）。这是 T5 回放可复现的前提。
4. **默认关**：整条链路由独立开关控制，默认关闭；关闭时与现状完全一致。

## 2. 流量采样口径

### 2.1 采样范围

数据源只有一处：`analytics.py` 的 `usage.db`，三张表各司其职：

| 表 | 在本设计中的用途 | 关键字段（现状） |
|---|---|---|
| `usage_events` | 成本/质量/延迟的主证据：每请求一行 | `timestamp, provider, model, status(success/failed), status_code, ttfb_ms, total_latency_ms, input_tokens, output_tokens, fallback_attempts, error_kind, circuit_breaker_events` |
| `failover_decisions` | 换路证据与请求侧上下文 | `requested_model`（用户请求的虚拟模型）、`from_candidate/to_candidate`、`reason`（枚举，见 `FAILOVER_REASONS`）、`strategy`、`quota_band` |
| `failover_outcomes` | 每条换路决策的最终结局 | `decision_id, outcome, status_code, error_kind, ttfb_ms, total_latency_ms` |

口径规则：

- **全量优先，不抽样**：`usage_events` 本就是每请求一行的全量元数据，量级为单机本地网关流量，聚合直接全量扫描窗口内事件，不做统计抽样。若未来单窗口事件数超过 T2 设定的聚合上限，允许分层抽样，但必须在聚合集里记录抽样率与原始计数，推荐证据链引用的是抽样后口径并明确标注——**不许静默抽样**。
- **窗口**：默认学习窗口 14 天，可配 7 / 14 / 30 天；上限受 `usage.db` 保留期 30 天约束（`AnalyticsStore(retention_days=30)`），窗口不得超过保留期。窗口以整日为界（UTC），起止时间写入聚合集，保证可溯源、可回放。
- **只计已结算事件**：窗口内 `usage_events` 中状态已落定的行计入；`failover_outcomes` 未结算（无 outcome 行）的决策不计入换路成功率分母。
- **客户端错误不算后端失败**：`status_code=400` / `error_kind=invalid_request` 的事件计入流量总量，但**不计入后端质量分母**——与现有换路策略一致（400 属请求侧问题，不触发换路、不归罪后端）。在聚合集中单列 `client_error_count` 以便核查。
- **探针/自检流量排除**：doctor 运行结论只存在于 `doctor_state` 单例表、不产生 `usage_events`，天然不在样本内；聚合口径不从 `doctor_state` 取数。
- **只用元数据，永不碰正文**：`usage.db` 按现有设计从不存提示词正文、响应正文、请求头与密钥（写入侧枚举化 + 导出侧 `SENSITIVE_PATTERN` 守卫）。学习链路继承这条边界：分类与指标只允许使用上表列出的元数据字段，**不新增任何正文采集**。

### 2.2 按提示词类型分类的口径

现状缺口（必须诚实写明）：`usage_events` **没有**提示词类型字段，也没有 `requested_model` 字段——网关从不落盘正文，因此**语义级分类（「这是代码问题还是翻译问题」）在本设计中不可能、也不应做**。本设计定义的「提示词类型」是**请求形态分类**：只由请求元数据特征判定，分类器在聚合时（T2）按下列规则派生，规则版本写入聚合集。

分类维度（每条事件派生一组标签，互斥主类 + 独立分档）：

**主类 `prompt_class`**（按优先级从上往下命中即停）：

| 优先级 | 类别 | 判定（仅用元数据） |
|---|---|---|
| 1 | `tool_use` | 该请求的换路上下文或事件表明带工具调用——注：现状 `usage_events` 无此字段，见下方「分类数据缺口」 |
| 2 | `long_context` | `input_tokens ≥ 32768` |
| 3 | `chat_long` | `8192 ≤ input_tokens < 32768` |
| 4 | `chat_short` | `0 ≤ input_tokens < 8192` |
| 5 | `unknown` | `input_tokens` 缺失（NULL）且无其他可判定特征 |

**独立分档**（与主类正交，一并记录）：输入长度档 `input_band ∈ {S<2k, M 2k–8k, L 8k–32k, XL≥32k}`、输出长度档 `output_band`（同阈值作用于 `output_tokens`）、是否经历换路 `had_fallback ∈ {0,1}`（`fallback_attempts > 0`）、请求侧虚拟模型 `requested_model`（仅能从 `failover_decisions` 关联得到的事件才有，见下）。

**分类数据缺口与 T2 的处理（写进 T2 契约，见 §10）**：

- `tool_use` 与 `requested_model` 在 `usage_events` 中无字段。T2 有两条可选路径，**由 T2 设计时定夺、本设计只给约束**：(a) 保守路径——首期只用 token 分档分类（主类退化为空缺 `tool_use`，`requested_model` 维度只覆盖有换路决策关联的事件并在聚合集中标注覆盖率）；(b) 补字段路径——在事件记录侧补一个只存枚举的 `prompt_class` 字段（在请求路径内存中按特征判定、只落枚举，延续现有「只存枚举与元数据」的惯例）。无论选哪条，**分类覆盖率（可分类事件占比）必须出现在每份聚合集与每条推荐的证据链里**，覆盖率低于 50% 的分类维度不出推荐。
- 分类规则一旦变更，`classifier_version` 递增；跨版本聚合集不得混合生成同一条推荐。

### 2.3 样本量门槛（PRD 风险项，写死为规则）

PRD 的风险条目——「流量样本偏差导致推荐误导」——在本设计中落实为**硬规则**，推荐生成器不得绕过：

| 规则 | 阈值 | 不满足时 |
|---|---|---|
| R1 分组最小样本 | 单个（`prompt_class` × provider × model）分组窗口内已结算事件 `n < 30` | 该分组只进观察报告，**不出推荐**，原因记 `insufficient_samples` |
| R2 对比双方样本 | 对比型推荐（建议把某类流量从 A 挪到 B）要求 A、B 两侧各 `n ≥ 30` | 不出推荐，记 `insufficient_samples` |
| R3 时间覆盖 | 样本须覆盖窗口内 ≥ 3 个不同自然日 | 不出推荐（防单日异常/单次故障污染），记 `insufficient_day_coverage` |
| R4 效应量门槛 | 差异须达到：成功率差 ≥ 5 个百分点，或 TTFB p95 差 ≥ 20%，或单位成本差 ≥ 15%（指标定义见 §3） | 不出推荐，记 `effect_below_threshold`（差异太小，挪动不值得） |
| R5 成本数据覆盖 | 成本型推荐要求分组 `token_coverage_rate ≥ 0.5`（口径同 `summary()`） | 成本型推荐不出（质量/延迟型不受此限），记 `insufficient_token_coverage` |
| R6 分类覆盖 | 所用分类维度覆盖率 < 50%（见 §2.2） | 该维度不出推荐，记 `insufficient_class_coverage` |

说明：本设计用「最小样本 + 最小效应量」代替统计显著性检验，不引入 p 值——本地网关流量量级下效应量门槛更直白、也可复核；这是刻意的取舍，评审时可议。所有「不出推荐」的结果都要落成观察记录（含触发的规则编号），让人能看见「为什么没推荐」，而不是静默无输出。

## 3. 成本与质量指标定义

总原则：**与现有口径对齐，不另造**。成本对齐 `costing.py`，延迟对齐 `latency.py` / `analytics.py` 的百分位算法，质量对齐 `scoring.py` 可观测因子。每个指标写明公式与数据来源。

先说清一个前提：本网关的「成本」不是货币价格。`costing.py` 的成本维度是**配额余量**（`cost_factor`：实时 `remaining/limit` 比值，低于 `downgrade_threshold=0.10` 时按 `downgrade_factor=0.5` 降权，无数据取 `missing_default=0.5` 不惩罚）；`docs/provider-profiles.json` 的 `free_tier` 只是文档性参考，运行时以实时配额信号为准，**未知数字绝不编造**（`costing.py` §4.3 “no evidence, no conviction”）。学习链路继承这一立场：成本指标全部是可观测的配额/Token 口径，并沿用 `summary()` 已有的诚实标注 `quota_usage_is_estimated=true`（「仅按本机观测估算，不代表厂商账单」）。

### 3.1 成本指标（按分组 = `prompt_class` × provider × model，窗口内聚合）

| 指标 | 公式 | 数据来源 |
|---|---|---|
| `tokens_per_success` 单位成功成本 | `(Σ input_tokens + Σ output_tokens) / 成功事件数`；Token 缺失事件按 `token_coverage_rate` 标注，不外推 | `usage_events.input/output_tokens, status` |
| `wasted_attempt_ratio` 换路浪费率 | `Σ fallback_attempts / 总事件数`——每次多余尝试都重复消耗配额，这是「用得不省」最直接的证据 | `usage_events.fallback_attempts` |
| `quota_exhausted_share` 配额耗尽占比 | 失败事件中 `error_kind` 对应配额耗尽（及 `failover_decisions.reason = quota_exhausted / credits_exhausted`）的占比 | `usage_events.error_kind`、`failover_decisions.reason` |
| `quota_band_at_decision` 决策时配额档 | 换路决策行 `quota_band` 的分布（仅作证据上下文，不单独成推荐依据） | `failover_decisions.quota_band` |

不做的事：不把 Token 换算成货币金额（无价格表、无账单数据，换算即编造）；不跨 provider 比较「绝对成本」，只在同一 `prompt_class` 内比较相对单位成本。

### 3.2 质量指标

| 指标 | 公式 | 数据来源 / 对齐 |
|---|---|---|
| `success_rate` 成功率 | 成功事件数 / 已结算事件数（分母按 §2.1 剔除客户端错误） | 与 `summary()` 的 `success_rate` 同公式 |
| `failure_breakdown` 失败构成 | 失败事件按 `error_kind` 分组占比；`error_kind` 取值域与换路侧 `FAILOVER_REASONS` 枚举对齐 | `usage_events.error_kind`、`failover_decisions.reason` |
| `failover_rescue_rate` 换路挽救率 | 经历换路且最终成功的事件数 / 经历换路的事件数 | `usage_events.fallback_attempts, status`；与 `summary()` 的 `saved_failed_requests` 口径一致 |
| `circuit_breaker_events` 熔断事件数 | 窗口内求和，仅作风险上下文 | `usage_events.circuit_breaker_events` |

与在线口径的关系（写明差异，不假装一致）：在线选路的质量维度是 `scoring.py` 四因子合成（health 0.25 / success_rate 0.20 / latency 0.20 / quota 0.15 等 `DEFAULT_WEIGHTS`，经 `routing._quality_dimension` 归一）。离线学习**只复现其中可由落盘数据计算的部分**（成功率、延迟、配额相关），health/capability 等依赖实时探针的因子不在离线口径内；推荐文档与证据链中引用质量指标时必须注明「离线可观测口径」，不得冒充在线打分本身。

### 3.3 延迟指标

| 指标 | 公式 | 数据来源 / 对齐 |
|---|---|---|
| `ttfb_p50 / ttfb_p95` | 窗口内分组事件 `ttfb_ms` 的 nearest-rank 百分位 | 与 `analytics._percentile` 及 `latency.py` 的 `percentile` 同算法 |
| `total_p50 / total_p95` | 同上，作用于 `total_latency_ms` | 同上 |

注意：`latency.py` 的滑动窗口（最近 100 条 TTFB、内存态）是在线选路用的，**不落盘、不可回放**，学习链路不用它；离线延迟一律用 `usage_events` 落盘值重算。这保证 T5 回放时延迟证据可复现。

### 3.4 指标计算通则

- 百分位统一 nearest-rank；`round(…, 6)` 精度与现有聚合一致。
- 任何指标缺数据时输出 `null` + 原因，**不填默认值**（与 `cost_factor` 无数据取 `missing_default` 的在线口径刻意不同：在线打分必须有值才能排序，离线推荐没有证据就该留空并触发 §2.3 的门槛规则）。
- 聚合集每行必须带：窗口起止、分组键、各指标值、`n`、覆盖率字段（`token_coverage_rate`、分类覆盖率）、`classifier_version`、口径版本 `metric_version`。这是「可溯源」的落点：任一指标都能回到「哪段时间、哪批事件、哪版口径」。

## 4. 推荐可作用的参数面（白名单）

推荐只能指向下列参数，清单之外一律不推荐——这是「推荐值作用在哪些参数上」的穷举：

| 参数 | 现状位置 | 当前默认 | 推荐形态 |
|---|---|---|---|
| 三维权重 `policy_weights.quality / .cost / .latency` | `routing.py` `RoutingConfig.policy_weights`（`POLICY_WEIGHT_DEFAULTS`） | 0.5 / 0.3 / 0.2 | 全局或按虚拟模型给出新权重；单次调整幅度上限 ±0.10，归一后和为 1 |
| 候选首选（某虚拟模型/某 `prompt_class` 的首选后端） | `RoutePlanner.plan` 的候选排序（scored 策略下由打分决定、priority 策略下由配置顺序决定） | 按现有打分/配置 | 「把 `prompt_class=X` 的首选从 A 换成 B」——通过建议调整该虚拟模型的候选优先级表达，不直接改打分公式 |
| 成本降级阈值 `cost_downgrade_threshold` | `routing.py` → `CostConfig` | 0.10 | 在 [0.05, 0.20] 内给出新值 |
| 成本降级因子 `cost_downgrade_factor` | 同上 | 0.5 | 在 [0.3, 0.8] 内给出新值 |
| 最大尝试数 `max_attempts` | `RoutePlanner` 候选截断 | 现有配置值 | ±1 步进，范围 [1, 5] |

**明确排除（永不推荐、应用器拒绝执行）**：凭据与密钥槽位、熔断/韧性安全阈值（`resilience`）、超时策略（PRD-059 `TimeoutPolicy`）、注册表增删 provider/model、沙盒层任何配置。理由：这些是安全与可用性底线，不是「用得省」的优化面；学习证据也不支撑动它们。

## 5. 推荐格式（固定格式，无证据不给推荐）

每条推荐是一条结构化记录，字段固定、缺一不可；证据链字段缺失或样本量不达 §2.3 门槛时，生成器**只能**产出观察记录（`kind=observation`），不得产出 `kind=recommendation`。

```json
{
  "recommendation_id": "<uuid>",
  "kind": "recommendation",
  "generated_at": "<ISO-8601 UTC>",
  "status": "pending",
  "scope": {
    "prompt_class": "<class>",
    "virtual_model": "<virtual-model-id or null = 全局>"
  },
  "parameter": "<§4 白名单内的参数名>",
  "current_value": "<当前配置值>",
  "recommended_value": "<推荐值>",
  "evidence": {
    "window": {"from": "<date>", "to": "<date>", "days_covered": 0},
    "classifier_version": "<v>",
    "metric_version": "<v>",
    "groups": [
      {"provider": "<provider-a>", "model": "<model-a>", "n": 0,
       "success_rate": 0.0, "ttfb_p95_ms": 0.0, "tokens_per_success": 0,
       "wasted_attempt_ratio": 0.0, "token_coverage_rate": 0.0}
    ],
    "rules_passed": ["R1", "R2", "R3", "R4", "R5", "R6"],
    "source": "usage.db: usage_events + failover_decisions（窗口内全量）"
  },
  "expected_benefit": {
    "metric": "<受影响指标>",
    "from": "<当前值>",
    "to": "<按窗口流量静态估算的值>",
    "basis": "按窗口内该 scope 流量静态重放估算；不是承诺，实际效果以应用后观察为准"
  },
  "expires_at": "<generated_at + 7 天>"
}
```

规则：

- **证据链可复核**：`evidence.groups` 的每个数字都必须能从聚合集（§3.4 的溯源字段）逐项对回；预期收益只能用窗口流量静态估算并写明 `basis`，不许给无依据的百分比。
- **一条推荐只动一个参数**（权重调整视为一个参数组）；多参数组合必须拆成多条，逐条确认。
- **推荐有保质期**：`expires_at` 默认生成后 7 天；过期未确认自动置 `expired`，不得应用（流量已经变了，旧证据不再算数），需重新生成。

占位符示例（数字均为虚构示意，不对应任何真实流量）：

> 推荐 `#<id>`：`prompt_class=chat_short` 的首选后端由 `<provider-a>/<model-a>` 换为 `<provider-b>/<model-b>`。证据：窗口 `<from>~<to>` 内 A 组 n=412、成功率 0.931、TTFB p95 2400ms、单位成功成本 3100 tokens；B 组 n=388、成功率 0.972、TTFB p95 1500ms、单位成功成本 2600 tokens；R1–R6 全过。预期收益：按窗口流量静态估算，该类请求成功率 +4.1 个百分点、单位成本 −16%。

## 6. 人工确认与应用流程

### 6.1 状态机

```
pending ──确认──► confirmed ──应用──► applied ──回滚──► rolled_back
   │                  │                    │
   ├──拒绝──► rejected ├──确认失效──► stale └──观察期结束──► closed
   └──过期──► expired
```

### 6.2 确认前绝不生效的机制（三重）

1. **存储隔离**：推荐记录存放在独立的推荐存储（本地文件/SQLite，0600，目录 0700，沿用 `analytics.py` 的权限惯例），与路由配置物理分离。推荐生成器进程对路由配置**没有写句柄**——它连写错地方的机会都没有。
2. **应用器单闸门**：唯一能写路由配置的组件是应用器。应用器执行前必须校验：推荐 `status=confirmed`、存在确认记录、确认记录里的**证据哈希与推荐当前证据一致**（证据被重新生成/篡改则确认自动失效置 `stale`，必须重新确认）、未过期、四项中任一不满足即拒绝并留痕。
3. **确认是显式动作**：确认只能由操作员通过 CLI 显式执行（形如 `learning confirm <recommendation_id>`），无批量默认确认、无「生成即确认」的快捷路径；确认动作本身要求操作员身份标识（本地操作员名，占位符口径）并写入确认记录。

### 6.3 留痕（全程 append-only）

确认与应用共用一条只追加的审计流水（风格对齐 PRD-149 审计账本：只追加、不改写），每条事件含：

| 事件 | 必含字段 |
|---|---|
| `generated` | recommendation_id、证据哈希、生成口径版本 |
| `confirmed` | recommendation_id、确认人、确认时间、确认时证据哈希 |
| `rejected` | recommendation_id、拒绝人、时间、原因（自由文本可空） |
| `applied` | recommendation_id、应用时间、参数名、旧值、新值、回滚点引用 |
| `rolled_back` | 被回滚的 applied 事件引用、回滚人、时间、回滚后值 |
| `expired / stale` | 触发原因、时间 |

审计流水与推荐存储同权限（0600）；任何一步失败（含应用器拒绝）也要落一条事件——**失败也要留痕**，不许静默。

### 6.4 应用与回滚

- 应用 = 应用器按确认记录把单个参数从旧值写为推荐值，写入前先保存旧值快照（回滚点）；写入后重读配置校验值已生效，校验失败自动恢复旧值并留痕 `apply_failed`。
- 一次应用只对应一条推荐；多条推荐逐条应用、逐条留痕，不做事务性打包（避免半应用状态说不清）。
- 回滚 = 操作员显式执行 `learning rollback <applied_id>`，应用器把参数恢复为该次应用的旧值快照并留痕；回滚后推荐置 `rolled_back`，同一推荐不得二次应用（要再动，重新生成新推荐走全流程）。
- 应用后观察：应用事件落痕后，后续窗口的聚合会自然反映新策略的效果；本设计不做自动对比报告（那属于 PRD-201 的漂移标注或后续增强），但 `applied` 事件与窗口口径保证了人工复盘可对回。

## 7. 与 PRD-201 的划界（单独一节）

PRD-201 是「路由决策可观测看板」，本条是「离线学习与推荐」，两者共用数据、互不越界：

| 维度 | PRD-201（可观测看板） | PRD-229（本条，学习网关） |
|---|---|---|
| 时间尺度 | 实时/近实时：每次路由决策的 trace 即时可见 | 离线批处理：以天为最小窗口（7/14/30 天），周期性运行 |
| 产物 | 看板 UI（路由分布、失败原因 Top、成本曲线）+ 固定评估集漂移报告 | 结构化推荐记录 + 观察记录；**不做任何看板/UI** |
| 对策略的态度 | 只展示，不建议、不修改 | 给出建议值，但**只建议**；修改只发生在 T4 人工确认后的应用步骤 |
| 数据角色 | trace 的**生产者与展示者**（含 201 自行新增的 trace 采集落盘） | 数据的**消费者**：只读现成落盘数据做聚合学习 |
| 主动性 | 被动呈现等人看 | 主动产出推荐等人确认 |

**数据共用关系（写明）**：

- 本条的主数据源是**既有** `analytics.py` 的 `usage.db`（PRD-059/149 已落地的表），不是 201 的产物；201 未落地时本条照常工作。
- 若 201 的 trace 落盘先行落地，本条可以把 trace 作为**补充证据源**（例如 trace 中的策略上下文），但必须走同样的只读口径与溯源标注；本条**不依赖** 201 的任何组件才能运行，**不新增**任何在线采集，不改 proxy 热路径。
- 反向边界：201 的看板**不展示**本条的推荐（推荐的呈现面是推荐记录文件 + CLI 查看）；将来若要在看板里露出推荐，须另行立项，不在本条范围内顺手做。
- 与 PRD-149 审计账本的关系一句带过：149 是 failover 决策的事后审计，本条 §6.3 的确认/应用流水是新增的独立流水，风格对齐但不混写、不改 149 的表。

## 8. 与 PRD-173 的边界（一句带过）

PRD-173（BYOS）解决「接得进来」——把外部订阅/账号作为供给接入网关；本条解决「用得省」——在已接入的供给之内学怎么路由更划算；两者只在「候选集合」这一事实上相交，本条不碰接入层、不读凭据、不评价供给本身（PRD-173 的在途 PR #84/#85 与本条无关、互不阻塞）。

## 9. 红线落实

| 红线 | 设计层面的保证 |
|---|---|
| 只出推荐不自动执行，策略变更必须人工确认 | §1 原则 2（读写分离）+ §6.2 三重机制（存储隔离 / 应用器单闸门校验确认记录与证据哈希 / 确认是显式 CLI 动作）；推荐生成器在架构上没有写路由配置的能力，不是靠约定自觉 |
| 样本不足不推荐 | §2.3 的 R1–R6 写死为生成器硬规则，不满足只出观察记录并注明触发规则 |
| 无证据不给推荐 | §5 固定格式：证据链字段缺失即不得产出 `kind=recommendation` |
| 借鉴源协议未核实，只借鉴形态不复用代码 | 头部借鉴声明；本设计全部口径锚定本仓库现有代码，未引用 experiential 的任何实现细节、数据结构或算法 |
| 只存元数据、不碰正文与密钥 | §2.1：继承 `usage.db` 既有边界，学习链路不新增正文采集；文档与示例一律占位符 |
| 热路径零改动 | §1 原则 1：离线只读，在线模块零改动 |

## 10. T2–T5 输入输出契约

### T2 流量数据汇总管道（trend#1146）

- **输入**：`usage.db`（`usage_events` / `failover_decisions` / `failover_outcomes`，只读打开）；§2.2 分类规则（含 `classifier_version`）；窗口参数（7/14/30 天）。
- **输出**：聚合集——按（窗口 × `prompt_class` × provider × model）成行，每行含 §3 全部指标 + `n` + 覆盖率 + 溯源字段（窗口起止、`classifier_version`、`metric_version`、事件总数）；另附分类覆盖率报告。
- **契约要点**：同输入同输出（确定性）；可溯源验收以「任取一行能对回窗口内原始事件」为准；§2.2 的分类数据缺口在 T2 内定夺 (a)/(b) 路径并回写本文档备注。

### T3 策略推荐生成（trend#1147）

- **输入**：T2 聚合集；当前路由配置快照（只读：`policy_weights`、候选顺序、成本参数、`max_attempts`）。
- **输出**：推荐记录集（§5 固定格式，`status=pending`）+ 观察记录集（含未出推荐的原因与触发的 R 规则编号）。
- **契约要点**：§2.3 门槛与 §4 白名单在生成器内硬编码执行；同输入同输出；无证据/样本不足时推荐集为空但观察集必须非空（解释清楚为什么空）。

### T4 人工确认与应用流程（trend#1148）

- **输入**：`status=pending` 的推荐记录；操作员的显式确认/拒绝/回滚指令。
- **输出**：确认记录 + §6.3 审计流水事件；应用成功时输出配置变更（单参数旧值→新值）与回滚点；任何校验失败输出拒绝事件与原因。
- **契约要点**：验收的负面用例是主验收——构造「无确认记录的推荐」「证据哈希不一致的确认」「已过期推荐」三种情况，应用器必须全部拒绝且留痕，缺一不可。

### T5 验收试运行：历史流量回放（trend#1149）

- **输入**：一份冻结的历史流量快照（固定窗口的 `usage.db` 导出副本，含导出时间与事件计数校验和）；当时的路由配置快照。
- **流程**：在快照上离线跑 T2→T3 全链路两次；全程监控路由配置文件的哈希。
- **输出**：回放验收报告——两次运行的推荐集逐项 diff、与 §2.3/§5 的合规核查、配置哈希变化记录。
- **契约要点（验收标准）**：两次推荐集**逐项一致**（可复现）；回放全程路由配置哈希**零变化**（零自动变更）；推荐证据能逐项对回快照原始事件。任一不满足即验收不通过。

## 11. 未决问题（留给评审）

1. §2.2 的分类数据缺口走 (a) 保守路径还是 (b) 补枚举字段路径——影响 T2 工作量与分类丰富度，建议评审先定。
2. 推荐呈现面：本设计定为「推荐记录文件 + CLI 查看」，不做 UI；若评审希望进 201 看板，按 §7 须另行立项。
3. §2.3 的阈值（n≥30、效应量 5pt/20%/15%、权重步长 ±0.10）是按本地单机流量量级拟的初值，上线后应以首个窗口的实际分布复核一次，调整须走设计变更记录。
4. 权重类推荐的作用域：首期只做全局权重 + 虚拟模型级首选；「按 `prompt_class` 分权重」需要选路侧支持按类取权重，超出本条范围，未纳入白名单。
