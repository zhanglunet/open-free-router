# PRD-174/T1：诚实标注枚举与 doctor 字段规范

> trend issue #870。对应 PRD-174（中央仓库 zhanglunet/trend）。
> 地位：本文档是 PRD-174 的规范稿（沿用 `docs/plans/failover-routing-policy.md`、`docs/plans/2026-10-23-failover-audit-ledger.md` 的「宪法」文档地位惯例）。T2（doctor 输出改造）、T3（写入 PRD-149 账本）、T4（查询/CLI 展示）、T5（验收）均以本文为准；实现不得擅自增删枚举与字段。
> 红线：不把推断标成实测；**默认一律 AMBIGUOUS，绝不默认 EXTRACTED**（PRD 风险条目的工程化卡点）；不在账本写 API key（沿用 `failover_audit` 既有红名单与门禁）；纯加法设计，不改 PRD-059/149 已定型的枚举语义与既有 10 列。
> 借鉴声明：「诚实标注 EXTRACTED（实测）/ INFERRED（推断）/ AMBIGUOUS（存疑）」三态设计借鉴 [Graphify-Labs/graphify](https://github.com/Graphify-Labs/graphify)（Apache-2.0；版权与许可声明引用保留于中央 PRD-174，T2 落地时按仓库 `NOTICE.md` 惯例补充 graphify 的 Apache-2.0 声明引用）。本文与后续实现均为自研表述，不拷贝代码。

## 1. 现状衔接：仓库已有三条「不冒充」先例，本枚举是给它们命名

「诚实标注」不是发明新哲学，而是把仓库已存在的三套「不冒充实测」机制统一成一个可查询的枚举（2026-10-25 实测 main）：

| 已有先例 | 现状 | 与本枚举的关系 |
|---|---|---|
| `evidence.py:FreeTierEvidence.status()` | `verified/expired/unverified/unknown/invalid`，过期/缺来源即不宣称已核验 | 同一哲学：`verified` ≈ EXTRACTED 域、`unverified/expired` ≈ AMBIGUOUS 域；本枚举补上「推断」一态并把口径扩到 doctor 与账本 |
| `quota_view` 的 `source` | `upstream_headers/event/never_measured`；宪法 §4.3 要求区分「从未测过」与「测过但过期」 | `never_measured/stale` 就是 AMBIGUOUS 的事实源；`upstream_headers` 是 EXTRACTED 的事实源；本枚举把「来源」上升为**结论级**标注 |
| 宪法原则 2「按结构分类，不按文案猜」（`failover-routing-policy.md` §0） | 429 的 quota 措辞只影响冷却时长，不作永久判死依据 | 措辞派生的结论必须 AMBIGUOUS；本枚举使该约束**可断言、可过滤**，不再只是注释纪律 |
| PRD-149 账本（已合并 main） | `doctor_state` 表（`at/verdict/failed_checks`）、`failover_decisions` +8 上下文列、`failover_outcomes`，`user_version=3`；`doctor_summary_from_report` 把检查项归约为 `pass/warn/fail` | 本 PRD 的 T3 只做**纯加法扩展**（§4）；verdict 本身是汇总推导，属于 INFERRED（§3 矩阵） |
| `cli.py:cmd_doctor` 检查项 | `{ok, severity, label, detail}`，分组 checks/routing/free_tier/protocol_matrix/clients；`--json` 输出 | T2 的改造点：每条检查项增加 `honesty` 字段（§3），形状向后兼容 |

差距：**结论没有可信度等级**——`doctor --json` 与账本都只说「ok/error、pass/fail」，不区分「本轮实测出来的」和「从声明/规则推出来的」，更不区分「证据缺失只能存疑的」。T1 即补齐枚举定义与字段规范。

## 2. 三态枚举定义（判定规则表）

### 2.1 枚举与存储形式

- 常量：`HONESTY_LABELS = frozenset({"extracted", "inferred", "ambiguous"})`（落点 `failover_audit.py`，与 `FAILOVER_QUOTA_BANDS` 等既有枚举常量同区）。
- 存储/传输一律小写（账本列、`--json` 字段）；人读展示用大写（`EXTRACTED/INFERRED/AMBIGUOUS`）。
- 旧数据无此字段 → 展示层显示 `unlabeled`，**不回填、不默认成任何一态**（与账本 `incomplete` 的「不编造」同一纪律）。

### 2.2 定义与判定规则（T2/T3 实现的唯一口径）

| 标注 | 定义 | 判定充要条件（满足其一即可标） | 仓库内事实源示例 |
|---|---|---|---|
| `extracted`（实测） | 本轮进程内由**真实 I/O 的直接观测**得出，观测本体可复现 | 本轮实际发生的：文件/目录存在性检查、HTTP 状态码读取、响应头数字解析、探测请求成功、端口可达性探测 | registry.yaml 存在性、proxy 可达、`RateLimit-*` 头数字、probe 1-token 成功 |
| `inferred`（推断） | 由**结构化规则**从实测值（或声明值）确定性推导，规则公开、输入可查 | 推导输入存在且规则命中：severity 计数 → verdict、声明协议配置 → 兼容矩阵结论、`Retry-After` 数字 → 等待秒数、quota 头数字 → band | `doctor_summary_from_report` 的 `pass/warn/fail`、`protocol_matrix` issues、`quota_band=tight/exhausted` |
| `ambiguous`（存疑） | 证据缺失、过期、冲突，或仅来自**文案/声明** | 任一命中即标，且**优先于**其他两态：`missing_default`/`never_measured`/`stale` 路径；证据过期（`expired/unverified`）；错误仅来自响应文案措辞（429 quota 措辞）；信号互相冲突 | `quota_source=never_measured`、evidence `expired/unverified`、429 措辞分类 |

### 2.3 默认值规则（卡 PRD 风险的核心条款）

1. **凡代码路径没有拿到本轮真实信号，标注必须写 `ambiguous`**——不存在「想不起来就标 extracted」的分支；枚举赋值处必须显式，禁止把 `extracted` 设为任何函数参数的默认值（T2 单测断言，§6）。
2. 两态之间犹豫时取更低的可信态（extracted < inferred 不确定时 → ambiguous 不适用时才 inferred；即优先 ambiguous > inferred > extracted 的保守序）。
3. 标注描述**结论的证据来源**，不改变结论本身：`verdict=fail` 不因标注 ambiguous 而降级，`honesty` 与 `ok/severity` 是正交字段。

## 3. doctor 字段规范（T2 的契约）

### 3.1 检查项形状扩展

```json
{"ok": true, "severity": "ok", "label": "registry.yaml found",
 "detail": "/<config-dir>/registry.yaml", "honesty": "extracted"}
```

- `honesty` 必填于 `checks` 数组每项与 `routing`/`free_tier.issues`/`protocol_matrix.issues` 每条 issue；缺失视实现 bug（T2 单测卡）。
- 新增汇总块（`--json` 顶层）：`"honesty_summary": {"extracted": N, "inferred": N, "ambiguous": N}`——按结论条目计数，不按 severity 重复计。
- 人读输出沿用现有 `✓/⚠/✗` 前缀，行尾追加标签：`✓ registry.yaml found [实测]`、`⚠ protocol matrix: declared only [推断]`、`✗ free_tier expired [存疑]`。

### 3.2 检查组 × 标注矩阵（T2 逐项对表，T5 验收逐项核对）

| doctor 检查组（`cmd_doctor` 现状） | 标注 | 理由（§2.2 口径） |
|---|---|---|
| config.yaml / registry.yaml 存在性、providers/keys/models 计数 | `extracted` | 本轮真实文件与内存注册表读取 |
| proxy / dashboard 可达性 | `extracted`；探测未执行（依赖进程不在）时 `ambiguous` | 本轮端口探测为实测；未探测不得沿用上次结果冒充 |
| 协议兼容矩阵 `protocol_matrix` issues | `inferred` | 由**声明**的 provider 配置推导；README 既有口径「不冒充服务器可用」正是此意 |
| 路由配置诊断 `diagnose_routing_config` | `inferred` | 规则校验：声明字段 → 结论 |
| 免费证据 `free_tier`：字段有效（`invalid` 判定、verified 未过期） | `extracted` | 对声明的字段做本轮结构校验与时间比较（真实读取） |
| 免费证据：`expired/unverified` 状态 | `ambiguous` | 证据过期/缺来源 = 存疑，正对应 evidence 哲学 |
| 客户端配置存在性（clients 列表） | `extracted` | 文件存在性检查（提示性条目，不入账本，PRD-149/T1 §3.1 既有口径不变） |
| 顶层汇总 `ok/problem_count/verdict` | `inferred` | severity 计数的确定性推导；同时每条被汇入的下级结论自带自己的标注 |

矩阵之外**新增检查项**（含未来 PRD-173 的 byos 组）时，T1 规则：先在本文补行再实现；缺省标注按 §2.3 取 `ambiguous`。

## 4. 账本写入规范（T3 的契约，纯加法）

| 变更 | 内容 | 迁移 |
|---|---|---|
| `failover_decisions` 新增 1 列 | `honesty TEXT`（可空）：决策行中 doctor 上下文与 quota 上下文**整体**的可信度——取「组成字段中最保守标注」（`doctor_verdict` 为 inferred、quota band 来自 headers 为 inferred / 来自文案为 ambiguous / 无信号为 ambiguous → 决策行取其中最保守） | 沿用 `FAILOVER_DECISION_CONTEXT_COLUMNS` 的声明式加列模式 + `user_version` 3→4；`_migrate_failover_audit` 先例（逐名检查 `ADD COLUMN`、幂等）直接照抄结构 |
| `doctor_state` 新增 1 列 | `honesty_summary TEXT`（可空，≤128 字符）：compact JSON `{"extracted":N,"inferred":N,"ambiguous":N}`；与既有 `verdict/failed_checks` 同行写入 | 同上；列集合校验从严格相等改为「必需子集」判定需在 T3 显式处理（警示：`_initialize` 现有 `columns != {...}` 整库禁用的教训，PRD-149/T1 §2.1 已踩过） |
| 写入门禁 | `honesty` 加入 `DECISION_KEYS` allowlist；值必须在 `HONESTY_LABELS` 内，越界**折叠为 `ambiguous`**（不拒收整行——与 `error_kind` 越界折叠同一先例），并在 warning 里记录原值**类别**（不含原值以外上下文） | 沿用三层门禁：allowlist → `SENSITIVE_PATTERN` 拒收 → 永不中断推理 |
| 红线不变 | 账本仍不存 label/detail/错误原文/env 值；`honesty` 是枚举一个词，不是自由文本口子 | `doctor --json` 仍是明细唯一去处，经 `doctor_at` 关联 |

## 5. 查询与展示规范（T4 的契约）

| 视图 | 形状 |
|---|---|
| `failover-log` | 新增 `--honesty extracted|inferred|ambiguous`（可重复、OR 语义）；旧行（NULL）仅在不过滤时出现并标 `unlabeled`；`--json` 每行带 `honesty` 字段 |
| `failover-summary` | 新增按 `honesty` 的计数分布列；`ambiguous` 占比单独给出行（「存疑结论占比」是信任设计的核心指标） |
| `doctor --json` | §3.1 的 `honesty` + `honesty_summary`；退出码语义零变化 |
| 仪表盘 | 既有 failover 视图与 doctor 面板展示标注徽标（实测/推断/存疑三色）；不新增写操作 |
| MCP | `get_resilience` 等只读诊断工具的输出**不加**标注字段（MCP 诊断口径本就只返回聚合状态，PRD-149 范围外）；如后续需要走本文修订 |
| 公开边界 | 标注只进本机（usage.db / 本地面板），不进 catalog.json 与公开站点——沿用 PRD-096 公开投影红线 |

## 6. 反模式红线与测试锚点（T5 验收）

| # | 反模式（PRD 风险「默认全标 EXTRACTED」的卡点） | 测试锚点（T2–T5） |
|---|---|---|
| 1 | 把 `extracted` 设为默认参数值 | 单测：反射枚举赋值点，断言无 `default="extracted"` 字面量（§2.3 规则 1） |
| 2 | 未执行探测的检查项沿用上次结果 | 单测：proxy 不可达场景，断言该检查 `honesty=ambiguous` 且 detail 不含「上次」语义 |
| 3 | 推导结论标成实测 | 单测：`protocol_matrix` 每条 issue 均为 `inferred`；verdict 恒 `inferred`（§3.2 矩阵逐行断言） |
| 4 | 证据过期仍报已核验 | 单测：`expires_at` 过去时 → free_tier issue `honesty=ambiguous`（复用 evidence 现有状态机，不重写判定） |
| 5 | 越界标注混入账本 | 单测：`honesty="EXTRACTED "`（带空格/大写）→ 折叠 `ambiguous`，行仍写入；`api_key=<占位值>` 的行仍被 `SENSITIVE_PATTERN` 拒收（复用 PRD-149/T2 测试形态） |
| 6 | 迁移损坏存量库 | 单测：v3 库升级后旧行 `honesty` 为 NULL 且展示 `unlabeled`，不回填 |

样例（占位符，不含真实主机名/账号/密钥）：

```json
{"record":"decision","decision_id":"0f1e2d3c4b5a","at":"2026-10-25T12:00:00+00:00",
 "request_id":"ofr_example_002","from_candidate":"<provider-a>/<model-x>",
 "to_candidate":"<byos-oc>/<model-y>","reason":"rate_limited","attempt":1,
 "quota_band":"unknown","quota_source":"never_measured",
 "doctor_verdict":"warn","doctor_failed_checks":0,"doctor_at":"2026-10-25T11:58:00+00:00",
 "honesty":"inferred"}
```

（该行：doctor 上下文为推导 → inferred；若 quota 曾由文案推断或无信号主导，则整体取 `ambiguous`——最保守原则，§4。）

## 下一步

确认本规范后进 T2（trend issue #871）：按 §3 改造 `cmd_doctor`——检查项与 issues 逐条补 `honesty`（§3.2 矩阵为唯一口径）、`--json` 增 `honesty_summary`、人读输出加标签，样例输出可解析（`doctor --json | 解析 honesty` 全量通过）；T3（#872）按 §4 做纯加法迁移与门禁测试；T4（#873）按 §5 补过滤与分布；T5（#874）以「模拟 doctor + 换路：标注正确且无密钥入账」收口。
