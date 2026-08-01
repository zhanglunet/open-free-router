# open-free-router

> **Attribution:** This repository is derived from
> [`NoelJudeNoel/open-free-router`](https://github.com/NoelJudeNoel/open-free-router)
> under the MIT License and is independently maintained by `zhanglunet`.
> This version adds the Codex Responses API bridge, secure proxy auth, a
> third-party model catalog, Gemini tool-call compatibility, expanded tests,
> and the project documentation site. See [`NOTICE.md`](NOTICE.md).

**One command to run everything:** proxy(8337) + UI(9057) + scheduler(12h)

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/zhanglunet/open-free-router/main/scripts/install.sh)
open-free-router serve
```

Tracks free models across 11 LLM providers (OpenRouter, NVIDIA NIM, OpenCode Zen, Nous Research, StepFun, SenseNova, Groq, Google AI Studio, DeepSeek, Poolside AI, Gitee AI), runs a local proxy routing by model ID to the correct upstream, and auto-refreshes the model list. Configure once, share across Codex, Hermes, OpenCode, PI, and OMP.

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
| `open-free-router sync --agent codex [--codex-model ID]` | Generate an isolated Codex Responses API profile |
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
| `proxy.py` | Single-port proxy(8337), model-ID routing, Chat Completions and Codex Responses API |
| `responses.py` | Responses ↔ Chat messages, function tools, and SSE event conversion |
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
| `/api/status` | GET | Dashboard status |
| `/api/providers` | GET / POST | Provider list / CRUD |
| `/api/models` | GET | Model details grouped by provider |
| `/api/config` | GET / POST | Read / write config.yaml |
| `/api/refresh` | POST | Trigger model refresh (optional `--source`) |

## Tests

```bash
pip install -e ".[dev]"
python3 -m pytest tests/ -v
```

86 tests covering registry/config, refresh sources, sync, proxy authentication, Responses text/tool streams, streaming, and Codex profiles.

## Codex integration

The generated profile lives at `~/.codex/open-free-router.config.toml` and does not overwrite `~/.codex/config.toml`. Automatic selection uses a model marked `tool_calling: true`; `--codex-model` can select an explicit registry model. See [`docs/PRD-codex-integration.md`](docs/PRD-codex-integration.md) for requirements and boundaries.

## License

MIT
