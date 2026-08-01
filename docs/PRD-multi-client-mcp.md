# open-free-router 多客户端适配 · MCP 接口 · 架构网页 PRD

Status: P0 implemented and verified
Owner: open-free-router
Last updated: 2026-08-01

（中文为主文档；术语与配置片段保留英文原文。）

## 1. 背景

open-free-router 已支持 OpenAI Chat Completions 代理、Codex Responses API
以及 Pi / OMP / OpenCode / Hermes 的配置同步。用户侧出现了四类新诉求：

1. **更多客户端**：Claude Code、Kimi CLI（kimi code）、OpenClaw、WorkBuddy
   等主流 AI CLI/客户端也应一条命令接入本地免费模型代理，并完成
   功能性与适配性测试。
2. **更易集成**：需要 MCP（Model Context Protocol）接口与更完整的 CLI
   （status / doctor / models），让 Agent 宿主和脚本无缝调用路由器能力。
3. **可视化架构**：项目网站需要系统架构图与信息图页面，直观展示
   数据流、端口拓扑与安全边界；README 需要网站截图。
4. **网站增强**：按提供商列出 API Key 获取方式与链接；提供免费模型
   实时可用性检测界面；按客户端提供图文安装指导；增加全球地图页
   展示提供商地理分布。

各客户端的配置格式均经过官方文档 / 源码核实（2026-08-01）：

| 客户端 | 协议 | 配置位置 | 关键事实 |
|---|---|---|---|
| Claude Code | Anthropic Messages | `~/.claude/settings.json` `env` 块 | `ANTHROPIC_AUTH_TOKEN` 走 `Authorization: Bearer`；`ANTHROPIC_API_KEY` 走 `x-api-key`；请求带 `?beta=true` 查询参数（按路径匹配）；启动时探测 `GET /v1/models` 填充 `/model` 选择器；响应必须 SSE 流式；`ANTHROPIC_DEFAULT_HAIKU_MODEL` 取代已废弃的 `ANTHROPIC_SMALL_FAST_MODEL`（两者都写以兼容旧版） |
| Kimi CLI | OpenAI Chat Completions | `~/.kimi/config.toml` | `[providers.X] type = "openai_legacy"` + `[models.X]`（`provider` / `model` / `max_context_size`）；顶层 `default_model` 必须位于任何表头之前 |
| OpenClaw | OpenAI Chat Completions | `~/.openclaw/openclaw.json`（JSON5） | `models.providers.<name>`：`baseUrl` / `apiKey` / `api: "openai-completions"` / 静态 `models` 数组（**不**自动发现 `/v1/models`）；默认模型在 `agents.defaults.model.primary`，格式 `provider/model-id`；Zod 严格校验，未知字段拒绝启动 |
| WorkBuddy（腾讯） | OpenAI Chat Completions | `~/.workbuddy/models.json` | 扁平 JSON 数组，每模型一条：`id`/`name`/`vendor:"Custom"`/`url`(/v1 base)/`apiKey`/`supportsToolCall`/`useCustomProtocol:false`；改完需重启 WorkBuddy |

## 2. 目标

一次 `open-free-router sync`，让 9 个客户端（Pi、OMP、OpenCode、Hermes、
Codex、Claude Code、Kimi CLI、OpenClaw、WorkBuddy）共享注册表内全部免费
模型；MCP 宿主可通过标准协议查询模型、发起推理、触发刷新与同步；网站
完整呈现系统架构、API Key 获取指引、实时可用性与全球分布。

## 3. P0 范围（已实现）

### 3.1 Anthropic Messages 兼容层（Claude Code）

1. 新增 `POST /v1/messages`：system / 多 content block / `tool_use` /
   `tool_result` / `stop_sequences` / `tool_choice`（`auto|any|tool|none`、
   `disable_parallel_tool_use`）与 Chat Completions 双向转换。
2. 流式：`message_start → content_block_start/delta/stop → message_delta →
   message_stop` 的类型化 SSE 事件；文本与 `input_json_delta` 工具参数
   增量；流结束**不**发送 `[DONE]`。
