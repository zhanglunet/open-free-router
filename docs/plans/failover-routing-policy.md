# 故障转移与选路策略（PRD-059/T1）

> 地位：本文档是 PRD-059 的"宪法"。T2（成本选路）、T3（延迟选路）、T4（策略化实现）、T5（混沌验收）均以本文为准；PRD-060（协议归一）的错误语义口径**以本文为准**，060 不得另发明错误码或触发条件。
>
> 术语全部沿用现有代码，不另造概念：`FailureDecision` / `FailureDecision.kind`（`resilience.py: classify_failure`）、`ResilienceManager` 三层状态（Provider / Credential-slot / Model）、`RoutePlanner` / `RoutePlan` / `RouteTarget`（`routing.py`）、`UpstreamExecutor`（`executor.py`）、`QuotaObservation`（`quota.py`）、`RouteDecisionStore`（`telemetry.py`）、探测证据 `probe-results.json`（`probe.py`，`EVIDENCE_MAX_AGE_SECONDS = 2700` 秒）。

## 0. 设计原则（不可违反）

1. **首字节边界**：只在首字节前转移候选。上游响应头或 SSE 事件一旦发给客户端，绝不重放、不换候选（`ARCHITECTURE.md` §6.4，防重复工具调用/重复计费/输出拼接）。
2. **按结构分类，不按文案猜**：错误分类只看状态码、头字段和已文档化字段，不读错误正文关键词（`CLAUDE.md`：Groq 把真耗尽写成普通 429 文案的教训）。429 的"quota/credit/balance"措辞只影响冷却时长倍数，**不**作为永久判死的依据。
3. **惩罚精确到实际故障的层级**：401 罚 credential slot，403 锁 model，5xx 罚 provider；扩大惩罚前先问"这次响应能证明哪一层坏了"（`CLAUDE.md`）。
4. **证据过期**：探测证据超过 45 分钟视为"从未测过"，不做"现在可用"的断言（`CLAUDE.md`）。
5. **永远留一条回来的路**：任何阻断流量的状态（熔断/terminal/冷却）必须有自动恢复路径，不依赖人工重置。

---

## 1. 触发条件

下表是**唯一**的触发条件清单。`kind` 列是 `classify_failure()` 返回的 `FailureDecision.kind`，实现时直接复用，不另写分类器。

### 1.1 触发转移的信号

| # | 信号 | kind | 转移动作 | 状态影响 | 理由 |
|---|---|---|---|---|---|
| 1 | 408 / 500 / 502 / 503 / 504 | `provider_unavailable` | 换下一个候选 | provider 失败计数 +1（`ResilienceManager.record_failure(provider_failure=True)`） | 上游服务挂了/网关坏了，是临时故障，换一家大概率能好 |
| 2 | 429（普通限流文案） | `rate_limited` | 同候选等 `Retry-After` 重试 1 次（§2）；或换候选 | credential 冷却 60s（`credential_cooldown=True`） | 限流是暂时的；等一会儿或换一家都能解 |
| 3 | 429（含 quota/credit/balance 措辞） | `quota_exhausted` | **换候选**（不等） | credential 冷却 480s（60s × 8，`QUOTA_COOLDOWN_SCALE`） | 措辞暗示额度见底，等几分钟没用；但措辞不可靠，不做永久判死 |
| 4 | 连接失败 / 连接超时 | `transport_error` | 换下一个候选 | provider 失败计数 +1 | 到上游的路不通，换一家 |
| 5 | 首字节超时（§2 默认 30s） | `transport_error` | 换下一个候选 | provider 失败计数 +1 | 首字节还没来，仍在首字节边界内，换候选安全 |
| 6 | 404 模型不存在 | `model_unavailable` | 换下一个**候选模型** | model 锁定 120s（`model_lockout=True`） | 模型下线/改名了，换个模型；不罚 provider（服务本身是好的） |
| 7 | 400 且命中 context 标记 | `context_overflow` | **条件转移**：仅当候选中有上下文更大的模型时换，否则直接返回错误 | model 锁定 120s | 请求超长是请求本身的问题；换大上下文模型可能有用，盲目换是浪费 |

### 1.2 明确不触发转移的信号

