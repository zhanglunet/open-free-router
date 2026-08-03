export const STATUS_KEY = "provider-status-v2";
export const STATUS_REFRESH_LOCK_KEY = "provider-status-refresh-lock";
export const PROBE_INTERVAL_MINUTES = 15;
export const PROBE_BATCHES = 2;
export const PROBE_TIMEOUT_MS = 12_000;
export const STATUS_STALE_MS = 45 * 60 * 1000;

/**
 * Seven staleness clocks now coexist in this project. Nothing enforces
 * coherence between them, and that is where the next drift bug will be written
 * — so they are at least named in one place:
 *
 *   PROBE_INTERVAL_MINUTES  15 min   how often the Worker probes
 *   statusIsStale()         20 min   when /api/catalog kicks a background probe
 *   STATUS_STALE_MS         45 min   when a model's evidence stops counting
 *   /api/catalog            300 s    edge cache TTL (JSON_HEADERS in index.js)
 *   getDiscovery()          7 h      models.dev candidate refresh
 *   REFRESH_MAX_AGE_DAYS    3 d      scheduled republish of the static snapshot
 *   MAX_CATALOG_AGE_DAYS    7 d      build gate on the committed snapshot
 *
 * The last two live in scripts/data-freshness.mjs and are unit-asserted to keep
 * REFRESH < MAX. Reconciling the rest is tracked as M3 in
 * docs/PRD-site-quality-and-roadmap.md.
 */

const SECRET_BINDINGS = {
  "deepseek": "OFR_PROBE_DEEPSEEK_API_KEY",
  "google-ai-studio": "OFR_PROBE_GOOGLE_AI_STUDIO_API_KEY",
  "groq": "OFR_PROBE_GROQ_API_KEY",
  "nvidia-nim": "OFR_PROBE_NVIDIA_NIM_API_KEY",
  "nous": "OFR_PROBE_NOUS_API_KEY",
  "poolside": "OFR_PROBE_POOLSIDE_API_KEY",
  "openrouter": "OFR_PROBE_OPENROUTER_API_KEY",
  "sensenova": "OFR_PROBE_SENSENOVA_API_KEY",
  "stepfun": "OFR_PROBE_STEPFUN_API_KEY",
  "gitee-ai": "OFR_PROBE_GITEE_AI_API_KEY",
};

const KEYLESS_PROVIDERS = new Set(["opencode-zen-free"]);

function credentialFor(providerId, env) {
  const binding = SECRET_BINDINGS[providerId];
  // Trimmed: ai.gitee.com answers `Authorization: Bearer ` (empty credential)
  // with 400, not 401, so without this a whitespace-only secret is
  // indistinguishable from a working one in our own output — it would look
  // like the provider rejecting every model.
  return binding ? String(env[binding] || "").trim() : "";
}

/* Ported from src/open_free_router/probe.py's _CREDENTIAL_RE. Providers do
   sometimes echo the offending key back in an error body, and that body is
   about to be stored in KV and served publicly. */
const CREDENTIAL_RE = /(?:Bearer\s+)?\b(?:sk|nvapi|gsk|xai|pplx|or)[-_][A-Za-z0-9._-]{6,}|AIza[0-9A-Za-z_-]{20,}/gi;

/**
 * A non-2xx result used to carry a locally synthesised reason and zero upstream
 * information, because the body was cancelled before the status was examined.
 * That body is the one artefact separating "our credential is malformed" from
 * "this account lacks entitlement" — the two live hypotheses for gitee-ai
 * returning 400 on all 16 of its models.
 *
 * The result is served by the unauthenticated /api/status and spread into
 * /api/catalog, so redaction has to be complete, not best-effort. The exact
 * credential we sent is removed first: CREDENTIAL_RE only knows the key formats
 * we have already seen, while credentialFor accepts an arbitrary secret string
 * — a Gitee private token carries no sk_/gsk_ style prefix and would sail
 * through a prefix allowlist. The pattern pass stays as defence in depth, since
 * a body may echo some *other* provider's key.
 */
