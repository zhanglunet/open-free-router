# Free Model Radar and Continuous Discovery PRD

## Goal

Expose a public, credential-free comparison of the router's trusted providers
and models, while continuously discovering potential new free endpoints without
automatically trusting or importing them.

## Trust states

- `available`: the latest safe authenticated smoke test succeeded.
- `unavailable`: the latest verification confirmed a current external blocker.
- `unverified`: a registered provider has no recent smoke-test evidence.
- `candidate`: a public catalog lists an HTTPS endpoint and at least one model
  with explicit zero input/output unit price. This is evidence for review only;
  it does not prove unconditional free access.

## Requirements

1. `/models/` lists every trusted provider and model with model/upstream/Codex
   IDs, context, output limit, reasoning, tools, status, observed latency, and a
   transparent declared-feature score.
2. The browser can search, filter, and sort the complete list locally.
3. Cloudflare Cron refreshes candidate data every six hours and stores one
   consolidated snapshot in Workers KV.
4. The local daemon runs an independent discovery cycle every 24 hours by
   default and writes an owner-only review snapshot outside the repository.
5. Discovery never mutates `registry.yaml`. Promotion requires manual endpoint,
   terms, free-tier, model-ID, auth, and tool-call verification.
6. Public export never includes API keys, bearer tokens, local paths, or
   authorization headers.

## Data flow

`models.dev → HTTPS/zero-price filter → candidate snapshot → manual review → registry`

Trusted data follows a separate path:

`private registry → redacted exporter → site/data/catalog.json → /api/catalog`

## Non-goals

- Claiming model intelligence or code quality from declared metadata.
- Treating a zero price field as proof of unlimited free use.
- Uploading upstream provider credentials to Cloudflare.
- Automatically routing traffic to newly discovered endpoints.
