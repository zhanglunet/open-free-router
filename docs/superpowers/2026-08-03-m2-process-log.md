# M2 开发过程记录（P0-3 公开目录数据新鲜度）

日期：2026-08-03
分支：`claude/architecture-multi-client-docs-5qy0oj`（从 `main@0ff9225` 重开）
方法论：Superpowers（未安装，按公开规则手工执行；合规核对见
`docs/superpowers/2026-08-03-m2-compliance.md`）

本文件是**按时间顺序**记录的过程日志，不是结论摘要。结论在设计文档与 PRD 里。

---

## 0. 环境与方法论前提

### 0.1 Superpowers 可用性核查

用户要求「验证系统是否遵守了 Superpowers」。首先核查它是否存在于本会话：

| 检查 | 命令 / 位置 | 结果 |
|---|---|---|
| 仓库内引用 | `grep -rn -i superpower .` | 无命中 |
| 已装插件 | `ls ~/.claude/plugins/` | 空目录 |
| 已装 skill | `ls ~/.claude/skills/` | 12 个，均与 Superpowers 无关 |
| 插件市场 | `SearchPlugins(["superpowers", ...])` 两轮 | 无匹配条目 |

**结论**：Superpowers 插件在本会话中不可用，无法通过 `Skill` 调用。
因此改为直接读取上游权威定义（`github.com/obra/superpowers`），按其 14 个
skill 的规则手工执行，并逐条留下可核对的证据。抓取到的 skill 清单：

```
brainstorming            dispatching-parallel-agents  executing-plans
finishing-a-development-branch  receiving-code-review  requesting-code-review
subagent-driven-development     systematic-debugging   test-driven-development
using-git-worktrees      using-superpowers            verification-before-completion
writing-plans            writing-skills
```

其中对本次开发有强约束的四条已抓取全文并据此执行：
`brainstorming`、`writing-plans`、`test-driven-development`、
`verification-before-completion`。

### 0.2 干净测试基线（using-git-worktrees 第 3 步要求）

Superpowers 要求在动手前确认基线是绿的，否则不得推进。实测（2026-08-03 05:41Z，
改动任何文件之前）：

| 套件 | 命令 | 结果 |
|---|---|---|
| Python | `.venv/bin/python -m pytest tests/ -q` | **280 passed** in 50.83s |
| Worker / 站点 | `npm run test:web` | **18 pass / 0 fail** |
| npm 包 | `npm run test:npm` | **0 fail** |
| 构建 | `npm run build` | 成功，27 个资源指纹化 |
| 全站审计（静态层） | `node scripts/audit-site.mjs --static` | **✔ 未发现问题** |

基线绿，可以推进。

---

## 1. 现状调查：M2 到底在修什么

PRD 对 P0-3 的描述是「静态兜底数据已经两天没更新」。动手前先去核实这句话是否
完整——结果它**低估了问题**。

### 1.1 已发布快照的真实状态

```
site/data/catalog.json
  schema_version : 1
  generated_at   : 2026-08-01T17:16:58Z
  status_as_of   : 2026-08-01T15:00:52Z
  provider_count : 11    model_count: 55
```

而当前 `src/open_free_router/public_catalog.py` 产出的是 **schema_version 2**。
用仓库内的注册表 + 状态文件重新导出一次（**无需任何凭据**）：

```
$ .venv/bin/python scripts/export-public-catalog.py \
    --registry src/open_free_router/registry.default.yaml \
    --status   docs/provider-status.json \
    --profiles docs/provider-profiles.json \
    --output   $SCRATCH/catalog-regen.json
Exported 11 providers / 55 models

$ diff site/data/catalog.json $SCRATCH/catalog-regen.json | wc -l
564
```

564 行差异，不是一个时间戳的问题。逐项确认：

| 差异 | 已发布 | 重新导出 | 影响 |
|---|---|---|---|
| `schema_version` | 1 | 2 | — |
| `free_tier` / `free_availability` | **完全缺失**（`grep -c` = 0） | 每个提供商与模型都有 | 见 1.2 |
| Gemini `api` | `.../v1beta` | `.../v1beta/openai` | 页面展示的接口地址是错的 |
| `status_note` | 旧文案 | 含免费证据说明 | 免责声明不完整 |

### 1.2 这导致了一个正在线上发生的缺陷

`site/models/models.js` 用 `provider.free_tier` / `row.free_tier` 渲染
「免费证据」列、提供商卡片徽章，以及「免费」筛选器；
`site/benchmarks/benchmarks.js:13` 也读 `row.free_tier?.status`。
已发布的快照里这个字段根本不存在。

在真实浏览器里跑一遍（构建产物 + 复用 `web-tests/site-audit.mjs` 的
`startServer()`，Chromium 无头）：

```json
{
  "statusTime": "可用性快照：2026/8/1 15:00:52 · 状态不是 SLA",
  "rowCount": 55,
  "freeEvidenceTally": { "条件未知": 55 },
  "providerEvidenceUnique": [ "免费证据：条件未知" ]
}
```

