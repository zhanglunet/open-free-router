# OmniRoute 能力借鉴与 open-free-router 演进 PRD

Status: Draft — proposed for implementation planning, no code copied
Owner: open-free-router
Last updated: 2026-08-02
Reference snapshot: OmniRoute `release/v3.8.50` at `fc35dc248f46354e80fdcdaa551e6598abcf5124`

## 1. 结论

OmniRoute 与 open-free-router 都提供“一个本地兼容接口连接多个模型提供商”的
能力，但二者的产品边界不同：OmniRoute 是 Node.js / Next.js / Electron / SQLite
组成的综合网关，open-free-router 是 Python 标准库 HTTP Server 为核心的轻量
免费模型路由器。

本项目不整包合并 OmniRoute，也不把其大型运行时、数据库和前端依赖引入核心。
建议独立重实现以下五类能力：

1. `auto` 虚拟模型与可解释的免费模型路由；
2. 提供商、凭据、模型三层故障隔离；
3. 限流、额度与免费条件的结构化状态；
4. 请求级路由解释、延迟与成功率观测；
5. 一键体检、配置预览与故障恢复界面。

其中“确定性 fallback + 三层故障隔离 + 路由解释”组成此专项 P0。额度学习、
持久化分析和加权智能路由进入 P1；Docker、远程 MCP 等进入 P2。

## 2. 参考来源与许可证边界

本 PRD 基于以下公开资料和代码快照形成：

