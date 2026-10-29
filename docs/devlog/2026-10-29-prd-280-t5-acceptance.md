# PRD-280/T5 验收试运行记录：路由配置快照全流程演练 + 自动回滚 + 红线断言 + 文档无密钥

- 日期：2026-10-30
- trend issue：[zhanglunet/trend#1405](https://github.com/zhanglunet/trend/issues/1405)
- 中央 PRD：`trend/adopted/PRD-280-ofr-route-config-snapshot.md`
- 全链：T1 #1401（设计 `docs/plans/2026-10-29-prd-280-route-config-snapshot.md`）→ T2 #1402（`config_snapshot.py` 快照/还原/审计）→ T3 #1403（`auto_rollback.py` 健康失败自动回滚+AST 红线）→ T4 #1404（只出推荐红线断言）→ 本任务 T5
- 验收测试：`tests/test_prd280_acceptance.py`（8 例）
- 状态：验收通过；实现类 PR 只开不合并，等用户 review

## 验收标准逐条核验（issue #1405）

### 1. 回滚全链通过 — 通过

虚构路由配置全流程演练：`take_snapshot`（单槽指纹）→ 改配置 → 健康检查失败 →
`maybe_auto_rollback`（客观信号触发 restore + switch_failed 事件）→ 回到原配置
（回滚后再快照指纹与原快照一致）。

### 2. 红线断言全过 — 通过

- 学习网关只出推荐、不自动执行：AST 断言 `maybe_auto_rollback` 决策输入仅
  snapshot/health_result（无 kwonly/vararg/kwarg），公开函数唯一入口，T3 红线复测
- 健康正常零事件零回滚（客观信号触发面唯一性）
- 零网络 import 断言
- 篡改检出（指纹不一致抛错）

### 3. 文档无密钥 — 通过

交付面（config_snapshot.py/auto_rollback.py/验收测试）扫描：密钥形态/内网 IPv4 零命中
（正则字面量与拼接构造豁免自指）。

## 运行记录

`.venv/bin/python -m pytest tests/test_prd280_acceptance.py -q` → 8 passed。

## 结论

PRD-280 全链（T1–T5）验收通过；只开不合并，等用户 review。
