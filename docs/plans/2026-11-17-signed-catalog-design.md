# PRD-466/T1 签名目录自更新设计

> PRD：`trend adopted/PRD-466-ofr-signed-catalog-sync.md`（issue #2250）
> 状态：设计草稿，待评审
> 借鉴源：tashfeenahmed/freellmapi，MIT 协议——只借鉴思路，不复用代码（对方 TypeScript，本仓库 Python）。其 live 目录源是付费服务：机制可借，数据流不可白嫖，本设计不接该源。
> 关联：PRD-060（协议归一，§8 记有 10-vs-11 提供方计数漂移问题）；PRD-059（故障转移）；PRD-467（按密钥额度跟踪，额度数值口径与本设计共用目录字段）

## §0 现状与问题（2026-11-17 实测主分支）

先把现状钉死，后面的设计每一处都对着它长出来：

| 现状 | 实测 |
|---|---|
| 运行时目录真相源 | `src/open_free_router/registry.default.yaml`（随仓库发布的基线）+ 用户运行目录 `registry.yaml`（config 可配，默认相对数据目录；`Registry.save` 会改写它，备份保留 10 份 `*.bak-时间戳`）。`registry.py` 是目录 CRUD 的唯一入口。 |
| 提供方数量 | `registry.default.yaml` 现有 **11 家**：deepseek、bigmodel-glm-coding、google-ai-studio、groq、nvidia-nim、nous、poolside、opencode-zen-free、openrouter、sensenova、stepfun。模型条目带 `id / upstream_id / reasoning / tool_calling / prefix`，每家带 `api_key_env`；`refresh_method` 多为 manual，个别是上游 API（如 poolside_api），`auto_refresh` 仅 poolside 为 true。 |
| 静态画像文件 | `docs/provider-profiles.json`：中文画像（背景/优势/注意事项/官方链接/取 key 步骤）+ `free_tier`。额度数字字段（requests_per_day / tokens_per_minute / rate_limit_rpm）多为 null，`confidence` 标 `estimated` 或 `documented`，`updated_at` 2026-10-18。人工维护，随仓库走。 |
| 已发布目录快照 | `site/data/catalog.json`（schema_version 2）：`provider_count` 11、`model_count` 40，`generated_at` 2026-11-11。由每日定时任务重新导出。 |
| 定时刷新流程 | `.github/workflows/data-refresh.yml`：每天 04:23 UTC 跑；先从已部署 Worker 的 `/api/status` 取回可用性证据（`scripts/refresh-provider-status.mjs`），再用 `scripts/export-public-catalog.py` 把 registry + provider-status.json + provider-profiles.json 拼成 catalog.json；**只开 PR 不直接推**，并有新鲜度门禁（`MAX_CATALOG_AGE_DAYS`，测试断言刷新间隔小于该值）。 |
| 060/T1 §8 的旧账 | 当时记的是"registry 只有 10 家，不是 PRD 宣称的 11 家"。2026-11-17 实测：registry 已是 11 家，与 catalog.json 的 11 家**数字上已对上**。但没有任何测试钉住这个等式——数字对上是巧合还是真对齐，没人能证明。这正是 T4 要补的回归测试点（§8）。 |

问题一句话：目录每周都在变（新模型上线、旧模型下线、额度调整），现在全靠人工改仓库里的静态文件再等每日定时任务重新发布；没有签名、没有版本底线、没有"谁改的"记录，计数漂移只能靠人眼发现。

## §1 目标与非目标

目标：让提供方目录能安全地自更新——有签名才信、有版本底线不回滚、用户改动不被覆盖、漂移能被发现。

非目标（写死，不在本 PRD 做）：

- 不接 freellmapi 的 live 目录源（付费服务）。本设计的目录源是**自托管**（来源见决策点 D2），机制照搬，数据自己产。
- 不改现有路由与故障转移逻辑（routing.py / executor.py / failover.py / translate.py 零改动）。目录更新只改"有哪些模型、额度多少、谁在线下"，不改"怎么选路"。
- 不做实时可用性探测（那是 probe.py / data-refresh 的事，见 §9 边界）。
- 不引入付费档位（freellmapi 的 live/monthly 两档与许可证状态机制不借鉴；本设计单档，格式里留 `tier` 字段备用，见 §2）。

## §2 签名目录格式

### 2.1 文档结构

