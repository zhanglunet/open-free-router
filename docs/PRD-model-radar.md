# Free Model Radar and Continuous Discovery PRD

## Goal

Expose a public, credential-free comparison of the router's trusted providers
and models, while continuously discovering potential new free endpoints and
offering a strict, opt-in live-validation gate before automatic import.

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
5. Discovery does not mutate `registry.yaml` unless auto-adoption is explicitly
   requested and an individual model passes the validation gate.
6. Public export never includes API keys, bearer tokens, local paths, or
   authorization headers.
7. Candidate metadata retains declared auth names for documentation but testing
   reads only a dedicated `OFR_<PROVIDER>_API_KEY` opt-in variable. Generic
   high-privilege variables such as `GITHUB_TOKEN` are never read automatically.
8. Local validation rejects non-public or non-HTTPS endpoints, disables HTTP
   redirects and proxy inheritance, performs a minimal authenticated Chat
   Completions request, and records only status/latency evidence.
9. Auto-adoption is opt-in. It adds only individually successful models,
   references credentials by environment name, and leaves tool calling disabled
   until separately verified.
10. Every registered provider has a Chinese background profile, and every
    registered model has a generated Chinese description and use-case guide.

## Data flow

`models.dev → zero-price/protocol filter → credential lookup → public HTTPS policy → live smoke test → opt-in registry`

Trusted data follows a separate path:

`private registry → redacted exporter → site/data/catalog.json → /api/catalog`

## Non-goals

- Claiming model intelligence or code quality from declared metadata.
- Treating a zero price field as proof of unlimited free use.
- Uploading upstream provider credentials to Cloudflare.
- Automatically routing traffic to candidates that have not passed the live
  validation gate.
- Claiming an external benchmark score when no comparable independent result
  exists.
