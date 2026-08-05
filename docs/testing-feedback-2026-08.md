# Testing Feedback — 2026-08-05

Real-world field testing report for open-free-router from a production deployment
(Hermes Agent → open-free-router 0.3.0 → 10 providers / 44 models over a 24h window).

This document records the bugs, observations and suggestions surfaced while probing
every live model through the local proxy and managing a real multi-agent fleet.
It is **not** an exhaustive audit — it is the findings that materially affected
availability and debuggability.

**Baseline:** the 44 models above are this deployment's local `registry.yaml` *after*
several `refresh` runs, not the shipped template — `registry.default.yaml` declares 10
providers / 38 models. Model counts in this report should be read against that local
registry, not the repo default.

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

### `terminal` has no recovery path at all

The 429 misclassification is the trigger, but the deeper gap is that **nothing ever
re-opens a `terminal` credential except an explicit operator reset**:

- The only exit is `ResilienceManager.reset()` (`resilience.py:508`), reachable solely
  via `resilience reset` / `POST /api/resilience/reset`.
- `record_success` deliberately skips terminal slots
  (`resilience.py:420`: `if credential.state != "terminal"`), so a recovered upstream
  cannot heal the state even when traffic would succeed.
- On reload, terminal is restored as active (`resilience.py:256`), so a restart does
  not clear it either.
- State is keyed by `(provider, credential_slot)`, not by the key's value — so
  **rotating in a fresh API key does not unlock the slot**. Even a *correctly*
  classified 401 leaves the operator stuck at 503 after they fix the credential.

That last point holds regardless of how 429s are classified, and is arguably the more
important defect: `terminal` is a one-way door with no probe, no expiry, and no
invalidation on credential change.

### Why the 429 classification is worth revisiting

- A 429 is by definition *retryable*. The distinction between `quota_exhausted` and
  `rate_limited` should hinge on a **confirmed, dated reset**, not on vocabulary in
  the transient error body.
- The code already supports bounded recovery via `credential_cooldown` +
  `cooldown_until` and even honors an upstream `Retry-After` for a declared reset
  (executor.py). A `terminal` state bypasses all of that.
- Real consequence: a single transient blip silently knocks every model on that
  provider offline; only a human who knows to call `/api/resilience/reset` recovers it.

**This is a deliberate design decision, not an oversight.** `tests/test_resilience.py:21-25`
(`test_rate_limit_and_quota_have_different_recovery`) explicitly asserts the current
split — `429` + quota wording ⇒ `credential_terminal=True, retryable=False`. Changing it
means consciously reversing that decision and updating both that test and the semantics
of `test_terminal_credential_is_not_downgraded_by_late_rate_limit`. This report argues
the tradeoff is wrong in the field, but a maintainer should weigh it as a design change.

### Proposed fix (direction, not a drop-in patch)

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

Note this sketch is **not** directly applicable: the trailing comment asks for a longer
quota-flavoured cooldown, but `record_failure` (`resilience.py:374`) draws the delay only
from `retry_after` or the single global `credential_cooldown`, and `FailureDecision` has
no field to carry a per-kind duration. Implementing it means adding that field first.

At minimum: never persist `quota_exhausted` as `terminal` without an expiry, and let a
periodic probe (or a half-open probe on the next request) re-open a terminal slot after
a sane backoff. Independently of the 429 question, terminal slots should also be
invalidated when the underlying credential changes.

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

## P3 — `credential_missing` never reaches the client; unconfigured looks identical to broken

**Severity:** Low (observability), but user-confusing.

### Symptom

An unconfigured provider is indistinguishable from a genuinely down one. In this
deployment `openrouter` had no key, and every one of its 9 `or/*:free` models returned
`503: All routes for model ... are temporarily unavailable` — byte-for-byte the same 503
emitted when an upstream is actually failing.

**Correction:** an earlier draft framed this as "`openrouter` ships with an empty key,"
implying that provider is special. It is not — *all 10* providers in
`registry.default.yaml` carry `api_key: ''`, by design; keys are filled in by
`open-free-router setup` or via `api_key_env`. The finding here is purely about
observability: the router knows the difference and does not tell anyone.

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
2. **Registry backup is good hygiene but undocumented for users.** `serve`/`refresh`
   write `.bak-YYYYMMDD-HHMMSS` files atomically — that is great. It is described in
   `AGENTS.md`, but not in either README, so operators have no user-facing pointer that
   recovery is one `cp` away.
3. **`/api/resilience/reset` is discoverable in docs but not at the moment of failure.**
   (An earlier draft claimed it was undocumented — that was wrong: it is covered in
   README.md:120/260/326 and README.en.md:113/224, as both a CLI command and an HTTP
   endpoint.) The real gap is situational: when a provider is stuck in `terminal`,
   nothing points the operator at it. `status` / `doctor` should surface stuck slots and
   name the reset command in their hint output.

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
