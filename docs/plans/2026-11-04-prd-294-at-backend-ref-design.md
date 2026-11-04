# PRD-294/T1：路由指令 @后端引用解析设计（引用语法 / @名→id schema / 不健康报告 / 禁止自动换路）

> trend issue #1471。对应 PRD-294（中央仓库 zhanglunet/trend，`adopted/PRD-294-ofr-at-backend-ref.md`）。
> 设计日：2026-11-04 · 目标仓库：zhanglunet/open-free-router
> 状态：T1 设计草案，待评审。仅设计文档（纯加法 .md），不含任何实现代码；解析实现（T2）、不健康报告与禁止自动换路闸门（T3）、划界断言（T4）、全流程回放验收（T5）按本设计后续落地。
> 示例全部虚构占位（`@后端名`、`provider-x` 沿用仓库既有文档占位风格），不出现真实主机名、IP、账号、密钥。

## 0. 红线（本 PRD 全程不可越，置顶声明）

| # | 红线 | 落地方式 |
|---|---|---|
| R1 | **不健康先报状态，不自动换路** | @引用解析出后端 id 后附带当前健康状态；不健康 → 解析结果 `healthy=false` 显形并交人工决策——**解析层不存在换路调用**（T3 断言换路路径从解析器不可达） |
| R2 | **学习网关只出推荐、不自动执行**（全仓红线不变） | 本条只做寻址与状态报告；换路建议（若有）走既有 gateway_recommend → learning_confirm 人工确认链，本条不触碰 |
| R3 | **解析不到或有歧义停下问/报错，不猜** | 候选 0 → `REF_NOT_FOUND`（附可用后端清单摘要）；候选 >1 → `REF_AMBIGUOUS`；无模糊词典 |
| R4 | **解析审计留档** | 每次解析追加审计记录（§6），只增不删 |
| R5 | **示例全虚构；文档与示例无真实主机名/IP/账号/密钥；实现类 PR 只开不合并** | 占位符纪律 + 分支 PR 纪律，全程适用 |

## 1. 借鉴对象、许可证与不复用红线

借鉴对象：macro-inc/macro，**AGPL-3.0（2026-10-29 经 GitHub License API 实测）**。

- **复用**：仅借鉴「@引用、不复制」的**操作形态**。
- **不复用**：AGPL-3.0 传染性——**零代码复用、零依赖引入**；解析器（T2）自研，风格对齐本仓既有「审计账本 + 故障分类占位化 + 确定性纯函数」纪律（failover_audit / availability / gateway_recommend 同族）。

## 2. 与既有机制划界（T4 断言预埋）

| 维度 | PRD-294（本条） | PRD-257 统一路由 | PRD-280 配置快照 |
|---|---|---|---|
| 管什么 | **指令寻址**：@后端名 → id + 健康 | 路由协议层（请求怎么发） | 配置可逆（快照/回滚） |
| 失败语义 | 报状态不换路 | failover 链路行为 | 版本回退 |
| 执行语义 | 零执行（报告面） | 执行请求 | 执行配置写 |

本条解析出的后端 id 供路由协议层与快照机制消费；三者只共享 id 词表。

## 3. 引用语法

### 3.1 形态（T2 实现口径）

| 形态 | 示例（占位） | 解析目标 |
|---|---|---|
| 后端引用 | `@后端名`（即 `@provider-x（占位）`） | 后端 id + 当前健康状态 |

### 3.2 禁止形态（出现即 `REF_SYNTAX_ERROR`，不尝试理解）

- `@provider-x#failover`——引用不带策略后缀（failover 策略是路由协议层语义，PRD-257）；
- `@provider-x+2`——引用不带权重/倍率修饰；
- 组合引用 `@provider-x,@provider-y`——一条引用一个目标（组合路由是协议层配置，不是寻址语法）。

## 4. @名→id 解析 schema

### 4.1 产物 schema（T2 预埋）

```json
{
  "schema": "ref-resolution/v1",
  "raw": "@provider-x（占位）",
  "kind": "backend",
  "backend_id": "provider-x",
  "health": {"status": "healthy|degraded|down|unknown", "checked_at": "<注入时刻>", "source": "availability 模块（只读）"},
  "meta_summary": {"models": ["…（占位）"], "byok": true},
  "resolution": {"status": "resolved", "candidates": 1, "strategy": "exact"}
}
```

- `health` 为**报告字段**（R1）：解析器只读 availability 模块现成状态，不主动探活、不等待、不重试——探活节奏归既有健康检查机制；
- `status=unknown`（该后端从未被健康检查覆盖）是合法值，显形报告而非猜测。

### 4.2 解析步骤（确定性）

1. 取候选集：后端注册表（BYOS/内置注册口径，只读）；
2. 精确匹配后端名全等；
3. 候选数裁决：=1 resolved；=0 `REF_NOT_FOUND`；>1 `REF_AMBIGUOUS`（同名后端跨命名空间时停下问）；
4. 附健康报告字段（§4.1）——**只报告，不动作**。

## 5. 不健康报告与禁止自动换路（T3 预埋）

| 错误码 | 触发 | 行为 |
|---|---|---|
| `REF_SYNTAX_ERROR` | §3.2 禁止形态 | 报错回显位置 |
| `REF_NOT_FOUND` | 候选 0 | 报错 + 可用后端清单摘要 |
| `REF_AMBIGUOUS` | 候选 >1 | 停下问 |
| `UNHEALTHY_REPORTED` | health.status ∈ {degraded, down} | **不是错误**：解析成功，产物显形携带健康状态；下游指令若要执行，须人工在知晓状态下决策——解析层不拦不换 |
| `AUTO_SWITCH_ATTEMPT` | 解析器调用路径出现换路函数 | 静态断言目标（T3：解析器模块 AST 无 failover/executor 导入） |

- 词表封闭；解析返回 `Resolution | Refusal` 二型；
- R1 的代码级保证：解析器模块**不导入** failover/executor/hot_swap（T3 AST 级断言）——换路在物理上不可从解析发生。

## 6. 解析审计记录

- 每次解析追加：`{ts, raw, backend_id, health_status, status, error_code?}`；
- 健康状态是**查询时点快照**（记录 checked_at）——审计回放时能区分"当时不健康"与"现在不健康"；
- 只增不删；`ts` 注入。

## 7. 与学习网关的衔接（R2 展开）

- 本条与 gateway_recommend（229/T3）、learning_confirm（229/T4）的关系：@引用解析产物可以**作为推荐的证据输入**（"用户@了某后端"是意图信号），但推荐仍走既有生成→确认链；
- 解析器不写任何推荐状态、不触发确认板动作（模块边界：只读注册表与 availability）。

## 8. 与后续任务的衔接

| 任务 | 消费本设计的部分 |
|---|---|
| T2 解析实现 | §3 语法、§4 schema 与步骤、§6 审计 |
| T3 闸门 | §5 词表、AUTO_SWITCH_ATTEMPT AST 断言、R1 报告不换路 |
| T4 划界断言 | §2/§7 表格逐行落断言（257/280/recommend 链行为不变） |
| T5 验收试运行 | 样例回放 + 不健康报告演练 + 文档无密钥 |

## 9. 开放问题（留待评审/T2）

1. 健康状态读取的接口形态（availability 模块内存态直读 vs 注入查询函数——倾向注入，测试可造状态，待 T2 裁决）；
2. BYOS 自定义后端与内置后端同名时的命名空间规则（候选 >1 即停下问是否足够）。
