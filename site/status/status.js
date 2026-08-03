/* 服务端可用状态页：拉取 Cloudflare 探测快照，60 秒刷新展示 */
(() => {
  "use strict";

  const REFRESH_SECONDS = 60;
  const state = { catalog: null, secondsLeft: REFRESH_SECONDS, loading: false, lastLoadedAt: 0, paused: false };
  const $ = (selector) => document.querySelector(selector);
  const number = new Intl.NumberFormat("zh-CN");

  /* 所有动态文本先经过 esc() 再进入 innerHTML；reason 等字段是外部数据 */
  function esc(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "'": "&#39;",
      '"': "&quot;",
    })[character]);
  }

  function relativeTime(iso) {
    if (!iso) return "未知";
    const date = new Date(iso);
    if (Number.isNaN(date.valueOf())) return String(iso);
    const seconds = Math.round(Math.max(0, Date.now() - date.valueOf()) / 1000);
    if (seconds < 45) return `${seconds} 秒前`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes} 分钟前`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours} 小时前`;
    return `${Math.round(hours / 24)} 天前`;
  }

  function statusClass(status) {
    return status === "available" || status === "unavailable" ? status : "unverified";
  }

  function statusLabel(status) {
    return status === "available" ? "可用" : status === "unavailable" ? "不可用" : "候选未验证";
  }

  function latencyLabel(ms) {
    if (ms == null) return "—";
    return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
  }

  function formatTokens(value) {
    if (!value) return "—";
    if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value % 1_000_000 ? 1 : 0)}M`;
    if (value >= 1000) return `${Math.round(value / 1000)}K`;
    return number.format(value);
  }

  function renderStats(catalog) {
    const providers = catalog.providers || [];
    const okProviders = providers.filter((p) => p.availability === "available").length;
    let okModels = 0;
    let totalModels = 0;
    providers.forEach((provider) => (provider.models || []).forEach((model) => {
      totalModels += 1;
      if (model.availability === "available") okModels += 1;
    }));
    $("#st-stat-providers").textContent = `${okProviders} / ${catalog.provider_count ?? providers.length}`;
    $("#st-stat-models").textContent = `${okModels} / ${catalog.model_count ?? totalModels}`;
    /* Labelled 最近检查. status_as_of is the availability-evidence clock;
       generated_at is when the catalog structure was exported. Falling back to
       the latter reports a time for a check that never ran — and once a
       scheduled job refreshes generated_at daily it would read "0 秒前". */
    $("#st-stat-asof").textContent = catalog.status_as_of ? relativeTime(catalog.status_as_of) : "未验证";
  }

  /* A shape glyph plus visually-hidden text: the three states must not be
     distinguishable by colour alone (WCAG 1.4.1), and a screen reader
     otherwise hears only the model ID with no availability at all. */
  const STATE_GLYPH = { available: "●", unavailable: "✕", unverified: "◐" };

  function modelBadge(model) {
    const cls = statusClass(model.availability);
    const title = [
      model.name || model.id,
      statusLabel(model.availability),
      model.speed_tier_zh || "未测",
      `上下文 ${formatTokens(model.context_window)}`,
      `最大输出 ${formatTokens(model.max_tokens)}`,
      model.reasoning ? "支持推理" : null,
      model.tool_calling ? "支持工具" : null,
    ].filter(Boolean).join(" · ");
    return `<span class="st-badge ${cls}" title="${esc(title)}"><i aria-hidden="true">${STATE_GLYPH[cls]}</i><span class="st-sr">${statusLabel(model.availability)}：</span>${esc(model.id)}</span>`;
  }

  function providerCard(provider) {
    const cls = statusClass(provider.availability);
    const zhName = provider.profile?.zh_name || provider.name || provider.id;
    const models = provider.models || [];
    return `<article class="st-card ${cls}">
      <header>
        <span class="st-lamp ${cls}" aria-hidden="true"></span>
        <h3>${esc(zhName)}</h3>
        <span class="st-statusword ${cls}">${statusLabel(provider.availability)}</span>
      </header>
      <div class="st-meta">
        <span><b>${esc(latencyLabel(provider.latency_ms))}</b>实测延迟</span>
        <span><b>${esc(String(provider.model_count ?? models.length))}</b>模型数</span>
        <span><b>${esc(relativeTime(provider.checked_at))}</b>最近检查</span>
      </div>
      <p class="st-reason">${esc(provider.reason || "暂无说明")}</p>
      <div class="st-badges">${models.map(modelBadge).join("")}</div>
    </article>`;
  }

  function renderCards(catalog) {
    const cards = $("#st-cards");
    const providers = catalog.providers || [];
    cards.innerHTML = providers.length
      ? providers.map(providerCard).join("")
      : `<p class="st-empty">目录快照里暂时没有任何提供商。</p>`;
    cards.setAttribute("aria-busy", "false");
  }

  function renderAll(catalog) {
    renderStats(catalog);
    renderCards(catalog);
    if (catalog.status_note) $("#st-note-text").textContent = catalog.status_note;
    $("#st-generated").textContent = catalog.status_source === "cloudflare-server-probe"
      ? `服务器探测于 ${relativeTime(catalog.status_as_of)} · 每 ${catalog.probe_interval_minutes || 15} 分钟轮换`
      : `目录生成于 ${relativeTime(catalog.generated_at)} · 等待服务器探测`;
  }

  function metadataOnly(catalog) {
    return {
      ...catalog,
      status_as_of: "",
      status_source: "static-metadata-fallback",
      /* catalog.json still carries the old English smoke-test note; in this
         mode the page has no availability evidence at all, so say that. */
      status_note: "服务器探测接口暂不可用，本页仅展示静态模型目录，不判断可用性。",
      providers: (catalog.providers || []).map((provider) => ({
        ...provider,
        availability: "unverified",
        reason: "Cloudflare 服务器探测接口暂不可用",
        latency_ms: null,
        checked_at: "",
        models: (provider.models || []).map((model) => ({
          ...model,
          availability: "unverified",
          reason: "等待服务器探测恢复",
          latency_ms: null,
          checked_at: "",
          speed_tier_zh: "未测",
        })),
      })),
    };
  }

  /* `detail` is passed explicitly because state.catalog is already
     populated by the time the static-fallback path reports itself — reading
     it there would claim we kept "previous data" on the very first load. */
  function showError(message, detail) {
    $("#st-error-text").textContent = message;
    $("#st-error").querySelector("em").textContent = detail
      ?? (state.catalog
        ? "已保留上一次成功获取的数据。"
        : "尚未获取到任何数据，将在倒计时结束后自动重试。");
    $("#st-error").hidden = false;
  }

  function hideError() {
    $("#st-error").hidden = true;
  }

  async function load() {
    if (state.loading) return;
    state.loading = true;
    document.body.classList.add("st-loading");
    $("#st-refresh").disabled = true;
    try {
      /* 本地仪表盘提供 /api/catalog；公开静态站回退到 /data/catalog.json */
      let response;
      let fallback = false;
      try {
        /* This board only reads availability; skip the discovery payload. */
        response = await fetch("/api/catalog?fields=status", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
      } catch {
        response = await fetch("/data/catalog.json", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        fallback = true;
      }
      const catalog = fallback ? metadataOnly(await response.json()) : await response.json();
      if (!Array.isArray(catalog.providers)) throw new Error("目录数据格式异常");
      state.catalog = catalog;
      renderAll(catalog);
      if (fallback) showError(
        "服务器状态接口暂不可用；当前只展示静态模型目录，不判断可用性。",
        "下方模型清单来自最近一次发布的静态快照，不代表当前可用性。",
      );
      else hideError();
    } catch (error) {
      showError(`无法获取 /api/catalog（${error.message || error}）`);
      if (state.catalog) {
        renderAll(state.catalog); /* 刷新旧数据的相对时间 */
      } else {
        /* Nothing has ever loaded: the skeletons would otherwise stay up
           forever with aria-busy stuck at true. */
        const cards = $("#st-cards");
        cards.innerHTML = `<p class="st-empty">暂时无法获取状态数据，将自动重试。</p>`;
        cards.setAttribute("aria-busy", "false");
      }
    } finally {
      state.loading = false;
      state.lastLoadedAt = Date.now();
      state.secondsLeft = REFRESH_SECONDS;
      $("#st-countdown").textContent = String(REFRESH_SECONDS);
      document.body.classList.remove("st-loading");
      $("#st-refresh").disabled = false;
    }
  }

  function tick() {
    if (state.paused) return;
    if (state.loading) return;
    if (document.hidden) return; /* 后台标签页暂停倒计时，回到前台继续 */
    state.secondsLeft -= 1;
    if (state.secondsLeft <= 0) {
      load();
      return;
    }
    $("#st-countdown").textContent = String(state.secondsLeft);
  }

  $("#st-refresh").addEventListener("click", () => load());
  /* WCAG 2.2.2: content that updates automatically needs a way to stop it. */
  $("#st-pause").addEventListener("click", () => {
    state.paused = !state.paused;
    const button = $("#st-pause");
    button.setAttribute("aria-pressed", String(state.paused));
    button.textContent = state.paused ? "继续自动刷新" : "暂停自动刷新";
    $("#st-countdown").textContent = state.paused ? "已暂停" : String(state.secondsLeft);
  });
  document.addEventListener("visibilitychange", () => {
    // The countdown is reset in load()'s finally, so secondsLeft is never
    // <= 0 here — compare against the last successful load instead, or the
    // tab could sit on stale data indefinitely after being backgrounded.
    if (document.hidden) return;
    // Returning to a paused tab must not refresh: load()'s finally would also
    // overwrite the "已暂停" countdown with a number.
    if (state.paused) return;
    if (Date.now() - state.lastLoadedAt >= REFRESH_SECONDS * 1000) load();
    else if (state.catalog) renderAll(state.catalog);
  });
  setInterval(tick, 1000);
  load();
})();
