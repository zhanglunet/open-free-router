# CLAUDE.md — how to work on this repo

`AGENTS.md` describes **what this project is** (entrypoints, architecture, code
conventions). This file is about **how to change it**. Every rule below was paid
for by a real defect in this repository; the references are so you can check the
reasoning rather than take it on faith.

## A test must be shown to fail without its fix

Adding a test alongside a fix proves nothing on its own. Revert the fix, run the
test, watch it fail, restore the fix. Only then does it pin anything.

This is not hypothetical here. A review pass added four fixes with tests, and
three of the four tests still passed with their fix reverted — the change was
real, the coverage was decorative. Two of them are now pinned by
`tests/test_cli_sync_flags.py`, which exists solely because deleting the
`--kimi-available-only` guard left the whole suite green.

When the fix is an *ordering* rather than a value, revert the ordering, not the
line. `sync_all` computes `explicit = agents is not None` **before** applying
`exclude`; moving that one statement after the filter is the natural way to
break it, and `test_sync_all_exclude_skips_agent_without_forcing_explicit_mode`
fails exactly then.

## A green suite is evidence, not proof

Ask what the run would have looked like if the code were wrong.

`311 passed` was true and meaningless: the suite only passed because the CI box
had no `~/.kimi/config.toml`. On a developer machine that had one, the same run
rewrote it — a test patched `KIMI_CONFIG` but not the legacy path the code had
just started falling back to. `tests/conftest.py` now repoints every
`Path.home()`-derived constant in `sync.py` at a per-test sandbox, so this class
of test can fail loudly instead of editing someone's real config.

Corollary: a test that only passes because of something absent from the machine
is not a passing test.

## Classify by structure, never by wording

An upstream's error *prose* is not an API. Status codes, headers and documented
fields are.

`classify_failure` used to read `"quota"`/`"credit"`/`"balance"` out of a 429
body and write a permanent terminal credential state. Those words appear in
ordinary rate-limit copy, so two healthy providers went offline until an
operator intervened. The same heuristic failed in the other direction too: Groq
reports a genuinely exhausted daily budget as a plain
`Rate limit reached ... tokens per day`, matching no marker, so a day-long
outage got a 60-second cooldown.

When a heuristic has to distinguish two cases, prefer evidence that accumulates
across requests over a keyword in one response. A 403 now locks out the model it
was returned for, and only convicts the credential once three *distinct* models
on the same slot have been refused.

## Scope a failure to whatever actually failed

401 is about the caller; 403 is about the resource. Grouping them meant one
model an account was not entitled to disabled every other model on that
provider.

Before widening a penalty from model to credential to provider, ask what the
response actually proves about each layer.

## Evidence expires

A probe result is a timestamped measurement, not a standing fact. Anything that
reads `probe-results.json` must age it out — `EVIDENCE_MAX_AGE_SECONDS` in
`probe.py`, deliberately equal to `STATUS_STALE_MS` in `worker/probe.js` so the
local and published views agree. A missing, unparseable or future-dated
timestamp counts as stale: none of them can demonstrate freshness.

Distinguish "never measured" from "measured too long ago" in whatever you report.
They call for different actions.

## Never leave a one-way door

Any state that blocks traffic needs a way back that does not require a human.
`terminal` credentials had no exit but an operator reset: `record_success`
skipped them, reload preserved them, and the state is keyed by credential *slot*,
so even rotating the API key left the slot dead. It now earns a bounded recovery
probe.

## Don't act on contradictory evidence

Two field reports disagreed about the same five models on the same day — 403 in
one, 429 or timeout in the other. Re-probing showed **all five were alive** and
neither report's status codes were right. Removing them from
`registry.default.yaml` would have shipped a contradiction to every new user.

When two sources conflict, the answer is to measure again, not to pick the more
convincing narrator. And a 429 is never grounds for deleting a model — that is
transient rate limiting, which is what the resilience layer is for.

## Prefer per-run judgement over a baked-in verdict

`refresh --probe-new` decides each model's fate from a real request at refresh
time. That is strictly better than freezing one sampling into the shipped
registry, and it is the shape to reach for whenever you are tempted to hardcode
"this one is broken".

## Working notes

- Full suite: `python3 -m pytest tests/ -q` (321 tests as of `b3ce8ea`).
- Tests must never depend on the developer's home directory; `tests/conftest.py`
  enforces this for `sync.py`'s paths.
- `registry.yaml` is gitignored because it holds API keys. A change described
  only there is invisible to everyone else — put shippable changes in
  `registry.default.yaml`.
- When docs and code disagree, one of them is a bug. `docs/PRD-*.md` are specs
  and go stale silently; check them when behaviour changes.
