const byId = (id) => document.getElementById(id);
const state = { status: null, providers: [], models: [], discovery: null };

const CLIENTS = [
  { id: 'codex', name: 'Codex', desc: '使用 Responses API 与隔离配置档，不影响原有 Codex 配置。', path: '~/.codex/open-free-router.config.toml' },
  { id: 'claude', name: 'Claude Code', desc: '通过 Anthropic Messages 兼容接口接入本地免费模型。', path: '~/.claude/settings.json' },
  { id: 'opencode', name: 'OpenCode', desc: '生成 OpenAI 兼容提供商及模型目录。', path: '~/.config/opencode/opencode.jsonc' },
  { id: 'kimi', name: 'Kimi CLI', desc: '写入独立提供商和模型别名，保留用户默认模型。', path: '~/.kimi/config.toml' },
  { id: 'openclaw', name: 'OpenClaw', desc: '同步本地路由提供商并维护可用主模型。', path: '~/.openclaw/openclaw.json' },
  { id: 'workbuddy', name: 'WorkBuddy', desc: '写入去重后的本地免费模型列表。', path: '~/.workbuddy/models.json' },
  { id: 'pi', name: 'Pi', desc: '同步统一模型目录，所有请求经本地路由。', path: '~/.pi/agent/models.json' },
  { id: 'omp', name: 'OMP', desc: '同步模型清单与本地代理提供商配置。', path: '~/.omp/agent/models.yml' },
  { id: 'hermes', name: 'Hermes', desc: '维护 OpenAI 兼容自定义提供商条目。', path: '~/.hermes/config.yaml' },
];

function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString('zh-CN');
}

function formatDate(value) {
  if (!value) return '暂无记录';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', { hour12: false });
}

function getAuthToken() {
  let token = sessionStorage.getItem('ofr_token');
  if (token) return token;
  token = prompt('请输入本地控制台令牌。可在 ~/.config/open-free-router/ui.token 或服务启动日志中查看：') || '';
  if (token) {
    sessionStorage.setItem('ofr_token', token);
    updateAuthButton();
  }
  return token;
}

function updateAuthButton() {
  const ready = Boolean(sessionStorage.getItem('ofr_token'));
  byId('auth-button').textContent = ready ? '写操作已解锁' : '解锁写操作';
  byId('auth-button').classList.toggle('primary', ready);
}

async function authFetch(path, options = {}) {
  const token = getAuthToken();
  if (!token) throw new Error('已取消写操作：未提供控制台令牌');
  const opts = { ...options, headers: { ...(options.headers || {}), Authorization: `Bearer ${token}` } };
  const response = await fetch(path, opts);
  if (response.status === 401) {
    sessionStorage.removeItem('ofr_token');
    updateAuthButton();
    throw new Error('控制台令牌无效，请重新解锁');
  }
  return response;
}

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((button) => button.classList.toggle('active', button.dataset.tab === name));
  document.querySelectorAll('.tab-content').forEach((section) => { section.hidden = section.id !== `tab-${name}`; });
  const loaders = { providers: loadProviders, models: loadModels, live: loadProbe, routing: loadRouting, radar: loadDiscovery, config: loadConfig };
  if (loaders[name]) loaders[name]();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

document.querySelectorAll('.tab').forEach((button) => button.addEventListener('click', () => switchTab(button.dataset.tab)));
document.querySelectorAll('.jump-tab').forEach((button) => button.addEventListener('click', () => switchTab(button.dataset.target)));
byId('auth-button').addEventListener('click', () => {
  if (sessionStorage.getItem('ofr_token')) {
    sessionStorage.removeItem('ofr_token');
    updateAuthButton();
  } else getAuthToken();
});

function metric(label, value, note, tone = '') {
  return `<div class="metric ${tone}"><div class="metric-label">${esc(label)}</div><div class="metric-value">${esc(value)}</div><div class="metric-note">${esc(note)}</div></div>`;
}

function endpointRow(name, value) {
  return `<div class="endpoint-row"><div class="endpoint-main"><div class="endpoint-name">${esc(name)}</div><div class="endpoint-value">${esc(value)}</div></div><button class="copy-button" data-copy="${esc(value)}">复制</button></div>`;
}

