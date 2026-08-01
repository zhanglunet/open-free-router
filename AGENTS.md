# open-free-router — AGENTS.md

## Entrypoint

- CLI: `open-free-router` (defined in `pyproject.toml` → `open_free_router.cli:main`)
- Install: `pip install -e .` (venv required, Python 3.11+) or `scripts/install.sh`
- Dependencies: `pyyaml` + `requests`; no web framework (stdlib `http.server`)

## Commands

| Command | What it does |
|---|---|
| `open-free-router serve` | **★ One command:** proxy(8337) + UI(9057) + scheduler (configurable interval, default 12h) |
| `open-free-router ui` | Web dashboard standalone |
| `open-free-router setup` | Interactive wizard: fill in API keys for all providers |
| `open-free-router refresh [--source NAME] [--dry-run]` | Poll provider APIs for free model changes |
| `open-free-router discover [--dry-run] [--output PATH]` | Find review-only free-provider candidates from the public directory |
| `open-free-router discover --test --adopt` | Test candidates with declared credential env vars and adopt only successful models |
| `open-free-router add NAME --base-url URL [--upstream-url URL] [--model ID] [--auto-refresh]` | Add a provider to registry |
| `open-free-router sync [--agent pi,omp,opencode,hermes,codex,claude,kimi,openclaw,workbuddy] [--diff]` | Sync registry to agent configs; Codex/Claude get isolated treatment |
| `open-free-router token` | Print the local inference proxy bearer token |
| `open-free-router mcp [--print-config]` | MCP stdio server (line-delimited JSON-RPC 2.0); or print host registration snippets |
| `open-free-router status [--json]` | One-shot health summary (registry stats, proxy/UI reachability) |
| `open-free-router models [--json]` | List registry models with capability flags |
| `open-free-router doctor` | Diagnose install: config, keys, ports, all 9 client config files |

## Config

- `~/.config/open-free-router/config.yaml` (or `./config.yaml` in CWD)
- `registry.yaml` is the single source of truth for providers + models
- `registry.yaml` on first `serve` is auto-created from `registry.default.yaml`
- Both files get `.bak-YYYYMMDD-HHMMSS` backups on write
- API keys live in `registry.yaml` — never commit
- Auto-discovered providers use `api_key_env`; registry stores the environment-variable name, never its value
- `refresh_interval_hours` in config.yaml controls scheduler frequency (default 12)

## Bootstrap

- `src/open_free_router/registry.default.yaml` — template with 10 upstream sources and model lists, no API keys
- `scripts/install.sh` — one-liner installer (git clone + venv + setup). `--with-systemd` for systemd auto-start
- `contrib/systemd/open-free-router.service` — systemd service unit
- First `serve` auto-bootstraps config + registry; `open-free-router setup` walks through key entry interactively
- On Linux with systemd: `bash install.sh --with-systemd` sets up auto-start via `open-free-router.service`

## Architecture

```
src/open_free_router/
├── cli.py              # argparser → serve/ui/refresh/add/sync/setup/mcp/status/models/doctor
├── config.py           # Config class: loads config.yaml, resolves paths
├── registry.py         # Registry CRUD: ProviderConfig + ModelInfo dataclasses
├── registry.default.yaml  # Template with 11 upstream sources, no API keys
├── proxy.py            # Single-port proxy (8337), auth + model-ID routing
├── responses.py        # Codex Responses API compatibility over Chat Completions
├── anthropic.py        # Anthropic Messages API compatibility (Claude Code): /v1/messages
├── probe.py            # Live availability probing: 1-token real request per model
├── mcp_server.py       # MCP stdio server (line-delimited JSON-RPC 2.0, 6 tools)
├── discovery.py        # Candidate-only free-provider discovery; never mutates registry
├── public_catalog.py   # Credential-free provider/model catalog export
├── refresh.py          # Dispatches per-provider refresh from refresh_sources/
├── refresh_sources/    # Pluggable: openrouter.py, nvidia_nim.py, groq.py, etc.
├── serve.py            # Daemon: proxy + UI + scheduler + Pi models.json writer
├── sync.py             # Sync registry to 9 client configs (dedup-aware)
├── ui.py               # Web dashboard (9057): status, provider CRUD, refresh, live probe
├── templates/          # UI templates (index.html)
└── web_static/         # UI static assets (CSS, JS)
```

## Key conventions