目录是一份 JSON 文档，HTTP 拉取，签名放在响应头里（不在正文里，避免"签名签了正文、正文又含签名"的自指）：

```json
{
  "version": "2026.11.17",
  "generated_at": "2026-11-17T00:00:00+00:00",
  "tier": "standard",
  "providers": [
    {
      "platform": "groq",
      "display_name": "GroqCloud",
      "enabled": true,
      "models": [
        {
          "model_id": "llama-3.3-70b",
          "upstream_id": "llama-3.3-70b-versatile",
          "display_name": "llama-3.3-70b",
          "context_window": 131072,
          "reasoning": false,
          "tool_calling": false,
          "supports_vision": false,
          "supports_tools": false,
          "limits": {"rpm": null, "rpd": null, "tpm": null, "tpd": null},
          "monthly_token_budget": null,
          "enabled": true
        }
      ]
    }
  ],
  "quirks": [
    {
      "slug": "example-quirk",
      "title": "占位标题",
      "body": "占位说明",
      "severity": "info",
      "targets": [{"platform": "groq", "model_glob": null}]
    }
  ]
}
```

字段表：

| 字段 | 类型 | 说明 |
|---|---|---|
| `version` | string，`YYYY.MM.DD` | 目录版本号，见 §4。每次发布递增，同一天多次发布加后缀 `-2`、`-3`。 |
| `generated_at` | string，ISO 8601 | 生成时间。只作展示与排障，不参与版本比较。 |
| `tier` | string | 固定 `standard`。为将来留的兼容字段，当前不做分档。 |
| `providers[].platform` | string | 提供方 id，与 registry.default.yaml 的顶级键同一命名空间。未知 platform 的条目整条跳过（旧版本客户端不认识新提供方时不报错，见 §6）。 |
| `providers[].enabled` | bool | false = 整家下线（强制，见 §5）。 |
| `models[].model_id` / `upstream_id` | string | 与 registry.yaml 同义。`upstream_id` 缺省时等于 `model_id`。 |
| `models[].context_window` | int | 上下文窗口（token）。 |
| `models[].reasoning` / `tool_calling` / `supports_vision` / `supports_tools` | bool | 能力 flag。缺省 false。 |
| `models[].limits` | object | `{rpm, rpd, tpm, tpd}`，未知填 null，**不许编数字**（与 provider-profiles.json 现有口径一致：没验证过的数字留空）。 |
| `models[].monthly_token_budget` | string \| null | 额度标签（如 `"1M"`），只作展示与护栏参考，不参与路由计算。 |
| `models[].enabled` | bool | false = 该模型下线（强制，见 §5）。 |
| `quirks[]` | array | 运营建议（见 §8）：`slug / title / body / severity(blocker\|warning\|info) / targets[{platform, model_glob}]`。每次同步整批替换。 |

结构校验（`isCatalog`，T2 实现）：先校验顶层形状（version 格式、`providers` 为数组、每条有 platform 与 models 数组），形状不对整份丢弃并报错——不许"修修补补再应用"。

### 2.2 签名头与验证流程

- 算法：Ed25519。签名是对**响应正文字节**的签名，Base64 编码后放在响应头 `x-catalog-signature` 里。
- 验证顺序（写死，T2 照此实现）：先验签，验签通过才解析 JSON；验签失败连解析都不做。顺序不能反——解析不可信输入本身就是风险。
- 超时：单次拉取 20 秒超时，超时按拉取失败处理（§6）。
- 验签失败：整份丢弃 + 大声报错（日志里写明 `catalog signature verification FAILED — discarding response`，不许静默吞掉）。
- 缺签名头：同样丢弃 + 报错。没有"无签名也接受"的降级路径。

## §3 密钥与信任模型

- 公钥**钉死在代码里**：Ed25519 公钥以常量形式写进源码（随仓库发布）。私钥从不离目录源站，源站被攻破也只能换私钥——换钥需要发版（新公钥随代码走评审进仓库），这是有意为之：换钥是高可见动作，不许静默换。
- 自托管源覆盖（环境变量，T2 实现）：
  - `OFR_CATALOG_BASE_URL`：目录源地址，默认占位值（见决策点 D2，默认值待评审定）。
  - `OFR_CATALOG_PUBKEY`：覆盖钉死的公钥，仅用于自托管场景。未设置时用钉死值。
  - `OFR_CATALOG_SYNC_DISABLED=1`：总开关，关闭自动同步（本地缓存继续服务）。
