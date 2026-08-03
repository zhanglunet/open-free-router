#!/usr/bin/env node
/**
 * Refresh docs/provider-status.json from the deployed Worker's own probe.
 *
 *   node scripts/refresh-provider-status.mjs [--dry-run] [--source URL]
 *
 * The availability evidence in the published catalog used to be refreshable
 * only by a maintainer holding real API keys. It is not: worker/index.js serves
 * the KV probe snapshot from an unauthenticated /api/status, minutes fresh and
 * covering every provider. The credentials stay in Cloudflare Secrets and the
 * probing happens server-side; this only fetches the result back so it can be
 * committed as the static fallback.
 *
 * Two rules make that safe:
 *
 * 1. The snapshot is validated before anything is written. A degraded API
 *    returning an empty provider map, an old as_of or a partial batch would
 *    otherwise be committed into the repo, turning the fallback data into
 *    fallback lies.
 *
 * 2. The projection is monotone-safe: `available` passes through, everything
 *    else becomes `unverified` with the observed reason preserved. The live
 *    endpoint genuinely reports providers as `unavailable` when they rate-limit
 *    our own probe, and the rotating batches mean roughly half the models have
 *    no evidence in any single snapshot. A static file that nothing revisits
 *    for a day may withhold good news; it must never invent bad news.
 */
import { writeFile, readFile } from "node:fs/promises";
import { resolve } from "node:path";

const ROOT = resolve(import.meta.dirname, "..");
const DEFAULT_SOURCE = "https://oaf.asia/api/status";
const STATUS_PATH = resolve(ROOT, "docs", "provider-status.json");
const CATALOG_PATH = resolve(ROOT, "site", "data", "catalog.json");

/** Beyond this the deployed probe is not doing its job and must not be copied. */
export const MAX_SNAPSHOT_AGE_MS = 30 * 60 * 1000;
const FUTURE_TOLERANCE_MS = 5 * 60 * 1000;

const METHOD =
  "Cloudflare 服务器端最小请求探测快照，经公开只读接口 /api/status 取回；不保留任何凭据。" +
  "仅 available 原样通过，其余一律降级为 unverified，避免把一次采样的限流或未覆盖当成提供商故障发布。";

/**
 * Matches worker/probe.js STATUS_STALE_MS. Evidence older than this no longer
 * speaks for a model there, so carrying it into the published file would give
 * the static fallback a longer memory than the live path.
 */
export const MODEL_EVIDENCE_STALE_MS = 45 * 60 * 1000;

/**
 * @param {unknown} snapshot parsed /api/status payload
 * @param {{catalogProviderIds: string[], catalogModelKeys?: string[], nowMs: number}} options
 * @returns {{ok: boolean, errors: string[], status: object|null}}
 */
