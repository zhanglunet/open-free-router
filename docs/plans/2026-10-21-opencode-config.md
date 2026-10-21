# PRD-116/T1：OpenCode 配置格式

> 对应：zhanglunet/trend#579（PRD-116/T1）
> PRD：zhanglunet/trend `adopted/` 下的 PRD-116
> 状态：**格式核对完毕，不生成预设、不改 `sync_opencode`。**
> 来源：anomalyco/opencode，MIT，提交 `5fa426f68d1dbf3c8aaa01557e357719f0577438`（2026-10-21）。对照过文档 `packages/web/src/content/docs/config.mdx`、发现顺序 `packages/core/src/config/discovery.ts`、加载 `packages/core/src/config.ts`、v1 提供商 `packages/core/src/v1/config/provider.ts`、迁移 `packages/core/src/v1/config/migrate.ts`、v2 提供商 `packages/schema/src/config/provider.ts`。

机器可读的约定在 `docs/presets/opencode-contract.yaml`。`tests/test_opencode_preset_contract.py` 钉住下面这些容易写错的点。

## 一句话

给 OpenCode 的预设继续写 **v1 的 `provider` 对象**。当前加载器会把它迁成 v2 的 `providers`。直接写 v2 会让仍按文档读取 `provider` 的版本看不见这份预设。本仓库的 `sync_opencode` 今天写的就是 v1。

## 文件与优先级

文件名只有两个：`opencode.json`、`opencode.jsonc`。同一目录里两个都在时，后读到的 `opencode.jsonc` 覆盖冲突的键，不冲突的键留着。解析器接受注释和尾逗号。

本仓库同步写的是 `~/.config/opencode/opencode.jsonc`。官方文档里的全局文件名是 `opencode.json`。两个名字都合法。

同一键以后出现的文档为准。加载顺序（越往下越优先）：

1. 托管配置里拆出来的 `policies`（策略反过来排，文件里的策略仍是最后说了算）
2. 组织的 `.well-known/opencode`
3. 全局目录（系统目录，然后是用户的 `~/.config/opencode`）
4. 环境变量 `OPENCODE_CONFIG` 指到的文件
5. 从当前目录向上找到的项目文件
6. 项目里的 `.opencode` 目录
7. 环境变量 `OPENCODE_CONFIG_CONTENT` 的正文
8. 托管配置的其余字段，以及 macOS 的 `ai.opencode.managed` 偏好（最高）

系统目录：macOS 是 `/Library/Application Support/opencode`，Linux 是 `/etc/opencode`，Windows 是 `%ProgramData%\opencode`。

`{env:NAME}` 会在解析前替换。变量不存在时换成空字符串，不会改用另一把钥匙。`{file:path}` 会把文件内容嵌进去。提交到仓库里的预设不用 `{file:…}`，示例里的钥匙是字面量 `placeholder-local-proxy-token`。用户自己的文件可以用 `{env:OPEN_FREE_ROUTER_TOKEN}` 指向本地代理的令牌，不要指向上游密钥。

## v1 提供商，预设就写这个

文档页在这次提交里仍把提供商写在 `provider` 下。字段以 v1 的 schema 为准：

| 位置 | 含义 |
|---|---|
| `provider.<id>.npm` | 适配包。走本地代理时用 `@ai-sdk/openai-compatible` |
| `provider.<id>.options.baseURL` | 上游根地址 |
| `provider.<id>.options.apiKey` | 该地址的钥匙 |
| `provider.<id>.models.<key>` | 模型表。`<key>` 不能含 `/` |
| `models.<key>.id` | 真正发给上游的模型 id，可以含 `/` |
| `models.<key>.reasoning` | 布尔。只对 v1 有意义，见下 |
| `models.<key>.tool_call` | 布尔。迁到 v2 时变成 `capabilities.tools` |
| `models.<key>.limit.context` / `output` | 上下文与输出上限 |

`model` 和 `small_model` 都是 `提供商id/模型键`，**恰好一个斜杠**，不能有 `#`。对不上这个形状的，v2 会把整条引用丢掉，并记一条“unsupported legacy model reference”。所以模型表的键必须是斜杠前面那一段能单独成立的短名；带组织前缀的上游 id 放在 `id` 字段里。