async function upstreamError(response, credential) {
  try {
    const raw = (await response.text()).slice(0, 512);
    let message = raw;
    try {
      const parsed = JSON.parse(raw);
      message = parsed?.error?.message ?? parsed?.message ?? raw;
    } catch {
      /* not JSON, or truncated mid-object: fall back to the raw slice */
    }
    let scrubbed = String(message);
    // A short secret would over-redact ordinary words; real credentials are long.
    if (credential && credential.length >= 8) {
      for (const form of new Set([credential, encodeURIComponent(credential)])) {
        scrubbed = scrubbed.split(form).join("[redacted-credential]");
      }
    }
    scrubbed = scrubbed.replace(CREDENTIAL_RE, "[redacted-credential]").trim();
    return scrubbed ? scrubbed.slice(0, 300) : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Mirrors src/open_free_router/public_catalog.py:_speed_tier exactly.
 *
 * The Python copy runs at export time against the PROVIDER's availability and
 * latency, and mergeServerStatus spreads evidence objects that contain no
 * speed_tier_zh key — so the frozen provider-derived string survived onto the
 * live catalog. Measured on production before this change: 17 models advertised
 * 中等/较慢 while unavailable (including a 12 s timeout rendering as 中等) and 2
 * advertised 当前不可用 while answering in under a second.
 */
function speedTier(availability, latencyMs) {
  if (availability === "unavailable") return "当前不可用";
  if (availability !== "available" || !Number.isFinite(latencyMs)) return "未测";
  if (latencyMs <= 1500) return "快";
  if (latencyMs <= 3000) return "中等";
  return "较慢";
}

function safeEndpoint(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password) return "";
    return url.href.replace(/\/$/, "");
  } catch {
    return "";
  }
}

function requestFor(provider, model, credential, signal) {
  const base = safeEndpoint(provider.api);
  if (!base) throw new Error("invalid_endpoint");
  const upstreamModel = model.upstream_id || model.id;
  // Google used to get a hand-written :generateContent path here. The catalog's
  // `api` field is generated from registry.default.yaml and reads
  // .../v1beta/openai, against which that path is a 404 (verified live), and
  // failureReason maps 404 to unavailable — the provider would go dark for a
  // reason indistinguishable from a real outage. The generic OpenAI-compatible
  // path below is what proxy.py already uses in production.
  const headers = {
    "Content-Type": "application/json",
    "User-Agent": "open-free-router-cloudflare/0.3",
  };
  if (credential) headers.Authorization = `Bearer ${credential}`;
  if (provider.id === "openrouter") {
    headers["HTTP-Referer"] = "https://oaf.asia/";
    headers["X-Title"] = "FreeModel Port server probe";
  }
  return [
    `${base}/chat/completions`,
    {
      method: "POST",
      signal,
      headers,
      body: JSON.stringify({
        model: upstreamModel,
        messages: [{ role: "user", content: "ping" }],
        max_tokens: 1,
        stream: false,
      }),
    },
  ];
}

function failureReason(status) {
  if (status === 401 || status === 403) return "服务器凭据无效或没有访问权限";
  if (status === 402) return "服务器探测账户没有可用额度";
  if (status === 404) return "模型或接口在提供商侧不存在";
  if (status === 429) return "提供商对服务器探测请求限流";
  if (status >= 500) return `提供商服务返回 HTTP ${status}`;
  return `服务器探测返回 HTTP ${status}`;
}

