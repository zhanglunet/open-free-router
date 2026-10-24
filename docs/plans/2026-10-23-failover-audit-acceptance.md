# PRD-149/T5 验收报告：failover 审计账本验收

- 日期：2026-10-23
- 范围：trend #749；验收对象为 T2（#746→PR #72）/T3（#747→PR #73）/T4（#748→PR #75）已合并进 main 的 failover 审计账本（`failover_audit.py` / `analytics.py` 扩展 / `failover_view.py` / `executor.py` 钩子接线）
- 验收方式：`tests/test_failover_acceptance.py`（5 个编号测试，逐项映射验收标准）+ T2-T4 回归套件 + 反向验证（破坏性反例）+ 本报告；T5 纪律：只动测试与文档，不改任何业务代码
- 结论：**验收标准「模拟 3 次换路账本完整；无密钥入账」全部通过**，无阻塞项；已知局限见末节

## 验收环境与口径

- 全部模拟走**真实生产接线**：`UpstreamExecutor` 内部经 `make_audit_hook(RouteDecisionStore(analytics=store))` 构建钩子，落账到真实 `usage.db`（0600/WAL/30 天保留）——不绕过任何已合并代码。
- 假上游为一次性本地回环 HTTP 服务（临时端口、占位凭据 `placeholder-*`）；文档与测试不含任何真实主机名、IP、账号或密钥。
- 三次换路形态（占位候选 id）：

| # | 形态 | 触发 | 账本结果 |
|---|---|---|---|
| 1 | `rate_limited` 切换成功 | 429（带 `X-RateLimit-Remaining: 0`，无 Retry-After、无配额字样）→ 第二候选 200 | 决策行 `rl1/model → rl2/model`，reason=`rate_limited`，quota=exhausted/upstream_headers/rate_limited；结果行 `success`（status 200，attempt 2，完成时回填 total_latency_ms） |
| 2 | `provider_unavailable` 链式失败 | 503 → 503 → 第三候选 200 | 每次转移一对：`pu1→pu2`（`failed`/503/provider_unavailable）+ `pu2→pu3`（`success`/200/attempt 3） |
| 3 | `quota_exhausted` 无候选 | 单候选 429（正文与响应头均为配额耗尽证据） | 决策行 `qe1/model → NULL`，reason=`quota_exhausted`，quota=exhausted/upstream_headers/quota_exhausted；结果行 `no_candidate`（status 0，attempt NULL） |

三次请求（三个 request_id）共 4 次转移、4 对记录，全部成对。

## 逐项对照

| # | 验收项 | 结果 | 测试与断言要点 |
|---|---|---|---|
| 1 | 模拟 3 次不同形态换路经 T2-T4 钩子完整落账，逐次断言成对完整（decision+outcome） | ✅ 通过 | `test_1_three_failover_shapes_land_a_complete_ledger`：三个形态逐一断言 from/to/reason/attempt、配额三列、结果行 outcome/status_code/error_kind/attempt、`no_candidate` 行 status 0 + attempt NULL；4 个 outcome 与 4 个 decision 按 `decision_id` 一一配对且顺序一致；`success` 行完成回填 `total_latency_ms` |
| 2 | doctor 结论入账 | ✅ 通过 | `test_2_doctor_conclusion_lands_on_every_decision`：先经 T3 对齐路径（`doctor_summary_from_report` 聚合 → `record_doctor_run`）落一次 fail/1 结论；三形态全部 4 条决策行的 `doctor_verdict=fail`、`doctor_failed_checks=1`、`doctor_at` 与落账时间一致 |
| 3 | T4 查询视图查得 3 次换路且过滤正确 | ✅ 通过 | `test_3_views_find_the_three_failovers_and_filter`：`collect_pairs` 4 对全 `paired`（按时间倒序）；按原因过滤逐一命中三形态（rate_limited 1 / provider_unavailable 2 / quota_exhausted 1 / 无关原因 0）；后端过滤两侧任一命中（`pu2` 两对、`qe1` 一对、未知后端 0）；`summarize` 的 by_reason/by_outcome/by_doctor_verdict/by_quota_band 全对；真实 CLI `cmd_failover_log`/`cmd_failover_summary`（--json 与人类可读模式）结果一致 |
| 4 | incomplete 口径正确 | ✅ 通过 | `test_4_incomplete_pair_is_judged_not_fabricated`：被门禁跳过的目的候选（冷却中，从未尝试）→ 写入侧**零**结果行（不回填、不编造），视图判 `pair_status=incomplete`、`incomplete_pairs=1`、`by_outcome` 为空 |
| 5 | 无密钥入账（写入侧拒收 + 文件扫描零命中） | ✅ 通过 | `test_5_no_secrets_reach_the_ledger`：(a) 模拟请求确实携带了凭据（每个假上游都收到非空 Authorization 值，证明写入路径手里有密钥）；(b) 写入侧拒收——`sk-` 形态候选与 `api_key=` 形态 decision_id 经生产钩子返回 None（拒收不掩码），带 `Bearer` 令牌的 doctor 结论抛 `ExportSecurityError`；(c) allowlist 层——`prompt`/`response_body`/`api_key` 等键外字段被丢弃、表结构不变；(d) 账本文件原始字节扫描（db+wal+shm）：`SENSITIVE_PATTERN` 零命中、全部占位凭据与令牌零命中、`Authorization`/`Bearer`/doctor 报告 label 与 detail 明文零命中 |

