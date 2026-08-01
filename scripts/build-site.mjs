import { cp, mkdir, readFile, rm } from "node:fs/promises";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const source = resolve(root, "site");
const output = resolve(root, "web");

await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
await cp(source, output, { recursive: true });

const html = await readFile(resolve(output, "index.html"), "utf8");
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
if (/AIza[0-9A-Za-z_-]{30,}|sk-[0-9A-Za-z]{20,}/.test(html)) {
  throw new Error("Generated site contains a credential-like value");
}

console.log(`Built ${output} from ${source}`);
