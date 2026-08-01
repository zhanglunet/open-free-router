# open-free-router

🌐 **Project website:** [oaf.asia](https://oaf.asia) · [Install guide](https://oaf.asia/guide/) · [Free Model Radar](https://oaf.asia/models/) · [Live Status](https://oaf.asia/status/) · [Architecture](https://oaf.asia/architecture/) · [World Map](https://oaf.asia/map/)

> **Attribution:** This repository is derived from
> [`NoelJudeNoel/open-free-router`](https://github.com/NoelJudeNoel/open-free-router)
> under the MIT License and is independently maintained by `zhanglunet`.
> This version adds the Codex Responses API bridge, the Anthropic Messages
> API bridge (Claude Code), nine-client config sync, a built-in MCP server,
> live availability probing, secure proxy auth, a third-party model catalog,
> Gemini tool-call compatibility, expanded tests, and the project
> documentation site. See [`NOTICE.md`](NOTICE.md).

**One command to run everything:** proxy(8337) + UI(9057) + scheduler(12h)

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/zhanglunet/open-free-router/main/scripts/install.sh)
open-free-router serve
```

Tracks free models across 11 LLM providers (OpenRouter, NVIDIA NIM, OpenCode Zen, Nous Research, StepFun, SenseNova, Groq, Google AI Studio, DeepSeek, Poolside AI, Gitee AI), runs a local proxy routing by model ID to the correct upstream, and auto-refreshes the model list. Configure once, share across **9 clients**: Codex, Claude Code, OpenCode, Hermes, Kimi CLI, OpenClaw, WorkBuddy, Pi, and OMP — plus a built-in MCP server for any MCP host.

## Website preview

| Home | Architecture |
|---|---|
| ![Home page](docs/screenshots/home.png) | ![Architecture page](docs/screenshots/architecture.png) |

| Live status | World map |
|---|---|
| ![Live status page](docs/screenshots/status.png) | ![World map page](docs/screenshots/map.png) |

| Local dashboard · live probing | Model radar · API-key guides |
|---|---|
| ![Dashboard live probing](docs/screenshots/dashboard-live.png) | ![Model radar](docs/screenshots/models.png) |

## Client support matrix

| Client | Protocol | One command | Written to |
|---|---|---|---|
| **Codex CLI** | Responses API | `sync --agent codex` | `~/.codex/open-free-router.config.toml` (isolated profile) |
| **Claude Code** | Anthropic Messages | `sync --agent claude` | `~/.claude/settings.json` `env` block (merged) |
| **OpenCode** | Chat Completions | `sync --agent opencode` | `~/.config/opencode/opencode.jsonc` |
| **Hermes** | Chat Completions | `sync --agent hermes` | `~/.hermes/config.yaml` (runtime model discovery) |
| **Kimi CLI** | Chat Completions | `sync --agent kimi` | `~/.kimi/config.toml` (managed marker block) |
| **OpenClaw** | Chat Completions | `sync --agent openclaw` | `~/.openclaw/openclaw.json` (static model catalog) |
| **WorkBuddy** | Chat Completions | `sync --agent workbuddy` | `~/.workbuddy/models.json` (restart to apply) |
| **Pi / OMP** | Chat Completions | automatic via serve | `~/.pi/agent/models.json` / `~/.omp/agent/models.yml` |
| **MCP hosts** | MCP (stdio) | `claude mcp add … -- open-free-router mcp` | any `mcpServers` config |

Every client receives only the **local proxy token** — upstream API keys never leave `registry.yaml`. Default `sync` touches only detected clients; explicit `--agent NAME` forces creation.

## Install

**Option 1: One-liner (recommended)**
```bash
bash <(curl -fsSL https://raw.githubusercontent.com/zhanglunet/open-free-router/main/scripts/install.sh)
```

Install with systemd auto-start:
```bash
bash <(curl -fsSL ...) --with-systemd
```

**Option 2: Manual**
```bash
git clone https://github.com/zhanglunet/open-free-router.git
cd open-free-router
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Commands

| Command | Description |
|---|---|
| `open-free-router serve` | **★ One command:** proxy(8337) + UI(9057) + scheduler(12h) |
| `open-free-router setup` | Interactive wizard: fill in API keys for all providers |
| `open-free-router refresh [--source NAME] [--dry-run]` | Refresh free models from APIs |
| `open-free-router add NAME --base-url URL [--model ID] [--auto-refresh]` | Add a provider |
| `open-free-router sync --agent codex,claude,kimi,…` | Sync 9 client configs; `--codex-model` / `--claude-model` pick defaults |
| `open-free-router mcp [--print-config]` | Built-in MCP stdio server; print host registration snippets |
| `open-free-router status [--json]` | One-shot health summary |
| `open-free-router models [--json]` | List registry models with capability flags |
| `open-free-router doctor` | Diagnose install: config, keys, ports, 9 client config files |
| `open-free-router token` | Print the local inference proxy token for command auth |
| `open-free-router ui` | Web dashboard standalone (debug) |

## Quick Start

```bash
# 1. Auto-creates config on first run, just start
open-free-router serve

# 2. In another terminal, enter API keys
open-free-router setup

# 3. Point all your agents to http://127.0.0.1:8337/v1

# 4. Open dashboard: http://127.0.0.1:9057

# 5. Optional: connect Codex
open-free-router sync --agent codex --codex-model gq/gpt-oss-120b
codex --profile open-free-router
```

## Config

`~/.config/open-free-router/config.yaml`:

```yaml
registry: ~/.config/open-free-router/registry.yaml

proxy:
  host: 127.0.0.1
  port: 8337

ui:
  host: 127.0.0.1
  port: 9057

refresh_interval_hours: 12
```

First `serve` auto-creates config + registry from defaults — no manual setup needed.

### Security notes

- `ui.host` and `proxy.host` default to `127.0.0.1` (local only). We do **not** recommend setting either to `0.0.0.0` or exposing them on an untrusted LAN/public network: the dashboard's write endpoints (save config, add/edit providers, trigger refresh) require a local auth token, but the dashboard itself has no HTTPS or fine-grained permissions — it isn't designed for public exposure.
- On first start, the dashboard generates a random token at `<config dir>/ui.token` (mode 0600). The browser will prompt for it once per session when you save config, add a provider, or trigger a refresh. Requests without a valid token get a 401.
- The inference proxy generates a separate `<config dir>/proxy.token` (mode 0600). Every inference POST requires that bearer token. Synced agent configs receive only this local token, never an upstream provider key.
- `registry.yaml` stores provider API keys **in plaintext** with mode 0600. Treat it and its backups as sensitive; never commit or share them.

## Architecture

| Module | Purpose |
|---|---|
| `proxy.py` | Single-port proxy(8337), model-ID routing, Chat Completions, Codex Responses API, and Anthropic Messages API |
| `responses.py` | Responses ↔ Chat messages, function tools, and SSE event conversion |
| `anthropic.py` | Messages ↔ Chat conversion (Claude Code): content blocks, tool_use/tool_result, typed SSE events |
| `probe.py` | Live availability probing: one real 1-token request per model |
| `mcp_server.py` | MCP stdio server (line-delimited JSON-RPC 2.0, 6 tools) |
| `serve.py` | Daemon: proxy + UI + scheduler + auto-write Pi models.json |
| `ui.py` | Web dashboard(9057): status, provider CRUD, model refresh, live config editor |
| `refresh.py` | Poll provider APIs for free model changes. Pluggable sources |
| `registry.py` | Registry (ProviderConfig / ModelInfo data model + YAML persistence) |
| `config.py` | Config loader (config.yaml + defaults + path resolution) |
| `cli.py` | CLI entry point (argparse routing) |

### Design Principles

- **Single port 8337** — all agents point to one base_url; routing by model ID
- **Multi-threaded** — ThreadingHTTPServer, no head-of-line blocking
- **True streaming passthrough** — `stream: true` requests relay upstream SSE line by line, not buffered and returned all at once
- **User-Agent** — upstream requests set `open-free-router/0.1` to avoid Cloudflare 1010 blocks
- **Zero web framework** — uses stdlib `http.server`, no Flask/FastAPI
- **Pluggable refresh sources** — one module per provider in `refresh_sources/`, exports `fetch(base_url, api_key) → list[ModelInfo]`. OpenRouter/NVIDIA NIM/Nous/SenseNova/Poolside auto-detect free models from pricing fields; Groq/DeepSeek/StepFun/OpenCode Zen have no pricing field and use a hand-maintained allowlist
- **Sync preserves hand-edited config** — agent config files (e.g. OMP's `models.yml`) are edited with `ruamel.yaml` (structured, comment-preserving) rather than text substitution, so only entries pointing at the local proxy are added/removed; any other providers, comments, or formatting the user configured by hand are left untouched
- **Auto Pi sync** — writes `~/.pi/agent/models.json` when Pi config dir exists

## API Endpoints

| Path | Method | Description |
|---|---|---|
| `/v1/models` | GET | List all free models (OpenAI-compatible) |
| `/v1/chat/completions` | POST | Route by model ID to upstream (OpenAI-compatible) |
| `/v1/responses` | POST | Codex Responses-compatible endpoint with SSE and function tools |
| `/v1/messages` | POST | Anthropic Messages-compatible endpoint (Claude Code) with SSE and tool_use |
| `/v1/messages/count_tokens` | POST | Local input-token estimate |
| `/api/status` | GET | Dashboard status |
| `/api/providers` | GET / POST | Provider list / CRUD |
| `/api/models` | GET | Model details grouped by provider |
| `/api/config` | GET / POST | Read / write config.yaml |
| `/api/refresh` | POST | Trigger model refresh (optional `--source`) |
| `/api/probe` | GET / POST | Live availability probing (POST requires dashboard token) |

## Tests

```bash
pip install -e ".[dev]"
python3 -m pytest tests/ -v
```

144 tests covering registry/config, refresh sources, nine-client sync (incl. Claude/Kimi/OpenClaw/WorkBuddy adapters), proxy authentication (Bearer + x-api-key), Responses and Messages conversion, streaming, live probing, MCP handshake/tools, and Codex profiles.

## Claude Code integration

```bash
open-free-router sync --agent claude   # writes the env block in ~/.claude/settings.json
claude                                 # free models appear in the /model picker
```

The proxy implements the Anthropic Messages API on `/v1/messages` (streaming + tools). `ANTHROPIC_BASE_URL` points at `http://127.0.0.1:8337` and `ANTHROPIC_AUTH_TOKEN` carries only the local proxy token. Remove the `ANTHROPIC_*` keys from the env block to restore official models.

## MCP interface

```bash
claude mcp add --scope user open-free-router -- open-free-router mcp
open-free-router mcp --print-config
```

Six tools over stdio JSON-RPC: `list_models`, `list_providers`, `get_status`, `chat`, `refresh_models`, `sync_clients`.

## Live availability

The dashboard's **Live Status** tab fires one real 1-token request per model and shows status, latency, and failure reasons. Aggregated snapshots drive the public [status page](https://oaf.asia/status/) (auto-refreshing every 60 s). Per-provider **API-key acquisition steps** live on each provider card of the [model radar](https://oaf.asia/models/#providers).

## Codex integration

The generated profile lives at `~/.codex/open-free-router.config.toml` and does not overwrite `~/.codex/config.toml`. Automatic selection uses a model marked `tool_calling: true`; `--codex-model` can select an explicit registry model. See [`docs/PRD-codex-integration.md`](docs/PRD-codex-integration.md) for requirements and boundaries.

## License

MIT
