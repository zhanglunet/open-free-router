# PRD-060/T1：协议归一设计（翻译层契约）

> 对应：zhanglunet/trend#358（PRD-060/T1）
> PRD：zhanglunet/trend `adopted/PRD-060-ofr-protocol-unify.md`
> 状态：设计文档（T1），只开 PR 不合并；T2–T5 按此实现
> 与现有文档的关系：`docs/protocol-capability-matrix.md` 回答"客户端协议面"（下游：Chat/Responses/Messages 三协议 × 提供商声明矩阵），本文档回答"上游翻译层"（上游：11 家方言 → 统一内部表示），两者不重复、互相引用。

## 0. 现状结论（代码实测）

逐家过完 `src/open_free_router/` 推理路径代码（`executor.py`、`proxy.py`、`resilience.py`、`quota.py`、`responses.py`、`anthropic.py`）后的结论：

1. **10 家内置上游今天全部走 OpenAI-compatible Chat Completions**（`protocol-capability-matrix.md` §3 已声明，`executor.py::_open` 实证：`POST {upstream_url}/{endpoint}` + JSON body + `Authorization: Bearer`）。"方言"差异不在大协议面，而在**鉴权方式、URL 路径、字段白名单严格度、reasoning 字段、错误信封细节**这五处。
2. 注册表（`registry.default.yaml`）与 `docs/provider-profiles.json` 实际登记 **10 家**；PRD 称 11 家，差 1 家应为用户经 `open-free-router add` 自添加的上游——翻译表模板（§4）即覆盖"第 11 家及以后"的新增流程，不硬编码家数。
3. 已有归一化（T2–T4 直接复用，不重造）：
   - 请求：`proxy.py` 白名单 `_ALLOWED_TOP_KEYS` / `_ALLOWED_MSG_KEYS`、`max_completion_tokens`→`max_tokens`、`developer`→`system`；
   - 响应：`_normalise_reasoning_content`（content 为 null 时拷 `reasoning_content`）；
   - 错误：`_normalise_error` 只保留 OpenAI 四字段（message/type/param/code）；
   - 限流头：`quota.py::parse_quota_headers` 已归一 `ratelimit-*` / `x-ratelimit-*-requests|tokens` / `retry-after`（含 ms/s/m/h/d/w 与 HTTP-date）；
   - 错误分类：`resilience.py::classify_failure`（见 §6）。

## 1. 11 家上游差异清单（逐家，来自代码）

> "代码已确认" = 在本仓库源码中有实证；"待实测" = 需 T2 逐家回归确认。

| # | 上游 | upstream_url | 鉴权 | 请求差异（代码已确认） | 响应/流式差异（代码已确认） | 错误差异（代码已确认） |
|---|---|---|---|---|---|---|
| 1 | deepseek | `https://api.deepseek.com/v1` | Bearer | 对非标准字段严格（400）；`reasoning_content` 需从历史消息剥离（`_ALLOWED_MSG_KEYS`） | 推理模型返回 `{"content": null, "reasoning_content": "..."}`（`_normalise_reasoning_content`） | 标准 OpenAI 信封 |
| 2 | google-ai-studio | `https://generativelanguage.googleapis.com/v1beta/openai` | Bearer | **路径特殊**：必须带 `/v1beta/openai`，缺失时 `doctor` 报 `google_openai_compatibility_path_missing` | 同上（OpenAI 兼容路径） | 标准 OpenAI 信封 |
| 3 | groq | `https://api.groq.com/openai/v1` | Bearer | 对非标准字段严格（400，注释点名）；`upstream_id` 带组织前缀（`groq/compound`、`openai/gpt-oss-20b`） | 同上 | 429 文案为 `Rate limit reached ... tokens per day`（**不带** quota/credit 字样 → `classify_failure` 按普通 `rate_limited` 处理，靠响应头声明的 reset 做长冷却，见 `executor.py` 注释） |
| 4 | nvidia-nim | `https://integrate.api.nvidia.com/v1` | Bearer | `upstream_id` 带组织前缀（`nvidia/`、`minimaxai/`、`stepfun-ai/`、`z-ai/`、`deepseek-ai/`） | 同上 | **403 表示模型未授权**（非凭据问题）→ `model_forbidden` 只锁模型，见 CLAUDE.md 教训 |
| 5 | nous | `https://inference-api.nousresearch.com/v1` | Bearer | 无特殊（最标准的一家，见 §4 示例） | 同上 | 标准 OpenAI 信封 |
| 6 | poolside | `https://inference.poolside.ai/v1` | Bearer | 无特殊 | 同上 | 标准 OpenAI 信封 |
| 7 | opencode-zen-free | `https://opencode.ai/zen/v1` | **无（`auth_mode: none`）** | 不发 `Authorization` 头（`executor.py::_credentials`） | 同上 | 标准 OpenAI 信封 |
| 8 | openrouter | `https://openrouter.ai/api/v1` | Bearer | 免费模型用 `:free` 后缀；`max_completion_tokens`→`max_tokens` 转换主因之一（422） | 同上 | 标准 OpenAI 信封 |
| 9 | sensenova | `https://token.sensenova.cn/v1` | Bearer | 无特殊（国内） | 同上 | 标准 OpenAI 信封（待实测确认 429 文案形态） |
| 10 | stepfun | `https://api.stepfun.com/v1` | Bearer | 无特殊（国内） | 同上 | 标准 OpenAI 信封（待实测确认 429 文案形态） |
| 11 | （用户自添加） | `open-free-router add` 登记 | 按翻译表 | 按翻译表声明 | 按翻译表声明 | 按翻译表声明 |