3. 新增 `POST /v1/messages/count_tokens` 本地估算端点（约 4 字符/token；
   Claude Code 在端点缺失时也能本地估算，此端点保证兼容性）。
4. 鉴权同时接受 `Authorization: Bearer` 与 `x-api-key`（均为本地代理
   token，恒定时间比较）；`/v1/messages*` 的错误返回 Anthropic 错误包裹
   `{"type":"error","error":{...}}`，401/404/429 等类型正确映射。
5. 推理模型 `content: null, reasoning_content: ...` 回退拷贝，保证
   `text` block 始终存在。

### 3.2 多客户端 sync 适配器

6. `sync --agent claude`：合并写入 `~/.claude/settings.json` `env` 块
   （`ANTHROPIC_BASE_URL`（不带 `/v1` 后缀）、`ANTHROPIC_AUTH_TOKEN`、
   `ANTHROPIC_MODEL`、`ANTHROPIC_DEFAULT_HAIKU_MODEL` +
   `ANTHROPIC_SMALL_FAST_MODEL`）；保留用户其他设置；settings.json
   无法解析时拒绝写入而非覆盖。`--claude-model` / config `claude.model`
   指定主模型，默认取首个 `tool_calling: true` 模型；小模型按
   `haiku|mini|small|flash|lite|tiny|8b|9b` 线索选取。
7. `sync --agent kimi`：`~/.kimi/config.toml` 内维护
   `# >>> open-free-router managed >>>` 标记块（TOML 无依赖安全重写），
   块内 `[providers.open-free-router]`（`openai_legacy`）+ 每模型
   `[models.ofr-*]`；顶层 `default_model` 仅在缺失或已指向 `ofr-*`
   别名时设置；用户注释与自有 provider 原样保留。
8. `sync --agent openclaw`：JSON5 宽容解析；`models.providers` 去重
   （移除指向本地代理的旧条目）后写入静态模型数组（含 `cost` 零价、
   `contextWindow`、`maxTokens`）；`agents.defaults.model.primary` 仅在
   缺失或指向本地代理时设置。
9. `sync --agent workbuddy`：`~/.workbuddy/models.json` 数组按 URL 去重
   本地代理条目后追加全部注册表模型；用户自有条目保留。
10. 检测规则：默认 `sync` / serve 周期只同步已检测到的客户端
    （目录存在；OpenClaw 以配置文件存在为准，因备份目录复用
    `~/.openclaw`）；显式 `--agent NAME` 强制创建。
11. 所有适配器只下发本地代理 token，上游 API Key 永不出现在客户端
    配置中（测试断言）。

### 3.3 MCP 接口与 CLI 完善

12. 新增 `open-free-router mcp`：stdio 传输、按行分隔 JSON-RPC 2.0、
    协议版本协商（2024-11-05 / 2025-03-26 / 2025-06-18）、零第三方依赖。
13. MCP tools：`list_models`（provider / tool_calling 过滤）、
    `list_providers`（含 has_key，永不含 key 值）、`get_status`
    （代理/仪表盘可达性）、`chat`（经本地代理发起真实推理）、
    `refresh_models`、`sync_clients`。
14. `open-free-router mcp --print-config` 输出 Claude Code
    `claude mcp add` 命令与 `mcpServers` JSON 片段。
15. 新增 CLI：`status [--json]`（健康摘要）、`models [--json]`
    （模型清单）、`doctor`（配置/注册表/密钥/端口/九客户端配置体检，
    发现严重问题退出码非 0）；SIGPIPE 优雅退出。

### 3.4 实时可用性检测

16. 新增 `probe.py`：每模型一次真实最小 1-token Chat Completions 请求
    （使用注册表内该 provider 的密钥；不落盘响应内容），输出
    ok / status / latency_ms / error / checked_at。
17. 仪表盘（9057）新增 **Live Status** 标签页：`POST /api/probe`
    （需本地 token）后台并发探测，`GET /api/probe` 轮询进度与结果，
    按 provider 分组显示状态徽章与延迟；运行中 2s 轮询、空闲 60s。