export function projectProbeSnapshot(snapshot, { catalogProviderIds, catalogModelKeys = [], nowMs }) {
  const errors = [];

  if (snapshot?.schema_version !== 2) {
    errors.push(`schema_version 是 ${JSON.stringify(snapshot?.schema_version ?? null)}，期望 2`);
  }
  if (snapshot?.source !== "cloudflare-server-probe") {
    errors.push(`source 是 ${JSON.stringify(snapshot?.source ?? null)}，期望 cloudflare-server-probe`);
  }

  const asOf = Date.parse(snapshot?.as_of ?? "");
  if (!Number.isFinite(asOf)) {
    errors.push(`as_of 无法解析：${JSON.stringify(snapshot?.as_of ?? null)}`);
  } else if (asOf - nowMs > FUTURE_TOLERANCE_MS) {
    errors.push(`as_of 位于未来（${snapshot.as_of}）`);
  } else if (nowMs - asOf > MAX_SNAPSHOT_AGE_MS) {
    errors.push(
      `as_of 已过期 ${Math.round((nowMs - asOf) / 60000)} 分钟（上限 ${MAX_SNAPSHOT_AGE_MS / 60000} 分钟）；` +
        "线上探测可能已经停摆，不能把它的结果当作新证据提交。",
    );
  }

  const providers = snapshot?.providers && typeof snapshot.providers === "object" ? snapshot.providers : null;
  if (!providers || !Object.keys(providers).length) {
    errors.push("快照里没有任何提供商");
  } else {
    const missing = catalogProviderIds.filter((id) => !(id in providers));
    if (missing.length) errors.push(`快照缺少目录中的提供商：${missing.join(", ")}`);
  }

  if (errors.length) return { ok: false, errors, status: null };

  const projected = {};
  for (const id of catalogProviderIds) {
    const entry = providers[id] ?? {};
    const available = entry.availability === "available";
    projected[id] = {
      availability: available ? "available" : "unverified",
      latency_ms: available && Number.isFinite(entry.latency_ms) ? entry.latency_ms : null,
      reason: entry.reason || (available ? "Cloudflare 服务器探测通过" : "本次快照未取得可用证据"),
    };
  }

  /* Per-model evidence, keyed exactly as /api/status keys it. Same monotone-safe
     rule as the provider level, applied independently: `available` passes
     through, everything else becomes `unverified` with the observed reason kept.
     Iterating the CATALOG's keys rather than the snapshot's is deliberate — a
     model removed from the registry must not survive as an orphan, and a model
     the rotating batch did not reach must appear as uncovered rather than
     silently absent. `upstream_error` is never copied: write_public_catalog
     rejects text containing "messages" or "prompt", so an echoed request body
     would fail the build, and it is a diagnostic for maintainers, not a public
     claim. */
  const snapshotModels = snapshot.models && typeof snapshot.models === "object" ? snapshot.models : {};
  const models = {};
  let covered = 0;
  for (const key of catalogModelKeys) {
    const evidence = snapshotModels[key];
    const checkedAt = Date.parse(evidence?.checked_at ?? "");
    const fresh = Number.isFinite(checkedAt) && nowMs - checkedAt <= MODEL_EVIDENCE_STALE_MS;
    if (!evidence || !fresh) {
      models[key] = {
        availability: "unverified",
        latency_ms: null,
        checked_at: "",
        status: "no_recent_evidence",
        reason: evidence ? "该模型的探测证据已过期" : "本次快照未覆盖该模型",
      };
      continue;
    }
    covered += 1;
    const available = evidence.availability === "available";
    models[key] = {
      availability: available ? "available" : "unverified",
      latency_ms: available && Number.isFinite(evidence.latency_ms) ? evidence.latency_ms : null,
      checked_at: evidence.checked_at,
      status: evidence.status || (available ? "http_200" : "unknown"),
      reason: evidence.reason || (available ? "Cloudflare 服务器探测通过" : "本次快照未取得可用证据"),
    };
  }

  return {
    ok: true,
    errors: [],
    status: {
      as_of: new Date(asOf).toISOString(),
      method: METHOD,
      model_evidence: { covered, total: catalogModelKeys.length },
      providers: projected,
      models,
    },
  };
}

async function main() {
  const args = new Set(process.argv.slice(2));
  const dryRun = args.has("--dry-run");
  const sourceArg = process.argv.find((value) => value.startsWith("--source="));
  const source = sourceArg ? sourceArg.slice("--source=".length) : DEFAULT_SOURCE;

  const catalog = JSON.parse(await readFile(CATALOG_PATH, "utf8"));
  const catalogProviderIds = (catalog.providers || []).map((provider) => provider.id);
  if (!catalogProviderIds.length) throw new Error("本地目录里没有提供商，先修好 site/data/catalog.json");
  const catalogModelKeys = (catalog.providers || []).flatMap((provider) =>
    (provider.models || []).map((model) => `${provider.id}/${model.id}`));

  const response = await fetch(source, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${source} 返回 HTTP ${response.status}`);
  const snapshot = await response.json();

  const { ok, errors, status } = projectProbeSnapshot(snapshot, {
    catalogProviderIds,
    catalogModelKeys,
    nowMs: Date.now(),
  });
  if (!ok) {
    console.error(`拒绝写入 docs/provider-status.json：\n  - ${errors.join("\n  - ")}`);
    process.exit(1);
  }

  const width = Math.max(...catalogProviderIds.map((id) => id.length));
  console.log(`来源 ${source} · as_of ${status.as_of}`);
  for (const [id, entry] of Object.entries(status.providers)) {
    const latency = entry.latency_ms == null ? "—" : `${entry.latency_ms}ms`;
    console.log(`  ${id.padEnd(width)}  ${entry.availability.padEnd(10)} ${latency.padStart(7)}  ${entry.reason}`);
  }

  console.log(`  逐模型证据覆盖 ${status.model_evidence.covered}/${status.model_evidence.total}` +
    `（轮换批次每轮只探测约一半，未覆盖的按 unverified 发布）`);

  if (dryRun) {
    console.log("\n--dry-run：没有写入任何文件。");
    return;
  }
  await writeFile(STATUS_PATH, `${JSON.stringify(status, null, 2)}\n`, "utf8");
  console.log(`\n已写入 ${STATUS_PATH}`);
}

if (import.meta.filename === process.argv[1]) {
  await main();
}