- **Single-port proxy (8337)** — all agents point to one base_url; routing by model ID via `_model_index`
- **Multi-threaded** — both proxy and UI use `ThreadingHTTPServer` (stdlib) to avoid head-of-line blocking
- **User-Agent** — upstream requests include `User-Agent: open-free-router/0.1` to avoid Cloudflare 1010 blocks (Python urllib default triggers bot detection)
- **Timeout** — upstream `urlopen` timeout is 120s (configurable via `upstream_timeout` in config.yaml)
- **Zero web framework** — uses stdlib `http.server`; no Flask/FastAPI/uvicorn
- **Refresh sources** are pluggable modules. Each must export `fetch(upstream_url, api_key) -> list[ModelInfo]`
- **Pi models.json** written by `serve.py` on startup and after each refresh. Format: `{providers: {name: {baseUrl, models: [...]}}}`. All providers point to local proxy; routing is by model ID.
- **Scheduler interval** configurable via `config.yaml: refresh_interval_hours` (default 12)
- **Discovery interval** configurable via `config.yaml: discovery.interval_hours` (default 24); candidates are review-only and never auto-promoted
- **Auto-adoption** is opt-in with `discovery.auto_test` + `discovery.auto_adopt`; it requires public HTTPS, declared OpenAI compatibility, a user-provided credential env, and a valid live response
- **ModelInfo** fields: `id` (short display name, e.g. `glm-5.2`), `upstream_id` (optional, e.g. `z-ai/glm-5.2`, falls back to `id`)
- **ProviderConfig** field: `prefix` (short channel prefix for model IDs, e.g. `nv`, `or`. Falls back to provider name)
- **config.yaml `registry:` path** resolved relative to config's parent directory, not CWD
- **Protocol surface** — health/models plus Chat Completions, Completions, Embeddings, and Responses.
- **4 model ID formats** — proxy resolves all of them:
  1. bare id       `glm-5.2`
  2. prefix/id     `nv/glm-5.2`
  3. upstream_id   `z-ai/glm-5.2`
  4. provider/upstream_id `nvidia-nim/z-ai/glm-5.2` (OMP format)
- **Sync** — `open-free-router sync` writes Pi models.json, OMP models.yml, OpenCode opencode.json, Hermes custom_providers, Codex profile, Claude Code settings.json env block, Kimi config.toml managed block, OpenClaw models.providers, and WorkBuddy models.json
- **Sync dedup** — before writing, removes all providers pointing to local proxy (baseURL contains 127.0.0.1) to prevent duplicate accumulation; Pi always overwrites entire file
- **Sync detection** — claude/kimi/workbuddy sync only when their home dir exists (openclaw: config file exists, because sync backups live under ~/.openclaw); explicit `--agent NAME` forces creation
- **Anthropic Messages** — `/v1/messages` + `/v1/messages/count_tokens`; auth accepts `Authorization: Bearer` AND `x-api-key`; errors use the Anthropic envelope; streams end after `message_stop` with no `[DONE]`
- **MCP** — `open-free-router mcp` speaks newline-delimited JSON-RPC over stdio; tools: list_models, list_providers, get_status, chat, refresh_models, sync_clients
- **Probe** — dashboard Live Status tab → POST /api/probe (UI token) runs one 1-token real request per model; snapshot persisted to `<data_dir>/probe-status.json` in provider-status.json shape

## Scripts

- `scripts/install.sh` — one-liner: clone → venv → pip install → optional systemd
- `scripts/export-public-catalog.py` — redacted registry/status export for the public model radar
- `scripts/discover-models.mjs` — build-time public candidate snapshot for Cloudflare
- `scripts/install.sh --codex` — one-click isolated install plus Codex profile; deployed as `https://oaf.asia/install.sh`
- `contrib/systemd/open-free-router.service` — systemd unit file for Linux auto-start + auto-restart

## Public development log

- `site/data/devlog.json` is the single source for the public `/logs/` history.
- Every user-visible feature, fix, security hardening, release, or website change must add one concise Chinese entry in the same change set.
- Add entries with `npm run log:add -- --title "标题" --summary "摘要" --type feature --items "变化一|变化二" --commit abc1234` or edit the JSON directly when links are needed.
- Allowed types: `feature`, `improvement`, `security`, `milestone`. Never include credentials, private paths, registry content, or user data.

## Testing

- `tests/test_responses.py` — auth, Responses conversion, SSE text/function calls, live index rebuild
- `tests/test_anthropic.py` — Messages conversion, stream event ordering, tool loop, dual auth, error envelope
- `tests/test_sync_codex.py` — Codex profile and upstream-key isolation
- `tests/test_sync_clients.py` — Claude/Kimi/OpenClaw/WorkBuddy adapters: schema, idempotency, user-config preservation, key isolation
- `tests/test_mcp.py` — JSON-RPC handshake, version negotiation, tool calls, stdio line protocol
- `tests/test_probe.py` — live probe against fake upstreams, aggregation, /api/probe auth
- `tests/test_config.py` — 4 tests: defaults, custom values, registry path resolution
- `tests/test_serve.py` — 2 tests: Pi models.json format, skip when no Pi dir
- `tests/test_discovery.py` — candidate filtering, registry exclusion, and secure persistence
- `tests/test_public_catalog.py` — redacted public catalog and credential-field rejection
- Run: `pip install -e ".[dev]" && python3 -m pytest tests/ -v`
- No CI/CD configured yet

## Supported providers (11)

openrouter, nvidia-nim, opencode-zen-free, sensenova, stepfun, google-ai-studio, groq, deepseek, nous, poolside, gitee-ai

## Related

- `registry.default.yaml` defines the canonical model list for each provider
- `refresh_sources/*.py` implement `fetch()` for auto-refresh capable providers
- `sync.py`'s `write_pi_models()` is called by both `serve.py` (on startup + refresh) and `ui.py`
- Sync removes stale local-proxy entries before re-adding from registry (prevents duplicate accumulation)
