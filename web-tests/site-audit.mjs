/**
 * Whole-site audit engine.
 *
 * Two layers:
 *   1. Static — parses every page in site/ with no dependencies. Always runs.
 *      Covers link/asset integrity, HTML structure, metadata, sitemap and
 *      robots consistency, CSP presence, and inline-style/script violations.
 *   2. Browser — drives the built site in headless Chromium (playwright-core
 *      + axe-core, both optional). Covers console/page errors, failed
 *      requests, CSP violations at runtime, pages stuck in a loading state,
 *      horizontal overflow at phone/tablet/desktop widths, and axe a11y.
 *
 * The browser layer is skipped (not failed) when playwright-core or a
 * Chromium binary is unavailable, so `npm run test:site` still gives useful
 * results in a bare checkout or CI image without browsers.
 */
import { readFile, readdir } from "node:fs/promises";
import { createReadStream, existsSync, statSync } from "node:fs";
import { resolve, join, relative, extname } from "node:path";
import http from "node:http";

export const ROOT = resolve(import.meta.dirname, "..");
export const SITE = join(ROOT, "site");
export const WEB = join(ROOT, "web");

const PUBLIC_ORIGIN = "https://oaf.asia";

/** Pages that are intentionally unlisted (no sitemap entry, noindex). */
const PRIVATE_PATHS = new Set(["/internal/benchmarks/"]);

// ── small helpers ───────────────────────────────────────────────────────

async function walk(dir, out = []) {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) await walk(full, out);
    else out.push(full);
  }
  return out;
}

/** Strip HTML comments so commented-out markup never trips a check. */
function withoutComments(html) {
  return html.replace(/<!--[\s\S]*?-->/g, "");
}

/** Attribute values for a given attribute across all tags. */
function attrValues(html, attr) {
  const re = new RegExp(`\\b${attr}\\s*=\\s*"([^"]*)"`, "gi");
  return [...html.matchAll(re)].map((m) => m[1]);
}

function tagsOf(html, tag) {
  const re = new RegExp(`<${tag}\\b[^>]*>`, "gi");
  return [...html.matchAll(re)].map((m) => m[0]);
}

function attrOf(tagText, attr) {
  const m = tagText.match(new RegExp(`\\b${attr}\\s*=\\s*"([^"]*)"`, "i"));
  return m ? m[1] : null;
}

/** URL → path on disk under site/, or null when it is not a local page/asset. */
export function localTarget(url) {
  if (!url) return null;
  const trimmed = url.trim();
  if (!trimmed || trimmed.startsWith("#")) return null;
  if (/^(https?:|mailto:|tel:|data:|javascript:)/i.test(trimmed)) return null;
  if (!trimmed.startsWith("/")) return null; // site uses root-relative links only
  const [pathPart] = trimmed.split("#");
  const [clean] = pathPart.split("?"); // drop cache-busting query
  if (clean.endsWith("/")) return join(SITE, clean, "index.html");
  return join(SITE, clean);
}

/** Public route for a page file, e.g. site/guide/index.html → /guide/ */
export function routeOf(pageFile) {
  const rel = "/" + relative(SITE, pageFile).split("\\").join("/");
  if (rel.endsWith("/index.html")) return rel.slice(0, -"index.html".length);
  return rel;
}

export async function listPages() {
  const files = await walk(SITE);
  return files.filter((f) => f.endsWith(".html")).sort();
}

// ── static checks ───────────────────────────────────────────────────────