提供商 id 同样不能含 `/` 或 `#`。加载器只改两个旧 id：`azure-cognitive-services` → `azure`，`google-vertex-anthropic` → `google-vertex`。`open-free-router` 保持原样。

## 加载器把它变成什么

`migrateStandardProvider` 的对应关系：

| v1 | v2 |
|---|---|
| 对象名 `provider` | 对象名 `providers` |
| `npm: @ai-sdk/openai-compatible` | `package: aisdk:@ai-sdk/openai-compatible` |
| `options.baseURL`、`options.apiKey` | `settings` 里的同名字段 |
| `options.headers` / `options.body` | 顶层 `headers` / `body` |
| `options.setCacheKey` | `compatibility.supportsPromptCacheKey`，并从 settings 里拿掉 |
| `models.<key>.id` | `modelID` |
| `limit` | 原样留下 |
| `tool_call: true/false` | `capabilities.tools` |
| `reasoning: true/false` | **不迁移。** `migrateModel` 的返回值里没有这个字段 |

最后一行是预设作者最容易看错的地方。今天 `sync_opencode` 会写 `reasoning` 布尔，v1 读得懂；换成只认 v2 的版本之后，这个布尔不会变成推理能力。要在 v2 里表达推理，得写 v2 的 `capabilities.reasoning` 对象（`supported` 加上可选的 `effort` / `budget`）。本任务的预设约定仍写 v1 布尔，并在契约里把结果标成 `dropped`，避免以后有人以为它还在。

`small_model` 若能通过上面的斜杠检查，会挪到 `agents.title` 的模型上。原生 v2 配置没有 `small_model` 这个键。预设为了两边都能读，继续写 `small_model`。

## 预设允许的形状

一份给本路由器的预设，顶层只能有：

- `$schema`
- `model`
- `small_model`
- `provider`

不能写 `providers`、`permissions`、`mcp`、`plugin`、`plugins`、`server`、`tools`、`agent`、`instructions`。那些会改 OpenCode 的权限、插件或监听地址，不属于“把模型指到本地代理”。

提供商上只能有 `name`、`npm`、`options`、`models`。`options` 只能有 `baseURL` 和 `apiKey`。`apiKey` 是本地代理令牌，不是上游密钥。示例：

```json
{
  "model": "open-free-router/placeholder-model",
  "small_model": "open-free-router/placeholder-small",
  "provider": {
    "open-free-router": {
      "name": "Open Free Router",
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://127.0.0.1:8337/v1",
        "apiKey": "placeholder-local-proxy-token"
      },
      "models": {
        "placeholder-model": {
          "name": "Placeholder Model",
          "id": "placeholder-model",
          "reasoning": false,
          "tool_call": true,
          "limit": { "context": 8192, "output": 1024 }
        }
      }
    }
  }
}
```

`127.0.0.1:8337` 是本仓库已经在用的本地代理，不是外部主机。迁完之后 `reasoning` 不在了，`tool_call` 变成 `capabilities.tools`，`npm` 变成 `package: aisdk:@ai-sdk/openai-compatible`，`baseURL` 和 `apiKey` 留在 `settings`。契约文件里的 `migrated_sample` 就是这个结果。

## 和今天的同步差在哪

`sync_opencode`（`src/open_free_router/sync.py`）已经在写 v1，有三处后续做预设时不能照搬：

1. 模型表的键取 `m.id` 最后一段并去掉 `:free`，这和“键里不能有斜杠”一致。但 `id` 字段写的是注册表的 `m.id`，不是 `effective_upstream_id`。经本地代理时短 id 也能路由；若预设要直连上游，`id` 必须是上游 id。
2. 它会把整个 JSON 文件重新序列化。注释、尾逗号、文件里其他手写内容的排版都保不住。不可解析的文件会放弃，这点是对的。预设写入应只改自己的提供商键。
3. 钥匙写的是代理令牌本身（没有令牌时是 `sk-no-key`）。仓库里的示例不能写真实令牌，也不能写上游钥匙。

## 本任务不做

- 不新增 `open-free-router preset` 之类的命令。
- 不改 `sync_opencode` 的输出。
- 不把 v2 的 `providers` 写成第二份并行配置。
- 不在示例里放真实密钥、账号、非回环地址，也不使用 `{file:…}`。
