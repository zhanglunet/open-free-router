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
「已核验免费」筛选项匹配不到任何一行。整整一列和一个筛选器在生产环境是死的——
而 `/validation/` 页面同时在宣称这些提供商的免费层已经过核验。

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

> 记录续写于设计评审返回之后。
