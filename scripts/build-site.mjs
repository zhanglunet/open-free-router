import { copyFile, cp, mkdir, readFile, readdir, rename, rm, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { join, relative, resolve } from "node:path";

import { checkCatalogFreshness } from "./data-freshness.mjs";

const root = resolve(import.meta.dirname, "..");
const source = resolve(root, "site");
const output = resolve(root, "web");

await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
await cp(source, output, { recursive: true });
await copyFile(resolve(root, "scripts", "install.sh"), resolve(output, "install.sh"));

const html = await readFile(resolve(output, "index.html"), "utf8");
const modelsHtml = await readFile(resolve(output, "models", "index.html"), "utf8");
const modelsJs = await readFile(resolve(output, "models", "models.js"), "utf8");
const guideHtml = await readFile(resolve(output, "guide", "index.html"), "utf8");
const npmGuideHtml = await readFile(resolve(output, "guide", "npm", "index.html"), "utf8");
const brandHtml = await readFile(resolve(output, "brand", "index.html"), "utf8");
const architectureHtml = await readFile(resolve(output, "architecture", "index.html"), "utf8");
const statusHtml = await readFile(resolve(output, "status", "index.html"), "utf8");
const statusJs = await readFile(resolve(output, "status", "status.js"), "utf8");
const statusCss = await readFile(resolve(output, "status", "status.css"), "utf8");
const mapHtml = await readFile(resolve(output, "map", "index.html"), "utf8");
const sitemapHtml = await readFile(resolve(output, "sitemap", "index.html"), "utf8");
const sitemapXml = await readFile(resolve(output, "sitemap.xml"), "utf8");
const robotsTxt = await readFile(resolve(output, "robots.txt"), "utf8");
const logsHtml = await readFile(resolve(output, "logs", "index.html"), "utf8");
const logsJs = await readFile(resolve(output, "logs", "logs.js"), "utf8");
const storyHtml = await readFile(resolve(output, "stories", "free-model-port", "index.html"), "utf8");
const storyJs = await readFile(resolve(output, "stories", "free-model-port", "article.js"), "utf8");
const compareHtml = await readFile(resolve(output, "compare", "index.html"), "utf8");
const validationHtml = await readFile(resolve(output, "validation", "index.html"), "utf8");
const benchmarksHtml = await readFile(resolve(output, "benchmarks", "index.html"), "utf8");
const benchmarksJs = await readFile(resolve(output, "benchmarks", "benchmarks.js"), "utf8");
const benchmarksCss = await readFile(resolve(output, "benchmarks", "benchmarks.css"), "utf8");
const benchmarksData = JSON.parse(await readFile(resolve(output, "data", "benchmarks.json"), "utf8"));
const internalBenchmarksHtml = await readFile(resolve(output, "internal", "benchmarks", "index.html"), "utf8");
const internalBenchmarksJs = await readFile(resolve(output, "internal", "benchmarks", "internal.js"), "utf8");
const internalBenchmarksCss = await readFile(resolve(output, "internal", "benchmarks", "internal.css"), "utf8");
const devlog = JSON.parse(await readFile(resolve(output, "data", "devlog.json"), "utf8"));
const navJs = await readFile(resolve(output, "nav.js"), "utf8");
const required = [
  "NoelJudeNoel/open-free-router",
  "系统架构",
  "快速开始",
  "安全边界",
  "Codex",
];
for (const marker of required) {
  if (!html.includes(marker)) {
    throw new Error(`Generated site is missing required content: ${marker}`);
  }
}
for (const marker of ['href="/guide/npm/"', 'href="/validation/"', 'href="/sitemap/"']) {
  if (!html.includes(marker)) throw new Error(`Homepage navigation is missing required route: ${marker}`);
}
for (const marker of ["免费模型", "模型参数与能力比较", "持续发现", "/api/catalog"]) {
  if (!modelsHtml.includes(marker) && !modelsJs.includes(marker)) {
    throw new Error(`Generated model radar is missing required content: ${marker}`);
  }
}
if (!modelsJs.includes("function html(value)") || !modelsJs.includes("&lt;")) {
  throw new Error("Model radar must HTML-escape public catalog fields");
}
for (const marker of [
  "Codex CLI", "Codex 客户端", "一键安装", "/v1/responses", "discover --test --adopt",
  "Claude Code", "Kimi CLI", "OpenClaw", "WorkBuddy", "/v1/messages",
  "MCP", "sync --agent claude", "如何获取 API Key", "protocols [--json]",
  "默认 8 个工具", "当前需要仓库读取权限",
]) {
  if (!guideHtml.includes(marker)) {
    throw new Error(`Generated guide is missing required content: ${marker}`);
  }
}
for (const marker of ["npm install -g open-free-router", "Python 3.11+", "OFR_NPM_HOME", "sync --agent codex", "升级、重装与卸载", "常见问题"]) {
  if (!npmGuideHtml.includes(marker)) {
    throw new Error(`Generated npm guide is missing required content: ${marker}`);
  }
}
if (!modelsJs.includes("key_url") || !modelsJs.includes("key_steps_zh")) {
  throw new Error("Model radar must render provider API-key guidance");
}
// The static-fallback branch used the snapshot raw: green provider dots, a
// numeric 最近可用 counter and per-row 可用 badges presented as current fact.
// Degradation is unconditional on that branch — it is by definition the branch
// that knows availability was not measured — so no age constant lives here.
for (const marker of ["静态兜底数据", "function degradeCatalog"]) {
  if (!modelsJs.includes(marker)) {
    throw new Error(`Generated model radar is missing required fallback degradation: ${marker}`);
  }
}
if (!modelsHtml.includes('value="unverified"')) {
  throw new Error("Model radar status filter must be able to select 待验证 rows");
}
for (const marker of [
  "系统架构", "8337", "Claude Code", "/v1/messages", "MCP", "registry.yaml",
  "三层故障隔离", "首字节前安全 fallback", "usage.db", "二十四个功能模块",
]) {
  if (!architectureHtml.includes(marker)) {
    throw new Error(`Generated architecture page is missing required content: ${marker}`);
  }
}
for (const marker of ["服务器端", "Cloudflare", "每 15 分钟", "完全不访问你的本机", "/api/catalog"]) {
  if (!statusHtml.includes(marker) && !statusJs.includes(marker)) {
    throw new Error(`Generated status page is missing required content: ${marker}`);
  }
}
if (!statusJs.includes("服务器探测于") || !statusJs.includes("probe_interval_minutes")) {
  throw new Error("Status page must distinguish server probe time from static catalog generation time");
}
// The element this fills is labelled 最近检查, and metadataOnly() blanks
// status_as_of on the fallback path, so falling through to the export clock
// reports a time for an availability check that never happened — and the
// scheduled refresh would make it read "0 秒前".
if (statusJs.includes("catalog.status_as_of || catalog.generated_at")) {
  throw new Error("Status board must not present the catalog export time as an availability check time");
}
if (!statusCss.includes(".st-error[hidden]") || !statusCss.includes("display:none")) {
  throw new Error("Status error banner must remain hidden after a successful refresh");
}
for (const marker of ["全球", "world-dots.svg"]) {
  if (!mapHtml.includes(marker)) {
    throw new Error(`Generated map page is missing required content: ${marker}`);
  }
}
for (const marker of ["开发日志", "搜索历史", "/data/devlog.json", "推荐文章"]) {
  if (!logsHtml.includes(marker) && !logsJs.includes(marker)) {
    throw new Error(`Generated development log is missing required content: ${marker}`);
  }
}
if (!Array.isArray(devlog.entries) || devlog.entries.length < 5) {
  throw new Error("Development log must contain structured historical entries");
}
for (const entry of devlog.entries) {
  if (!entry.id || !entry.date || !entry.type || !entry.title || !entry.summary) {
    throw new Error(`Development log entry is incomplete: ${entry.id || "unknown"}`);
  }
}
// ── published catalog freshness and shape ───────────────────────────────
//
// Anchored to min(now, HEAD committer date). A wall-clock gate would fire on
// commits whose catalog was fresh when they were made, which silently corrupts
// `git bisect` (it reads exit 1 as "bad" and this build never emits 125) and
// breaks rebuilding an old tag. Anchoring means a 30-day-old commit with a
// 30-day-old catalog passes, while today's commit with a 30-day-old catalog
// fails — which is the case that actually matters.
const publishedCatalog = JSON.parse(await readFile(resolve(output, "data", "catalog.json"), "utf8"));
let anchorMs = Date.now();
let anchorLabel = "墙钟";
try {
  const committedAt = execFileSync("git", ["log", "-1", "--format=%cI"], { cwd: root, encoding: "utf8" }).trim();
  const committedMs = Date.parse(committedAt);
  if (Number.isFinite(committedMs) && committedMs < anchorMs) {
    anchorMs = committedMs;
    anchorLabel = `HEAD 提交时刻 ${committedAt}`;
  }
} catch {
  // Tarball, vendored checkout or shallow export: no git metadata. The wall
  // clock is strictly stricter, so this direction fails safe.
}
const freshness = checkCatalogFreshness(publishedCatalog, { anchorMs });
console.log(`新鲜度锚点：${anchorLabel}`);
for (const line of freshness.info) console.log(`  · ${line}`);
if (!freshness.ok) {
  throw new Error(`公开目录数据新鲜度校验失败：\n  - ${freshness.errors.join("\n  - ")}`);
}

for (const marker of ["免费大模型", "真实实测", "本地优先", "复制朋友圈文案", "https://oaf.asia/"]) {
  if (!storyHtml.includes(marker) && !storyJs.includes(marker)) {
    throw new Error(`Generated recommendation story is missing required content: ${marker}`);
  }
}
for (const marker of ["OmniRoute", "9Router", "LiteLLM", "Free Router", "比较依据", "2026-08-03", "290+", "Provider Reference", "统一准入门槛", "扩容候选"]) {
  if (!compareHtml.includes(marker)) throw new Error(`Generated comparison page is missing required content: ${marker}`);
}
for (const marker of ["八道闸门", "models.dev", "44 家 / 322 模型", "58 / 58 通过", "OFR_*_API_KEY", "ready ≠ 永久免费", "auto_adopt: false", "非空 message.content", "DEEPSEEK V4 FLASH", "OpenCode Zen", "NVIDIA NIM", "HTTP 404", "软件自由，不是免费云算力"]) {
  if (!validationHtml.includes(marker)) throw new Error(`Generated candidate validation page is missing required content: ${marker}`);
}
for (const route of ["/", "/models/", "/status/", "/validation/", "/benchmarks/", "/compare/", "/architecture/", "/map/", "/guide/", "/guide/npm/", "/stories/free-model-port/", "/logs/", "/brand/", "/sitemap/"]) {
  if (!sitemapHtml.includes(`href="${route}"`)) throw new Error(`Generated site map is missing route: ${route}`);
}
for (const marker of ["页面关系结构", "全部页面链接", "推荐路径", "14 PAGES"]) {
  if (!sitemapHtml.includes(marker)) throw new Error(`Generated site map is missing required content: ${marker}`);
}
if ((sitemapXml.match(/<url>/g) || []).length !== 14 || !robotsTxt.includes("https://oaf.asia/sitemap.xml")) {
  throw new Error("Machine-readable sitemap and robots.txt must expose all 14 public pages");
}
for (const marker of ["模型评测", "任务适配分", "Artificial Analysis", "不从图片猜分", "35%", "25%", "30%", "10%"]) {
  if (!benchmarksHtml.includes(marker) && !benchmarksJs.includes(marker)) {
    throw new Error(`Generated benchmarks page is missing required content: ${marker}`);
  }
}
// 「不再继承同提供商其他模型的快照」 replaced 「同提供商模型目前继承同一快照」:
// the old sentence described benchmarks.js accurately until it started
// scoring from per-model evidence, at which point it became a false
// statement pinned in place by this very guard.
for (const marker of ["明确计分规则", "不再继承同提供商其他模型的快照", "逐模型探测证据", "目录未提供", "不是 0 分"]) {
  if (!benchmarksHtml.includes(marker) && !benchmarksJs.includes(marker)) {
    throw new Error(`Generated benchmarks methodology is missing required content: ${marker}`);
  }
}
// 45% of 任务适配分 comes from availability (35) and latency (10), and the
// methodology table asserts the data is a Cloudflare probe snapshot — false on
// the static-fallback path, where this page previously showed no timestamp at
// all.
for (const marker of ["静态兜底数据", "function degradeCatalog"]) {
  if (!benchmarksJs.includes(marker)) {
    throw new Error(`Generated benchmarks page is missing required fallback degradation: ${marker}`);
  }
}
if (!benchmarksHtml.includes('id="bench-freshness"')) {
  throw new Error("Benchmarks page must render catalog freshness");
}
if (benchmarksJs.includes('style="') || benchmarksJs.includes("style='")) {
  throw new Error("Benchmarks dynamic markup must not use inline styles blocked by CSP");
}
for (const marker of [".x-100", ".y-100", ".score-100"]) {
  if (!benchmarksCss.includes(marker)) throw new Error(`Benchmarks CSP-safe chart positioning is missing: ${marker}`);
}
for (const marker of [".scatter-viewport", ".jitter-7", 'font-family:"PingFang SC"', ".point.labelled span"]) {
  if (!benchmarksCss.includes(marker)) throw new Error(`Benchmarks responsive visual fix is missing: ${marker}`);
}
for (const marker of ["labelledBins", "return 86 -", "jitter-${index % 8}", "viewport.scrollLeft = viewport.scrollWidth - viewport.clientWidth"]) {
  if (!benchmarksJs.includes(marker)) throw new Error(`Benchmarks chart collision guard is missing: ${marker}`);
}
if (!benchmarksJs.includes("/data/benchmarks.json?v=")) {
  throw new Error("Benchmarks static snapshot request must be versioned to avoid stale edge 404s");
}
// Cache-busting is now handled by content-hash fingerprinting below, which
// covers every asset rather than the handful that carried manual markers.
if (!benchmarksJs.includes("function html(value)") || !benchmarksJs.includes("&lt;")) {
  throw new Error("Benchmarks page must HTML-escape public catalog and benchmark fields");
}
if (!Array.isArray(benchmarksData.sources) || !benchmarksData.sources.some((source) => source.id === "artificial-analysis" && source.attribution_required)) {
  throw new Error("Benchmark snapshot must preserve Artificial Analysis attribution requirements");
}
for (const marker of ["noindex,nofollow,noarchive", "内部访问令牌", "仅供内部使用", "/api/internal/benchmarks"]) {
  if (!internalBenchmarksHtml.includes(marker) && !internalBenchmarksJs.includes(marker)) {
    throw new Error(`Internal benchmarks page is missing its access boundary: ${marker}`);
  }
}
for (const marker of ["sessionStorage", "Authorization: `Bearer ${state.token}`", "/api/internal/benchmarks/refresh", "function html(value)"]) {
  if (!internalBenchmarksJs.includes(marker)) throw new Error(`Internal benchmarks client security is missing: ${marker}`);
}
if (!internalBenchmarksCss.includes(".access-panel") || /localStorage/.test(internalBenchmarksJs)) {
  throw new Error("Internal benchmarks must use a dedicated access panel and tab-scoped credentials");
}
if (sitemapHtml.includes("/internal/benchmarks/") || sitemapXml.includes("/internal/benchmarks/")) {
  throw new Error("Internal benchmarks must never be included in public sitemaps");
}
if (/<script(?![^>]*src=)[^>]*>[^<]/.test(internalBenchmarksHtml)) {
  throw new Error("Internal benchmarks must not contain inline scripts");
}
const headersFile = await readFile(resolve(output, "_headers"), "utf8");
if (!headersFile.includes("/data/*.json")) {
  throw new Error("Catalog data must carry an explicit cache policy");
}
await readFile(resolve(output, "assets", "map", "world-dots.svg"));
await readFile(resolve(output, "assets", "brand", "og-free-model-port-share.jpg"));
for (const page of [html, modelsHtml, guideHtml, npmGuideHtml, brandHtml, architectureHtml, statusHtml, mapHtml, sitemapHtml, logsHtml, storyHtml, compareHtml, validationHtml, benchmarksHtml]) {
  if (/<script(?![^>]*src=)[^>]*>[^<]/.test(page)) {
    throw new Error("Site pages must not contain inline scripts (CSP script-src 'self')");
  }
  // Checked before fingerprinting, so the reference is still the plain path.
  if (!page.includes('src="/nav.js"')) {
    throw new Error("Every page with the shared header must load the responsive navigation");
  }
}
for (const marker of ["mobile-nav-toggle", "aria-expanded", "Escape", "本页目录"]) {
  if (!navJs.includes(marker)) {
    throw new Error(`Responsive navigation is missing required behavior: ${marker}`);
  }
}
if (!logsJs.includes("function html(value)") || !logsJs.includes("&lt;")) {
  throw new Error("Development log must HTML-escape public log fields");
}
if (!storyJs.includes("textContent")) {
  throw new Error("Recommendation story copy action must use text content safely");
}
for (const marker of ["模力自由港", "FreeModel Port", "品牌宣言", "下载主 Logo PNG"]) {
  if (!brandHtml.includes(marker)) {
    throw new Error(`Generated brand page is missing required content: ${marker}`);
  }
}
for (const asset of ["modelport-mark.png", "favicon.png", "og-modelport.jpg"]) {
  await readFile(resolve(output, "assets", "brand", asset));
}
if (/AIza[0-9A-Za-z_-]{30,}|sk-[0-9A-Za-z]{20,}/.test(html)) {
  throw new Error("Generated site contains a credential-like value");
}

// ── content-hash fingerprinting ─────────────────────────────────────────
//
// Hand-maintained `?v=` markers only ever covered 6 of 27 assets — styles.css
// among the ones they missed — so an edit to it reached returning visitors
// only when their cache happened to revalidate. Renaming each CSS/JS file
// after a hash of its own bytes makes the URL change exactly when the
// content does, which lets `_headers` mark them immutable for a year.
//
// `site/` stays clean: only the built copy under `web/` is rewritten.

async function walk(dir, out = []) {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) await walk(full, out);
    else out.push(full);
  }
  return out;
}

