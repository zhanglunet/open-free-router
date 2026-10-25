#!/usr/bin/env python3
"""BYOS adapter: OpenAI 兼容订阅网关（PRD-173/T2，T1 §2 row 1）。

用户已有的订阅经其官方 OpenAI 兼容端点或自建网关接入；密钥只经
``OFR_BYOS_OPENAI_COMPAT_API_KEY`` 环境变量（registry 只存变量名），
端点地址经 ``OFR_BYOS_OPENAI_COMPAT_BASE_URL``（缺省为占位符，不可路由，
须用户显式设置后才产生实际流量）。
"""
from __future__ import annotations

from open_free_router.byos.base import BaseByosAdapter, ByosDescriptor, TOS_NOTE_ZH

DESCRIPTOR = ByosDescriptor(
    name="byos-openai-compat",
    display_name="BYOS: OpenAI 兼容订阅网关",
    auth_mode="bearer",
    key_envs=("OFR_BYOS_OPENAI_COMPAT_API_KEY",),
    base_url_env="OFR_BYOS_OPENAI_COMPAT_BASE_URL",
    default_base_url="https://<byos-openai-compat-host>/v1",
    priority_class="subscribed",
    tos_note_zh=TOS_NOTE_ZH,
)


class OpenAiCompatAdapter(BaseByosAdapter):
    descriptor = DESCRIPTOR
    prefix = "byos-oc"  # T1 §5: candidates show up as "byos-oc/<model>"
    model_id_env = "OFR_BYOS_OPENAI_COMPAT_MODEL"