export async function staticAudit() {
  const findings = [];
  const add = (severity, page, title, detail) =>
    findings.push({ layer: "static", severity, page, title, detail });

  const pages = await listPages();
  const pageSet = new Set(pages.map(routeOf));
  const titles = new Map();

  for (const file of pages) {
    const route = routeOf(file);
    const raw = await readFile(file, "utf8");
    const html = withoutComments(raw);

    // ── metadata ──
    if (!/<html[^>]*\blang\s*=\s*"[^"]+"/i.test(html))
      add("major", route, "缺少 lang 属性", "<html> 没有 lang，屏幕阅读器无法选择正确的语音库。");
    const title = (html.match(/<title>([\s\S]*?)<\/title>/i) || [])[1]?.trim();
    if (!title) add("major", route, "缺少 <title>", "搜索结果与浏览器标签页无标题。");
    else {
      if (titles.has(title))
        add("minor", route, "标题与其他页面重复", `与 ${titles.get(title)} 使用了相同的 <title>：${title}`);
      else titles.set(title, route);
      if (title.length > 60)
        add("minor", route, "标题过长", `${title.length} 字符，搜索结果会被截断（建议 ≤60）。`);
    }
    const desc = html.match(/<meta\s+name="description"\s+content="([^"]*)"/i);
    if (!desc) add("minor", route, "缺少 meta description", "搜索结果摘要由引擎自行拼接。");
    if (!/<meta\s+name="viewport"/i.test(html))
      add("major", route, "缺少 viewport", "移动端会按桌面宽度渲染再缩放。");
    if (!/<meta\s+http-equiv="Content-Security-Policy"/i.test(html))
      add("major", route, "缺少 CSP meta", "该页没有与其他页面一致的内容安全策略。");

    // ── structure ──
    // Sections marked `hidden` are removed from the accessibility tree, so
    // a heading inside one (e.g. a gated dashboard behind a login form) is
    // not a competing h1 for assistive tech.
    const visibleHtml = html.replace(/<section\b[^>]*\bhidden\b[^>]*>[\s\S]*?<\/section>/gi, "");
    const h1s = [...visibleHtml.matchAll(/<h1\b[^>]*>/gi)];
    if (h1s.length === 0) add("major", route, "没有 h1", "文档缺少主标题，影响大纲与 SEO。");
    if (h1s.length > 1) add("minor", route, "多个 h1", `发现 ${h1s.length} 个可见 <h1>。`);

    const ids = attrValues(html, "id");
    const dupes = ids.filter((id, i) => ids.indexOf(id) !== i);
    if (dupes.length)
      add("major", route, "重复的 id", `重复 id：${[...new Set(dupes)].join(", ")}（querySelector 与锚点会指向第一个）。`);

    // ── malformed tags ──
    // An unterminated tag swallows the ones after it; when that happens to
    // a <meta> in <head>, the parser closes <head> early and the CSP is
    // silently dropped (browsers only report this at runtime).
    for (const line of html.split("\n")) {
      if (/<(meta|link)\b[^>]*$/.test(line.trim()))
        add("critical", route, "标签未闭合", `缺少 '>'：${line.trim().slice(0, 90)}`);
    }
    if (html.includes(">>"))
      add("major", route, "多余的 '>'", "标记中出现 '>>'，通常意味着上一处编辑破坏了标签结构。");
    const headMatch = html.match(/<head\b[^>]*>([\s\S]*?)<\/head>/i);
    if (headMatch && !/Content-Security-Policy/i.test(headMatch[1]))
      add("critical", route, "CSP 不在 <head> 内", "CSP meta 必须位于 <head>，否则浏览器会忽略整条策略。");

    // ── CSP compliance in markup ──
    if (/\bstyle\s*=\s*"/i.test(html))
      add("major", route, "内联 style 属性", "CSP style-src 'self' 会拦截，样式在生产环境不生效。");
    const inlineScript = html.match(/<script\b(?![^>]*\bsrc=)[^>]*>[\s\S]*?<\/script>/i);
    if (inlineScript && inlineScript[0].replace(/<\/?script[^>]*>/gi, "").trim())
      add("major", route, "内联 <script>", "CSP script-src 'self' 会拦截该脚本。");
    if (/<style\b/i.test(html))
      add("major", route, "内联 <style> 块", "CSP style-src 'self' 会拦截。");

    // ── images ──
    for (const img of tagsOf(html, "img")) {
      if (attrOf(img, "alt") === null)
        add("major", route, "图片缺少 alt", `装饰图应写 alt=""，内容图应写描述：${img.slice(0, 90)}`);
    }

    // ── external links ──
    for (const a of tagsOf(html, "a")) {
      const href = attrOf(a, "href") || "";
      if (/^https?:/i.test(href) && !href.startsWith(PUBLIC_ORIGIN)) {
        const rel = (attrOf(a, "rel") || "").toLowerCase();
        if (attrOf(a, "target") === "_blank" && !rel.includes("noopener"))
          add("minor", route, "外链缺少 rel=noopener", `target=_blank 外链应带 rel="noopener"：${href}`);
      }
    }

    // ── local link + asset integrity ──
    const refs = [
      ...attrValues(html, "href"),
      ...attrValues(html, "src"),
    ];
    for (const ref of refs) {
      const target = localTarget(ref);
      if (!target) continue;
      if (!existsSync(target))
        add("critical", route, "死链 / 资源缺失", `${ref} → 磁盘上不存在 ${relative(ROOT, target)}`);
    }

    // ── in-page and cross-page anchors ──
    for (const ref of refs) {
      if (!ref.includes("#")) continue;
      const [pathPart, anchor] = ref.split("#");
      if (!anchor) continue;
      let targetHtml = html;
      if (pathPart && pathPart !== route) {
        const target = localTarget(pathPart || route);
        if (!target || !existsSync(target)) continue; // already reported above
        targetHtml = withoutComments(await readFile(target, "utf8"));
      }
      const hasId = new RegExp(`\\bid\\s*=\\s*"${anchor.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"`).test(targetHtml);
      if (!hasId)
        add("major", route, "锚点无目标", `${ref} 指向的 id="${anchor}" 在目标页面不存在，点击后停在页首。`);
    }
  }

  // ── sitemap.xml ↔ real pages ──
  const sitemapPath = join(SITE, "sitemap.xml");
  if (!existsSync(sitemapPath)) {
    add("major", "/sitemap.xml", "缺少 sitemap.xml", "搜索引擎需要逐页爬取发现内容。");
  } else {
    const xml = await readFile(sitemapPath, "utf8");
    const locs = [...xml.matchAll(/<loc>([^<]+)<\/loc>/g)].map((m) => m[1].trim());
    for (const loc of locs) {
      if (!loc.startsWith(PUBLIC_ORIGIN)) {
        add("major", "/sitemap.xml", "sitemap 含非站点 URL", loc);
        continue;
      }
      const route = loc.slice(PUBLIC_ORIGIN.length) || "/";
      if (PRIVATE_PATHS.has(route))
        add("critical", "/sitemap.xml", "内部页面被收录", `${route} 是受保护页面，不应出现在 sitemap.xml。`);
      else if (!pageSet.has(route))
        add("major", "/sitemap.xml", "sitemap 指向不存在的页面", `${loc} → site 下没有对应页面。`);
    }
    const listed = new Set(locs.map((l) => l.slice(PUBLIC_ORIGIN.length) || "/"));
    for (const route of pageSet) {
      if (route.endsWith(".html") && route !== "/404.html") continue;
      if (route === "/404.html" || PRIVATE_PATHS.has(route)) continue;
      if (!listed.has(route))
        add("minor", "/sitemap.xml", "页面未被收录", `${route} 存在但不在 sitemap.xml 中。`);
    }
  }

  // ── robots.txt ──
  const robotsPath = join(SITE, "robots.txt");
  if (!existsSync(robotsPath)) {
    add("minor", "/robots.txt", "缺少 robots.txt", "无法声明 sitemap 位置与抓取规则。");
  } else {
    const robots = await readFile(robotsPath, "utf8");
    if (!/sitemap:/i.test(robots))
      add("minor", "/robots.txt", "robots.txt 未声明 Sitemap", "应包含 Sitemap: https://oaf.asia/sitemap.xml。");
    const disallowed = [...robots.matchAll(/^\s*Disallow:\s*(\S+)/gim)].map((m) => m[1]);
    for (const priv of PRIVATE_PATHS) {
      // A broader prefix (Disallow: /internal/) already covers the page.
      if (!disallowed.some((rule) => priv.startsWith(rule)))
        add("minor", "/robots.txt", "内部页面未在 robots 中屏蔽", `建议 Disallow: ${priv}`);
    }
  }

  return findings;
}

