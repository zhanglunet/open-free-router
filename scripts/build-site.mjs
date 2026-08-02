import { copyFile, cp, mkdir, readFile, rm } from "node:fs/promises";
import { resolve } from "node:path";

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
const brandHtml = await readFile(resolve(output, "brand", "index.html"), "utf8");
const architectureHtml = await readFile(resolve(output, "architecture", "index.html"), "utf8");
const statusHtml = await readFile(resolve(output, "status", "index.html"), "utf8");
const statusJs = await readFile(resolve(output, "status", "status.js"), "utf8");
const statusCss = await readFile(resolve(output, "status", "status.css"), "utf8");
const mapHtml = await readFile(resolve(output, "map", "index.html"), "utf8");
const logsHtml = await readFile(resolve(output, "logs", "index.html"), "utf8");
const logsJs = await readFile(resolve(output, "logs", "logs.js"), "utf8");
const storyHtml = await readFile(resolve(output, "stories", "free-model-port", "index.html"), "utf8");
const storyJs = await readFile(resolve(output, "stories", "free-model-port", "article.js"), "utf8");
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
if (!modelsJs.includes("key_url") || !modelsJs.includes("key_steps_zh")) {
  throw new Error("Model radar must render provider API-key guidance");
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
for (const marker of ["免费大模型", "真实实测", "本地优先", "复制朋友圈文案", "https://oaf.asia/"]) {
  if (!storyHtml.includes(marker) && !storyJs.includes(marker)) {
    throw new Error(`Generated recommendation story is missing required content: ${marker}`);
  }
}
await readFile(resolve(output, "assets", "map", "world-dots.svg"));
await readFile(resolve(output, "assets", "brand", "og-free-model-port-share.jpg"));
for (const page of [html, modelsHtml, guideHtml, brandHtml, architectureHtml, statusHtml, mapHtml, logsHtml, storyHtml]) {
  if (/<script(?![^>]*src=)[^>]*>[^<]/.test(page)) {
    throw new Error("Site pages must not contain inline scripts (CSP script-src 'self')");
  }
  if (!page.includes('src="/nav.js?')) {
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

console.log(`Built ${output} from ${source}`);
