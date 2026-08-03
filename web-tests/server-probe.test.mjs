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

test("a failing probe keeps a scrubbed slice of the upstream error", async () => {
  // Every non-2xx result currently carries a locally synthesised reason and
  // zero upstream information, because the body is cancelled before the status
  // is examined. That is the one artefact that separates "our credential is
  // malformed" from "the account lacks entitlement" — gitee-ai returns 400 for
  // all 16 models and we cannot tell which without it.
  const provider = { id: "gitee-ai", api: "https://ai.gitee.com/v1", models: [{ id: "DeepSeek-V3" }] };
  const result = await probeModel(provider, provider.models[0], { OFR_PROBE_GITEE_AI_API_KEY: "k" }, async () =>
    new Response(JSON.stringify({ error: { code: "400", message: "该模型需要开通资源包" } }), { status: 400 }));
  assert.equal(result.availability, "unavailable");
  assert.equal(result.status, "http_400");
  assert.equal(result.upstream_error, "该模型需要开通资源包");
});

test("a successful probe carries no upstream_error", async () => {
  const provider = { id: "groq", api: "https://api.groq.com/openai/v1", models: [{ id: "m" }] };
  const result = await probeModel(provider, provider.models[0], { OFR_PROBE_GROQ_API_KEY: "k" }, async () =>
    new Response(JSON.stringify({ choices: [] }), { status: 200 }));
  assert.equal(result.availability, "available");
  assert.equal(result.upstream_error, undefined);
});

test("a credential echoed back by the provider is scrubbed out of upstream_error", async () => {
  const provider = { id: "groq", api: "https://api.groq.com/openai/v1", models: [{ id: "m" }] };
  const result = await probeModel(provider, provider.models[0], { OFR_PROBE_GROQ_API_KEY: "gsk_abcdef123456" }, async () =>
    new Response(JSON.stringify({ error: { message: "invalid key gsk_abcdef123456 supplied" } }), { status: 401 }));
  assert.ok(!result.upstream_error.includes("gsk_abcdef123456"), result.upstream_error);
  assert.match(result.upstream_error, /redacted-credential/);
});

test("upstream_error is bounded so a hostile body cannot bloat the KV snapshot", async () => {
  const provider = { id: "groq", api: "https://api.groq.com/openai/v1", models: [{ id: "m" }] };
  const result = await probeModel(provider, provider.models[0], { OFR_PROBE_GROQ_API_KEY: "k" }, async () =>
    new Response(JSON.stringify({ error: { message: "x".repeat(5000) } }), { status: 500 }));
  assert.ok(result.upstream_error.length <= 300, `length ${result.upstream_error.length}`);
});

test("a whitespace-only secret reports no_server_credential instead of sending an empty Bearer", async () => {
  // ai.gitee.com returns 400 — not 401 — for `Authorization: Bearer ` with an
  // empty credential (verified live). Without trimming, a blank Cloudflare
  // secret is indistinguishable from a valid one in our own output.
  const provider = { id: "gitee-ai", api: "https://ai.gitee.com/v1", models: [{ id: "DeepSeek-V3" }] };
  let called = false;
  const result = await probeModel(provider, provider.models[0], { OFR_PROBE_GITEE_AI_API_KEY: "   " }, async () => {
    called = true;
    return new Response("{}", { status: 400 });
  });
  assert.equal(result.status, "no_server_credential");
  assert.equal(result.availability, "unverified");
  assert.equal(called, false, "no request may be sent with a blank credential");
});

