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
for (const marker of ["Codex CLI", "Codex 客户端", "一键安装", "/v1/responses", "discover --test --adopt"]) {
  if (!guideHtml.includes(marker)) {
    throw new Error(`Generated guide is missing required content: ${marker}`);
  }
}
if (/AIza[0-9A-Za-z_-]{30,}|sk-[0-9A-Za-z]{20,}/.test(html)) {
  throw new Error("Generated site contains a credential-like value");
}

console.log(`Built ${output} from ${source}`);