- [OmniRoute 官网](https://omniroute.online/)
- [OmniRoute GitHub 仓库](https://github.com/diegosouzapw/OmniRoute)
- [固定版本架构文档](https://github.com/diegosouzapw/OmniRoute/blob/fc35dc248f46354e80fdcdaa551e6598abcf5124/docs/architecture/ARCHITECTURE.md)
- [固定版本韧性设计](https://github.com/diegosouzapw/OmniRoute/blob/fc35dc248f46354e80fdcdaa551e6598abcf5124/docs/architecture/RESILIENCE_GUIDE.md)
- [固定版本 MIT LICENSE](https://github.com/diegosouzapw/OmniRoute/blob/fc35dc248f46354e80fdcdaa551e6598abcf5124/LICENSE)

OmniRoute 使用 MIT License。产品思想、交互模式和通用架构可独立实现；若未来
复制或改写其具体代码、测试、文案或素材，必须保留 OmniRoute 的版权与 MIT
许可声明，并在 `NOTICE` 中逐文件记录来源。当前 PRD 没有复制其实现代码。

官网与仓库 README 的规模数字存在版本差异，例如提供商和路由策略数量不一致。
本项目不得把这些营销数字作为已验证事实，也不得把不同提供商的理论免费额度简单
相加后宣传“无限免费”。所有免费条件必须有来源、核验时间和限制说明。

## 3. 当前基线

| 能力 | open-free-router 当前状态 | OmniRoute 可借鉴点 | 决策 |
|---|---|---|---|
| 单一代理入口 | 已有，默认 `127.0.0.1:8337` | 一个端点供多客户端使用 | 保持现状 |
| 模型 ID 路由 | 已支持 4 种 ID 和 Codex 安全别名 | 虚拟组合模型与候选池 | 扩展，不破坏现有 ID |
| 协议兼容 | Chat/Completions/Embeddings/Responses/Messages | 更系统的协议转换与一致性测试 | P1 加固 |
| 多提供商/多 Key | 已有 provider 和 `api_keys` | 按凭据选择、冷却和恢复 | P0 |
| 自动发现/实测 | 已有候选发现、模型探测和可选收录 | 探测结果参与路由 | P0 只读接入 |
| fallback | 当前一次请求只选一个匹配 provider | 有序候选、账号 fallback | P0 |
| 故障隔离 | 有探测快照，无请求路径断路器 | provider / credential / model 分层 | P0 |
| 额度管理 | 仅展示上游错误，无统一状态 | Retry-After、重置时间、额度预检 | P1 |
| 可观测性 | 状态页与探测延迟 | 路由原因、p50/p95、fallback 次数 | P0/P1 |
| MCP | 已有 6 个 stdio tools | route explain、quota、health | P1，小规模扩展 |
| 安装与体检 | 已有 `sync`、`status`、`doctor` | 更完整的可恢复诊断 | P1 |
| 本地优先 | 密钥保留在本机，Cloudflare 仅用专用 Secrets | 加密、管理面鉴权 | 保持边界并加固 |

## 4. 产品目标与非目标

### 4.1 目标

- 用户选择 `auto` 或一个自定义路由组时，单个免费模型失败不会直接终止任务。
- 路由决策可重复、可解释，用户可以知道“为什么选它、跳过了谁”。
- 429、额度耗尽、凭据失效、模型下线和提供商故障不会互相污染状态。
- 不改变已有显式模型 ID 的行为；用户指定具体 provider/model 时默认不静默换模。
- 上游密钥、请求正文和响应正文默认不进入日志、网站或 Cloudflare。
- 保持轻量安装：P0 不增加 Web 框架，不引入 Node 运行时或外部数据库。

### 4.2 非目标

- 追求数百家提供商或复制 OmniRoute 的完整产品面。
- 自动使用浏览器 Cookie、订阅账号或来源不清晰的 OAuth 凭据。
- 把“注册免费”描述为“无限免费”，或绕过供应商限流与服务条款。
- P0 实现提示词压缩、向量记忆、语义缓存、代理池、TLS 指纹伪装。
- P0 实现图像、音频、视频、搜索、Batches、Files 等新协议面。
- 引入 Electron 桌面端、云端 Agent 平台或完整 A2A Server。

## 5. 核心用户故事

1. 作为 Codex/Claude Code 用户，我希望使用 `auto/coding`，在首选免费模型
   临时限流时自动切到另一个已验证且支持工具调用的模型。
2. 作为运维者，我希望看到某次请求选择、跳过和 fallback 的原因，而不是只看到
   最终的 502/429。
3. 作为多 Key 用户，我希望一个 Key 额度耗尽只冷却该 Key，不禁用整个提供商。
4. 作为谨慎用户，我希望显式指定 `ds/deepseek-chat` 时不会被静默换成别的模型。
5. 作为项目维护者，我希望路由状态不包含密钥值、Prompt、响应内容或个人信息。

## 6. 需求优先级

### 6.1 P0 — 确定性路由与故障隔离

#### FR-P0-1 虚拟模型和路由组

新增保守的虚拟模型：

- `auto`：已验证可用模型中的均衡优先队列；
- `auto/coding`：只允许 `tool_calling: true`，优先代码和推理模型；
- `auto/fast`：优先近期成功且低延迟模型；
- `auto/free`：只允许免费条件仍有效且未过期的模型。

P0 不使用不透明机器学习评分。每个别名解析为配置中有序候选列表，再按实时健康
状态做确定性过滤。候选相同时保持稳定顺序，避免请求在模型间无故漂移。

建议配置：

```yaml
routing:
  aliases:
    auto/coding:
      require:
        tool_calling: true
      candidates:
        - or/gpt-oss-20b:free
        - gq/llama-3.3-70b
  fallback:
    enabled: true
    max_attempts: 3
    explicit_model: false
```

兼容性要求：

- 现有 bare、prefix/id、upstream_id、provider/upstream_id 和 `ofr-*` 不变；
- `GET /v1/models` 可列出虚拟模型，并以 `owned_by: open-free-router` 标识；
- 显式模型默认 `explicit_model: false`，即不静默 fallback；用户可按路由组显式启用。

#### FR-P0-2 请求前候选过滤

路由器在请求前依次检查：

1. 模型能力是否满足协议与工具调用要求；
2. 模型是否处于 lockout；
3. 凭据是否失效、额度耗尽或处于 cooldown；
4. provider circuit 是否 OPEN；
5. 最近一次服务端/本地探测是否过期或失败。

探测快照只作为降权/跳过证据，不得替代本次真实请求结果；超过可配置 TTL 后状态为
`unknown`，不得永久判死。Cloudflare 服务端状态不直接控制用户本地路由，本地代理
仅使用自己的运行时状态和本地探测。

#### FR-P0-3 三层故障隔离

实现三个互不替代的状态域：

| 层级 | 典型触发 | 影响范围 | 恢复方式 |
|---|---|---|---|
| provider circuit | 408/500/502/503/504 连续失败 | 整个 provider | OPEN → HALF_OPEN 探测 → CLOSED |
| credential cooldown | 401、403、429、402、Retry-After | 单个 Key 槽位 | 时间到、凭据更新或手工重置 |
| model lockout | 404、模型级 429、明确模型下线 | 单 provider + 单 model | 指数冷却后试探或手工重置 |

错误分类必须区分：

- 客户端断开、代理内部流控制错误不计为上游 provider 失败；
- 401/403 默认标记凭据问题，不打开 provider circuit；
- 402 或明确的 quota/credit exhausted 是终止状态，不做短间隔重试；
- 400 默认不 fallback，只有可识别的上下文超限、模型下线或 provider 特有的
  模型级错误才能进入下一候选；
- 429 优先解析 `Retry-After` 和 reset headers，无法解析时使用有上限的退避。

默认值必须保守、可配置、有上限，并对并发失败去重，避免惊群导致冷却时间不断延长。

#### FR-P0-4 安全 fallback

- 仅在尚未向客户端发送响应头/正文/SSE event 时切换候选；
- 一旦向客户端发送首字节，不得重放请求到另一个模型；
- 单请求最多 3 次尝试，总等待预算默认不超过 10 秒；
- 客户端取消请求后立即取消等待和后续尝试；
- 不对认证错误、余额耗尽、无效请求和大多数 400 做盲重试；
- 每次尝试使用新的上游连接，绝不把 A provider 的 Authorization header
  转发给 B provider；
- Responses/Messages/Chat 三条路径使用同一个路由计划和错误分类器。

#### FR-P0-5 路由解释

每次请求生成不含敏感信息的 `RoutingDecision`：

```json
{
  "request_id": "ofr_...",
  "requested_model": "auto/coding",
  "selected": "gq/llama-3.3-70b",
  "attempts": 2,
  "reason": "priority_after_rate_limit",
  "skipped": [{"model": "or/...", "reason": "credential_cooldown"}]
}
```

- 非流式成功响应返回 `X-OFR-Request-Id`、`X-OFR-Provider`、
  `X-OFR-Model`、`X-OFR-Fallback-Attempts`；
- 路由详情只在本地管理 API 或 CLI 中按 request_id 查询；
- 上游错误正文只保留分类后的短摘要，必须经过 token/key/Authorization 脱敏；
- 默认不记录 request messages、response content、tool arguments 和 IP 地址。

#### FR-P0-6 CLI 与本地仪表盘

新增或扩展：

- `open-free-router route explain <model> [--json]`：离线解释当前候选过滤结果；
- `open-free-router resilience [--json]`：查看三层状态；
- `open-free-router resilience reset --provider NAME [--model ID]`：精确恢复；
- Live Status 增加“路由状态”区：断路器、Key 槽位状态、模型冷却、最近 fallback；
- 所有管理写操作继续要求本地 token，页面默认中文。

#### FR-P0-7 P0 数据与持久化

P0 使用线程安全内存状态，按节流频率原子写入
`<data_dir>/runtime-state.json`，文件权限 0600。文件只含 provider 名、Key 槽位编号、
模型 ID、计数和时间戳，不含 Key 值、Key 哈希、Prompt 或响应。损坏时隔离旧文件并
以空状态启动，不能阻断代理服务。

### 6.2 P1 — 额度感知、智能评分和分析

#### FR-P1-1 免费条件证据模型

为 provider/model 增加：

- `free_tier.type`: `unlimited_claimed | recurring_quota | trial | keyless | unknown`；
- `free_tier.limit`、`unit`、`reset_period`、`regions`；
- `evidence_url`、`verified_at`、`expires_at`；
- `terms_warning_zh` 和 `requires_payment_method`。

证据过期后只能显示“待复核”，不能继续标记“免费可用”。模型雷达和路由器共用同一
口径，但公开导出继续执行严格脱敏。

#### FR-P1-2 额度与速率状态

- 统一解析 402/429、`Retry-After`、`RateLimit-*`、`X-RateLimit-*` 等常见头；
- 区分瞬时速率限制、日/月额度耗尽、余额不足、凭据被禁用；
- 支持 provider 插件补充特有额度 API，但不得把密钥或原始返回上传到网站；
- 额度未知时显示 unknown，不根据错误文本猜出具体剩余额度；
- 多 Key 选择优先使用未冷却、重置更早且近期成功的槽位。

#### FR-P1-3 可解释评分

在 P0 稳定候选队列上增加可选评分，因子限定为：

- 健康状态、近期成功率；
- p95 首字节/总延迟；
- 额度余量或重置时间；
- 模型能力匹配；
- 免费证据有效性。

所有因子归一化到 `[0,1]`，权重总和归一化，NaN/缺失使用明确默认值。UI 必须展示
因子和权重。关闭智能评分后回到确定性 priority 策略。

#### FR-P1-4 本地使用分析

使用 Python 标准库 `sqlite3` 保存最小化元数据：时间、provider、model、状态码、
延迟、输入/输出 token、fallback 次数和分类后错误。默认保留 30 天，可配置为 0
完全关闭。不得保存消息正文、响应正文、工具参数或完整请求头。

提供：

- provider/model 成功率和 p50/p95 延迟；
- fallback 率、断路器次数和节省的失败请求；
- token 用量与免费额度的估算，并明确“估算”口径；
- JSON/CSV 导出前再次做敏感字段拒绝检查。

#### FR-P1-5 MCP 与诊断

在现有 6 个 MCP tools 基础上最多增加 4 个只读工具：

- `explain_route`、`get_resilience`、`check_quota`、`get_metrics`。

写操作不默认暴露给 MCP。若未来增加，必须有独立 scope、明确确认和审计记录，避免
复制 OmniRoute 的大规模工具面导致上下文膨胀。

#### FR-P1-6 协议一致性测试

建立 provider × protocol × capability 矩阵，至少验证：

- Chat Completions、Responses、Anthropic Messages 的非流式和流式文本；
- function/tool call 多轮；
- usage、finish reason、错误 envelope 和 `Retry-After`；
- fallback 前无输出、首字节后禁止 fallback；
- provider 特有字段不泄漏到不兼容客户端。

### 6.3 P2 — 可选扩展

- 官方 Docker 镜像、健康检查和非 root 运行；
- MCP Streamable HTTP（默认关闭、需管理 token 和 scope）；
- 配置导入/导出及一键回滚；
- 公开目录的 provider 插件 manifest 与 schema 校验；
- 移动端 PWA 仅作为状态查看器，不承载密钥。

## 7. 明确不采纳或暂缓的 OmniRoute 能力

| 能力 | 决策 | 原因 |
|---|---|---|
| Next.js/Electron 整体运行时 | 不采纳 | 与轻量 Python 架构冲突，依赖和发布面过大 |
| 向量记忆/Qdrant | 暂缓 | 与路由器核心职责无关，扩大隐私边界 |
| Prompt/token 压缩 | 暂缓 | 可能改变语义和工具调用，需独立评测与用户同意 |
| 代理池/TLS stealth | 不采纳 | 合规、稳定性和供应链风险高 |
| 浏览器 Cookie/订阅额度聚合 | 不采纳 | 凭据敏感，可能违反供应商条款 |
| 自动 key 注册/发放 | 不采纳 | 本项目只管理用户主动提供的凭据 |
| 完整 A2A/Cloud Agents | 暂缓 | 超出免费模型路由器定位 |
| 数百个 MCP tools | 不采纳 | 上下文膨胀、权限面和维护成本过高 |
| “无限免费”总量营销 | 不采纳 | 免费规则会变化，理论额度不可等同实际可用性 |

## 8. 安全与合规要求

1. 上游 API Key 仍只保存在 owner-only 本地注册表或显式环境变量中；客户端只拿
   本地代理 token。
2. Cloudflare 只能保存维护者主动设置的专用 Secrets；不能上传用户本地 Key。
3. Runtime state、usage DB、导出和日志均通过字段 allowlist；出现 `api_key`、
   `authorization`、`cookie`、`token_value` 等字段时拒绝公开导出。
4. 所有管理端点默认 loopback；未来远程开放必须单独设计鉴权、CSRF 和速率限制。
5. 不透传客户端 Authorization/Cookie 到上游；每个 provider 从自己的配置重新构造
   headers。
6. 错误分类不得把上游完整响应、账号标识或 Key 片段写入日志。
7. 自动发现仍是候选制；只有专用凭据实测成功并通过协议/合规检查后才能收录。
8. 供应商免费条款、地区限制、付款方式要求和数据使用政策应展示来源与核验日期。

## 9. P0 验收标准

### 9.1 功能

- `auto/coding` 在首选模型返回 429 且未发送任何下游字节时切换到第二候选并成功；
- 显式模型同样返回 429 时默认把该错误返回客户端，不静默换模；
- 单 Key 401/429 不打开 provider circuit，其他 Key 仍可被选择；
- 连续 provider 503 达阈值后 circuit OPEN，冷却后仅放行一次 HALF_OPEN 探测；
- 单模型 404/模型级 429 只 lockout 对应模型；
- 客户端断开、内部 SSE controller 错误不计入 provider 失败；
- 首个 SSE event 发出后即使上游断开，也不执行第二次上游请求；
- Responses、Messages、Chat 三协议得到一致的候选和错误分类结果；
- 重启后只恢复未过期状态，损坏状态文件不影响启动。

### 9.2 安全

- 单元测试以 canary secret 验证 runtime state、路由解释、CLI、UI、日志和公开导出
  均不含 Key、Authorization、Cookie、Prompt 和 tool arguments；
- provider 切换测试确认不会复用前一 provider 的 headers；
- 管理 API 未认证写入返回 401/403；
- 路由解释对错误正文执行长度限制和凭据擦除。

### 9.3 性能与稳定性

- 健康候选的路由决策 p95 小于 5 ms（不含网络请求）；
- 状态持久化不在每个 token/SSE chunk 上写盘；
- 100 个并发失败不会产生无界线程、无界状态或重复延长 cooldown；
- 现有测试全部通过，新增路由/韧性测试不得依赖真实外网。

### 9.4 兼容性

- 现有配置无需迁移即可启动；未配置 `routing` 时行为与当前版本一致；
- 现有 4 种模型 ID、Codex `ofr-*` 别名和 9 个客户端 sync 不回归；
- `registry.default.yaml` 不包含真实密钥或本机路径；
- `doctor` 能显示路由配置错误并给出可执行修复建议。

## 10. 建议模块边界

```text
src/open_free_router/
├── routing.py       # aliases, candidate plan, deterministic selection
├── resilience.py    # classifier, circuit, cooldown, model lockout
├── telemetry.py     # redacted decision/metric records
├── quota.py         # P1: headers and provider quota adapters
├── proxy.py         # protocol I/O only; invokes one shared route executor
├── registry.py      # model/provider capability and free-tier metadata
└── ui.py            # read models; management actions require local token
```

`proxy.py` 不应分别为 Responses/Messages/Chat 实现三套 retry。共同执行器接收规范化
请求，生成 route plan，执行候选并在成功后交给对应协议 adapter 输出。

## 11. 实施顺序

1. **P0-A 基础模型**：错误分类器、状态对象、配置 schema、持久化与单元测试；
2. **P0-B 路由计划**：虚拟模型、显式模型策略、候选过滤和 explain；
3. **P0-C 共同执行器**：先覆盖 buffered，再覆盖 streaming 首字节安全；
4. **P0-D 运维面**：CLI、UI、精确 reset、doctor；
5. **P0-E 回归与安全**：协议矩阵、并发、canary secret、构建与真实本地 smoke test；
6. **P1**：额度证据、SQLite 指标、可解释评分、MCP 只读扩展。

每个阶段都应保持现有显式模型路径可用，不能等全部智能路由完成后一次性替换核心
代理。P0 发布应有配置开关，可立即退回当前单模型直连行为。

## 12. 成功指标

- 虚拟路由请求在存在至少一个健康候选时，因单一 429/5xx 导致的最终失败率下降；
- 100% fallback 请求可以通过 request_id 给出机器可读原因；
- provider circuit 误伤（凭据错误导致整个 provider 被禁用）为 0；
- 公开与本地导出 secret canary 泄漏为 0；
- 未启用 `routing` 的用户无配置迁移、无行为变化；
- P0 不增加 Node、Web framework 或外部数据库运行时依赖。