通用上游请求形状（`executor.py::_open` 实证，所有家一致）：
`POST {upstream_url}/{endpoint_suffix}`，`Content-Type: application/json`，`User-Agent: open-free-router/0.1`，body 为白名单过滤后的 chat 请求 + `model=<upstream_model_id>`；3xx **不跟随**（视为传输失败，不把凭据带到别处）。

## 2. 统一内部 canonical schema（OpenAI 兼容为基准）

翻译层内部只处理以下三种表示；任何上游方言先进翻译表、再进核心逻辑。

### 2.1 CanonicalRequest

| 字段 | 类型 | 说明 |
|---|---|---|
| `model` | string（必填） | 内部模型 ID（网关路由用），出站时由翻译表映射为 `upstream_model_id` |
| `messages` | array（必填） | 每项 `{role, content, name?, tool_calls?, tool_call_id?}`；role ∈ {system, user, assistant, tool}（`developer` 已在入口归一为 system） |
| `max_tokens` | int? | 上限；`max_completion_tokens` 在入口已归一至此 |
| `temperature` / `top_p` | float? | 采样参数，原样透传 |
| `n` / `stop` / `seed` | — | 原样透传 |
| `stream` | bool | 是否流式 |
| `stream_options` | object? | 如 `{"include_usage": true}` |
| `tools` / `tool_choice` / `parallel_tool_calls` | — | 工具调用，原样透传（能力过滤在 RoutePlanner，不在本层） |
| `frequency_penalty` / `presence_penalty` / `logit_bias` / `logprobs` / `top_logprobs` / `response_format` / `user` | — | 白名单内字段，原样透传 |
| `passthrough` | object? | 透传逃生口声明的字段（见 §5），网关不解释、只搬运并记日志 |

### 2.2 CanonicalResponse

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 响应 ID（上游无则网关生成 `ofr_…`） |
| `model` | string | 实际响应的上游模型 ID |
| `choices[]` | array | 每项 `{index, message{role, content, tool_calls?}, finish_reason}`；`content` 为 null 时已由 `_normalise_reasoning_content` 兜底为 string |
| `reasoning_content` | string? | 保留但**不**进入 content；下游按需取用 |
| `usage` | object? | `{prompt_tokens, completion_tokens, total_tokens}`（流式下从末 chunk 收集） |

### 2.3 CanonicalStreamEvent（SSE 统一事件）

上游原生 SSE（`data: {...}` / `data: [DONE]`）先解析为以下事件，下游客户端适配器（Responses/Messages）只消费这一种事件流：