async function loadStatus() {
  try {
    const response = await fetch('/api/status');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    state.status = data;
    byId('service-state').textContent = '本地服务运行中';
    byId('service-state').closest('.service-pill').classList.add('online');
    byId('version-label').textContent = `版本 ${data.version || '未知'}`;
    renderOverview(data);
    renderProtocolGuides(data);
  } catch (error) {
    byId('service-state').textContent = '服务连接异常';
    showToast('action-status', `读取运行状态失败：${error.message}`, false);
  }
}

function renderOverview(data) {
  const summary = data.summary || {};
  byId('summary-cards').innerHTML = [
    metric('免费模型', formatNumber(summary.model_count), `来自 ${formatNumber(summary.provider_count)} 个提供商`, 'accent'),
    metric('凭据就绪', `${formatNumber(summary.credential_count)}/${formatNumber(summary.provider_count)}`, '只统计是否配置，不读取到页面'),
    metric('自动刷新', formatNumber(summary.auto_refresh_count), '按提供商能力更新模型目录'),
    metric('支持客户端', formatNumber((data.clients || []).length), '可选择同步到本机开发工具'),
  ].join('');

  const service = data.service || {};
  byId('service-endpoints').innerHTML = [
    endpointRow('OpenAI 兼容入口', service.proxy_url || '未配置'),
    endpointRow('Anthropic Messages', `${service.proxy_url || ''}/messages`.replace('/v1/v1/', '/v1/')),
    endpointRow('本地控制台', service.ui_url || window.location.origin),
    endpointRow('MCP 服务', 'open-free-router mcp'),
  ].join('');
  bindCopyButtons();

  const discovery = data.discovery || {};
  byId('automation-status').innerHTML = `
    <div class="automation-row"><span>模型目录刷新</span><span class="status-chip ok">运行周期由配置控制</span></div>
    <div class="automation-row"><span>候选发现</span><span class="status-chip ${discovery.enabled ? 'ok' : 'warn'}">${discovery.enabled ? `每 ${discovery.interval_hours} 小时` : '已关闭'}</span></div>
    <div class="automation-row"><span>候选自动实测</span><span class="status-chip ${discovery.auto_test ? 'ok' : 'warn'}">${discovery.auto_test ? '已开启' : '需人工启用'}</span></div>
    <div class="automation-row"><span>验证后自动接入</span><span class="status-chip ${discovery.auto_adopt ? 'ok' : 'warn'}">${discovery.auto_adopt ? '已开启' : '默认关闭'}</span></div>`;

  const providers = data.providers || [];
  byId('providers-overview').innerHTML = providers.length ? providers.map((provider) => `
    <div class="provider-row">
      <div><div class="provider-name">${esc(provider.name)}</div><div class="provider-prefix">路由前缀 ${esc(provider.prefix)}</div></div>
      <div class="provider-detail">${formatNumber(provider.model_count)} 个模型 · ${provider.auto_refresh ? '自动刷新' : '手动维护'}</div>
      <div><span class="status-chip ${provider.credential_configured ? 'ok' : 'warn'}">${provider.credential_configured ? '凭据已就绪' : '未配置凭据'}</span></div>
      <div class="provider-detail">${esc(provider.refresh_method || 'manual')}</div>
    </div>`).join('') : '<div class="empty">尚未配置提供商</div>';
}

function bindCopyButtons() {
  document.querySelectorAll('[data-copy]').forEach((button) => button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      const original = button.textContent;
      button.textContent = '已复制';
      setTimeout(() => { button.textContent = original; }, 1200);
    } catch (_) { button.textContent = '复制失败'; }
  }));
}

function showToast(id, message, ok = true) {
  byId(id).innerHTML = `<div class="toast ${ok ? 'ok' : 'bad'}">${esc(message)}</div>`;
}

async function refreshAll() {
  const button = byId('refresh-all');
  button.disabled = true;
  button.textContent = '正在刷新…';
  try {
    const response = await authFetch('/api/refresh', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || '刷新失败');
    const changed = Object.values(data.results || {}).filter(Boolean).length;
    showToast('action-status', `模型目录刷新完成，${changed} 个提供商发生变化。`);
    await Promise.all([loadStatus(), loadProviders(), loadModels()]);
  } catch (error) { showToast('action-status', error.message, false); }
  finally { button.disabled = false; button.textContent = '刷新全部模型'; }
}
byId('refresh-all').addEventListener('click', refreshAll);