test("speed_tier_zh is recomputed from the merged per-model evidence", () => {
  // public_catalog.py derives it from the PROVIDER's availability and latency
  // at export time, and mergeServerStatus spreads evidence objects that contain
  // no such key — so the frozen provider-derived string survives onto the live
  // catalog. Measured on production: 17 models advertised 中等/较慢 while
  // unavailable, and 2 advertised 当前不可用 while responding in under a second.
  const now = Date.parse("2026-08-03T00:00:00Z");
  const checkedAt = new Date(now - 60_000).toISOString();
  const catalog = {
    providers: [{
      id: "p",
      models: [
        { id: "fast", speed_tier_zh: "当前不可用" },
        { id: "slow", speed_tier_zh: "快" },
        { id: "down", speed_tier_zh: "中等" },
        { id: "nostatus", speed_tier_zh: "快" },
      ],
    }],
  };
  const snapshot = {
    schema_version: 2,
    as_of: checkedAt,
    providers: {},
    models: {
      "p/fast": { availability: "available", latency_ms: 800, checked_at: checkedAt },
      "p/slow": { availability: "available", latency_ms: 4000, checked_at: checkedAt },
      "p/down": { availability: "unavailable", latency_ms: 12000, checked_at: checkedAt },
    },
  };
  const models = mergeServerStatus(catalog, snapshot, now).providers[0].models;
  const tier = (id) => models.find((m) => m.id === id).speed_tier_zh;
  assert.equal(tier("fast"), "快");
  assert.equal(tier("slow"), "较慢");
  assert.equal(tier("down"), "当前不可用");
  // No surviving evidence: not a speed claim at all.
  assert.equal(tier("nostatus"), "未测");
});

/* The KV provider summary is written once at probe time and never decays. The
   model evidence beside it does decay, through recent()/STATUS_STALE_MS. Serving
   the frozen summary therefore rendered a green provider card sitting directly
   above its own "候选未验证" model badges — starting at 46 minutes, and
   byte-identically at 3 days and at 30 days. */
const agedSnapshot = (nowMs, ageMs) => {
  const checkedAt = new Date(nowMs - ageMs).toISOString();
  return {
    schema_version: 2,
    as_of: checkedAt,
    providers: {
      p: {
        availability: "available",
        reason: "Cloudflare 服务器已验证 2/2 个模型可用",
        latency_ms: 900,
        checked_at: checkedAt,
      },
    },
    models: {
      "p/a": { availability: "available", latency_ms: 900, checked_at: checkedAt },
      "p/b": { availability: "available", latency_ms: 800, checked_at: checkedAt },
    },
  };
};
const twoModelCatalog = { providers: [{ id: "p", models: [{ id: "a" }, { id: "b" }] }] };

test("a stale KV snapshot cannot keep a provider green", () => {
  const now = Date.parse("2026-08-03T00:00:00Z");
  for (const ageMs of [46 * 60 * 1000, 3 * 86_400_000, 30 * 86_400_000]) {
    const provider = mergeServerStatus(twoModelCatalog, agedSnapshot(now, ageMs), now).providers[0];
    assert.equal(provider.availability, "unverified", `age ${ageMs}ms`);
    assert.equal(provider.latency_ms, null);
    assert.ok(provider.models.every((model) => model.availability === "unverified"));
  }
});

test("fresh evidence still produces a green provider", () => {
  const now = Date.parse("2026-08-03T00:00:00Z");
  const provider = mergeServerStatus(twoModelCatalog, agedSnapshot(now, 60_000), now).providers[0];
  assert.equal(provider.availability, "available");
  assert.equal(provider.latency_ms, 800);
  assert.ok(provider.models.every((model) => model.availability === "available"));
});

test("the provider verdict never contradicts its own model badges", () => {
  const now = Date.parse("2026-08-03T00:00:00Z");
  const fresh = new Date(now - 60_000).toISOString();
  const stale = new Date(now - 3 * 86_400_000).toISOString();
  const snapshot = {
    schema_version: 2,
    as_of: fresh,
    providers: { p: { availability: "available", reason: "旧摘要", latency_ms: 900, checked_at: stale } },
    models: {
      "p/a": { availability: "unavailable", reason: "提供商服务返回 HTTP 503", latency_ms: 120, checked_at: fresh },
      "p/b": { availability: "available", latency_ms: 800, checked_at: stale },
    },
  };
  const provider = mergeServerStatus(twoModelCatalog, snapshot, now).providers[0];
  // b's evidence expired, so only a's "unavailable" survives; the provider must
  // follow it rather than the summary that still says available.
  assert.equal(provider.availability, "unavailable");
  assert.equal(provider.reason, "提供商服务返回 HTTP 503");
});

test("a KV miss must not advertise a Cloudflare measurement", () => {
  const merged = mergeServerStatus({ providers: [] }, null, Date.parse("2026-08-03T00:00:00Z"));
  assert.notEqual(merged.status_source, "cloudflare-server-probe");
  assert.ok(!merged.status_note.includes("实测"), `status_note claimed measurement: ${merged.status_note}`);
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
