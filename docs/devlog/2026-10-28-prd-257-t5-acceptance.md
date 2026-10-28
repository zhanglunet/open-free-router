# PRD-257/T5 验收试运行记录：双后端切换对比

- 日期：2026-10-28
- trend issue：[zhanglunet/trend#1289](https://github.com/zhanglunet/trend/issues/1289)
- 中央 PRD：`trend/adopted/PRD-257-ofr-harnessrouter-backend-routing.md`
- 仓库：open-free-router，分支 `trend/prd-257-t5-acceptance`（自 `trend/prd-257-t4-audit-export` 切出）
- 验收标准（issue 原文）：**对比记录齐全；文档无密钥**
- 本 PRD 红线：切换走配置，客户端不随后端重写；doctor 诚实标注纪律不减

## 1. 验收对象（T1–T4 全链）

| 环节 | trend issue | 交付 | 自测 |
|---|---|---|---|
| T1 设计 | #1285（issue #94 对应 PR） | `docs/plans/2026-10-28-prd-257-backend-routing.md` | — |
| T2 后端统一路由最小实现 | PR #96（trend #1286） | `src/open_free_router/harness_route.py`（协议对象/双适配器/切换审计/redaction） | 11 例 |
| T3 路由合同深化 | PR #98（trend #1287） | seq 单调/SESSION_EXPIRED/cancel ack/StructuredFailure 校验器 | 12 例 |
| T4 切换审计导出与对比记录 | PR #100（trend #1288） | `src/open_free_router/route_audit.py`（export_jsonl/export_compare） | 8 例 |

T5 为纯验收：只新增 `tests/test_prd257_acceptance.py`（8 例）与本记录，不改 T2–T4 行为。

## 2. 验收项 ↔ 测试映射（1:1）

| # | 验收项 | 测试 |
|---|---|---|
| 1 | 同一虚构请求双后端切换对比：status / content 前缀 / synthetic_stream / usage 逐字段对照；export_compare 对照行四字段齐全 | `test_1_dual_backend_field_by_field_compare` |
| 2 | 切换审计事件字段齐全（route_from/route_to/reason/actor）并进机读导出 | `test_2_switch_audit_event_fields` |
| 3 | JSONL 导出可解析：每行合法 JSON、字段 ⊆ 白名单、零对话正文、落临时档案后复读一致 | `test_3_jsonl_export_parsable_and_archivable` |
| 4 | 取消路径：双后端 cancel → cancelled chunk（payload.ack=true）+ status=cancelled + failure.class=cancelled，`cancel_ack` 校验器通过 | `test_4_cancel_path_on_both_backends` |
| 5 | 会话过期：SESSION_EXPIRED / protocol / retryable，send 与 stream 两面一致，失败类进审计摘要 | `test_5_session_expired_structured_failure` |
| 6 | 失败 redaction：自造失败后端把假密钥+假内网 IP 塞进 backend_detail，经路由边界 → Response → 审计导出全链路零泄露（占位符化） | `test_6_failure_redaction_no_secret_no_lan_ip` |
| 7 | 零网络断言：socket 全程封死（构造即抛）跑完双后端切换/流式/取消/过期/导出全场景零触发；交付模块无网络库导入 | `test_7_zero_network_by_construction` |
| 8 | 文档与交付面无密钥：PRD-257 全部交付面（源码 2 + 测试 4 + 设计稿 + 本记录）假密钥/内网 IP 样式扫描零命中 | `test_8_delivery_surface_no_secrets_no_lan_ip` |

## 3. 试运行结果（真实输出）

验收测试逐项（`python -m pytest tests/test_prd257_acceptance.py -v`，分支
`trend/prd-257-t5-acceptance`，工作区 commit `585812d` 之上）：

```
tests/test_prd257_acceptance.py::Test1DualBackendCompare::test_1_dual_backend_field_by_field_compare PASSED
tests/test_prd257_acceptance.py::Test2SwitchAuditEvent::test_2_switch_audit_event_fields PASSED
tests/test_prd257_acceptance.py::Test3JsonlExport::test_3_jsonl_export_parsable_and_archivable PASSED
tests/test_prd257_acceptance.py::Test4CancelPath::test_4_cancel_path_on_both_backends PASSED (2 subtests)
tests/test_prd257_acceptance.py::Test5SessionExpired::test_5_session_expired_structured_failure PASSED
tests/test_prd257_acceptance.py::Test6FailureRedaction::test_6_failure_redaction_no_secret_no_lan_ip PASSED
tests/test_prd257_acceptance.py::Test7ZeroNetwork::test_7_zero_network_by_construction PASSED
tests/test_prd257_acceptance.py::Test8DocsNoSecrets::test_8_delivery_surface_no_secrets_no_lan_ip PASSED
=============== 8 passed, 2 subtests passed ===============
```

试运行过程记录（如实）：首轮 #8 报红两次，均为扫描面发现链内既有测试自带的
redaction 样本 fixture（T2/T3 脱敏测试的合成样本，其断言恰好证明样本经
`redact()` 零存活）。处置：在验收测试中以 `DECLARED_SYNTHETIC_SAMPLES` 申报
豁免且仅限测试面——docs/ 与 src/ 仍严格零命中（连申报样本也不允许）；另一次
报红为本记录文件尚未落盘（扫描面含本记录）。申报后复跑 8/8 全绿。

## 4. 全量回归（含 T1–T4 零回归）

```
.venv/bin/python -m pytest tests/ -q
...
11 failed, 751 passed, 2 subtests passed in 160.35s (0:02:40)
```

11 个失败与基线逐条相同，全部为 sandbox 环境性既有失败（`test_sandbox_acceptance.py`
1 例 + `test_sandbox_hardening.py` 2 例 + `test_sandbox_runner.py` 8 例，均为
`subprocess.SubprocessError: Exception occurred in preexec_fn`），与本 PRD 交付面
无关。passed 由基线 743 → 751（+8 = 本验收测试例数），**T1–T4 全链零回归**。

基线（切分支前的 `trend/prd-257-t4-audit-export`，2026-10-28 实测）：
**11 failed, 743 passed**，失败清单与本节完全一致。

## 5. 红线自查

- **切换走配置，客户端不随后端重写**：验收 #1 用同一 `HarnessRouter.send`
  调用只换 `route_id`，双后端结果形态逐字段一致（差异仅在内容与前缀，不在结构）。
- **doctor 诚实标注纪律不减**：`synthetic_stream` 在 Response、审计摘要、
  JSONL 导出三面如实标注（验收 #1/#3：local-echo 合成流三面均为 true）。
- **零对话正文**：导出白名单出料，正文标记零外泄（验收 #3）。
- **无密钥/占位符纪律**：源码与文档零真实凭据、零内网主机名；redaction 样本
  全部为运行时拼接的自造值（验收 #6/#8）。

## 6. 结论

验收标准「对比记录齐全；文档无密钥」达成：8/8 验收测试通过；对比记录
（switch 事件 + 每请求摘要 + export_compare 对照行）逐字段齐全可机读；
交付面扫描零密钥零内网 IP。全量回归 751 passed / 11 个 sandbox 环境性既有失败
（与基线逐条相同），T1–T4 零回归。PR 只开不合并，等用户 review。
