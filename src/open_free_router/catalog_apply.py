# -*- coding: utf-8 -*-
"""PRD-466/T3（trend issue #2252 的 T3 环节）：事务应用与覆盖规则。

按 T1 设计（docs/plans/2026-11-17-signed-catalog-design.md）§5 覆盖规则表
落地——核心原则「目录是建议，用户是主人」。操作对象是 registry 的
dict 形状（providers 按 platform 键控，models 按 model_id 键控，来源
标记 source ∈ catalog/user/baseline 存在状态文件，不改 registry.yaml
文件格式）：

  | 情形 | 规则 |
  |---|---|
  | 目录元数据 | 跟目录走，除非该行被用户显式覆盖过（override 钉住） |
  | enabled:false | 强制下线（无论来源）——死模型不许继续被路由 |
  | enabled:true | 绝不重新启用用户关掉的行 |
  | source=user | 永不更新、永不删除、永不被目录收编（撞名用户行赢） |
  | 用户删掉的行 | 进墓碑（T4 完整实现；T3 先按 state.tombstones 生效） |
  | 目录消失 | 只删 source=catalog 行；user/baseline 不动 |
  | 新提供方 | 整条跳过 + skippedUnknownPlatform 计数 |
  | quirks | 每次同步整批替换 |

事务性（§7）：apply_catalog 产出的整份新 registry 先经 validate_fn 校验，
校验不过 → 整份不生效（原子语义「要么整份生效要么纹丝不动」由调用方
临时文件+rename 完成，本模块返回完整新 registry 供其落盘）。
零 LLM 零网络零子进程；示例全虚构占位。
测试：tests/test_prd466_t3_catalog_apply.py
"""
from __future__ import annotations

import copy
from typing import Any

APPLY_SCHEMA = "catalog-apply/v1"
ERROR_CODES = ("APPLY_INCOMPLETE", "APPLY_VALIDATION_REJECTED")

_SOURCES = ("catalog", "user", "baseline")


def _require(ok: bool, code: str, detail: str) -> None:
    if not ok:
        raise ApplyError(f"{code}：{detail}")


class ApplyError(ValueError):
    """应用违规——显形拒绝。"""


def _ensure_source(rows: dict[str, Any]) -> None:
    for model_id, row in rows.items():
        _require(row.get("source", "baseline") in _SOURCES,
                 "APPLY_INCOMPLETE", f"{model_id} 来源标记词表外")


def apply_catalog(registry: dict[str, Any], catalog: dict[str, Any],
                  state: dict[str, Any], *,
                  validate_fn: Any = None) -> dict[str, Any]:
    """§5 覆盖规则整份应用：返回 {registry, changes, applied}。

    registry：dict 形状 {providers: {platform: {..., models: {mid: row}}}}；
    catalog：T2 验签通过的目录文档；
    state：状态文件（含 rows.source 覆盖标记与 tombstones）；
    validate_fn：调用方注入的整份校验（返回 False → 整份拒绝）。
    """
    _require(isinstance(registry.get("providers"), dict),
             "APPLY_INCOMPLETE", "registry.providers 缺失")
    _require(isinstance(catalog.get("providers"), list),
             "APPLY_INCOMPLETE", "catalog.providers 缺失")
    _require(isinstance(state.get("rows", {}), dict) and
             isinstance(state.get("tombstones", []), list),
             "APPLY_INCOMPLETE", "state.rows/tombstones 缺失")

    for provider in registry.get("providers", {}).values():
        _ensure_source(provider.get("models", {}))   # 来源词表全表前置校验
    rows: dict[str, dict[str, Any]] = copy.deepcopy(state["rows"])
    tombstones = copy.deepcopy(state["tombstones"])
    new_registry = copy.deepcopy(registry)
    providers = new_registry["providers"]
    changes = {"updated": 0, "inserted": 0, "removed": 0,
               "disabled": 0, "skippedUnknownPlatform": [], "quirks": 0,
               "tombstoned": 0}

    for entry in catalog["providers"]:
        platform = entry["platform"]
        if platform not in providers:
            changes["skippedUnknownPlatform"].append(platform)
            continue
        provider = providers[platform]
        models = provider.setdefault("models", {})
        catalog_enabled = entry.get("enabled", True)
        for m in entry["models"]:
            mid = m["model_id"]
            key = f"{platform}/{mid}"
            if any(t["platform"] == platform and t["model_id"] == mid
                   for t in tombstones):
                changes["tombstoned"] += 1        # 墓碑命中：不复活
                continue
            if mid not in models:
                if not catalog_enabled:
                    continue                       # 下线的新模型不入表
                models[mid] = dict(m, source="catalog")
                rows[key] = {"source": "catalog"}
                changes["inserted"] += 1
                continue
            row = models[mid]
            # 强制下线（§5：无论来源，优先于「用户行赢」）——死模型不许继续被路由
            if not catalog_enabled or not m.get("enabled", True):
                if row.get("enabled", True):
                    row["enabled"] = False
                    changes["disabled"] += 1
                continue
            src = row.get("source", "baseline")
            if src == "user":
                continue                            # 用户行赢，永不收编
            if not m.get("enabled", True) or not row.get("enabled", True):
                continue                            # 用户关的不擅自打开
            # 元数据跟目录走（source=catalog 的行）
            if src != "catalog":
                continue
            for field in ("context_window", "reasoning", "tool_calling",
                          "supports_vision", "supports_tools", "limits"):
                if field in m and row.get(field) != m[field]:
                    row[field] = copy.deepcopy(m[field])
                    changes["updated"] += 1

        # 整家强制下线
        if not catalog_enabled:
            for mid in list(models):
                if models[mid].get("enabled", True):
                    models[mid]["enabled"] = False
                    changes["disabled"] += 1

    # 目录里消失的模型：只删 source=catalog 行
    catalog_ids = {(e["platform"], m["model_id"])
                   for e in catalog["providers"]          # 整家下线也算「还在目录里」
                   for m in e["models"]}
    for platform, provider in providers.items():
        for mid in list(provider.get("models", {})):
            key = f"{platform}/{mid}"
            if (platform, mid) in catalog_ids:
                continue
            src = provider["models"][mid].get("source", "baseline")
            if src == "user":
                continue
            if src == "catalog":
                del provider["models"][mid]
                changes["removed"] += 1

    # quirks 整批替换（状态文件承载，registry 不动）
    state["quirks"] = copy.deepcopy(catalog.get("quirks", []))
    changes["quirks"] = len(catalog.get("quirks", []))
    state["rows"] = rows
    state["tombstones"] = tombstones

    if validate_fn is not None and not validate_fn(new_registry):
        raise ApplyError("APPLY_VALIDATION_REJECTED：整份校验不过——"
                         "整份不生效（原子语义）")
    return {"schema": APPLY_SCHEMA, "registry": new_registry,
            "changes": changes, "applied": True, "state": state}