const builtFiles = await walk(output);
const hashedAssets = new Map(); // "/styles.css" → "/styles.a1b2c3d4.css"

for (const file of builtFiles) {
  if (!/\.(css|js)$/.test(file)) continue;
  const url = "/" + relative(output, file).split("\\").join("/");
  const bytes = await readFile(file);
  const hash = createHash("sha256").update(bytes).digest("hex").slice(0, 8);
  const hashedUrl = url.replace(/\.(css|js)$/, `.${hash}.$1`);
  await rename(file, join(output, hashedUrl.slice(1)));
  hashedAssets.set(url, hashedUrl);
}

// Longest-first so /models/models.css is never partially matched by a
// shorter path that happens to be a prefix.
const assetPatterns = [...hashedAssets.entries()].sort((a, b) => b[0].length - a[0].length);

for (const file of await walk(output)) {
  if (!/\.(html|js|css)$/.test(file)) continue;
  let text = await readFile(file, "utf8");
  const original = text;
  for (const [url, hashedUrl] of assetPatterns) {
    // Match the reference with or without a leftover query string.
    text = text.split(`"${url}"`).join(`"${hashedUrl}"`);
    text = text.replaceAll(new RegExp(`"${url.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&")}\\?[^"]*"`, "g"), `"${hashedUrl}"`);
  }
  if (text !== original) await writeFile(file, text);
}

// Nothing referenced from a page may keep an unhashed URL, or it silently
// falls back to the old caching behaviour this exists to replace.
for (const file of await walk(output)) {
  if (!file.endsWith(".html")) continue;
  const page = await readFile(file, "utf8");
  for (const [, ref] of page.matchAll(/(?:src|href)="(\/[^"]+\.(?:css|js))(?:\?[^"]*)?"/g)) {
    if (!/\.[0-9a-f]{8}\.(css|js)$/.test(ref)) {
      throw new Error(`Unfingerprinted asset referenced by ${relative(output, file)}: ${ref}`);
    }
  }
}

console.log(`Built ${output} from ${source} (${hashedAssets.size} assets fingerprinted)`);
