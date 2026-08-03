const state = { catalog: null, rows: [] };
const $ = (selector) => document.querySelector(selector);
const number = new Intl.NumberFormat("zh-CN");

function html(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  })[character]);
}

function externalUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" ? html(url.href) : "#";
  } catch {
    return "#";
  }
}

function formatTokens(value) {
  if (!value) return "—";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value % 1_000_000 ? 1 : 0)}M`;
  if (value >= 1000) return `${Math.round(value / 1000)}K`;
  return number.format(value);
}

function formatTime(value) {
  if (!value) return "未知";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function statusLabel(status) { return status === "available" ? "可用" : status === "unavailable" ? "不可用" : "待验证"; }
function freeStatus(value) { return value?.status || "unknown"; }
function freeLabel(value) {
  return ({ verified: "已核验免费", expired: "证据过期", unverified: "待补证据", invalid: "证据错误", unknown: "条件未知" })[freeStatus(value)] || "条件未知";
}
function latencyLabel(ms) { return ms == null ? "—" : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`; }

function flatten(catalog) {
  return catalog.providers.flatMap((provider) => provider.models.map((model) => ({
    ...model,
    provider_id: provider.id,
    provider_name: provider.name,
    /* provider_availability is kept for the provider cards only. Rows used to
       take the provider's verdict and the provider's Math.min latency, so a
       12-second timeout rendered as ~1.2s and 6 of 55 badges contradicted their
       own evidence. The merged catalog carries genuine per-model values. */
    provider_availability: provider.availability,
    model_status: model.availability ?? "unverified",
    latency_ms: model.latency_ms ?? null,
  })));
}

function renderProviders(catalog) {
  $("#provider-cards").innerHTML = catalog.providers.map((provider) => `
    <article class="provider-card ${provider.availability}">
      <div><span class="dot ${provider.availability}"></span><small>${statusLabel(provider.availability)}</small></div>
      <h3>${html(provider.profile?.zh_name || provider.name)}</h3>
      <p class="provider-org">${html(provider.profile?.organization || provider.name)} · ${html(provider.profile?.region || "")}</p>
      <p>${html(provider.profile?.background_zh || provider.reason)}</p>
      <div class="provider-tags">${(provider.profile?.strengths_zh || []).map((item) => `<span>${html(item)}</span>`).join("")}</div>
      <p class="provider-caution">${html(provider.profile?.cautions_zh || provider.reason)}</p>
      ${provider.profile?.official_url ? `<a class="provider-source" href="${externalUrl(provider.profile.official_url)}" rel="noreferrer">官方资料 ↗</a>` : ""}
      <p class="free-evidence ${freeStatus(provider.free_tier)}">免费证据：${freeLabel(provider.free_tier)}${provider.free_tier?.expires_at ? ` · 有效至 ${html(provider.free_tier.expires_at.slice(0, 10))}` : ""}</p>
      ${renderKeyGuide(provider.profile)}
      <footer><b>${provider.model_count}</b><span>模型</span><b>${latencyLabel(provider.latency_ms)}</b><span>最近延迟</span></footer>
    </article>`).join("");
}

function renderKeyGuide(profile) {
  if (!profile?.key_url) return "";
  const steps = (profile.key_steps_zh || []).map((step) => `<li>${html(step)}</li>`).join("");
  return `
    <details class="key-guide">
      <summary>🔑 如何获取 API Key</summary>
      ${steps ? `<ol>${steps}</ol>` : ""}
      ${profile.free_quota_zh ? `<p class="key-quota">${html(profile.free_quota_zh)}</p>` : ""}
      <a href="${externalUrl(profile.key_url)}" rel="noreferrer">前往密钥控制台 ↗</a>
    </details>`;
}

function filteredRows() {
  const query = $("#search").value.trim().toLowerCase();
  const provider = $("#provider-filter").value;
  const status = $("#status-filter").value;
  const feature = $("#feature-filter").value;
  const free = $("#free-filter").value;
  const sort = $("#sort").value;
  const rows = state.rows.filter((row) => {
    const haystack = `${row.provider_id} ${row.provider_name} ${row.id} ${row.name} ${row.upstream_id} ${row.codex_alias}`.toLowerCase();
    return (!query || haystack.includes(query)) &&
      (provider === "all" || row.provider_id === provider) &&
      (status === "all" || row.model_status === status) &&
      (free === "all" || (free === "verified" && freeStatus(row.free_tier) === "verified") || (free === "review" && ["expired", "unverified", "invalid"].includes(freeStatus(row.free_tier))) || (free === "unknown" && freeStatus(row.free_tier) === "unknown")) &&
      (feature === "all" || (feature === "tools" && row.tool_calling) || (feature === "reasoning" && row.reasoning));
  });
  const sorters = {
    provider: (a, b) => a.provider_id.localeCompare(b.provider_id) || a.id.localeCompare(b.id),
    score: (a, b) => b.capability_score - a.capability_score || b.context_window - a.context_window,
    context: (a, b) => b.context_window - a.context_window,
    latency: (a, b) => (a.latency_ms ?? Infinity) - (b.latency_ms ?? Infinity),
  };
  return rows.sort(sorters[sort]);
}

