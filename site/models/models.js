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
function latencyLabel(ms) { return ms == null ? "—" : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`; }

function flatten(catalog) {
  return catalog.providers.flatMap((provider) => provider.models.map((model) => ({
    ...model,
    provider_id: provider.id,
    provider_name: provider.name,
    provider_status: provider.availability,
    latency_ms: provider.latency_ms,
  })));
}

function renderProviders(catalog) {
  $("#provider-cards").innerHTML = catalog.providers.map((provider) => `
    <article class="provider-card ${provider.availability}">
      <div><span class="dot ${provider.availability}"></span><small>${statusLabel(provider.availability)}</small></div>
      <h3>${html(provider.name)}</h3>
      <p>${html(provider.reason)}</p>
      <footer><b>${provider.model_count}</b><span>模型</span><b>${latencyLabel(provider.latency_ms)}</b><span>最近延迟</span></footer>
    </article>`).join("");
}

function filteredRows() {
  const query = $("#search").value.trim().toLowerCase();
  const provider = $("#provider-filter").value;
  const status = $("#status-filter").value;
  const feature = $("#feature-filter").value;
  const sort = $("#sort").value;
  const rows = state.rows.filter((row) => {
    const haystack = `${row.provider_id} ${row.provider_name} ${row.id} ${row.name} ${row.upstream_id} ${row.codex_alias}`.toLowerCase();
    return (!query || haystack.includes(query)) &&
      (provider === "all" || row.provider_id === provider) &&
      (status === "all" || row.provider_status === status) &&
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
      <td><span class="status-badge ${row.provider_status}">${statusLabel(row.provider_status)}</span></td>
      <td><small>${html(row.provider_name)}</small><b>${html(row.name)}</b><code>${html(row.upstream_id)}</code></td>
      <td data-value="${row.context_window}">${formatTokens(row.context_window)}</td>
      <td data-value="${row.max_tokens}">${formatTokens(row.max_tokens)}</td>
      <td><span class="feature ${row.reasoning ? "yes" : "no"}">${row.reasoning ? "YES" : "—"}</span></td>
      <td><span class="feature ${row.tool_calling ? "yes" : "no"}">${row.tool_calling ? "YES" : "—"}</span></td>
      <td><div class="score"><i style="--score:${row.capability_score}%"></i><b>${row.capability_score}</b></div></td>
      <td>${latencyLabel(row.latency_ms)}</td>
      <td><code>${html(row.codex_alias)}</code></td>
    </tr>`).join("") : `<tr><td colspan="9" class="empty">没有符合当前筛选条件的模型。</td></tr>`;
}

function renderDiscovery(discovery) {
  $("#candidate-count").textContent = number.format(discovery.candidate_provider_count ?? 0);
  $("#discovered-providers").textContent = number.format(discovery.candidate_provider_count ?? 0);
  $("#discovered-models").textContent = number.format(discovery.candidate_model_count ?? 0);
  $("#discovery-time").textContent = `最近扫描：${formatTime(discovery.generated_at)}`;
  const providers = discovery.providers ?? [];
  $("#candidate-list").innerHTML = providers.length ? providers.slice(0, 30).map((provider) => `
    <details>
      <summary><div><span class="dot candidate"></span><b>${html(provider.name)}</b><small>${html(provider.api)}</small></div><em>${provider.model_count} models</em></summary>
      <div class="candidate-models">${provider.models.slice(0, 12).map((model) => `<span><b>${html(model.name)}</b><small>${formatTokens(model.context_window)} ctx · ${model.tool_calling ? "tools" : "text"}${model.reasoning ? " · reasoning" : ""}</small></span>`).join("")}</div>
    </details>`).join("") : `<p class="empty">尚未发现满足严格零价格与 HTTPS 条件的新候选。</p>`;
}

async function load() {
  let catalog;
  try {
    const response = await fetch("/api/catalog", { headers: { "Accept": "application/json" } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    catalog = await response.json();
  } catch {
    const response = await fetch("/data/catalog.json");
    catalog = await response.json();
    catalog.discovery = { candidate_provider_count: 0, candidate_model_count: 0, providers: [] };
  }
  state.catalog = catalog;
  state.rows = flatten(catalog);
  $("#provider-count").textContent = number.format(catalog.provider_count);
  $("#model-count").textContent = number.format(catalog.model_count);
  $("#available-count").textContent = number.format(catalog.providers.filter((item) => item.availability === "available").length);
  $("#status-time").textContent = `可用性快照：${formatTime(catalog.status_as_of)} · 状态不是 SLA`;
  $("#provider-filter").insertAdjacentHTML("beforeend", catalog.providers.map((item) => `<option value="${html(item.id)}">${html(item.name)}</option>`).join(""));
  renderProviders(catalog);
  renderDiscovery(catalog.discovery ?? { providers: [] });
  renderRows();
}

document.querySelectorAll(".filters input,.filters select").forEach((control) => control.addEventListener("input", renderRows));
$("#reset").addEventListener("click", () => {
  $("#search").value = "";
  ["#provider-filter", "#status-filter", "#feature-filter", "#sort"].forEach((id) => $(id).selectedIndex = 0);
  renderRows();
});
load().catch((error) => {
  $("#model-rows").innerHTML = `<tr><td colspan="9" class="empty">目录加载失败：${html(error.message)}</td></tr>`;
});
