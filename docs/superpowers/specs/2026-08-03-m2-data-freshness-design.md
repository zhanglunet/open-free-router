# M2 数据新鲜度设计文档

> Superpowers `brainstorming` skill 要求：在写任何代码之前，设计必须先被写下来
> 并被认可。本文件是该阶段的产物。实施计划另见
> `docs/superpowers/plans/2026-08-03-m2-data-freshness.md`。

- 日期：2026-08-03
- 对应 PRD 条目：`docs/PRD-site-quality-and-roadmap.md` §P0-3「公开目录数据新鲜度」（里程碑 M2）
- 基线：`main@0ff9225`（v0.3.0，11 提供商 / 55 模型 / 16 个公开页面）

---

## 1. 目标（一句话）

**任何时候，访客在页面上看到的可用性与免费证据，要么是新鲜的，要么被明确标注为
过期——不允许存在第三种状态：过期数据被当作当前数据渲染。**

---

## 2. 证据：问题比 PRD 描述的更严重

PRD 写的是「静态兜底数据已经两天没更新」。实际核实后发现这是低估。全部证据在
动手改任何文件之前采集，命令与输出记录在
`docs/superpowers/2026-08-03-m2-process-log.md` §1。

### 2.1 已发布快照落后的不只是时间戳，而是整个 schema

| | 已发布 `site/data/catalog.json` | 现行导出器产出 |
|---|---|---|
| `schema_version` | **1** | 2 |
| `free_tier` / `free_availability` | **字段完全不存在**（`grep -c` = 0） | 每个提供商与模型都有 |
| Gemini `api` | `.../v1beta` | `.../v1beta/openai` |
| `generated_at` | 2026-08-01T17:16Z | 重新导出即为当前时间 |

用仓库内文件重新导出一次，`diff` 是 **564 行**。

### 2.2 由此产生的、正在生产环境发生的缺陷

`site/models/models.js` 用 `provider.free_tier` / `row.free_tier` 渲染「免费证据」
列、提供商卡片徽章和「免费」筛选器；`site/benchmarks/benchmarks.js:13` 也读它。
已发布快照里没有这个字段。真实无头浏览器实测（构建产物 + `startServer()`）：

```json
{
  "statusTime": "可用性快照：2026/8/1 15:00:52 · 状态不是 SLA",
  "rowCount": 55,
  "freeEvidenceTally": { "条件未知": 55 },
  "providerEvidenceUnique": [ "免费证据：条件未知" ]
}
```

- 55 个模型、11 张提供商卡片**全部**显示「条件未知」
- 「已核验免费」筛选项匹配不到任何一行
- 同时 `/validation/` 页面正在宣称这些提供商的免费层已经过核验

**一个数据快照过期，直接让一整列和一个筛选器在生产环境失效，并让站点自相矛盾。**

### 2.3 时间戳被无判断地渲染

- `site/models/models.js:177` → `可用性快照：${formatTime(catalog.status_as_of)} · 状态不是 SLA`
- `site/status/status.js:122` → `目录生成于 ${relativeTime(catalog.generated_at)} · 等待服务器探测`

两处都只是把时间戳格式化后贴出来，没有任何新鲜度判断。这正是 P0-3 第 3 条要修的。

---

## 3. 约束（设计必须满足的硬条件）

### C1 两条时钟含义不同，不能混为一谈

| 时钟 | 含义 | 来源 |
|---|---|---|
| `generated_at` | 目录**结构**导出时间 | 本仓库的 `registry.default.yaml` + `provider-profiles.json` |
| `status_as_of` | 可用性**证据**采集时间 | 真实上游探测 |

结构可以在任何机器上重算；证据不能凭空产生。把两者合成一个「新鲜度」会导致
**结构刷新掩盖证据陈旧**——即用一次无成本的重导出，把页面上的时间戳刷新成今天，
而底下的可用性判断仍然是一周前的。这是本设计要防的头号失真模式。