async function loadProviders() {
  try {
    const response = await fetch('/api/providers');
    const data = await response.json();
    state.providers = data.providers || [];
    updateProviderSelectors();
    renderProviders();
  } catch (error) { byId('provider-list').innerHTML = `<div class="empty">读取失败：${esc(error.message)}</div>`; }
}

function credentialReady(provider) { return Boolean(provider.api_key); }

function renderProviders() {
  const query = byId('provider-search').value.trim().toLowerCase();
  const credential = byId('provider-credential-filter').value;
  const filtered = state.providers.filter((provider) => {
    const haystack = [provider.name, provider.prefix, ...(provider.models || []).map((model) => model.id)].join(' ').toLowerCase();
    const credentialMatch = credential === 'all' || (credential === 'ready' ? credentialReady(provider) : !credentialReady(provider));
    return haystack.includes(query) && credentialMatch;
  });
  byId('provider-count').textContent = `显示 ${filtered.length} / ${state.providers.length} 个提供商`;
  byId('provider-list').innerHTML = filtered.length ? filtered.map((provider) => `
    <article class="provider-card">
      <div class="provider-card-head">
        <div><h3>${esc(provider.name)}</h3><div class="provider-url">${esc(provider.upstream_url || provider.base_url || '未设置端点')}</div></div>
        <span class="status-chip ${credentialReady(provider) ? 'ok' : 'warn'}">${credentialReady(provider) ? '凭据已配置' : '缺少凭据'}</span>
      </div>
      <div class="provider-stats">
        <div class="provider-stat"><span>模型数量</span><strong>${formatNumber(provider.model_count)}</strong></div>
        <div class="provider-stat"><span>刷新方式</span><strong>${provider.auto_refresh ? '自动' : '手动'}</strong></div>
        <div class="provider-stat"><span>凭据来源</span><strong>${provider.api_key_env ? `$${esc(provider.api_key_env)}` : (credentialReady(provider) ? '本机注册表' : '未设置')}</strong></div>
      </div>
      <details><summary>查看 ${formatNumber(provider.model_count)} 个模型</summary><div class="mini-models">${(provider.models || []).map((model) => `<span class="mini-model">${esc(model.id)}</span>`).join('') || '<span class="mini-model">暂无模型</span>'}</div></details>
    </article>`).join('') : '<div class="empty">没有符合条件的提供商</div>';
}

byId('provider-search').addEventListener('input', renderProviders);
byId('provider-credential-filter').addEventListener('change', renderProviders);

const providerDialog = byId('provider-dialog');
byId('provider-add').addEventListener('click', () => providerDialog.showModal());
byId('provider-dialog-close').addEventListener('click', () => providerDialog.close());
byId('provider-cancel').addEventListener('click', () => providerDialog.close());
byId('provider-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const payload = {
    name: String(form.get('name') || '').trim(), prefix: String(form.get('prefix') || '').trim(),
    base_url: String(form.get('base_url') || '').trim(), upstream_url: String(form.get('upstream_url') || '').trim(),
    api_key_env: String(form.get('api_key_env') || '').trim(), api_key: String(form.get('api_key') || ''),
    models: String(form.get('models') || '').split(/\r?\n|,/).map((item) => item.trim()).filter(Boolean),
    auto_refresh: form.get('auto_refresh') === 'on',
  };
  try {
    const response = await authFetch('/api/providers', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '保存失败');
    providerDialog.close(); event.currentTarget.reset(); await Promise.all([loadProviders(), loadStatus(), loadModels()]);
  } catch (error) { alert(`保存提供商失败：${error.message}`); }
});

async function loadModels() {
  try {
    const response = await fetch('/api/models');
    const data = await response.json();
    state.models = Object.entries(data).flatMap(([provider, models]) => models.map((model) => ({ provider, ...model })));
    updateProviderSelectors();
    renderModels();
  } catch (error) { byId('models').innerHTML = `<div class="empty">读取失败：${esc(error.message)}</div>`; }
}

