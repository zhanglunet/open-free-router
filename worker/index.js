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
  // A transient 503 must not be cached for five minutes by browsers and
  // shared caches the way a successful catalog response is.
  const headers = status >= 400 ? { ...JSON_HEADERS, "Cache-Control": "no-store" } : JSON_HEADERS;
  return new Response(JSON.stringify(data), { status, headers });
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

// 六小时格子：`*/15` 这条 cron 里，哪些触发要顺带刷新目录与基准。
//
// 目录/基准原本挂在自己那条 `17 */6 * * *` 上。合并的动机不在本项目——
// Workers **Free** 档的 cron 触发器是「每账号 5 条」且已占满，本 Worker 一个人占了 2 条，
// 别的项目因此配不上定时任务。折进 `*/15` 之后腾出一格，**本项目行为不变**：
// 仍是每 6 小时一次、每天 4 次，只是从 :17 移到 :00，且不增加任何调用次数。
//
// 判据取 `controller.scheduledTime` 而不是 `Date.now()`：cron 是尽力而为的触发，
// 真实执行时刻可能晚几十秒；scheduledTime 是**计划时刻**，用它判格子，迟到不会让这一格丢掉。
//
// 为什么钉「分钟 === 0」而不是「=== 15」：`*/15` 只是当下的节奏。任何 `*/N`（N 整除 60）
// 都会在整点触发，钉在 0 分让这段逻辑不随节奏调整而**静默失效**——而静默失效正是它替换掉的
// 那种写法的毛病：`controller.cron === "17 */6 * * *"` 是个必须与 wrangler.jsonc 逐字相同的
// 字符串，改了配置这里不报错，只是不再执行。
//
// 拿不到计划时刻时返回 false（跳过本轮）而不是 true：目录刷新单次约 245 ms CPU，
// 误判成「每格都跑」会把它从 4 次/天变成 96 次/天。漏跑有兜底——`getDiscovery` 在快照
// 超过 7 小时时会自行补刷。
//
// （本段用行注释而非 /** */：cron 表达式里的 `*/` 会把块注释提前闭合。）
export function isSixHourlySlot(scheduledTime) {
  const at = new Date(scheduledTime);
  if (Number.isNaN(at.getTime())) return false;
  return at.getUTCHours() % 6 === 0 && at.getUTCMinutes() === 0;
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
      // mktemp, not a fixed /tmp name: on a shared host the predictable path
      // can be pre-created by someone else, which defeats the inspect step.
      "f=$(mktemp)",
      "curl -fsSLo \"$f\" https://oaf.asia/install.sh",
      "less \"$f\"",
      "bash \"$f\" --codex --auto-discovery",
    ],
    next: ["open-free-router setup", "open-free-router serve", "codex --profile open-free-router"],
  };
}

/**
 * Security response headers live in `site/_headers`, not here.
 *
 * wrangler.jsonc sets `run_worker_first: ["/api/*"]`, so page and asset
 * requests are answered by Cloudflare's Asset Worker and never reach this
 * handler — any header set here would be dead code for everything except
 * the JSON APIs below (which carry their own headers). `_headers` is served
 * by the asset layer and keeps edge caching intact.
 */
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
      catch (error) {
        console.error(JSON.stringify({ event: "discovery_unavailable", message: String(error?.message || error) }));
        return json({ error: "discovery_unavailable" }, 503);
      }
    }
    if (url.pathname === "/api/catalog") {
      try {
        // The status board polls this every 60s but only reads availability,
        // so it asks for ?fields=status and skips the discovery half.
        const withDiscovery = url.searchParams.get("fields") !== "status";
        // Discovery is a soft dependency: a models.dev outage must not take
        // the whole catalog (and with it the model radar) down to a 503.
        const [catalog, discovery, status] = await Promise.all([
          loadRegistry(env),
          withDiscovery ? getDiscovery(env, ctx).catch(() => null) : Promise.resolve(null),
          loadProviderStatus(env),
        ]);
        await refreshProviderStatusIfNeeded(env, ctx, status);
        const merged = mergeServerStatus(catalog, status);
        if (!withDiscovery) return json(merged);
        return json({ ...merged, discovery });
      } catch (error) {
        console.error(JSON.stringify({ event: "catalog_unavailable", message: String(error?.message || error) }));
        return json({ error: "catalog_unavailable" }, 503);
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
    const tasks = [refreshProviderStatus(env)];
    if (isSixHourlySlot(controller.scheduledTime)) {
      tasks.push(refreshDiscovery(env));
      if (env.ARTIFICIAL_ANALYSIS_API_KEY) {
        tasks.push(loadRegistry(env).then((catalog) => refreshInternalBenchmarks(env, catalog)));
      }
    }
    ctx.waitUntil(Promise.allSettled(tasks).then((outcomes) => {
      for (const outcome of outcomes) {
        if (outcome.status === "rejected") {
          console.error(JSON.stringify({ event: "scheduled_task_failed", message: String(outcome.reason?.message || outcome.reason) }));
        }
      }
    }));
  },
};