function renderRows() {
  const rows = filteredRows();
  $("#result-count").textContent = number.format(rows.length);
  $("#model-rows").innerHTML = rows.length ? rows.map((row) => `
    <tr>
      <td><span class="status-badge ${row.model_status}" title="${html(row.reason || "")}">${statusLabel(row.model_status)}</span></td>
      <td><small>${html(row.provider_name)}</small><b>${html(row.name)}</b><code>${html(row.upstream_id)}</code></td>
      <td class="model-description"><b>${html(row.family_zh)}</b><span>${html(row.description_zh)}</span><em>适合：${html(row.recommended_for_zh)}</em><small>${html(row.speed_tier_zh)} · ${html(row.benchmark_note_zh)}</small></td>
      <td><span class="status-badge free-${freeStatus(row.free_tier)}">${freeLabel(row.free_tier)}</span>${row.free_tier?.type && row.free_tier.type !== "unknown" ? `<code>${html(row.free_tier.type)}</code>` : ""}${row.free_tier?.requires_payment_method ? '<small>需要付款方式</small>' : ''}${row.free_tier?.evidence_url ? `<a class="provider-source" href="${externalUrl(row.free_tier.evidence_url)}" rel="noreferrer">证据 ↗</a>` : ""}</td>
      <td data-value="${row.context_window}">${formatTokens(row.context_window)}</td>
      <td data-value="${row.max_tokens}">${formatTokens(row.max_tokens)}</td>
      <td><span class="feature ${row.reasoning ? "yes" : "no"}">${row.reasoning ? "YES" : "—"}</span></td>
      <td><span class="feature ${row.tool_calling ? "yes" : "no"}">${row.tool_calling ? "YES" : "—"}</span></td>
      <td><div class="score"><i data-score="${row.capability_score}"></i><b>${row.capability_score}</b></div></td>
      <td>${latencyLabel(row.latency_ms)}</td>
      <td><code>${html(row.codex_alias)}</code></td>
    </tr>`).join("") : `<tr><td colspan="11" class="empty">没有符合当前筛选条件的模型。</td></tr>`;
  paintScoreBars();
}

/**
 * Width of the capability bars.
 *
 * This has to go through the CSSOM rather than a `style="--score:…"`
 * attribute in the markup above: the site ships `style-src 'self'` with no
 * `'unsafe-inline'`, which blocks style *attributes* — the bars rendered at
 * zero width in production. Programmatic setProperty is not covered by the
 * directive, so the value applies normally.
 */
function paintScoreBars() {
  for (const bar of document.querySelectorAll("#model-rows .score i[data-score]")) {
    const score = Number(bar.dataset.score);
    bar.style.setProperty("--score", `${Number.isFinite(score) ? score : 0}%`);
  }
}

function renderDiscovery(discovery) {
  $("#candidate-count").textContent = number.format(discovery.candidate_provider_count ?? 0);
  $("#discovered-providers").textContent = number.format(discovery.candidate_provider_count ?? 0);
  $("#discovered-models").textContent = number.format(discovery.candidate_model_count ?? 0);
  $("#discovery-time").textContent = `最近扫描：${formatTime(discovery.generated_at)}`;
  const providers = discovery.providers ?? [];
  $("#candidate-list").innerHTML = providers.length ? providers.slice(0, 30).map((provider) => `
    <details>
      <summary><div><span class="dot candidate"></span><b>${html(provider.name)}</b><small>${html(provider.api)} · ${html(provider.protocol || "协议待识别")} · 专用变量 ${html(provider.credential_env || "未生成")}</small></div><em>${provider.model_count} models</em></summary>
      <div class="candidate-models">${provider.models.slice(0, 12).map((model) => `<span><b>${html(model.name)}</b><small>${formatTokens(model.context_window)} ctx · ${model.tool_calling ? "tools" : "text"}${model.reasoning ? " · reasoning" : ""}</small></span>`).join("")}</div>
    </details>`).join("") : `<p class="empty">尚未发现满足严格零价格与 HTTPS 条件的新候选。</p>`;
}

