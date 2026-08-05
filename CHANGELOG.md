# Changelog

## Unreleased

### Testing feedback (field report)

- Added `docs/testing-feedback-2026-08.md` — a field testing report from a live
  10-provider / 44-model deployment. Highlights a P1 false-positive: a transient
  upstream 429 whose body mentions "quota"/"credit"/"balance" was classified as
  `quota_exhausted` with `credential_terminal=True` and **permanently** disabled an
  otherwise-healthy provider (both `google-ai-studio` and `sensenova` were stuck
  dead until a manual `/api/resilience/reset`, while their upstreams were alive).
  More broadly, `terminal` had no exit but an operator reset — `record_success`
  skipped it, reload preserved it, and it is keyed by credential *slot*, so
  rotating in a new key did not clear it either. Also documents P2 (`refresh`
  adopts models that `/models` lists but that are not reachable, e.g.
  `groq/compound-mini`, `z-ai/glm-5.2`) and P3 (`credential_missing` is computed
  but never surfaced, so an unconfigured provider 503s indistinguishably from a
  genuinely down one). The fixes ship in the same cycle — see below.

### Resilience recovery

- A 429 no longer disables a credential permanently. Quota-flavoured wording
  ("quota"/"credit"/"balance") still earns an eight-times-longer cooldown, but
  never a terminal stop — that wording is ordinary rate-limit copy, and treating
  it as proof of exhaustion took two healthy providers offline in field testing
  until an operator ran `/api/resilience/reset` by hand.
- `terminal` is now a long stop rather than a one-way door. After a backoff
  window one request is let through to re-test the condition; a success clears
  the state and a failure doubles the window up to 8×. A rotated API key
  therefore recovers on its own, without the manager storing anything derived
  from the credential.
- Runtime state written before this change reloads with a bounded window instead
  of inheriting a permanent stop.
- A 503 now says why nothing was attempted. When every candidate was skipped for
  want of an API key the router returns a `configuration_error` naming
  `open-free-router setup`, instead of the same opaque "temporarily unavailable"
  used for genuinely failing upstreams; mixed cases list the distinct reasons.
- `refresh --probe-new` validates models that are not yet in the registry with a
  real 1-token request before adopting them, so `GET /models` listing a model no
  longer implies `/chat/completions` accepts it. Pre-existing models are never
  re-probed, a probe that could not reach the upstream adopts rather than
  rejects, and a provider is never emptied by probing. It spends free-tier quota
  on every run, so it is opt-in and the `serve` scheduler never enables it; a
  skipped model is simply not adopted this round and is offered again next
  refresh.
- A reset declared in the response headers now sets the cooldown for any
  rate-limited response, not only the ones whose body happened to say "quota".
  Groq reports an exhausted daily token budget as a plain "Rate limit reached
  ... tokens per day", which matched no marker and fell back to the 60-second
  default, so the router retried against a day-long budget all day. Headers are
  structured evidence in a way error prose is not; a declared reset is capped at
  24 hours so one bad header cannot park a credential.
- A 403 is now scoped to the model, not the credential. Providers return it for a
  model the account is not entitled to — NVIDIA NIM gates several that way — and
  classifying it as `credential_invalid` took every other model on that provider
  down with the one that was refused. The refused model is locked out; the
  credential is only convicted once three distinct models on the same slot have
  been refused, which is evidence rather than error-body vocabulary. A success
  clears that accumulated evidence.
- README documents the `.bak-YYYYMMDD-HHMMSS` snapshots and their 10-file
  retention, which previously appeared only in `AGENTS.md`.
- Reported in `docs/testing-feedback-2026-08.md` (P1, P2, P3, minor note 2).

### Data freshness (M2)

- Provider availability on `/api/catalog` is now derived from the model evidence
  that survived the 45-minute recency window instead of a KV summary frozen at
  probe time. A stale snapshot used to render "可用 · 已验证 3/3 个模型可用 ·
  900ms" above three "候选未验证" model badges from 46 minutes onward, with the
  3-day and 30-day payloads byte-identical.
- A KV miss no longer advertises `status_source: cloudflare-server-probe` or a
  note claiming a live measurement, and a provider whose evidence expired no
  longer reports "首次探测".
- `_speed_tier` returns 未测 rather than the affirmative 当前不可用 for a model
  that was never probed; the Worker copies this field onto the live catalog
  without recomputing it.
- The status board, model radar and benchmarks page degrade explicitly when
  `/api/catalog` is unreachable, instead of rendering the published snapshot as
  current fact. The board no longer attributes the catalog export time to an
  element labelled 最近检查.
- `npm run build` fails on a published catalog that is stale, a schema
  generation behind, internally inconsistent or empty, anchored to the commit
  date so `git bisect` and old-tag rebuilds are unaffected. CI additionally
  re-exports the catalog and diffs it, so the gate cannot be cleared by editing
  a timestamp.
- `scripts/refresh-provider-status.mjs` refreshes availability evidence from the
  deployed Worker's own unauthenticated `/api/status` with no credentials,
  validating the snapshot first and projecting it monotone-safely: `available`
  passes through, everything else becomes `unverified`, so a rate-limited probe
  never publishes an outage.
- New scheduled workflows: `data-refresh.yml` opens a pull request with a
  refreshed snapshot, and `deployed-freshness.yml` checks what the live site
  actually serves — the only check that does, since deploys are manual.
