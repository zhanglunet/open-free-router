# M2 实施计划：公开目录数据新鲜度

> Superpowers `writing-plans` skill 要求：任务切到 2-5 分钟粒度，每项都给出确切
> 文件路径、先写的失败测试、具体改动和可执行的验证命令，不允许出现 TBD 或
> 「参照第 N 项」。设计依据见
> `docs/superpowers/specs/2026-08-03-m2-data-freshness-design.md`。

**目标**：让访客看到的可用性与免费证据要么新鲜、要么被明确标注为过期，并让这个
性质无法再次漂移。

**架构**：四层——L1 Worker 时效闸门、L2 前端无条件降级、L3 构建期形状+年龄门禁、
L4 可复现性 diff。

**技术栈**：Python 3.11+（导出器）、Node 22 ESM（构建与门禁）、原生浏览器 JS
（站点）、GitHub Actions。

## 全局约束

- 不引入前端框架或打包器；不放宽 CSP；站点保持静态 HTML + 原生 CSS/JS
- 任何前端文件里**不得**出现新鲜度年龄常量（兜底分支无条件降级）
- 站点 JS **不得**新增跨文件引用（`build-site.mjs` 先哈希后重写引用，且
  `_headers` 把 `/*.js` 标为一年 immutable → 漂移即硬 404）
- 每项先写会失败的测试并**亲眼看它失败**，再写实现（TDD 铁律）

**依赖顺序要点**：W4 必须与 W5 同一提交（否则 Google 变黑）；W9 先于 W10 先于 W11；
W15 先于 W16。

---

## W1 · _speed_tier must say 未测 for unverified, not 当前不可用

**文件**

- `src/open_free_router/public_catalog.py`
- `tests/test_public_catalog.py`

**RED — 先写、并亲眼看它失败**

```
Append to tests/test_public_catalog.py:

from open_free_router.public_catalog import _speed_tier

def test_speed_tier_does_not_assert_unavailability_without_evidence():
    assert _speed_tier("unverified", None) == "未测"
    assert _speed_tier("unverified", 100) == "未测"
    assert _speed_tier("unavailable", None) == "当前不可用"

Run `.venv/bin/python -m pytest tests/test_public_catalog.py -q` and watch it fail with `AssertionError: assert '当前不可用' == '未测'`.
```

**GREEN — 改动**

In public_catalog.py replace the body head of _speed_tier (lines 53-57) with: `if availability == "unavailable": return "当前不可用"` then `if availability != "available": return "未测"` then the existing `if latency_ms is None: return "未测"` and latency bands. Only 'unavailable' may produce the affirmative claim.

**验证**

```
.venv/bin/python -m pytest tests/test_public_catalog.py -q  →  expect a line ending in `passed` and zero failures (currently 4 tests in this file, so `5 passed`).
```

---

## W2 · Injectable clock on build_public_catalog

**文件**

- `src/open_free_router/public_catalog.py`
- `tests/test_public_catalog.py`

**RED — 先写、并亲眼看它失败**

```
Append to tests/test_public_catalog.py:

from datetime import datetime, timezone

def test_public_catalog_accepts_an_injected_clock():
    reg = Registry({"p": {"upstream_url": "https://api.example/v1", "prefix": "p", "models": [{"id": "m"}]}})
    pinned = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    out = build_public_catalog(reg, {"as_of": "2020-01-01T00:00:00Z"}, None, now=pinned)
    assert out["generated_at"] == "2020-01-02T03:04:05+00:00"

Run the file and watch it fail with `TypeError: build_public_catalog() takes from 2 to 3 positional arguments but 4 were given`.
```

**GREEN — 改动**

Change the signature at public_catalog.py:65 to `def build_public_catalog(registry: Registry, status_snapshot: dict, provider_profiles: dict | None = None, now: datetime | None = None) -> dict:` and line 70 to `generated_at = now or datetime.now(timezone.utc)`. Nothing else moves — lines 78 and 83 already pass `now=generated_at` into free_tier.to_dict, so pinning one value makes the whole export deterministic.

**验证**

```
.venv/bin/python -m pytest tests/test_public_catalog.py -q  →  `6 passed`.
```

---

## W3 · --now flag on the exporter, making re-export byte-reproducible

**文件**

- `scripts/export-public-catalog.py`

**RED — 先写、并亲眼看它失败**

```
Run this and watch it fail:

SP=/tmp/claude-0/-home-user-open-free-router/361488ae-6ba2-5757-8260-31a2ab2d1c54/scratchpad
.venv/bin/python scripts/export-public-catalog.py --registry src/open_free_router/registry.default.yaml --now 2026-08-03T00:00:00+00:00 --output $SP/a.json

Expected RED: `error: unrecognized arguments: --now 2026-08-03T00:00:00+00:00` and exit 2.
```

**GREEN — 改动**

Add `parser.add_argument("--now", type=lambda v: datetime.fromisoformat(v), default=None, help="Pin generated_at (ISO-8601) for reproducible exports")` plus `from datetime import datetime`, and pass `now=args.now` into build_public_catalog at line 21.

**验证**

```
SP=/tmp/claude-0/-home-user-open-free-router/361488ae-6ba2-5757-8260-31a2ab2d1c54/scratchpad; for f in a b; do .venv/bin/python scripts/export-public-catalog.py --registry src/open_free_router/registry.default.yaml --now 2026-08-03T00:00:00+00:00 --output $SP/$f.json; done; diff $SP/a.json $SP/b.json && echo REPRODUCIBLE  →  expect two `Exported 11 providers / 55 models` lines and then `REPRODUCIBLE` with no diff output.
```

---

## W4 · Delete the google-ai-studio probe special case (must precede regeneration)

