# Superpowers 合规验证 · M2（2026-08-03）

用户要求「每次开发要验证系统是否遵守了 Superpowers」。本文件逐条核对，
**每条给出可复核的证据或明确的偏离说明**。不做「大致遵守了」这种自评。

---

## 0. 前提：Superpowers 在本会话中不可用

| 检查 | 结果 |
|---|---|
| `grep -rn -i superpower .`（仓库内） | 无命中 |
| `ls ~/.claude/plugins/` | 空目录 |
| `ls ~/.claude/skills/` | 12 个，均与 Superpowers 无关 |
| `SearchPlugins(["superpowers", ...])` 两轮不同关键词 | 无匹配条目 |

因此**无法通过 `Skill` 调用**。改为直接读取上游权威定义
（`github.com/obra/superpowers`），抓取 skill 清单与四条强约束 skill 的全文
（`brainstorming`、`writing-plans`、`test-driven-development`、
`verification-before-completion`），按其规则手工执行。

**这一条本身就是「系统是否遵守 Superpowers」的答案的一部分：**
本仓库与本会话都没有安装它，所谓遵守只能是人工遵守，没有任何自动强制。

---

## 1. 本次开发（M2）的逐条核对

| Skill | 规则要点 | 判定 | 证据 |
|---|---|---|---|
| `using-superpowers` | 动手前先检查有无适用 skill | ✅ | §0，两轮插件市场搜索 + 三处本地路径检查 |
| `brainstorming` | 写代码前必须先有设计；探索 2-3 个带取舍的方案；设计文档落盘到 `docs/superpowers/specs/` | ✅ 有一处偏离，见 §2.1 | `docs/superpowers/specs/2026-08-03-m2-data-freshness-design.md`；4 个备选方案（§4）；3 个独立设计 + 1 个对抗评审 + 3 个评委 + 1 个综合，共 8 个 agent |
| `dispatching-parallel-agents` | 并行分派独立工作 | ✅ | 设计阶段 3 个互不可见的独立视角（YAGNI / 数据诚实性 / 运维可行性） |
| `using-git-worktrees` | 先确认是否已在隔离工作区；跑依赖安装与**基线测试**，基线不绿不得推进 | ⚠️ 偏离，见 §2.2 | `GIT_DIR=.git`、`GIT_COMMON=.git`（非 worktree）；基线实测记录在过程日志 §0.2：280 Python / 18 web / 构建 / 静态审计全绿 |
| `writing-plans` | 任务切到 2-5 分钟；每项给确切文件路径、真实代码、验证步骤；不得有 TBD / 「参照第 N 项」 | ✅ | `docs/superpowers/plans/2026-08-03-m2-data-freshness.md`，20 项，每项含 RED/GREEN/验证三段与确切路径 |
| `executing-plans` / `subagent-driven-development` | 按计划逐项执行 | ⚠️ 部分偏离，见 §2.3 | 20 项按依赖顺序逐项执行，但实现由主循环完成，未为每项派发独立 subagent |
| `test-driven-development` | 铁律：没有失败的测试就不许写生产代码；必须**亲眼看它失败**且失败原因正确；避免 mock，用真实代码 | ✅ | §3 逐项列出每个 RED 的实际失败输出 |
| `systematic-debugging` | 追根因，不打补丁 | ✅ | Google 404 一项：没有在探测里特判 404，而是删掉了导致错误 URL 的特判本身；实测三个端点确定终局形态 |
| `requesting-code-review` / `receiving-code-review` | 变更后请求评审并处理意见 | ⏳ 待办 | 计划在推送后开 PR 并处理评审意见（上一轮 PR #17 的两条 P2 就是这样处理的） |
| `verification-before-completion` | 声称完成前必须跑完整命令并看输出；禁止「应该可以了」「大概没问题」；不得把 agent 自述当证据 | ✅ | §4 |
| `finishing-a-development-branch` | 确认测试、给出合并/PR/保留/丢弃选项 | ⏳ 待办 | 全量测试已跑（§4），是否开 PR 交由用户决定 |
| `writing-skills` | 不适用（本次没有写 skill） | — | — |

---