- 信任边界写清：签名只证明"这份目录确实是目录源签发的、且中途没被改过"，**不证明目录内容正确**。额度数字写错、模型标错能力，签名拦不住——那是 §8 漂移检测与人工评审的事。

## §4 版本号底线规则

- 版本格式：`YYYY.MM.DD`（可加 `-2`、`-3` 同日后缀），按日期字典序比较。拒绝语义化版本之外的花样。
- 底线（floor）：随仓库发布的基线带一个 `MIN_CATALOG_VERSION`（等于基线 registry.default.yaml 生成时的目录版本，随基线更新而提高）。规则：
  - 拉到的 `version < MIN_CATALOG_VERSION` → `skipped_older`，整份跳过。旧快照永远不能把新版本基线里已有的模型回滚掉。
  - `version == 已应用版本` → `up_to_date`，什么都不做（配合 HTTP 304，见 §6）。
  - `version > 已应用版本` → 走 §5 覆盖规则应用。
- 已应用版本与已验证文档的持久化：存在独立状态文件（§7），不写进 registry.yaml。启动时先读状态文件，再决定要不要拉取。

## §5 覆盖规则表

核心原则：**目录是建议，用户是主人**。每一行（提供方 / 模型）都有来源标记 `source`，取值 `catalog`（目录同步建的）、`user`（用户手工加的 / 声明式配置）、`baseline`（随仓库基线来的）。来源标记存在 §7 的状态文件里，**不改 registry.yaml 的文件格式**（Registry CRUD 不用动）。

| 情形 | 规则 |
|---|---|
| 目录里的元数据（名字、额度、上下文、能力 flag） | 跟目录走，**除非**该行被用户显式覆盖过（用户改过的字段钉住不跟）。 |
| `enabled: false`（整家或单个模型） | **强制下线**：无论来源，一律停用。死掉的模型不许继续被路由。 |
| `enabled: true` | **绝不重新启用**用户关掉的行。用户关的只能由用户再打开。 |
| 用户自定义行（`source=user`） | 永不更新、永不删除、永不被目录"收编"。平台+模型 id 撞上时用户行赢，目录条目跳过。 |
| 用户删掉的行 | 进墓碑（§8）：后续目录再出现同名模型也不许悄悄复活，须人工确认。 |
| 目录里消失的模型 | 只有 `source=catalog` 的行才删除；`source=user` / `baseline` 的行不动。删除前先清掉引用（回退配置等，按外键顺序）。 |
| 目录里出现的新提供方 | 整条跳过并记入同步日志（`skippedUnknownPlatform` 计数），不报错、不自动接纳。新提供方须经人工评审进基线。 |
| `quirks` | 每次同步整批替换（DELETE + INSERT 语义），与模型行互不干扰。 |

## §6 拉取节奏与失败降级

- 节奏：启动后延迟 10 秒跑一次，之后每 12 小时跑一次；另支持手动触发（CLI 子命令，T2 定名）。这是 freellmapi 的节奏（启动延迟 + 每 12 小时），照搬。
- 条件请求：带 `?since=<已应用版本>`，源站返回 304 即 `up_to_date`，只更新"上次同步时间"，不写任何数据。
- 失败处理（全部写死，T2 的验收锚点）：
  - 网络超时 / 非 2xx / JSON 形状非法 / 缺签名 / 验签失败 → **整份丢弃**，本地缓存目录继续服务，当前请求不受任何影响。
  - 验签失败额外记一条大声错误日志（§2.2）。
  - 重试策略：失败后按退避重试（T2 定具体间隔与上限，默认不建议短于 5 分钟），重试只影响下一次拉取时机，不阻塞请求路径。同步任务永不在请求处理线程里跑。
- 降级底线：同步服务挂掉时，网关表现与"没有同步功能"完全一致——用本地 registry.yaml 继续跑。同步是锦上添花，不是单点故障。

## §7 事务应用与启动重放

