# Changelog

## Unreleased

### Added

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