function updateProviderSelectors() {
  const names = [...new Set([...state.providers.map((provider) => provider.name), ...state.models.map((model) => model.provider)])].sort();
  const modelValue = byId('model-provider-filter').value;
  byId('model-provider-filter').innerHTML = '<option value="all">全部提供商</option>' + names.map((name) => `<option value="${esc(name)}">${esc(name)}</option>`).join('');
  if (names.includes(modelValue)) byId('model-provider-filter').value = modelValue;
  const probeValue = byId('probe-provider').value;
  byId('probe-provider').innerHTML = '<option value="">全部提供商</option>' + names.map((name) => `<option value="${esc(name)}">${esc(name)}</option>`).join('');
  if (names.includes(probeValue)) byId('probe-provider').value = probeValue;
}

function renderModels() {
  const query = byId('model-search').value.trim().toLowerCase();
  const provider = byId('model-provider-filter').value;
  const capability = byId('model-capability-filter').value;
  const filtered = state.models.filter((model) => {
    const textMatch = [model.id, model.name, model.upstream_id, model.provider].join(' ').toLowerCase().includes(query);
    const providerMatch = provider === 'all' || model.provider === provider;
    const capabilityMatch = capability === 'all' || (capability === 'reasoning' && model.reasoning) || (capability === 'tools' && model.tool_calling) || (capability === 'both' && model.reasoning && model.tool_calling);
    return textMatch && providerMatch && capabilityMatch;
  });
  byId('model-count').textContent = `显示 ${formatNumber(filtered.length)} / ${formatNumber(state.models.length)} 个模型`;
  const freeLabels = { verified: '免费证据有效', expired: '免费证据过期', unverified: '免费待核验', unknown: '免费条件未知', invalid: '免费证据错误' };
  byId('models').innerHTML = filtered.length ? filtered.map((model) => `
    <article class="model-card">
      <div class="model-provider">${esc(model.provider)}</div><div class="model-id">${esc(model.id)}</div>
      <div class="model-name">${esc(model.name || model.upstream_id || '免费模型')}</div>
      <div class="model-specs"><div class="model-spec"><span>上下文窗口</span><strong>${formatNumber(model.context_window || 131072)}</strong></div><div class="model-spec"><span>最大输出</span><strong>${formatNumber(model.max_tokens || 8192)}</strong></div></div>
      <div class="capabilities">${model.reasoning ? '<span class="cap-chip reasoning">推理增强</span>' : ''}${model.tool_calling ? '<span class="cap-chip tools">工具调用</span>' : ''}${!model.reasoning && !model.tool_calling ? '<span class="cap-chip">基础对话</span>' : ''}<span class="cap-chip ${model.free_tier_effective?.status === 'verified' ? 'free' : 'review'}">${esc(freeLabels[model.free_tier_effective?.status] || '免费条件未知')}</span>${model.free_tier_effective?.requires_payment_method ? '<span class="cap-chip review">需付款方式</span>' : ''}</div>
    </article>`).join('') : '<div class="empty">没有符合条件的模型</div>';
}
['model-search', 'model-provider-filter', 'model-capability-filter'].forEach((id) => byId(id).addEventListener(id === 'model-search' ? 'input' : 'change', renderModels));

const PROBE_LABELS = {
  no_key: '未配置凭据', no_endpoint: '未配置端点', network_error: '网络错误', error: '请求异常',
  http_400: '上游参数不兼容', http_401: '凭据无效', http_403: '无访问权限', http_404: '模型不存在',
  http_429: '频率或额度受限', http_500: '上游内部错误', http_502: '上游网关错误', http_503: '上游暂不可用', http_529: '上游过载', http_200: '可用',
};
let probeTimer = null;

