# PRD-258/T5 验收试运行记录：HIGH 样例不接流量

- 日期：2026-10-28
- trend issue：[zhanglunet/trend#1294](https://github.com/zhanglunet/trend/issues/1294)
- 中央 PRD：`trend/adopted/PRD-258-ofr-skillspector-plugin-gate.md`
- 仓库：open-free-router，分支 `trend/prd-258-t5-acceptance`（自 `trend/prd-258-t4-whitelist` 切出）
- 验收标准（issue 原文）：**报告存档；无密钥**
- 本 PRD 红线：HIGH 及以上默认不接流量；安检在接流量前，不替代执行时隔离

## 1. 验收对象（T1–T4 全链）

| 环节 | trend issue | 交付 | 自测 |
|---|---|---|---|
| T1 设计 | #1290（issue #95 对应 PR） | `docs/plans/2026-10-28-prd-258-plugin-gate.md` | — |
| T2 安检适配最小实现 | PR #97（trend #1291） | `src/open_free_router/plugin_gate.py`（B 四维/plugin-gate-report-v1/redaction） | 10 例 |
| T3 接流量闸门 | PR #99（trend #1292） | `src/open_free_router/plugin_gate_gate.py`（五道前置校验/隔离清单/doctor 视图） | 8 例 |
| T4 误报白名单与复核 | PR #101（trend #1293） | `src/open_free_router/plugin_whitelist.py`（reason/reviewer 必填/90 天保质期/audit_log） | 8 例 |

T5 为纯验收：只新增 `tests/test_prd258_acceptance.py`（10 例）与本记录，不改 T2–T4 行为。

## 2. 验收项 ↔ 测试映射（1:1）

| # | 验收项 | 测试 |
|---|---|---|
| 1 | N-1 工具投毒 blocked：安检 decision=blocked → 闸门拒注册（结构性拦截）+ 隔离清单/doctor 视图留痕 | `test_1_n1_poisoned_blocked_at_gate` |
| 2 | N-2 近名伪装 blocked：同上全链 | `test_2_n2_near_name_blocked_at_gate` |
| 3 | N-3 明文密钥 blocked：B-2-01 floor HIGH + redaction（假密钥零出现在报告/决策面，占位符在位）+ 闸门拒注册 | `test_3_n3_plaintext_secret_redacted_blocked` |
| 4 | N-4 越权域 blocked：B-3 HIGH → 闸门拒注册 | `test_4_n4_out_of_domain_blocked` |
| 5 | P-1/P-2 正常样例 admitted：可接流量（对照面，门槛不误伤） | `test_5_clean_samples_admitted` |
| 6 | 白名单误报放行留痕：reason/reviewer 必填、whitelist_applied 留痕、audit_log 机读可审计；未豁免的剩余 HIGH 维持拦截（白名单不绕过安检） | `test_6_whitelist_release_with_audit_trace` |
| 7 | 报告存档机读：6 份样例报告（含 content_hash 章）落档案目录（每插件 JSON + index.jsonl），复读逐份可解析、字段齐全、decision 与闸门判定一致、存档字节零密钥 | `test_7_reports_archived_machine_readable` |
| 8 | 哈希改包重扫：内容变更 → hash_mismatch 旧报告作废；重扫新报告 → 恢复注册；scan_version 不匹配同样作废 | `test_8_hash_tamper_invalidates_rescan` |
| 9 | 零网络断言：socket 封死跑完扫描/闸门/白名单/doctor 全流程零触发；交付模块无网络库导入 | `test_9_zero_network` |
| 10 | 交付面无密钥：PRD-258 全部交付面（源码 3 + 测试 4 + 设计稿 + 本记录）密钥/内网 IP 样式扫描零命中 | `test_10_delivery_surface_no_secrets` |

## 3. 试运行结果（真实输出）

验收测试逐项（`python -m pytest tests/test_prd258_acceptance.py -v`，分支
`trend/prd-258-t5-acceptance`，工作区 commit `f218d30` 之上）：

```
tests/test_prd258_acceptance.py::TestHighSamplesNeverAdmitted::test_1_n1_poisoned_blocked_at_gate PASSED
tests/test_prd258_acceptance.py::TestHighSamplesNeverAdmitted::test_2_n2_near_name_blocked_at_gate PASSED
tests/test_prd258_acceptance.py::TestHighSamplesNeverAdmitted::test_3_n3_plaintext_secret_redacted_blocked PASSED
tests/test_prd258_acceptance.py::TestHighSamplesNeverAdmitted::test_4_n4_out_of_domain_blocked PASSED
tests/test_prd258_acceptance.py::TestCleanAndWhitelist::test_5_clean_samples_admitted PASSED (2 subtests)
tests/test_prd258_acceptance.py::TestCleanAndWhitelist::test_6_whitelist_release_with_audit_trace PASSED
tests/test_prd258_acceptance.py::TestArchiveAndRescan::test_7_reports_archived_machine_readable PASSED
tests/test_prd258_acceptance.py::TestArchiveAndRescan::test_8_hash_tamper_invalidates_rescan PASSED
tests/test_prd258_acceptance.py::TestZeroNetworkAndSecrets::test_9_zero_network PASSED
tests/test_prd258_acceptance.py::TestZeroNetworkAndSecrets::test_10_delivery_surface_no_secrets PASSED
```

试运行过程记录（如实）：首轮 #10 报红一次——本记录文件尚未落盘（扫描面含
本记录，先有鸡先有蛋）；本记录落盘后复跑 10/10 全绿。链内 T2–T4 测试自带的
占位密钥样本（`sk-placeholder…` 形态，其断言恰好证明样本经 redact 零存活）在
测试面申报豁免；docs/ 与 src/ 严格零命中。验收用假密钥为本文件运行时拼造，
本记录与全部源文件零密钥字面量。

## 4. 全量回归（含 T1–T4 零回归）

```
.venv/bin/python -m pytest tests/ -q
11 failed, 748 passed, 2 subtests passed in 220.23s (0:03:40)
```

基线（同一分支、剔除本验收文件 `--ignore=tests/test_prd258_acceptance.py`）实测：
**11 failed, 738 passed**——11 个失败全部为 sandbox 环境性既有失败
（`test_sandbox_acceptance.py` 1 例 + `test_sandbox_hardening.py` 2 例 +
`test_sandbox_runner.py` 8 例，均为 `subprocess.SubprocessError: Exception
occurred in preexec_fn`），与本 PRD 交付面无关。738 → 748（+10 = 本验收例数），
失败清单逐条相同，**T1–T4 全链零回归**（T2 10 例 + T3 8 例 + T4 8 例全部通过，
含在 748 内）。

## 5. 红线自查

- **HIGH 及以上默认不接流量**：N-1…N-4 四类 HIGH 样例全部在闸门被结构性拒绝
  （register=False / gate=blocked / 隔离清单留痕，验收 #1–#4）；白名单只在
  blocked 判定上做已复核的显式例外且留痕，剩余 HIGH 维持拦截（验收 #6 场景 B）。
- **安检在接流量前**：注册前五道前置校验（报告/schema/scan_version/哈希/decision）
  原样生效；改包自动作废旧报告、重扫后恢复（验收 #8）。
- **报告存档**：每插件 JSON 报告 + index.jsonl 索引，复读逐份可解析、字段齐全、
  与闸门判定一致（验收 #7）。
- **无密钥**：报告/决策面/存档字节零密钥（占位符化，验收 #3/#7）；交付面扫描
  零命中（验收 #10）。
- **不替代执行时隔离**：本层只做准入决策（闸门 reason/message 原文如此），
  运行时隔离归 PRD-181，验收未触碰。

## 6. 结论

验收标准「报告存档；无密钥」达成：10/10 验收测试通过；N-1…N-4 全部 blocked 且
闸门拒注册，P-1/P-2 放行；报告存档机读可复读；交付面零密钥零内网 IP。全量回归
748 passed / 11 个 sandbox 环境性既有失败（与基线逐条相同），T1–T4 零回归。
PR 只开不合并，等用户 review。
