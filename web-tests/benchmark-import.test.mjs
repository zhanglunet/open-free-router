import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

import { buildSnapshot, findRegistryMatches } from "../scripts/import-artificial-analysis.mjs";

const catalog = {
  providers: [{ id: "google-ai-studio", models: [{ id: "gemini-2.5-pro", upstream_id: "gemini-2.5-pro" }] }],
};
const payload = {
  version: "4.1",
  data: [{
    name: "Gemini 2.5 Pro",
    slug: "gemini-2-5-pro",
    model_creator: { name: "Google" },
    evaluations: {
      artificial_analysis_intelligence_index: 47,
      artificial_analysis_coding_index: 42,
      artificial_analysis_agentic_index: 39,
    },
    artificial_analysis_intelligence_index_cost: { cost_per_task: { total_cost: 0.22 } },
    pricing: { price_1m_input_tokens: 1.25, price_1m_output_tokens: 10 },
    performance: { median_output_tokens_per_second: 118, median_time_to_first_token_seconds: 0.44 },
  }],
};

test("Artificial Analysis snapshot keeps attribution and conservative registry match", async () => {
  const snapshot = await buildSnapshot(payload, catalog, "2026-08-02T00:00:00.000Z");
  const source = snapshot.sources[0];
  assert.equal(source.name, "Artificial Analysis");
  assert.equal(source.index_version, "4.1");
  assert.equal(source.models[0].intelligence, 47);
  assert.equal(source.models[0].cost_per_task_usd, 0.22);
  assert.deepEqual(source.models[0].registry_matches, ["google-ai-studio/gemini-2.5-pro"]);
  assert.equal(source.attribution_required, true);
  assert.match(source.license_note_zh, /署名/);
});

test("unrelated model names are not force-matched", () => {
  assert.deepEqual(findRegistryMatches({ name: "Unrelated Alpha", slug: "unrelated-alpha" }, catalog), []);
});

test("exact matches suppress broader family matches and null metrics stay empty", async () => {
  const expandedCatalog = { providers: [{ id: "google", models: [{ id: "gemini-2.5-flash" }, { id: "gemini-2.5-flash-lite" }] }] };
  assert.deepEqual(findRegistryMatches({ name: "Gemini 2.5 Flash Lite", slug: "gemini-2-5-flash-lite" }, expandedCatalog), [
    { registry_id: "google/gemini-2.5-flash-lite", match: "exact" },
  ]);
  const snapshot = await buildSnapshot({ version: "4.1", data: [{ name: "Null Model", slug: "null-model", evaluations: { artificial_analysis_intelligence_index: null } }] }, catalog);
  assert.equal(snapshot.sources[0].models[0].intelligence, null);
});

test("offline importer never writes the environment credential", async () => {
  const directory = await mkdtemp(resolve(tmpdir(), "ofr-benchmark-"));
  const input = resolve(directory, "input.json");
  const output = resolve(directory, "output.json");
  await writeFile(input, JSON.stringify({ intelligence_index_version: "4.1", data: payload.data }));
  const result = spawnSync(process.execPath, ["scripts/import-artificial-analysis.mjs", "--input", input, "--output", output], {
    cwd: resolve(import.meta.dirname, ".."),
    env: { ...process.env, ARTIFICIAL_ANALYSIS_API_KEY: "secret-test-key-must-not-leak" },
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  const content = await readFile(output, "utf8");
  assert.doesNotMatch(content, /secret-test-key-must-not-leak/);
  await rm(directory, { recursive: true, force: true });
});