**文件**

- `worker/probe.js`
- `web-tests/server-probe.test.mjs`

**RED — 先写、并亲眼看它失败**

```
In web-tests/server-probe.test.mjs rewrite the test at lines 49-66: set `api: "https://generativelanguage.googleapis.com/v1beta/openai"` and assert `captured.url === "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"`, `captured.options.headers.Authorization === "Bearer google-private-key"`, and `captured.options.headers["X-Goog-Api-Key"] === undefined`. Run `node --test web-tests/server-probe.test.mjs` and watch it fail on the url assertion with actual `.../v1beta/openai/models/gemini-test:generateContent`.
```

**GREEN — 改动**

Delete worker/probe.js lines 42-59 (the `if (provider.id === "google-ai-studio")` block). Google then takes the generic OpenAI-compatible path: `${base}/chat/completions` with `Authorization: Bearer`. Verified live: `/v1beta/openai/chat/completions` → 400 (exists), `/v1beta/openai/models/X:generateContent` → 404, and probe.js:88 maps 404 → unavailable.

**验证**

```
node --test web-tests/server-probe.test.mjs  →  expect `# fail 0` in the TAP summary.
```

---

## W5 · Regenerate site/data/catalog.json (schema 1→2) — reviewed data commit

**文件**

- `site/data/catalog.json`

**RED — 先写、并亲眼看它失败**

```
Run and watch it fail:

node -e 'const c=require("./site/data/catalog.json"); if(c.schema_version!==2) throw new Error(`schema_version is ${c.schema_version}, exporter emits 2`); if(!c.providers.every(p=>p.free_tier)) throw new Error("providers missing free_tier");'

Expected RED: `Error: schema_version is 1, exporter emits 2`.
```

**GREEN — 改动**

Run the exporter against the in-repo default registry with a pinned clock and write it over the committed catalog. This is a 562-line semantic diff: schema_version 1→2, free_tier on all 11 providers, free_tier + free_availability on all 55 models, longer status_note, and google-ai-studio.api → .../v1beta/openai. It must be reviewed as a data change and must be in the same commit as W4.

**验证**

```
.venv/bin/python scripts/export-public-catalog.py --registry src/open_free_router/registry.default.yaml --now "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" && node -e 'const c=require("./site/data/catalog.json"); const g=c.providers.find(p=>p.id==="google-ai-studio"); console.log(c.schema_version, c.provider_count, c.model_count, g.api);'  →  expect `Exported 11 providers / 55 models to site/data/catalog.json` then `2 11 55 https://generativelanguage.googleapis.com/v1beta/openai`.
```

---

## W6 · Reproducibility check in CI's python job (makes the gate non-forgeable)

**文件**

- `.github/workflows/ci.yml`

**RED — 先写、并亲眼看它失败**

```
Prove the check catches drift before wiring it. Run:

SP=/tmp/claude-0/-home-user-open-free-router/361488ae-6ba2-5757-8260-31a2ab2d1c54/scratchpad
cp site/data/catalog.json $SP/orig.json
python - <<'EOF'
import json,pathlib
p=pathlib.Path('site/data/catalog.json'); d=json.loads(p.read_text())
d['providers'][0]['availability']='available'; d['provider_count']=99
p.write_text(json.dumps(d,ensure_ascii=False,indent=2)+"\n")
EOF
NOW=$(.venv/bin/python -c "import json;print(json.load(open('site/data/catalog.json'))['generated_at'])"); .venv/bin/python scripts/export-public-catalog.py --registry src/open_free_router/registry.default.yaml --now "$NOW" --output $SP/check.json; git diff --no-index --stat site/data/catalog.json $SP/check.json

Expected RED: a non-empty diffstat and exit 1. Then `cp $SP/orig.json site/data/catalog.json`.
```

**GREEN — 改动**

Add a step to the `python` job after `Test`:

      - name: Public catalog is reproducible from the registry
        run: |
          NOW=$(python -c "import json;print(json.load(open('site/data/catalog.json'))['generated_at'])")
          python scripts/export-public-catalog.py --registry src/open_free_router/registry.default.yaml --now "$NOW" --output /tmp/catalog.check.json
          git diff --no-index --stat site/data/catalog.json /tmp/catalog.check.json \
            || { echo '::error::site/data/catalog.json is not what the exporter produces. Re-run: python scripts/export-public-catalog.py --registry src/open_free_router/registry.default.yaml'; exit 1; }

**验证**

```
SP=/tmp/claude-0/-home-user-open-free-router/361488ae-6ba2-5757-8260-31a2ab2d1c54/scratchpad; NOW=$(.venv/bin/python -c "import json;print(json.load(open('site/data/catalog.json'))['generated_at'])"); .venv/bin/python scripts/export-public-catalog.py --registry src/open_free_router/registry.default.yaml --now "$NOW" --output $SP/check.json && git diff --no-index site/data/catalog.json $SP/check.json && echo IDENTICAL  →  expect `IDENTICAL` with no diff output.
```

---

## W7 · Worker: derive the provider verdict from surviving model evidence

**文件**

- `worker/probe.js`
- `web-tests/server-probe.test.mjs`

**RED — 先写、并亲眼看它失败**

```
Add to web-tests/server-probe.test.mjs:

test("a stale KV snapshot cannot keep a provider green", () => {
  const now = Date.parse("2026-08-03T00:00:00Z");
  const old = new Date(now - 3 * 86400000).toISOString();
  const catalog = { providers: [{ id: "p", models: [{ id: "m" }] }] };
  const snap = { schema_version: 2, as_of: old,
    providers: { p: { availability: "available", reason: "已验证 1/1", latency_ms: 900, checked_at: old } },
    models: { "p/m": { availability: "available", checked_at: old, latency_ms: 900 } } };
  const merged = mergeServerStatus(catalog, snap, now);
  assert.equal(merged.providers[0].models[0].availability, "unverified");
  assert.equal(merged.providers[0].availability, "unverified");
  assert.equal(merged.providers[0].latency_ms, null);
});

