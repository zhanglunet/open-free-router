# PRD-350/T1：路由决策重放包设计（当时为什么走这条后端，可复现）

> trend issue #1629。对应 PRD-350（2026-10-30 日报遗留第 6 条，用户「继续」按既定流水线立项）。
> 设计日：2026-11-11 · 目标仓库：zhanglunet/open-free-router
> 状态：T1 设计草案，待评审。仅设计文档（纯加法 .md）；重放账本内核（T2）、重放闸门（T3）、划界断言（T4）、全链验收（T5）按本设计后续落地。
> 示例全部虚构占位（`ds-001` 风格），不出现真实主机名、IP、账号、密钥。

## 0. 红线

| # | 红线 | 落地方式 |
|---|---|---|
| R1 | **重放包三件套齐备** | `{input_fingerprint（请求特征+当时健康/配额快照 digest）, decisions（选的后端+理由+规则版本）, output（延迟与成本记录）}` 缺一拒绝入账（T3） |
| R2 | **学习网关只出推荐不自动执行** | 重放是**判定层复算**——零真实路由切换、零待生效配置写（T4 AST：零配置写导入）；重放结果不改变任何当前路由 |
| R3 | **对不上即报账本失败** | 重放选路与包内不一致 → REPLAY_MISMATCH 显形落失败证据，零静默换新路由 |
| R4 | **账本 append-only** | ReplayLedger 只增不删 |
| R5 | 占位符纪律 | T5 检查覆盖本文件 |
| R6 | 实现类 PR 只开不合并 | 等用户 review |

## 1. 借鉴对象、许可证与不复用红线

beenuar/AiSOC（MIT，2026-11-11 实测）+ CopilotKit/OpenBot（MIT，实测）——只借鉴「可重放决策账本」形态；包 schema 按本仓路由域自研，零代码复用，不引入原仓依赖。

## 2. 与既有机制划界

| 维度 | PRD-350（本条） | PRD-280 配置快照 | PRD-201 可观测看板 | PRD-257 统一路由 |
|---|---|---|---|---|
| 管什么 | 路由决策**可重算** | 配置可逆 | 看（非批） | 协议层 |
| 重放 | 判定层复算 | 版本回滚 | 无 | 无 |
| 执行语义 | 零路由切换（R2） | 回滚写 | 只读 | 转发 |

## 3. 重放包设计

### 3.1 schema（T2 预埋）

```json
{"schema": "routing-replay-pack/v1",
 "input_fingerprint": {"request_traits": "…（占位）", "traits_digest": "sha16:…",
   "health_snapshot_digest": "sha16:…", "quota_snapshot_digest": "sha16:…",
   "rules_version": "vN"},
 "decisions": [{"backend": "backend-a（占位）", "why": "健康+配额最优且命中规则（占位）"}],
 "output": {"latency_ms_expected": 0, "cost_expected": 0}}
```

### 3.2 流程（确定性）

指纹复核（健康/配额快照 digest 不符 → REPLAY_INPUT_DRIFT 拒绝——防拿当前状态冒充当时）→ 注入快照重放选路判定（纯函数）→ 选路与预期值逐字节比对 → REPLAY_OK / REPLAY_MISMATCH。

## 4. 失败闸门（T3 落地）

| 错误码 | 触发 | 行为 |
|---|---|---|
| REPLAY_INPUT_DRIFT | 快照指纹不符 | 拒绝重放 |
| REPLAY_MISMATCH | 选路或预期值不一致 | 显形+失败证据入账 |
| PACK_INCOMPLETE | 三件套缺一 | 拒绝入账 |

## 5. 衔接

T2 内核；T3 闸门（三码+R2 零配置写 AST）；T4 划界（与 280/201/257 划界钉值）；T5 样例回放+MISMATCH 演练+文档无密钥。

## 6. 开放问题

1. 健康/配额快照的采集口径（重放需要当时快照——倾向决策时点即固化进包，重放注入同快照，待 T2）；
2. 并发路由的快照一致性（同一 tick 多请求共享快照 digest，待 T2）。
