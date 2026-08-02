const state = { rows: [], external: new Map() };
const $ = (selector) => document.querySelector(selector);
const number = new Intl.NumberFormat("zh-CN");

function html(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}

function freeStatus(row) { return row.free_tier?.status || "unknown"; }
function evidencePoints(row) {
  return ({ verified: 25, unverified: 10, expired: 5, invalid: 0, unknown: 5 })[freeStatus(row)] ?? 5;
}
function availabilityPoints(status) { return ({ available: 35, unverified: 12, unavailable: 0 })[status] ?? 12; }
function latencyPoints(ms) {
  if (ms == null) return 2;
  if (ms <= 1000) return 10;
  if (ms <= 1500) return 9;
  if (ms <= 3000) return 6;
  if (ms <= 5000) return 3;
  return 1;
}
function readiness(row) {
  return Math.round(Math.min(100, availabilityPoints(row.provider_status) + evidencePoints(row) + row.capability_score * .3 + latencyPoints(row.latency_ms)));
}
function formatLatency(ms) { return ms == null ? "未测" : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`; }
function formatTokens(value) { return value >= 1_000_000 ? `${(value / 1_000_000).toFixed(1)}M` : value >= 1000 ? `${Math.round(value / 1000)}K` : String(value || "—"); }
function externalKey(providerId, modelId) { return `${providerId}/${modelId}`.toLowerCase(); }
function formatUsd(value) { return value == null ? "—" : `$${Number(value).toFixed(value < 0.1 ? 3 : 2)}`; }

function flatten(catalog) {
  return catalog.providers.flatMap((provider) => provider.models.map((model) => {
    const row = { ...model, provider_id: provider.id, provider_name: provider.name, provider_status: provider.availability, latency_ms: provider.latency_ms };
    row.readiness = readiness(row);
    row.external = state.external.get(externalKey(provider.id, model.id)) || state.external.get(externalKey(provider.id, model.upstream_id));
    return row;
  }));
}

function renderScatter() {
  const rows = [...state.rows].sort((a, b) => b.readiness - a.readiness || (a.latency_ms ?? 99999) - (b.latency_ms ?? 99999)).slice(0, 30);
  const xPosition = (ms) => {
    const clamped = Math.max(300, Math.min(8000, ms ?? 8000));
    return 100 - ((Math.log(clamped) - Math.log(300)) / (Math.log(8000) - Math.log(300))) * 92;
  };
  $("#scatter").innerHTML = '<div class="gridline y25"></div><div class="gridline y50"></div><div class="gridline y75"></div>' + rows.map((row) => {
    const label = row.name || row.id;
    const title = `${label} · 任务适配 ${row.readiness} · ${formatLatency(row.latency_ms)}${row.external ? ` · AA Intelligence ${row.external.intelligence ?? "—"}` : ""}`;
    return `<button class="point ${html(row.provider_status)} ${row.external ? "external-match" : ""}" style="left:${xPosition(row.latency_ms).toFixed(2)}%;bottom:${row.readiness}%" title="${html(title)}" aria-label="${html(title)}"><span>${html(label)}</span></button>`;
  }).join("");
}

function filteredRows() {
  const query = $("#bench-search").value.trim().toLowerCase();
  const rows = state.rows.filter((row) => `${row.id} ${row.upstream_id} ${row.provider_name} ${row.family_zh}`.toLowerCase().includes(query));
  const sorters = {
    readiness: (a, b) => b.readiness - a.readiness,
    latency: (a, b) => (a.latency_ms ?? Infinity) - (b.latency_ms ?? Infinity),
    intelligence: (a, b) => (b.external?.intelligence ?? -Infinity) - (a.external?.intelligence ?? -Infinity),
    context: (a, b) => b.context_window - a.context_window,
  };
  return rows.sort(sorters[$("#bench-sort").value]);
}

function renderRows() {
  const labels = { available: "可用", unverified: "未验证", unavailable: "不可用" };
  const evidence = { verified: "已核验", unverified: "待补证据", expired: "证据过期", invalid: "证据错误", unknown: "条件未知" };
  const rows = filteredRows();
  $("#bench-rows").innerHTML = rows.length ? rows.map((row) => `<tr>
    <td><span class="status-pill ${html(row.provider_status)}">${labels[row.provider_status] || "未验证"}</span></td>
    <td><small>${html(row.provider_name)}</small><b>${html(row.name || row.id)}</b><code>${html(row.upstream_id)}</code></td>
    <td><div class="scorebar"><i style="--score:${row.readiness}%"></i><b>${row.readiness}</b></div></td>
    <td>${formatLatency(row.latency_ms)}</td><td>${evidence[freeStatus(row)] || "条件未知"}</td>
    <td><div class="scorebar"><i style="--score:${row.capability_score}%"></i><b>${row.capability_score}</b></div><small>${formatTokens(row.context_window)} ctx</small></td>
    <td>${row.external?.intelligence ?? "—"}</td><td>${row.external?.coding ?? "—"}</td><td>${row.external?.agentic ?? "—"}</td><td>${formatUsd(row.external?.cost_per_task_usd)}</td>
    <td>${row.external ? `<b>${html(row.external.match_label || "模型族匹配")}</b><small>${html(row.external.source_name || "Artificial Analysis")}</small>` : "—"}</td>
  </tr>`).join("") : '<tr><td colspan="11">没有符合搜索条件的模型。</td></tr>';
}

function renderExternalScatter(models) {
  const rows = models.filter((item) => item.registry_matches?.length && item.intelligence != null && item.cost_per_task_usd > 0).slice(0, 40);
  if (!rows.length) return;
  const x = (cost) => Math.max(3, Math.min(97, ((Math.log(cost) - Math.log(.005)) / (Math.log(5) - Math.log(.005))) * 94 + 3));
  const y = (score) => Math.max(4, Math.min(96, ((score - 5) / 60) * 92 + 4));
  $("#external-scatter").innerHTML = '<div class="external-grid g25"></div><div class="external-grid g50"></div><div class="external-grid g75"></div>' + rows.map((item) => {
    const title = `${item.name} · Intelligence ${item.intelligence} · ${formatUsd(item.cost_per_task_usd)}/task`;
    return `<button class="external-point" style="left:${x(item.cost_per_task_usd).toFixed(2)}%;bottom:${y(item.intelligence).toFixed(2)}%" title="${html(title)}" aria-label="${html(title)}"><span>${html(item.name)}</span></button>`;
  }).join("");
  $("#external-chart").hidden = false;
}

function indexExternal(snapshot) {
  const source = snapshot.sources?.find((item) => item.id === "artificial-analysis");
  for (const item of source?.models || []) {
    for (const match of item.registry_matches || []) state.external.set(String(match).toLowerCase(), { ...item, source_name: source.name });
  }
  const count = state.external.size;
  $("#external-models").textContent = number.format(count);
  $("#index-version").textContent = source?.index_version ? `v${source.index_version}` : "待导入";
  if (count) {
    $("#external-status").classList.add("ready");
    $("#external-status").innerHTML = `<b>已导入 ${number.format(source.models.length)} 条 Artificial Analysis 记录</b><p>Index v${html(source.index_version)} · 最近导入 ${html(source.last_imported_at || "未知")} · 页面已按要求署名来源。</p>`;
    renderExternalScatter(source.models);
  }
}

async function load() {
  const [catalogResponse, benchmarkResponse] = await Promise.all([
    fetch("/api/catalog", { headers: { Accept: "application/json" } }).then((response) => response.ok ? response.json() : fetch("/data/catalog.json").then((fallback) => fallback.json())).catch(() => fetch("/data/catalog.json").then((response) => response.json())),
    fetch("/data/benchmarks.json?v=20260802a").then((response) => response.ok ? response.json() : Promise.reject(new Error(`评测快照 HTTP ${response.status}`))),
  ]);
  indexExternal(benchmarkResponse);
  state.rows = flatten(catalogResponse);
  $("#evaluated-models").textContent = number.format(state.rows.length);
  $("#live-models").textContent = number.format(state.rows.filter((row) => row.provider_status === "available").length);
  renderScatter(); renderRows();
}

$("#bench-search").addEventListener("input", renderRows);
$("#bench-sort").addEventListener("input", renderRows);
load().catch((error) => { $("#scatter").innerHTML = `<p class="scatter-loading">评测数据加载失败：${html(error.message)}</p>`; $("#bench-rows").innerHTML = '<tr><td colspan="11">评测数据加载失败。</td></tr>'; });
