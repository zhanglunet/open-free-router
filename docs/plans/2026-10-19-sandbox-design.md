# PRD-079/T1：沙盒层设计文档

> PRD：[PRD-079](../adopted/PRD-079-ofr-sandbox.md)（trend 仓库）· trend issue [#416](https://github.com/zhanglunet/trend/issues/416)
> 状态：**设计定稿，未实现**。本文档是 T2–T4 的契约，实现时不得擅自放宽。
> 红线：`routing.py` / `executor.py` / `translate.py` **零改动**（PRD-059/060 刚合并完）。沙盒层是网关的**客户端**，不是网关的补丁。

## 一句话

网关之上加一层**可选**的代码执行沙盒：用户自选模型，沙盒里跑代码，模型侧失败自动换模型重试。

打个比方：现在的网关像一个配了智能导航的车队调度台——能告诉你走哪条路最快（选路）、哪条路封了自动绕行（故障转移）。但调度台不管"货"：你想让车顺路帮你搬个箱子（跑段代码），得自己找车、自己盯着。本层就是给调度台配的"代办员"：你说目标（跑这段代码），它按你指定的车（模型）去办，车坏了换一辆接着送，货（代码）全程放在带锁的货柜（沙盒）里。

## 总体架构

```
用户
 │
 │  open-free-router sandbox run --model <id> code.py   (CLI，见 §8①)
 ▼
┌──────────────────────────────┐
│ 沙盒层（新增，与网关解耦）      │
│  ┌────────────┐  ┌─────────┐  │
│  │ 模型调用器  │  │ 执行器   │  │
│  │ (model     │  │ (sandbox│  │
│  │  caller)   │  │  runner) │  │
│  └─────┬──────┘  └────┬────┘  │
└────────┼──────────────┼───────┘
         │ HTTP         │ subprocess
         ▼              ▼
  127.0.0.1:8337   沙盒后端
  (本地代理，      (local/docker/none)
   网关现有行为)
```

两条调用线完全独立：

- **模型调用线**：沙盒层经本地代理 `POST http://127.0.0.1:8337/v1/chat/completions` 调模型，`model` 字段透传用户指定的模型 ID。网关按现有"按 model ID 路由"行为处理（proxy.py 原话：*routes by model ID to upstream provider*），**不经过 RoutePlanner 改写、不改网关任何逻辑**。
- **代码执行线**：沙盒层按 `SANDBOX_PROVIDER` 起后端跑代码，与网关进程无关。

沙盒层**不 import 网关内部模块**（`routing` / `executor` / `translate` / `resilience` / `failover` 一律不引用），只走 HTTP + 读 `proxy.token`。退避参数对齐 PRD-059 策略文档的默认值，但**自实现**，不复用 `failover.compute_backoff`——避免与网关内部实现耦合。

## Provider 抽象

借鉴 elie222/rakazo 的 `SANDBOX_PROVIDER` 思路（Apache-2.0，**只借鉴思路，不复用代码**）：用一个配置项决定代码在哪里跑。

| 后端 | 值 | 语义 | 首期 |
|---|---|---|---|
| 本地进程 | `local` | 本机 subprocess 跑代码，工作目录隔离 + 超时杀进程树 | ✅ 首期唯一实现 |
| 容器 | `docker` | Docker 起隔离容器跑 | 接口预留，T2/T4 实现 |
| 关闭 | `none` | 沙盒层整体旁路，网关行为零变化 | ✅ 首期实现（回归用） |
| 远端（预留） | `e2b` / `daytona` | 第三方沙盒服务 | 只留枚举位，不实现 |

配置项（独立配置文件 `sandbox.toml`，不碰网关现有 config）：

```toml
[sandbox]
provider = "local"          # local | docker | none | e2b | daytona
default_models = ["deepseek-v3"]   # 用户指定的模型优先级列表（逗号分隔亦可）
timeout_s = 120             # 单次执行超时（秒）
max_model_retries = 3       # 模型侧失败最多换几个模型（含首选）
total_budget_s = 300        # 单次任务总预算（秒），与 PRD-059/T4 的 300s 对齐
workdir_parent = "~/.open-free-router/sandbox"  # 每次执行的隔离工作目录父目录
network = false             # 沙盒内网络，默认关闭
env_allowlist = ["PATH", "HOME", "LANG"]  # 透传给沙盒的环境变量白名单
```

`docker` / `e2b` / `daytona` 在首期若被配置，启动时直接报错退出（"not implemented in this release"），不静默降级——静默降级会让人误以为有隔离。

## 执行模型

一次执行的输入输出契约（T2 照此实现，T5 照此验收）：

**输入 `SandboxRequest`**

| 字段 | 说明 |
|---|---|
| `code` | 代码原文（首期只支持 `python3`，见 §8② 相关说明） |
| `language` | 首期固定 `"python3"`，非此值直接拒绝 |
| `workdir` | 本次执行的隔离工作目录（沙盒层自动创建，见下） |
| `env` | 环境变量（只允许白名单内的键，值由调用方给） |
| `timeout_s` | 超时秒数，超了杀**整个进程树** |
| `network` | 是否开网络，默认 `false` |

**输出 `SandboxResult`**

| 字段 | 说明 |
|---|---|
| `exit_code` | 进程退出码（`None` 表示被杀） |
| `stdout` / `stderr` | 捕获的输出，各截断上限 1MB（防爆内存） |
| `duration_ms` | 实际耗时 |
| `timed_out` | 是否因超时被杀 |
| `workdir` | 本次工作目录路径（供排查） |

**流程**：`run → capture → timeout`。

1. 沙盒层在 `workdir_parent` 下 `mkdtemp` 建本次隔离目录，代码写入 `main.py`。
2. `subprocess.Popen(["python3", "main.py"], cwd=workdir, env=白名单环境)` 启动，管道收 stdout/stderr。
3. `timeout_s` 到则杀**整个进程树**（父进程 + 所有子进程，防止 `code.py` 起了后台进程残留），`timed_out=true`，`exit_code=None`。
4. 正常结束返回退出码与输出。工作目录默认清理；非零退出或超时时**保留**（供排查，可配）。

**路径 jail**：代码里出现的工作目录外写操作，在执行前做静态路径检查（`..` 跳出、`/` 绝对路径写）直接拒绝——这是第一道；OS 用户权限是第二道。两道都写进 T2 测试。

## 模型自选 + 失败换模型重试策略

用户指定模型的方式：`--model` 单模型，或 `--models a,b,c` 优先级列表（逗号分隔，先后即优先级）。CLI 也支持从 `sandbox.toml` 的 `default_models` 读。

**什么算"模型侧失败"（触发换模型）**：

| 失败 | 处理 |
|---|---|
| 429 / 5xx / 超时 / 连接失败 | 换列表中下一个模型；换之前指数退避（参数对齐 PRD-059 策略文档默认值，自实现） |
| 404（模型不存在） | 跳过该模型，换下一个（不退避，立刻换） |
| 401 / 403（key/权限） | **停止重试整个列表，直接报错**——key 坏了换模型大概率同样失败，不烧额度 |
| 400（bad request，如超长） | **不换模型，直接返回**——换模型也解决不了请求本身的问题 |

**什么算"代码侧失败"（不换模型）**：进程非零退出、断言失败、超时被杀。代码本身有问题，换再贵的模型也一样——直接返回 `SandboxResult`，把退出码和 stderr 交给用户。

**双封顶**（防烧额度，PRD-079 风险项）：`max_model_retries`（默认 3，含首次）+ `total_budget_s`（默认 300 秒，单次任务从第一次模型调用到结束的总墙钟）。任一耗尽即停，报错里写清是"模型都试完了"还是"预算用完了"。

**与网关选路的关系**：沙盒层**不调用 RoutePlanner**，`model` 字段透传用户指定的 ID。网关收到后按现有逻辑按 ID 路由——用户要的就是"我就要这家"，选路不插手。这是"自选"的含义。

## 安全边界

**沙盒里能做什么**：在本次隔离工作目录内读写文件；用白名单内的环境变量；stdout/stderr 输出（各 1MB 上限）。

**沙盒里不能做什么**：写工作目录之外的路径；默认无网络；起常驻进程（执行结束进程树全清）；提权（沙盒继承网关的用户身份，不做 setuid，不以 root 跑）。

**危险操作黑名单**（兜底，非主防）：`rm -rf /` 类路径破坏、反弹 shell（`bash -i >& /dev/tcp/` 等）、`curl|bash` 管道执行。匹配即拒绝执行并记审计。**黑名单是兜底，隔离是主防**——文档写清，不夸大。

**local 后端隔离强度上限**（见 §8②）：subprocess 级 = OS 用户级隔离。能防"意外"（跑飞、死循环、写错目录），**不能防"恶意"**（有意逃逸的用户代码）。威胁模型写死：防失误不防攻击者。真要防攻击者，上 Docker（T2/T4）。

**审计日志**：每次执行记 `{时间, 模型, 代码 sha256（只记哈希，不记原文）, 退出码, 耗时, 是否换模型}`。日志与错误回显走密钥正则脱敏（`sk-`、`api[_-]?key` 等模式打码），与网关 `failover_decisions` 表的脱敏口径一致。

## 与网关的接口

| 项 | 约定 |
|---|---|
| 地址 | `POST http://127.0.0.1:8337/v1/chat/completions`（网关现有端点，零改动） |
| 认证 | `Authorization: Bearer <token>`，token 读网关 config 目录下的 `proxy.token`（0600 权限，`auth.py:get_or_create_proxy_token` 生成，沙盒层只读不写） |
| 模型指定 | JSON body 的 `model` 字段 = 用户指定的模型 ID，原样透传 |
| 响应处理 | 2xx 按正常解析；429/5xx/超时按 §5 策略换模型；其余按 §5 表格处理 |
| 耦合面 | 沙盒层不 import 网关内部模块；只依赖 HTTP 接口 + `proxy.token` 文件位置 |
| 漂移防护 | T5 加契约测试：网关 `/v1/chat/completions` 的 model 透传行为钉住，变了即红 |

沙盒层默认**关闭**（`provider=none` 或无 `sandbox.toml` 时，`sandbox` 子命令提示未启用并退出）。opt-in 设计：用户显式开了才有，不影响现有用户。

## 待定点二选一（T1 结论）

### ① 沙盒入口形态：CLI vs HTTP → **选 CLI**

**结论**：`open-free-router sandbox run --model <id> [--models a,b,c] [--timeout 120] <code.py>`

**理由**：

1. 网关本身是 CLI-first：`open-free-router` 已有 `serve` / `sync` / `route` / `doctor` / `models` 等完整子命令体系（argparse），用户已经在终端里用网关，加一个 `sandbox` 子命令是自然延伸。
2. 沙盒调用是一次性的"代办"语义（送代码进去、拿结果回来），不需要常驻服务；HTTP 意味着再占一个端口（网关已有 8337 代理 + 9057 界面），增加运维面。
3. CLI 可以直接复用终端的 stdin/stdout，`--file` / 管道两种给代码的方式都顺手。

**锁死的子命令表**（T2 照此实现）：

| 命令 | 作用 |
|---|---|
| `sandbox run --model ID [--models a,b,c] [--timeout S] [--network] <code.py \| ->stdin>` | 跑一段代码，打印 JSON 结果（exit_code/stdout/stderr/duration_ms/timed_out/换模型记录） |
| `sandbox config` | 打印当前生效的 `sandbox.toml`（脱敏：不打印 token） |
| `sandbox models` | 列出网关 registry 里的可用模型（只读代理 `/v1/models`，方便用户挑 `--model`） |

HTTP 入口**不在本期**：T4 之后若有"远程触发沙盒"的需求再议，接口契约届时另出设计。

### ② local 后端定位：首期不做 Docker 是否可接受 → **可接受**

**结论**：首期只实现 `local`（subprocess）+ `none`，`docker` 只留接口。

**理由**：

1. 网关的用户是个人开发者，典型场景是"跑段验证脚本/数据处理"，subprocess + 工作目录隔离 + 超时杀进程树已覆盖。
2. Docker 后端要求用户机器有 Docker Engine，直接抬高使用门槛；且网关自身定位是"一条命令跑起来"，首期不引入重依赖。
3. 隔离强度上限在 §6 写死（防失误不防攻击者），**不夸大**——用户知情选择；真要强隔离等 T2/T4 的 docker 后端。

## T5 验收用例清单（先行）

T5 照此清单验收，T2–T4 实现时不得删减：

1. 指定单模型，沙盒跑通 `print("hi")`，stdout/exit_code/duration 完整。
2. mock 模型失败序列（A 返回 429 → B 成功），验证换模型重试链路打通且只调了两次模型。
3. 代码 `sys.exit(3)`：不换模型，直接返回 exit_code=3。
4. `while True: pass`：`timeout_s` 到被杀，`timed_out=true`，执行后无残留进程（`pgrep` 验证）。
5. 代码写 `/tmp/evil.txt`（工作目录外）：执行前被拒，返回明确错误。
6. `provider=none` 时网关现有行为零变化（回归套件全绿）。
7. `routing.py` / `executor.py` / `translate.py` 的 diff 为空（CI 或脚本校验）。
8. 审计日志里只有代码 sha256、无代码原文；伪造 `sk-xxx` 的输出被脱敏。
9. 危险模式（`rm -rf /`、反弹 shell）被黑名单拦截。
10. 用户文档：怎么开沙盒、怎么指定模型、失败策略说明，三节齐全。

## 给 T2/T3/T4 的契约输入

- T2 实现 `SandboxRequest` / `SandboxResult`（§4 表格即字段定义）、`local` 与 `none` 后端、`SANDBOX_PROVIDER` 解析；单测覆盖 §10 的 4/5/9。
- T3 实现模型调用器 + §5 的失败分类表 + 双封顶；单测覆盖 §10 的 2/3（mock 模型端）。
- T4 实现资源限制（CPU/内存，`resource` 模块）、审计日志、脱敏、黑名单；§10 的 8/9 为其验收。
- 任何一步不得 import 网关内部模块；不得修改网关现有文件（除新增 `sandbox` 子命令的 CLI 注册与新增模块文件）。

## 明确不做的

- 通用 agent 框架（多轮规划、工具调用编排）——那是 rakazo 整机的事，本 PRD 只取"沙盒执行"一层。
- 首期 Docker / e2b / daytona 后端（接口预留，实现在后）。
- 首期多语言（只 `python3`）。
- 常驻沙盒服务 / HTTP 入口（本期锁死 CLI）。
- 触碰网关现有行为：沙盒默认关闭；`routing.py` / `executor.py` / `translate.py` diff 为空是 T5 硬门槛。