function renderProbe(probe) {
  const results = Object.values(probe.results || {});
  const ok = results.filter((item) => item.ok).length;
  const noKey = results.filter((item) => item.status === 'no_key').length;
  const failed = results.length - ok - noKey;
  byId('probe-summary').innerHTML = [
    metric('实测可用', ok, '真实请求成功', 'accent'), metric('实测失败', failed, '需查看上游错误', failed ? 'danger' : ''),
    metric('待配置凭据', noKey, '未发起上游请求', noKey ? 'warning' : ''), metric('已有记录', results.length, `最近完成：${formatDate(probe.finished_at)}`),
  ].join('');
  const total = Number(probe.total || results.length || 0);
  const done = Number(probe.done || results.length || 0);
  const percent = total ? Math.min(100, Math.round(done / total * 100)) : 0;
  byId('probe-progress-bar').style.width = `${percent}%`;
  byId('probe-progress').textContent = probe.running ? `正在实测：${done} / ${total}（${percent}%）` : (probe.finished_at ? `最近完成：${formatDate(probe.finished_at)}` : '尚未开始实测');
  byId('probe-run').disabled = probe.running;
  byId('probe-run-selected').disabled = probe.running;
  if (!results.length) { byId('probe-results').innerHTML = '<div class="empty">尚无实测记录，点击“开始全量实测”。</div>'; return; }
  const groups = Object.groupBy ? Object.groupBy(results, (item) => item.provider) : results.reduce((acc, item) => ((acc[item.provider] ||= []).push(item), acc), {});
  byId('probe-results').innerHTML = Object.entries(groups).sort().map(([provider, rows]) => {
    const live = rows.filter((item) => item.ok).length;
    return `<div class="probe-provider"><h3>${esc(provider)}</h3><span class="status-chip ${live ? 'ok' : 'warn'}">${live} / ${rows.length} 可用</span></div><div class="probe-grid">${rows.sort((a,b) => a.model.localeCompare(b.model)).map((item) => {
      const tone = item.ok ? 'ok' : (item.status === 'no_key' ? 'warn' : 'bad');
      const label = item.ok ? `可用 · ${item.latency_ms ?? '—'} 毫秒` : (PROBE_LABELS[item.status] || item.status || '失败');
      return `<article class="probe-card"><div class="probe-card-line"><span class="probe-card-id">${esc(item.display_id)}</span><span class="status-chip ${tone}">${esc(label)}</span></div>${item.error ? `<div class="probe-reason">${esc(item.error)}</div>` : ''}</article>`;
    }).join('')}</div>`;
  }).join('');
}

async function loadProbe() {
  try {
    const response = await fetch('/api/probe');
    const probe = await response.json();
    renderProbe(probe);
    clearTimeout(probeTimer);
    probeTimer = setTimeout(loadProbe, probe.running ? 1800 : 60000);
  } catch (error) { byId('probe-progress').textContent = `读取实测状态失败：${error.message}`; }
}

async function runProbe(provider = '') {
  try {
    const response = await authFetch('/api/probe', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider }) });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.hint || data.error || '已有实测任务正在运行');
    byId('probe-progress').textContent = '实测任务已启动…';
    loadProbe();
  } catch (error) { byId('probe-progress').textContent = error.message; }
}
byId('probe-run').addEventListener('click', () => runProbe(''));
byId('probe-run-selected').addEventListener('click', () => runProbe(byId('probe-provider').value));

async function loadDiscovery() {
  try {
    const response = await fetch('/api/discovery');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '读取失败');
    state.discovery = data;
    renderDiscovery(data);
  } catch (error) { byId('discovery-list').innerHTML = `<div class="empty">读取候选快照失败：${esc(error.message)}</div>`; }
}

function renderDiscovery(data) {
  const providers = data.providers || [];
  const ready = providers.filter((provider) => provider.validation?.state === 'ready').length;
  const needsKey = providers.filter((provider) => provider.validation?.state === 'needs_credentials').length;
  byId('radar-summary').innerHTML = [
    metric('候选提供商', data.candidate_provider_count ?? providers.length, '尚未进入可信路由'),
    metric('候选免费模型', data.candidate_model_count ?? providers.reduce((sum, item) => sum + Number(item.model_count || 0), 0), '公开目录标记零价格', 'accent'),
    metric('实测就绪', ready, '通过带凭据的真实请求'), metric('等待专用凭据', needsKey, '设置候选专用环境变量', needsKey ? 'warning' : ''),
  ].join('');
  byId('discovery-list').innerHTML = providers.length ? providers.map((provider) => {
    const validation = provider.validation || {};
    const labels = { ready: '实测通过', needs_credentials: '等待凭据', failed: '实测失败', unsupported: '协议不支持', blocked: '安全策略拦截', not_tested: '尚未实测' };
    const tone = validation.state === 'ready' ? 'ok' : (validation.state === 'failed' || validation.state === 'blocked' ? 'bad' : 'warn');
    return `<article class="provider-card"><div class="provider-card-head"><div><h3>${esc(provider.name || provider.id)}</h3><div class="provider-url">${esc(provider.api)}</div></div><span class="status-chip ${tone}">${esc(labels[validation.state] || '待评审')}</span></div><div class="provider-stats"><div class="provider-stat"><span>免费模型</span><strong>${formatNumber(provider.model_count)}</strong></div><div class="provider-stat"><span>兼容协议</span><strong>${provider.protocol === 'openai-compatible' ? 'OpenAI 兼容' : '不支持'}</strong></div><div class="provider-stat"><span>专用凭据变量</span><strong>${esc(provider.credential_env || '未声明')}</strong></div></div><details><summary>查看候选模型</summary><div class="mini-models">${(provider.models || []).map((model) => `<span class="mini-model">${esc(model.id)}</span>`).join('')}</div></details></article>`;
  }).join('') : '<div class="empty">当前没有候选快照，可点击“立即扫描候选”。</div>';
}

