#!/usr/bin/env node
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const path = resolve(root, "site", "data", "devlog.json");
const args = process.argv.slice(2);
const options = {};
for (let index = 0; index < args.length; index += 2) {
  const key = args[index]?.replace(/^--/, "");
  if (key) options[key] = args[index + 1] || "";
}

const allowedTypes = new Set(["feature", "improvement", "security", "milestone"]);
if (!options.title || !options.summary) {
  throw new Error("Usage: npm run log:add -- --title \"标题\" --summary \"摘要\" [--type feature] [--items \"变化一|变化二\"] [--version v0.2.0] [--commit abc1234]");
}
const type = options.type || "improvement";
if (!allowedTypes.has(type)) throw new Error(`Unsupported log type: ${type}`);

const today = options.date || new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
}).format(new Date());
const slug = options.title.toLowerCase().replace(/[^a-z0-9\u4e00-\u9fff]+/g, "-").replace(/^-|-$/g, "").slice(0, 42) || "update";
const data = JSON.parse(await readFile(path, "utf8"));
const entry = {
  id: `${today}-${slug}`,
  date: today,
  type,
  version: options.version || "开发更新",
  title: options.title,
  summary: options.summary,
  items: (options.items || "").split("|").map((item) => item.trim()).filter(Boolean),
};
if (options.commit) entry.commit = options.commit;
if ((data.entries || []).some((item) => item.id === entry.id)) {
  throw new Error(`Log entry already exists: ${entry.id}`);
}
data.updated_at = new Date().toISOString();
data.entries = [entry, ...(data.entries || [])].sort((a, b) => b.date.localeCompare(a.date));
await writeFile(path, `${JSON.stringify(data, null, 2)}\n`);
console.log(`Added development log: ${entry.id}`);
