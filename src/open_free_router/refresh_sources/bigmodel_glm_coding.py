#!/usr/bin/env python3
"""BigModel GLM Coding Plan models source.

The Coding Plan uses a dedicated OpenAI-compatible endpoint:
https://open.bigmodel.cn/api/coding/paas/v4

The model-list response does not expose stable free-tier pricing metadata, so
this source follows the same conservative allowlist pattern as DeepSeek and
StepFun. Refresh only adopts GLM model IDs that are both returned by the live
endpoint and listed in KNOWN_FREE.
"""
from __future__ import annotations

from typing import List

import requests

from open_free_router.registry import ModelInfo


SOURCE_NAME = "bigmodel-glm-coding"

KNOWN_FREE = [
    "glm-5.2",
    "glm-5",
]


def fetch(provider_base_url: str, api_key: str | None = None) -> List[ModelInfo]:
    if not api_key:
        return []

    models: List[ModelInfo] = []
    try:
        r = requests.get(
            f"{provider_base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
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
        ctx = m.get("context_window") or m.get("context_length") or 1_048_576
        max_tokens = m.get("max_tokens") or m.get("max_output_length") or 65_536
        models.append(ModelInfo(
            id=mid,
            name=m.get("name", mid),
            context_window=int(ctx),
            max_tokens=int(max_tokens),
            reasoning=True,
            tool_calling=True,
        ))

    print(f"  Found {len(models)} free {SOURCE_NAME} models")
    return models