**55 个模型全部显示「条件未知」**，11 张提供商卡片全部显示「免费证据：条件未知」，
「已核验免费」筛选项匹配不到任何一行。

> **【2026-08-03 修正】** 本节最初写的是「整整一列和一个筛选器在生产环境是死的」。
> 重新导出目录之后我重跑了同一个取证脚本，输出**逐字未变**——仍然是 55 个
> 「条件未知」。查证原因：`src/open_free_router/registry.default.yaml` 里
> **一条 free_tier 证据都没有**（`grep -c free_tier` = 0），所以重新导出后每个
> `free_tier.status` 都是 `"unknown"`；而 `models.js` 与 `benchmarks.js` 读的是
> `row.free_tier?.status || "unknown"`，字段缺失时本来就回落到同一个值。
>
> 准确的说法是：**字段缺失与字段存在但为 unknown，用户可见输出完全相同**。
> 那一列显示「条件未知」不是因为快照过期，而是因为注册表里确实没有记录任何免费
> 证据——这是一个独立的内容准确性问题（`/validation/` 页面在宣称这些提供商的
> 免费层已核验，而结构化注册表一条都没有），已记入 §5 待办，**不属于 M2 范围，
> 也不会被重新导出修好**。
>
> 重新导出真正修好的是：schema 1→2（门禁才能断言形状）、`status_note` 补上免费
> 证据说明、`google-ai-studio.api` 修正为 `/v1beta/openai`，以及让 L4 可复现性
> diff 成为可能。这个更正由 GREEN 之后的复验发现，不是事后合理化。

同一次浏览器会话里，状态页在静态兜底路径下显示：

```json
{ "generated": "目录生成于 2 天前 · 等待服务器探测", "asOf": "2 天前" }
```

即：**页面把过期快照的时间戳原样渲染，不做任何新鲜度判断**——这正是 P0-3
第 3 条要修的东西，现在有了可复现的证据而不是推断。

### 1.3 两条时钟必须分开处理（本次设计的核心约束）

| 时钟 | 含义 | 谁能刷新 | 能否在 CI 里刷新 |
|---|---|---|---|
| `generated_at` | 目录**结构**导出时间 | 任何人（注册表与 profiles 都在仓库里） | **能**，已实测，无需凭据 |
| `status_as_of` | 可用性**证据**采集时间 | 只有持有真实 API Key 的维护者（`AGENTS.md:97`：仪表盘 Live Status → `POST /api/probe`） | **不能** |

由此得到一个必须写进设计的推论：**如果定时任务每天刷新 `generated_at`，那么只对
`generated_at` 设门禁几乎没有意义——它永远会通过。** 真正保护访客的是
`status_as_of`，而它又无法在 CI 里修复，所以不能简单地硬阻断构建。这个矛盾是
M2 设计的主要难点，交给设计评审去解。

### 1.4 推翻上一节的结论：证据时钟其实也能在 CI 里刷新

在把「`status_as_of` 无法在 CI 刷新」写进设计之前，先去证伪它。
`worker/index.js` 里 `/api/status` 这条路由**没有任何鉴权**，且用公开的
`JSON_HEADERS` 返回 KV 里的探测快照。实测生产环境：

```
$ curl -sS https://oaf.asia/api/status
as_of: 2026-08-03T05:45:08.505Z   schema: 2   checked: 27/55
providers in snapshot: 11 → google-ai-studio, groq, nous, nvidia-nim,
  opencode-zen-free, openrouter, poolside, sensenova, stepfun, deepseek, gitee-ai
missing from catalog: []   extra: []
availability tally: {"available": 8, "unavailable": 3}
model evidence entries: 55
```

**快照只有几分钟大**（Worker 每 15 分钟轮换探测两批），覆盖全部 11 个提供商与
55 个模型的证据，而且字段与 `docs/provider-status.json` 的形状可以直接投影：

| `docs/provider-status.json` | `/api/status` |
|---|---|
| `as_of` | `as_of` |
| `providers[id].availability` | `providers[id].availability` |
| `providers[id].latency_ms` | `providers[id].latency_ms` |
| `providers[id].reason` | `providers[id].reason` |

凭据留在 Cloudflare Secrets 里，探测由 Worker 在服务端完成，**CI 只是把它自己的
探测结果取回来落盘**。于是：

> **两条时钟都可以在没有任何凭据的 CI 里刷新。**

这直接推翻了 1.3 的推论，也推翻了我草案里 A、B.3 两段的前提——硬门禁对两条时钟
都是正当的，因为 CI 有能力把它们都修好。这条事实在设计评审启动之后才被发现，
评审的对抗性 agent 也被显式要求去检验同一条声明，两边结论一致（见设计文档
第 4 节）。

同时暴露出一个必须防的新风险：如果定时任务无条件信任 `/api/status` 的返回值，
那么生产 API 一旦降级（返回空 providers、旧 `as_of`、或 503），就会把坏数据
**提交回仓库**，把「兜底数据」变成「兜底谎言」。取数之后必须校验再落盘。

---

## 2. Brainstorming 阶段（Superpowers 规则：设计先于代码）