### C2 两条时钟都能在无凭据的 CI 里刷新（实测推翻的初始假设）

初始判断是「证据只能由持有 API Key 的维护者刷新」。实测证伪：

```
$ curl -sS https://oaf.asia/api/status
as_of: 2026-08-03T05:45:08.505Z   schema: 2   checked: 27/55
providers: 11（与目录完全一致，无缺失、无多余）
model evidence entries: 55        availability: 8 available / 3 unavailable
```

`worker/index.js` 的 `/api/status` **无鉴权**、返回 KV 中的服务器探测快照，
只有几分钟大。凭据留在 Cloudflare Secrets 里，探测在服务端完成，CI 只是把结果
取回落盘。字段可以直接投影成 `docs/provider-status.json` 的形状。

**推论**：对两条时钟都设硬门禁是正当的，因为 CI 有能力把它们都修好。

### C3 但「取回来就提交」会把兜底数据变成兜底谎言

若定时任务无条件信任 `/api/status`，生产 API 一旦降级（空 providers、旧 `as_of`、
503、部分探测失败）就会把坏数据提交回仓库。取数后必须校验再落盘。

### C4 站点不能引入新的跨文件 JS 引用

`scripts/build-site.mjs` 的指纹化分两步：**先**对每个 css/js 按自身字节哈希改名，
**再**在 html/js/css 内容里重写引用。因此若 `a.js` 引用 `b.js`，`a.js` 的文件名
是用重写前的字节算出来的——`b.js` 变更时 `a.js` 内容会变而 URL 不变，缓存里会留下
指向已被改名文件的旧副本。

实测确认这个隐患目前是**潜在的而非已发生的**：

```
$ grep -rn '"/[^"]*\.\(js\|css\)"' site/ --include="*.js"   # 无输出
$ grep -rn "@import\|url(.*\.css" site/ --include="*.css"    # 无输出
```

所以阈值常量不能靠新建一个共享 JS 模块来同步，必须用别的机制。

### C5 项目既有非目标不得违反

不引入前端框架或打包器；不放宽 CSP；审计工具保持站点专用；公开站点不做任何真实
上游调用（注意：从自己的 `/api/status` 取回快照发生在 **CI**，不是访客浏览器，
不违反这一条）。

### C6 门禁不能阻断它帮不上忙的工作

一个只会说「数据过期了」却没人能在当前上下文里修好的构建失败，会被绕过或被
`--force` 掉。门禁必须同时给出**在本仓库内可执行的修复命令**。

---

## 4. 正在评估的备选方案

按 Superpowers `brainstorming` 的要求，列出带取舍的候选，交由独立评审打分
（结果见 §5）：

| 方案 | 做法 | 取舍 |
|---|---|---|
| **A 最小改动** | 只在 `build-site.mjs` 里加一个 `generated_at` 断言 + 前端一句提示 | 满足验收但不解决 C1：定时刷新会架空门禁 |
| **B 数据层为准** | 导出期就把过期证据降级为 `unverified` 并写入说明，前端无需判断 | 兜底数据无法说谎；但历史快照重放时行为依赖导出时刻 |
| **C 三层纵深** | 数据层降级 + 前端显式降级 + 构建期门禁，各自独立生效 | 覆盖最全；面积最大，需防常量漂移（C4） |
| **D 全自动** | 定时任务同时刷新两条时钟并提交，门禁对两者都硬失败 | 依赖生产 API 的可用性与可信度（C3） |

评审方式：3 个互不可见的独立设计（YAGNI / 数据诚实性 / 运维可行性视角）+ 1 个
对抗性评审（唯一任务是证伪草案，每条缺陷必须引用真实证据）+ 3 个独立镜头评委
（correctness / maintainability / operability）+ 1 个综合定稿。

---

## 5. 设计定稿

> 待评审返回后补齐。
