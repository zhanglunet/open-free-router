# PRD-473/T5 开发记录：CLI 一键客户端配置验收试运行（2026-11-17）

> trend issue #2289。本链收官单（T1 #221 / T2~T4 #222 / T5 本单）。

## 交付

- `tests/test_prd473_t5_cli_doctor_acceptance.py`：7 例全绿（本链合计 16 例：T2~T4 9+T5 7）。
  - 全链两遍逐字节一致：doctor before（error：无配置）→ setup 生成器（备份+只填空缺键）→ doctor after（warning→ok：config 修好了但 key 环境变量需另设——BYOS 可后补是 T1 设计口径）。
  - 失败演练：已有配置不被覆盖（custom_field 保持）/零留存凭据不返回/子进程 env 不写文件。
  - 链复跑：T2~T4 子进程复跑 exit 0。文档无密钥（needle 排除设计文档合法引用）。

## PR 门槛合规核对（repos.yaml default_pr_gate）

| 门槛 | 落实 |
|---|---|
| require_integration_point | 接入点全链演练：doctor before → setup → doctor after（error→ok） |
| no_missing_channel | 全链零网络；唯一子进程是零留存启动器 T4 设计写死的例外 + 链复跑测试自身 |
| no_duplicate_module | 只消费本链 T2~T4 产物，零新模块 |
| impl_requires_devlog | 本文件即开发记录 |

## 合规核对

- setup 绝不覆盖已有内容（测试钉值）；零留存启动器不替用户保管密钥（测试钉值）。
- 示例全合成占位。只开 PR 不合并。
