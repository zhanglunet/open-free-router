# -*- coding: utf-8 -*-
"""PRD-466/T3 测试（trend #2252 的 T3 环节）：事务应用与覆盖规则。
运行：.venv/bin/python -m pytest tests/test_prd466_t3_catalog_apply.py -q
覆盖（T1 §5 覆盖规则表逐行）：用户行赢/强制下线/不擅自重开/消失只删
catalog 行/墓碑不复活/新提供方跳过/quirks 整批/整份校验拒绝（原子）。
零网络；全合成占位。
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/open_free_router"))
from catalog_apply import APPLY_SCHEMA, ApplyError, apply_catalog  # noqa: E402

PROV = {"deepseek": {"enabled": True,
                     "models": {"deepseek-chat": {
                         "id": "deepseek-chat", "enabled": True,
                         "source": "baseline"}}}}


def catalog(models, enabled=True, platform="deepseek"):
    return {"version": "2026.11.18", "tier": "standard",
            "providers": [{"platform": platform, "enabled": enabled,
                           "models": models}], "quirks": []}


def reg(models=None, platform="deepseek", enabled=True):
    return {"providers": {platform: {"enabled": enabled,
                                     "models": models or {}}}}


class TestOverwriteRules(unittest.TestCase):
    def test_catalog_metadata_follows_catalog(self):
        rows = {"deepseek/deepseek-chat": {"source": "catalog"}}
        models = {"deepseek-chat": {"id": "deepseek-chat", "enabled": True,
                                    "source": "catalog",
                                    "context_window": 8192}}
        out = apply_catalog(reg(models), catalog([
            {"model_id": "deepseek-chat", "enabled": True,
             "context_window": 131072}]), {"rows": rows, "tombstones": []})
        m = out["registry"]["providers"]["deepseek"]["models"]["deepseek-chat"]
        self.assertEqual(m["context_window"], 131072)
        self.assertEqual(out["changes"]["updated"], 1)

    def test_enabled_false_forces_offline_any_source(self):
        models = {"deepseek-chat": {"id": "deepseek-chat", "enabled": True,
                                    "source": "user"}}
        out = apply_catalog(reg(models), catalog([
            {"model_id": "deepseek-chat", "enabled": False}]),
            {"rows": {}, "tombstones": []})
        m = out["registry"]["providers"]["deepseek"]["models"]["deepseek-chat"]
        self.assertFalse(m["enabled"])
        self.assertEqual(out["changes"]["disabled"], 1)

    def test_never_reenable_user_disabled(self):
        models = {"deepseek-chat": {"id": "deepseek-chat", "enabled": False,
                                    "source": "catalog"}}
        out = apply_catalog(reg(models), catalog([
            {"model_id": "deepseek-chat", "enabled": True}]),
            {"rows": {}, "tombstones": []})
        self.assertFalse(out["registry"]["providers"]["deepseek"]
                         ["models"]["deepseek-chat"]["enabled"])
        self.assertEqual(out["changes"]["disabled"], 0)

    def test_user_row_never_absorbed(self):
        rows = {"deepseek/deepseek-chat": {"source": "user"}}
        models = {"deepseek-chat": {"id": "deepseek-chat", "enabled": True,
                                    "source": "user",
                                    "context_window": 4096}}
        out = apply_catalog(reg(models), catalog([
            {"model_id": "deepseek-chat", "enabled": True,
             "context_window": 131072}]), {"rows": rows, "tombstones": []})
        m = out["registry"]["providers"]["deepseek"]["models"]["deepseek-chat"]
        self.assertEqual(m["context_window"], 4096)   # 用户行赢
        self.assertEqual(out["changes"]["updated"], 0)

    def test_user_row_survives_catalog_disappearance(self):
        rows = {"deepseek/deepseek-chat": {"source": "user"}}
        models = {"deepseek-chat": {"id": "deepseek-chat", "enabled": True,
                                    "source": "user"}}
        out = apply_catalog(reg(models), catalog([]),
                            {"rows": rows, "tombstones": []})
        self.assertIn("deepseek-chat",
                      out["registry"]["providers"]["deepseek"]["models"])
        self.assertEqual(out["changes"]["removed"], 0)

    def test_catalog_row_removed_when_absent(self):
        rows = {"deepseek/deepseek-chat": {"source": "catalog"}}
        models = {"deepseek-chat": {"id": "deepseek-chat", "enabled": True,
                                    "source": "catalog"}}
        out = apply_catalog(reg(models), catalog([]),
                            {"rows": rows, "tombstones": []})
        self.assertNotIn("deepseek-chat",
                         out["registry"]["providers"]["deepseek"]["models"])
        self.assertEqual(out["changes"]["removed"], 1)

    def test_tombstone_blocks_revival(self):
        state = {"rows": {}, "tombstones": [
            {"platform": "deepseek", "model_id": "dead-model",
             "deleted_at": "2026-11-15T09:00:00Z",
             "deleted_by": "user（占位）"}]}
        out = apply_catalog(reg({}), catalog([
            {"model_id": "dead-model", "enabled": True}]), state)
        self.assertNotIn("dead-model",
                         out["registry"]["providers"]["deepseek"]["models"])
        self.assertEqual(out["changes"]["tombstoned"], 1)

    def test_unknown_platform_skipped(self):
        out = apply_catalog(reg({}), catalog([
            {"model_id": "x", "enabled": True}], platform="new-provider（占位）"),
            {"rows": {}, "tombstones": []})
        self.assertEqual(out["changes"]["skippedUnknownPlatform"],
                         ["new-provider（占位）"])
        self.assertEqual(out["changes"]["inserted"], 0)

    def test_provider_wide_disable(self):
        models = {"deepseek-chat": {"id": "deepseek-chat", "enabled": True,
                                    "source": "catalog"}}
        out = apply_catalog(reg(models), catalog([
            {"model_id": "deepseek-chat", "enabled": True}], enabled=False),
            {"rows": {}, "tombstones": []})
        self.assertFalse(out["registry"]["providers"]["deepseek"]["models"]
                         ["deepseek-chat"]["enabled"])
        self.assertEqual(out["changes"]["disabled"], 1)

    def test_new_model_inserted(self):
        out = apply_catalog(reg({}), catalog([
            {"model_id": "new-m（占位）", "enabled": True}]),
            {"rows": {}, "tombstones": []})
        self.assertIn("new-m（占位）",
                      out["registry"]["providers"]["deepseek"]["models"])
        self.assertEqual(out["changes"]["inserted"], 1)

    def test_quirks_replaced_wholesale(self):
        state = {"rows": {}, "tombstones": [], "quirks": [{"slug": "old（占位）"}]}
        out = apply_catalog(reg({}), catalog([]), state)
        self.assertEqual(out["state"]["quirks"], [])
        self.assertEqual(out["changes"]["quirks"], 0)

    def test_atomic_reject_on_validation_failure(self):
        with self.assertRaisesRegex(ApplyError, "APPLY_VALIDATION_REJECTED"):
            apply_catalog(reg(), catalog([]), {"rows": {}, "tombstones": []},
                          validate_fn=lambda reg: False)

    def test_source_vocab_enforced(self):
        models = {"deepseek-chat": {"id": "deepseek-chat", "enabled": True,
                                    "source": "ghost"}}
        with self.assertRaisesRegex(ApplyError, "APPLY_INCOMPLETE"):
            apply_catalog(reg(models), catalog([]), {"rows": {},
                                                     "tombstones": []})

    def test_schema(self):
        out = apply_catalog(reg({}), catalog([]), {"rows": {},
                                                   "tombstones": []})
        self.assertEqual(out["schema"], APPLY_SCHEMA)
        self.assertTrue(out["applied"])


if __name__ == "__main__":
    unittest.main()
