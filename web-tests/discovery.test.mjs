import test from "node:test";
import assert from "node:assert/strict";
import { fetchDiscovery, normalizeModelsDev } from "../worker/discovery.js";

const source = {
  registered: { api: "https://registered.example/v1", models: { free: { cost: { input: 0, output: 0 } } } },
  candidate: {
    name: "Candidate", api: "https://candidate.example/v1", doc: "https://candidate.example/docs",
    env: ["CANDIDATE_API_KEY"], npm: "@ai-sdk/openai-compatible",
    models: {
      free: { cost: { input: 0, output: 0 }, limit: { context: 100000, output: 8000 }, tool_call: true },
      paid: { cost: { input: 1, output: 1 } },
    },
  },
  unsafe: { api: "http://unsafe.example/v1", models: { free: { cost: { input: 0, output: 0 } } } },
  "credentialed-query": { api: "https://query.example/v1?api_key=must-not-be-published", models: { free: { cost: { input: 0, output: 0 } } } },
};

test("normalization excludes registered, paid and insecure endpoints", () => {
  const result = normalizeModelsDev(source, [{ id: "registered", api: "https://registered.example/v1" }]);
  assert.deepEqual(result.map((item) => item.id), ["candidate"]);
  assert.equal(result[0].models[0].tool_calling, true);
  assert.deepEqual(result[0].auth_env, ["CANDIDATE_API_KEY"]);
  assert.equal(result[0].credential_env, "OFR_CANDIDATE_API_KEY");
  assert.equal(result[0].protocol, "openai-compatible");
});

test("fetchDiscovery labels data as candidate-only", async () => {
  const snapshot = await fetchDiscovery([], async () => new Response(JSON.stringify(source)));
  assert.equal(snapshot.candidate_provider_count, 2);
  assert.match(snapshot.trust, /manual verification/);
});
