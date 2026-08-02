#!/usr/bin/env node

import { spawn, spawnSync } from "node:child_process";
import { constants, existsSync, realpathSync } from "node:fs";
import { access, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const PACKAGE_VERSION = "0.3.0";

function cacheRoot(env = process.env, platform = process.platform) {
  if (env.OFR_NPM_HOME) return resolve(env.OFR_NPM_HOME);
  if (platform === "win32") return resolve(env.LOCALAPPDATA || tmpdir(), "open-free-router", "npm", PACKAGE_VERSION);
  return resolve(env.XDG_CACHE_HOME || resolve(homedir(), ".cache"), "open-free-router", "npm", PACKAGE_VERSION);
}

function executable(root, platform = process.platform) {
  return platform === "win32"
    ? resolve(root, "Scripts", "open-free-router.exe")
    : resolve(root, "bin", "open-free-router");
}

function pythonCandidates(platform = process.platform) {
  return platform === "win32"
    ? [["py", ["-3"]], ["python", []], ["python3", []]]
    : [["python3", []], ["python", []]];
}

function findPython(spawnSyncImpl = spawnSync, platform = process.platform) {
  const check = "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 2)";
  for (const [command, prefix] of pythonCandidates(platform)) {
    const result = spawnSyncImpl(command, [...prefix, "-c", check], { stdio: "ignore" });
    if (result.status === 0) return { command, prefix };
  }
  throw new Error("未找到 Python 3.11+。请先安装 Python：https://www.python.org/downloads/");
}

async function wait(milliseconds) { await new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds)); }

async function acquireLock(path, readyPath, commandPath) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (existsSync(readyPath) && existsSync(commandPath)) return false;
    try { await mkdir(path); return true; } catch (error) {
      if (error.code !== "EEXIST") throw error;
      await wait(250);
    }
  }
  throw new Error("等待 npm 引导锁超时；请删除缓存目录后重试。");
}

async function runChecked(command, args, options = {}) {
  await new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, { stdio: "inherit", ...options });
    child.on("error", reject);
    child.on("exit", (code, signal) => code === 0 ? resolvePromise() : reject(new Error(`安装步骤失败（${signal || `退出码 ${code}`}）`)));
  });
}

async function ensureInstalled({ env = process.env } = {}) {
  const root = cacheRoot(env);
  const commandPath = executable(root);
  const marker = resolve(root, ".ofr-npm-version");
  try {
    if ((await readFile(marker, "utf8")).trim() === PACKAGE_VERSION) {
      await access(commandPath, constants.X_OK);
      return commandPath;
    }
  } catch { /* bootstrap below */ }

  await mkdir(dirname(root), { recursive: true });
  const lock = `${root}.lock`;
  const ownsLock = await acquireLock(lock, marker, commandPath);
  if (!ownsLock) return executable(root);
  try {
    const python = findPython();
    await rm(root, { recursive: true, force: true });
    await runChecked(python.command, [...python.prefix, "-m", "venv", root]);
    const environmentPython = process.platform === "win32" ? resolve(root, "Scripts", "python.exe") : resolve(root, "bin", "python");
    await runChecked(environmentPython, ["-m", "pip", "install", "--disable-pip-version-check", resolve(PACKAGE_ROOT, "vendor", "open-free-router")]);
    await writeFile(marker, `${PACKAGE_VERSION}\n`, { mode: 0o644 });
    return executable(root);
  } catch (error) {
    await rm(root, { recursive: true, force: true });
    throw error;
  } finally {
    await rm(lock, { recursive: true, force: true });
  }
}

async function main() {
  const commandPath = await ensureInstalled();
  const child = spawn(commandPath, process.argv.slice(2), { stdio: "inherit", env: process.env });
  child.on("error", (error) => { console.error(error.message); process.exitCode = 1; });
  child.on("exit", (code, signal) => {
    if (signal) process.kill(process.pid, signal);
    else process.exitCode = code ?? 1;
  });
}

let invokedDirectly = false;
try { invokedDirectly = process.argv[1] && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url)); } catch { /* imported in tests */ }
if (invokedDirectly) main().catch((error) => { console.error(`open-free-router: ${error.message}`); process.exitCode = 1; });

export { cacheRoot, executable, findPython, pythonCandidates };
