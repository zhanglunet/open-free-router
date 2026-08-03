#!/usr/bin/env node
import { readFile, writeFile } from "node:fs/promises";
import { execSync } from "node:child_process";
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
// A commit hash cannot be known before the commit exists. Passing
// `--commit "$(git rev-parse --short HEAD)"` therefore records the PARENT,
// and site/logs/logs.js turns that into a public GitHub link pointing at an
// unrelated revision — it happened to five entries in a row before anyone
// noticed. Record the hash after committing (or amend), or leave it out.
if (options.commit) {
  const head = execSync("git rev-parse HEAD", { encoding: "utf8" }).trim();
  if (head.startsWith(options.commit.toLowerCase()) || options.commit.toLowerCase() === "head") {
    throw new Error(
      `--commit ${options.commit} is the current HEAD, so it cannot contain the change you are logging.\n` +
      "Omit --commit and add the hash after committing, or reference a pull request via the links field.",
    );
  }
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
