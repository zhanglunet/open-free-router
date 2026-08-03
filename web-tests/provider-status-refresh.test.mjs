import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { MAX_SNAPSHOT_AGE_MS, projectProbeSnapshot } from "../scripts/refresh-provider-status.mjs";

const NOW = Date.parse("2026-08-03T06:00:00Z");
const IDS = ["groq", "google-ai-studio"];

function snapshot(overrides = {}) {
  const asOf = new Date(NOW - 5 * 60 * 1000).toISOString();
  return {
    schema_version: 2,
    source: "cloudflare-server-probe",
    as_of: asOf,
    providers: {
      groq: { availability: "available", reason: "已验证 5/6 个模型可用", latency_ms: 43, checked_at: asOf },
      "google-ai-studio": { availability: "unavailable", reason: "提供商对服务器探测请求限流", latency_ms: 302, checked_at: asOf },
    },
    ...overrides,
  };
}

const project = (snap) => projectProbeSnapshot(snap, { catalogProviderIds: IDS, nowMs: NOW });

test("available passes through with its measurement", () => {
  const { ok, status } = project(snapshot());
  assert.equal(ok, true);
  assert.deepEqual(status.providers.groq, {
    availability: "available",
    latency_ms: 43,
    reason: "已验证 5/6 个模型可用",
  });
});

test("a rate-limited provider is never published as down", () => {
  // /api/status genuinely reports google-ai-studio and openrouter as
  // unavailable when they rate-limit our own probe. Baking that into the static
  // fallback would publish a one-sample outage claim that nothing revisits for
  // a day. The static file may withhold good news; it must not invent bad news.
  const { status } = project(snapshot());
  assert.equal(status.providers["google-ai-studio"].availability, "unverified");
  assert.equal(status.providers["google-ai-studio"].latency_ms, null);
  // The observed reason is kept so a human can see why it was downgraded.
  assert.match(status.providers["google-ai-studio"].reason, /限流/);
});

test("a snapshot older than the bound is rejected rather than published", () => {
  const stale = new Date(NOW - MAX_SNAPSHOT_AGE_MS - 60_000).toISOString();
  const { ok, errors } = project(snapshot({ as_of: stale }));
  assert.equal(ok, false);
  assert.match(errors.join("\n"), /as_of/);
});

test("a snapshot from the future is rejected", () => {
  const { ok, errors } = project(snapshot({ as_of: new Date(NOW + 3600_000).toISOString() }));
  assert.equal(ok, false);
  assert.match(errors.join("\n"), /未来/);
});

test("a degraded snapshot missing catalog providers is rejected", () => {
  const partial = snapshot();
  delete partial.providers["google-ai-studio"];
  const { ok, errors } = project(partial);
  assert.equal(ok, false);
  assert.match(errors.join("\n"), /google-ai-studio/);
});

test("an empty or wrong-schema snapshot is rejected", () => {
  assert.equal(project(snapshot({ schema_version: 1 })).ok, false);
  assert.equal(project(snapshot({ providers: {} })).ok, false);
  assert.equal(project(null).ok, false);
});

test("the refresh workflow opens a pull request and never pushes to the branch under test", async () => {
  // A direct push with the default GITHUB_TOKEN does not trigger ci.yml or
  // site.yml, so a bot commit would be the one change nothing validates.
  const workflow = await readFile(new URL("../.github/workflows/data-refresh.yml", import.meta.url), "utf8");
  assert.ok(!/git push origin (main|HEAD)/.test(workflow), "must not push to main");
  assert.match(workflow, /gh pr create/);
  // workflow_dispatch is the documented exception that does create runs when
  // triggered with GITHUB_TOKEN, so CI actually sees the bot's branch.
  assert.match(workflow, /gh workflow run/);
});

/* Per-model evidence. public_catalog.py stamps the provider's verdict onto
   every one of its models, so the published catalog's per-model availability
   carries zero per-model information — 7 models are published available with
   no evidence of their own behind them. /api/status has carried the real thing
   all along, keyed "provider/model". */
const MODEL_KEYS = ["groq/a", "groq/b", "google-ai-studio/c"];

