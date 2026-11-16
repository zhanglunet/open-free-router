# -*- coding: utf-8 -*-
"""PRD-466/T4（trend issue #2253 的 T4 环节）：漂移检测与模型下线墓碑。

按 T1 设计 §8 落地：
  - 差异报告（格式固定）：version_from / version_to / added[] /
    removed[] / changed[{platform, model_id, field, old, new}] /
    disabled[] / skippedUnknownPlatform[]；报告落盘并记入同步日志；
  - 墓碑：用户删掉的模型行进墓碑表（platform / model_id / deleted_at /
    deleted_by），墓碑命中时目录同名条目被跳过并计入报告；复活只能人工
    删墓碑——墓碑永不自动过期；
  - 10-vs-11 回归锚点（060/T1 §8 旧账）：三处提供方计数一致性测试由
    test_prd466_t4_catalog_drift.py 承载（registry.default.yaml ==
    provider-profiles.json == catalog.json 的 provider_count）；
    10-vs-11 的守卫不是巧合而是测试钉住。
零 LLM 零网络零子进程；示例全虚构占位。
测试：tests/test_prd466_t4_catalog_drift.py
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

DRIFT_SCHEMA = "catalog-drift/v1"
ERROR_CODES = ("DRIFT_INCOMPLETE",)
TOMBSTONE_SCHEMA = "catalog-tombstone/v1"


class DriftError(ValueError):
    """漂移报告违规——显形拒绝。"""


def _model_index(registry: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for platform, provider in (registry.get("providers") or {}).items():
        for model_id, row in (provider.get("models") or {}).items():
            index[(platform, model_id)] = row
    return index


def drift_report(old_registry: dict[str, Any], new_registry: dict[str, Any],
                 catalog: dict[str, Any] | None = None,
                 skipped_unknown: list[str] | None = None) -> dict[str, Any]:
    """§8 差异报告：version_from/to + added/removed/changed/disabled。"""
    old_index = _model_index(old_registry)
    new_index = _model_index(new_registry)
    added = sorted(f"{p}/{m}" for p, m in new_index if (p, m) not in old_index)
    removed = sorted(f"{p}/{m}" for p, m in old_index if (p, m) not in new_index)
    changed = []
    disabled = []
    for key in sorted(set(old_index) & set(new_index)):
        old_row, new_row = old_index[key], new_index[key]
        platform, model_id = key
        if old_row.get("enabled", True) and not new_row.get("enabled", True):
            disabled.append(f"{platform}/{model_id}")
        for field in ("context_window", "reasoning", "tool_calling",
                      "supports_vision", "supports_tools", "limits"):
            if old_row.get(field) != new_row.get(field) and \
                    (field in old_row or field in new_row):
                changed.append({"platform": platform, "model_id": model_id,
                                "field": field, "old": old_row.get(field),
                                "new": new_row.get(field)})
    report = {"schema": DRIFT_SCHEMA,
              "version_from": old_registry.get("applied_version", "baseline"),
              "version_to": (catalog or {}).get("version", "baseline"),
              "added": added, "removed": removed, "changed": changed,
              "disabled": disabled,
              "skippedUnknownPlatform": list(skipped_unknown or [])}
    return report


def save_drift_report(report: dict[str, Any], path: str | Path) -> Path:
    """报告落盘（路径 T4 定死：data/catalog-drift.json）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True,
                              indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def add_tombstone(state: dict[str, Any], *, platform: str, model_id: str,
                  deleted_at: str, deleted_by: str) -> dict[str, Any]:
    """用户删掉的行进墓碑：永不自动过期，复活只能人工删墓碑。"""
    if not (platform and model_id and deleted_at and deleted_by):
        raise DriftError("DRIFT_INCOMPLETE：墓碑四字段缺一")
    for t in state.get("tombstones", []):
        if t["platform"] == platform and t["model_id"] == model_id:
            return copy.deepcopy(t)          # 幂等：已存在不重复
    tomb = {"schema": TOMBSTONE_SCHEMA, "platform": platform,
            "model_id": model_id, "deleted_at": deleted_at,
            "deleted_by": deleted_by}
    state.setdefault("tombstones", []).append(tomb)
    return copy.deepcopy(tomb)


def filter_tombstoned(catalog: dict[str, Any],
                      state: dict[str, Any]) -> dict[str, Any]:
    """墓碑命中的目录条目被跳过（apply 前的过滤层）；墓碑永不自动过期。"""
    tombstones = [(t["platform"], t["model_id"])
                  for t in state.get("tombstones", [])]
    filtered = copy.deepcopy(catalog)
    skipped = []
    for provider in filtered.get("providers", []):
        kept = []
        for m in provider.get("models", []):
            if (provider["platform"], m["model_id"]) in tombstones:
                skipped.append(f"{provider['platform']}/{m['model_id']}")
                continue
            kept.append(m)
        provider["models"] = kept
    return {"catalog": filtered, "skipped_tombstoned": skipped}
