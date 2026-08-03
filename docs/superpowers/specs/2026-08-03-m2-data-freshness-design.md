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

**【实施期修正】** 重新导出后重跑同一脚本，输出**逐字未变**。原因是
`registry.default.yaml` 里一条 free_tier 证据都没有（`grep -c` = 0），因此重新
导出后每个 `free_tier.status` 都是 `"unknown"`；而前端读的是
`row.free_tier?.status || "unknown"`，字段缺失时本来就回落到同一个值。
**字段缺失与字段存在但为 unknown，用户可见输出完全相同。**

所以本节的准确结论是：schema 漂移是真实的（门禁必须断言形状，否则今天 1.54 天的
年龄门禁会绿着通过），但它**不是**那一列显示「条件未知」的原因。真正的原因是
注册表里确实没有任何免费证据——一个独立的内容准确性问题（`/validation/` 页面
在宣称这些免费层已核验），记入 §5.3 接受清单，M2 不修。

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

评审结论：以「YAGNI 最小改动」方案的**形态**为底（面积最小、不要提交回仓库的
机器人、把可复现性 diff 当作真正的完整性检查），它被扣分的三处都是**遗漏**，
补进来很便宜；另两个方案被扣分的是**增项**，删起来很贵。从「数据诚实性」方案
嫁接 Worker 时效闸门、`benchmarks.js` 与 `status.js:65`；从「运维可行性」方案
嫁接提交时刻锚定、形状断言与线上产物监控。

### 5.0 一条重新定义里程碑的事实

**今天的快照年龄是 1.54 天。** 一个只看年龄的 7 天门禁**今天就是绿的**——它会
在什么都没修的情况下宣告通过。真正的缺陷不是「旧了两天」，而是**已发布产物是
上一代 schema**。因此门禁断言的是**形状，不只是年龄**，且 `schema_version === 2`
让门禁**第一天就是红的**，强制完成那次重新导出——那才是真正的交付物。

### 5.1 四层架构，每层做一件别层结构上做不到的事

| 层 | 保护对象 | 做法 |
|---|---|---|
| **L1 Worker** | 实时访客 | 提供商结论从**通过 `recent()` 存活下来的模型证据**推导，两者永不矛盾；`status_source` / `status_note` 以 `live` 存在为条件。这是**减少**一个时钟，不是新增 |
| **L2 前端** | 访客（不依赖重新构建、CI 或 Worker） | 每个兜底分支**无条件**降级。`catch` 分支本来就知道自己是兜底，无条件降级是「超过阈值」的严格超集——**因此任何前端文件里都不存在年龄常量** |
| **L3 构建** | 仓库 | `scripts/data-freshness.mjs` 一个纯函数：年龄（**锚定到 `min(now, HEAD 提交时刻)`**）、未来偏移 > 24h、`schema_version === 2`、计数自洽 |
| **L4 数据** | 让门禁不可伪造 | 用 `--now` 钉住已提交的 `generated_at` 重新导出并 `git diff --no-index`，跑在已有 `pip install -e ".[dev]"` 的 CI job 里。捕获年龄门禁结构上抓不到的东西：schema 漂移、空注册表 fail-open、Google `api` 变更 |

L2 的无条件降级把「四份常量互相镜像」的问题**从构造上消除**，而不是靠纪律维持。
L4 意味着维护者**无法靠手改一个时间戳把红色构建改绿**。

### 5.2 有争议的决定及其结论

**两条时钟仍然不对称执行，但理由被修正了。** 草案的前提（「只有持有密钥的维护者
能刷新 `status_as_of`」）是**错的**（§C2 已实测证伪）。但结论换个理由仍然成立：
构建按项目非目标不联网，且 `ci.yml` 的 node job 里没有 Python，所以构建期的
`status_as_of` 门禁**没法被构建自己清掉**；而针对陈旧可用性的访客侧保护现在在
L1、L2 已经是无条件的。**`status_as_of` 年龄只作为构建期信息行打印，永不使构建失败**，
改在能被处理的地方执行（§5.4）。

**提交时刻锚定，因此不需要逃生舱。** `staleness = min(now, HEAD 提交时刻) − generated_at`。
`git bisect` 把退出码 1 读成「坏」，而构建永远不会返回 125，所以用墙钟的门禁会
**静默污染一次与之无关的 bisect**；打 tag 重建、在旧提交上重跑 CI 同理。锚定之后：
一个 30 天前的提交带着 30 天前的目录，delta ≈ 0，通过；今天的 PR 带着 30 天前的
目录，delta = 30 天，失败——**正是 PRD 的验收用例**。这消除了逃生舱存在的全部
理由，所以 `OFR_ALLOW_STALE_CATALOG` **不做**。git 元数据缺失时回落到墙钟，
那是**更严格**的方向，失败安全；实际使用的锚点会被打印出来。

**刷新任务开 PR，绝不直接提交。** 对抗性评审证明了「只在 diff 超过时间戳行时才
提交」会自爆：输入全是仓库内静态文件，连续两次导出**只有** `generated_at` 不同，
于是这条规则会把时钟冻住，7 天后主干对每个无关 PR 都变红。两处修正：
(a) 内容变化**或** `generated_at` 超过 `REFRESH_MAX_AGE_DAYS = 3` 时都提交，
该常量由 `data-freshness.mjs` 导出并**用单元测试断言严格小于 `MAX_CATALOG_AGE_DAYS`**
——续期永远跑在门禁前面，且有测试保证；
(b) 开 PR 而不是 push，因为用默认 `GITHUB_TOKEN` 的 push **不会触发** `ci.yml` /
`site.yml`，直接提交会成为唯一没有任何检查的变更。开 PR 也让 `api` 字段或 schema
变化必须经人过目。

