# 站点质量基线与下一阶段开发计划 PRD

Status: 质量基线 + M1（CI 门禁与资源指纹）已实现；M2 起待排期
Owner: open-free-router
Last updated: 2026-08-03
Baseline: v0.3.0（11 提供商 / 55 模型 / 9 客户端 / 16 个公开页面）

---

## 1. 背景：本轮做了什么，为什么需要它

v0.3.0 之前，站点从 4 个页面长到 16 个，但**没有任何自动化质量门禁**：
`npm run test:web` 只覆盖 worker 的 3 个纯函数（18 个断言），页面本身
从未被机器检查过。结果是一批只在真实浏览器里才暴露的缺陷长期存在于
生产环境。

本轮引入全站审计工具 `npm run test:site` 后，一次扫描发现 **58 个问题
（1 critical / 42 major / 15 minor）**，全部修复，现在归零。

### 1.1 被漏掉最久、影响最直接的三类缺陷

| 缺陷 | 用户实际看到什么 | 为什么测试没抓到 |
|---|---|---|
| `style-src 'self'` 拦截了 `style="--meter/--score"` 属性 | 首页四条能力条、模型雷达整列「功能指数」条**全部渲染成 0 宽度** | 本地直接打开 HTML 文件不触发 CSP；只有真实浏览器 + 真实响应头才复现 |
| `frame-ancestors` 只写在 `<meta>` 里 | 点击劫持防护**从未生效**，且 15 个页面每次访问都往控制台打错误 | 浏览器静默忽略该指令，页面看起来完全正常 |
| `--dim: #587087` 对比度 3.0–3.7:1 | 12 个页面的小字说明、图例、时间戳**低于 WCAG AA 正文门槛 4.5:1** | 深色主题下肉眼「看得见」，但低视力用户和强光环境下读不了 |

### 1.2 结论

站点已经是产品的主要入口（安装指南、模型雷达、实时状态都在这里），
它需要和 Python 后端同等的测试待遇。**质量不能靠人工巡检维持** —— 这是
本 PRD 后续所有优先级排序的第一依据。

---

## 2. 已交付：质量基线（P0，本轮完成）

### 2.1 全站审计工具 `npm run test:site`

两层结构，静态层零依赖、永远可跑；浏览器层缺少 Chromium 时优雅跳过
而不是失败，保证裸检出和无浏览器的 CI 镜像里仍然有用。

**静态层**（`web-tests/site-audit.mjs`，无第三方依赖）
- 死链与资源缺失：解析全部 `href`/`src`，逐个核对磁盘（含 `?v=` 查询串剥离）
- 锚点完整性：页内与跨页 `#anchor` 必须在目标页面存在对应 `id`
- HTML 结构：唯一可见 `h1`、重复 `id`、`lang`、标题唯一性与长度
- 元数据：`title` / `description` / `viewport` / CSP meta
- CSP 合规：内联 `style=` 属性、`<style>` 块、内联 `<script>`
- 图片 `alt`、外链 `rel=noopener`
- `sitemap.xml` ↔ 真实页面双向一致；`robots.txt` 前缀覆盖判定

**浏览器层**（playwright-core + axe-core，可选依赖）
- 控制台报错、未捕获异常、失败请求、**运行时 CSP 违规**
- axe-core WCAG 2.1 A/AA 全量规则
- 三档视口（375 / 768 / 1440）横向溢出检测，自动定位越界元素
- 网络空闲后仍停在加载态的页面（门控页按表单存在自动豁免）
- 点击目标尺寸，按 WCAG 2.5.8 的 24×24px 门槛，并实现两条法定豁免：
  行内链接豁免、以及页面通过 `data-target-size="essential"` 显式声明的
  「呈现方式必要」豁免（散点图数据点属此类，页面已提供等价数据表格）
- 404 路径返回码与内容

退出码在存在 critical/major 时非 0，可直接作为部署门禁。

### 2.2 修复清单（58 项）

