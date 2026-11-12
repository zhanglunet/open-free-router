# PRD-378/T1：后端异常验证台设计（告警拨测复现过才进换路批准）

> trend issue #1882。来源：2026-11-12 GitHub 趋势日报（digests/2026-11-12.md）第 6 条，用户 2026-11-13 指令「拍板」采纳。
> 设计日：2026-11-12 · 目标仓库：zhanglunet/open-free-router
> 状态：T1 设计草案，待评审。仅设计文档（纯加法 .md）；验证台内核（T2）、闸门（T3）、划界断言（T4）、验收试运行（T5）按本设计后续落地。
> 示例全部虚构占位，不出现真实内部主机名、IP、账号或密钥。

## 0. PR 门槛自查（repos.yaml default_pr_gate）

| 门槛 | 落实 |
|---|---|
| require_integration_point | **接入点：`src/open_free_router/gateway_recommend.py::recommend` 产出换路推荐（含当时健康/配额快照）之后、`src/open_free_router/reroute_inbox.py::collect(recommendations=…)`（PRD-364 收件箱）之前**——T2 验证台以注入式函数 `verify_alert(alert, probe_data, recheck_fn)` 供换路流程在 collect 前调用；只有 VERIFIED 的异常告警进换路推荐来源 |
| no_missing_channel | 只消费 recommend 既有产物与 availability 既有探针形态（probe_data 注入）；复算=对同一探针快照重跑同一拨测判定（recheck_fn 注入，复用 availability.probe_batch_time 判定形态）；验证结论写本地 JSONL 账本（文件落库），零新外部渠道 |
| no_duplicate_module | main 上同类模块盘点：gateway_recommend.py（推荐产出）/ gateway_aggregate.py（聚合）/ availability.py（探针）/ reroute_inbox.py（364 收件箱）/ routing_replay.py（350 重放——管"决策可重算"，本条管"告警可复现"，粒度不同）。**本条不另起探针**：复算=重跑注入的同一拨测判定，比对告警的后端与异常类型一致才 VERIFIED |
| impl_requires_devlog | T2~T5 实现 PR 各附 docs/devlog/2026-11-13-prd-378-*.md 开发记录+合规核对；账本落 JSONL 文件（路径注入），不只存内存 |

## 1. 红线

| # | 红线 | 落地方式 |
|---|---|---|
| R1 | **告警拨测复现过才进换路批准** | 每条健康/配额异常告警先在探针快照上重跑同一拨测判定复现；复现结果包含该异常 → VERIFIED；未验证告警不进换路推荐来源（不进 364 收件箱） |
| R2 | **验证不产生新告警** | 复算=重跑注入的同一拨测判定（探针快照只读深拷贝），零新增检测逻辑、零真实拨测外呼（拨测=对既有快照复算）；复现不出=UNVERIFIED 不猜不补 |
| R3 | **机器可读落账** | 每次验证 `{alert_id, backend, kind, source_ref, probe_fingerprint, alert, reproduce, verdict}` 八件套落 JSONL 账本；缺任一 BENCH_INCOMPLETE 拒绝入账 |
| R4 | **账本 append-only + 修复二次验证才关闭** | 验证账本只增不删；探针快照变更后二次验证落新记录，关闭以最新 VERIFIED 为准 |
| R5 | 占位符纪律 | T5 检查覆盖本文件 |
| R6 | 实现 PR 只开不合并 | 等用户 review |

## 2. 借鉴对象、许可证与不复用红线

openai/codex-security（Apache-2.0，实测）+ cloudflare/security-audit-skill（MIT，实测）——只借鉴「发现→验证→修复→再验证」控制面形态；条目 schema 按本仓路由域自研，零代码复用，不引入原仓依赖。

## 3. 与既有机制划界

| 维度 | PRD-378（本条） | PRD-149 failover 账本 | PRD-258 插件安检 | PRD-336 fallback_gate | PRD-350 重放包 | PRD-364 收件箱 |
|---|---|---|---|---|---|---|
| 管什么 | 逐条 **告警可复现** | 切换记录 | 进门扫描 | 结果回填 | 决策可重算 | 等人批准 |
| 比对 | 告警 vs 重跑拨测判定 | 无 | 无 | 无 | 包内输出 vs 重演 | 无 |
| 关系 | recommend 之前、collect 之前 | 被区分 | 被区分 | 被区分 | 被区分 | 下游：验证过才可进 |

- 学习网关只出推荐不自动执行红线不变（验证台零路由切换零配置写）。

## 4. 验证台设计

### 4.1 条目 schema（T2 预埋）

```json
{"schema": "backend-verify-bench/v1",
 "items": [{"alert_id": "a-001",
   "backend": "backend-a（占位）",
   "kind": "health_degraded|quota_low",
   "source_ref": "availability/v1#probe[0]（占位）",
   "probe_fingerprint": "sha16:…（探针快照）",
   "alert": {"backend": "backend-a（占位）", "status": "degraded", "detail": "…（占位）"},
   "reproduce": {"match": true, "detail": "…（占位）"},
   "verdict": "VERIFIED|UNVERIFIED"}]}
```

### 4.2 流程（确定性）

异常告警 → 逐条重跑同一拨测判定（注入 recheck_fn，探针快照深拷贝）→ 复现包含同 backend+异常类型 → VERIFIED / 否则 UNVERIFIED → 落账 → 换路推荐来源只收 VERIFIED。

## 5. 失败闸门（T3 落地）

| 错误码 | 触发 | 行为 |
|---|---|---|
| BENCH_INCOMPLETE | 八件套缺一/词表外 | 拒绝入账 |
| BENCH_REPRO_MISMATCH | 声称 VERIFIED 但复现不含同 backend+异常类型（或反之） | 显形拒绝 |
| BENCH_STALE | 探针快照指纹与当前不符 | 拒绝复用旧验证 |

## 6. 衔接

T2 内核（verify_alert + JSONL 账本）；T3 闸门（三码+零配置写零路由切换 AST）；T4 划界（与 350 重放/364 收件箱/fallback_gate 划界钉值+拨测判定零 import 注入重跑）；T5 样例全链回放+UNVERIFIED 拦截演练+文档无密钥。

## 7. 开放问题（留待评审/T2）

1. quota_low 类复现口径（倾向重跑配额阈值判定比对——待 T2）；
2. probe_fingerprint 口径（倾向探针快照 sha16 摘要，与 350 snapshots_digest 同族——待 T2）。
