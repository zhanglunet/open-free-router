# PRD-473/T1：CLI 一键客户端配置与 doctor 设计

> PRD：adopted/PRD-473-ofr-cli-setup-doctor.md（issue #2285）
> 借鉴源：freellmapi（MIT）——只借鉴思路，零代码复用。
> 日期：2026-11-17

## §1 doctor 诊断（T2）

一键自检四项：配置文件 / 密钥环境变量 / 连通性 / 目录新鲜度。

| 检查项 | 检查什么 | 结论 |
|---|---|---|
| config | registry.yaml 存在且可解析 | ok/warning/error |
| keys | 每个 provider.api_key_env 环境变量非空 | ok/warning（缺 key 只警告不报错——BYOS 可后补） |
| connectivity | 至少一个提供方可达（probe_fn 注入式） | ok/warning/error |
| catalog | 签名目录版本 ≥ MIN_CATALOG_VERSION | ok/warning（无目录时 info） |

结论可操作：每个非 ok 项给出明确的修复建议文字。

## §2 setup 生成器（T3）

拉取当前实时目录 → 生成 registry.yaml 草稿 → 备份已有配置 → 只写不存在的键，覆盖是硬禁。

规则写死：已有 registry.yaml 时先备份（*.bak-时间戳），然后**只填空缺键**，已有键的值（含空值）一律不动。

## §3 零留存启动器（T4）

凭据只注入子进程环境变量，不落盘，进程结束即失效：
- 启动器从环境变量读取密钥 → 组装子进程 env → subprocess.Popen 传递；
- 不写任何文件；不进日志；进程结束凭据即消失。

## §4 与 PRD-173/T4 doctor 的边界

173/T4 doctor 是 BYOS 接入层的自检（密钥注册状态、订阅状态）；本条是客户端配置层的自检（registry.yaml 可解析性、环境变量、连通性）。零交集。