function relativeDays(iso) {
  const parsed = Date.parse(iso ?? "");
  if (!Number.isFinite(parsed)) return "时间未知";
  const days = Math.floor(Math.max(0, Date.now() - parsed) / 86400000);
  if (days >= 1) return `${days} 天前`;
  const hours = Math.floor(Math.max(0, Date.now() - parsed) / 3600000);
  return hours >= 1 ? `${hours} 小时前` : "不到 1 小时前";
}

/* The static snapshot carries whatever availability was true when it was
   exported. Rendering it as current fact is the whole defect this page had:
   nine green provider dots, a numeric 最近可用 counter and per-row 可用 badges,
   with nothing telling the visitor none of it was measured just now. This runs
   unconditionally on the fallback branch — that branch is by definition the one
   that knows /api/catalog did not answer — so there is no age threshold here to
   drift out of step with the build gate.

   Deliberately duplicated with status.js's metadataOnly() and benchmarks.js's
   copy rather than shared: build-site.mjs hashes each JS file before rewriting
   references inside it, and _headers marks /*.js immutable for a year, so a
   JS→JS import would leave cached visitors fetching a renamed dependency — a
   hard 404 that kills the page. build-site.mjs asserts all three copies exist. */
function degradeCatalog(catalog) {
  const blank = (entry) => ({
    ...entry,
    availability: "unverified",
    latency_ms: null,
    checked_at: "",
    reason: "静态兜底数据，未做可用性判断",
    /* Exported from the provider's numbers; without evidence it is not a speed
       claim we can stand behind. Mirrors worker/probe.js speedTier. */
    speed_tier_zh: "未测",
  });
  return {
    ...catalog,
    status_as_of: "",
    providers: (catalog.providers || []).map((provider) => ({
      ...blank(provider),
      models: (provider.models || []).map(blank),
    })),
  };
}

async function load() {
  let catalog;
  let degraded = false;
  try {
    const response = await fetch("/api/catalog", { headers: { "Accept": "application/json" } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    catalog = await response.json();
  } catch {
    const response = await fetch("/data/catalog.json");
    catalog = degradeCatalog(await response.json());
    degraded = true;
    // discovery-seed.json is already deployed alongside the catalog; using it
    // keeps the radar's candidate section populated when /api/catalog is down
    // instead of claiming zero candidates.
    catalog.discovery = await fetch("/data/discovery-seed.json")
      .then((seed) => (seed.ok ? seed.json() : null))
      .catch(() => null)
      ?? { candidate_provider_count: 0, candidate_model_count: 0, providers: [] };
  }
  state.catalog = catalog;
  state.rows = flatten(catalog);
  $("#provider-count").textContent = number.format(catalog.provider_count);
  $("#model-count").textContent = number.format(catalog.model_count);
  const availableCount = $("#available-count");
  // 未验证 is three CJK glyphs in a slot typeset for two or three digits at up
  // to 3.8rem, so the qualitative class drops the size instead of overflowing.
  availableCount.classList.toggle("qualitative", degraded);
  availableCount.textContent = degraded
    ? "未验证"
    : number.format(catalog.providers.filter((item) => item.availability === "available").length);
  $("#status-time").textContent = degraded
    ? `静态兜底数据 · 目录生成于 ${relativeDays(catalog.generated_at)} · 本页未做可用性判断`
    : `可用性快照：${formatTime(catalog.status_as_of)} · 状态不是 SLA`;
  $("#provider-filter").insertAdjacentHTML("beforeend", catalog.providers.map((item) => `<option value="${html(item.id)}">${html(item.name)}</option>`).join(""));
  renderProviders(catalog);
  renderDiscovery(catalog.discovery ?? { providers: [] });
  renderRows();
}

document.querySelectorAll(".filters input,.filters select").forEach((control) => control.addEventListener("input", renderRows));
$("#reset").addEventListener("click", () => {
  $("#search").value = "";
  ["#provider-filter", "#status-filter", "#free-filter", "#feature-filter", "#sort"].forEach((id) => $(id).selectedIndex = 0);
  renderRows();
});
load().catch((error) => {
  // Every region that would otherwise sit in its loading skeleton forever
  // has to be told the load failed, not just the table.
  const reason = html(error?.message || error || "未知错误");
  $("#model-rows").innerHTML = `<tr><td colspan="11" class="empty">目录加载失败：${reason}</td></tr>`;
  $("#provider-cards").innerHTML = `<p class="empty">提供商信息加载失败：${reason}</p>`;
  $("#candidate-list").innerHTML = `<p class="empty">发现队列加载失败：${reason}</p>`;
  $("#status-time").textContent = "目录加载失败，以下数据可能不可用";
  ["#provider-count", "#model-count", "#available-count", "#candidate-count"].forEach((id) => {
    const el = $(id);
    if (el) el.textContent = "—";
  });
});
