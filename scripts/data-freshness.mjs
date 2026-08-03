/**
 * Freshness and shape gate for the published catalog snapshot.
 *
 * Why shape and not only age: on the day this was written the published
 * `site/data/catalog.json` was 1.54 days old — comfortably inside any age
 * threshold — while being `schema_version: 1`, a whole schema generation behind
 * what the exporter emits. An age-only gate would have shipped green having
 * fixed nothing. The schema and count assertions are what make it red when the
 * artifact is actually wrong.
 *
 * Why the age is anchored to the commit and not the wall clock: `git bisect`
 * reads exit code 1 as "bad" and this build never emits 125, so a wall-clock
 * gate would silently corrupt a bisect of an unrelated bug once the catalog
 * aged out; the same applies to rebuilding an old tag or re-running CI on an
 * old commit. Anchoring to `min(now, HEAD committer date)` means a 30-day-old
 * commit carrying a 30-day-old catalog passes, while today's commit carrying a
 * 30-day-old catalog fails. That is the case the PRD actually asks for, and it
 * removes every scenario an --allow-stale escape hatch would have existed for.
 *
 * `status_as_of` is deliberately never fatal here: the build has no network by
 * project non-goal and ci.yml's node job has no Python, so a build could not
 * clear such a failure. Availability evidence is enforced by the scheduled
 * workflows instead (see docs/superpowers/specs/2026-08-03-m2-data-freshness-design.md §5.4).
 */

/** Beyond this the published catalog must not ship. */
export const MAX_CATALOG_AGE_DAYS = 7;

/**
 * The scheduled refresh republishes at this age. It MUST stay strictly below
 * MAX_CATALOG_AGE_DAYS — otherwise the renewal loses the race with the gate and
 * main goes red for every unrelated PR on a calendar date. Asserted in
 * web-tests/data-freshness.test.mjs.
 */
export const REFRESH_MAX_AGE_DAYS = 3;

/** Runner clocks drift; a genuinely forged timestamp does not drift by a day. */
export const FUTURE_SKEW_TOLERANCE_MS = 24 * 60 * 60 * 1000;

const DAY_MS = 86_400_000;
const EXPORT_COMMAND =
  "python scripts/export-public-catalog.py --registry src/open_free_router/registry.default.yaml " +
  '--now "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"';

const days = (ms) => (ms / DAY_MS).toFixed(1);

/**
 * @param {object} catalog parsed site/data/catalog.json
 * @param {{anchorMs: number}} options anchor is min(now, HEAD committer date)
 * @returns {{ok: boolean, errors: string[], info: string[]}}
 */
export function checkCatalogFreshness(catalog, { anchorMs }) {
  const errors = [];
  const info = [];

  const generatedAt = Date.parse(catalog?.generated_at ?? "");
  if (!Number.isFinite(generatedAt)) {
    errors.push(`generated_at 无法解析：${JSON.stringify(catalog?.generated_at ?? null)}`);
  } else if (generatedAt - anchorMs > FUTURE_SKEW_TOLERANCE_MS) {
    errors.push(
      `generated_at 位于未来 ${days(generatedAt - anchorMs)} 天（${catalog.generated_at}），` +
        "超出 24 小时时钟偏移容忍度；这通常意味着时间戳被手工编辑过。",
    );
  } else {
    const age = Math.max(0, anchorMs - generatedAt);
    if (age > MAX_CATALOG_AGE_DAYS * DAY_MS) {
      errors.push(
        `公开目录快照已过期 ${days(age)} 天（阈值 ${MAX_CATALOG_AGE_DAYS} 天，generated_at=${catalog.generated_at}）。` +
          `\n    重新导出：${EXPORT_COMMAND}`,
      );
    } else {
      info.push(`generated_at 距锚点 ${days(age)} 天（阈值 ${MAX_CATALOG_AGE_DAYS} 天）`);
    }
  }

  if (catalog?.schema_version !== 2) {
    errors.push(
      `schema_version 是 ${JSON.stringify(catalog?.schema_version ?? null)}，导出器产出 2。` +
        `\n    已发布产物落后一代 schema，年龄门禁抓不到这种漂移。重新导出：${EXPORT_COMMAND}`,
    );
  }

  const providers = Array.isArray(catalog?.providers) ? catalog.providers : [];
  if (!providers.length) {
    errors.push(
      "公开目录是空的（0 个提供商）。Registry.load 会吞掉不存在的注册表文件，" +
        `所以一个拼错的 --registry 曾经能导出 0/0 并退出 0。重新导出：${EXPORT_COMMAND}`,
    );
  } else {
    const modelTotal = providers.reduce((total, provider) => total + (provider?.models?.length ?? 0), 0);
    if (catalog.provider_count !== providers.length) {
      errors.push(`provider_count=${catalog.provider_count} 与实际 ${providers.length} 个提供商不符`);
    }
    if (catalog.model_count !== modelTotal) {
      errors.push(`model_count=${catalog.model_count} 与实际 ${modelTotal} 个模型不符`);
    }
    if (!modelTotal) errors.push("公开目录里一个模型都没有");
  }

  const statusAsOf = Date.parse(catalog?.status_as_of ?? "");
  info.push(
    Number.isFinite(statusAsOf)
      ? `status_as_of 距锚点 ${days(Math.max(0, anchorMs - statusAsOf))} 天（仅提示，不阻断构建）`
      : "status_as_of 缺失或无法解析（仅提示，不阻断构建）",
  );

  return { ok: errors.length === 0, errors, info };
}