// ── static file server for the browser layer ────────────────────────────

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".txt": "text/plain; charset=utf-8",
  ".xml": "application/xml",
  ".sh": "text/plain; charset=utf-8",
};

/**
 * Serve web/ and emulate the worker's JSON APIs from the committed data
 * files, so pages render with realistic content instead of error states.
 */
/**
 * @param {string} rootDir built site to serve
 * @param {{apiCatalog?: "ok"|"fail"}} options `fail` makes /api/* return 503 so
 *   the pages' static-fallback branches become reachable. Until this existed
 *   every test hit the happy path, which is why the fallback branch shipped
 *   rendering a stale snapshot as current fact.
 */
export async function startServer(rootDir = WEB, { apiCatalog = "ok" } = {}) {
  const catalogPath = join(rootDir, "data", "catalog.json");
  const catalog = existsSync(catalogPath)
    ? JSON.parse(await readFile(catalogPath, "utf8"))
    : { providers: [], provider_count: 0, model_count: 0 };
  const discovery = {
    schema_version: 2,
    generated_at: new Date().toISOString(),
    candidate_provider_count: 0,
    candidate_model_count: 0,
    providers: [],
    trust: "candidate-only; needs manual verification",
  };

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://localhost");
    const send = (status, body, type = "application/json; charset=utf-8") => {
      res.writeHead(status, { "Content-Type": type });
      res.end(body);
    };
    if (apiCatalog === "fail" && url.pathname.startsWith("/api/")) {
      return send(503, JSON.stringify({ error: "catalog_unavailable" }));
    }
    if (url.pathname === "/api/catalog") return send(200, JSON.stringify({ ...catalog, discovery }));
    if (url.pathname === "/api/status") return send(200, JSON.stringify(catalog));
    if (url.pathname === "/api/discovery") return send(200, JSON.stringify(discovery));
    if (url.pathname === "/api/health") return send(200, JSON.stringify({ ok: true }));
    if (url.pathname.startsWith("/api/internal/")) return send(401, JSON.stringify({ error: "unauthorized" }));

    let path = join(rootDir, decodeURIComponent(url.pathname));
    if (existsSync(path) && statSync(path).isDirectory()) path = join(path, "index.html");
    if (!existsSync(path)) {
      const notFound = join(rootDir, "404.html");
      if (existsSync(notFound)) {
        res.writeHead(404, { "Content-Type": MIME[".html"] });
        return createReadStream(notFound).pipe(res);
      }
      return send(404, "not found", "text/plain");
    }
    res.writeHead(200, { "Content-Type": MIME[extname(path)] || "application/octet-stream" });
    createReadStream(path).pipe(res);
  });
  await new Promise((ok) => server.listen(0, "127.0.0.1", ok));
  return { server, port: server.address().port };
}

