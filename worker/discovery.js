export const MODELS_DEV_URL = "https://models.dev/api.json";

function safeHttpsUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash) return "";
    return url.href.replace(/\/$/, "");
  } catch {
    return "";
  }
}

function zeroCost(model) {
  return model?.cost?.input === 0 && model?.cost?.output === 0;
}

export function normalizeModelsDev(data, registeredProviders = []) {
  const names = new Set(registeredProviders.map((item) => item.id?.toLowerCase()).filter(Boolean));
  const hosts = new Set(registeredProviders.map((item) => {
    try { return new URL(item.api).hostname; } catch { return ""; }
  }).filter(Boolean));
  const providers = [];
  for (const [id, provider] of Object.entries(data ?? {})) {
    if (!provider || typeof provider !== "object" || names.has(id.toLowerCase())) continue;
    const api = safeHttpsUrl(provider.api ?? "");
    if (!api || hosts.has(new URL(api).hostname)) continue;
    const models = Object.entries(provider.models ?? {}).flatMap(([modelId, model]) => {
      if (!model || model.status === "deprecated" || !zeroCost(model)) return [];
      return [{
        id: modelId,
        name: model.name ?? modelId,
        context_window: Number(model.limit?.context ?? 0),
        max_tokens: Number(model.limit?.output ?? 0),
        reasoning: Boolean(model.reasoning),
        tool_calling: Boolean(model.tool_call),
        modalities: model.modalities?.input ?? ["text"],
        last_updated: model.last_updated ?? "",
      }];
    }).sort((a, b) => b.context_window - a.context_window || a.id.localeCompare(b.id));
    if (!models.length) continue;
    providers.push({
      id,
      name: provider.name ?? id,
      api,
      documentation: safeHttpsUrl(provider.doc ?? ""),
      status: "candidate",
      evidence: "models.dev lists zero unit price; free eligibility still requires verification",
      model_count: models.length,
      models: models.slice(0, 50),
    });
  }
  return providers.sort((a, b) => b.model_count - a.model_count || a.id.localeCompare(b.id)).slice(0, 100);
}

export async function fetchDiscovery(registeredProviders = [], fetcher = fetch) {
  const response = await fetcher(MODELS_DEV_URL, {
    headers: { "Accept": "application/json", "User-Agent": "open-free-router-cloudflare/0.2" },
  });
  if (!response.ok) throw new Error(`models.dev returned HTTP ${response.status}`);
  const providers = normalizeModelsDev(await response.json(), registeredProviders);
  return {
    schema_version: 1,
    generated_at: new Date().toISOString(),
    source: MODELS_DEV_URL,
    trust: "candidate-only; manual verification required before registry import",
    candidate_provider_count: providers.length,
    candidate_model_count: providers.reduce((sum, item) => sum + item.model_count, 0),
    providers,
  };
}
