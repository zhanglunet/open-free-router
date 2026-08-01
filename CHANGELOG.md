# Changelog

## Unreleased

### Added

- Public `/logs/` development history with search and type filters, a structured
  append-only log workflow, and a dedicated recommendation article at
  `/stories/free-model-port/` with a copy-ready WeChat Moments message.

- Anthropic Messages API compatibility (`POST /v1/messages` +
  `/v1/messages/count_tokens`) with typed SSE streaming, tool_use/tool_result
  translation, dual `Authorization: Bearer` / `x-api-key` auth, and Anthropic
  error envelopes — Claude Code connects with one sync command.
- Four new client sync adapters: Claude Code (`~/.claude/settings.json` env
  block), Kimi CLI (`~/.kimi/config.toml` managed block), OpenClaw
  (`~/.openclaw/openclaw.json` models.providers), and WorkBuddy
  (`~/.workbuddy/models.json`) — all dedup-aware, user-config-preserving, and
  covered by adapter tests; 9 clients total now sync from one registry.
- Built-in MCP stdio server (`open-free-router mcp`) with six tools
  (list_models, list_providers, get_status, chat, refresh_models,
  sync_clients) and `--print-config` host registration snippets.
- New CLI commands: `status [--json]`, `models [--json]`, and `doctor`
  (full install diagnosis including all client config files).
- Live availability probing: dashboard "Live Status" tab runs one real
  1-token request per model (`POST/GET /api/probe`), persists a redacted
  snapshot for the public catalog.
- Website: `/architecture/` system diagram & infographic page, `/status/`
  auto-refreshing live availability page, `/map/` global provider
  distribution map (original Natural Earth dot-matrix base), per-client
  illustrated setup guides, and per-provider API-key acquisition guidance
  on the model radar.
- PRD: `docs/PRD-multi-client-mcp.md` covering all of the above.

- “模力自由港 / FreeModel Port” brand identity, generated primary mark,
  favicon, social card, dedicated `/brand/` page, and a complete deep-harbor
  visual redesign across the homepage, free-model radar, guide, and 404 page.

- Detailed `/guide/` for one-click installation, Codex CLI, the ChatGPT Codex
  desktop client, isolated profiles, troubleshooting, CLI commands, and APIs.
- Public `/install.sh` plus machine-readable `/api/install` manifest.
- Credential-aware candidate validation and opt-in auto-adoption through
  `open-free-router discover --test --adopt`; secrets remain environment-only.
- Chinese provider backgrounds and per-model Chinese family descriptions,
  use-case guidance, speed tiers, and transparent benchmark caveats.

- Public `/models/` radar with provider availability, model limits, declared
  capabilities, feature-density comparison, filtering, and Codex aliases.
- Review-only continuous provider discovery from models.dev in both the local
  daemon and a six-hour Cloudflare Cron/KV pipeline.
- Public, credential-redacted model catalog export and `open-free-router
  discover` CLI command.

- Codex-compatible `POST /v1/responses` endpoint with buffered and SSE output.
- Function tool and function-result translation for complete Codex tool loops.
- Dedicated `open-free-router` Codex profile sync.
- Codex model catalog generation for third-party `/model` visibility and
  explicit capability metadata.
- Gemini thought-signature preservation across Responses API function-call
  turns, including streaming responses.
- Isolated Codex profile disables ChatGPT Apps and OpenAI Docs MCP startup so
  unrelated connector handshakes cannot interrupt third-party model sessions.
- Router-specific Codex profile omits global Skill metadata injection to avoid
  the fixed 2% skill-description budget warning and reduce prompt size.
- Disabled OpenAI Docs MCP profile entries retain their URL transport so Codex
  TUI `config/batchWrite` can validate and persist model selections.
- Codex catalog entries use stable `ofr-...` aliases containing only safe
  telemetry characters; the proxy maps aliases to original upstream IDs.
- The isolated Codex profile disables plugin and memory loading to prevent
  unrelated global-plugin warnings in third-party model sessions.
- Local inference bearer token and command-backed Codex authentication.
- `tool_calling` model capability metadata.
- Codex integration PRD and acceptance criteria.

### Fixed

- Runtime proxy indexes now rebuild on the active handler after model refresh.
- Distribution wheels now include `registry.default.yaml`.
- Refresh detects context, upstream ID, reasoning, output-limit, and tool-capability changes.
- Synced agent configurations no longer receive upstream provider API keys.
- Chunked SSE responses now use HTTP/1.1 for strict clients such as Codex.
- Registry files and timestamped backups use owner-only permissions when supported.
- Normal HTTP/1.1 client disconnects no longer emit misleading server
  tracebacks after a successful Codex response.
