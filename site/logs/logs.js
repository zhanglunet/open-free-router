const list = document.querySelector("#log-list");
const search = document.querySelector("#log-search");
const count = document.querySelector("#log-result-count");
const filters = [...document.querySelectorAll("[data-log-type]")];
const typeNames = { feature: "新功能", improvement: "体验改进", security: "安全修复", milestone: "里程碑" };
let entries = [];
let activeType = "all";

function html(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function localUrl(value) {
  const url = String(value || "");
  return url.startsWith("/") && !url.startsWith("//") ? html(url) : "#";
}

function render() {
  const query = search.value.trim().toLowerCase();
  const visible = entries.filter((entry) => {
    const haystack = [entry.title, entry.summary, entry.version, ...(entry.items || [])].join(" ").toLowerCase();
    return (activeType === "all" || entry.type === activeType) && haystack.includes(query);
  });
  count.textContent = `显示 ${visible.length} / ${entries.length} 条`;
  list.innerHTML = visible.length ? visible.map((entry) => `
    <article class="log-card">
      <div class="log-meta"><span>${html(entry.date)}</span><span class="log-type ${html(entry.type)}">${html(typeNames[entry.type] || "更新")}</span><span>${html(entry.version || "")}</span></div>
      <h2>${html(entry.title)}</h2><p>${html(entry.summary)}</p>
      <ul class="log-items">${(entry.items || []).map((item) => `<li>${html(item)}</li>`).join("")}</ul>
      <div class="log-links">${(entry.links || []).map((link) => `<a href="${localUrl(link.url)}">${html(link.label)} →</a>`).join("")}${/^[0-9a-f]{7,40}$/i.test(entry.commit || "") ? `<a class="log-commit" href="https://github.com/zhanglunet/open-free-router/commit/${html(entry.commit)}" rel="noreferrer">提交 ${html(entry.commit)} ↗</a>` : ""}</div>
    </article>`).join("") : '<p class="log-empty">没有找到匹配的开发日志，请换一个关键词或筛选条件。</p>';
}

filters.forEach((button) => button.addEventListener("click", () => {
  activeType = button.dataset.logType;
  filters.forEach((item) => item.classList.toggle("active", item === button));
  render();
}));
search.addEventListener("input", render);

fetch("/data/devlog.json", { headers: { Accept: "application/json" } })
  .then((response) => response.ok ? response.json() : Promise.reject(new Error(String(response.status))))
  .then((data) => {
    entries = Array.isArray(data.entries) ? data.entries : [];
    document.querySelector('[data-log-stat="entries"]').textContent = entries.length;
    document.querySelector('[data-log-stat="days"]').textContent = new Set(entries.map((entry) => entry.date)).size;
    document.querySelector('[data-log-stat="latest"]').textContent = entries[0]?.date?.slice(5) || "—";
    render();
  })
  .catch(() => { list.innerHTML = '<p class="log-empty">开发日志暂时无法读取，请稍后刷新。</p>'; });