export async function probeModel(provider, model, env, fetcher = fetch, now = () => Date.now()) {
  const checkedAt = new Date(now()).toISOString();
  const credential = credentialFor(provider.id, env);
  if (!credential && !KEYLESS_PROVIDERS.has(provider.id)) {
    return {
      availability: "unverified",
      status: "no_server_credential",
      reason: "服务器尚未配置该提供商的专用 API Key",
      latency_ms: null,
      checked_at: checkedAt,
    };
  }
  const started = now();
  try {
    const [url, options] = requestFor(provider, model, credential, AbortSignal.timeout(PROBE_TIMEOUT_MS));
    const response = await fetcher(url, options);
    const latency = Math.max(0, Math.round(now() - started));
    if (response.ok) {
      if (response.body) await response.body.cancel();
      return {
        availability: "available",
        status: `http_${response.status}`,
        reason: "Cloudflare 服务器最小请求实测成功",
        latency_ms: latency,
        checked_at: checkedAt,
      };
    }
    // Read rather than cancel: failureReason() only knows the status code, and
    // the status code alone cannot tell a malformed credential from a missing
    // entitlement. Scrubbed and bounded before it goes anywhere.
    const detail = await upstreamError(response, credential);
    return {
      availability: "unavailable",
      status: `http_${response.status}`,
      reason: failureReason(response.status),
      ...(detail ? { upstream_error: detail } : {}),
      latency_ms: latency,
      checked_at: checkedAt,
    };
  } catch (error) {
    const timeout = error?.name === "TimeoutError" || error?.name === "AbortError";
    return {
      availability: "unavailable",
      status: timeout ? "timeout" : error?.message === "invalid_endpoint" ? "invalid_endpoint" : "network_error",
      reason: timeout ? "Cloudflare 服务器请求超时" : "Cloudflare 服务器无法连接提供商",
      latency_ms: timeout ? PROBE_TIMEOUT_MS : null,
      checked_at: checkedAt,
    };
  }
}

function modelKey(providerId, modelId) {
  return `${providerId}/${modelId}`;
}

function recent(result, nowMs) {
  const checked = Date.parse(result?.checked_at || 0);
  return Number.isFinite(checked) && nowMs - checked <= STATUS_STALE_MS;
}

/**
 * One derivation, two call sites.
 *
 * summarizeProviders runs at probe time over raw evidence; mergeServerStatus
 * runs at serve time over evidence that has already been through recent().
 * Sharing this function is what guarantees a provider verdict can never
 * contradict the model badges rendered beside it. The previous code copied a
 * summary frozen at probe time, so any KV snapshot older than STATUS_STALE_MS
 * produced a green provider card above its own "候选未验证" models — starting
 * at 46 minutes, and byte-identically at 3 days and at 30 days.
 *
 * `evidence` is the list of surviving per-model results; `totalModels` is the
 * provider's full model count, so the ratio reads the same in both contexts.
 */
function deriveProviderSummary(evidence, totalModels) {
  const checkedAt = evidence.map((item) => item.checked_at).filter(Boolean).sort().at(-1) || "";
  const available = evidence.filter((item) => item.availability === "available");
  const failed = evidence.filter((item) => item.availability === "unavailable");
  const latencies = available.map((item) => item.latency_ms).filter(Number.isFinite);
  let summary;
  if (available.length) {
    summary = {
      availability: "available",
      reason: `Cloudflare 服务器已验证 ${available.length}/${totalModels} 个模型可用`,
      // Math.min of an empty list is Infinity, which would serialise as null
      // through JSON anyway but reads as a measurement in the merge.
      latency_ms: latencies.length ? Math.min(...latencies) : null,
    };
  } else if (failed.length) {
    summary = { availability: "unavailable", reason: failed[0].reason, latency_ms: failed[0].latency_ms };
  } else {
    summary = {
      availability: "unverified",
      // Not "首次探测": with no surviving evidence we cannot tell a provider
      // that has never been probed from one whose evidence simply expired.
      reason: evidence[0]?.reason || "暂无有效的 Cloudflare 服务器探测证据",
      latency_ms: null,
    };
  }
  return { ...summary, checked_at: checkedAt, evidence_count: evidence.length };
}

function summarizeProviders(catalog, results, nowMs) {
  return Object.fromEntries((catalog.providers || []).map((provider) => {
    const evidence = (provider.models || [])
      .map((model) => results[modelKey(provider.id, model.id)])
      .filter((result) => recent(result, nowMs));
    return [provider.id, deriveProviderSummary(evidence, (provider.models || []).length)];
  }));
}