byId('discovery-run').addEventListener('click', async () => {
  const button = byId('discovery-run'); button.disabled = true; button.textContent = '正在扫描…';
  try {
    const response = await authFetch('/api/discovery', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '扫描失败');
    await loadDiscovery();
  } catch (error) { alert(`候选扫描失败：${error.message}`); }
  finally { button.disabled = false; button.textContent = '立即扫描候选'; }
});

function renderClients() {
  byId('client-grid').innerHTML = CLIENTS.map((client) => `<label class="client-card"><input type="checkbox" value="${client.id}"><div class="client-card-head"><span class="client-name">${esc(client.name)}</span><span class="client-check">✓</span></div><div class="client-description">${esc(client.desc)}</div><div class="client-path">${esc(client.path)}</div></label>`).join('');
  document.querySelectorAll('.client-card input').forEach((input) => input.addEventListener('change', () => input.closest('.client-card').classList.toggle('selected', input.checked)));
}

function runtimeStateLabel(kind, item) {
  if (kind === 'provider') return item.state === 'open' ? '熔断中' : (item.state === 'half_open' ? '恢复探测' : '正常');
  if (kind === 'credential') return item.state === 'terminal' ? '已停用' : (item.state === 'cooldown' ? '冷却中' : '正常');
  return item.lockout_until ? '临时隔离' : '正常';
}

function runtimeRow(name, kind, item) {
  const bad = runtimeStateLabel(kind, item) !== '正常';
  const model = kind === 'model' ? name.slice(name.indexOf('/') + 1) : '';
  const provider = kind === 'model' ? name.slice(0, name.indexOf('/')) : name.split(':slot-')[0];
  return `<div class="runtime-row"><div><strong>${esc(name)}</strong><span>${esc(item.reason || (kind === 'provider' ? `${item.failures || 0} 次连续故障` : '运行时保护'))}</span></div><div class="runtime-actions"><span class="status-chip ${bad ? 'bad' : 'ok'}">${runtimeStateLabel(kind, item)}</span>${bad ? `<button class="text-button resilience-reset" data-provider="${esc(provider)}" data-model="${esc(model)}">重置</button>` : ''}</div></div>`;
}