**状态投影必须单调安全。** 刷新任务可以把 `/api/status` 投影成
`docs/provider-status.json`，但只有一条规则：`available` 原样通过；**其余一律变成
`unverified`**，保留观测到的 reason。实测证明这条必要——此刻 `/api/status` 就把
google-ai-studio 和 openrouter 报成 `unavailable`，原因纯粹是**限流我们自己的探测**；
且 `PROBE_BATCHES = 2` 意味着任何单次快照里约一半模型没有证据。静态兜底数据
**绝不能**凭一次采样宣称某提供商挂了。

**导出期降级（草案 B.1）删除，不是推迟。** 没有任何消费者信任静态 `availability`：
`probe.js` 无新鲜证据时强制 `unverified`，`status.js` 在 `metadataOnly()` 里覆盖它，
L2 之后 `models.js` 与 `benchmarks.js` 也一样。它会引入第二个墙钟依赖，破坏 L4 的
可复现性 diff，还会变成 `tests/test_public_catalog.py` 里的日历定时炸弹。
它唯一真正坏掉的子部分**单独修**（W1）：`_speed_tier` 对 `unverified` 返回
**当前不可用**——一个肯定性的假陈述，且 `probe.js:235` 会把它泄漏到**实时**
`/api/catalog` 上。实测确认：

```
unverified   latency=None  -> 当前不可用
unverified   latency=800   -> 当前不可用
```

**不要拍脑袋的计数下限。** 「`provider_count >= 8`」这类数字离 `registry.default.yaml`
太远，掉了三个提供商也能静默通过。改为**内部自洽**（`provider_count === providers.length`、
`model_count === Σ models.length`，且都 > 0），它能抓住已实测的 fail-open：

```
$ python scripts/export-public-catalog.py --registry <不存在的路径> ...
Exported 0 providers / 0 models      ← 退出码 0，generated_at 崭新
```

**重新导出与探测修复必须在同一个提交里。** 实测三个 Google 端点：

```
400  /v1beta/models/gemini-2.5-flash:generateContent          ← 存在
404  /v1beta/openai/models/gemini-2.5-flash:generateContent   ← 不存在
400  /v1beta/openai/chat/completions                          ← 存在
```

重新导出会把 `google-ai-studio.api` 改成 `/v1beta/openai`，而 `worker/probe.js:42-59`
的 Google 特判会拼出中间那个 404，`failureReason(404)` 把它变成「模型或接口在
提供商侧不存在」→ `unavailable`。**只提交数据刷新会让 Google 在 M2 要修的那块
看板上变黑，而且看起来像提供商挂了。** 删掉这个特判也正好是正确的终局：
`/v1beta/openai/chat/completions` 存在，`proxy.py:410` 生产环境用的就是它，
而且这样就不必让手写逻辑耦合一个**生成文件**里的字段。

### 5.3 明确接受、本轮不修的

- **逐模型可用性是伪造的**（`public_catalog.py:95` 把提供商结论盖到全部 55 个模型上）。
  M2 范围外。新知的路径：`/api/status` **确实**带有以 `provider/model` 为键的逐模型
  证据，所以它不再卡在凭据上，而是卡在 `docs/provider-status.json` 的 schema 变更上。记入 M3。
- **没有部署工作流。** M2 内无法修复。用 §5.4 的线上监控缓解，并写进 PRD，
  这样 M2 不能在访客侧问题仍然存在时被标记为完成。
- **七个互不协调的陈旧度常量**（`recent()` 45 分、`statusIsStale()` 20 分、
  探测 15 分、discovery 7 小时、`/api/catalog` 300 秒、7 天、3 天）。
  M2 在 `probe.js` 加一段注释把七个都点名并交叉引用 `data-freshness.mjs`；
  统一它们是 M3。
- **指纹排序隐患**（`build-site.mjs` 先哈希后重写引用）。目前休眠——没有已发布的
  JS 引用被哈希的资源，且三个页面都用经典 `<script defer>` 加载，`import` 会先
  报 SyntaxError。加注释，不修。**这也是 L2 在三个文件里重复约 12 行
  `degradeCatalog()` 而不共享模块的正当理由**：`_headers` 把 `/*.js` 标为一年
  immutable，JS→JS 引用一旦漂移就是**硬 404 打死整页**，不是外观瑕疵。

### 5.4 `status_as_of` 真正被执行的地方

不在构建里，而在两个都不需要凭据的定时工作流：

1. `data-refresh.yml` —— 单调安全地投影 `/api/status` 并开 PR；探测卡住会表现为
   一个 `status_as_of` 不再前进的 PR。
2. `deployed-freshness.yml` —— 拉取 `https://oaf.asia/data/catalog.json`，
   `generated_at` 超过 14 天则失败。**这是唯一一个测量访客实际收到什么的检查**；
   构建期门禁测量的是 git。

外加 `site/_headers` 增加 `/data/*.json` 规则（`max-age=300, must-revalidate`）——
今天 `/data/*.json` 既没被指纹化也没有任何缓存规则，于是即便部署是崭新的，
访客也可能拿到比门禁阈值本身还旧的目录。