| 类别 | 数量 | 代表性修复 |
|---|---|---|
| CSP 拦截的内联样式 | 2 处（影响 2 个页面的核心可视化） | 静态值改 CSS 类；动态值改走 CSSOM `setProperty`（实测验证 CSP 不覆盖该路径） |
| 安全响应头缺失 | 15 页 | Worker 统一下发 `frame-ancestors`、`X-Frame-Options`、`nosniff`、`Referrer-Policy`、`Permissions-Policy`，`/internal/` 额外 `X-Robots-Tag` |
| 对比度不达标 | 12 页 | `--dim` #587087→#6f8eab；新增文字专用 `--blue-text`；品牌页浅色面板与色板标签单独修正 |
| 点击目标过小 | 7 页 | CTA 链接与图标按钮统一 `min-height:24px` |
| 行内链接仅靠颜色区分 | 2 页 | 正文段落内链接加下划线 |
| 元数据与索引 | 3 项 | 404 页补 CSP/description 并外置样式；internal 页补 description；robots 屏蔽 `/internal/` |

### 2.3 第二轮：对抗性深度审计（24 项）

机械审计能证明「链接不死、对比度达标、无 CSP 违规」，但证明不了
**页面说的话是不是真的**。第二轮用 6 个维度的独立审计 + 每条结论单独
对抗性验证，补上了这一层：

| 维度 | 代表性发现 |
|---|---|
| 内容准确性 | 首页宣传「Gemini 3.6 Flash · 1M context」「Nemotron 3 Ultra · 256K」——注册表里没有这两个模型，55 个模型**全部**是 131K；指南图示里 5 个模型 ID 复制过去直接 404；npm 指南让用户用 `--version` 验证安装，而该参数从未注册（退出码 2） |
| 数据契约 | `devlog.json` 存 `{label, href}`，`logs.js` 读 `link.url` → **13 条开发日志链接全部指向 `#`**；模型雷达离线兜底声称候选为 0，而 129 KB 的候选快照就部署在旁边 |
| Worker 韧性 | `/api/catalog` 把 discovery 放进同一个 `Promise.all`，models.dev 一挂整个模型雷达 503 |
| 无障碍语义 | 状态徽章与散点图**仅用颜色**表达可用性；`aria-live` 每次刷新重播整块看板；`/guide/npm/` 同时播报两个「当前页」；移动端导航面板无对话框语义、无焦点管理；地图标记在未悬停时无名称 |
| 视觉与布局 | 粘性顶栏遮挡 6 个锚点目标 |

最终确认 42 项（19 major / 23 minor），全部修复。其中一项值得单独记录：
**上一轮加的安全响应头是死代码**——`wrangler.jsonc` 的
`run_worker_first: ["/api/*"]` 决定了页面与静态资源请求由 Cloudflare 的
Asset Worker 直接响应，根本不进入 `fetch` 处理器。改用平台原生的
`site/_headers` 后，`wrangler dev` 实测五个头全部下发。

**结论**：两类审计互补且都必要——机械审计防回归，深度审计防失真，而且
**深度审计能发现机械审计自己修复中的错误**。后者成本高（本轮 61 个 agent），
适合按版本节奏跑，而不是每次提交。

另有一条反向验证：本轮补社交元数据时正则漏了闭合的 `>`，破坏了 13 个页面的
标签结构并导致 CSP 被浏览器丢弃——**是机械审计的浏览器层当场抓住的**。
已把「标签未闭合」「CSP 不在 head 内」补进静态层，让这类编辑快速失败。

### 2.4 验收

- `npm run test:site`：0 critical / 0 major / 0 minor
- `pytest tests/`：280 通过
- `npm run test:web`：18 通过
- `npm run test:npm`、`wrangler deploy --dry-run`：通过

---

## 3. 下一阶段需求

优先级依据：**能否防止已修复的问题重新出现** > **用户可感知价值** > **扩展面**。

### P0-1 CI 流水线 ✅ 已实现

**问题**：仓库至今没有 `.github/workflows/`。280 个 Python 测试、18 个 web
测试、全站审计**全部依赖人工在本地执行**。本轮修复的 58 个问题里，至少
CSP、对比度、死链三类会随任何一次页面改动悄悄回归。

**需求**
1. `ci.yml`：push 与 PR 触发，矩阵跑 Python 3.11/3.12 的 `pytest`、
   `npm run test:web`、`npm run test:npm`、`npm run build`。
2. `site.yml`：安装 Chromium 后跑 `npm run test:site`，critical/major 阻断合并。
3. 缓存 pip 与 npm 依赖，目标单次 < 5 分钟。
4. 失败时把审计 JSON 作为 artifact 上传，便于定位。

**验收**：新建一个故意引入内联 `style=` 或低对比度颜色的 PR，CI 必须红。

**补充**：内容准确性检查（页面宣称的模型 ID / 数量 / CLI 参数是否真实存在）
应做成构建期断言而非依赖人工或 LLM 审计——本轮发现的 4 类失真都可以用
「拿页面里的标识符去注册表和 argparse 里查」这一条规则机械捕获。