Run `node --test web-tests/server-probe.test.mjs` and watch it fail: model is already `unverified` but provider asserts `available` with `latency_ms: 900`.
```

**GREEN — 改动**

In mergeServerStatus (probe.js:226-245): after building `models`, stop reading `summary` for the verdict. Compute from the already-recency-filtered `models`: `const ok = models.filter(m => m.availability === "available"); const bad = models.filter(m => m.availability === "unavailable");` then availability = ok.length ? "available" : bad.length ? "unavailable" : "unverified"; reason = ok.length ? `Cloudflare 服务器已验证 ${ok.length}/${models.length} 个模型可用` : bad.length ? bad[0].reason : "等待 Cloudflare 服务器首次探测"; latency_ms = ok.length ? Math.min(...ok.map(m=>m.latency_ms).filter(Number.isFinite)) : null; checked_at = models.map(m=>m.checked_at).filter(Boolean).sort().at(-1) || "". Provider and model verdicts then cannot disagree. Add a comment block naming all seven staleness clocks and cross-referencing scripts/data-freshness.mjs.

**验证**

```
node --test web-tests/server-probe.test.mjs  →  `# fail 0`.
```

---

## W8 · Worker: status_source and status_note must not claim live measurement on a KV miss

**文件**

- `worker/probe.js`
- `web-tests/server-probe.test.mjs`

**RED — 先写、并亲眼看它失败**

```
Add to web-tests/server-probe.test.mjs:

test("a KV miss must not advertise a Cloudflare measurement", () => {
  const merged = mergeServerStatus({ providers: [] }, null, Date.now());
  assert.notEqual(merged.status_source, "cloudflare-server-probe");
  assert.ok(!merged.status_note.includes("实测"));
});

Run and watch both assertions fail — status_source is unconditionally "cloudflare-server-probe" and the note contains 最小请求实测.
```

**GREEN — 改动**

In mergeServerStatus's return (probe.js:246-254) make both fields conditional on `live`: `status_source: live ? "cloudflare-server-probe" : "server-probe-pending"` and `status_note: live ? "可用性来自 Cloudflare 服务器端最小请求实测，与访问者本机无关；它是最近快照，不构成 SLA。" : "Cloudflare 服务器探测尚无有效快照，本页不判断可用性。"`. status.js:120 already branches on status_source === "cloudflare-server-probe", so it falls to the honest 目录生成于… line automatically.

**验证**

```
node --test web-tests/server-probe.test.mjs  →  `# fail 0`.
```

---

## W9 · scripts/data-freshness.mjs — the pure gate function

**文件**

- `scripts/data-freshness.mjs`
- `web-tests/data-freshness.test.mjs`

**RED — 先写、并亲眼看它失败**

```
Create web-tests/data-freshness.test.mjs first, importing `{ checkCatalogFreshness, MAX_CATALOG_AGE_DAYS, REFRESH_MAX_AGE_DAYS } from "../scripts/data-freshness.mjs"`, with a `good()` helper building a valid schema-2 catalog and cases: (1) fresh → ok true; (2) generated_at 30 days before anchor → ok false and an error matching /export-public-catalog/; (3) 25h in the future → ok false; (4) 1h in the future → ok true; (5) schema_version 1 → ok false; (6) provider_count 99 → ok false; (7) generated_at "not-a-date" → ok false; (8) stale status_as_of → ok true with a non-empty info array; (9) `assert.ok(REFRESH_MAX_AGE_DAYS < MAX_CATALOG_AGE_DAYS)` — this one encodes the critic's fatal defect so the refresh window can never stop beating the gate. Run `node --test web-tests/data-freshness.test.mjs` and watch every case fail with `Cannot find module`.
```

**GREEN — 改动**

Create scripts/data-freshness.mjs exporting `MAX_CATALOG_AGE_DAYS = 7`, `REFRESH_MAX_AGE_DAYS = 3`, `FUTURE_SKEW_TOLERANCE_MS = 24*3600*1000`, and a pure `checkCatalogFreshness(catalog, { anchorMs })` returning `{ ok, errors: string[], info: string[] }`. Rules, in order: generated_at parseable; generated_at not more than 24h beyond anchorMs; `(anchorMs - generated_at) <= 7d`; `schema_version === 2`; `provider_count === providers.length` and > 0; `model_count === Σ providers[].models.length` and > 0. status_as_of: parseable → push its age to `info`; never an error. Every error string must name the fix: `python scripts/export-public-catalog.py --registry src/open_free_router/registry.default.yaml`. No I/O, no git, no Date.now() inside the function.

**验证**

```
node --test web-tests/data-freshness.test.mjs  →  `# pass 9` and `# fail 0`.
```

---

## W10 · Wire the gate into build-site.mjs with commit-time anchoring

**文件**

- `scripts/build-site.mjs`

**RED — 先写、并亲眼看它失败**

```
Run and watch it fail:

SP=/tmp/claude-0/-home-user-open-free-router/361488ae-6ba2-5757-8260-31a2ab2d1c54/scratchpad
cp site/data/catalog.json $SP/orig.json
node -e 'const fs=require("fs");const p="site/data/catalog.json";const c=JSON.parse(fs.readFileSync(p));c.generated_at=new Date(Date.now()-30*86400000).toISOString();fs.writeFileSync(p,JSON.stringify(c,null,2)+"\n")'
npm run build; echo "exit=$?"

