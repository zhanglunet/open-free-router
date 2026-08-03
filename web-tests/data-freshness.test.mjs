import test from "node:test";
import assert from "node:assert/strict";

import {
  MAX_CATALOG_AGE_DAYS,
  REFRESH_MAX_AGE_DAYS,
  FUTURE_SKEW_TOLERANCE_MS,
  checkCatalogFreshness,
} from "../scripts/data-freshness.mjs";

const DAY = 86_400_000;
const ANCHOR = Date.parse("2026-08-03T00:00:00Z");

/* A minimal catalog of the shape the exporter produces. Tests mutate a copy. */
function good(overrides = {}) {
  return {
    schema_version: 2,
    generated_at: new Date(ANCHOR - DAY).toISOString(),
    status_as_of: new Date(ANCHOR - 2 * DAY).toISOString(),
    provider_count: 2,
    model_count: 3,
    providers: [
      { id: "a", models: [{ id: "m1" }, { id: "m2" }] },
      { id: "b", models: [{ id: "m3" }] },
    ],
    ...overrides,
  };
}

const check = (catalog) => checkCatalogFreshness(catalog, { anchorMs: ANCHOR });

test("a fresh, well-formed catalog passes", () => {
  const result = check(good());
  assert.equal(result.ok, true, result.errors.join("; "));
  assert.deepEqual(result.errors, []);
});

test("a catalog older than the threshold fails with an actionable message", () => {
  const result = check(good({ generated_at: new Date(ANCHOR - 30 * DAY).toISOString() }));
  assert.equal(result.ok, false);
  const joined = result.errors.join("\n");
  assert.match(joined, /30\.0 天/);
  assert.match(joined, new RegExp(`${MAX_CATALOG_AGE_DAYS} 天`));
  // A gate that cannot tell you how to clear it gets bypassed.
  assert.match(joined, /export-public-catalog\.py/);
});

test("a timestamp far in the future fails rather than reading as fresh", () => {
  const result = check(good({ generated_at: new Date(ANCHOR + 3 * DAY).toISOString() }));
  assert.equal(result.ok, false);
  assert.match(result.errors.join("\n"), /未来/);
});

test("a timestamp inside the skew tolerance still passes", () => {
  const result = check(good({ generated_at: new Date(ANCHOR + FUTURE_SKEW_TOLERANCE_MS / 2).toISOString() }));
  assert.equal(result.ok, true, result.errors.join("; "));
});

test("an unparseable generated_at fails", () => {
  const result = check(good({ generated_at: "not-a-date" }));
  assert.equal(result.ok, false);
  assert.match(result.errors.join("\n"), /generated_at/);
});

test("a previous schema generation fails even when the timestamp is fresh", () => {
  // This is the case an age-only gate misses: today's published catalog was
  // 1.54 days old — green on age — while being schema_version 1.
  const result = check(good({ schema_version: 1 }));
  assert.equal(result.ok, false);
  assert.match(result.errors.join("\n"), /schema_version/);
});

test("provider_count that disagrees with providers.length fails", () => {
  const result = check(good({ provider_count: 9 }));
  assert.equal(result.ok, false);
  assert.match(result.errors.join("\n"), /provider_count/);
});

test("model_count that disagrees with the models actually present fails", () => {
  const result = check(good({ model_count: 99 }));
  assert.equal(result.ok, false);
  assert.match(result.errors.join("\n"), /model_count/);
});

test("an empty catalog fails", () => {
  // Registry.load swallows a missing file, so a typo in --registry used to
  // publish 0 providers / 0 models with a brand-new generated_at.
  const result = check({ schema_version: 2, generated_at: new Date(ANCHOR).toISOString(), provider_count: 0, model_count: 0, providers: [] });
  assert.equal(result.ok, false);
  assert.match(result.errors.join("\n"), /空/);
});

test("status_as_of age is reported as information, never as an error", () => {
  // The build cannot refresh availability evidence — no network, and ci.yml's
  // node job has no Python — so failing on it would block work it cannot help.
  const result = check(good({ status_as_of: new Date(ANCHOR - 90 * DAY).toISOString() }));
  assert.equal(result.ok, true, result.errors.join("; "));
  assert.match(result.info.join("\n"), /status_as_of/);
});

test("the refresh cadence always beats the gate", () => {
  // If the scheduled refresh could ever run later than the gate's threshold,
  // main would go red for every unrelated PR on a calendar date.
  assert.ok(
    REFRESH_MAX_AGE_DAYS < MAX_CATALOG_AGE_DAYS,
    `REFRESH_MAX_AGE_DAYS (${REFRESH_MAX_AGE_DAYS}) must be < MAX_CATALOG_AGE_DAYS (${MAX_CATALOG_AGE_DAYS})`,
  );
});
