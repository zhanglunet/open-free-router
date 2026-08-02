import { fetchDiscovery } from "./discovery.js";
import {
  isInternalBenchmarkAuthorized,
  loadInternalBenchmarks,
  refreshInternalBenchmarks,
} from "./benchmarks.js";
import { STATUS_KEY, STATUS_REFRESH_LOCK_KEY, buildServerSnapshot, mergeServerStatus, statusIsStale } from "./probe.js";

const DISCOVERY_KEY = "catalog";
const JSON_HEADERS = {
  "Content-Type": "application/json; charset=utf-8",
  "Cache-Control": "public, max-age=300",
  "X-Content-Type-Options": "nosniff",
};
const PRIVATE_JSON_HEADERS = {
  "Content-Type": "application/json; charset=utf-8",
  "Cache-Control": "private, no-store, max-age=0",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
};

async function loadRegistry(env) {
  const response = await env.ASSETS.fetch("https://assets.local/data/catalog.json");
  if (!response.ok) throw new Error(`catalog asset returned HTTP ${response.status}`);
  return response.json();
}

async function refreshDiscovery(env) {
  const registry = await loadRegistry(env);
  const snapshot = await fetchDiscovery(registry.providers);
  await env.DISCOVERY.put(DISCOVERY_KEY, JSON.stringify(snapshot));
  return snapshot;
}

async function loadProviderStatus(env) {
  return env.DISCOVERY.get(STATUS_KEY, { type: "json" });
}

async function refreshProviderStatus(env) {
  const catalog = await loadRegistry(env);
  const existing = await loadProviderStatus(env);
  const snapshot = await buildServerSnapshot(catalog, env, existing);
  await env.DISCOVERY.put(STATUS_KEY, JSON.stringify(snapshot));
  await env.DISCOVERY.delete(STATUS_REFRESH_LOCK_KEY);
  console.log(JSON.stringify({
    event: "provider_probe_complete",
    batch: snapshot.batch,
    checked_models: snapshot.checked_models,
    total_models: snapshot.total_models,
    as_of: snapshot.as_of,
  }));
  return snapshot;
}

async function refreshProviderStatusIfNeeded(env, ctx, snapshot) {
  if (!statusIsStale(snapshot)) return;
  const lock = await env.DISCOVERY.get(STATUS_REFRESH_LOCK_KEY);
  if (lock) return;
  await env.DISCOVERY.put(STATUS_REFRESH_LOCK_KEY, new Date().toISOString(), { expirationTtl: 60 });
  ctx.waitUntil(refreshProviderStatus(env).catch(async (error) => {
    await env.DISCOVERY.delete(STATUS_REFRESH_LOCK_KEY);
    console.error(JSON.stringify({ event: "provider_probe_failed", message: String(error?.message || error) }));
  }));
}

async function getDiscovery(env, ctx) {
  const existing = await env.DISCOVERY.get(DISCOVERY_KEY, { type: "json" });
  if (existing?.schema_version >= 2) {
    const age = Date.now() - Date.parse(existing.generated_at || 0);
    if (age > 7 * 60 * 60 * 1000) ctx.waitUntil(refreshDiscovery(env));
    return existing;
  }
  return refreshDiscovery(env);
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: JSON_HEADERS });
}

function privateJson(data, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(data), { status, headers: { ...PRIVATE_JSON_HEADERS, ...extraHeaders } });
}

async function handleInternalBenchmarks(request, env) {
  if (!isInternalBenchmarkAuthorized(request, env)) {
    return privateJson({ error: "unauthorized" }, 401, { "WWW-Authenticate": "Bearer" });
  }
  const url = new URL(request.url);
  if (url.pathname === "/api/internal/benchmarks" && request.method === "GET") {
    const snapshot = await loadInternalBenchmarks(env);
    if (!snapshot) return privateJson({ error: "snapshot_pending" }, 503);
    return privateJson(snapshot);
  }
  if (url.pathname === "/api/internal/benchmarks/refresh" && request.method === "POST") {
    try {
      const catalog = await loadRegistry(env);
      return privateJson(await refreshInternalBenchmarks(env, catalog));
    } catch (error) {
      console.error(JSON.stringify({ event: "internal_benchmarks_refresh_failed", message: String(error?.message || error) }));
      return privateJson({ error: "refresh_failed", message: String(error?.message || error) }, 502);
    }
  }
  return privateJson({ error: "method_not_allowed" }, 405, { Allow: url.pathname.endsWith("/refresh") ? "POST" : "GET" });
}

export function installManifest() {
  return {
    name: "open-free-router",
    brand: "模力自由港",
    brand_en: "FreeModel Port",
    repository: "https://github.com/zhanglunet/open-free-router",
    installer: "https://oaf.asia/install.sh",
    one_liner: "curl -fsSL https://oaf.asia/install.sh | bash -s -- --codex --auto-discovery",
    safe_steps: [
      "curl -fsSLo /tmp/open-free-router-install.sh https://oaf.asia/install.sh",
      "less /tmp/open-free-router-install.sh",
      "bash /tmp/open-free-router-install.sh --codex --auto-discovery",
    ],
    next: ["open-free-router setup", "open-free-router serve", "codex --profile open-free-router"],
  };
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/api/internal/benchmarks" || url.pathname === "/api/internal/benchmarks/refresh") {
      return handleInternalBenchmarks(request, env);
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return json({ error: "method_not_allowed" }, 405);
    }
    if (url.pathname === "/api/health") {
      const status = await loadProviderStatus(env);
      return json({
        ok: true,
        discovery_schedule: "every 6 hours",
        provider_probe_schedule: "every 15 minutes",
        provider_probe_source: "cloudflare-server-probe",
        provider_probe_as_of: status?.as_of || null,
        source: "models.dev",
      });
    }
    if (url.pathname === "/api/install") {
      return json(installManifest());
    }
    if (url.pathname === "/api/discovery") {
      try { return json(await getDiscovery(env, ctx)); }
      catch (error) { return json({ error: "discovery_unavailable", message: error.message }, 503); }
    }
    if (url.pathname === "/api/catalog") {
      try {
        const [catalog, discovery, status] = await Promise.all([
          loadRegistry(env), getDiscovery(env, ctx), loadProviderStatus(env),
        ]);
        await refreshProviderStatusIfNeeded(env, ctx, status);
        return json({ ...mergeServerStatus(catalog, status), discovery });
      } catch (error) {
        return json({ error: "catalog_unavailable", message: error.message }, 503);
      }
    }
    if (url.pathname === "/api/status") {
      const status = await loadProviderStatus(env);
      if (!status) return json({ error: "server_probe_pending" }, 503);
      return json(status);
    }
    return env.ASSETS.fetch(request);
  },

  async scheduled(controller, env, ctx) {
    const tasks = [];
    if (controller.cron === "17 */6 * * *") {
      tasks.push(refreshDiscovery(env));
      if (env.ARTIFICIAL_ANALYSIS_API_KEY) {
        tasks.push(loadRegistry(env).then((catalog) => refreshInternalBenchmarks(env, catalog)));
      }
    }
    if (controller.cron === "*/15 * * * *") tasks.push(refreshProviderStatus(env));
    ctx.waitUntil(Promise.allSettled(tasks).then((outcomes) => {
      for (const outcome of outcomes) {
        if (outcome.status === "rejected") {
          console.error(JSON.stringify({ event: "scheduled_task_failed", message: String(outcome.reason?.message || outcome.reason) }));
        }
      }
    }));
  },
};