| 事件 | 字段 | 说明 |
|---|---|---|
| `text_delta` | `delta: string` | 增量文本 |
| `reasoning_delta` | `delta: string` | 增量推理内容（如有） |
| `tool_call_delta` | `index, id?, name?, arguments_delta` | 增量工具调用参数 |
| `usage` | `prompt_tokens, completion_tokens, total_tokens` | 计量（通常在末事件） |
| `done` | `finish_reason?` | 正常结束 |
| `error` | 见 §2.4 | 流中错误 |

客户端面映射（现状已实现，T3 保持）：`text_delta`→ Responses `response.output_text.delta` / Messages `content_block_delta`；`done`→`response.completed` / `message_stop`；`[DONE]` 只作为流结束标记不向上传。

### 2.4 CanonicalError

| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | string（必填） | 内部错误码，见 §6（口径以 PRD-059/T1 为准） |
| `http_status` | int | 上游原始 HTTP 状态码（0 = 传输层失败） |
| `message` | string | 脱敏后的一句话（已剥离厂商 trace ID、私有字段） |
| `retry_after_s` | float? | 归一化为秒的重试等待（`parse_retry_after`：数字秒或 HTTP-date，上限 3600s） |
| `provider` / `model` | string | 出错的上游与模型（内部用，不向客户端泄露凭据槽） |

## 3. 翻译表（translation table）

### 3.1 模板（新增上游复制此份填写）

```yaml
# 翻译表模板：新增一家上游时复制本文件，只填映射，不碰核心逻辑。
# 对应 T5 验收：只写本表，mock 上游即可跑通。
provider_id: example-provider          # 与 registry.yaml 中的 key 一致
upstream_url: https://api.example.com/v1
auth:
  mode: bearer                          # bearer | none
  header: Authorization                 # bearer 时固定为 Authorization: Bearer <key>

request:
  model_field: model                    # 出站 body 中模型 ID 的字段名
  max_tokens_field: max_tokens          # 若上游用别名（如 max_completion_tokens），填别名
  role_map:                             # 入站 role → 出站 role（缺省恒等）
    developer: system
  drop_fields: []                       # 出站前删除的顶层字段（白名单之外的已知雷区）
  passthrough: []                       # 透传字段路径，见 §5

response:
  content_field: message.content        # 文本字段 JSON 路径
  reasoning_field: message.reasoning_content  # 推理字段路径（无则留空）
  null_content_policy: copy_reasoning    # content 为 null 时：copy_reasoning | empty_string
  usage_fields:                         # 计量字段路径
    prompt_tokens: usage.prompt_tokens
    completion_tokens: usage.completion_tokens
    total_tokens: usage.total_tokens

streaming:
  event_format: openai_sse             # openai_sse | anthropic_sse | custom
  done_marker: "[DONE]"                # 流结束标记原文
  usage_in_stream: false               # usage 是否出现在流事件里

error:
  envelope: openai                     # openai | anthropic | custom
  # 状态码 → 内部错误码的覆写（未列出的走 §6 默认分类，口径以 PRD-059/T1 为准）
  status_overrides: {}
  retry_after_headers:                 # 按优先级列出该上游的 Retry-After 头
    - retry-after
    - ratelimit-reset
```

### 3.2 填表示例：nous（最标准的一家，无特殊方言）

```yaml
provider_id: nous
upstream_url: https://inference-api.nousresearch.com/v1
auth:
  mode: bearer
  header: Authorization

request:
  model_field: model
  max_tokens_field: max_tokens
  role_map:
    developer: system
  drop_fields: []
  passthrough: []

response:
  content_field: message.content
  reasoning_field: message.reasoning_content
  null_content_policy: copy_reasoning
  usage_fields:
    prompt_tokens: usage.prompt_tokens
    completion_tokens: usage.completion_tokens
    total_tokens: usage.total_tokens

streaming:
  event_format: openai_sse
  done_marker: "[DONE]"
  usage_in_stream: false

error:
  envelope: openai
  status_overrides: {}
  retry_after_headers:
    - retry-after
    - ratelimit-reset
```

## 4. 现有代码与翻译层的对应（T2–T4 落点）

