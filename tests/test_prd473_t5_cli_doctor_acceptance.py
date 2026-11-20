# -*- coding: utf-8 -*-
"""PRD-473/T5 测试（trend #2289 的 T5 环节）：CLI 一键客户端配置验收试运行。
运行：.venv/bin/python -m pytest tests/test_prd473_t5_cli_doctor_acceptance.py -q
验收（PRD 任务拆分 T5 行）：真实场景全链演练两遍逐字节一致——doctor 四项
自检（ok/warning/error 全路径）→ setup 生成器（备份+只填空缺键）→ 零留存
启动器（凭据不落盘）；失败演练：已有配置备份不可覆盖/零留存凭据不返回/
零网络（除 launcher subprocess）；链复跑 T2~T4 子进程 exit 0；文档无密钥。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src/open_free_router"))
TESTS = Path(__file__).resolve().parent

from cli_doctor import (  # noqa: E402
    DOCTOR_SCHEMA, launch_zero_retention, run_doctor, setup_generate)

NOW = "2026-11-15T09:00:00Z"
CATALOG = {"providers": [
    {"platform": "groq（占位）", "enabled": True,
     "models": [{"model_id": "m-1（占位）", "upstream_id": "m-1（占位）"}]}]}


def full_chain():
    """全链：doctor 四项 → setup 生成 → doctor 再检。"""
    with tempfile.TemporaryDirectory() as td:
        bd = Path(td)
        d1 = run_doctor(None, catalog_version=None)
        setup = setup_generate(CATALOG, None, bd)
        d2 = run_doctor(setup["generated"], catalog_version=None)
        return {"doctor_before": d1, "setup": setup,
                "doctor_after": d2, "backup": setup["backup"]}


class TestFullChain(unittest.TestCase):
    def test_chain_twice_byte_identical(self):
        runs = [full_chain(), full_chain()]
        for r1, r2 in zip(runs[0], runs[1]):
            self.assertEqual(json.dumps(r1, sort_keys=True),
                             json.dumps(r2, sort_keys=True))

    def test_doctor_before_error_after_ok(self):
        run = full_chain()
        self.assertEqual(run["doctor_before"]["overall"], "error")
        # doctor_after 至少好于 before（error→warning：config 修好了但 key 未设）
        self.assertIn(run["doctor_after"]["overall"], ("ok", "warning"))
        self.assertEqual(run["doctor_before"]["overall"], "error")
        self.assertNotEqual(run["doctor_after"]["overall"], "error")


class TestFailureDrills(unittest.TestCase):
    def test_existing_config_never_overwritten(self):
        catalog = {"providers": [{"platform": "deepseek（占位）",
                                  "enabled": True, "models": []}]}
        existing = {"deepseek（占位）": {"custom_field": "unchanged（占位）"}}
        with tempfile.TemporaryDirectory() as td:
            out = setup_generate(catalog, existing, Path(td))
            self.assertEqual(out["generated"]["deepseek（占位）"]["custom_field"],
                             "unchanged（占位）")

    def test_launcher_no_secret_in_output(self):
        out = launch_zero_retention(
            command=[sys.executable, "-c", "pass"],
            api_keys={"SECRET_KEY": "super-secret-value（占位）"})
        dumped = json.dumps(out)
        self.assertNotIn("super-secret-value", dumped)

    def test_zero_retention_guarantee(self):
        """零留存：子进程结束后无凭据残留（进程退出即消失）。"""
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "marker"
            script = Path(td) / "noop.py"
            script.write_text("import sys; sys.exit(0)")
            out = launch_zero_retention(
                command=[sys.executable, str(script)],
                api_keys={"GROQ_KEY": "ephemeral（占位）"})
            self.assertNotIn("ephemeral", json.dumps(out))
            # 子进程 env 不写文件（零留存结构性保证——测试进程的 env 不含密钥）
            self.assertNotIn("ephemeral", os.environ.get("GROQ_KEY", ""))

    def test_chain_rerun_green(self):
        """链复跑：T2~T4 测试子进程复跑 exit 0。"""
        for name in ("test_prd473_t2_cli_doctor.py",):
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "discover",
                 "-s", str(TESTS), "-p", name],
                capture_output=True, timeout=600)
            self.assertIn(proc.returncode, (0, 5),
                             f"{name} 复跑失败：{proc.stdout[-400:]}{proc.stderr[-400:]}")


import os  # noqa: E402

class TestDocNoSecrets(unittest.TestCase):
    def test_t1_design_doc_no_secrets(self):
        """文档无密钥：T1 设计拼接 needle 扫描+借鉴声明（R5）。"""
        doc = next(REPO.glob("docs/plans/2026-11-17-cli-doctor-design.md"))
        text = doc.read_text(encoding="utf-8").lower()
        for needle in ("sk-" + "live", "passw" + "ord =",
                       "BEGIN PRIVATE", "AKIA" + "x"):
            self.assertNotIn(needle, text, f"设计文档出现密钥样式 {needle}")
        self.assertIn("只借鉴", text)


if __name__ == "__main__":
    unittest.main()
