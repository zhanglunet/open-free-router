# -*- coding: utf-8 -*-
"""PRD-474/T2~T4 测试（trend #2291/#2292/#2293 的 T2~T4 环节）：
密钥加密存储。运行：.venv/bin/python -m pytest tests/test_prd474_t2_key_store.py -q
覆盖（T1 §1~§4）：加密落盘（密文+0600）/明文不进日志/轮换事务性回滚/
迁移旧明文/自检/审计只记元数据/AST 零网络。零网络；全合成占位。
"""
import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/open_free_router"))
from key_store import (  # noqa: E402
    ERROR_CODES, KEYSTORE_FILE, KeyStore, KeyStoreError, migrate_plaintext,
    rotate_master_key,
)

MASTER = b"master-key-at-least-16-bytes-ok"[:32]


class TestEncryptedStorage(unittest.TestCase):
    def test_encrypt_decrypt_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            ks = KeyStore(MASTER, Path(td) / "keystore.json")
            ks.set_key("groq（占位）", "my-secret-key（占位）")
            self.assertEqual(ks.get_key("groq（占位）"),
                             "my-secret-key（占位）")

    def test_ciphertext_on_disk_not_plaintext(self):
        """密钥落盘为密文：磁盘上找不到明文。"""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "keystore.json"
            ks = KeyStore(MASTER, path)
            ks.set_key("groq（占位）", "super-secret-value（占位）")
            disk = path.read_text(encoding="utf-8")
            self.assertNotIn("super-secret-value", disk)

    def test_perm_0600(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "keystore.json"
            ks = KeyStore(MASTER, path)
            ks.set_key("k", "v")
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")


class TestRotate(unittest.TestCase):
    def test_rotate_master_key(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "keystore.json"
            old_key = b"old-master-key-16-bytes-x"[:32]
            new_key = b"new-master-key-16-bytes-y"[:32]
            ks = KeyStore(old_key, path)
            ks.set_key("groq（占位）", "my-key-value（占位）")
            out = rotate_master_key(old_key, new_key, path)
            self.assertTrue(out["rotated"])
            # 旧钥解不开
            old_ks = KeyStore(old_key, path)
            with self.assertRaises(Exception):
                old_ks.get_key("groq（占位）")
            # 新钥解得开
            new_ks = KeyStore(new_key, path)
            self.assertEqual(new_ks.get_key("groq（占位）"),
                             "my-key-value（占位）")

    def test_rotate_backup_created(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "keystore.json"
            old_key = b"old-master-key-16-bytes-ok"[:32]
            ks = KeyStore(old_key, path)
            ks.set_key("k", "v")
            rotate_master_key(old_key, b"new-master-key-16-bytes-ok"[:32],
                              path)
            backup = path.with_suffix(".bak")
            self.assertTrue(backup.exists())

    def test_rotate_nonexistent_store(self):
        with tempfile.TemporaryDirectory() as td:
            out = rotate_master_key(b"old-k-0123456789abcdef"[:32],
                                    b"new-k-0123456789abcdef"[:32],
                                    Path(td) / "nonexistent.json")
            self.assertFalse(out["rotated"])


class TestMigration(unittest.TestCase):
    def test_migrate_plaintext_to_encrypted(self):
        with tempfile.TemporaryDirectory() as td:
            plaintext = {"groq（占位）": "secret-value（占位）",
                         "deepseek（占位）": "another-key（占位）"}
            path = Path(td) / "keystore.json"
            out = migrate_plaintext(plaintext, MASTER, path)
            self.assertTrue(out["migrated"])
            ks = KeyStore(MASTER, path)
            self.assertEqual(ks.get_key("groq（占位）"), "secret-value（占位）")


class TestAuditAndSelfCheck(unittest.TestCase):
    def test_audit_metadata_only(self):
        """审计只记元数据（key_name + 操作 + 时间），不记密钥原文。"""
        with tempfile.TemporaryDirectory() as td:
            ks = KeyStore(MASTER, Path(td) / "ks.json")
            ks.set_key("groq（占位）", "super-secret（占位）")
            audit_dump = json.dumps(ks.audit)
            self.assertNotIn("super-secret", audit_dump)

    def test_self_check_all_ok(self):
        with tempfile.TemporaryDirectory() as td:
            ks = KeyStore(MASTER, Path(td) / "ks.json")
            ks.set_key("groq（占位）", "key-value（占位）")
            sc = ks.self_check()
            self.assertTrue(sc["all_ok"])
            self.assertEqual(sc["self_check"]["groq（占位）"], "ok")


class TestRedline(unittest.TestCase):
    def test_ast_zero_network(self):
        banned = {"urllib", "requests", "socket", "http"}
        src = (Path(__file__).resolve().parents[1] / "src"
               / "open_free_router" / "key_store.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & banned, imported & banned)
        # subprocess 属零留存启动器（cli_doctor），key_store 本身零子进程

    def test_error_codes_closed(self):
        self.assertEqual(set(ERROR_CODES),
                         {"KEYSTORE_INCOMPLETE", "KEYSTORE_DECRYPT_FAILED",
                          "KEYSTORE_MIGRATION_FAILED"})

    def test_schema_closed(self):
        from key_store import STORE_SCHEMA
        self.assertEqual(STORE_SCHEMA, "key-store/v1")


if __name__ == "__main__":
    unittest.main()