Expected RED: `exit=0` (the build currently ignores catalog.json entirely). Restore with `cp $SP/orig.json site/data/catalog.json`.
```

**GREEN — 改动**

At the top add `import { execFileSync } from "node:child_process";` and `import { checkCatalogFreshness } from "./data-freshness.mjs";`. Beside the devlog assertions (after line 120) insert: read+parse `web/data/catalog.json`; compute `let anchorMs = Date.now(), anchorLabel = "wall clock"; try { const t = Date.parse(execFileSync("git", ["log", "-1", "--format=%cI"], { cwd: root, encoding: "utf8", stdio: ["ignore","pipe","ignore"] }).trim()); if (Number.isFinite(t)) { anchorMs = Math.min(anchorMs, t); anchorLabel = "HEAD committer date"; } } catch {}`; call the checker; `console.log` each info line prefixed with the anchor label; `throw new Error("公开目录数据新鲜度校验失败：\n" + errors.join("\n"))` if not ok. Anchoring is what keeps git bisect, tag rebuilds and old-commit CI re-runs green.

**验证**

```
Repeat the doctoring above, then `npm run build; echo "exit=$?"` → expect a message containing `公开目录数据新鲜度校验失败` and `export-public-catalog.py`, and `exit=1`. Then `cp $SP/orig.json site/data/catalog.json && npm run build` → expect `Built /home/user/open-free-router/web ... assets fingerprinted` and exit 0.
```

---

## W11 · PRD acceptance criterion as the last step of ci.yml's node job

**文件**

- `.github/workflows/ci.yml`

**RED — 先写、并亲眼看它失败**

```
Before editing ci.yml, run the step's body verbatim in the shell against the CURRENT build and watch it fail:

cp site/data/catalog.json /tmp/orig.json; node -e '...same doctoring...'; if npm run build; then echo 'NO GATE'; fi; cp /tmp/orig.json site/data/catalog.json

Expected RED before W10: `NO GATE`.
```

**GREEN — 改动**

Append AFTER `Worker dry-run` (it must be last: build-site.mjs does `rm -rf web/` first, so a deliberately-failing build leaves web/ half-written and both `Static site audit` and `Worker dry-run` consume web/):

      - name: Freshness gate fails on a 30-day-old catalog
        run: |
          node -e 'const fs=require("fs");const p="site/data/catalog.json";const c=JSON.parse(fs.readFileSync(p));c.generated_at=new Date(Date.now()-30*86400000).toISOString();fs.writeFileSync(p,JSON.stringify(c,null,2)+"\n")'
          if npm run build; then echo '::error::build succeeded with a 30-day-old catalog'; exit 1; fi
          echo 'gate fired as expected'

      - name: Restore catalog
        if: always()
        run: git checkout -- site/data/catalog.json

**验证**

```
After W10, run the same body: expect `gate fired as expected` and no `NO GATE`. Then `git checkout -- site/data/catalog.json && git status --porcelain site/data/catalog.json` → expect empty output.
```

---

## W12 · status.js must never render the export clock as 最近检查

**文件**

- `site/status/status.js`
- `scripts/build-site.mjs`

**RED — 先写、并亲眼看它失败**

```
Add the build-site.mjs assertion FIRST, then run `npm run build` and watch it fail with `Error: Status board must not present the catalog export time as an availability check time`.
```

**GREEN — 改动**

status.js:65 becomes `$("#st-stat-asof").textContent = catalog.status_as_of ? relativeTime(catalog.status_as_of) : "未验证";`. The element is labelled 最近检查 (site/status/index.html:55) and metadataOnly() blanks status_as_of at :128, so today the fallback board always attributes generated_at to an availability check that never ran. Add to build-site.mjs after line 99: `if (statusJs.includes("catalog.status_as_of || catalog.generated_at")) throw new Error("Status board must not present the catalog export time as an availability check time");`

**验证**

```
npm run build  →  exit 0 with the `Built ...` line; then `grep -c 'catalog.status_as_of || catalog.generated_at' site/status/status.js` → expect `0`.
```

---

## W13 · models.js: unconditional degradation on the static-fallback branch

**文件**

- `site/models/models.js`
- `site/models/index.html`
- `scripts/build-site.mjs`

**RED — 先写、并亲眼看它失败**

```
Add the build-site.mjs marker assertions FIRST, then `npm run build` and watch it fail with `Generated model radar is missing required content: 静态兜底数据`.
```

**GREEN — 改动**

In models.js add a local `function relativeDays(iso)` (days-ago string, or 未知) and `function degradeCatalog(catalog)` mirroring status.js's metadataOnly(): every provider and model → availability "unverified", latency_ms null, checked_at "", reason "等待服务器探测恢复". In the catch branch at :161-171 apply `catalog = degradeCatalog(catalog); degraded = true;` BEFORE `flatten()` at :173 (flatten copies provider.availability and latency_ms onto every row at :49-50). Then at :176-177: when degraded, `$("#available-count").textContent = "未验证"` and `$("#status-time").textContent = \`静态兜底数据，最后更新 ${relativeDays(catalog.generated_at)} · 不代表当前可用性\``; otherwise keep today's strings. In models/index.html:68 add `<option value="unverified">待验证</option>` to #status-filter — after neutralization the 可用 filter matches zero rows with no way to see them. In build-site.mjs extend the modelsJs marker loop at :58-62 with `["静态兜底数据", "function degradeCatalog", "不代表当前可用性"]` and assert modelsHtml includes `value="unverified"`.

**验证**

