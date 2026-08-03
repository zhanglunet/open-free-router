import test from "node:test";
import assert from "node:assert/strict";

import {
  STATUS_KEY,
  buildServerSnapshot,
  mergeServerStatus,
  probeModel,
  statusIsStale,
} from "../worker/probe.js";
import worker from "../worker/index.js";

const openaiProvider = {
  id: "deepseek",
  api: "https://api.deepseek.com/v1",
  models: [{ id: "deepseek-chat", upstream_id: "deepseek-chat" }],
};

test("server probe treats missing credentials as unverified without a request", async () => {
  let called = false;
  const result = await probeModel(openaiProvider, openaiProvider.models[0], {}, async () => {
    called = true;
    return new Response("unexpected");
  });
  assert.equal(called, false);
  assert.equal(result.availability, "unverified");
  assert.equal(result.status, "no_server_credential");
});

test("server probe sends a minimal authenticated OpenAI-compatible request", async () => {
  let captured;
  let clock = 1000;
  const result = await probeModel(openaiProvider, openaiProvider.models[0], {
    OFR_PROBE_DEEPSEEK_API_KEY: "private-test-key",
  }, async (url, options) => {
    captured = { url, options };
    clock = 1123;
    return new Response(JSON.stringify({ choices: [] }), { status: 200 });
  }, () => clock);
  assert.equal(result.availability, "available");
  assert.equal(result.latency_ms, 123);
  assert.equal(captured.url, "https://api.deepseek.com/v1/chat/completions");
  assert.equal(captured.options.headers.Authorization, "Bearer private-test-key");
  const body = JSON.parse(captured.options.body);
  assert.equal(body.model, "deepseek-chat");
  assert.equal(body.max_tokens, 1);
});

test("Google server probe uses the OpenAI-compatible endpoint the catalog advertises", async () => {
  // The published catalog's api field is generated from registry.default.yaml
  // and reads .../v1beta/openai. A hand-written special case that appended
  // /models/X:generateContent to it produced a 404 (verified live), which
  // failureReason maps to unavailable — Google would go dark on the status
  // board for a reason that looks like a provider outage. The generic path
  // .../v1beta/openai/chat/completions exists and is what proxy.py uses.
  const provider = {
    id: "google-ai-studio",
    api: "https://generativelanguage.googleapis.com/v1beta/openai",
    models: [{ id: "gemini-test", upstream_id: "gemini-test" }],
  };
  let captured;
  const result = await probeModel(provider, provider.models[0], {
    OFR_PROBE_GOOGLE_AI_STUDIO_API_KEY: "google-private-key",
  }, async (url, options) => {
    captured = { url, options };
    return new Response("{}", { status: 200 });
  });
  assert.equal(result.availability, "available");
  assert.equal(captured.url, "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions");
  assert.equal(captured.options.headers.Authorization, "Bearer google-private-key");
  assert.equal(captured.options.headers["X-Goog-Api-Key"], undefined);
  assert.equal(JSON.parse(captured.options.body).model, "gemini-test");
});

test("rotating server snapshot stays below one half of the model catalog", async () => {
  const catalog = {
    providers: [{
      id: "opencode-zen-free",
      api: "https://opencode.ai/zen/v1",
      models: [0, 1, 2, 3, 4].map((index) => ({ id: `m${index}`, upstream_id: `m${index}` })),
    }],
  };
  let requests = 0;
  const snapshot = await buildServerSnapshot(catalog, {}, null, {
    batch: 0,
    nowMs: Date.parse("2026-08-02T00:00:00Z"),
    fetcher: async () => {
      requests += 1;
      return new Response("{}", { status: 200 });
    },
  });
  assert.equal(snapshot.checked_models, 3);
  assert.equal(requests, 3);
  assert.equal(Object.keys(snapshot.models).length, 3);
  assert.equal(snapshot.providers["opencode-zen-free"].availability, "available");
});

test("public catalog discards the old local snapshot and merges server evidence", () => {
  const catalog = {
    status_as_of: "old-local-time",
    providers: [{
      id: "deepseek",
      availability: "unavailable",
      reason: "old local failure",
      models: [{ id: "deepseek-chat", availability: "unavailable" }],
    }],
  };
  const checkedAt = "2026-08-02T00:00:00.000Z";
  const snapshot = {
    schema_version: 2,
    as_of: checkedAt,
    scope: "server probe",
    providers: {
      deepseek: { availability: "available", reason: "server ok", latency_ms: 99, checked_at: checkedAt },
    },
    models: {
      "deepseek/deepseek-chat": { availability: "available", reason: "server ok", latency_ms: 99, checked_at: checkedAt },
    },
  };
  const merged = mergeServerStatus(catalog, snapshot, Date.parse("2026-08-02T00:10:00Z"));
  assert.equal(merged.status_source, "cloudflare-server-probe");
  assert.equal(merged.providers[0].availability, "available");
  assert.equal(merged.providers[0].models[0].availability, "available");
  assert.equal(merged.status_as_of, checkedAt);
});

test("server status freshness expires independently of browser refreshes", () => {
  const now = Date.parse("2026-08-02T01:00:00Z");
  assert.equal(statusIsStale({ as_of: "2026-08-02T00:50:00Z" }, now), false);
  assert.equal(statusIsStale({ as_of: "2026-08-02T00:30:00Z" }, now), true);
});

test("catalog API replaces the bundled local status with the Cloudflare snapshot", async () => {
  const checkedAt = new Date().toISOString();
  const staticCatalog = {
    provider_count: 1,
    model_count: 1,
    providers: [{
      id: "deepseek",
      api: "https://api.deepseek.com/v1",
      availability: "unavailable",
      reason: "old local result",
      models: [{ id: "deepseek-chat", availability: "unavailable" }],
    }],
  };
  const values = new Map([
    ["catalog", { schema_version: 2, generated_at: checkedAt, providers: [] }],
    [STATUS_KEY, {
      schema_version: 2,
      as_of: checkedAt,
      scope: "server probe",
      providers: { deepseek: { availability: "available", reason: "server ok", latency_ms: 88, checked_at: checkedAt } },
      models: { "deepseek/deepseek-chat": { availability: "available", reason: "server ok", latency_ms: 88, checked_at: checkedAt } },
    }],
  ]);
  const env = {
    ASSETS: { fetch: async () => Response.json(staticCatalog) },
    DISCOVERY: {
      get: async (key) => values.get(key) ?? null,
      put: async (key, value) => values.set(key, JSON.parse(value)),
      delete: async (key) => values.delete(key),
    },
  };
  const background = [];
  const response = await worker.fetch(new Request("https://oaf.asia/api/catalog"), env, {
    waitUntil: (promise) => background.push(promise),
  });
  assert.equal(response.status, 200);
  const catalog = await response.json();
  assert.equal(catalog.status_source, "cloudflare-server-probe");
  assert.equal(catalog.providers[0].availability, "available");
  assert.equal(catalog.providers[0].models[0].availability, "available");
  assert.equal(background.length, 0);
});