| 现有位置 | 现状 | 翻译层改造点 |
|---|---|---|
| `executor.py::_open` | 硬编码 POST + Bearer + `model` 字段 | 改为读翻译表：`auth.mode`、`request.model_field`、`upstream_url` 拼接 |
| `proxy.py` 白名单/归一 | 硬编码在 handler 里 | 下沉为翻译表驱动的请求归一化（白名单=模板默认 + 单家 `drop_fields` 增补） |
| `proxy.py::_normalise_reasoning_content` / `_normalise_error` | 硬编码 | 下沉为 `response.*` 映射驱动 |
| `quota.py::parse_quota_headers` | 已归一多命名 | 保留；`error.retry_after_headers` 声明各家优先级 |
| `resilience.py::classify_failure` | 已分类（见 §6） | 保留并作为默认分类；`error.status_overrides` 仅做单家覆写 |
| `responses.py` / `anthropic.py` | 客户端面适配（下游） | 不动；只消费 §2.3 统一事件流（T3 抹平上游 SSE 差异后接入） |

## 5. 透传逃生口规则（防静默丢字段）

1. 翻译表 `request.passthrough` 显式声明字段路径（如 `metadata.user_id`）→ 网关原样搬运，不解释、不校验。
2. **未声明**的上游未知字段：记 `warning` 日志（含 provider、字段路径、请求 ID 脱敏）后丢弃；**禁止静默丢弃**。
3. 个性字段在统一 schema 无位置时：走透传 + 告警，不硬塞进标准字段（PRD 风险项落实）。
4. 流式事件中上游特有事件：翻译表显式声明 `drop_with_log: true` 方可丢弃，否则按规则 2 处理。

## 6. 错误码语义

**硬要求：错误码口径以 PRD-059/T1（zhanglunet/trend#353，`docs/plans/failover-routing-policy.md`）为准，本文档不另定义。**

本文档只记录翻译层"现状已实现的分类"（`resilience.py::classify_failure` 实证，供 T4 对齐时核对），不作为口径来源：

| 现状分类 | 触发 |
|---|---|
| `provider_unavailable` | 408/500/502/503/504 |
| `rate_limited` / `quota_exhausted` | 429（文案含 quota/credit/balance → 后者，长冷却；Groq 式纯 "Rate limit reached" → 前者，靠响应头 reset 做长冷却） |
| `credential_invalid` | 401 |
| `model_forbidden` | 403（只锁模型；NVIDIA NIM 式模型级授权） |
| `credits_exhausted` | 402 |
| `model_unavailable` | 404 |
| `context_overflow` | 400 + 上下文长度文案 |
| `invalid_request` | 其他 4xx（不触发转移，见 PRD-059） |
| `transport_error` | 传输层失败/3xx |

PRD-059/T1 落定后，若其口径与上表有出入，以 PRD-059/T1 为准并同步修订本节。

## 7. 给 T2–T5 的接口约定

- T2（请求归一）：实现翻译表加载器 + 出站请求构建器；`max_completion_tokens`→`max_tokens`、`developer`→`system`、白名单过滤作为默认翻译表行为。
- T3（响应归一）：实现 SSE 解析器（openai_sse 先行）→ §2.3 统一事件；`responses.py`/`anthropic.py` 改为消费统一事件（行为不变，有 diff 测试）。
- T4（错误码归一）：`classify_failure` 的默认分类不动；单家覆写只允许出现在翻译表 `error.status_overrides`；与 PRD-059/T4 联调前先对齐两边 T1。
- T5（回归验收）：11 家（10 内置 + 1 mock 新增）× 请求/响应/流式/错误；新增 mock 上游只写翻译表即跑通。

## 8. 待确认/风险（诚实清单）

1. **10 家 vs 11 家**：注册表与 provider-profiles.json 实际为 10 家，PRD 称 11 家；本文档按 10 家实测编写，第 11 家视为用户自添加（翻译表模板已覆盖新增流程）。T2 开工前建议与 PRD-059/T1 一并确认家数口径。
2. **sensenova/stepfun 的 429 文案形态**：代码中无实证，标记"待实测"；T2 回归时以真实 429 响应为准，不猜测。
3. **流式 `reasoning_delta`**：现状上游 SSE 为字节透传，推理增量事件的字段形态需 T3 逐家实测后钉死。
4. **与 PRD-059 并行开发的接口漂移**：两边 T1 已互相引用；T4 联调前必须先对齐（PRD-060 风险项）。
