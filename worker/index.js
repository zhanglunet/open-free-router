import { fetchDiscovery } from "./discovery.js";

const DISCOVERY_KEY = "catalog";
const JSON_HEADERS = {
  "Content-Type": "application/json; charset=utf-8",
  "Cache-Control": "public, max-age=300",
  "X-Content-Type-Options": "nosniff",
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

async function getDiscovery(env, ctx) {
  const existing = await env.DISCOVERY.get(DISCOVERY_KEY, { type: "json" });
  if (existing) {
    const age = Date.now() - Date.parse(existing.generated_at || 0);
    if (age > 7 * 60 * 60 * 1000) ctx.waitUntil(refreshDiscovery(env));
    return existing;
  }
  return refreshDiscovery(env);
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: JSON_HEADERS });
}

export function installManifest() {
  return {
    name: "open-free-router",
    repository: "https://github.com/zhanglunet/open-free-router",
    installer: "https://oaf.asia/install.sh",
    one_liner: "curl -fsSL https://oaf.asia/install.sh | bash -s -- --codex",
    safe_steps: [
      "curl -fsSLo /tmp/open-free-router-install.sh https://oaf.asia/install.sh",
      "less /tmp/open-free-router-install.sh",
      "bash /tmp/open-free-router-install.sh --codex",
    ],
    next: ["open-free-router setup", "open-free-router serve", "codex --profile open-free-router"],
  };
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method !== "GET" && request.method !== "HEAD") {
      return json({ error: "method_not_allowed" }, 405);
    }
    if (url.pathname === "/api/health") {
      return json({ ok: true, discovery_schedule: "every 6 hours", source: "models.dev" });
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
        const [catalog, discovery] = await Promise.all([loadRegistry(env), getDiscovery(env, ctx)]);
        return json({ ...catalog, discovery });
      } catch (error) {
        return json({ error: "catalog_unavailable", message: error.message }, 503);
      }
    }
    return env.ASSETS.fetch(request);
  },

  async scheduled(_controller, env, ctx) {
    ctx.waitUntil(refreshDiscovery(env));
  },
};
