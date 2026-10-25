#!/usr/bin/env python3
"""BYOS adapter: 任意用户自备 OpenAI 兼容端点（PRD-173/T2，T1 §2 row 3）。

自带订阅或企业网关皆归此类。与 openai-compat 的差别只在端点来源：
``OFR_BYOS_CUSTOM_BASE_URL`` 必填，未设置则该适配器保持未注册
（T1 §2 row 3「env 必填」）。密钥只经
``OFR_BYOS_CUSTOM_API_KEY`` 环境变量；``auth_mode: none`` 仅当用户在
registry 显式声明时生效（适配器默认 bearer）。
"""
from __future__ import annotations

from open_free_router.byos.base import BaseByosAdapter, ByosDescriptor, TOS_NOTE_ZH

DESCRIPTOR = ByosDescriptor(
    name="byos-custom",
    display_name="BYOS: 自备 OpenAI 兼容端点",
    auth_mode="bearer",
    key_envs=("OFR_BYOS_CUSTOM_API_KEY",),
    base_url_env="OFR_BYOS_CUSTOM_BASE_URL",
    default_base_url="",  # env 必填；没有可路由的占位端点
    priority_class="subscribed",
    tos_note_zh=TOS_NOTE_ZH,
)


class CustomAdapter(BaseByosAdapter):
    descriptor = DESCRIPTOR
    prefix = "byos-custom"
    model_id_env = "OFR_BYOS_CUSTOM_MODEL"
    require_base_url_env = True  # T1 §2 row 3: no env, no registration
