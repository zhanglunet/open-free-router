# Testing Feedback — 2026-08-05

Real-world field testing report for open-free-router from a production deployment
(Hermes Agent → open-free-router 0.3.0 → 10 providers / 44 models over a 24h window).

This document records the bugs, observations and suggestions surfaced while probing
every live model through the local proxy and managing a real multi-agent fleet.
It is **not** an exhaustive audit — it is the findings that materially affected
availability and debuggability.

---

## P1 — Transient 429s permanently disable live providers (false-positive `terminal`)

**Severity:** High. Causes real outages and forces manual intervention.

### Symptom

Two providers whose upstreams were **fully alive** (`google-ai-studio`, `sensenova`)
were marked dead at the router and every request returned:

```
HTTP 503: All routes for model 'gai/gemini-3.5-flash' are temporarily unavailable.
```

Direct probing of the same upstream with the same key succeeded:

```text
GET https://generativelanguage.googleapis.com/v1beta/openai/...  → 200 OK (gemini-2.5/3.5/3.1-flash)
POST https://token.sensenova.cn/v1/chat/completions             → 200 OK (deepseek-v4-flash / glm-5.2)
```

The router's persisted resilience state (`~/.local/share/open-free-router/runtime-state.json`)
had both credentials stuck at:

```json
{"provider":"google-ai-studio","state":"terminal","reason":"quota_exhausted","slot":0}
{"provider":"sensenova",        "state":"terminal","reason":"quota_exhausted","slot":0}
```

Both were cleared only after a manual `POST /api/resilience/reset {"provider":...}`,
after which all 7 models recovered immediately.

### Root cause

`src/open_free_router/resilience.py::classify_failure`:

```python
if status == 429:
    quota = any(marker in lowered for marker in ("quota", "credit", "balance"))
    return FailureDecision(
        "quota_exhausted" if quota else "rate_limited",
        retryable=not quota,
        credential_cooldown=not quota,
        credential_terminal=quota,      # <-- quota becomes a PERMANENT terminal stop
    )
```

Any upstream 429 whose error body **mentions** "quota" / "credit" / "balance" is
classified as `quota_exhausted` with `credential_terminal=True`. In `record_failure`
this writes a `terminal` credential state with **no expiry and no automatic
recovery**:

```python
if decision.credential_terminal:
    self._credentials[key] = CredentialState(state="terminal", ...)   # permanent
```

A momentary rate-limit response that happens to include the word "quota" (common in
rate-limit wording) therefore **permanently** disables an otherwise-healthy provider
until an operator manually resets it.

### Why this is wrong

- A 429 is by definition *retryable*. The distinction between `quota_exhausted` and
  `rate_limited` should hinge on a **confirmed, dated reset**, not on vocabulary in
  the transient error body.
- The code already supports bounded recovery via `credential_cooldown` +
  `cooldown_until` and even honors an upstream `Retry-After` for a declared reset
  (executor.py). A `terminal` state bypasses all of that.
- Real consequence: a single transient blip silently knocks every model on that
  provider offline; only a human who knows to call `/api/resilience/reset` recovers it.

### Proposed fix (suggested)

Treat an unconfirmed 429 as **bounded cooldown**, and reserve `terminal` for states
that are verifiably permanent (e.g. `credential_invalid`, `credits_exhausted`,
`model_unavailable`, or a confirmed non-retryable reset):

```python
if status == 429:
    quota = any(marker in lowered for marker in ("quota", "credit", "balance"))
    return FailureDecision(
        "quota_exhausted" if quota else "rate_limited",
        retryable=True,                 # 429 is always retryable
        credential_cooldown=True,       # bounded, not terminal
        credential_terminal=False,
        # keep a longer cooldown (or honor Retry-After) for quota-flavoured 429s
    )
```

At minimum: never persist `quota_exhausted` as `terminal` without an expiry, and let a
periodic probe (or a half-open probe on the next request) re-open a terminal slot after
a sane backoff.

**Reproduction:** hit a provider until its upstream returns any 429 whose body includes
"quota"/"credit"/"balance"; the provider stays 503 thereafter until `reset`.

---

## P2 — `refresh` registers models that are not actually reachable

**Severity:** Medium. Pollutes auto-refresh registries with dead routes.

### Symptom

`open-free-router refresh` reported providers "updated" and added models that were
**not reachable** at the moment of refresh:

```text
✓ groq updated: 6 models          → groq/compound (4096-CTX ok) but groq/compound-mini → 429
✓ nvidia-nim updated: 4 models    → z-ai/glm-5.2 → timeout (>60s), no route credit
```

After restart, live probing of the newly-added IDs:

```text
gq/groq/compound-mini      HTTP 429  (rate limited)
nv/z-ai/glm-5.2            ERR  timed out (>55s)
```

### Root cause

The per-provider `refresh_sources/*.py` `fetch()` implementations only call
`GET /models` (the metadata list) and never make a **functional** 1-token request to
validate each model before it is adopted into the registry. `/models` listing a model
does not mean `/chat/completions` accepts it at that moment.

### Proposed fix (suggested)

When `refresh` adds a **new** model (one not previously in the registry), validate it
with the same 1-token probe used by `probe.py` before persisting; skip (or mark
`unverified`) any that fail. Only pre-existing models should be allowed to remain
without a fresh probe on refresh, to keep the diff non-destructive.

---

## P3 — `openrouter` provider ships with empty key and silently 503s

**Severity:** Low (observability), but user-confusing.

### Symptom

The default/template registry includes an `openrouter` provider with `api_key: ''`.
Every one of its 9 `or/*:free` models immediately returns `503: All routes for model
... are temporarily unavailable` — the same 503 used for genuinely down providers, so
from the client's perspective an *unconfigured* provider is indistinguishable from a
*broken* one.

### Proposed fix (suggested)

- Emit a distinct error / status for `credential_missing` (the executor already appends
  `{"reason":"credential_missing"}` to the rejected list) — surface that reason in the
  HTTP body alongside the 503, and surface it in the UI provider panel.
- Consider marking providers with an empty `api_key` as `unconfigured` in `status`
  / `/api/catalog` rather than mixing them into the same "unavailable" bucket.

---

## Minor / UX notes

1. **`refresh --verbose` does not exist.** Help text lists no `--verbose`; users wanting
   to see *which* models changed have no flag. Suggest `refresh --diff` or `--dry-run`
   already shows per-provider counts; add a per-model diff for clarity.
2. **Registry backup is good hygiene but undocumented.** `serve`/`refresh` write
   `.bak-YYYYMMDD-HHMMSS` files atomically — that is great. Consider documenting this
   in README so operators know recovery is one `cp` away.
3. **`/api/resilience/reset` is a critical operator tool but not in the docs.** Without
   the P1 fix, this endpoint is the only way to un-stick a provider. It deserves a line
   in the README and in `open-free-router status`/`doctor` hint output when a provider
   is stuck in `terminal`.

---

## What worked well (no action needed)

- **Single-port proxy (8337) with model-ID routing** across all 4 ID formats worked
  exactly as documented; no head-of-line blocking seen under concurrent agent traffic.
- **`sync` / Pi `models.json` writer** produced correctly-shaped client configs; the
  local-proxy dedup guard prevented accumulation.
- **Directory discovery (`discover`)** correctly stayed review-only and never mutated
  the registry; 44 candidate providers surfaced cleanly.
- **Atomic `.bak` snapshots** and the VCS-tracked `docs/provider-status.json` made
  forensics easy.

---

*Prepared from a live probe sweep of 44 registered models through the 0.3.0 proxy,
plus direct upstream verification of affected providers.*