### P0-2 构建期资源指纹 ✅ 已实现

**问题**：27 个前端资源里只有 6 个带 `?v=20260803b` 手工版本号，其余 21 个
（**包括本轮刚改过的 `styles.css`**）没有任何缓存失效标记。手工维护版本号
必然遗漏——现状本身就是证据。

**需求**
1. `scripts/build-site.mjs` 按文件内容哈希重写引用：
   `/styles.css` → `/styles.<hash8>.css`，产物落到 `web/`，`site/` 保持干净。
2. 带哈希的资源下发 `Cache-Control: public, max-age=31536000, immutable`；
   HTML 保持 `must-revalidate`。
3. 移除所有手工 `?v=` 版本号。
4. 审计工具增加检查：`web/` 中任何被 HTML 引用的 CSS/JS 都必须带哈希。

**验收**：改一个字节的 CSS，构建产物文件名必须变化；未改动的资源文件名保持稳定。

### P0-3 公开目录数据新鲜度

**问题**：`site/data/catalog.json` 的 `generated_at` 停在 `2026-08-01T17:16Z`，
`status_as_of` 停在 `2026-08-01T15:00Z`。Worker 的 `/api/catalog` 会用 KV 里的
实时状态覆盖，但**静态兜底数据已经两天没更新**——一旦 KV 未命中或 API 降级，
访客看到的是过期快照，而页面上标注的时间戳会让他们以为是新鲜数据。

**需求**
1. 构建时校验：`catalog.json` 的 `generated_at` 超过 N 天则构建失败（默认 7 天）。
2. 定时任务把 `export-public-catalog.py` 的产物提交回仓库，或改为构建时生成。
3. 前端对超过阈值的快照显式降级提示（「静态兜底数据，最后更新 X 天前」），
   而不是照常渲染时间戳。

**验收**：把 `generated_at` 改成 30 天前，构建必须失败并给出明确提示。

### P1-1 英文站点

**问题**：13 个页面全部 `lang="zh-CN"`，仓库有 `README.en.md` 但站点无英文版。
项目接入的 11 家提供商中 6 家在美国，目标用户有相当比例不读中文。

**需求**
1. `/en/` 路径下提供首页、模型雷达、实时状态、安装指南四个核心页面的英文版。
2. `hreflang` 互链 + `sitemap.xml` 同步收录。
3. 数据驱动页面（雷达/状态）的中文字段（`description_zh`、`recommended_for_zh`
   等）需要英文对应字段，`public_catalog.py` 同步产出。
4. 审计工具的元数据/结构检查自动覆盖新页面（当前已按目录遍历，无需改动）。

**验收**：`npm run test:site` 对英文页面同样 0 critical/major；语言切换不丢失当前页面。

### P1-2 视觉回归与截图自动化

**问题**：README 里的 7 张截图是人工生成的一次性产物，站点改版后会静默过期。
本轮修复的对比度问题也没有任何视觉基线能证明「没改坏别的地方」。

**需求**
1. `scripts/screenshots.mjs`：复用审计工具的静态服务器，批量生成 README 与
   文档所需截图，输出到 `docs/screenshots/`。
2. 视觉回归：关键页面的基线图存仓库，CI 比对像素差异超过阈值则标注（不阻断，
   人工确认后更新基线）。
3. 截图与视觉基线共用同一套页面清单，避免两处维护。

**验收**：改动首页布局后，CI 产出 diff 图供 review。

### P1-3 性能预算

**现状测量**（HTML + 引用的 CSS/JS，不含图片）：

| 页面 | 总计 | 说明 |
|---|---|---|
| `/architecture/` | 68.5 KB | HTML 31 KB（大量内联 SVG） |
| `/guide/` | 63.0 KB | HTML 29 KB |
| `/benchmarks/` | 59.1 KB | |
| `/validation/` | 56.4 KB | |
| 其余 11 页 | 35–54 KB | |

`styles.css`（约 26 KB）被全部 16 个页面加载，但其中相当部分是首页专属样式
（hero、orbit 动画、live-strip 等），其他页面全部下载却不使用。

**需求**
1. 审计工具增加性能预算断言：单页 HTML+CSS+JS 上限 80 KB、图片上限 500 KB，
   超出则 major。
