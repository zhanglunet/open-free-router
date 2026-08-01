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
| `open-free-router add NAME --base-url URL [--upstream-url URL] [--model ID] [--auto-refresh]` | Add a provider to registry |
| `open-free-router sync [--agent omp,opencode,codex] [--diff]` | Sync registry to agent configs; Codex uses an isolated profile |
| `open-free-router token` | Print the local inference proxy bearer token |

## Config

- `~/.config/open-free-router/config.yaml` (or `./config.yaml` in CWD)
- `registry.yaml` is the single source of truth for providers + models
- `registry.yaml` on first `serve` is auto-created from `registry.default.yaml`
- Both files get `.bak-YYYYMMDD-HHMMSS` backups on write
- API keys live in `registry.yaml` — never commit
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
├── cli.py              # argparser → routes to serve/ui/refresh/add/sync/setup
├── config.py           # Config class: loads config.yaml, resolves paths
├── registry.py         # Registry CRUD: ProviderConfig + ModelInfo dataclasses
├── registry.default.yaml  # Template with 10 upstream sources, no API keys
├── proxy.py            # Single-port proxy (8337), auth + model-ID routing
├── responses.py        # Codex Responses API compatibility over Chat Completions
├── discovery.py        # Candidate-only free-provider discovery; never mutates registry
├── public_catalog.py   # Credential-free provider/model catalog export
├── refresh.py          # Dispatches per-provider refresh from refresh_sources/
├── refresh_sources/    # Pluggable: openrouter.py, nvidia_nim.py, groq.py, etc.
├── serve.py            # Daemon: proxy + UI + scheduler + Pi models.json writer
├── sync.py             # Sync registry to Pi/OMP/OpenCode/Hermes configs (dedup-aware)
├── ui.py               # Web dashboard (9057): status, provider CRUD, refresh, config edit
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
- **ModelInfo** fields: `id` (short display name, e.g. `glm-5.2`), `upstream_id` (optional, e.g. `z-ai/glm-5.2`, falls back to `id`)
- **ProviderConfig** field: `prefix` (short channel prefix for model IDs, e.g. `nv`, `or`. Falls back to provider name)
- **config.yaml `registry:` path** resolved relative to config's parent directory, not CWD
- **Protocol surface** — health/models plus Chat Completions, Completions, Embeddings, and Responses.
- **4 model ID formats** — proxy resolves all of them:
  1. bare id       `glm-5.2`
  2. prefix/id     `nv/glm-5.2`
  3. upstream_id   `z-ai/glm-5.2`
  4. provider/upstream_id `nvidia-nim/z-ai/glm-5.2` (OMP format)
- **Sync** — `open-free-router sync` writes Pi models.json, OMP models.yml, OpenCode opencode.json, and ensures Hermes custom_providers entry from registry
- **Sync dedup** — before writing, removes all providers pointing to local proxy (baseURL contains 127.0.0.1) to prevent duplicate accumulation; Pi always overwrites entire file

## Scripts

- `scripts/install.sh` — one-liner: clone → venv → pip install → optional systemd
- `scripts/export-public-catalog.py` — redacted registry/status export for the public model radar
- `scripts/discover-models.mjs` — build-time public candidate snapshot for Cloudflare
- `contrib/systemd/open-free-router.service` — systemd unit file for Linux auto-start + auto-restart

## Testing

- `tests/test_responses.py` — auth, Responses conversion, SSE text/function calls, live index rebuild
- `tests/test_sync_codex.py` — Codex profile and upstream-key isolation
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
