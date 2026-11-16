# -*- coding: utf-8 -*-
"""PRD-470/T2+T3（trend issue #2271/#2272 的 T2+T3 环节）：分组合并+命名配置档。

按 T1 设计（docs/plans/2026-11-17-model-groups-design.md）§1/§2 落地：
  - 分组合并（T2）：同 upstream_model_id 多提供方合并为一组，组内按
    priority 严格递增；合并后模型列表无重复；
  - 命名配置档（T3）：回退链可点名引用（coding/vision/default），按请求
    指定优先于默认档；配置档可切换；默认档行为不变（不指定走全量路由）；
  - T4 衔接：resolve_profile(profile_name) 返回分组子集供换模型 target
    路由限制；未指定配置档时零路由变更。
纯内存实现（分组从 providers dict 导出）；零网络零子进程；示例全占位。
测试：tests/test_prd470_t2_model_groups.py
"""
from __future__ import annotations

import copy
from typing import Any

GROUPS_SCHEMA = "model-groups/v1"
PROFILES_SCHEMA = "fallback-profiles/v1"
ERROR_CODES = ("GROUPS_INCOMPLETE", "PROFILE_NOT_FOUND")


class GroupsError(ValueError):
    """分组违规——显形拒绝。"""


def merge_groups(registry_providers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """§1 分组合并：同 upstream_model_id 多提供方合并，组内 priority 递增。"""
    if not isinstance(registry_providers, dict):
        raise GroupsError("GROUPS_INCOMPLETE：providers 非 dict")
    grouped: dict[str, list[dict[str, Any]]] = {}
    seen_ids: set[str] = set()
    priority = 0
    for platform, provider in registry_providers.items():
        for model_id, row in (provider.get("models") or {}).items():
            upstream = str(row.get("upstream_id") or model_id)
            priority += 1
            member = {"platform": platform, "model_id": model_id,
                      "priority": priority,
                      "enabled": row.get("enabled", True)}
            grouped.setdefault(upstream, []).append(member)
            if upstream not in seen_ids:
                seen_ids.add(upstream)
    return {"schema": GROUPS_SCHEMA,
            "groups": [{"model_id": uid, "members": members}
                       for uid, members in sorted(grouped.items())]}


class FallbackProfiles:
    """命名回退链配置档（§2）：按序排列模型 id，按请求指定优先于默认档。"""

    def __init__(self, profiles: dict[str, list[str]], *,
                 default_profile: str = "default") -> None:
        if not profiles:
            raise GroupsError("GROUPS_INCOMPLETE：profiles 不能为空")
        if default_profile not in profiles:
            raise GroupsError("GROUPS_INCOMPLETE：默认档不存在")
        self._profiles = copy.deepcopy(profiles)
        self.default_profile = default_profile

    def resolve(self, profile_name: str | None = None) -> list[str]:
        """按请求指定优先于默认档；未指定走默认档（行为不变）。"""
        name = profile_name or self.default_profile
        if name not in self._profiles:
            raise GroupsError(f"PROFILE_NOT_FOUND：{name!r} 配置档不存在")
        return list(self._profiles[name])

    def switch(self, profile_name: str) -> list[str]:
        """配置档可切换（切换后返回新档列表，不改默认档）。"""
        return self.resolve(profile_name)

    @property
    def names(self) -> list[str]:
        return list(self._profiles)

    def entries(self) -> dict[str, list[str]]:
        return copy.deepcopy(self._profiles)
