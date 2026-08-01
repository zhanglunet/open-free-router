import test from "node:test";
import assert from "node:assert/strict";
import { installManifest } from "../worker/index.js";

test("install manifest uses the public audited script and isolated Codex profile", () => {
  const manifest = installManifest();
  assert.equal(manifest.installer, "https://oaf.asia/install.sh");
  assert.match(manifest.one_liner, /--codex/);
  assert.deepEqual(manifest.next, [
    "open-free-router setup",
    "open-free-router serve",
    "codex --profile open-free-router",
  ]);
});
