import assert from "node:assert/strict";
import test from "node:test";
import { cacheRoot, executable, findPython, pythonCandidates } from "../bin/open-free-router.mjs";

test("explicit npm cache path wins", () => {
  assert.match(cacheRoot({ OFR_NPM_HOME: "/tmp/ofr-test" }, "darwin"), /\/tmp\/ofr-test$/);
});

test("virtualenv executable is platform-aware", () => {
  assert.match(executable("/cache", "darwin"), /\/cache\/bin\/open-free-router$/);
  assert.match(executable("C:\\cache", "win32"), /Scripts\/open-free-router\.exe$/);
});

test("python discovery only accepts successful 3.11 check", () => {
  const calls = [];
  const result = findPython((command, args) => {
    calls.push([command, args]);
    return { status: command === "python" ? 0 : 2 };
  }, "darwin");
  assert.deepEqual(result, { command: "python", prefix: [] });
  assert.equal(calls.length, 2);
  assert.match(calls[0][1].at(-1), /3, 11/);
});

test("windows prefers py -3", () => {
  assert.deepEqual(pythonCandidates("win32")[0], ["py", ["-3"]]);
});
