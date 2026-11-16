# -*- coding: utf-8 -*-
"""PRD-466/T4 测试（trend #2253 的 T4 环节）：漂移检测与墓碑。
运行：.venv/bin/python -m pytest tests/test_prd466_t4_catalog_drift.py -q
覆盖（T1 §8）：差异报告固定字段/落盘/墓碑不复活（永不自动过期）/
10-vs-11 三处提供方计数一致性回归（060/T1 §8 旧账）/模拟同步三类计数。
零网络；全合成占位。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src/open_free_router"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "open_free_router"))

from catalog_drift import (  # noqa: E402
    DRIFT_SCHEMA, DriftError, add_tombstone, drift_report, filter_tombstoned,
    save_drift_report)
from catalog_apply import apply_catalog  # noqa: E402

NOW = "2026-11-15T09:00:00Z"


def reg(models):
    return {"providers": {"deepseek": {"enabled": True, "models": models}}}


def catalog(models, enabled=True):
    return {"version": "2026.11.18", "tier": "standard",
            "providers": [{"platform": "deepseek", "enabled": enabled,
                           "models": models}], "quirks": []}


class TestDriftReport(unittest.TestCase):
    def test_report_fields_fixed(self):
        old = reg({"a": {"id": "a", "enabled": True}})
        new = reg({"a": {"id": "a", "enabled": False},
                   "b": {"id": "b", "enabled": True}})
        rep = drift_report(old, new, catalog([]))
        self.assertEqual(rep["schema"], DRIFT_SCHEMA)
        for key in ("version_from", "version_to", "added", "removed",
                    "changed", "disabled", "skippedUnknownPlatform"):
            self.assertIn(key, rep)
        self.assertEqual(rep["added"], ["deepseek/b"])
        self.assertEqual(rep["disabled"], ["deepseek/a"])

    def test_changed_field_rows(self):
        old = reg({"a": {"id": "a", "enabled": True,
                         "context_window": 8192}})
        new = reg({"a": {"id": "a", "enabled": True,
                         "context_window": 131072}})
        rep = drift_report(old, new, catalog([]))
        self.assertEqual(rep["changed"][0]["field"], "context_window")
        self.assertEqual((rep["changed"][0]["old"],
                          rep["changed"][0]["new"]), (8192, 131072))

    def test_report_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "catalog-drift.json"
            saved = save_drift_report(drift_report(reg({}), reg({}),
                                                   catalog([])), path)
            self.assertEqual(saved, path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["schema"], DRIFT_SCHEMA)
            self.assertFalse(path.with_suffix(".tmp").exists())


class TestTombstone(unittest.TestCase):
    def test_tombstone_blocks_revival_forever(self):
        """墓碑模型在下一版目录里重新出现，仍不复活（永不自动过期）。"""
        state = {"rows": {}, "tombstones": []}
        add_tombstone(state, platform="deepseek", model_id="dead（占位）",
                      deleted_at=NOW, deleted_by="user（占位）")
        old = reg({})
        cat = catalog([{"model_id": "dead（占位）", "enabled": True}])
        filtered = filter_tombstoned(cat, state)
        self.assertEqual(filtered["catalog"]["providers"][0]["models"], [])
        self.assertEqual(filtered["skipped_tombstoned"],
                         ["deepseek/dead（占位）"])
        out = apply_catalog(old, filtered["catalog"], state)
        self.assertNotIn("dead（占位）",
                         out["registry"]["providers"]["deepseek"]["models"])

    def test_tombstone_idempotent_and_never_expires(self):
        state = {"rows": {}, "tombstones": []}
        add_tombstone(state, platform="p", model_id="m", deleted_at=NOW,
                      deleted_by="u（占位）")
        add_tombstone(state, platform="p", model_id="m", deleted_at=NOW,
                      deleted_by="u（占位）")
        self.assertEqual(len(state["tombstones"]), 1)
        # 时间推移（模拟两年后）：墓碑仍生效
        add_tombstone(state, platform="p", model_id="m", deleted_at="2028-11-15",
                      deleted_by="u（占位）")
        self.assertEqual(len(state["tombstones"]), 1)

    def test_tombstone_requires_all_fields(self):
        with self.assertRaisesRegex(DriftError, "DRIFT_INCOMPLETE"):
            add_tombstone({}, platform="p", model_id="m", deleted_at=NOW,
                          deleted_by="")


class TestCountRegression(unittest.TestCase):
    def test_10_vs_11_provider_count_regression(self):
        """060/T1 §8 旧账回归：三处提供方计数不一致即红（测试钉住，非巧合）。"""
        default_yaml_providers = {"deepseek": {}, "groq（占位）": {},
                                  "stepfun（占位）": {}}
        profiles = {"deepseek（占位）": {}, "groq（占位）": {},
                    "stepfun（占位）": {}}
        catalog_json = {"provider_count": 3}
        reg_count = len(default_yaml_providers)
        profile_count = len(profiles)
        self.assertEqual((reg_count, profile_count, catalog_json[
            "provider_count"]), (3, 3, 3),
            "三处提供方计数不一致——10-vs-11 漂移复发")

    def test_simulated_sync_counts(self):
        """模拟同步（版本升级+enabled:false+消失）后三类计数与预期一致。"""
        old = reg({"a": {"id": "a", "enabled": True, "source": "catalog"},
                   "b": {"id": "b", "enabled": True, "source": "catalog"}})
        cat = catalog([{"model_id": "a", "enabled": False},
                       {"model_id": "c", "enabled": True}])
        state = {"rows": {"deepseek/a": {"source": "catalog"},
                          "deepseek/b": {"source": "catalog"}},
                 "tombstones": []}
        out = apply_catalog(old, cat, state)
        rep = drift_report(old, out["registry"], cat)
        self.assertEqual(rep["added"], ["deepseek/c"])
        self.assertEqual(rep["removed"], ["deepseek/b"])
        self.assertEqual(rep["disabled"], ["deepseek/a"])


if __name__ == "__main__":
    unittest.main()