// ── browser checks ──────────────────────────────────────────────────────

const VIEWPORTS = [
  { name: "phone", width: 375, height: 812 },
  { name: "tablet", width: 768, height: 1024 },
  { name: "desktop", width: 1440, height: 900 },
];

async function loadChromium() {
  let chromium;
  try {
    ({ chromium } = await import("playwright-core"));
  } catch {
    return { skip: "playwright-core 未安装（npm i -D playwright-core）" };
  }
  const candidates = [
    process.env.CHROMIUM_PATH,
    "/opt/pw-browsers/chromium",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
  ].filter(Boolean);
  const executablePath = candidates.find((p) => existsSync(p));
  if (!executablePath) return { skip: "找不到 Chromium 可执行文件（设置 CHROMIUM_PATH）" };
  return { chromium, executablePath };
}

async function loadAxeSource() {
  try {
    const axePath = new URL(import.meta.resolve("axe-core/axe.min.js"));
    return await readFile(axePath, "utf8");
  } catch {
    return null;
  }
}

export async function browserAudit({ verbose = false } = {}) {
  const { chromium, executablePath, skip } = await loadChromium();
  if (skip) return { skipped: skip, findings: [] };

  const findings = [];
  const add = (severity, page, title, detail) =>
    findings.push({ layer: "browser", severity, page, title, detail });

  if (!existsSync(WEB)) return { skipped: "web/ 未构建，先运行 npm run build", findings: [] };

  const { server, port } = await startServer();
  const axeSource = await loadAxeSource();
  const browser = await chromium.launch({ executablePath });
  const pages = await listPages();

  try {
    for (const file of pages) {
      const route = routeOf(file);
      if (route === "/404.html") continue; // exercised via the 404 path below
      const url = `http://127.0.0.1:${port}${route}`;

      for (const viewport of VIEWPORTS) {
        const context = await browser.newContext({
          viewport: { width: viewport.width, height: viewport.height },
        });
        const page = await context.newPage();
        const consoleErrors = [];
        const pageErrors = [];
        const failedRequests = [];
        const cspViolations = [];

        page.on("console", (msg) => {
          if (msg.type() === "error") consoleErrors.push(msg.text());
        });
        page.on("pageerror", (err) => pageErrors.push(String(err)));
        page.on("requestfailed", (req) =>
          failedRequests.push(`${req.url()} (${req.failure()?.errorText})`));
        page.on("response", (res) => {
          if (res.status() >= 400) failedRequests.push(`${res.url()} → HTTP ${res.status()}`);
        });
        await page.addInitScript(() => {
          window.__cspViolations = [];
          document.addEventListener("securitypolicyviolation", (e) => {
            window.__cspViolations.push(`${e.violatedDirective}: ${e.blockedURI}`);
          });
        });

        await page.goto(url, { waitUntil: "networkidle", timeout: 30000 });
        await page.waitForTimeout(600);
        cspViolations.push(...(await page.evaluate(() => window.__cspViolations || [])));

        // Only report page-level problems once (on desktop) — they repeat per viewport.
        if (viewport.name === "desktop") {
          for (const e of new Set(consoleErrors))
            add("major", route, "控制台报错", e.slice(0, 300));
          for (const e of new Set(pageErrors))
            add("critical", route, "未捕获的 JS 异常", e.slice(0, 300));
          for (const e of new Set(failedRequests))
            add("major", route, "请求失败", e.slice(0, 300));
          for (const e of new Set(cspViolations))
            add("major", route, "运行时 CSP 违规", e.slice(0, 300));

          // Pages stuck in a loading/skeleton state after the network settled.
          const stuck = await page.evaluate(() => {
            const markers = ["正在加载", "正在读取", "加载中", "Loading…", "loading…"];
            const visibleText = document.body.innerText || "";
            return markers.filter((m) => visibleText.includes(m));
          });
          if (stuck.length)
            add("major", route, "内容卡在加载态", `网络空闲后仍显示：${stuck.join(" / ")}`);

          // Empty-looking page (render failure). A page gated behind a form
          // legitimately shows little until the visitor authenticates.
          const render = await page.evaluate(() => ({
            length: (document.body.innerText || "").trim().length,
            gated: Boolean(document.querySelector("form")),
          }));
          const floor = render.gated ? 80 : 200;
          if (render.length < floor)
            add("critical", route, "页面几乎没有内容",
              `body 可见文本仅 ${render.length} 字符${render.gated ? "（门控页）" : ""}，疑似渲染失败。`);

          // axe must be injected as an inline script, which the page's own
          // CSP (correctly) forbids — so the a11y pass runs in a separate
          // bypassCSP context rather than weakening the CSP check above.
          if (axeSource) {
            const a11yContext = await browser.newContext({
              viewport: { width: viewport.width, height: viewport.height },
              bypassCSP: true,
            });
            const a11yPage = await a11yContext.newPage();
            try {
              await a11yPage.goto(url, { waitUntil: "networkidle", timeout: 30000 });
              await a11yPage.waitForTimeout(400);
              await a11yPage.addScriptTag({ content: axeSource });
              const axe = await a11yPage.evaluate(async () => {
                const result = await window.axe.run(document, {
                  runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] },
                });
                return result.violations.map((v) => ({
                  id: v.id, impact: v.impact, help: v.help,
                  nodes: v.nodes.slice(0, 3).map((n) => n.target.join(" ")),
                }));
              });
              for (const v of axe) {
                const severity = v.impact === "critical" || v.impact === "serious" ? "major" : "minor";
                add(severity, route, `无障碍：${v.id}`, `${v.help}（${v.impact}）→ ${v.nodes.join(" | ")}`);
              }
            } finally {
              await a11yContext.close();
            }
          }
        }

        // Horizontal overflow: the page body must never scroll sideways.
        const overflow = await page.evaluate(() => {
          const doc = document.documentElement;
          const scrollW = Math.max(doc.scrollWidth, document.body.scrollWidth);
          if (scrollW <= doc.clientWidth + 1) return null;
          const guilty = [];
          for (const el of document.querySelectorAll("body *")) {
            const rect = el.getBoundingClientRect();
            if (rect.width === 0) continue;
            if (rect.right > doc.clientWidth + 1 || rect.left < -1) {
              const style = getComputedStyle(el);
              // An element allowed to scroll internally is fine.
              if (style.overflowX === "auto" || style.overflowX === "scroll") continue;
              const parent = el.parentElement;
              if (parent) {
                const ps = getComputedStyle(parent);
                if (ps.overflowX === "auto" || ps.overflowX === "scroll") continue;
              }
              guilty.push(`${el.tagName.toLowerCase()}${el.className ? "." + String(el.className).split(" ").filter(Boolean)[0] : ""} (right=${Math.round(rect.right)})`);
            }
          }
          return { scrollW, clientW: doc.clientWidth, guilty: [...new Set(guilty)].slice(0, 5) };
        });
        if (overflow && overflow.guilty.length)
          add("major", route, `${viewport.name} 横向溢出`,
            `视口 ${viewport.width}px，文档宽 ${overflow.scrollW}px；越界元素：${overflow.guilty.join(", ")}`);

        // Tap targets, per WCAG 2.5.8 (AA): at least 24×24 CSS px, with the
        // spec's exception for links flowing inline inside a sentence.
        if (viewport.name === "phone") {
          const small = await page.evaluate(() => {
            const MIN = 24;
            const out = [];
            for (const el of document.querySelectorAll("a, button, [role=button], input, select")) {
              const r = el.getBoundingClientRect();
              if (r.width === 0 || r.height === 0) continue;
              if (r.height >= MIN && r.width >= MIN) continue;
              // WCAG 2.5.8 "Essential" exception: a chart declares
              // data-target-size="essential" when point size/position carries
              // the information itself and an equivalent data table exists.
              if (el.closest('[data-target-size="essential"]')) continue;
              // Inline exception: the target sits in a line of text.
              const parent = el.parentElement;
              const inlineInText =
                el.tagName === "A" &&
                getComputedStyle(el).display.startsWith("inline") &&
                parent &&
                (parent.textContent || "").trim().length > (el.textContent || "").trim().length;
              if (inlineInText) continue;
              const label = (el.innerText || el.getAttribute("aria-label") || el.tagName).trim().slice(0, 24);
              out.push(`${label} (${Math.round(r.width)}×${Math.round(r.height)})`);
            }
            return [...new Set(out)].slice(0, 8);
          });
          if (small.length)
            add("minor", route, "移动端点击目标过小", `低于 WCAG 2.5.8 的 24×24px：${small.join(", ")}`);
        }

        await context.close();
      }
      if (verbose) console.log(`  ✓ 已检查 ${route}`);
    }

    // 404 handling
    const context = await browser.newContext();
    const page = await context.newPage();
    const response = await page.goto(`http://127.0.0.1:${port}/definitely-not-a-real-page`, {
      waitUntil: "domcontentloaded",
    });
    if (response && response.status() !== 404)
      add("minor", "/404", "未知路径未返回 404", `返回了 HTTP ${response.status()}。`);
    const notFoundText = await page.evaluate(() => (document.body.innerText || "").trim().length);
    if (notFoundText < 40)
      add("major", "/404", "404 页面内容为空", "找不到页面时用户看到空白页。");
    await context.close();
  } finally {
    await browser.close();
    server.close();
  }

  return { skipped: null, findings };
}

export async function auditSite(options = {}) {
  const staticFindings = await staticAudit();
  const browser = await browserAudit(options);
  return {
    findings: [...staticFindings, ...browser.findings],
    browserSkipped: browser.skipped,
  };
}