| # | 信号 | kind | 动作 | 理由 |
|---|---|---|---|---|
| 8 | 400（其他参数错误） | `invalid_request` | 直接返回错误给客户端，不重试不转移 | 参数错了，重试一万次也一样错；转移只会把坏请求扩散到别家 |
| 9 | 401 | `credential_invalid` | 该 credential slot 标记 terminal；**不**跨候选转移（同 provider 其他可用 slot 可试一次，见 §3.2） | 钥匙错了，换候选也没用——同一把钥匙开哪扇门都打不开 |
| 10 | 402 | `credits_exhausted` | 该 slot terminal；虚拟模型请求可继续走**其他 provider** 的候选，显式模型请求直接返回错误 | 额度没了，换谁都没用；但别家可能还有，虚拟模型不拦路 |
| 11 | 403 | `model_forbidden` | 锁 model；累计 3 个不同模型被拒 → 该 slot terminal | 账号没这个模型的权限，不是临时故障；多个模型都拒才说明钥匙有问题（`CLAUDE.md`：NVIDIA NIM 拿 403 做模型门禁的教训） |
| 12 | 首字节**后**的任何失败 | — | 不转移，直接透传/截断 | 首字节边界（原则 1）：已发送的数据无法收回 |

### 1.3 给 PRD-060 的映射表

PRD-060/T4 消费统一错误语义时，按本表映射为内部错误码（060 不得自创）：

| 内部错误码 | 对应 kind | 060 翻译要求 |
|---|---|---|
| `UPSTREAM_5XX` | `provider_unavailable` | 5xx 家族统一归一 |
| `RATE_LIMITED` | `rate_limited` | `Retry-After` 归一化为秒数（数字或 HTTP-date） |
| `QUOTA_EXHAUSTED` | `quota_exhausted` | 同上；措辞标记仅作提示 |
| `TIMEOUT` | `transport_error`（超时分支） | 连接超时 / 首字节超时 / 总超时统一为 `TIMEOUT`，附 `timeout_phase`（connect/ttfb/total） |
| `AUTH_INVALID` | `credential_invalid` | 401 |
| `CREDITS_EXHAUSTED` | `credits_exhausted` | 402 |
| `MODEL_FORBIDDEN` | `model_forbidden` | 403 |
| `MODEL_NOT_FOUND` | `model_unavailable` | 404（沿用 PRD-014 已定形状 `{"error":"MODEL_NOT_FOUND","model_id":"..."}`） |
| `BAD_REQUEST` | `invalid_request` / `context_overflow` | 400；`context_overflow` 附 `context_limit`（如上游给了） |

---

## 2. 重试与退避

### 2.1 尝试次数

| 项 | 默认值 | 可调范围 | 说明 |
|---|---|---|---|
| `max_attempts`（单请求总尝试次数，沿用 `RoutingConfig`） | 3 | 1–10 | 候选切换 + 同候选重试的总和；`RoutePlan.candidates` 本来就按它截断 |
| 同候选 429 重试 | 1 次 | 0–1（开关） | 仅当 `Retry-After` 可解析**且** ≤ `retry_after_max_seconds` 时 |
| `retry_after_max_seconds` | 120s | 0–3600 | 超过此值的 Retry-After 不等待，直接换候选（等太久不如换一家） |
| 候选切换次数 | `max_attempts - 已用` | — | 429 同候选重试占用一次 attempt 计数 |

### 2.2 退避公式

候选切换前的等待（指数退避 + 抖动，防惊群）：

```
delay_n = min(backoff_cap_seconds, backoff_base_seconds * backoff_factor^(n-1))
实际等待 = delay_n * (1 ± backoff_jitter 均匀随机)
```

| 项 | 默认值 | 可调范围 |
|---|---|---|
| `backoff_base_seconds` | 1.0 | 0–10 |
| `backoff_factor` | 2.0 | 1–4 |
| `backoff_cap_seconds` | 30 | 1–300 |
| `backoff_jitter` | 0.25 | 0–0.5 |

注：429 且有 `Retry-After` 时**优先用上游声明的值**（`parse_retry_after`，上限 3600s），不用指数退避——上游说等多久就等多久（在 `retry_after_max_seconds` 内）。

### 2.3 超时（三种，缺一不可）

| 项 | 默认值 | 说明 |
|---|---|---|
| `connect_timeout_seconds` | 10 | TCP 建连超时；超时 → `transport_error`（§1 #4） |
| `ttfb_timeout_seconds` | 30 | 首字节超时；超时 → `transport_error`（§1 #5），仍在首字节边界内可转移 |
| `request_timeout_seconds` | 120 | 单次尝试总超时（沿用 `UpstreamExecutor` 现有 `timeout`） |
| `total_budget_seconds` | 300 | 单请求 failover 总耗时上限；超限直接返回最后一次错误，不再转移（防请求 hang 住） |

---

## 3. 熔断与半开恢复

三层隔离沿用 `ResilienceManager` 现状，本节把"策略值"定死（T4 照此实现，可配置）。

### 3.1 Provider 层（熔断器）