function renderRouting(data) {
  const resilience = data.resilience || {};
  const routes = data.routes || {};
  const providers = Object.entries(resilience.providers || {});
  const credentials = Object.entries(resilience.credentials || {});
  const models = Object.entries(resilience.models || {});
  const active = providers.filter(([, item]) => item.state !== 'closed').length + credentials.filter(([, item]) => item.state !== 'ready').length + models.length;
  byId('routing-summary').innerHTML = [
    metric('保护状态', active, active ? '存在正在生效的隔离策略' : '当前没有隔离', active ? 'warning' : 'accent'),
    metric('提供商熔断', providers.filter(([, item]) => item.state !== 'closed').length, '上游整体故障保护'),
    metric('凭据槽受限', credentials.filter(([, item]) => item.state !== 'ready').length, '仅显示匿名槽位'),
    metric('最近决策', routes.total || 0, `内存最多保留 ${routes.max_entries || 0} 条`),
  ].join('');
  const rows = [
    ...providers.map(([name, item]) => runtimeRow(name, 'provider', item)),
    ...credentials.map(([name, item]) => runtimeRow(name, 'credential', item)),
    ...models.map(([name, item]) => runtimeRow(name, 'model', item)),
  ];
  byId('resilience-list').innerHTML = rows.join('') || '<div class="empty">当前没有熔断、冷却或模型隔离。</div>';
  byId('route-list').innerHTML = (routes.items || []).map((item) => `<div class="runtime-row route-row"><div><strong>${esc(item.requested_model)}</strong><span>${esc(item.request_id)} · ${formatDate(Number(item.timestamp || 0) * 1000)}</span></div><div class="route-result"><span class="status-chip ${item.status === 'success' ? 'ok' : 'bad'}">${item.status === 'success' ? '成功' : `失败 ${item.status_code || ''}`}</span><span>${esc(item.provider || '未选中')} / ${esc(item.model || '—')} · ${item.attempts || 0} 次尝试</span></div></div>`).join('') || '<div class="empty">尚无路由决策；通过代理发送请求后会显示在这里。</div>';
  document.querySelectorAll('.resilience-reset').forEach((button) => button.addEventListener('click', () => resetResilience(button.dataset.provider, button.dataset.model)));
}

async function loadRouting() {
  try {
    const response = await fetch('/api/routing');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '读取失败');
    renderRouting(data);
  } catch (error) {
    byId('resilience-list').innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    byId('route-list').innerHTML = '<div class="empty">请确认本地代理已启动。</div>';
  }
}

async function resetResilience(provider, model = '') {
  try {
    const response = await authFetch('/api/routing/reset', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider, model }) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '重置失败');
    showToast('routing-status', `已重置 ${provider}${model ? ` / ${model}` : ''} 的保护状态。`);
    await loadRouting();
  } catch (error) { showToast('routing-status', error.message, false); }
}
byId('routing-refresh').addEventListener('click', loadRouting);

function renderProtocolGuides(data) {
  const url = data.service?.proxy_url || 'http://127.0.0.1:8337/v1';
  byId('protocol-guides').innerHTML = `
    <div class="protocol-item"><strong>OpenAI Chat Completions</strong><code>${esc(url)}/chat/completions</code></div>
    <div class="protocol-item"><strong>OpenAI Responses</strong><code>${esc(url)}/responses</code></div>
    <div class="protocol-item"><strong>Anthropic Messages</strong><code>${esc(url)}/messages</code></div>
    <div class="protocol-item"><strong>MCP 标准输入输出</strong><code>open-free-router mcp</code></div>`;
}

byId('sync-selected').addEventListener('click', async () => {
  const agents = [...document.querySelectorAll('.client-card input:checked')].map((input) => input.value);
  if (!agents.length) { showToast('sync-status', '请先选择至少一个客户端。', false); return; }
  const button = byId('sync-selected'); button.disabled = true; button.textContent = '正在同步…';
  try {
    const response = await authFetch('/api/sync', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ agents }) });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || '同步失败');
    const errors = Object.entries(data.results || {}).filter(([, changes]) => (changes || []).some((item) => String(item).startsWith('ERROR:')));
    showToast('sync-status', errors.length ? `同步完成，但 ${errors.map(([name]) => name).join('、')} 返回错误，请检查本机配置。` : `已同步 ${agents.length} 个客户端，请重启对应客户端。`, !errors.length);
  } catch (error) { showToast('sync-status', error.message, false); }
  finally { button.disabled = false; button.textContent = '同步所选客户端'; }
});

async function loadConfig() {
  try {
    const response = await fetch('/api/config');
    const data = await response.json();
    byId('config-editor').value = data.yaml || '';
    byId('config-status').textContent = '';
  } catch (error) { byId('config-status').textContent = `读取失败：${error.message}`; }
}

byId('config-save').addEventListener('click', async () => {
  try {
    const response = await authFetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ yaml: byId('config-editor').value }) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '保存失败');
    byId('config-status').textContent = `已保存 · ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}，部分设置需重启服务`;
  } catch (error) { byId('config-status').textContent = `保存失败：${error.message}`; }
});
byId('config-reload').addEventListener('click', loadConfig);

renderClients();
updateAuthButton();
Promise.all([loadStatus(), loadProviders(), loadModels(), loadProbe()]);
setInterval(loadStatus, 30000);