```
npm run build  →  exit 0; then `grep -c '静态兜底数据\|function degradeCatalog' site/models/models.js` → expect `2` or more, and `grep -c 'value="unverified"' site/models/index.html` → expect `1`.
```

---

## W14 · benchmarks.js: degrade its fallback and grow a freshness line

**文件**

- `site/benchmarks/benchmarks.js`
- `site/benchmarks/index.html`
- `scripts/build-site.mjs`

**RED — 先写、并亲眼看它失败**

```
Add the build-site.mjs assertion FIRST: extend the loop at :146 with `"静态兜底数据"` and `"function degradeCatalog"`, plus `if (!benchmarksHtml.includes('id="bench-freshness"')) throw new Error("Benchmarks page must render catalog freshness");`. Run `npm run build` and watch it fail with `Benchmarks page must render catalog freshness`.
```

**GREEN — 改动**

benchmarks.js:147 currently hides the fallback inside a Promise.all chain and the page renders NO freshness timestamp anywhere while weighting stale provider_status 35% and latency_ms 10% (:26-36). Restructure into an explicit `let degraded = false;` with a named async catalog loader that sets the flag on the /data/catalog.json path. Add the same local `relativeDays()` + `degradeCatalog()` (identical to W13 — duplicated deliberately, see the fingerprint-ordering note; a build assertion keeps them in step). Apply degradeCatalog BEFORE flatten() at :151 (flatten computes readiness at :55). Add `<p id="bench-freshness"></p>` to site/benchmarks/index.html next to #external-status, and set it in load(): degraded → `静态兜底数据，最后更新 X 天前 · 服务可用性未验证，任务适配分中的服务分按未验证计`, else → `可用性快照：<relative> · 状态不是 SLA`. Add both files' markers to build-site.mjs's benchmarks loop at :141-150.

**验证**

```
npm run build  →  exit 0; then `grep -c 'bench-freshness' site/benchmarks/index.html site/benchmarks/benchmarks.js` → expect `1` from each file.
```

---

## W15 · startServer() must be able to fail /api/catalog

**文件**

- `web-tests/site-audit.mjs`
- `web-tests/data-freshness.test.mjs`

**RED — 先写、并亲眼看它失败**

```
Add to web-tests/data-freshness.test.mjs:

import { startServer } from "./site-audit.mjs";
test("the audit server can simulate a degraded /api/catalog", async () => {
  const s = await startServer(undefined, { apiCatalog: "fail" });
  const r = await fetch(`${s.origin}/api/catalog`);
  assert.equal(r.status, 503);
  await s.close();
});

(Match the actual return shape of startServer — read lines 310-330 for the origin/close names.) Run `node --test web-tests/data-freshness.test.mjs` and watch it fail with `Expected values to be strictly equal: 200 !== 503`.
```

**GREEN — 改动**

Change the signature at site-audit.mjs:273 to `startServer(rootDir = WEB, { apiCatalog = "ok" } = {})` and line 293 to `if (url.pathname === "/api/catalog") { if (apiCatalog === "fail") return send(503, JSON.stringify({ error: "catalog_unavailable" })); return send(200, JSON.stringify({ ...catalog, discovery })); }` (and the same guard on /api/status). Today line 293 answers 200 unconditionally, so the static-fallback branches in models.js:161, status.js:179 and benchmarks.js:147 are unreachable from every test and audit in the repo.

**验证**

