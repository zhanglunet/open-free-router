#!/usr/bin/env python3
"""BYOS（自带订阅）接入层 —— PRD-173/T2。

T1 契约：``docs/plans/2026-10-25-byos-access.md``（trend issue #865）。
T2 落地 ``byos-openai-compat`` 与 ``byos-custom`` 两个适配（T1 §2 规则 1）；
``byos-anthropic-compat`` 为 T1 标注的可选项，随后续任务并行。

安全边界（T1 §6）：密钥只经环境变量且 registry/config 只存变量名；
``env.py`` 是唯一 env 访问点；适配器只有 ``resolve`` 与 ``probe`` 两个动作；
写前扫描拒收任何携带密钥值的字段。T3（订阅优先排序）、T4（config byos 块
接线 / doctor 检查组 / 额度与账本对接）按 T1 §4/§5 另行落地。
"""
from __future__ import annotations

from open_free_router.byos.adapters.custom import CustomAdapter
from open_free_router.byos.adapters.openai_compat import OpenAiCompatAdapter
from open_free_router.byos.base import (
    BaseByosAdapter,
    ByosAdapter,
    ByosDescriptor,
    TOS_NOTE_ZH,
)
from open_free_router.registry import ByosMetadata

# Known adapters, keyed by registry provider name. The opt-in allowlist in
# config (T1 §3.4, wired in T4) may only reference names present here --
# unknown names are warnings, never a silent widening of the adapter set.
ADAPTERS: dict[str, type[BaseByosAdapter]] = {
    adapter.descriptor.name: adapter
    for adapter in (OpenAiCompatAdapter, CustomAdapter)
}


def make_adapter(name: str) -> ByosAdapter | None:
    """Instantiate a known adapter by name; ``None`` for unknown names."""
    adapter_cls = ADAPTERS.get(name)
    return adapter_cls() if adapter_cls else None


__all__ = [
    "ADAPTERS",
    "BaseByosAdapter",
    "ByosAdapter",
    "ByosDescriptor",
    "ByosMetadata",
    "CustomAdapter",
    "OpenAiCompatAdapter",
    "TOS_NOTE_ZH",
    "make_adapter",
]