18. 探测完成后聚合写入 `<data_dir>/probe-status.json`（与
    `docs/provider-status.json` 同构且含模型级明细），供
    `scripts/export-public-catalog.py` 发布到公开目录。
19. 公开站点新增 `/status/` 状态页：读取 `/api/catalog` 快照渲染
    provider/模型可用性、延迟与 `checked_at`，每 60 秒自动刷新；
    明确标注"快照来自维护者实例的真实探测，非 SLA"。

### 3.5 网站（oaf.asia）

20. `/architecture/`：系统架构图（客户端层 → 协议层 → 本地控制面 →
    上游层的内联 SVG）、请求生命周期图、同步扇出图、端口/协议/模型
    ID 信息图；符合站点 CSP（无外部资源、无内联脚本）。
21. `/models/`（模型雷达）与 `/guide/`：按提供商列出 **API Key 获取
    方式**——控制台链接、步骤、免费额度注意事项（数据源
    `docs/provider-profiles.json` 新增 `key_url` / `key_steps_zh`）。
22. `/guide/` 按客户端分节的图文安装指导：Codex、Claude Code、
    OpenCode、Hermes、Kimi CLI、OpenClaw、WorkBuddy、Pi/OMP、MCP，
    每节含终端图示、配置片段与验证步骤。
23. `/map/`：全球地图页（自托管 SVG 世界地图 + 提供商坐标标记 +
    区域统计信息图），展示 11 家提供商地理分布。
24. 站点导航与 `scripts/build-site.mjs` 构建校验同步更新；README
    （中英文）加入网站截图与新功能矩阵。

## 4. 非目标

- 模拟各客户端专有云端功能（ChatGPT Apps、Claude 订阅特性、
  WorkBuddy 桌面协作等）。
- 图像/文件多模态输入透传（上游为纯文本 Chat Completions；
  Messages 层收到 `image`/`document` block 时返回明确 400 错误）。
- 公开站点直接实测上游可用性（站点无密钥；实时数据来自本地实例
  探测快照，页面如实标注时间戳）。
- 保证第三方客户端配置格式永不变化；适配器路径常量集中于
  `sync.py` 顶部，格式变化时单点更新。

## 5. 安全要求（与既有约定一致）

- 上游 API Key 只存在于 `registry.yaml`（0600）；所有客户端配置与
  MCP 输出只携带本地代理 token。
- 探测结果与公开目录经脱敏校验（禁止 `api_key` 等字段名出现）。
- `/api/probe` 写操作要求仪表盘 token；探测错误信息在前端渲染前
  HTML 转义。
- Claude Code `settings.json` 属用户敏感文件：合并写入、解析失败即
  中止、同步前自动备份至 `~/.openclaw/agent-backup/<date>/`。

## 6. 验收与测试

- `tests/test_anthropic.py`（16 例）：转换、流式事件顺序、工具循环、
  x-api-key/Bearer 双鉴权、Anthropic 错误包裹、count_tokens。
- `tests/test_sync_clients.py`（13 例）：四适配器 schema 正确性、
  幂等性、用户配置保留、坏文件拒写、密钥不泄漏、显式/检测行为。
- `tests/test_mcp.py`（9 例）：握手/版本协商、tools 清单、过滤调用、
  无 serve 时的干净报错、stdio 行协议（含 parse error）。
- `tests/test_probe.py`（7 例）：真实假上游探测、429 原因提取、
  no_key 跳过、并发运行互斥、聚合、`/api/probe` 鉴权。
- 全量套件 144 例通过；`node scripts/build-site.mjs` 构建校验通过。

## 7. 发布说明

- 版本：0.2.0（`open_free_router.__version__`）。
- 用户迁移：无破坏性变更；`sync` 默认新增四客户端仅在检测到安装时
  生效。移除 Claude Code 接管：删除 `~/.claude/settings.json` env 块中
  `ANTHROPIC_*` 五个键即可恢复官方模型。