2. 拆分 `styles.css`：公共层（重置、令牌、topbar、footer、按钮）+ 首页专属层。
3. 大图检查：`site/assets/**` 逐个核对尺寸，超阈值的转 WebP 或降采样。
4. 内联 SVG 超过 20 KB 的页面（`/architecture/`）评估外链 `.svg` + `<img>`。

**验收**：全部页面进入预算；预算断言进 CI。

### P1-4 官方 Docker 镜像（承接 OmniRoute PRD 的 P2）

**需求**：多阶段构建、非 root 运行、`HEALTHCHECK` 打到 `/health`、
`docker-compose.yml` 示例挂载 `~/.config/open-free-router`。镜像不内置任何凭据。

**验收**：`docker run` 后 `open-free-router doctor` 在容器内通过；镜像 < 200 MB。

### P2 扩展项（承接既有 PRD，本轮不排期）

- MCP Streamable HTTP 传输（默认关闭，需管理 token 与 scope）
- 配置导入/导出与一键回滚
- 公开目录的 provider 插件 manifest 与 schema 校验
- 移动端 PWA 只读状态查看器（不承载任何凭据）

明确**不采纳**的方向沿用 `docs/PRD-omniroute-adoption.md` 第 7 节的判断
（Next.js/Electron 运行时、向量记忆、prompt 压缩、代理池、浏览器 Cookie 额度聚合）。

---

## 4. 里程碑

| 里程碑 | 内容 | 判定标准 |
|---|---|---|
| M1 · 门禁 ✅ | P0-1 CI + P0-2 资源指纹 | **已完成**：`ci.yml`（Python 3.11/3.12 + worker/npm/构建/静态审计/dry-run）与 `site.yml`（Chromium + 浏览器层审计）；27 个资源内容哈希化并下发 immutable |
| M2 · 数据可信 | P0-3 数据新鲜度 | 过期快照阻断构建并在前端显式降级 |
| M3 · 覆盖面 | P1-1 英文站 + P1-2 截图/视觉回归 | 英文四页上线且审计通过；截图由脚本产出 |
| M4 · 效率与分发 | P1-3 性能预算 + P1-4 Docker | 全部页面进入预算；镜像发布 |

M1 是其余所有里程碑的前置：**没有 CI，后面每一项都会重新退化成人工巡检**。

**M1 实测验收（2026-08-03）**

| 验收项 | 结果 |
|---|---|
| 故意引入内联 `style=` | 静态层报 major，退出码 1 |
| 故意把 `--dim` 改回低对比度 | 浏览器层 axe 报 4 处 color-contrast |
| 故意引入死链 | 静态层报 critical，退出码 1 |
| 未指纹资源被 HTML 引用 | 构建期守卫直接抛错 |
| 同样输入重复构建 | 文件名稳定（`styles.b840d91a.css`） |
| 改一个字节 | 文件名变化（`b840d91a` → `3ae071bc`） |
| `wrangler dev` 实测缓存头 | 指纹资源 `max-age=31536000, immutable`；HTML `max-age=0, must-revalidate` |

一处实现细节值得记录：CI 里的审计**不能**用 `npm run test:site -- --json`，
因为 npm 会把自己的横幅写进 stdout 污染 JSON 报告；必须直接调用
`node scripts/audit-site.mjs --json`。同时 workflow 显式断言
`browserSkipped` 为空——否则 Chromium 安装失败会让审计"跳过即通过"。

---

## 5. 非目标

- 不引入前端框架或构建期打包器（站点是静态 HTML + 原生 CSS/JS，这是刻意选择）
- 不为了视觉一致而放宽 CSP（本轮的 CSSOM 方案证明无需 `'unsafe-inline'`）
- 不把审计工具做成通用 linter；它只服务本站点的具体约定
- 不在公开站点上做任何真实上游调用；实时数据仍由维护者实例探测后发布快照

---

## 6. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| CI 中 Chromium 安装拖慢流水线 | 合并变慢 | 浏览器层拆成独立 workflow 并缓存浏览器；静态层留在主 CI |
| 资源指纹改动构建产物路径 | Cloudflare 缓存与 KV 键可能受影响 | 先 `deploy --dry-run` 验证，灰度一个页面 |
| 英文站增加双份维护成本 | 内容漂移 | 数据驱动页面共用同一份 JSON，只翻译静态文案；审计工具检查两侧页面集合一致 |
| 视觉回归基线频繁失效 | 噪音淹没真实回归 | 只对关键页面设基线，且不阻断合并 |
