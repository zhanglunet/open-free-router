# -*- coding: utf-8 -*-
"""PRD-470/T5 测试（trend #2274 的 T5 环节）：统一模型分组验收试运行。
运行：.venv/bin/python -m pytest tests/test_prd470_t5_model_groups_acceptance.py -q
验收（PRD 任务拆分 T5 行）：真实场景全链演练两遍逐字节一致——多提供方
分组 → 配置档 resolve → 按请求指定 → 切换 → 默认档不变 → 失败演练
（PROFILE_NOT_FOUND/未知提供方跳过）；链复跑 T2+T3/T4 子进程 exit 0；
文档无密钥。零网络；全合成占位。
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src/open_free_router"))
TESTS = Path(__file__).resolve().parent

from model_groups import (  # noqa: E402
    FallbackProfiles, merge_groups)

NOW = "2026-11-15T09:00:00Z"
PROVIDERS = {
    "groq": {"models": {
        "llama-3.3-70b": {"upstream_id": "llama-3.3-70b-versatile",
                          "enabled": True}}},
    "openrouter": {"models": {
        "llama-3.3-versatile": {"upstream_id": "llama-3.3-70b-versatile",
                                "enabled": True}}},
    "deepseek": {"models": {
        "deepseek-chat": {"upstream_id": "deepseek-chat", "enabled": True}}},
    "bigmodel（占位）": {"models": {
        "glm-5.2": {"upstream_id": "glm-5.2", "enabled": True}}},
}
PROFILES = {"coding": ["deepseek-chat", "llama-3.3-70b-versatile"],
            "vision": ["glm-5.2"],
            "default": ["deepseek-chat"]}


def full_chain():
    """全链：分组→配置档→按请求指定→切换→默认档不变。"""
    groups = merge_groups(PROVIDERS)
    fp = FallbackProfiles(PROFILES)
    default = fp.resolve()
    coding = fp.resolve("coding")
    vision = fp.switch("vision")
    default_again = fp.resolve()
    return {"groups": groups, "default": default, "coding": coding,
            "vision": vision, "default_again": default_again}


class TestFullChain(unittest.TestCase):
    def test_chain_twice_byte_identical(self):
        runs = [full_chain(), full_chain()]
        self.assertEqual(json.dumps(runs[0], sort_keys=True),
                         json.dumps(runs[1], sort_keys=True))

    def test_merged_groups(self):
        run = full_chain()
        groups = {g["model_id"]: g for g in run["groups"]["groups"]}
        self.assertEqual(len(groups["llama-3.3-70b-versatile"]["members"]), 2)
        self.assertEqual(groups["llama-3.3-70b-versatile"]["members"][0][
            "priority"], 1)

    def test_profile_switch_no_default_mutation(self):
        run = full_chain()
        self.assertEqual(run["default"], ["deepseek-chat"])
        self.assertEqual(run["default_again"], ["deepseek-chat"])
        self.assertEqual(run["coding"],
                         ["deepseek-chat", "llama-3.3-70b-versatile"])
        self.assertEqual(run["vision"], ["glm-5.2"])

    def test_no_duplicates(self):
        run = full_chain()
        ids = [g["model_id"] for g in run["groups"]["groups"]]
        self.assertEqual(len(ids), len(set(ids)))


class TestFailureDrills(unittest.TestCase):
    def test_profile_not_found(self):
        fp = FallbackProfiles(PROFILES)
        with self.assertRaises(Exception):
            fp.resolve("ghost（占位）")

    def test_unknown_platform_silent(self):
        extra = dict(PROVIDERS)
        extra["unknown（占位）"] = {"models": {
            "x": {"upstream_id": "x", "enabled": True}}}
        out = merge_groups(extra)   # 读操作不报错
        self.assertGreaterEqual(len(out["groups"]), 1)

    def test_chain_rerun_green(self):
        """链复跑：T2+T3/T4 测试子进程复跑 exit 0。"""
        for name in ("test_prd470_t2_model_groups.py",
                     "test_prd470_t4_model_groups_boundary.py"):
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "discover",
                 "-s", str(TESTS), "-p", name],
                capture_output=True, timeout=600)
            self.assertEqual(proc.returncode, 0,
                             f"{name} 复跑失败：{proc.stdout[-400:]}{proc.stderr[-400:]}")


class TestDocNoSecrets(unittest.TestCase):
    def test_t1_design_doc_no_secrets(self):
        """文档无密钥：T1 设计拼接 needle 扫描+借鉴声明（R5）。"""
        doc = next(REPO.glob("docs/plans/2026-11-17-model-groups-design.md"))
        text = doc.read_text(encoding="utf-8").lower()
        for needle in ("sk-" + "live", "passw" + "ord =",
                       "BEGIN PRIVATE", "AKIA" + "x"):
            self.assertNotIn(needle, text, f"设计文档出现密钥样式 {needle}")
        self.assertIn("只借鉴思路", text)


if __name__ == "__main__":
    unittest.main()