- 原子应用：先在临时文件里拼好新 registry.yaml，校验通过后重命名覆盖；写坏中途失败时旧文件纹丝不动。沿用 registry.py 已有的备份习惯（`*.bak-时间戳`，保留 10 份）。
- 应用本身是一个事务：要么整份目录生效，要么整份不生效，不许"一半新一半旧"。
- 状态文件：`data/catalog-state.json`（路径待 T2 定死），存 `applied_version / applied_tier / applied_json（已验证的整份文档）/ last_sync_ms / last_error`。`applied_json` 的作用是启动重放——
- 启动重放问题（从 freellmapi 的教训照搬）：基线会在每次启动时被重新断言；如果启动时不把已应用的目录重放一遍，重启会把数据库（这里是 registry.yaml）漂回基线，直到下次目录版本更新。所以启动流程是：读基线 → 重放已缓存的已验证目录（纯本地，无网络）→ 再安排网络同步。没有缓存文档的老安装（只有版本号）→ 清掉版本号，下次拉取取全量。
- 同步日志：每次同步记 `updated / inserted / removed / skippedUnknownPlatform / quirks` 五个计数，供 §8 的漂移报告与看板（PRD-201）消费。

## §8 漂移检测与墓碑的设计口径（为 T4 留契约）

- 差异报告格式（T4 实现，`catalog drift` 输出）：每次同步生成一份，字段固定——`version_from / version_to / added[] / removed[] / changed[{platform, model_id, field, old, new}] / disabled[] / skippedUnknownPlatform[]`。报告落盘（路径 T4 定），并记入同步日志。
- 墓碑（tombstone）：用户删掉的模型行进墓碑表（状态文件内一节），字段 `platform / model_id / deleted_at / deleted_by`。墓碑命中时目录里的同名条目被跳过并记入报告；复活只能人工删墓碑。**墓碑永不自动过期。**
- 10-vs-11 回归测试点（T4 的验收锚点，针对 060/T1 §8 的旧账）：
  - 测试 1：registry.default.yaml 的提供方数 == provider-profiles.json 的提供方数 == catalog.json 的 `provider_count`，三处不一致即红。
  - 测试 2：一次模拟同步（版本升级、某模型 `enabled:false`、某模型从目录消失）后，漂移报告的 added/removed/disabled 三类计数与预期一致。
  - 测试 3：墓碑模型在下一版目录里重新出现，仍不复活。
- `quirks` 的展示口径：严重级别 `blocker / warning` 的条目由看板（PRD-201）展示，`info` 只记日志。T4 不做看板 UI，只定数据格式。

## §9 与 provider-profiles.json / data-refresh 的边界（写死）

本节是验收标准的一部分，边界改动须重新评审。

| 事项 | 归属 | 边界 |
|---|---|---|
| 运行时目录（有哪些模型、额度、上下线） | **本 PRD（签名目录）** | 签名目录是运行时目录的上游：目录 → registry.yaml。 |
| 中文画像（背景/优势/注意事项/取 key 步骤） | **provider-profiles.json（人工）** | 签名目录**不写**画像字段。画像继续人工维护、随仓库走。目录里的 `display_name` 只作内部展示名，不覆盖中文名。 |
| 额度数字的"可信来源" | **分工** | 目录 `limits` 字段是额度数字的机器来源；provider-profiles.json 的 `free_tier` 数字字段仍可由人工填写（带 `confidence` 标注）。两者冲突时，以目录为准，但须进漂移报告（`changed` 类），不许静默覆盖。 |
| 已发布目录快照 catalog.json | **data-refresh（不变）** | data-refresh 仍是 catalog.json 的唯一发布者：registry.yaml + provider-status.json + provider-profiles.json → catalog.json，每日 04:23 UTC，只开 PR。签名目录**不直接写** catalog.json。 |
| 可用性证据 | **data-refresh / probe（不变）** | 目录不管"现在能不能用"，只管"目录里有没有"。可用性仍由 probe 与 data-refresh 的 `/api/status` 回填。 |
| 新鲜度门禁 | **不变** | `MAX_CATALOG_AGE_DAYS` 门禁继续对 catalog.json 生效；签名同步另有自己的 `last_sync_ms`，两者不混用。 |
| 上游关系（一句话） | 签名目录 → registry.yaml → data-refresh 导出 → catalog.json。画像文件 provider-profiles.json 在旁边平行输入，不参与同步。 | |
| 冲突仲裁 | 签名目录与 data-refresh 对同一计数（提供方数、模型数）说法不一时，**以漂移报告为准**，catalog.json 的生成应大声失败（不发布漂移的快照），而不是悄悄取一边。 | |