async function inChunks(items, size, fn) {
  const output = [];
  for (let index = 0; index < items.length; index += size) {
    output.push(...await Promise.all(items.slice(index, index + size).map(fn)));
  }
  return output;
}

export async function buildServerSnapshot(catalog, env, existing = null, options = {}) {
  const fetcher = options.fetcher || fetch;
  const nowMs = options.nowMs ?? Date.now();
  const batch = options.batch ?? Math.floor(nowMs / (PROBE_INTERVAL_MINUTES * 60 * 1000)) % PROBE_BATCHES;
  const targets = (catalog.providers || []).flatMap((provider) =>
    (provider.models || []).map((model) => ({ provider, model }))
  );
  const selected = targets.filter((_, index) => index % PROBE_BATCHES === batch);
  const prior = existing?.models && typeof existing.models === "object" ? existing.models : {};
  const results = Object.fromEntries(Object.entries(prior).filter(([, value]) => recent(value, nowMs)));
  const probed = await inChunks(selected, 5, async ({ provider, model }) => ({
    key: modelKey(provider.id, model.id),
    provider_id: provider.id,
    model_id: model.id,
    ...await probeModel(provider, model, env, fetcher, () => Date.now()),
  }));
  for (const result of probed) results[result.key] = result;
  return {
    schema_version: 2,
    source: "cloudflare-server-probe",
    scope: "all registered models in two rotating batches",
    interval_minutes: PROBE_INTERVAL_MINUTES,
    batch,
    batch_count: PROBE_BATCHES,
    checked_models: selected.length,
    total_models: targets.length,
    as_of: new Date(nowMs).toISOString(),
    providers: summarizeProviders(catalog, results, nowMs),
    models: results,
  };
}

export function statusIsStale(snapshot, nowMs = Date.now()) {
  const asOf = Date.parse(snapshot?.as_of || 0);
  return !Number.isFinite(asOf) || nowMs - asOf > 20 * 60 * 1000;
}

export function mergeServerStatus(catalog, snapshot, nowMs = Date.now()) {
  const live = snapshot?.schema_version === 2 ? snapshot : null;
  const providers = (catalog.providers || []).map((provider) => {
    // The KV summary is deliberately not read here. Only evidence that survives
    // recent() may speak for the provider — see deriveProviderSummary.
    const surviving = [];
    const models = (provider.models || []).map((model) => {
      const evidence = live?.models?.[modelKey(provider.id, model.id)];
      if (!recent(evidence, nowMs)) {
        // speed_tier_zh comes from the static export, where it was derived from
        // the provider's numbers. With no surviving evidence it is not a speed
        // claim we can stand behind.
        return {
          ...model,
          availability: "unverified",
          latency_ms: null,
          checked_at: "",
          reason: "等待服务器探测",
          speed_tier_zh: "未测",
        };
      }
      surviving.push(evidence);
      return {
        ...model,
        ...evidence,
        speed_tier_zh: speedTier(evidence.availability, evidence.latency_ms),
        key: undefined,
        provider_id: undefined,
        model_id: undefined,
      };
    });
    return { ...provider, ...deriveProviderSummary(surviving, models.length), models };
  });
  return {
    ...catalog,
    providers,
    status_as_of: live?.as_of || "",
    // A KV miss means nothing was measured. Claiming a Cloudflare measurement
    // anyway is the same class of lie the summary decay above fixes, and
    // status.js branches on exactly this field to pick its heading.
    status_source: live ? "cloudflare-server-probe" : "server-probe-pending",
    probe_scope: live?.scope || "server probe pending",
    probe_interval_minutes: PROBE_INTERVAL_MINUTES,
    status_note: live
      ? "可用性来自 Cloudflare 服务器端最小请求实测，与访问者本机无关；它是最近快照，不构成 SLA。"
      : "Cloudflare 服务器探测尚无有效快照，本页不判断可用性。",
  };
}
