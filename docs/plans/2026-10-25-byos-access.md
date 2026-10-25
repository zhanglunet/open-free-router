# PRD-173/T1：BYOS 提供方清单与接入接口草案

> trend issue #865。对应 PRD-173（中央仓库 zhanglunet/trend）。
> 地位：本文档是 PRD-173 的接入契约稿（沿用 `docs/plans/2026-10-23-failover-audit-ledger.md` 的「T1 定契约、T2 起实现」文档地位惯例）。T2（≥2 个适配落地）、T3（订阅优先排序与回退）、T4（与 PRD-096/131/149 额度与审计对接）、T5（验收）均以本文为准；实现不得擅自增删字段。
> 红线：密钥仅环境变量——registry/config 只存**变量名**，绝不存密钥值；不代管用户订阅账号密码；不破解付费墙；本文与所有示例一律用占位符，无真实主机名/IP/账号/密钥。
> 借鉴声明：BYOS（自带订阅）打法与「用户已有订阅一键接入、已有订阅优先」的形态借鉴 [anomalyco/opencode](https://github.com/anomalyco/opencode)（MIT，版权声明引用保留于中央 PRD-173；仓库 `NOTICE.md` 惯例下 T2 落地时补充 opencode 的 MIT 声明引用）。本文与后续实现均为自研表述，不整仓拷贝、不搬代码。

## 1. 现状衔接：BYOS 是注册表里的一类 Provider，不是新子系统

opencode 的做法是「自定义 provider + 环境变量密钥 + 偏好排序」；open-free-router 已有全部承接结构，BYOS 不需要新子系统，只需要**一个适配层 + 一个元数据扩展 + 一个排序因子**。本文所有设计都落在以下真实结构上（2026-10-25 实测 main）：

| 已有模块 | 现状 | 与 BYOS 的关系 |
|---|---|---|
| `registry.py:ProviderConfig` | `name/base_url/upstream_url/api_key/api_key_env/auth_mode(bearer/none)/api_keys/models/auto_refresh/refresh_method/prefix/free_tier`；`effective_key` 已支持从环境变量解析（`os.environ.get(self.api_key_env)`），registry.yaml 强制 0600 | BYOS provider 就是 `ProviderConfig`：`api_key_env` 只存变量名（现状即如此），`auth_mode` 沿用 bearer/none；新增的只有 `byos` 元数据块（§3.3） |
| `refresh_sources/` 插拔模式 | 每 provider 一个模块，导出 `fetch(base_url, api_key) → list[ModelInfo]` | BYOS 适配器沿同一惯例（§3.1），但**不参与定时刷新**（订阅额度不经 models 目录轮询） |
| `routing.py:RoutePlanner.plan()` | 三种 strategy：`explicit` / `priority`（注册表顺序）/ `scored`；候选按 `max_attempts` 截断；`RoutePlan.explain()` 输出候选与 rejected | 订阅优先 = 在现有 strategy 前加一个**稳定分桶**（`subscribed` 桶优先，桶内保持原顺序/评分），不改三个 strategy 的语义（§4） |
| `quota.py` / `quota_view.py` | `RateLimit-*`/`X-RateLimit-*`/`Retry-After` 归一化为 band：`sufficient/tight/exhausted/unknown/stale`，来源 `upstream_headers/event/never_measured` | 订阅类上游多数不给结构化额度头 → 诚实标 `never_measured`，**不猜余额**（§5） |
| `cli.py:cmd_doctor` | 检查项形状 `{ok, severity, label, detail}`；分组 checks/routing/free_tier/protocol_matrix | BYOS 新增一个检查组（§5），沿用形状与 `doctor_summary_from_report` 汇总，账本列不改 |
| `failover_audit.py`（PRD-149 已合并 main） | 成对账本 `failover_decisions`(+8 上下文列) + `failover_outcomes` + `doctor_state`，`user_version=3` | 账本**不加列**：BYOS 上下文经既有 `requested_model`/`strategy`/quota 列表达（§5） |
| `executor.py` / `resilience.py` | 统一走 `FailureDecision.kind` 分类与三层隔离 | BYOS 上游与免费上游同一条执行/熔断/换路路径，零特判 |

差距即本 PRD 要补的三件事：**没有「订阅类」这个类别**（注册表分不出免费额度与用户自备订阅）、**没有适配器契约**（各订阅网关的发现/校验/探活动作没有统一接口）、**没有订阅优先的排序语义**。§2–§5 逐一补齐。

## 2. BYOS 提供方清单（T2 的实现清单）

按「用户已有订阅」的接入形态分三类。**全部遵守：密钥/令牌只经环境变量；端点地址来自环境变量或用户 config，文档与代码示例一律占位符。** LLM 供应商产品名仅作类别说明，端点不得写死。

| # | 适配器名（registry provider name） | 订阅形态 | auth_mode | 必需环境变量（存名不存值） | 可选环境变量 | base_url 来源 | 默认模型登记 | opt-in |
|---|---|---|---|---|---|---|---|---|
| 1 | `byos-openai-compat` | OpenAI 兼容订阅网关（用户已有订阅经其官方兼容端点或自建网关） | `bearer` | `OFR_BYOS_OPENAI_COMPAT_API_KEY` | `OFR_BYOS_OPENAI_COMPAT_BASE_URL`（缺省占位 `https://<byos-openai-compat-host>/v1`）、`OFR_BYOS_OPENAI_COMPAT_MODEL` | env 优先，缺省占位符并要求用户显式确认 | 用户显式登记（`--model` 或 registry），**不做目录轮询** | 显式：config `byos.adapters` 含该项才启用 |
| 2 | `byos-anthropic-compat` | Anthropic Messages 兼容订阅网关（同上形态） | `bearer`（经本地转换层统一为 Chat Completions） | `OFR_BYOS_ANTHROPIC_COMPAT_API_KEY` | `OFR_BYOS_ANTHROPIC_COMPAT_BASE_URL`（占位 `https://<byos-anthropic-compat-host>`）、`OFR_BYOS_ANTHROPIC_COMPAT_MODEL` | 同上 | 同上 | 同上 |
| 3 | `byos-custom` | 任意用户自备 OpenAI 兼容端点（自带订阅/企业网关皆归此类） | `bearer` 或显式 `none`（须用户在 registry 显式写 `auth_mode: none`） | `OFR_BYOS_CUSTOM_API_KEY` | `OFR_BYOS_CUSTOM_BASE_URL`、`OFR_BYOS_CUSTOM_MODEL` | env 必填，未设置则该适配器保持 `missing_env` 不注册 | 同上 | 同上 |

清单规则：

1. **T2 至少落 2 个**：`byos-openai-compat` + `byos-custom`（假密钥/沙盒验收）；`byos-anthropic-compat` 可并行。PRD 验收「适配可注册，单测绿」以此为准。
2. **不做的事**（PRD 非目标的工程化表述）：适配器不读取 `~/.<客户端>/` 下的会话凭据、不代登录、不刷新 OAuth 令牌、不解析付费墙；接入动作止步于「用用户自备的 env 密钥对用户指定的端点发一次最小探活」。
3. **ToS 免责**：文档（`docs/plans/` 与 README BYOS 节）固定一句：「BYOS 接入是否被提供方条款允许，由用户自行确认；本项目不规避任何访问控制」。每适配器 `descriptor.tos_note_zh` 携带同样文案，doctor 输出可见（§5）。

## 3. 接入接口草案（字段级）

### 3.1 模块落点（沿 `refresh_sources/` 惯例）

```
src/open_free_router/byos/
  __init__.py     # 导出 ByosDescriptor / ByosAdapter / ADAPTERS / make_adapter
  base.py         # 协议与数据类；注册表 byos 元数据映射（纯函数）
  env.py          # env-only 解析：只读 os.environ，永不读文件；值永不进日志/异常
adapters:
  openai_compat.py    # T2
  anthropic_compat.py # T2 可选
  custom.py           # T2
tests/test_byos_adapters.py   # T2：注册、缺 env、探活、假密钥沙盒
```

### 3.2 适配器协议（T2 实现契约）

```python
@dataclass(frozen=True)
class ByosDescriptor:
    name: str                      # 如 "byos-openai-compat"
    display_name: str              # 如 "BYOS: OpenAI 兼容订阅网关"
    auth_mode: str                 # "bearer"（"none" 仅当用户 registry 显式声明）
    key_envs: tuple[str, ...]      # 必需 env 变量名；只存名
    base_url_env: str              # 端点 env 变量名
    default_base_url: str          # 占位符，如 "https://<byos-openai-compat-host>/v1"
    priority_class: str            # 恒 "subscribed"（§4）
    tos_note_zh: str               # §2 规则 3 的固定文案

class ByosAdapter(Protocol):
    descriptor: ByosDescriptor
    def resolve(self, env: Mapping[str, str] = os.environ) -> ProviderConfig | None:
        """env 齐备 → 返回可注册的 ProviderConfig；缺 env → None（记录 missing_env，
        绝不抛异常、绝不打印变量值）。返回的 ProviderConfig 满足：
        api_key_env=变量名、api_key 恒空串、byos=ByosMetadata(...)。"""
    def probe(self, provider: ProviderConfig, timeout: float = 10.0) -> dict:
        """最小 Chat Completions 1-token 探活（复用 probe.py 哲学与超时口径）。
        返回 {ok, status_code, latency_ms, classification}；classification 只用
        FailureDecision.kind 枚举。永不携带请求正文上下文。"""
```

### 3.3 注册表字段扩展（纯加法）

`ProviderConfig` 新增 `byos: ByosMetadata | None = None`；`Registry._load/to_dict` 增加对应可选键，缺省行为零变化：

```yaml
byos-openai-compat:                # registry.yaml（0600 不变）
  base_url: https://<byos-openai-compat-host>/v1   # 占位示例；env 优先
  api_key_env: OFR_BYOS_OPENAI_COMPAT_API_KEY       # 只存变量名（现状能力）
  auth_mode: bearer
  refresh_method: byos            # 新增取值：定时刷新跳过该 provider
  prefix: byos-oc                 # 模型 ID 前缀，沿用现有 canonical_id 规则
  byos:
    kind: subscription_gateway    # 目前唯一取值；留枚举空间
    priority_class: subscribed    # subscribed / free（免费 provider 无 byos 块 → free）
    enabled: true                 # false = 保留配置但退出排序与探活
    adopted_at: 2026-10-25T00:00:00Z
```

`ByosMetadata` 字段固定为 `kind/priority_class/enabled/adopted_at` 四个，T3/T4 不得再扩（要扩走本文修订）。

### 3.4 config.yaml 形状

```yaml
byos:
  enabled: true              # 总开关；false 时全部 BYOS 适配器不注册、doctor 跳过
  prefer_subscribed: true    # T3 的订阅优先开关；T1 只定字段与默认值，不实现排序
  adapters:                  # 显式允许清单（opt-in 红线）：空 = 不注册任何 BYOS
    - byos-openai-compat
    - byos-custom
```

`Config` 新增 `byos_enabled / byos_prefer_subscribed / byos_adapters` 三个属性，解析规则沿用 `mcp`/`discovery` 块的容错惯例（非 dict 回空、bool 严格 `is True`、清单元素必须是 `ADAPTERS` 内的已知名，未知名记 warning 丢弃——不静默扩面）。

### 3.5 校验与失败语义

| 场景 | 行为 |
|---|---|
| 必需 env 缺失 | 适配器 `resolve` 返回 None；doctor 报 `byos_env_missing`（warning 级）；**不注册** provider |
| env 有值但探活失败 | 注册但标 `probe: failed`（沿用 evidence 的「目录可见、不宣称已核验」哲学）；路由正常参与，失败交给三层熔断 |
| `byos.enabled=false` 或不在 `adapters` 清单 | 适配器完全不加载；已注册的 byos provider 的 `enabled=false` 时从候选中排除（routing `rejected` 记 `byos_disabled`，走现有 rejected 通道，不加新字段名） |
| env 值出现在任何待写文件/日志 | 沿用 `analytics.SENSITIVE_PATTERN` 拒收哲学：写前扫描，命中即拒并 warning（§6） |

## 4. 订阅优先排序与回退衔接（T3 的契约，T1 只定字段）

1. **分桶字段**：`RouteTarget` 增加只读属性 `byos_class`（provider 有 `byos.priority_class` 取之，否则 `"free"`）。`RoutePlan.explain()` 的每个候选增加 `byos_class` 键——纯加法，`candidates/rejected/selected` 形状不变。
2. **排序语义**：`byos.prefer_subscribed: true` 时，在现有 strategy 计算出顺序之后执行一次**稳定分桶**：`subscribed` 桶整体前置，桶内相对顺序不变；`explicit` 请求不参与（用户点名哪个就是哪个，BYOS 不劫持显式模型）。`false` 时行为与今天逐字节一致。
3. **回退**：无新增规则——BYOS 候选与免费候选同链排队，走 PRD-059 宪法的触发条件、退避与三层熔断；`prefer_subscribed` 只改初始顺序，不改任何转移/恢复语义。账本既有 `strategy` 列记录原 strategy 名（`priority/scored/explicit`），分桶事实经 `explain()` 与遥测呈现，**不新造 strategy 字符串**（避免污染 `FAILOVER_REASONS`/汇总口径）。

## 5. 与额度 / doctor / 账本对接（T4 的契约）

| 对接点 | 契约 |
|---|---|
| 额度 | BYOS 上游给 `RateLimit-*`/`X-RateLimit-*` 头 → 照常归一化入 `quota.py` 通道；**没有头** → `quota_source=never_measured`、`quota_band=unknown`，按宪法 §4.4 中性 0.5 计分，**不猜订阅余额、不按套餐文案推算数字** |
| doctor | 新增检查组 `byos`（插在 free_tier 之后）：每启用的适配器两条检查——`byos env present`（缺 → warning `byos_env_missing`）、`byos probe reachable`（warning 级）；形状严格 `{ok, severity, label, detail}`，detail 只含适配器名与状态枚举，**不含 env 名以外任何值**。汇总经 `doctor_summary_from_report` 自动并入 verdict，账本列零改动 |
| 账本 | `failover_decisions/failover_outcomes/doctor_state` **不加列不改枚举**；BYOS 上下文由既有 `requested_model`/`strategy`/quota 列与 `from/to_candidate`（`byos-oc/<model>` 前缀自明）表达 |
| sync | `sync.py` 对 byos provider 零特判：与免费 provider 同样只向 9 客户端下发本地 proxy token；`refresh_method=byos` 使定时刷新跳过（refresh 调度处按该值过滤，一处判断） |
| metrics | 沿用现有 provider 维度聚合，BYOS 不单开表；导出白名单照旧，env 值永不进 `usage.db` |

## 6. 安全边界（T5 验收锚点）

| # | 红线 | 机制/验收 |
|---|---|---|
| 1 | 密钥仅环境变量 | `ProviderConfig.api_key_env` 只存名；BYOS 适配器**禁止**任何文件读取密钥的代码路径（`env.py` 是唯一 env 访问点，便于审查）；T5 验收「密钥仅 env」即断言 registry/config 落盘内容中无 `OFR_BYOS_*` 的值 |
| 2 | 值不进日志/异常/账本 | `env.py` 不打值、异常不带值；写前 `SENSITIVE_PATTERN` 扫描沿用（拒收 + warning，不中断主流程） |
| 3 | 不代管账号密码、不破解付费墙 | 适配器只有 `resolve` + `probe` 两个动作；PR/文档评审以此为准绳 |
| 4 | ToS 免责成文 | §2 规则 3 文案进 README BYOS 节与 descriptor；缺失即 T2 验收不过 |
| 5 | 占位符纪律 | 本文 `https://<byos-*-host>` 即示例口径；T2 起所有文档/测试 fixture 沿用，出现真实主机名/IP/账号/密钥即验收不过 |
| 6 | opt-in 默认关闭 | `byos.adapters` 默认空、`enabled` 默认 true 但无清单即无适配器——升级用户的零行为变化 |

## 下一步

确认本草案后进 T2（trend issue #866）：按 §2 清单落地 `byos-openai-compat` 与 `byos-custom` 两个适配（假密钥/沙盒），按 §3.2 协议实现 `resolve`/`probe`，`tests/test_byos_adapters.py` 覆盖注册、缺 env、探活失败与红名单拒收；T3（#867，§4 分桶排序与回退）、T4（#868，§5 额度/doctor/账本对接）可并行；T5（#869）以「本地演示 BYOS 优先 + 密钥仅 env + 无密钥入仓」收口。
