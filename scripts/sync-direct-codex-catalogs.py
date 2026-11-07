#!/usr/bin/env python3
"""Write Codex catalogs for the official-provider direct adapters."""
from __future__ import annotations

import json
from pathlib import Path


BASE_INSTRUCTIONS = (
    "You are Codex, a coding agent working in the user's current workspace. "
    "Follow the user's instructions and repository guidance, use the available "
    "tools when useful, preserve unrelated changes, and verify completed work."
)


def model(slug: str, display: str, context: int, *, tools: bool, priority: int) -> dict:
    return {
        "slug": slug,
        "display_name": display,
        "description": "official provider model through the local Chat-to-Responses adapter",
        "default_reasoning_level": None,
        "supported_reasoning_levels": [],
        "shell_type": "shell_command" if tools else "disabled",
        "visibility": "list",
        "supported_in_api": True,
        "priority": priority,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "availability_nux": None,
        "upgrade": None,
        "base_instructions": BASE_INSTRUCTIONS,
        "model_messages": None,
        "include_skills_usage_instructions": False,
        "default_reasoning_summary": "none",
        "support_verbosity": False,
        "default_verbosity": None,
        "apply_patch_tool_type": "freeform" if tools else None,
        "web_search_tool_type": "text",
        "truncation_policy": {"mode": "tokens", "limit": 10000 if tools else 8192},
        "supports_parallel_tool_calls": tools,
        "supports_image_detail_original": False,
        "experimental_supported_tools": [],
        "context_window": context,
        "max_context_window": context,
        "effective_context_window_percent": 95,
        "input_modalities": ["text"],
        "supports_search_tool": False,
        "use_responses_lite": False,
        "tool_mode": "direct" if tools else None,
    }


catalogs = {
    "/Users/john/.codex/deepseek.models.json": [
        model("deepseek-v4-flash", "DeepSeek - V4 Flash (official)", 1048576, tools=True, priority=0),
        model("deepseek-v4-pro", "DeepSeek - V4 Pro (official)", 1048576, tools=True, priority=1),
    ],
    "/Users/john/.codex/glm.models.json": [
        model("glm-4.5", "GLM - 4.5 (official)", 131072, tools=False, priority=0),
        model("glm-4.5-air", "GLM - 4.5 Air (official)", 131072, tools=False, priority=1),
        model("glm-4.6", "GLM - 4.6 (official)", 200000, tools=False, priority=2),
        model("glm-4.7", "GLM - 4.7 (official)", 200000, tools=True, priority=3),
        model("glm-5", "GLM - 5 (official)", 1048576, tools=True, priority=4),
        model("glm-5-turbo", "GLM - 5 Turbo (official)", 1048576, tools=True, priority=5),
        model("glm-5.1", "GLM - 5.1 (official)", 1048576, tools=True, priority=6),
        model("glm-5.2", "GLM - 5.2 (official)", 1048576, tools=True, priority=7),
    ],
}

for path, models in catalogs.items():
    Path(path).write_text(json.dumps({"models": models}, ensure_ascii=False, indent=2) + "\n")
    print(path, len(models))