```
npm run build && node --test web-tests/data-freshness.test.mjs  →  `# fail 0`.
```

---

## W16 · Assert the degradation copy in browserAudit, not in a skippable node --test

**文件**

- `web-tests/site-audit.mjs`

**RED — 先写、并亲眼看它失败**

```
Wire the assertion in but revert models.js's degradation temporarily (`git stash push site/models/models.js`), then run the audit and watch it report the critical finding. Restore with `git stash pop`.
```

**GREEN — 改动**

In browserAudit() (site-audit.mjs:352) add a short second pass: start a server with `{ apiCatalog: "fail" }`, visit /models/ and /benchmarks/, wait for network idle, and add a `critical` finding if the page text does not contain 静态兜底数据, or if /models/ still shows a numeric #available-count. This must live in browserAudit because ci.yml's node job installs no Chromium and site.yml never runs test:web — a 'gracefully skipping' node --test would be green-by-skip forever, while site.yml:81-84 already hard-fails the run when browserSkipped is set. Exclude the degraded pass from the existing 'stuck in loading state' heuristic at :414-421.

**验证**

```
npm run build && CHROMIUM_PATH="$(find ~/.cache/ms-playwright -name chrome -type f 2>/dev/null | head -1)" node scripts/audit-site.mjs --json > /tmp/audit.json; node -e 'const r=require("/tmp/audit.json"); if(r.browserSkipped) throw new Error("browser layer skipped: "+r.browserSkipped); const c=r.findings.filter(f=>f.severity==="critical"); console.log("critical:",c.length); if(c.length) { console.log(c); process.exit(1); }'  →  expect `critical: 0`. If Chromium is absent locally this throws `browser layer skipped` — that is the correct signal that this check only proves itself in site.yml.
```

---

## W17 · site/_headers: stop /data/*.json inheriting default edge caching

**文件**

- `site/_headers`
- `scripts/build-site.mjs`

**RED — 先写、并亲眼看它失败**

```
Add the build-site.mjs assertion FIRST, run `npm run build`, watch it fail with `Error: Catalog data must carry an explicit cache policy`.
```

**GREEN — 改动**

Append to site/_headers:

# /data/*.json is not fingerprinted (build-site.mjs only hashes css/js), so it
# needs an explicit short TTL — otherwise a visitor can be served a catalog
# older than the build gate's own threshold on a perfectly fresh deploy.
/data/*.json
  Cache-Control: public, max-age=300, must-revalidate

Add to build-site.mjs: read web/_headers and `if (!headers.includes("/data/*.json")) throw new Error("Catalog data must carry an explicit cache policy");`

**验证**

```
npm run build  →  exit 0; then `grep -A1 '/data/\*.json' site/_headers` → expect the `Cache-Control: public, max-age=300, must-revalidate` line.
```

---

## W18 · data-refresh.yml — scheduled refresh that opens a PR, never a direct commit

**文件**

- `.github/workflows/data-refresh.yml`
- `scripts/refresh-provider-status.mjs`

**RED — 先写、并亲眼看它失败**

```
The invariant that matters is already the RED test from W9 case (9): `assert.ok(REFRESH_MAX_AGE_DAYS < MAX_CATALOG_AGE_DAYS)`. Add one more to web-tests/data-freshness.test.mjs asserting the workflow cannot regress it: read .github/workflows/data-refresh.yml and assert it does NOT contain `git push` and DOES contain `REFRESH_MAX_AGE_DAYS`. Run `node --test web-tests/data-freshness.test.mjs` before creating the workflow and watch it fail with ENOENT.
```

**GREEN — 改动**

Create scripts/refresh-provider-status.mjs: fetch https://oaf.asia/api/status, reject if not schema_version 2 or as_of older than 30 minutes, then project into docs/provider-status.json with the monotone-safe rule — `available` passes through with its latency_ms/reason; EVERY other value becomes `unverified` with the observed reason preserved. Verified necessary: /api/status right now reports google-ai-studio and openrouter unavailable purely from rate-limiting our own probe, and PROBE_BATCHES=2 means ~half the models lack evidence in any run.
Create .github/workflows/data-refresh.yml: `on: schedule: - cron: "0 6 * * *"` plus workflow_dispatch; `permissions: { contents: write, pull-requests: write }`; concurrency group; steps = checkout, setup-node, setup-python, `pip install -e ".[dev]"`, `node scripts/refresh-provider-status.mjs`, export the catalog, then `npm ci && npm run build && npm run test:web && python -m pytest tests/ -q` on its OWN output, then open a PR via `gh pr create` ONLY IF the catalog content changed OR generated_at is older than REFRESH_MAX_AGE_DAYS (3) — read from scripts/data-freshness.mjs, never hardcoded. A PR (not a push) is required because default-GITHUB_TOKEN pushes do not trigger ci.yml or site.yml.

**验证**

```
node --test web-tests/data-freshness.test.mjs → `# fail 0`; then `node scripts/refresh-provider-status.mjs --dry-run` → expect it to print the projected provider table with google-ai-studio and openrouter shown as `unverified` (NOT `unavailable`), and to exit 0 without writing docs/provider-status.json.
```

---

## W19 · deployed-freshness.yml — the only check that measures what visitors get

**文件**

- `.github/workflows/deployed-freshness.yml`

**RED — 先写、并亲眼看它失败**

```
Run the node body against today's deployed artifact and watch it fail on schema:

curl -fsS https://oaf.asia/data/catalog.json -o /tmp/deployed.json && node -e 'const c=require("/tmp/deployed.json"); if(c.schema_version!==2){console.error("deployed catalog is an old schema generation");process.exit(1)}'

