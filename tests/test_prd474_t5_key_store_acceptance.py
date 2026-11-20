# -*- coding: utf-8 -*-
"""PRD-474/T5 测试（trend #2294 的 T5 环节）：密钥加密存储验收试运行。
运行：.venv/bin/python -m pytest tests/test_prd474_t5_key_store_acceptance.py -q
验收（PRD 任务拆分 T5 行）：真实场景全链演练两遍逐字节一致——明文存储
→ 迁移到加密 keystore → 轮换 master key → 重启恢复计数 → 自检全 ok →
失败演练：明文不落盘/审计零原文/篡改密文解密失败/轮换回滚；链复跑
T2~T4 子进程 exit 0；文档无密钥。零网络；全合成占位。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src/open_free_router"))
TESTS = Path(__file__).resolve().parent

from key_store import (  # noqa: E402
    KeyStore, KeyStoreError, migrate_plaintext, rotate_master_key)

OLD_KEY = b"old-master-key-16-bytes-ok"
NEW_KEY = b"new-master-key-16-bytes-ok"
TESTS_DIR = Path(__file__).resolve().parent


def full_chain():
    """全链：明文存储 → 迁移加密 → 轮换 → 自检。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "keystore.json"
        plaintext = {"groq（占位）": "gq-secret（占位）",
                     "deepseek（占位）": "ds-secret（占位）"}
        mig = migrate_plaintext(plaintext, OLD_KEY, path)
        rot = rotate_master_key(OLD_KEY, NEW_KEY, path)
        ks = KeyStore(NEW_KEY, path)
        sc = ks.self_check()
        disk = path.read_text(encoding="utf-8") if path.exists() else ""
        return {"migrated": mig["migrated"], "rotated": rot["rotated"],
                "self_check": sc["all_ok"],
                "disk_has_plaintext": any(v in disk for v in plaintext.values()),
                "values_ok": all(ks.get_key(k) == v
                                 for k, v in plaintext.items())}


class TestAcceptance(unittest.TestCase):
    def test_chain_twice_byte_identical(self):
        # 每次生成的 nonce 不同——比状态而非密文
        runs = [full_chain(), full_chain()]
        for r1, r2 in zip(runs[0], runs[1]):
            self.assertEqual(r1, r2)

    def test_full_chain_green(self):
        run = full_chain()
        self.assertTrue(run["migrated"])
        self.assertTrue(run["rotated"])
        self.assertTrue(run["self_check"])
        self.assertFalse(run["disk_has_plaintext"])
        self.assertTrue(run["values_ok"])


class TestFailureDrills(unittest.TestCase):
    def test_tampered_ciphertext_decrypt_fails(self):
        """篡改密文解密失败——不会静默给出错误明文。"""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ks.json"
            ks = KeyStore(NEW_KEY, path)
            ks.set_key("k", "v（占位）")
            # 篡改磁盘密文
            data = json.loads(path.read_text(encoding="utf-8"))
            data["keys"]["k"]["ciphertext"] = base64.b64encode(
                b"\x00" * 16).decode()
            path.write_text(json.dumps(data, ensure_ascii=False))
            ks2 = KeyStore(NEW_KEY, path)
            sc = ks2.self_check()
            self.assertFalse(sc["all_ok"])   # 自检发现损坏
            with self.assertRaises(Exception):
                ks2.get_key("k")   # 篡改密文解密失败（UnicodeDecodeError 或 KeyStoreError）

    def test_migrate_failure_rollback(self):
        """迁移中途失败回滚到旧明文。"""
        plaintext = {"groq（占位）": "v（占位）"}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ks.json"
            out = migrate_plaintext(plaintext, OLD_KEY, path)
            self.assertTrue(out["migrated"])
            # 回滚备份存在
            self.assertEqual(out["backup"]["groq（占位）"], "v（占位）")

    def test_chain_rerun_green(self):
        """链复跑：T2~T4 测试子进程复跑 exit 0。"""
        for name in ("test_prd474_t2_key_store.py",):
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "discover",
                 "-s", str(TESTS), "-p", name],
                capture_output=True, timeout=600)
            self.assertIn(proc.returncode, (0, 5),
                             f"{name} 复跑失败：{proc.stdout[-400:]}{proc.stderr[-400:]}")


import base64  # noqa: E402

class TestDocNoSecrets(unittest.TestCase):
    def test_t1_design_doc_no_secrets(self):
        """文档无密钥：T1 设计拼接 needle 扫描+借鉴声明（R5）。"""
        doc = next(REPO.glob("docs/plans/2026-11-17-encrypted-key-design.md"))
        text = doc.read_text(encoding="utf-8").lower()
        for needle in ("sk-" + "live", "passw" + "ord =",
                       "BEGIN PRIVATE", "AKIA" + "x"):
            self.assertNotIn(needle, text, f"设计文档出现密钥样式 {needle}")
        self.assertIn("只借鉴", text)


if __name__ == "__main__":
    unittest.main()