| 项 | 默认值 | 说明 |
|---|---|---|
| `provider_threshold` | 3 | 连续失败 3 次 → `open`（沿用现有；失败计数只统计 `provider_failure=True` 的 kind，不把 401/403 算进来） |
| `provider_cooldown_seconds` | 30 | `open` 持续 30s，期间 `can_attempt` 返回 `provider_circuit_open` |
| 半开 | 冷却结束后自动 `half_open`，**只放 1 个探测请求**（`probe_in_flight` 占位，防并发打穿） | 成功 → `closed` 且计数清零；失败 → 重新 `open` 30s |
| half_open 探测成功但返回 401/403/404 | provider 直接 `closed`（路是通的），窄层状态（credential/model）按 §3.2/3.3 处理 | 路通≠钥匙对，分层归因（`resilience.py` 现有逻辑） |

### 3.2 Credential slot 层

| 信号 | 动作 | 默认值 |
|---|---|---|
| 429 普通 | `cooldown` | 60s（`credential_cooldown`）；到期自动回 `ready` |
| 429 quota 措辞 | `cooldown` | 480s（60 × 8） |
| 401 / 402 | `terminal` | **不是一扇单向门**：`terminal_since + 900s × 2^(terminal_failures-1)`（上限 8 倍 = 7200s）后放 1 个恢复探测；成功则复活——换了新 Key 不用人手工重置（`CLAUDE.md` "Never leave a one-way door"） |
| 403 累计 | 3 个**不同**模型被拒 → `terminal`（`credential_forbidden_threshold`） | 单个模型被拒只锁模型（§3.3）；跨模型累计才定钥匙的罪 |
| 同 provider 多 slot | 401 只 terminal 出错的 slot；其他 slot 照常用 | 钥匙是按 slot 隔离的，一把坏不连坐 |

### 3.3 Model 层

| 项 | 默认值 | 说明 |
|---|---|---|
| `model_cooldown_seconds` | 120 | 404 / 403 / context_overflow 后该 `(provider, model)` 锁定 120s，`can_attempt` 返回 `model_lockout` |
| 锁定期满 | 自动删除状态（不留僵尸记录） | 沿用现有 `_load_state` / `can_attempt` 逻辑 |

---

## 4. 三维选路权重

### 4.1 维度定义与事实源

| 维度 | 含义 | 事实源（不另起炉灶） | T2/T3 落点 |
|---|---|---|---|
| **质量** `quality` | 现有可解释评分：health / success_rate / capability / free_evidence | `scoring.score_candidate`（沿用 `DEFAULT_WEIGHTS` 六维内部权重） | T2 直接用 |
| **成本** `cost` | 免费额度剩余 / 限流状态 | ① `quota.py: parse_quota_headers` 的 `QuotaObservation`（remaining/limit/reset）；② `provider.free_tier_for(model).status()` 免费证据；③ PRD-014 `get_model_score` 的 quota 因子 | T2：从 `provider-profiles.json` 扩展成本表（`free_quota_zh` 已有文字描述，结构化为 `free_tier` 字段） |
| **延迟** `latency` | 实时延迟 | T2 前：探测快照 `latency_ms`（`probe-results.json`）；T3：滑动窗口 p50/p95（内存，最近 100 次，可关闭） | T3 实现采样；口径 `latency_good_ms=500` / `latency_bad_ms=10000` 沿用现有 |

### 4.2 权重

```
final_score = w_quality * quality + w_cost * cost + w_latency * latency
```

| 项 | 默认值 | 可调范围 | 说明 |
|---|---|---|---|
| `w_quality` | 0.5 | 0–1 | 质量是基本盘，默认占一半 |
| `w_cost` | 0.3 | 0–1 | 免费额度是本项目的立身之本 |
| `w_latency` | 0.2 | 0–1 | 默认关闭实时采样时退化为探测快照延迟 |
| 归一化 | 权重和≠1 时自动归一 | — | 沿用 `ScoringConfig.from_dict` 的归一逻辑 |

### 4.3 缺失值策略（沿用现有，不发明）

- 任一维度无数据 → 取 `missing_default = 0.5`，并在可解释输出中标注 `source: missing_default`（沿用 `scoring.py`）。
- 必须区分"从未测过"和"测过但过期了"（`CLAUDE.md`）：前者标 `never_measured`，后者标 `stale`，两者都按 0.5 计分但解释不同。

### 4.4 免费额度耗尽降级规则

| 条件 | 动作 | 说明 |
|---|---|---|
| `remaining / limit < 0.10`（任一维度） | 成本分 × 0.5（`cost_downgrade_factor`） | 额度将尽，降权不断流 |
| `classification = credits_exhausted` | 该候选**直接排除**（与 `ResilienceManager` 联动） | 没额度了，选它必 402 |
| credential 处于 429 冷却中 | 排除直到 `cooldown_until` | 冷却期选它必再吃 429 |
| 无 quota 数据（`has_headers=False` 且无历史） | 按 `missing_default` 计分，不惩罚 | 没有证据不予定罪（原则 2） |

