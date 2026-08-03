const state = { token: "", models: [] };
const $ = (selector) => document.querySelector(selector);
const number = new Intl.NumberFormat("zh-CN");

function html(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}

function metric(value, digits = 1) {
  return value == null ? '<span class="missing">未测</span>' : `<span class="metric">${Number(value).toFixed(digits)}</span>`;
}

function money(value) {
  if (value == null) return '<span class="missing">未测</span>';
  return `<span class="metric">$${Number(value).toFixed(value < .1 ? 3 : 2)}</span>`;
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { Accept: "application/json", Authorization: `Bearer ${state.token}`, ...(options.headers || {}) },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(response.status === 401 ? "访问令牌不正确" : payload.message || payload.error || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function filteredModels() {
  const query = $("#model-search").value.trim().toLowerCase();
  const rows = state.models.filter((model) => `${model.name} ${model.slug} ${model.creator} ${model.registry_matches.join(" ")}`.toLowerCase().includes(query));
  const sorters = {
    intelligence: (a, b) => (b.intelligence ?? -Infinity) - (a.intelligence ?? -Infinity),
    coding: (a, b) => (b.coding ?? -Infinity) - (a.coding ?? -Infinity),
    agentic: (a, b) => (b.agentic ?? -Infinity) - (a.agentic ?? -Infinity),
    cost: (a, b) => (a.cost_per_task_usd ?? Infinity) - (b.cost_per_task_usd ?? Infinity),
    speed: (a, b) => (b.median_output_tokens_per_second ?? -Infinity) - (a.median_output_tokens_per_second ?? -Infinity),
  };
  return rows.sort(sorters[$("#model-sort").value]);
}

function renderRows() {
  const rows = filteredModels();
  $("#result-count").textContent = `${number.format(rows.length)} 条结果`;
  $("#model-rows").innerHTML = rows.length ? rows.map((model) => `<tr>
    <td><b>${html(model.name)}</b><code>${html(model.slug)}</code><small>${html(model.release_date || "发布日期未提供")}</small></td>
    <td>${html(model.creator || "—")}</td><td>${metric(model.intelligence)}</td><td>${metric(model.coding)}</td><td>${metric(model.agentic)}</td>
    <td>${money(model.cost_per_task_usd)}</td><td>${money(model.input_price_per_million_usd)}<small>输入 / 1M</small>${money(model.output_price_per_million_usd)}<small>输出 / 1M</small></td>
    <td>${metric(model.median_output_tokens_per_second)}<small>tokens/s</small></td><td>${metric(model.median_time_to_first_token_seconds, 2)}<small>秒</small></td>
    <td>${model.registry_matches.length ? `<span class="match">${html(model.match_type === "exact" ? "精确匹配" : "模型族匹配")}</span><small>${html(model.registry_matches.join("、"))}</small>` : '<span class="missing">未匹配</span>'}</td>
  </tr>`).join("") : '<tr><td colspan="10">没有符合条件的记录。</td></tr>';
}

function renderSnapshot(snapshot) {
  state.models = Array.isArray(snapshot.models) ? snapshot.models : [];
  $("#model-count").textContent = number.format(snapshot.model_count || state.models.length);
  $("#matched-count").textContent = number.format(snapshot.matched_model_count || 0);
  $("#index-version").textContent = snapshot.source?.index_version ? `v${snapshot.source.index_version}` : "—";
  $("#generated-at").textContent = snapshot.generated_at ? new Date(snapshot.generated_at).toLocaleString("zh-CN", { hour12: false }) : "—";
  renderRows();
}

async function unlock(token) {
  state.token = token;
  const snapshot = await request("/api/internal/benchmarks");
  sessionStorage.setItem("ofrInternalBenchmarkToken", token);
  $("#access-panel").hidden = true;
  $("#dashboard").hidden = false;
  renderSnapshot(snapshot);
}

$("#access-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = $("#access-error");
  error.hidden = true;
  try { await unlock($("#access-token").value); }
  catch (caught) { error.textContent = caught.message; error.hidden = false; }
});

$("#refresh-button").addEventListener("click", async () => {
  const button = $("#refresh-button");
  button.disabled = true;
  button.textContent = "正在刷新…";
  try { renderSnapshot(await request("/api/internal/benchmarks/refresh", { method: "POST" })); }
  catch (error) { window.alert(`刷新失败：${error.message}`); }
  finally { button.disabled = false; button.textContent = "从官方 API 刷新"; }
});

$("#lock-button").addEventListener("click", () => {
  sessionStorage.removeItem("ofrInternalBenchmarkToken");
  state.token = ""; state.models = [];
  $("#dashboard").hidden = true; $("#access-panel").hidden = false; $("#access-token").value = "";
});

$("#model-search").addEventListener("input", renderRows);
$("#model-sort").addEventListener("input", renderRows);

const savedToken = sessionStorage.getItem("ofrInternalBenchmarkToken");
if (savedToken) {
  unlock(savedToken).catch((error) => {
    // Only an actual rejection means the token is bad. A 503 while the
    // snapshot is still being built used to silently discard a valid token
    // and drop the user back to the login form with no explanation.
    if (error?.status === 401) {
      sessionStorage.removeItem("ofrInternalBenchmarkToken");
    }
    const banner = $("#access-error");
    if (banner) {
      banner.textContent = error?.status === 401
        ? "访问令牌不正确，请重新输入。"
        : `暂时无法载入内部数据：${error?.message || error}`;
      banner.hidden = false;
    }
  });
}
