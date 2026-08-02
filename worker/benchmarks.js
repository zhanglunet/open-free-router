export const INTERNAL_BENCHMARKS_KEY = "private:artificial-analysis:benchmarks:v1";
export const ARTIFICIAL_ANALYSIS_API_URL = "https://artificialanalysis.ai/api/v2/language/models/free";

function canonical(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/:free$/i, "")
    .replace(/\([^)]*\)/g, "")
    .replace(/\b(?:preview|instruct|chat|latest|thinking|reasoning)\b/g, "")
    .replace(/[^a-z0-9]+/g, "");
}

function modelKeys(model) {
  const values = [model.id, model.upstream_id, model.name]
    .filter(Boolean)
    .flatMap((value) => [value, String(value).split("/").at(-1)]);
  return [...new Set(values.map(canonical).filter((value) => value.length >= 5))];
}

function findRegistryMatches(external, catalog) {
  const targets = [...new Set([external.name, external.slug].map(canonical).filter((value) => value.length >= 5))];
  if (!targets.length) return [];
  const matches = [];
  for (const provider of catalog.providers || []) {
    for (const model of provider.models || []) {
      const keys = modelKeys(model);
      const exact = targets.some((target) => keys.includes(target));
      const family = !exact && targets.some((target) => keys.some((key) =>
        Math.min(target.length, key.length) >= 7 && (target.includes(key) || key.includes(target))
      ));
      if (exact || family) matches.push({ registry_id: `${provider.id}/${model.id}`, match: exact ? "exact" : "family" });
    }
  }
  const exactMatches = matches.filter((match) => match.match === "exact");
  return exactMatches.length ? exactMatches : matches;
}

function numberOrNull(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function compactModel(model, catalog) {
  const evaluations = model.evaluations || {};
  const pricing = model.pricing || {};
  const performance = model.performance || {};
  const matches = findRegistryMatches(model, catalog);
  return {
    id: String(model.id || ""),
    name: String(model.name || ""),
    slug: String(model.slug || ""),
    creator: String(model.model_creator?.name || ""),
    release_date: model.release_date || null,
    intelligence: numberOrNull(evaluations.artificial_analysis_intelligence_index),
    coding: numberOrNull(evaluations.artificial_analysis_coding_index),
    agentic: numberOrNull(evaluations.artificial_analysis_agentic_index),
    cost_per_task_usd: numberOrNull(model.artificial_analysis_intelligence_index_cost?.cost_per_task?.total_cost),
    input_price_per_million_usd: numberOrNull(pricing.price_1m_input_tokens),
    output_price_per_million_usd: numberOrNull(pricing.price_1m_output_tokens),
    median_output_tokens_per_second: numberOrNull(performance.median_output_tokens_per_second),
    median_time_to_first_token_seconds: numberOrNull(performance.median_time_to_first_token_seconds),
    median_end_to_end_response_time_seconds: numberOrNull(performance.median_end_to_end_response_time_seconds),
    registry_matches: matches.map((match) => match.registry_id),
    match_type: !matches.length ? null : matches.some((match) => match.match === "family") ? "family" : "exact",
  };
}

export function isInternalBenchmarkAuthorized(request, env) {
  const expected = String(env.INTERNAL_BENCHMARKS_TOKEN || "");
  const header = request.headers.get("Authorization") || "";
  const supplied = header.startsWith("Bearer ") ? header.slice(7) : "";
  if (!expected || !supplied || expected.length !== supplied.length) return false;
  let mismatch = 0;
  for (let index = 0; index < expected.length; index += 1) mismatch |= expected.charCodeAt(index) ^ supplied.charCodeAt(index);
  return mismatch === 0;
}

export async function fetchArtificialAnalysis(apiKey, fetcher = fetch) {
  if (!apiKey) throw new Error("artificial_analysis_secret_missing");
  const data = [];
  let page = 1;
  let tier = null;
  let indexVersion = null;
  let quota = null;
  while (page <= 10) {
    const url = new URL(ARTIFICIAL_ANALYSIS_API_URL);
    url.searchParams.set("page", String(page));
    const response = await fetcher(url, { headers: { "x-api-key": apiKey, Accept: "application/json" } });
    if (!response.ok) throw new Error(`artificial_analysis_http_${response.status}`);
    const payload = await response.json();
    if (!Array.isArray(payload.data)) throw new Error("artificial_analysis_invalid_payload");
    tier ||= payload.tier || response.headers.get("x-aa-tier") || null;
    indexVersion ||= payload.intelligence_index_version || null;
    quota = {
      limit: response.headers.get("x-ratelimit-limit"),
      remaining: response.headers.get("x-ratelimit-remaining"),
      reset: response.headers.get("x-ratelimit-reset"),
    };
    data.push(...payload.data);
    const pagination = payload.pagination || {};
    if (!pagination.has_more || page >= Number(pagination.total_pages || 1) || payload.data.length === 0) break;
    page += 1;
  }
  return { tier, indexVersion, quota, data };
}

export async function refreshInternalBenchmarks(env, catalog, options = {}) {
  const payload = await fetchArtificialAnalysis(env.ARTIFICIAL_ANALYSIS_API_KEY, options.fetcher);
  const generatedAt = new Date(options.nowMs ?? Date.now()).toISOString();
  const models = payload.data
    .map((model) => compactModel(model, catalog))
    .filter((model) => model.name || model.slug)
    .sort((left, right) => (right.intelligence ?? -1) - (left.intelligence ?? -1));
  const snapshot = {
    schema_version: 1,
    generated_at: generatedAt,
    access: "internal-only",
    source: {
      name: "Artificial Analysis",
      url: "https://artificialanalysis.ai/",
      api_url: ARTIFICIAL_ANALYSIS_API_URL,
      tier: payload.tier,
      index_version: payload.indexVersion,
      attribution_required: true,
      license_note_zh: "内部使用；不得从本接口公开再分发。公开展示前必须取得 Artificial Analysis 相应授权。",
      quota: payload.quota,
    },
    model_count: models.length,
    matched_model_count: models.filter((model) => model.registry_matches.length).length,
    models,
  };
  await env.DISCOVERY.put(INTERNAL_BENCHMARKS_KEY, JSON.stringify(snapshot));
  return snapshot;
}

export async function loadInternalBenchmarks(env) {
  return env.DISCOVERY.get(INTERNAL_BENCHMARKS_KEY, { type: "json" });
}