一句话总结：签名目录管"目录长什么样"，data-refresh 管"目录怎么发布"，provider-profiles.json 管"目录怎么向人介绍"。三者各管一摊，互不越界。

## §10 T2–T5 契约

### T2 签名验证与拉取（issue #2251）

- 输入：本设计 §2（格式）、§3（密钥）、§6（节奏与降级）。
- 输出：拉取模块 + 验签模块 + 同步调度（启动延迟 10 秒、每 12 小时、手动触发、总开关 `OFR_CATALOG_SYNC_DISABLED`）；环境变量 `OFR_CATALOG_BASE_URL / OFR_CATALOG_PUBKEY`。
- 验收锚点：伪造/篡改签名被拒绝且记大声错误；验签失败不影响当前服务；拉取失败有明确降级路径（本地缓存继续服务）；超时 20 秒。
- 不得做：不写应用逻辑（那是 T3）、不改 registry.yaml 格式、不接 freellmapi live 源。

### T3 事务应用与覆盖规则（issue #2252）

- 输入：本设计 §5（覆盖规则表）、§7（原子应用与状态文件）。
- 输出：事务应用模块 + 状态文件 `data/catalog-state.json` + 来源标记（`source`）的读写；启动重放逻辑。
- 验收锚点：用户改动在更新后保留（关掉的不被打开、自定义行不被覆盖）；旧版本目录无法回滚已上线模型；更新原子性（中途失败旧文件完好）。
- 不得做：不做漂移报告 UI（T4）、不改路由逻辑。

### T4 漂移检测与模型下线墓碑（issue #2253）

- 输入：本设计 §8（报告格式、墓碑、回归测试点）。
- 输出：`catalog drift` 差异报告 + 墓碑表 + §8 的三项回归测试。
- 验收锚点：漂移报告可生成且字段齐全；10-vs-11 计数问题有回归测试（测试 1 三处计数一致）；墓碑模型不会被后续更新复活。
- 不得做：不做看板 UI（PRD-201 的事）、不自动复活墓碑。

### T5 验收试运行（issue #2254）

- 输入：T2–T4 全部落地。
- 输出：验收记录（签名伪造拒绝、旧版本不回滚、用户改动保留、漂移报告生成、墓碑不复活五项演练），结论可复核。
- 验收锚点：五项全部通过；验收记录落 docs（路径 T5 定）。

## §11 待评审决策点

| 编号 | 问题 | 默认建议 | 影响 |
|---|---|---|---|
| D1 | Ed25519 验签用哪个库 | 优先复用仓库已有依赖；若无，T2 在 `cryptography` 与 `PyNaCl` 之间选一个并说明理由；不许自写验签算法 | T2 |
| D2 | 目录源托管在哪 | 自托管：建议 GitHub Release 附件或自有静态端点，二选一由评审定；默认值在 T2 落地前不得写死成 freellmapi 的地址 | T2 |
| D3 | 目录是否带可用性证据 | 不带。可用性仍归 probe / data-refresh（§9）。若评审坚持要带，须另立 PRD，不得塞进本 PRD | T3 |
| D4 | 状态文件路径 | 建议 `data/catalog-state.json`，与 registry.yaml 同目录；T2 定死 | T2/T3 |
| D5 | `tier` 字段是否需要分档 | 不需要，固定 `standard`；将来要分档时另立 PRD | 无 |
| D6 | 目录更新是否触发 data-refresh 立即重发 | 默认不触发，等每日 04:23 UTC 的常规刷新；紧急下线（`enabled:false`）是否触发一次即时重发，由评审定 | T3/T4 |

## §12 借鉴声明

- 借鉴源 tashfeenahmed/freellmapi（MIT）：签名验证流程（先验签后解析、验签失败整份丢弃）、版本号底线（`MIN_CATALOG_VERSION`）、覆盖规则（用户行永不覆盖、enabled:false 强制下线、enabled:true 不重开用户关掉的行）、墓碑、启动重放、quirks 整批替换、同步日志五计数——以上均为**思路借鉴**，无代码复用（语言不同）。
- 未借鉴：其 live/monthly 两档与许可证状态机制（本仓库无付费档）；其 live 目录源（付费服务，不接）。
- 本设计中所有字段名、环境变量名、文件路径均为本仓库新定，与借鉴源不共享命名。
