# PRD-079/T5 验收报告：沙盒层验收

- 日期：2026-10-19
- 范围：trend #420；验收对象为 T2（#52）/T3（#53）/T4（#54）已合并进 main 的 `sandbox` 包
- 验收方式：`tests/test_sandbox_acceptance.py`（19 个测试，逐项映射 T1 设计文档 §10）+ 回归套件 + 反向验证 + 本报告
- 结论：**§10 十项全部通过**，无阻塞项；已知局限见末节

## 逐项对照（T1 §10）

| # | 验收标准 | 结果 | 测试 |
|---|---|---|---|
| 1 | 指定单模型，`print("hi")` 跑通，stdout/exit_code/duration 完整 | ✅ 通过 | `test_10_1_single_model_print_hi` |
| 2 | mock 模型失败序列（A 429 → B 成功），换模型重试链路打通且**只调两次** | ✅ 通过 | `test_10_2_failover_429_then_success_two_calls`（断言 `calls == ["model-A","model-B"]`，switch 记录 outcome=switched→success） |
| 3 | `sys.exit(3)`：不换模型，直接返回 exit_code=3 | ✅ 通过 | `test_10_3_code_exit_3_no_switch`（审计 `model_switched=false`） |
| 4 | `while True`：超时被杀，`timed_out=true`，执行后无残留进程（pgrep 验证） | ✅ 通过 | `test_10_4_timeout_kills_tree_no_residue`（父进程 + 特意起的子进程都被杀，pgrep 无残留；失败工作目录保留） |
| 5 | 写 `/tmp/evil.txt`（工作目录外）：执行前被拒，明确错误 | ✅ 通过 | `test_10_5_path_jail_blocks_outside_write`（绝对路径与 `..` 跳出两种，文件确未创建） |
| 6 | `provider=none` 时网关现有行为零变化（回归套件全绿） | ✅ 通过 | `test_10_6a_provider_none_refuses`（`SandboxDisabledError` + CLI 明确报错）；`test_10_6b_existing_sandbox_suites_stay_green`（T2/T3/T4 共 82 测试全绿） |
| 7 | `routing.py` / `executor.py` / `translate.py` diff 为空 | ✅ 通过 | `test_10_7_sandbox_imports_no_gateway_internals`（AST 扫描 sandbox 包，零网关 import）；API 核验 #52/#53/#54 的文件清单：三 PR 均未触碰网关三文件 |
| 8 | 审计只有代码 sha256、无原文；伪造 `sk-xxx` 输出被脱敏 | ✅ 通过 | `test_10_8a_audit_has_sha256_not_code`（sha256 精确匹配，原文不在日志）；`test_10_8b_output_redaction`（`[REDACTED]` 替换，日志无残留） |
| 9 | 危险模式（`rm -rf /`、反弹 shell）被黑名单拦截 | ✅ 通过 | `test_10_9_blacklist_blocks_dangerous_code`（3 种：rm -rf、/dev/tcp、curl\|bash；拦截记审计且用户代码不进日志） |
| 10 | 用户文档：怎么开、怎么指定模型、失败策略三节齐全 | ✅ 通过 | `test_10_10_user_doc_sections`（`docs/sandbox-guide.md` 四节：开启/指定模型/失败策略/安全边界） |

额外（T1 §7 漂移防护 + 串联）：

| 项 | 结果 | 测试 |
|---|---|---|
| 网关 `/v1/chat/completions` 的 model 透传钉住（用户 ID 原样送达，不被改写） | ✅ 通过 | `test_contract_model_id_passthrough`（拦截 HTTP 请求体断言 `model` 字段） |
| 端到端串联：429 换模型 → 执行 → 审计落账 → 输出脱敏，一次跑通 | ✅ 通过 | `test_e2e_full_chain`（switch 记录 + `model_switched=true` + sha256 + 输出脱敏） |
| 双封顶 `max_model_retries` | ✅ 通过 | `test_double_cap_max_model_retries`（第 3 个模型永不被调） |
| 401/403 停整个列表 | ✅ 通过 | `test_401_aborts_whole_list`（只调一次） |
| 400 直接返回不换模型 | ✅ 通过 | `test_400_returns_without_switch`（只调一次） |

## 测试数据

- 验收测试：**19/19 通过**
- 回归（T2/T3/T4 既有套件）：**82/82 通过**（runner 27 + caller 24 + hardening 31）
- 反向验证（CLAUDE.md 纪律：每个守卫做一次破坏性反例，确认测试真红）：**7/7 通过**
  - 黑名单失效 → 拦截测试变红；路径 jail 失效 → 越界写测试变红；脱敏失效 → 脱敏测试变红；
    审计失效 → sha256 测试变红；429 不换模型 → 失败重试测试变红；model 被改写 → 透传契约测试变红；
    超时标记丢失 → 超时测试变红。恢复后全部回绿，业务代码逐字节还原（diff 全 pristine）。

## 已知局限（非阻塞，诚实记录）

1. **模型侧用 mock，未做"真网关 + 真上游 key"的端到端**：`call_with_retry` 的失败分类/退避/双封顶逻辑已全覆盖，但真实 429/5xx 的网关行为差异（如各家 429 文案）留待 060/T5 回归验收时实测。
2. **docker/e2b/daytona 后端未实现**：T1 明确本期不做，配置即显式报错（`test` 覆盖 `SandboxNotImplementedError` 由 T2 既有测试保证）。
3. **资源限制的诚实上限**（T4 已披露，本报告重申）：`RLIMIT_AS` 限虚拟地址空间非 RSS；`classify_resource_kill` 尽力分类；fork 炸弹靠 `timeout_s` 兜底。
4. **脱敏是模式匹配**：新型密钥格式可能漏网；但审计记录本身只含 sha256，`assert_no_secrets` 是纵深防御。
5. **威胁模型上限**（T1 §6）：local 后端防失误不防攻击者；黑名单是兜底。文档 `docs/sandbox-guide.md` 已写清，不夸大。
6. **平台相关**：超时杀进程树/pgrep 验证/resource 限制均为 POSIX 路径；Windows 下 `LocalProvider` 会诚实报错（T2 设计）。
7. 验收测试中资源限制取 `None`（求快）；CPU/内存限制本身由 T4 的 31 个测试覆盖。

## 已知问题

**无**。验收过程中未发现需要修的业务代码 bug（T5 纪律：只动测试 + 文档，不做"顺手修"）。
唯一一次测试失败是验收测试自身的断言过严（审计原因里含黑名单标签自带 "/dev/tcp" 字样），已修正为断言"用户代码原文不进日志"，与业务代码无关。

## 交付物

- `tests/test_sandbox_acceptance.py`（19 测试）
- `docs/sandbox-guide.md`（用户文档：开启 / 指定模型 / 失败策略 / 安全边界）
- 本报告 `docs/plans/2026-10-19-sandbox-acceptance.md`

PRD-079 至此 T1–T5 全部完成：设计（#51）→ 执行器（#52）→ 模型自选与重试（#53）→ 安全加固（#54）→ 验收（本 PR）。