---

## 5. 决策日志（给 T4 实现）

每次转移（换候选 / 同候选重试 / 熔断开闭）记一条，写入 `usage.db` 新表 `failover_decisions`（沿用现有 `RouteDecisionStore.record` 的字段风格，0600 权限，默认保留 30 天）。

| 字段 | 类型 | 说明 |
|---|---|---|
| `at` | ISO8601（带时区） | 决策时间 |
| `request_id` | string | `ofr_` 前缀（沿用 `UpstreamExecutor`） |
| `from_candidate` | string | 原候选 `canonical_id`（`provider/model`）；同候选重试时为 null |
| `to_candidate` | string | 新候选 `canonical_id`；无可用候选时为 null |
| `reason` | string | `FailureDecision.kind` 枚举值（§1.3 映射表），**不用**原文 |
| `attempt` | int | 第几次尝试（从 1 计） |
| `elapsed_ms` | float | 本次尝试耗时 |
| `ttfb_ms` | float \| null | 首字节耗时（如有） |
| `circuit` | string \| null | 触发的熔断层级：`provider` / `credential` / `model` / null |

**脱敏要求**（沿用现有 `telemetry.py` / `probe.py:_scrub`）：不记响应头原文、不记 Key、不记 prompt/响应正文、不记工具参数。`reason` 只用枚举值。

仪表盘（:9057）T4 需提供查询视图：按时间/模型/原因过滤。

---

## 6. 与 PRD-060（协议归一）的关系

1. **错误语义以本文为准**：060/T4 做错误码归一时，分类枚举、触发/不触发划分、 `Retry-After` 语义全部照 §1（含 §1.3 映射表）实现；060 只做"各家方言 → 本表"的翻译，不增删条目。
2. **翻译层不得吞掉分类信号**：060 归一化 429 时必须保留 `Retry-After` 原始值（数字/HTTP-date 任一），归一化为秒数后透传给本策略的 §2.2；归一化 5xx 时保留原始 status code 供 `classify_failure` 使用。
3. **并行开发对齐点**：059/T4 与 060/T4 并行；联调前双方对照本文 §1.3 映射表做一次 diff，060 的翻译表缺哪个 kind 就补哪个。

---

## 7. 配置参考（T4 实现时的 yaml 形状，全部带默认值）

```yaml
routing:
  policy:
    max_attempts: 3                 # 1-10
    retry_same_candidate_on_429: true
    retry_after_max_seconds: 120    # 0-3600
    backoff_base_seconds: 1.0       # 0-10
    backoff_factor: 2.0             # 1-4
    backoff_cap_seconds: 30         # 1-300
    backoff_jitter: 0.25            # 0-0.5
    connect_timeout_seconds: 10
    ttfb_timeout_seconds: 30
    request_timeout_seconds: 120
    total_budget_seconds: 300
    weights: { quality: 0.5, cost: 0.3, latency: 0.2 }  # 0-1，自动归一
    cost_downgrade_threshold: 0.10  # remaining/limit 低于此值降权
    cost_downgrade_factor: 0.5
    missing_default: 0.5
failover:
  provider_threshold: 3
  provider_cooldown_seconds: 30
  credential_cooldown_seconds: 60
  quota_cooldown_scale: 8.0
  model_cooldown_seconds: 120
  credential_terminal_probe_seconds: 900   # 起步；失败指数退避，上限 8 倍
  credential_terminal_probe_max_scale: 8
  credential_forbidden_threshold: 3
```

校验规则（T4）：数值超范围 → 回退默认值并记 warning 日志；`weights` 全 0 → 回退默认三维权重。

---

## 8. 给 T2–T5 的验收映射

| 任务 | 验收锚点（本文章节） |
|---|---|
| T2 成本选路 | §4.1 成本事实源、§4.4 降级规则；`provider-profiles.json` 结构化 `free_tier` 字段 |
| T3 延迟选路 | §4.1 延迟口径（滑动窗口 100 次 p50/p95，可关闭）；`latency_good/bad_ms` 沿用 |
| T4 策略化实现 | §2（退避公式）、§3（三层熔断值）、§5（决策日志表）、§7（配置形状与校验） |
| T5 混沌验收 | 故障矩阵：429 / 5xx / 连接超时 / 首字节超时 × mock 上游；断言：不断流、熔断/半开行为与 §3 一致、决策日志完整（§5 字段全）、选路解释输出三维得分明细（§4.2） |

---

*文档版本：v1.0（PRD-059/T1）。后续 T2–T5 实现若需调整策略值，走本文档的修订 PR，不在代码里偷改默认值。*
