import test from "node:test";
import assert from "node:assert/strict";

import worker from "../worker/index.js";
import {
  INTERNAL_BENCHMARKS_KEY,
  fetchArtificialAnalysis,
  isInternalBenchmarkAuthorized,
  refreshInternalBenchmarks,
} from "../worker/benchmarks.js";

function request(token) {
  return new Request("https://oaf.asia/api/internal/benchmarks", token ? { headers: { Authorization: `Bearer ${token}` } } : {});
}

test("internal benchmark authorization fails closed", () => {
  const env = { INTERNAL_BENCHMARKS_TOKEN: "private-token" };
  assert.equal(isInternalBenchmarkAuthorized(request(), env), false);
  assert.equal(isInternalBenchmarkAuthorized(request("wrong-token"), env), false);
  assert.equal(isInternalBenchmarkAuthorized(request("private-token"), env), true);
  assert.equal(isInternalBenchmarkAuthorized(request("private-token"), {}), false);
});

test("Artificial Analysis fetcher follows pagination without exposing the key", async () => {
  const calls = [];
  const payload = await fetchArtificialAnalysis("private-aa-key", async (url, options) => {
    calls.push({ url: String(url), key: options.headers["x-api-key"] });
    const page = Number(new URL(url).searchParams.get("page"));
    return Response.json({
      tier: "free",
      intelligence_index_version: 4.1,
      pagination: { page, total_pages: 2, has_more: page < 2 },
      data: [{ id: `m${page}`, name: `Model ${page}`, slug: `model-${page}` }],
    }, { headers: { "x-ratelimit-limit": "100", "x-ratelimit-remaining": String(100 - page) } });
  });
  assert.equal(calls.length, 2);
  assert.deepEqual(payload.data.map((item) => item.id), ["m1", "m2"]);
  assert.equal(payload.tier, "free");
  assert.equal(JSON.stringify(payload).includes("private-aa-key"), false);
});

test("refresh stores a compact internal-only snapshot in KV", async () => {
  const values = new Map();
  const env = {
    ARTIFICIAL_ANALYSIS_API_KEY: "private-aa-key",
    DISCOVERY: { put: async (key, value) => values.set(key, JSON.parse(value)) },
  };
  const catalog = { providers: [{ id: "google", models: [{ id: "gemini-test" }] }] };
  const snapshot = await refreshInternalBenchmarks(env, catalog, {
    nowMs: Date.parse("2026-08-03T00:00:00Z"),
    fetcher: async () => Response.json({
      tier: "free", intelligence_index_version: 4.1,
      pagination: { page: 1, total_pages: 1, has_more: false },
      data: [{
        id: "external-1", name: "Gemini Test", slug: "gemini-test", model_creator: { name: "Google" },
        evaluations: { artificial_analysis_intelligence_index: 42 },
      }],
    }),
  });
  assert.equal(snapshot.access, "internal-only");
  assert.equal(snapshot.model_count, 1);
  assert.equal(snapshot.matched_model_count, 1);
  assert.equal(snapshot.models[0].intelligence, 42);
  assert.deepEqual(values.get(INTERNAL_BENCHMARKS_KEY), snapshot);
  assert.equal(JSON.stringify(snapshot).includes("private-aa-key"), false);
});

test("worker protects private snapshots and disables caching", async () => {
  const snapshot = { schema_version: 1, access: "internal-only", models: [] };
  const env = {
    INTERNAL_BENCHMARKS_TOKEN: "private-token",
    DISCOVERY: { get: async (key) => key === INTERNAL_BENCHMARKS_KEY ? snapshot : null },
  };
  const ctx = { waitUntil() {} };
  const denied = await worker.fetch(request(), env, ctx);
  assert.equal(denied.status, 401);
  assert.equal(denied.headers.get("cache-control"), "private, no-store, max-age=0");
  const allowed = await worker.fetch(request("private-token"), env, ctx);
  assert.equal(allowed.status, 200);
  assert.deepEqual(await allowed.json(), snapshot);
  assert.equal(allowed.headers.get("cache-control"), "private, no-store, max-age=0");
});