`brainstorming` skill 要求「在设计被提出并认可之前，不得写任何代码」，且要
「提出 2-3 个带取舍的方案」。执行方式：先由我起草一版设计（见设计文档附录
「草案 A-E」），然后用 workflow 并行跑：

- 3 个**互相不可见**的独立设计，各自一个视角：YAGNI 最小改动 / 数据诚实性 /
  运维可行性
- 1 个**对抗性评审**，唯一任务是证明我的草案是错的，且每条缺陷必须引用真实证据
- 3 个**评委**，各用一个独立镜头（correctness / maintainability / operability）
  给所有方案打分
- 1 个**综合**，产出唯一的设计定稿与可直接执行的任务分解

### 2.1 评审中途返回的第一个结论：我把范围搞错了

「数据诚实性」视角的设计返回后，指出**最严重的「过期数据被当作当前数据」根本不在
静态兜底路径上，而在 Worker 的实时路径上**。Superpowers `verification-before-completion`
明确要求「不得把 agent 的自述当作证据」，所以逐条自己复现。

#### 复现 1：`mergeServerStatus` 对提供商摘要没有任何时效闸门

`worker/probe.js:226-247`：模型证据每一条都过 `recent()`（45 分钟，`STATUS_STALE_MS`），
而提供商摘要的 `availability` / `reason` / `latency_ms` / `checked_at` 是**无条件**
从 KV 快照里拷出来的。

独立复现（直接 import 真实模块，喂入不同年龄的 KV 快照）：

```
--- KV snapshot aged 10 minutes ---
  provider: available | Cloudflare 服务器已验证 3/3 个模型可用 | latency 900ms
  models  : available, available, available
--- KV snapshot aged 46 minutes ---
  provider: available | Cloudflare 服务器已验证 3/3 个模型可用 | latency 900ms
  models  : unverified, unverified, unverified
--- KV snapshot aged 3 days ---
  provider: available | Cloudflare 服务器已验证 3/3 个模型可用 | latency 900ms
  models  : unverified, unverified, unverified
--- KV snapshot aged 30 days ---
  provider: available | Cloudflare 服务器已验证 3/3 个模型可用 | latency 900ms
  models  : unverified, unverified, unverified

3-day vs 30-day provider payload identical (ignoring checked_at): true
```

即：**从第 46 分钟开始**，状态页就会出现一张写着「可用 · 已验证 3/3 个模型可用 ·
900ms 实测延迟」的提供商卡片，下面挂着三个「候选未验证」的模型徽章；而 3 天与
30 天的提供商输出**逐字节相同**，访客无法区分。

`worker/index.js:60-63,180` 里刷新是 `ctx.waitUntil()` 的后台任务，本次请求返回的
就是这份陈旧合并结果；若探测持续失败（例如密钥轮换），KV 永远不被覆盖，那个
`available` 就是不朽的。

**这比 PRD 描述的静态兜底问题严重得多**：它在主路径上，从 46 分钟起就成立，
且没有任何视觉线索。

#### 复现 2：定时刷新会把「最近检查」变成一个从未发生的检查

`site/status/status.js:65` 是 `relativeTime(catalog.status_as_of || catalog.generated_at)`，
填入的元素标签是 **「最近检查」**（`site/status/index.html:55`）。而
`metadataOnly()` 会把 `status_as_of` 置空（`status.js:128`），于是静态兜底路径
**总是**落到 `generated_at` 上。

当前实测显示「最近检查 2 天前」（诚实）。一旦按 PRD 需求 2 加上每日刷新
`generated_at` 的定时任务，同一个位置会变成「最近检查 0 秒前」——**为一次从未
发生的可用性检查报出 0 秒**。换言之，PRD 需求 2 按字面实现会让页面更不诚实，
必须先修这一行。

#### 复现 3：模型雷达的静态兜底分支没有等价的中和层

`site/status/status.js:125-148` 有 `metadataOnly()` 把兜底数据整体降级；
`site/models/models.js:161-171` 的兜底分支**直接使用原始 JSON**。后果：
提供商绿点、`最近可用` 计数、每行 `可用` 徽章都按「当前事实」渲染。

#### 复现 4：评测页把过期可用性折进一个数字分

`site/benchmarks/benchmarks.js:26-33`：`availability`（满分 35）来自
`row.provider_status`，`latency`（满分 10）来自 `row.latency_ms`——
**任务适配分的 45% 来自可用性与延迟**。而 `site/benchmarks/index.html:65` 的
计分规则表宣称数据来源是「Cloudflare 最近一次提供商级探测快照」。在静态兜底
路径上这句话是假的，且该页**没有任何时间戳**，连判断的机会都不给。

#### 复现 5：构建期门禁保护不了已经在线的站点

`ls .github/workflows/` 只有 `ci.yml` 和 `site.yml`，**没有部署工作流**——
部署是人工 `npm run deploy`。所以构建期门禁挡住的是「修复一个已经腐坏的站点」，
而不是「防止它腐坏」。门禁仍然值得做（它防的是把过期快照**提交**进仓库），
但不能把它当作对访客的保护，真正的保护必须发生在渲染时。

---

> 记录续写于设计评审全部返回之后。
