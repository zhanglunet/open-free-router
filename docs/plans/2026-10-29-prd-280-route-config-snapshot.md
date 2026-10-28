# PRD-280/T1：路由配置变更快照设计（route-config-snapshot-v1 + restore + 健康检查失败自动回滚）

- 日期：2026-10-29
- 来源：2026-10-28 趋势日报 open-free-router 第 1 条（⭐），用户当日拍板采纳
- trend issue：[zhanglunet/trend#1401](https://github.com/zhanglunet/trend/issues/1401)（中央 PRD 任务表所写 #1405 为旧编号，以本 issue 实测为准）
- 中央 PRD：`trend/adopted/PRD-280-ofr-route-config-snapshot.md`
- 状态：T1 设计稿，待评审；实现类任务（T2–T4）只开 PR 不合并
- 本批主题：可逆改动包（14 仓同构：先快照、再改动、一键回上一版）

> ⚠️ 借鉴红线：[Gathin23/cleanyourtv](https://github.com/Gathin23/cleanyourtv) 经 GitHub License API 实测为 **NOASSERTION（无协议）**（中央 PRD 记录 2026-10-28 实测，本设计 2026-10-29 复核一致）。本设计**仅借鉴"先快照再改动、一键还原"的形态思路**，零代码、零 schema 定义、零文案复用；本仓快照对象、还原语义、审计事件全部原创设计。无协议仓库的任何内容引入即有法律风险，明确禁止（详见 §7）。

## 1. 目标与非目标

**目标**：路由配置（今天的 `registry.yaml` 全量 + `config.yaml` 路由段；PRD-257 落地后含 `routes.json`）在每次**配置生效动作之前**强制留一份快照（routes 全文 + 逐文件指纹 + taken_at）；提供 restore 一键回上一版；变更生效后的**健康检查失败**这一客观信号自动触发 restore，并写一条 `switch_failed` 审计事件，与既有 failover 审计账本衔接。全程留痕、回滚本身可再回滚。

**非目标**：
- 自动回滚只由健康检查失败这一客观信号触发，不引入任何其他自动决策——学习/推荐面**只出推荐、永不自动改配置**，红线不变（T4 结构断言）；
- 不做多版本历史——单槽（上一版），覆盖式；
- 不碰运行时 failover——executor 的按请求换路不写配置文件，已由 PRD-149 failover 账本覆盖，280 不管；
- 不新增凭据面与网络面——健康探测复用既有 probe，还原零网络。

## 2. 与既有能力的划界

| 既有能力 | 关系 |
|---|---|
| PRD-257 统一路由（协议层） | **257 管协议层**：会话/流式/取消/结构化失败合同 + `routes.json` 路由表 + 切换对比记录（T2–T5 PR 在途，main 尚无）；**280 管配置变更可逆**：变更前快照 / restore / 回滚审计。280 的快照面把 `routes.json` 预留为 `role:"route_table"` 覆盖对象（257 合入即自动纳入快照范围）；257 的切换审计与 280 的 `switch_failed` 同账本不同事件，互不替代——257 管"怎么切得规范"，280 管"切坏了怎么全身而退" |
| `save_registry` 的 `.bak-*` 备份（`config.py:165`，轮转保留 10 份，`registry.py` `BACKUP_RETENTION`） | 它是**保存副产物**：每次保存都产生（含 refresh 周期）、无指纹、无 restore 入口、无审计、会被轮转删除、不 fail-closed。280 的快照是**变更动作锚定的还原凭据**：只在配置生效动作前强制拍、带全文+指纹+`taken_at`、可 restore、留审计。T2 可把紧邻的 `.bak-<ts>` 作为 restore 数据源之一（ref 复用不重复占盘），但快照生效不依赖 `.bak` 幸存（inline 为默认） |
| `failover_audit.py`（PRD-149 换路账本，`usage.db` 成对 decision/outcome 行） | 280 的配置级事件**同账本落点、同一 allowlist+脱敏+`user_version` 增量迁移纪律**，新增事件种类而非新账本；`classify_failure`（`resilience.py:47`）"不留原始错误正文"的纪律沿用 |
| `probe.py`（`probe_model`，`probe.py:48`） | 健康检查信号的**唯一来源**：变更生效后对受影响 route 复用既有探测器，零新增网络面；探测结论过 `classify_failure` 分类，不自造判定 |
| 学习/推荐面（`route_query.py`、`route_explain`、`public_catalog` 等只读推荐） | 只读推荐、零配置写路径；280 不改变这一点，T4 结构断言：推荐/学习面 import 图无 routesnap 依赖 |

## 3. 方案设计

### 3.1 快照对象 `route-config-snapshot-v1`（schema）

```json
{
  "schema": "route-config-snapshot-v1",
  "snapshot_id": "rcs_<uuid>",
  "taken_at": "<ISO8601>",
  "trigger": "pre_apply",
  "operator": "setup | cli | mcp | api",
  "reason": "<变更摘要，占位符>",
  "files": [
    {
      "path": "<占位：配置目录>/registry.yaml",
      "role": "registry | routing_policy | route_table",
      "before": { "state": "present | absent", "sha256": "<改前指纹；absent 时 null>", "size": 4096 },
      "restore": { "kind": "inline | ref", "blob_ref": "blobs/<sha256> | bak:<原名>.bak-<ts> | null" }
    }
  ],
  "file_count": 2,
  "config_fingerprint": "<各文件 sha256 排序拼接后再哈希的整组指纹>"
}
```

要点：

1. **routes 全文 + 指纹 + taken_at**：`files[]` 覆盖本次变更写面的每个配置文件（默认面 = `registry.yaml` + `config.yaml` 路由段；257 落地后 `routes.json` 以 `role:"route_table"` 纳入）。`registry.yaml` 实测含凭据字节（`registry.py:148` `ProviderConfig.api_key/api_keys`）——全文快照即含敏感内容，快照文件永不入仓库、永不进日志与审计（红线 §4）；
2. **inline 默认、ref 降级**：配置文件 KB 级，改前字节在 apply 动作时已在内存/盘上，inline 写入快照库 blobs（零额外读盘）；超阈值（占位 1 MiB）文件降级 `ref`，ref 源 = `save_registry` 刚生成的 `.bak-<ts>`；restore 预检核 ref 指纹，ref 源已被轮转删除 → 预检失败拒绝执行，不静默降级；
3. **捕获时机 = 配置生效动作前强制触发（fail-closed）**：锚点 = 配置写入面的变更类调用（`save_registry` 变更类路径 + `config.yaml` 路由段变更 + 未来 257 的路由切换确认）。快照动作失败 → **本次变更拒绝生效**（宁可这单失败，不可留一个不可回滚的配置）。运行时 failover（executor 按请求换路）不是配置变更，不触发快照；
4. **单槽 + 逆快照**：每配置面一个活动快照（新变更覆盖旧快照）。restore 前先对当前状态拍逆快照，逆快照成为新的活动快照——回滚可再回滚，任意时刻只有一份（满足"不做多版本历史"）；
5. `taken_at` 与 `config_fingerprint` 是审计与预检的主键凭据：预检时"当前应等于快照记录的变更后状态"，逐文件指纹核对。

### 3.2 存储落点（T2）

```
src/open_free_router/routesnap/
  snapshot.py    # 捕获：文件清单 + 指纹 + blob 落盘（含凭据字节处理纪律）
  restore.py     # 还原：两阶段预检 + 执行（§3.4）
  probe_gate.py  # 变更后健康探测信号判定（§3.3，复用 probe.py + classify_failure）
  events.py      # 审计事件 schema 与写入（§3.5）
tests/test_routesnap_*.py   # 每模块一个测试文件（纯函数、零真实网络）
```

快照库**不进仓库**：`data_dir`（`config.py:145` 实测默认 `~/.local/share/open-free-router`，本文一律以 `<数据目录>` 占位）下 `routesnap/<snapshot_id>.json` 与 `blobs/<sha256>`，目录 0700、文件 0600（对齐 `save_registry` chmod 0600 纪律，`config.py:189`）；`.gitignore` 兜底。

### 3.3 restore 接口与触发器

```
restore(snapshot_id, { trigger, operator, at }) -> restore_report
  前置 1：该配置面存在活动快照（无快照 → route_restore_refused，如实记录）
  前置 2：逆快照先行（§3.1 第 4 点）
  预检 3：逐文件核对当前指纹与快照预期一致（§3.4）
  执行 4：按 restore.kind 逐文件还原：inline→写回 blobs 字节；ref→从引用源复制；absent→删除
  校验 5：写后重读核指纹 + 重载 registry/planner（不改审计账本与快照库自身）
  记录 6：route_config_restored（§3.5）
```

| 触发器 | 来源（实测锚点） | 行为 |
|---|---|---|
| **健康检查失败（客观信号，唯一自动触发器）** | 变更生效后对受影响 route 跑既有 `probe_model`；结论过 `classify_failure`：**连续 2 次（占位阈值，可配置）非可重试失败，或单次 credential_terminal 级（401/402，`resilience.py:75/:80`）** | 自动 restore + `route_switch_failed` 事件入账本 |
| 人工命令 | 操作员显式调用 CLI | 手动回滚（trigger:"manual"） |

自动回滚是**仅有的一个自动触发器**，除此之外不存在任何自动还原路径（T4 断言）。探测零新增网络面（复用既有探测器与超时纪律）；一次失败一次回滚——回滚后的验证探测**只记录、不决策**（不自动重试换路、不触发二次自动动作，防回滚风暴，闭环交人工）。

### 3.4 还原语义：两阶段、部分失败不静默

还原是写操作，半还原比不还原更危险，故两阶段：

1. **预检**：逐文件比对当前指纹与快照预期（存在性 + sha256）。文件已被后续动作再修改（例如回滚前又有一次合法变更）→ 该文件预检失败。**预检有任一失败 → 整体不执行**，记 `route_restore_failed`（逐文件原因），人工介入；
2. **执行**：预检全过才逐文件写入。执行中失败（磁盘/权限）→ **中止剩余**并如实记录已完成与未完成清单，不重试不吞错。

还原范围硬边界：只允许触碰快照清单内的配置文件；`usage.db`、failover 审计账本、快照库自身永不触碰。还原全程零网络。

### 3.5 审计事件（同账本落点、新增事件种）

落 `failover_audit` 同一 store（`usage.db`），沿其字段 allowlist + 脱敏 + `user_version` 增量迁移纪律（`analytics.py` `_migrate_failover_audit` 同款手法）：

```json
{ "event": "route_config_snapshot_taken", "at": 0, "snapshot_id": "rcs_<uuid>",
  "config_fingerprint": "<整组指纹>", "file_count": 2, "trigger": "pre_apply", "operator": "cli" }
{ "event": "route_switch_failed", "at": 0, "snapshot_id": "rcs_<uuid>",
  "failure_class": "provider_unavailable | credential_invalid | rate_limited | …",
  "probe_attempts": 2, "detail": "<占位化，零原始错误正文>" }
{ "event": "route_config_restored", "at": 0, "snapshot_id": "rcs_<uuid>",
  "trigger": "health_check_failed | manual", "operator": "system | cli",
  "file_count": 2,
  "files": [ { "path": "<占位>/registry.yaml", "sha256": "<还原后（=改前）指纹>" } ] }
{ "event": "route_restore_failed | route_restore_refused", "at": 0, "snapshot_id": "rcs_<uuid>",
  "trigger": "<同上>", "reason": "<逐文件预检失败/拒绝原因，占位化>" }
```

**`route_config_restored` 的完整性即 T4 断言对象**：必须带齐快照 ID、触发器、操作员、文件数、逐文件指纹、时间——缺任一字段视为审计不合规（T4 逐字段断言）。事件**零文件内容、零密钥**（指纹可以、正文不可以），对齐 `classify_failure` 不留原始错误正文的既有纪律。

## 4. 安全红线

1. **自动回滚的唯一触发器 = 健康检查失败客观信号**：学习/推荐面只出推荐——其 import 图无 routesnap 依赖、无配置写路径；除 probe 失败一处 + manual 一处外无任何还原调用点（T4 结构断言）；
2. **无快照不生效**：配置生效动作前快照失败 → 变更拒绝落盘（fail-closed，快照遗漏在结构上导致"改不了"，而不是"不可回滚"）；
3. **快照含凭据字节的隔离**：`registry.yaml` 实测含 `api_key`——快照库仅本机 `<数据目录>/routesnap/`（0700/0600）、不入仓库、不进日志/审计/外发通道；审计只留指纹不留正文；
4. **回滚可再回滚**：逆快照先行；回滚后无二次自动动作（一次失败一次回滚，其余交人工）；
5. **零新增网络面**：健康检查复用既有 probe；restore 零网络；
6. 示例全虚构：配置路径、指纹、provider 名、密钥全占位；文档与测试无真实主机名/IP/账号/密钥。

## 5. T2–T5 任务拆分（按 PRD）

| 任务 | 内容 | 交付 |
|---|---|---|
| T2 | 快照存储与回滚最小实现：`routesnap/`（snapshot/restore/events）+ `<数据目录>/routesnap/` 快照库 + 两阶段还原 + 逆快照 + 配置生效动作前 fail-closed 接线 | 样例可快照可回滚，审计可查，有测试 |
| T3 | 健康检查失败自动回滚：变更后 probe 信号（阈值 + credential_terminal 判定）→ 自动 restore + `route_switch_failed` 入账本；回滚后验证探测只记录不决策 | 自动回滚与事件有测试 |
| T4 | 只出推荐红线断言：还原调用点仅 probe 失败 + manual 两处（代码路径扫描）+ 学习/推荐面 import 图无 routesnap/配置写路径 + `route_config_restored` 字段完整性 | 断言通过 |
| T5 | 验收试运行：样例全流程回放（快照→变更→探测失败→自动回滚→switch_failed/restored 留痕→再回滚）+ 回滚演练（无快照/预检失败/凭据文件快照不外泄）+ 文档无密钥 | 快照回滚全链通过；红线断言全过；文档无密钥 |

## 6. T5 验收标准

1. 虚构 provider 走完 快照→配置变更生效→探测失败（合成失败样例，零真实网络）→自动回滚：逐文件回到改前状态（指纹比对），`route_config_snapshot_taken`/`route_switch_failed`/`route_config_restored` 全程留痕；
2. 探测通过路径：不回滚、无 `switch_failed`；连续失败未达阈值：不回滚（断言）；
3. 无活动快照调用 restore → `route_restore_refused`；预检有文件不符 → 整体不执行且 `route_restore_failed` 逐文件如实记录；
4. 回滚后再回滚（逆快照路径）成功，任意时刻每配置面仅一个活动快照；
5. 结构断言全过：自动触发器仅 probe 失败一处 + manual 一处；学习/推荐面 import 图无 routesnap 依赖与配置写路径；快照库不入仓库（`.gitignore` 生效）；fail-closed（无快照的变更被拒）；
6. 含伪密钥样例（明显假值）的 `registry.yaml` 快照：字节只落本机快照库（0600），审计与文档零密钥、零文件正文；文档无内网主机名。

## 7. 借鉴划界

- [Gathin23/cleanyourtv](https://github.com/Gathin23/cleanyourtv)：GitHub License API 实测 **NOASSERTION（无协议）**（中央 PRD 记录 2026-10-28 实测，本设计 2026-10-29 复核一致）。本设计只借鉴"**先快照、再改动、一键还原**"这一产品形态思路（通用工程常识，不构成独创表达），**未读取、未复用其任何代码、schema、文案、配置**。本仓快照对象字段、两阶段还原语义、审计事件全部基于本仓既有资产（`save_registry` 备份轮转、`probe_model` 探测、`classify_failure` 失败分类、`failover_audit` 账本纪律）原创推导。因其无许可证，任何形式的代码或文案引入都有法律风险，明确禁止，未来也不会引入。
- 与 PRD-257 的划界落实：中央 PRD 背景称"区别于 PRD-257 统一路由（协议层）：本条是配置变更可逆"——本设计落实为层划界（257=协议合同与路由表、切换规范，280=配置文件变更的快照/还原/回滚审计），并明确 280 不实现、不依赖 257 的协议层；`routes.json` 仅作为快照覆盖面的预留对象（`role:"route_table"`）。
- 编号修正：中央 PRD 任务表所写 T1 issue #1405 为旧编号，实际 issue 为 [zhanglunet/trend#1401](https://github.com/zhanglunet/trend/issues/1401)（实测为准）。