## 测试数据（原样记录）

- 验收测试：`tests/test_failover_acceptance.py` **5/5 通过**（`.venv/bin/python -m pytest tests/test_failover_acceptance.py`，5.83s）
- T2-T4 回归（直接相关套件）：`test_failover_audit.py` + `test_failover_view.py` + `test_doctor_audit.py` + `test_analytics.py` **44/44 通过**
- 全量套件：`pytest tests/` → **637 通过 / 11 失败**；11 个失败全部为 PRD-079 沙盒子系统（`test_sandbox_runner` 8 + `test_sandbox_hardening` 2 + `test_sandbox_acceptance::test_10_6b` 1，均为本机 subprocess `preexec_fn` 环境问题）。**该 11 个失败在未含本 T5 改动的干净 main 上原样复现（11 failed, 66 passed 同一批用例），与 PRD-149 无关，属既有环境问题，T5 不修（纪律：不顺手修）。**
- 反向验证（CLAUDE.md「测试必须被证明会在无修复时失败」纪律）：**2/2 通过**
  - 破坏 1：移除 `record_failover_decision` 的 `SENSITIVE_PATTERN` 拒收门 → `test_5` 变红；
  - 破坏 2：移除 executor 成功路径的 `audit.settle(outcome="success")` → `test_1` 变红。
  - 两例验证后业务代码 `git checkout --` 逐字节还原，工作区仅剩新增测试文件。

## 已知局限（非阻塞，诚实记录）

1. **上游为回环 mock，未对真实上游实测**：429/503/配额头的形态由本地一次性 HTTP 服务模拟；真实上游的错误文案差异已由分类器既有测试覆盖，账本只存枚举不受影响。
2. **doctor 结论走 T3 聚合路径而非完整 `cmd_doctor` 进程**：`cmd_doctor` → `record_doctor_run` 的接线由 T3 的 `test_doctor_audit.py`（含 human/json 双模式、store 故障不中断）覆盖；本验收组合 `doctor_summary_from_report` + `record_doctor_run` + 钩子盖戳三段做端到端断言。
3. **文件扫描是纵深防御不是证明**：`SENSITIVE_PATTERN` 为模式匹配，新型密钥格式理论上可能漏网；账本 schema 本身（allowlist + 枚举折叠 + 定长字符串）决定了自由文本进不来，扫描是对此的二重确认。
4. **成对完整性的时序口径**：`incomplete` 判定按「无结果行即 incomplete」（T1 §2.4 不回填、不编造），未实现「预算 300s + 60s 宽限」的时间维度区分——当前无需求场景，视图口径与 T4 实现一致。
5. **人类可读表格列宽**：`failover-log` 非 JSON 模式 outcome 列截断到 10 字符（`no_candidate` 显示为 `no_candida`），JSON 模式完整；测试按列宽断言。

## 已知问题

**无**。验收过程未发现需要修的业务代码 bug；唯二测试调整均为验收测试自身的断言口径（视图按时间倒序返回、表格列宽截断），与业务代码无关。

## 交付物

- `tests/test_failover_acceptance.py`（5 个验收测试）
- 本报告 `docs/plans/2026-10-23-failover-audit-acceptance.md`

PRD-149 至此 T1–T5 全部完成：schema（#745）→ 写入钩子（#746）→ doctor 对齐（#747）→ 查询汇总（#748）→ 验收（本 PR，#749）。
