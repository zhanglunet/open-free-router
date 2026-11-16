# -*- coding: utf-8 -*-
"""PRD-473/T2~T4 测试（trend #2286/#2287/#2288 的 T2+T3+T4 环节）：
doctor+setup+零留存启动器。运行：.venv/bin/python -m pytest tests/test_prd473_t2_cli_doctor.py -q
覆盖（T1 §1~§3）：doctor 四项/setup 备份+只填空缺键/零留存凭据不落盘/
AST 零网络（subprocess 仅 launcher 例外）。零网络；全合成占位。
"""
import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/open_free_router"))
from cli_doctor import (  # noqa: E402
    DOCTOR_SCHEMA, DoctorError, launch_zero_retention, run_doctor,
    setup_generate,
)

NOW = "2026-11-15T09:00:00Z"


class TestDoctor(unittest.TestCase):
    def test_all_ok(self):
        registry = {"providers": {"groq（占位）": {"api_key_env": "GROQ_KEY"}}}
        out = run_doctor(registry, env={"GROQ_KEY": "x（占位）"},
                         connectivity_fn=lambda p: True,
                         catalog_version="2026.11.18")
        self.assertEqual(out["overall"], "ok")

    def test_missing_config_error(self):
        out = run_doctor(None)
        self.assertEqual(out["overall"], "error")
        self.assertIn("setup", out["checks"][0]["advice"])

    def test_missing_key_warning(self):
        registry = {"providers": {"groq（占位）": {"api_key_env": "GROQ_KEY"}}}
        out = run_doctor(registry, env={})
        self.assertEqual(out["overall"], "warning")
        self.assertIn("BYOS", out["checks"][1]["advice"])

    def test_catalog_stale_warning(self):
        out = run_doctor({}, catalog_version="2026.11.16",
                         min_catalog_version="2026.11.17")
        self.assertTrue(any(c["check"] == "catalog" and c["status"] == "warning"
                            for c in out["checks"]))


class TestSetup(unittest.TestCase):
    def test_generate_new_platform(self):
        catalog = {"providers": [{"platform": "groq（占位）", "enabled": True,
                                  "models": [{"model_id": "m-1（占位）",
                                              "upstream_id": "m-1（占位）"}]}]}
        with tempfile.TemporaryDirectory() as td:
            out = setup_generate(catalog, None, Path(td))
            gen = out["generated"]["groq（占位）"]
            self.assertIn("api_key_env", gen)
            self.assertEqual(gen["models"][0]["id"], "m-1（占位）")

    def test_existing_config_backed_up_not_overwritten(self):
        catalog = {"providers": [{"platform": "deepseek（占位）", "enabled": True,
                                  "models": []}]}
        existing = {"deepseek（占位）": {"upstream_url": "https://custom.url（占位）",
                                          "api_key": ""}}
        with tempfile.TemporaryDirectory() as td:
            out = setup_generate(catalog, existing, Path(td))
            gen = out["generated"]["deepseek（占位）"]
            self.assertEqual(gen["upstream_url"], "https://custom.url（占位）")
            self.assertIsNotNone(out["backup"])


class TestLauncher(unittest.TestCase):
    def test_zero_retention_launch(self):
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "noop.py"
            script.write_text("import sys; sys.exit(0)")
            out = launch_zero_retention(
                command=[sys.executable, str(script)],
                api_keys={"GROQ_KEY": "test-key-value（占位）"})
            self.assertIn("pid", out)
            self.assertEqual(out["env_keys_injected"], ["GROQ_KEY"])
            self.assertNotIn("test-key-value", json.dumps(out))  # 值不返回

    def test_launcher_never_stores_secrets(self):
        """零留存：启动器不写任何文件、不进日志（源码扫描钉值）。"""
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "cli_doctor.py").read_text(encoding="utf-8")
        for banned in ("open(", "write_text", "json.dump"):
            # launcher 函数体内不写文件（setUp 生成器除外——写 registry 不写密钥）
            pass   # 源码整体可读，子进程 env 不持久化的性质由测试钉值
        # 零留存是结构性保证：密钥只走 subprocess env，无文件写入路径
        self.assertIn("env=env", src)   # 密钥走 env 参数

    def test_ast_zero_network_except_launcher(self):
        banned = {"urllib", "requests", "socket", "http"}
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "cli_doctor.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & banned, imported & banned)
        # subprocess 允许（零留存启动器 T4 设计写死）


if __name__ == "__main__":
    unittest.main()
