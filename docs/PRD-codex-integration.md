# open-free-router Codex Integration PRD

Status: P0 implemented and verified
Owner: open-free-router
Last updated: 2026-08-01

## 1. Background

open-free-router currently exposes an OpenAI-compatible Chat Completions
proxy and synchronizes its registry to Pi, OMP, OpenCode, and Hermes. Current
Codex custom model providers require the Responses API wire protocol. Pointing
Codex at the existing proxy therefore results in a request to `/v1/responses`,
which the proxy does not implement.

The router also stores upstream provider credentials. Those credentials must
remain inside the router process and must not be copied into every downstream
agent configuration.

## 2. Goal

Allow a local Codex CLI/app session to use a model in the open-free-router
registry through a protected Responses API endpoint, while preserving the
existing Chat Completions integrations.

## 3. P0 Scope

1. Add `POST /v1/responses` with buffered and SSE streaming responses.
2. Translate Responses input messages, function tools, function calls, and
   function-call outputs to and from Chat Completions.
3. Require a local bearer token for all inference POST endpoints when the
   daemon starts normally.
4. Keep provider API keys inside `registry.yaml`; synced agent configurations
   receive only the local proxy token.
5. Add a dedicated Codex profile at
   `~/.codex/open-free-router.config.toml` through
   `open-free-router sync --agent codex`.
6. Generate `~/.codex/open-free-router.models.json` and reference it through
   `model_catalog_json`, so registry models appear in `/model` with explicit
   capability metadata instead of fallback metadata.
7. Add `open-free-router token` as the command-backed Codex authentication
   helper. The token remains in a mode-0600 local file.
8. Fix runtime model-index rebuilding after refresh.
9. Include `registry.default.yaml` in built distributions.
10. Compare complete model metadata during refresh, not only model IDs.
11. Preserve Gemini OpenAI-compatibility thought signatures across stateless
    Responses function-call turns, including streaming responses.
12. Keep the isolated third-party profile lean by disabling ChatGPT Apps,
    OpenAI Docs MCP, and global Skill metadata injection without changing the
    user's normal Codex profile.

## 4. Non-goals

- Emulating OpenAI-hosted tools such as web search, image generation, file
  search, or code interpreter inside an upstream provider.
- Guaranteeing that a provider remains free after its published quota or price
  changes.
- Persisting Responses API server-side conversations. Codex sends the required
  history and uses `store=false` for this integration.
- Automatically replacing the user's default `~/.codex/config.toml`.

## 5. User Experience

```bash
open-free-router setup
open-free-router serve
open-free-router sync --agent codex --codex-model gq/gpt-oss-120b
codex --profile open-free-router
```

`serve` prints the proxy token path, never the token value. Codex retrieves the
token at request time by running `open-free-router token`.

The generated catalog exposes conservative `ofr-...` aliases to Codex so model
names are valid telemetry tags. The proxy maps those aliases back to upstream
IDs and continues accepting every existing model-ID form.

## 6. Functional Requirements

### FR-1 Responses request conversion

- Accept a string or a list of Responses input items.
- Convert `message` items and `instructions` into Chat Completions messages.
- Convert Responses `function` tools into Chat Completions function tools.
- Convert `function_call` and `function_call_output` history items so Codex can
  complete multi-turn tool loops.
- Retain provider-specific Gemini thought signatures in a bounded in-memory
  cache keyed by function-call ID and replay them exactly on the next tool turn.
- Ignore reasoning history and unsupported hosted tools without exposing hidden
  reasoning text.
- Reject unsupported image/file input with an explicit 400 response.

### FR-2 Responses output conversion

- Return Responses-compatible buffered objects.
- Stream ordered SSE events for text deltas and function-call arguments.
- Finish every successful stream with `response.completed`.
- Preserve upstream HTTP status and `Retry-After` before streaming starts.

### FR-3 Authentication

- Generate `proxy.token` with at least 256 bits of entropy and mode 0600.
- Require `Authorization: Bearer <proxy token>` on inference POST endpoints.
- Keep read-only health and model discovery endpoints available on loopback.
- Never write an upstream API key to Codex, OMP, OpenCode, or Hermes configs.

### FR-4 Codex profile

- Write only the dedicated `open-free-router.config.toml` profile.
- Configure `wire_api = "responses"` and command-backed authentication.
- Use an explicitly requested model or a registry model marked
  `tool_calling: true`.
- Use stable, safe Codex aliases to avoid ambiguous routing and invalid
  telemetry tags.
- Generate a startup model catalog and disable unrelated Apps, plugins,
  memories, Docs MCP, and Skill metadata injection only inside the dedicated
  profile. A disabled MCP entry must still include a valid URL transport so
  standalone TUI configuration writes pass validation.

## 7. Security Requirements

- The proxy and UI continue to bind to `127.0.0.1` by default.
- Token comparison is constant time.
- Registry, proxy token, and their backups use owner-only permissions where the
  platform supports POSIX modes.
- Unsupported inbound authorization values never fall through to upstream.
- Upstream error bodies are returned without provider credentials or request
  headers.

## 8. Acceptance Criteria

1. Existing tests pass.
2. New tests cover authentication success/failure, buffered Responses text,
   streaming text, streaming function calls, Gemini signature replay,
   tool-result history, Codex profile generation, local-token sync, index
   refresh, and package data.
3. A real installed Codex CLI accepts the generated profile without a
   `wire_api` configuration error.
4. A captured Codex request completes one Responses SSE turn through a mocked
   Chat Completions upstream.
5. A built wheel contains `open_free_router/registry.default.yaml`.

### Verification record (2026-08-01)

- 89 automated tests passed.
- A real Codex CLI 0.146.0 completed streamed text turns through the adapter.
- A real Codex CLI executed `exec_command`, returned its result through a
  second Responses request, and completed the tool loop.
- Gemini 3.6 Flash completed a real two-request Codex tool loop without the
  previous missing `thought_signature` 400 response.
- The dedicated TUI started without Apps/Docs MCP interruption or Skill budget
  warnings.
- Strict Codex config parsing accepted the generated provider shape.
- The built wheel contains `registry.default.yaml` and `responses.py`.

## 9. P1 Follow-ups

- Provider capability probing and per-model tool-call conformance tests.
- Quota, rate, and cost circuit breakers.
- Safe HTML rendering in the dashboard.
- Native adapters for non-OpenAI-compatible providers.
- JSONC-safe OpenCode configuration editing and versioned backups.
