#!/usr/bin/env node
/**
 * Whole-site audit runner.
 *
 *   npm run test:site              # static + browser checks, fails on critical/major
 *   npm run test:site -- --json    # machine-readable report
 *   npm run test:site -- --static  # skip the browser layer
 *
 * Exit code is non-zero when any critical or major finding remains, so this
 * can gate a deploy. Minor findings are reported but do not fail the run.
 */
import { auditSite, staticAudit } from "../web-tests/site-audit.mjs";

const args = new Set(process.argv.slice(2));
const asJson = args.has("--json");
const staticOnly = args.has("--static");

const result = staticOnly
  ? { findings: await staticAudit(), browserSkipped: "已通过 --static 跳过" }
  : await auditSite({ verbose: !asJson });

const order = { critical: 0, major: 1, minor: 2 };
const findings = result.findings.sort(
  (a, b) => order[a.severity] - order[b.severity] || a.page.localeCompare(b.page),
);

if (asJson) {
  console.log(JSON.stringify({ ...result, findings }, null, 2));
} else {
  const counts = findings.reduce((acc, f) => ({ ...acc, [f.severity]: (acc[f.severity] || 0) + 1 }), {});
  console.log("\n=== 全站审计 ===");
  if (result.browserSkipped) console.log(`浏览器层已跳过：${result.browserSkipped}`);
  if (!findings.length) {
    console.log("✔ 未发现问题");
  } else {
    let currentPage = null;
    for (const f of findings) {
      if (f.page !== currentPage) {
        currentPage = f.page;
        console.log(`\n${currentPage}`);
      }
      const mark = f.severity === "critical" ? "✗" : f.severity === "major" ? "⚠" : "·";
      console.log(`  ${mark} [${f.severity}/${f.layer}] ${f.title}`);
      console.log(`      ${f.detail}`);
    }
    console.log(
      `\n合计：${counts.critical || 0} critical · ${counts.major || 0} major · ${counts.minor || 0} minor`,
    );
  }
}

const blocking = findings.filter((f) => f.severity === "critical" || f.severity === "major");
process.exit(blocking.length ? 1 : 0);
