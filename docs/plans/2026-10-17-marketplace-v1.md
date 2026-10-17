# PRD-015/T1 草稿：marketplace.json v1

> 直接可用的 `.claude-plugin/marketplace.json` 内容（T2 落地时原样放入仓库）。
> 对齐 `anthropics/claude-plugins-official` 格式（Apache-2.0）。

```json
{
  "$schema": "https://anthropic.com/claude-code/marketplace.schema.json",
  "name": "openfree-marketplace",
  "owner": {
    "name": "zhanglunet",
    "url": "https://github.com/zhanglunet"
  },
  "description": "模力自由港 · FreeModel Port：全球免费大模型一站发现、实测、接入（本地代理 + 模型注册表 + 只读 MCP tools）",
  "plugins": [
    {
      "name": "openfree-router",
      "source": "./plugins/openfree-router",
      "description": "本地代理：一键跑起 proxy(8337)+UI(9057)，聚合 10 家免费 LLM 提供商 38 个模型，9 个客户端共享",
      "version": "0.4.0",
      "keywords": ["llm-proxy", "free-models", "model-router", "mcp"],
      "license": "MIT"
    },
    {
      "name": "openfree-mcp-tools",
      "source": "./plugins/openfree-mcp-tools",
      "description": "只读 MCP tools：查可用免费模型 / 查可解释智能评分 / 查实时可用性探测状态（PRD-014）",
      "version": "0.1.0",
      "keywords": ["mcp", "model-availability", "readonly"],
      "license": "MIT"
    },
    {
      "name": "openfree-skill",
      "source": "./plugins/openfree-skill",
      "description": "Claude Code skill：一句话“找个免费可用的模型”，自动完成查可用→路由→fallback（PRD-016）",
      "version": "0.1.0",
      "keywords": ["skill", "model-router", "fallback"],
      "license": "MIT"
    }
  ]
}
```

## 配套 plugin.json（每个插件目录下）

以 `plugins/openfree-router/.claude-plugin/plugin.json` 为例：

```json
{
  "name": "openfree-router",
  "version": "0.4.0",
  "description": "本地代理：一键跑起 proxy(8337)+UI(9057)，聚合 10 家免费 LLM 提供商 38 个模型，9 个客户端共享",
  "keywords": ["llm-proxy", "free-models", "model-router"],
  "license": "MIT",
  "install": {
    "steps": [
      "bash scripts/install.sh --auto-discovery",
      "验证：curl -s http://127.0.0.1:8337/health | grep ok"
    ]
  }
}
```

## T2 待办（本草稿不覆盖，只列清单）

- `plugins/openfree-router/`：收敛 install.sh + 9 客户端声明式配置（Codex/Claude Code/OpenCode/Hermes/Kimi CLI/OpenClaw/WorkBuddy/Pi/OMP 各一条验证命令）。
- `plugins/openfree-mcp-tools/`：等 PRD-014 T2/T3 实现后补 tools 定义。
- `plugins/openfree-skill/`：等 PRD-016 T1 SKILL.md 定稿后放入。
- schema 版本备注：当前对齐 2026-10-17 时点的官方 `$schema`；官方改格式时 diff 本文件即可。

## 校验方法（T3 e2e 用）

1. JSON 合法：`python3 -m json.tool .claude-plugin/marketplace.json > /dev/null`
2. `$schema` 字段存在且为官方 URL。
3. 每个 `source` 指向的目录存在且含 `.claude-plugin/plugin.json`。
4. 干净环境：`/plugin marketplace add zhanglunet/open-free-router` → install → `curl 127.0.0.1:8337/health` 通。
