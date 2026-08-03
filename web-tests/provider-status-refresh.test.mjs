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
