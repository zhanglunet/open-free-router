# PRD-014/T1 草稿：只读 MCP tools 接口 schema

> 3 个只读 tool 的输入/输出 JSON schema + 示例。T2/T3 按此实现。

## tool 1：`list_available_models`

查当前可用模型。只读。

**输入**

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "properties": {
    "provider": {
      "type": "string",
      "description": "可选，按提供商过滤（如 openrouter、groq、deepseek）。不填返回全部。"
    }
  },
  "additionalProperties": false
}
```

**输出**

```json
{
  "type": "object",
  "required": ["models", "probed_at"],
  "properties": {
    "probed_at": { "type": "string", "format": "date-time", "description": "本次探测批次时间（注意：探测是定时批次，非实时）" },
    "models": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["model_id", "provider", "available"],
        "properties": {
          "model_id": { "type": "string", "example": "deepseek/deepseek-chat" },
          "provider": { "type": "string", "example": "openrouter" },
          "virtual_model": { "type": "string", "description": "虚拟模型名（如有）", "example": "free-smart" },
          "available": { "type": "boolean" }
        }
      }
    }
  }
}
```

**示例响应**

```json
{
  "probed_at": "2026-10-17T09:00:00+08:00",
  "models": [
    {"model_id": "deepseek/deepseek-chat", "provider": "openrouter", "virtual_model": "free-smart", "available": true},
    {"model_id": "llama-3.3-70b-versatile", "provider": "groq", "available": true},
    {"model_id": "some/dead-model", "provider": "poolside", "available": false}
  ]
}
```

## tool 2：`get_model_score`

查指定模型的可解释智能评分（原样暴露现有评分，不重新发明）。

**输入**

```json
{
  "type": "object",
  "required": ["model_id"],
  "properties": {
    "model_id": { "type": "string", "description": "list_available_models 返回的 model_id" }
  },
  "additionalProperties": false
}
```

**输出**

```json
{
  "type": "object",
  "required": ["model_id", "score", "breakdown", "updated_at"],
  "properties": {
    "model_id": { "type": "string" },
    "score": { "type": "number", "minimum": 0, "maximum": 100 },
    "breakdown": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["dimension", "score", "evidence"],
        "properties": {
          "dimension": { "type": "string", "example": "可用性" },
          "score": { "type": "number" },
          "evidence": { "type": "string", "description": "得分依据（一句话）", "example": "近 12h 探测成功率 98.2%" }
        }
      }
    },
    "updated_at": { "type": "string", "format": "date-time" }
  }
}
```

## tool 3：`get_live_status`

查实时可用性探测状态。

**输入**

```json
{
  "type": "object",
  "properties": {
    "model_id": { "type": "string", "description": "可选，不填返回全部模型的最新探测状态" }
  },
  "additionalProperties": false
}
```

**输出**

```json
{
  "type": "object",
  "required": ["statuses"],
  "properties": {
    "statuses": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["model_id", "last_probe_at", "latency_ms", "error_rate"],
        "properties": {
          "model_id": { "type": "string" },
          "last_probe_at": { "type": "string", "format": "date-time" },
          "latency_ms": { "type": "number" },
          "error_rate": { "type": "number", "minimum": 0, "maximum": 1 }
        }
      }
    }
  }
}
```

## 错误码

| 场景 | 返回 |
|---|---|
| model_id 不存在 | `{"error": "MODEL_NOT_FOUND", "model_id": "..."}` |
| 未鉴权 | `{"error": "UNAUTHORIZED"}`（T4 实现） |
| 超限 | `{"error": "RATE_LIMITED", "retry_after_s": 60}`（T4 实现） |

## 设计约束（T2/T3 必须遵守）

1. `list_available_models` 的数据源必须与代理实际路由**同一份**（口径一致是验收项）。
2. `get_model_score` 与站内模型雷达页口径一致（同一评分函数）。
3. 所有时间字段带时区；`probed_at` 必须真实，不伪装实时。
