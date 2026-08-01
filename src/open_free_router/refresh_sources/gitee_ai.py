#!/usr/bin/env python3
"""Gitee AI (模力方舟) free models source.

Gitee AI at https://ai.gitee.com offers 100 free calls/day for any
model. The /v1/models endpoint returns 235+ models with no pricing
field — since every model shares the same free quota, there is no
free/paid signal to filter on. Instead we use a curated whitelist of
popular chat/reasoning models that are most useful to route to.

Auth is optional for listing models; a key (when present) is sent
as Bearer token for a more reliable response.

Not covered by CI (network access required); run
`open-free-router refresh --source gitee-ai` to verify.
"""
from __future__ import annotations

from typing import List

import requests

from open_free_router.registry import ModelInfo

SOURCE_NAME = "gitee-ai"

# Curated whitelist of popular models worth tracking.
# Gitee AI offers 100 free calls/day for ANY model, so this is a
# selection of the most useful ones rather than a free/paid filter.
KNOWN_FREE = [
    "DeepSeek-V3",
    "DeepSeek-R1",
    "DeepSeek-V3.2",
    "DeepSeek-V4-Flash",
    "Qwen3-32B",
    "Qwen3-Coder-Flash",
    "Qwen3.5-Flash",
    "Qwen3.7-Max",
    "GLM-4.6",
    "GLM-5.2",
    "kimi-k2-instruct",
    "MiniMax-M3",
    "ERNIE-4.5-Turbo",
    "ERNIE-X1-Turbo",
    "gpt-oss-120b",
    "QwQ-32B",
]

# Models that support reasoning / chain-of-thought.
REASONING_MODELS = {"DeepSeek-R1", "QwQ-32B", "ERNIE-X1-Turbo"}


def fetch(provider_base_url: str, api_key: str | None = None) -> List[ModelInfo]:
    models: List[ModelInfo] = []
    try:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        r = requests.get(
            f"{provider_base_url}/models",
            headers=headers,
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  ✗ {SOURCE_NAME} fetch failed: {e}")
        return models

    for m in data.get("data", []):
        mid = m.get("id", "")
        if mid not in KNOWN_FREE:
            continue
        models.append(ModelInfo(
            id=mid,
            name=m.get("name", mid),
            reasoning=mid in REASONING_MODELS,
            tool_calling=mid == "gpt-oss-120b",
        ))

    print(f"  Found {len(models)} {SOURCE_NAME} models")
    return models