Expected RED today: `deployed catalog is an old schema generation`, exit 1 — correct, because W5 has not been deployed yet.
```

**GREEN — 改动**

Create a workflow, `on: schedule: - cron: "0 7 * * *"` plus workflow_dispatch, with one step:

          curl -fsS https://oaf.asia/data/catalog.json -o deployed.json
          node -e '
            const c = require("./deployed.json");
            const days = (Date.now() - Date.parse(c.generated_at)) / 86400000;
            console.log(`deployed generated_at ${c.generated_at} (${days.toFixed(1)}d), schema ${c.schema_version}`);
            if (c.schema_version !== 2) { console.error("::error::deployed catalog is an old schema generation"); process.exit(1); }
            if (days > 14) { console.error(`::error::deployed catalog is ${days.toFixed(1)} days old — run npm run deploy`); process.exit(1); }
          '

The build gate certifies git; there is no deploy workflow and `npm run deploy` is manual, so the repo can stay green for months while production serves a hand-deployed snapshot.

**验证**

```
After the M2 branch is deployed, the same command exits 0 and prints `deployed generated_at ... schema 2`. Until then the workflow is expected red and that is the accurate signal.
```

---

## W20 · Record the accepted gaps so M2 cannot be closed while the visitor problem is live

**文件**

- `docs/PRD-site-quality-and-roadmap.md`
- `CHANGELOG.md`

**RED — 先写、并亲眼看它失败**

```
Add to build-site.mjs's existing marker idiom a check that the PRD records the deployed-artifact gap — read docs/PRD-site-quality-and-roadmap.md and assert it contains `deployed-freshness`. Run `npm run build` and watch it fail with the missing-content error. (If you prefer not to couple the build to docs, skip the assertion and verify by grep only.)
```

**GREEN — 改动**

In the P0-3 section add a short 「本轮取舍」 block recording, each with its verified evidence: (1) the build gate protects the repo, not the visitor — no deploy workflow exists, hence W19; (2) requirement 2 is met by a PR-opening scheduled job rather than a direct commit-back, because default-GITHUB_TOKEN pushes do not trigger ci.yml/site.yml and because a commit-suppression rule would freeze generated_at and redden main (REFRESH_MAX_AGE_DAYS < MAX_CATALOG_AGE_DAYS is unit-asserted); (3) build-time generation is rejected — ci.yml's node job has no setup-python; (4) the export-time availability downgrade is deleted, with the _speed_tier leak onto the live /api/catalog as the reason it was dangerous; (5) per-model availability (public_catalog.py:95) is deferred to M3, and /api/status is now known to carry per-model evidence so it no longer blocks on credentials; (6) the seven staleness clocks and the dormant fingerprint-ordering 404.

**验证**

```
npm run build  →  exit 0; then `grep -c 'deployed-freshness\|REFRESH_MAX_AGE_DAYS' docs/PRD-site-quality-and-roadmap.md` → expect `2` or more. Finally run the whole suite: `.venv/bin/python -m pytest tests/ -q && npm run test:web && npm run build && npm run test:site -- --static` → expect `passed`, `# fail 0`, `Built ...`, and `✔ 未发现问题`.
```

---

## 开放风险（实施时必须持续盯住）

1. W5's regeneration is the riskiest item in M2, not the gate: a 562-line diff flipping schema_version 1→2, adding free_tier/free_availability to 11 providers and 55 models, rewriting status_note, and changing google-ai-studio.api. It must be reviewed as a data change and must share a commit with W4 — I verified /v1beta/openai/models/X:generateContent returns 404 and probe.js:88 maps that to unavailable, so shipping W5 alone turns Google dark on the very board M2 exists to make trustworthy, and it will look like a provider outage rather than like our commit.

2. W13/W14 are a visible product regression by design: when /api/catalog is unreachable the models page goes from 9 green 可用 cards to 11 待验证 cards and #available-count reads 未验证 instead of a number. The #available-count element is a <b> sized for digits, so check the CSS. Also re-run the browser audit's 'page almost empty' and 'stuck loading' heuristics (site-audit.mjs:~414-435) against the degraded rendering before W16 turns it into a gating check.

3. degradeCatalog() and relativeDays() are duplicated across models.js and benchmarks.js (and conceptually triplicated with status.js's metadataOnly()). This is deliberate — build-site.mjs:247-255 hashes each JS file before :261-271 rewrites references inside it, and _headers marks /*.js immutable for a year, so a JS→JS import would leave cached visitors requesting a renamed dependency: a hard 404 that kills the page, not a cosmetic mismatch. The build markers keep the three copies in step, but nothing detects semantic drift between them.

4. Commit-time anchoring falls back to wall clock when git metadata is unavailable (tarball, vendored checkout, shallow export). The fallback is strictly stricter so it fails safe, but the same bytes can be green in a git checkout and red in a tarball. W10 prints which anchor was used; do not remove that line.

5. The W6 reproducibility diff is deterministic today only because every free_tier is unconfigured. FreeTierEvidence.status() compares expires_at against now, so the first provider shipping real evidence with an expiry makes the export time-dependent in a second dimension and the diff will start failing spontaneously on the expiry date. Decide then whether to normalise the status key out of the comparison; do not pre-build that.

6. The W18 refresh job's monotone-safe rule means a genuinely-down provider will show as unverified rather than unavailable in the static fallback until a human intervenes. That is the correct trade against baking a 429 blip in for a day (verified: google-ai-studio and openrouter are unavailable right now purely from rate-limiting), but it does mean the static file can never carry bad news.

7. GitHub disables scheduled workflows after 60 days of repository inactivity. If data-refresh.yml silently stops, generated_at freezes and on day 8 the gate reddens every unrelated PR. The mitigation is that clearing it is one credential-free command named in the error message — but a failure notification on the refresh workflow should be added if the repo ever goes quiet.

8. The circular data dependency is unfixed: the Worker probes exactly the providers present in the deployed /data/catalog.json, and W18 derives status from that probe. A provider newly added to registry.default.yaml stays unverified until a deploy happens, so the first refresh after any provider change is expected to be partially unverified — not a bug.

9. public_catalog.py:95 still stamps the provider verdict onto all 55 models, so 37 models are published available on the strength of one smoke test each. Deferred to M3 with a now-known path (/api/status carries per-model evidence keyed provider/model), but until then the models page's per-row 可用 badge remains an unqualified claim on the live path.

10. Seven staleness constants now coexist: recent() 45min, statusIsStale() 20min, PROBE_INTERVAL_MINUTES 15, getDiscovery 7h, /api/catalog max-age 300s, MAX_CATALOG_AGE_DAYS 7d, REFRESH_MAX_AGE_DAYS 3d. W7 adds a comment naming all seven; nothing enforces coherence between them, and that is where the next drift bug will be written.

## 评审中被否决的方案

| 方案 | 否决理由 |
|---|---|
| Draft D — a daily job that runs export-public-catalog.py and commits back, but 'only if the diff is more than the timestamp line'. | Self-detonating and proven so by execution. The exporter's inputs are static in-repo files; two consecutive runs differ ONLY in generated_at. So after the first commit, every daily run is a timestamp-only diff, the suppression rule refuses to commit, generated_at freezes, and on day 8 the 7-day gate turns main red for every unrelated PR — the exact harm the draft invoked as the reason not to gate status_as_of. Replaced by a PR-opening job whose suppression window (REFRESH_MAX_AGE_DAYS = 3) is unit-asserted to be strictly less than the gate threshold. |
| Draft D — committing the refreshed catalog directly with the default GITHUB_TOKEN. | Pushes made with the default GITHUB_TOKEN do not trigger push or pull_request workflows, so neither ci.yml nor site.yml would ever validate the one commit nobody reviewed. It would also auto-merge the 562-line schema migration and the google-ai-studio api change unattended. A PR keeps CI coverage and puts a human in front of data-contract changes. |
| Draft B.1 — export-time downgrade of every availability to 'unverified' when status evidence is stale, so 'the published fallback CANNOT lie regardless of frontend code'. | Three independent disqualifiers. (1) No consumer trusts the static availability field: probe.js already forces unverified without recent evidence, status.js overwrites it in metadataOnly(), and after W13/W14 so do models.js and benchmarks.js. (2) It corrupts a derived field the Worker never recomputes — _speed_tier returns the affirmative false claim 当前不可用 for anything not available, and probe.js:235 spreads only {availability,status,reason,latency_ms,checked_at}, so that string survives onto the LIVE /api/catalog for models the probe just verified as available. (3) It adds a second wall-clock dependency to the exporter, breaking the reproducibility diff that makes the age gate non-forgeable, and it is a calendar-dated time bomb in tests/test_public_catalog.py, which pins as_of: 2026-08-01 and asserts speed_tier_zh == 快. The one genuinely broken sub-part (_speed_tier) is fixed independently in W1. Also note the claim itself is false: an export-time stamp is written when evidence is freshest and then freezes while wall-clock advances — site/data/catalog.json is the existence proof. |
| Draft C — the age threshold as a single source of truth in scripts/data-freshness.mjs, with build-site.mjs asserting that status.js and models.js carry the same constant. | The frontend does not need a threshold at all. The catch branch already knows it is the fallback, so it degrades unconditionally — a strict superset of 'over threshold'. That deletes the constant from every frontend file and with it the whole mirroring mechanism, which would otherwise have been four authored copies across two languages (status.js, models.js, benchmarks.js, public_catalog.py) with a substring assertion covering two of them and unable to detect unit drift (7 vs 604800 vs 7*24*3600*1000). The CSP/fingerprint reasoning behind 'no cross-file JS imports' is correct and is preserved as a comment — but it is an argument against a problem we should not create. |
| Draft B.3 — hard-fail the build when status_as_of exceeds a larger bound (30 days). | The build has no network by project non-goal and ci.yml's node job has no setup-python, so nothing in the build can ever clear this gate; on day 31 it blocks every unrelated PR until a maintainer intervenes. It is also trivially defeated by hand-editing the as_of line in docs/provider-status.json, which is worse than no gate because it trains people to edit evidence files. status_as_of age is printed as an informational build line and enforced instead in the two scheduled workflows, where it can be acted on. |
| Shipping the freshness threshold inside catalog.json (a `freshness` object written by the exporter) as the SSOT. | Circular: changing the UI's staleness rule would then require re-exporting the very artifact whose staleness the rule governs, so the knob is gated by the condition it controls. It also rides through mergeServerStatus's ...catalog spread onto /api/catalog forever, putting an internal build constant into the public data contract. Moot anyway once the frontend degrades unconditionally and holds no threshold. |
| OFR_ALLOW_STALE_CATALOG escape hatch (in either form — exit-code-only, or stamping degraded:true into web/data/catalog.json). | Commit-time anchoring removes every scenario the hatch existed for: git bisect, tag rebuilds, and CI re-runs on old commits all pass unchanged. The only remaining failure is 'you are committing today with a >7d-old catalog', which is precisely when you should refresh — and refreshing is one credential-free command named in the error message. The exit-code-only form decays into a workflow file within a quarter; the artifact-degrading form is four coupled artifacts (env var, JSON field, UI branch, workflow-grep test) that a contributor must remember forever for a mechanism meant never to be used. |
| Shape assertions with magic floors (provider_count >= 8, model_count >= 40). | Numbers living nowhere near registry.default.yaml (11/55 today): dropping three providers passes silently, dropping four fails with a message naming no cause. Replaced by internal consistency (provider_count === providers.length, model_count === Σ models.length, both > 0), which catches the verified 0-provider fail-open, plus the W6 byte-diff against a real re-export, which catches 'the registry lost three providers' properly and for the right reason. |
| Draft E — a new web-tests/*.test.mjs Playwright test for the degradation banner that 'skips gracefully without Chromium'. | It would be green-by-skip forever: ci.yml's node job installs no Chromium and site.yml never runs test:web. site.yml:81-84 already hard-fails the run when browserSkipped is set, so the assertion belongs in browserAudit(). Separately, startServer() answers /api/catalog with 200 from the on-disk catalog unconditionally (site-audit.mjs:293), so the fallback branch the test targets is structurally unreachable — hence W15 before W16. The gate's own arithmetic still gets a real unit test (W9), because it is branchy pure logic; that file exists for testability, not for constant-sharing. |
| Generating the catalog at build time (the PRD's stated alternative to requirement 2). | ci.yml's node job sets up Node only, with no setup-python step. Making npm run build depend on export-public-catalog.py would break that job, every frontend-only contributor, offline builds, npm run dev and npm run deploy:dry-run. A build that fetches oaf.asia would additionally make deploys non-reproducible and violate the project non-goal that the public site makes no real upstream calls. |
| Fixing the per-model availability fabrication (public_catalog.py:95) inside M2. | Real defect — 37 of 55 models are published as available on the strength of one provider-level smoke test each — but it requires a docs/provider-status.json schema change plus exporter changes, and it is not what the PRD asked for. Deferred to M3 with the blocker now removed: /api/status carries per-model evidence keyed provider/model, so it no longer depends on a maintainer run with real API keys. |
| Making /api/health fail closed on probe staleness, and annotating /api/status with a computed staleness flag. | Correct observation (worker/index.js:147-157 returns ok:true unconditionally and advertises 'every 15 minutes' as a static string) but nothing in the repo consumes /api/health — grep finds only the worker itself and the audit stub. Zero visitor impact, and changing a public endpoint's contract inside a data-freshness milestone widens the blast radius for no gain in the stated problem. |