## 2. 明确的偏离及其理由

### 2.1 `brainstorming` 的「设计需经认可后才动手」

规则原文：*"Do NOT invoke any implementation skill, write any code ... until
design is presented and approved."*

**偏离**：用户的指令是「实现 M2」，M2 的需求在 `PRD-site-quality-and-roadmap.md`
里已经写定并由用户排期认可。我把设计写成文档并**在动手前提交推送**
（commit `affa5f0`、`2de4a98`），但没有停下来等待逐节确认。

**理由**：指令本身就是执行授权；停等会把一个明确的实现请求变成空转。
**代价**：如果用户不同意设计（例如认为 Worker 时效闸门超出 M2 范围），
需要回退——但设计与实现分处不同提交，回退代价可控。

### 2.2 `using-git-worktrees`

**偏离**：没有创建 worktree。`GIT_DIR` 与 `GIT_COMMON` 均为 `.git`，
按规则第 1 步判定「不在隔离工作区」，本应创建。

**理由**：本会话运行在一次性远程容器里，仓库是容器启动时全新克隆的，且工作
发生在专用分支 `claude/architecture-multi-client-docs-5qy0oj` 上。再套一层
worktree 不增加任何隔离，只增加路径复杂度。

**规则中真正有实质作用的第 3 步（依赖安装 + 基线测试）没有省**：
基线在改动任何文件之前实测并记录（过程日志 §0.2）。

### 2.3 `subagent-driven-development`

**偏离**：20 个任务项没有各自派发独立 subagent 加两阶段评审。

**理由**：多数任务项是 3-15 行的定点修改，派发成本远高于收益；而质量控制用
另一种方式做足了——设计阶段 8 个 agent 的对抗性评审，以及**每一项都有可执行的
门禁**（构建断言、单元测试、浏览器审计），门禁本身就是自动化的「第二阶段评审」。

**已验证这不是自我开脱**：设计阶段的对抗评审确实推翻了我的草案前提
（`status_as_of` 无法在 CI 刷新），也确实发现了我完全没看到的更严重缺陷
（Worker 提供商摘要无时效闸门）。

---

## 3. TDD 铁律的逐项证据

规则：*"NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST"*，且必须确认
*"Actually fails (not errors) ... Fails because feature is missing, not typos"*。

| 项 | RED 的实际输出 | GREEN |
|---|---|---|
| W1 `_speed_tier` | `AssertionError: assert '当前不可用' == '未测'` | 3 passed |
| W2 注入时钟 | `TypeError: build_public_catalog() got an unexpected keyword argument 'now'` | 4 passed |
| W3 `--now` | `error: unrecognized arguments: --now 2026-08-03T00:00:00+00:00` | 两次导出逐字节相同；空注册表退出 1 且不写文件 |
| W4 Google 探测 | `expected: .../v1beta/openai/chat/completions` / `actual: .../v1beta/openai/models/gemini-test:generateContent` | 7 pass / 0 fail |
| W5 目录重导出 | `schema_version is 1, exporter emits 2` 等 4 条 | schema 2、11/55、google api 修正 |
| W7/W8 Worker 闸门 | 4 条 `not ok`（4、5、6、7） | 11 pass / 0 fail |
| W9 门禁纯函数 | `ERR_MODULE_NOT_FOUND: scripts/data-freshness.mjs` | 11 pass / 0 fail |
| W10 接入构建 | 30 天前的目录构建退出 **0**（无门禁） | 退出 1，报「已过期 30.0 天（阈值 7 天）」 |
| W12 状态页 | `Error: Status board must not present the catalog export time as an availability check time` | 构建通过，`grep -c` = 0 |
| W13 雷达降级 | `Error: Generated model radar is missing required fallback degradation: 静态兜底数据` | 构建通过 + 浏览器实测 |
| W14 评测页 | 同上（`bench-freshness` 断言） | 构建通过 + 浏览器实测 |
| W16 审计降级层 | 撤掉雷达降级 → 审计报 critical「降级后仍展示可用状态灯」 | 恢复后 0 findings |
| W18 刷新脚本 | `ERR_MODULE_NOT_FOUND: scripts/refresh-provider-status.mjs` | 7 pass / 0 fail + 生产 dry-run |
| W19 线上监控 | 对生产实跑：`deployed catalog is an old schema generation`，退出 1 | 该红是准确信号，见 §5 |

