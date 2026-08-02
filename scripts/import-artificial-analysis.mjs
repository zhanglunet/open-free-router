#!/usr/bin/env node

import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { basename, dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const API_URL = "https://artificialanalysis.ai/api/v2/language/models/free";
const ROOT = resolve(import.meta.dirname, "..");

function parseArgs(argv) {
  const args = { input: "", output: resolve(ROOT, "site/data/benchmarks.json") };
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === "--input") args.input = resolve(argv[++index] || "");
    else if (token === "--output") args.output = resolve(argv[++index] || "");
    else if (token === "--help" || token === "-h") args.help = true;
    else throw new Error(`未知参数：${token}`);
  }
  return args;
}

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

function externalKeys(model) {
  return [...new Set([model.name, model.slug].map(canonical).filter((value) => value.length >= 5))];
}

function findRegistryMatches(external, catalog) {
  const targets = externalKeys(external);
  if (!targets.length) return [];
  const matches = [];
  for (const provider of catalog.providers || []) {
    for (const model of provider.models || []) {
      const keys = modelKeys(model);
      const exact = targets.some((target) => keys.includes(target));
      const family = !exact && targets.some((target) => keys.some((key) =>
        Math.min(target.length, key.length) >= 7 && (target.includes(key) || key.includes(target))
      ));
      if (exact || family) {
        matches.push({ registry_id: `${provider.id}/${model.id}`, match: exact ? "exact" : "family" });
      }
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
    name: String(model.name || ""),
    slug: String(model.slug || ""),
    creator: String(model.model_creator?.name || model.creator?.name || ""),
    intelligence: numberOrNull(evaluations.artificial_analysis_intelligence_index),
    coding: numberOrNull(evaluations.artificial_analysis_coding_index),
    agentic: numberOrNull(evaluations.artificial_analysis_agentic_index),
    cost_per_task_usd: numberOrNull(
      model.artificial_analysis_intelligence_index_cost?.cost_per_task?.total_cost
      ?? model.cost_per_task?.value
      ?? model.cost_per_task
    ),
    input_price_per_million_usd: numberOrNull(pricing.price_1m_input_tokens),
    output_price_per_million_usd: numberOrNull(pricing.price_1m_output_tokens),
    median_output_tokens_per_second: numberOrNull(performance.median_output_tokens_per_second),
    median_time_to_first_token_seconds: numberOrNull(performance.median_time_to_first_token_seconds),
    registry_matches: matches.map((match) => match.registry_id),
    match_label: !matches.length ? "未匹配" : matches.some((match) => match.match === "family") ? "模型族匹配" : "精确名称匹配",
  };
}

async function fetchAll(apiKey, fetchImpl = fetch) {
  if (!apiKey) {
    throw new Error("缺少 ARTIFICIAL_ANALYSIS_API_KEY；请通过环境变量提供，不要写入文件或命令参数。");
  }
  const models = [];
  let page = 1;
  let version = null;
  while (page <= 100) {
    const url = new URL(API_URL);
    url.searchParams.set("page", String(page));
    const response = await fetchImpl(url, { headers: { "x-api-key": apiKey } });
    if (!response.ok) throw new Error(`Artificial Analysis API 请求失败：HTTP ${response.status}`);
    const payload = await response.json();
    if (!Array.isArray(payload.data)) throw new Error("Artificial Analysis API 返回缺少 data 数组");
    version ||= payload.intelligence_index_version || payload.version || null;
    models.push(...payload.data);
    const pagination = payload.pagination || payload.meta?.pagination || {};
    const totalPages = Number(pagination.total_pages || pagination.last_page || 1);
    if (page >= totalPages || payload.data.length === 0) break;
    page += 1;
  }
  return { version, data: models };
}

async function readInput(path) {
  const payload = JSON.parse(await readFile(path, "utf8"));
  if (Array.isArray(payload)) return { version: null, data: payload };
  if (!Array.isArray(payload.data)) throw new Error("输入快照必须包含 data 数组");
  return { version: payload.intelligence_index_version || payload.version || null, data: payload.data };
}

async function buildSnapshot(payload, catalog, importedAt = new Date().toISOString()) {
  const models = payload.data
    .map((model) => compactModel(model, catalog))
    .filter((model) => model.name || model.slug)
    .sort((left, right) => (right.intelligence ?? -1) - (left.intelligence ?? -1));
  return {
    schema_version: 1,
    generated_at: importedAt,
    sources: [{
      id: "artificial-analysis",
      name: "Artificial Analysis",
      url: "https://artificialanalysis.ai/",
      api_url: API_URL,
      status: "imported",
      index_version: payload.version,
      attribution_required: true,
      license_note_zh: "展示和分享必须清晰署名 Artificial Analysis；再分发或商业使用须遵守其条款并自行确认授权。",
      last_imported_at: importedAt,
      models,
    }],
    methodology: {
      local_readiness_zh: "本地任务就绪度由服务器实测可用性、免费证据、声明能力和延迟组成，不等于模型智力。",
      external_quality_zh: "外部质量分仅来自 Artificial Analysis API 快照；未匹配或未导入时不推断、不补零。",
    },
  };
}

async function writeAtomic(path, value) {
  await mkdir(dirname(path), { recursive: true });
  const temporary = resolve(dirname(path), `.${basename(path)}.${process.pid}.tmp`);
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o644 });
  await rename(temporary, path);
}

async function main(argv = process.argv.slice(2)) {
  const args = parseArgs(argv);
  if (args.help) {
    console.log("用法：node scripts/import-artificial-analysis.mjs [--input snapshot.json] [--output path]");
    return;
  }
  const catalog = JSON.parse(await readFile(resolve(ROOT, "site/data/catalog.json"), "utf8"));
  const payload = args.input
    ? await readInput(args.input)
    : await fetchAll(process.env.ARTIFICIAL_ANALYSIS_API_KEY);
  const snapshot = await buildSnapshot(payload, catalog);
  await writeAtomic(args.output, snapshot);
  console.log(`已写入 ${args.output}：${snapshot.sources[0].models.length} 个公开评测条目。`);
}

if (import.meta.url === pathToFileURL(process.argv[1] || "").href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}

export { buildSnapshot, canonical, compactModel, fetchAll, findRegistryMatches, parseArgs };
