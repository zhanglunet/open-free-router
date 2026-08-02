import { cp, mkdir, readFile, rm } from "node:fs/promises";
import { resolve } from "node:path";

const packageRoot = resolve(import.meta.dirname, "..");
const repoRoot = resolve(packageRoot, "../..");
const target = resolve(packageRoot, "vendor/open-free-router");

await rm(target, { recursive: true, force: true });
await mkdir(target, { recursive: true });
for (const file of ["pyproject.toml", "README.md", "LICENSE"]) {
  await cp(resolve(repoRoot, file), resolve(target, file));
}
await cp(resolve(repoRoot, "src"), resolve(target, "src"), {
  recursive: true,
  filter: (source) => !source.split(/[\\/]/).some((part) =>
    part === "__pycache__" || part.endsWith(".egg-info") || part.endsWith(".pyc")
  ),
});

const registry = await readFile(resolve(target, "src/open_free_router/registry.default.yaml"), "utf8");
const credentialValue = registry.split(/\r?\n/).some((line) => {
  const match = line.match(/^\s*(?:api_key|token)\s*:\s*(.*?)\s*$/i);
  return match && !["", "''", '\"\"', "null", "~"].includes(match[1].toLowerCase());
});
if (credentialValue) {
  throw new Error("拒绝打包：默认注册表疑似包含凭据值");
}
console.log(`Prepared bundled Python source at ${target}`);
