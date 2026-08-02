import assert from "node:assert/strict";
import { access, readFile, readdir } from "node:fs/promises";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const manifest = JSON.parse(await readFile(resolve(root, "package.json"), "utf8"));
assert.equal(manifest.private, undefined, "发布包不能标记 private");
assert.equal(manifest.license, "MIT");
assert.equal(manifest.bin["open-free-router"], "bin/open-free-router.mjs");
for (const path of [
  "bin/open-free-router.mjs",
  "vendor/open-free-router/pyproject.toml",
  "vendor/open-free-router/src/open_free_router/registry.default.yaml",
]) await access(resolve(root, path));
async function list(path) {
  const entries = await readdir(path, { withFileTypes: true });
  return (await Promise.all(entries.map(async (entry) => {
    const child = resolve(path, entry.name);
    return entry.isDirectory() ? list(child) : [child];
  }))).flat();
}
const vendorFiles = await list(resolve(root, "vendor"));
assert.equal(vendorFiles.some((path) => /(?:__pycache__|\.pyc$|\.egg-info)/.test(path)), false, "包内不能包含构建缓存");
console.log("npm package structure verified");
