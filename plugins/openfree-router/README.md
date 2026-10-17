# openfree-router 插件（PRD-015/T2）

本地代理 + 9 客户端同步打包。marketplace 条目：`openfree-marketplace` → `openfree-router` v0.4.0。

## 目录

```
plugins/openfree-router/
├── .claude-plugin/plugin.json  # 插件清单（含 install.steps）
├── clients.yaml                # 9 客户端声明式打包规范（同步命令/配置文件/验证命令）
├── scripts/install.sh          # 一键安装脚本（与仓库根 scripts/install.sh 同步，见下注）
└── README.md                   # 本文件
```

> 注：`scripts/install.sh` 是仓库根 `scripts/install.sh` 的原样拷贝，保证插件目录自包含
> （marketplace 安装时只取插件目录）。改动根脚本后请同步拷贝一份到这里。

## 安装

```bash
bash scripts/install.sh --auto-discovery   # 克隆 + venv + pip install -e .
open-free-router setup                      # 交互式填写各家 API key（可选）
open-free-router serve                      # 启动 proxy(8337) + UI(9057)
open-free-router sync                       # 同步本机已安装客户端的配置
curl -s http://127.0.0.1:8337/health | grep -q ok && echo proxy-ok
```

`install.sh` 支持 `--with-systemd`（开机自启）、`--codex`（顺带建 Codex 独立 profile）。

## 9 客户端接入（声明式，以 clients.yaml 为准）

| 客户端 | 协议 | 同步命令 | 配置文件 | 验证命令 |
|---|---|---|---|---|
| Codex CLI | Responses API | `open-free-router sync --agent codex` | `~/.codex/open-free-router.config.toml`（独立 profile） | `test -f ~/.codex/open-free-router.config.toml && grep -q '127.0.0.1:8337' ~/.codex/open-free-router.config.toml && curl -sf http://127.0.0.1:8337/health \| grep -q ok && echo 'codex ok'` |
| Claude Code | Anthropic Messages | `open-free-router sync --agent claude` | `~/.claude/settings.json`（env 块合并） | `grep -q '127.0.0.1:8337' ~/.claude/settings.json && curl -sf http://127.0.0.1:8337/health \| grep -q ok && echo 'claude ok'` |
| OpenCode | Chat Completions | `open-free-router sync --agent opencode` | `~/.config/opencode/opencode.jsonc` | `test -f ~/.config/opencode/opencode.jsonc && grep -q '127.0.0.1:8337' ~/.config/opencode/opencode.jsonc && curl -sf http://127.0.0.1:8337/health \| grep -q ok && echo 'opencode ok'` |
| Hermes | Chat Completions | `open-free-router sync --agent hermes` | `~/.hermes/config.yaml` | `test -f ~/.hermes/config.yaml && grep -q '127.0.0.1:8337' ~/.hermes/config.yaml && curl -sf http://127.0.0.1:8337/health \| grep -q ok && echo 'hermes ok'` |
| Kimi CLI | Chat Completions | `open-free-router sync --agent kimi` | `~/.kimi-code/config.toml`（托管标记块） | `test -f ~/.kimi-code/config.toml && grep -q '127.0.0.1:8337' ~/.kimi-code/config.toml && curl -sf http://127.0.0.1:8337/health \| grep -q ok && echo 'kimi ok'` |
| OpenClaw | Chat Completions | `open-free-router sync --agent openclaw` | `~/.openclaw/openclaw.json` | `test -f ~/.openclaw/openclaw.json && grep -q '127.0.0.1:8337' ~/.openclaw/openclaw.json && curl -sf http://127.0.0.1:8337/health \| grep -q ok && echo 'openclaw ok'` |
| WorkBuddy | Chat Completions | `open-free-router sync --agent workbuddy` | `~/.workbuddy/models.json`（重启生效） | `test -f ~/.workbuddy/models.json && grep -q '127.0.0.1:8337' ~/.workbuddy/models.json && curl -sf http://127.0.0.1:8337/health \| grep -q ok && echo 'workbuddy ok'` |
| Pi | Chat Completions | `open-free-router sync --agent pi` | `~/.pi/agent/models.json` | `test -f ~/.pi/agent/models.json && grep -q '127.0.0.1:8337' ~/.pi/agent/models.json && curl -sf http://127.0.0.1:8337/health \| grep -q ok && echo 'pi ok'` |
| OMP | Chat Completions | `open-free-router sync --agent omp` | `~/.omp/agent/models.yml` | `test -f ~/.omp/agent/models.yml && grep -q '127.0.0.1:8337' ~/.omp/agent/models.yml && curl -sf http://127.0.0.1:8337/health \| grep -q ok && echo 'omp ok'` |

聚合检查（一次看全 9 个）：`open-free-router doctor`

## 约定

- 同步是幂等的：重复跑 `sync` 只会重写本工具托管的段落，用户手写内容不动；写入前自动备份到 `~/.openclaw/agent-backup/<日期>/`。
- Codex 走独立 profile（`codex --profile open-free-router`），Claude Code 走 settings.json env 块合并——两者互不干扰。
- API key 只存在 `registry.yaml` / 环境变量里，永不进仓库（见 `.gitleaksignore`）。