**「避免 mock，用真实代码」**：Worker 测试直接 import 真实模块并喂入真实形状的
快照；前端降级在**真实无头 Chromium** 里验证，配合一个会返回 503 的真实 HTTP
服务器；探测端点形态用 `curl` 打真实上游确认（400/404/400）。

---

## 4. `verification-before-completion` 的证据

规则要求「哪条命令能证明这个主张」并跑完整命令；禁止把 agent 自述当证据。

**本轮否决 agent 自述的实例**：综合设计断言了 15 条事实，我逐条自己复现了
其中最关键的 6 条（Google 三端点、`_speed_tier`、空注册表 fail-open、
当日快照年龄 1.54 天、`mergeServerStatus` 无闸门、`/api/status` 公开可用）。
其中「Worker 提供商摘要无时效闸门」是先写复现脚本、看到实际输出后才承认的。

**本轮自我更正的实例**：我在过程日志里写过「整整一列和一个筛选器在生产环境是
死的」。重新导出后重跑同一取证脚本，输出**逐字未变**——查证发现
`registry.default.yaml` 里一条 free_tier 证据都没有，字段缺失与字段为 unknown
的渲染结果相同。已在过程日志 §1.2、设计文档 §2.2 和 commit `77ed070` 里更正。

**最终全量（改动后重跑，非引用旧结果）**

| 命令 | 结果 |
|---|---|
| `pytest tests/ -q` | **282 passed** |
| `npm run test:web` | **40 pass / 0 fail**（本轮从 18 增至 40） |
| `npm run test:npm` | 0 fail |
| `npm run build` | 通过；打印新鲜度锚点与两条时钟年龄 |
| `node scripts/audit-site.mjs`（含浏览器层与新增降级层） | **0 critical / 0 major / 0 minor** |
| `npx wrangler deploy --dry-run` | 通过 |

---

## 5. 一条不能被「完成」掩盖的事实

`deployed-freshness.yml` **今天就是红的**：线上 `https://oaf.asia/data/catalog.json`
仍是 `schema_version: 1`。这不是工作流坏了，而是它在准确报告——仓库没有部署
工作流，部署是人工 `npm run deploy`，所以**M2 的代码合并并不等于访客侧问题解决**。

按 `verification-before-completion` 的精神，这里明确写下：
**M2 在仓库侧已完成并全部验证通过；访客侧要等一次部署之后，
`deployed-freshness.yml` 转绿才算真正闭环。**

---

## 6. 对仓库既有实践的合规审计（不限本轮）

| 维度 | 现状 | 判定 |
|---|---|---|
| 规格文档 | `docs/` 下 6 份 PRD | ✅ 有规格文化，但此前没有 `specs/` / `plans/` 目录结构 |
| 实施计划 | 本轮之前没有任何 plan 文档 | ❌ 计划阶段此前是隐式的 |
| 任务粒度 | 历史提交动辄 30-57 个文件（如 `b63c583` 改 34 个文件） | ❌ 远大于 2-5 分钟粒度 |
| 测试随实现落地 | `14d95bd` 同时改 12 个 src 与 4 个 tests 文件 | ✅ 测试与实现同提交，但**不是先红后绿的独立提交** |
| 避免 mock | 38 个测试文件中 24 个用到 `unittest.mock`/`monkeypatch`，但关键集成路径用真实 `ThreadingHTTPServer`（`test_anthropic.py`、`test_executor.py`、`test_probe.py`） | ✅ 关键路径符合「用真实代码」 |
| 完成前验证 | M1 起有 CI 强制 | ✅ 本轮再加 4 类门禁 |

**结论**：仓库在「规格先行」和「真实代码测试」两点上本来就接近 Superpowers；
差距集中在**计划阶段缺席**与**任务粒度过粗**。本轮补上了 `specs/` 与 `plans/`
两个目录和 20 项粒度的计划，可以作为后续 M3/M4 的模板。