function snapshotWithModels(overrides = {}) {
  const asOf = new Date(NOW - 5 * 60 * 1000).toISOString();
  return {
    ...snapshot(),
    models: {
      "groq/a": { availability: "available", status: "http_200", reason: "实测成功", latency_ms: 42, checked_at: asOf },
      "groq/b": { availability: "unavailable", status: "http_429", reason: "提供商对服务器探测请求限流", latency_ms: 88, checked_at: asOf, upstream_error: "rate limited" },
      "google-ai-studio/c": { availability: "unavailable", status: "http_404", reason: "模型或接口在提供商侧不存在", latency_ms: 180, checked_at: asOf },
      ...overrides,
    },
  };
}

const projectModels = (snap) =>
  projectProbeSnapshot(snap, { catalogProviderIds: IDS, catalogModelKeys: MODEL_KEYS, nowMs: NOW });

test("per-model evidence is projected monotone-safely, like the provider level", () => {
  const { ok, status } = projectModels(snapshotWithModels());
  assert.equal(ok, true);
  assert.deepEqual(status.models["groq/a"], {
    availability: "available",
    latency_ms: 42,
    checked_at: new Date(NOW - 5 * 60 * 1000).toISOString(),
    status: "http_200",
    reason: "实测成功",
  });
  // A 429 is our own probe being throttled. It must not publish an outage into
  // a file nothing revisits for a day.
  assert.equal(status.models["groq/b"].availability, "unverified");
  assert.equal(status.models["groq/b"].latency_ms, null);
  assert.match(status.models["groq/b"].reason, /限流/);
  assert.equal(status.models["google-ai-studio/c"].availability, "unverified");
});

test("upstream_error never reaches the published status file", () => {
  // write_public_catalog rejects text containing "messages" or "prompt", and an
  // echoed request body would fail the build. It stays in the Worker/KV only.
  const { status } = projectModels(snapshotWithModels());
  assert.equal(JSON.stringify(status).includes("upstream_error"), false);
  assert.equal(JSON.stringify(status).includes("rate limited"), false);
});

test("a model the rotating batch did not cover is reported, not invented", () => {
  const partial = snapshotWithModels();
  delete partial.models["groq/b"];
  const { ok, status } = projectModels(partial);
  assert.equal(ok, true, "a partial batch is normal, not a rejection");
  assert.equal(status.models["groq/b"].availability, "unverified");
  assert.equal(status.model_evidence.total, MODEL_KEYS.length);
  assert.equal(status.model_evidence.covered, 2);
});

test("evidence older than the worker's own recency window is not carried forward", () => {
  const stale = snapshotWithModels();
  stale.models["groq/a"].checked_at = new Date(NOW - 46 * 60 * 1000).toISOString();
  const { status } = projectModels(stale);
  assert.equal(status.models["groq/a"].availability, "unverified");
  assert.match(status.models["groq/a"].reason, /过期/);
  // covered counts models with FRESH evidence, whatever that evidence says —
  // it measures how much of the catalog the rotating batch reached, not how
  // much of it passed. b and c were probed 5 minutes ago and still count.
  assert.equal(status.model_evidence.covered, 2);
});

test("only catalog models are published, so a removed model leaves no orphan", () => {
  const extra = snapshotWithModels();
  extra.models["groq/deleted-from-registry"] = { availability: "available", latency_ms: 1, checked_at: new Date(NOW).toISOString() };
  const { status } = projectModels(extra);
  assert.deepEqual(Object.keys(status.models).sort(), [...MODEL_KEYS].sort());
});

test("the devlog guard exempts site/data so it cannot deadlock the refresh PR", async () => {
  // ci.yml requires a devlog entry whenever site/** changes. The scheduled
  // refresh PR only ever touches site/data/catalog.json, so without this
  // exemption the two workflows would block each other forever.
  const ci = await readFile(new URL("../.github/workflows/ci.yml", import.meta.url), "utf8");
  assert.match(ci, /grep -v '\^site\/data\/'/);
  assert.match(ci, /grep -qx 'site\/data\/devlog\.json'/);
});