- `site/_headers` gives `/data/*.json` an explicit short TTL; it is not
  fingerprinted, so a visitor could previously be served a catalog older than
  the gate's own threshold from a fresh deploy.
- The site audit gains a degraded pass that serves the build with `/api/*`
  failing and requires each fallback page to say so.

- Published the public `open-free-router@0.3.0` npm bootstrap package with
  `open-free-router` and `ofr` commands; verified a clean registry install,
  isolated Python environment creation and CLI startup.

## 0.3.0 - 2026-08-02

### Added

- Executable 11-provider × 3-client-protocol compatibility matrix covering
  buffered/streaming text, reasoning-only output, tool calls, usage, finish
  semantics, Retry-After, safe fallback and cross-envelope field isolation.
- New `protocols [--json]` command and Doctor report section distinguish
  declared adapter readiness from live provider availability.
- The bundled Google AI Studio base URL now uses its OpenAI-compatible
  `/v1beta/openai` path; Doctor gives an exact repair for older registries.
- Responses and Messages streaming now preserve reasoning-only model output;
  cross-protocol errors retain only standard allowlisted fields.
- Four read-only MCP diagnostics: offline route explanation, resilience state,
  quota claims/runtime availability and privacy-minimized usage metrics.
- MCP write tools are now hidden by default and require the explicit
  `mcp.allow_write_tools: true` opt-in; provider endpoints and runtime errors are
  sanitized before entering tool results.
- P1 privacy-minimized SQLite analytics with owner-only files, configurable
  30-day retention (`0` disables storage), safe upserts and inference-isolated
  database failures.
- Buffered and streaming Chat/Responses/Messages usage now feeds provider/model
  success rate, p50/p95 latency, fallback recovery, circuit-block, Token coverage
  and explicitly estimated free-quota metrics.
- Authenticated metrics/JSON/CSV APIs, a `metrics` CLI and Chinese dashboard
  expose allowlisted aggregates; exports reject secret-like values and CSV
  formulas, and exclude raw request IDs, prompts, responses and tool arguments.
- Optional P1 explainable scoring for virtual routes with normalized health,
  recent success, p95 latency, quota, capability and free-evidence factors.
- `route explain --json`, redacted route history and the Chinese dashboard now
  show each candidate's total, factor value, normalized weight, contribution
  and source; missing metrics use a configured explicit default.
- Successful and failed routes record bounded first-byte/total latency metadata
  in memory. Disabling scoring restores the exact deterministic priority order.
- P1 quota awareness normalizes common `RateLimit-*`, `X-RateLimit-*` and
  `Retry-After` response headers into redacted per-credential request/token
  limits, remaining values and reset timestamps.
- Recurring quota exhaustion with a declared reset uses a bounded credential
  cooldown; unknown/credit exhaustion stays terminal, while multi-key routing
  prefers attemptable slots, earlier resets and recent successes.
- Quota state survives restart, is visible through the resilience CLI/API and
  Chinese dashboard, and never persists raw upstream headers or credentials.
- P1 free-tier evidence model at provider/model scope with typed quota claims,
  public HTTPS sources, verification/expiry timestamps, region/payment warnings,
  strict validation and backward-compatible registry serialization.
- `auto/free` now accepts only candidates with currently verified evidence;
  expired, invalid, incomplete and unknown claims remain visible with explicit
  review states but cannot be advertised or routed as verified-free.
- Free-tier evidence flows through `models --json`, `doctor --json`, the local
  Chinese dashboard and public catalog schema v2 with evidence status filters.
- Public export blocks credential fields, credential-bearing evidence URLs and
  common Bearer/API-key value patterns before writing catalog data.

- P0 routing foundation: deterministic `auto`, `auto/coding`, `auto/fast`, and
  `auto/free` virtual models, custom alias configuration, bounded route plans,
  `/v1/models` exposure, and `route explain` CLI output.
- Provider/credential/model resilience primitives with scoped failure
  classification, bounded Retry-After parsing, circuit half-open gating,
  credential cooldown/terminal states, model lockout, and redacted snapshots.
- Shared pre-first-byte fallback execution for Chat Completions, Responses and
  Anthropic Messages, including multi-key rotation, no replay after stream
  start, explicit-model isolation, bounded attempts and `X-OFR-*` decision
  headers.
- Token-protected resilience status/reset API and matching `resilience` CLI.
- Owner-only atomic `runtime-state.json` persistence with expired-state pruning,
  corrupt-file quarantine, and inference-safe I/O failure handling.
- Bounded redacted request-ID route-decision history, authenticated lookup API,
  and a Chinese dashboard panel for routes, fallback and three-layer isolation.
- Restart recovery, concurrent-write, credential-redaction, history-bound and
  dashboard proxy-auth regression coverage.
- Actionable routing diagnostics in `doctor [--json]`, including exact YAML
  paths, stable issue codes, invalid aliases/candidates/types and repair hints.
- Concurrency hardening prevents duplicate in-flight failures from extending
  active provider, credential or model penalties; only one half-open probe wins.
- Virtual routes now skip credential-less providers before network I/O, and
  regression tests verify provider-specific authorization and sub-5ms p95 plans.

## 0.2.0 - 2026-08-02

### Added

- OmniRoute comparative architecture review and an executable Chinese PRD for
  deterministic auto routing, safe fallback, provider/credential/model
  resilience isolation, route explanation, quota awareness, observability,
  security boundaries, and phased acceptance criteria.

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
