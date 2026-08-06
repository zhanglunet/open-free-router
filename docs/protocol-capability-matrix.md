# P1 协议与能力兼容矩阵

更新时间：2026-08-06
适用版本：v0.4.0

## 1. 口径

本矩阵回答“注册表配置能否经过本地路由器适配到客户端协议”，不回答“某家服务器
此刻是否在线”。下列状态均为声明能力；实时联网结果必须以本机 `probe` 或公开
Cloudflare 服务端状态页为准，二者不能互相替代。

运行：

```bash
open-free-router protocols
open-free-router protocols --json
open-free-router doctor --json
```

## 2. 客户端协议矩阵

| 客户端协议 | 入口 | 非流式文本 | 流式文本 | 工具循环 | Usage | 结束语义 | 错误格式 |
|---|---|---:|---:|---:|---:|---|---|
| OpenAI Chat Completions | `/v1/chat/completions` | ✓ | ✓ | ✓（模型需声明） | ✓ | `finish_reason` | OpenAI |
| OpenAI Responses | `/v1/responses` | ✓ | ✓ | ✓（模型需声明） | ✓ | `response.completed` | OpenAI |
| Anthropic Messages | `/v1/messages` | ✓ | ✓ | ✓（模型需声明） | ✓ | `stop_reason` / `message_stop` | Anthropic |

三种协议共享同一个候选计划与上游执行器：首字节前可按规则 fallback；一旦向客户端
发送响应头或流事件，禁止重放到第二个模型。429 的 `Retry-After` 会保留，厂商私有
账号、追踪和扩展字段不会进入不兼容的 Responses/Messages envelope。

## 3. 内置提供商声明矩阵

所有内置提供商都通过 OpenAI-compatible Chat Completions 上游进入统一适配层；表中
“工具/推理”仅统计注册表已明确标记的模型，不代表未标记模型一定不支持。

| 提供商 | 模型 | 已声明工具模型 | 已声明推理模型 | Chat | Responses | Messages |
|---|---:|---:|---:|---:|---:|---:|
| DeepSeek 开放平台 | 2 | 0 | 0 | ✓ | ✓ | ✓ |
| Google AI Studio | 3 | 0 | 0 | ✓ | ✓ | ✓ |
| GroqCloud | 6 | 2 | 2 | ✓ | ✓ | ✓ |
| NVIDIA NIM | 5 | 0 | 1 | ✓ | ✓ | ✓ |
| Nous Research | 2 | 0 | 0 | ✓ | ✓ | ✓ |
| Poolside AI | 2 | 0 | 2 | ✓ | ✓ | ✓ |
| OpenCode Zen Free | 7 | 0 | 0 | ✓ | ✓ | ✓ |
| OpenRouter | 9 | 1 | 0 | ✓ | ✓ | ✓ |
| SenseNova | 2 | 0 | 0 | ✓ | ✓ | ✓ |
| StepFun | 1 | 0 | 0 | ✓ | ✓ | ✓ |

Google AI Studio 必须使用
`https://generativelanguage.googleapis.com/v1beta/openai`。旧注册表若仍是
`/v1beta`，`doctor` 会报 `google_openai_compatibility_path_missing` 并打印修复值。

## 4. 自动化验收

`tests/test_protocol_matrix.py` 通过真实本地 HTTP socket 模拟上游，数据驱动验证：

- 文本与仅 `reasoning_content` 输出的流式/非流式转换；
- function/tool call 及三类客户端结束语义；
- usage 字段映射、429 envelope 与 `Retry-After`；
- 厂商私有字段的跨协议隔离；
- 内置注册表恰好形成 11 × 3 = 33 行且没有配置错误。

`tests/test_proxy_fallback.py` 另对三种协议共同验证首字节前 fallback 和首字节后禁止
二次请求。所有测试均使用占位凭据和本机假上游，不访问真实厂商，也不读取用户 Key。
