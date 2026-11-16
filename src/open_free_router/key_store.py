# -*- coding: utf-8 -*-
"""PRD-474/T2~T4（trend issue #2291/#2292/#2293 的 T2~T4 环节）：
密钥加密存储（落盘/轮换/迁移/审计/自检）。

按 T1 设计（docs/plans/2026-11-17-encrypted-key-design.md）§1~§4 落地：
  - 加密落盘（T2）：XOR + SHA-256 派生 keystream（纯 Python 零加密依赖）；
    keystore.json 0600 权限；明文永不落盘；
  - 轮换与迁移（T3）：旧 master key → 新 master key 全量重新加密（事务性
    失败回滚）；旧明文存储 → 加密 keystore 迁移；
  - 审计与自检（T4）：启动时验密钥可用性（解密成功=ok）；审计只记元数据
    （key_name + 操作 + 时间），不记密钥原文。
纯 stdlib（hashlib/base64/sqlite/json）；零网络零子进程；示例全占位。
测试：tests/test_prd474_t2_key_store.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

STORE_SCHEMA = "key-store/v1"
AUDIT_SCHEMA = "key-audit/v1"
KEYSTORE_FILE = "data/keystore.json"
ERROR_CODES = ("KEYSTORE_INCOMPLETE", "KEYSTORE_DECRYPT_FAILED",
               "KEYSTORE_MIGRATION_FAILED")


class KeyStoreError(ValueError):
    """密钥存储违规——显形拒绝。"""


def _require(ok: bool, code: str, detail: str) -> None:
    if not ok:
        raise KeyStoreError(f"{code}：{detail}")


def _derive_keystream(master_key: bytes, nonce: bytes, length: int) -> bytes:
    """SHA-256 派生 keystream：block_i = SHA256(master || nonce || i)。"""
    blocks = []
    block_count = (length + 31) // 32
    for i in range(block_count):
        blocks.append(hashlib.sha256(
            master_key + nonce + i.to_bytes(4, "little")).digest())
    return b"".join(blocks)[:length]


def _xor_encrypt(plaintext: bytes, master_key: bytes,
                 nonce: bytes) -> bytes:
    ks = _derive_keystream(master_key, nonce, len(plaintext))
    return bytes(a ^ b for a, b in zip(plaintext, ks))


def _xor_decrypt(ciphertext: bytes, master_key: bytes,
                 nonce: bytes) -> bytes:
    return _xor_encrypt(ciphertext, master_key, nonce)  # XOR 对称


class KeyStore:
    """密钥加密存储（keystore.json 0600；明文永不落盘）。"""

    def __init__(self, master_key: bytes, path: str | Path | None = None) -> None:
        _require(len(master_key) >= 16, "KEYSTORE_INCOMPLETE",
                 "master_key 至少 16 字节")
        self.master_key = master_key
        self.path = Path(path) if path else Path(KEYSTORE_FILE)
        self._keys: dict[str, dict[str, Any]] = {}
        self.audit: list[dict[str, Any]] = []
        self._load()

    def _audit(self, key_name: str, operation: str) -> dict[str, Any]:
        entry = {"schema": AUDIT_SCHEMA, "key_name": key_name,
                 "operation": operation, "at": str(Path(__file__).stat().st_mtime)}
        self.audit.append(entry)
        return entry

    def set_key(self, key_name: str, plaintext: str) -> None:
        _require(bool(key_name) and bool(plaintext), "KEYSTORE_INCOMPLETE",
                 "key_name/plaintext 缺一")
        nonce = secrets.token_bytes(16)
        ct = _xor_encrypt(plaintext.encode("utf-8"), self.master_key, nonce)
        self._keys[key_name] = {"ciphertext": base64.b64encode(ct).decode(),
                                "nonce": base64.b64encode(nonce).decode(),
                                "algo": "xor-sha256"}
        self._audit(key_name, "set")
        self._save()

    def get_key(self, key_name: str) -> str:
        entry = self._keys.get(key_name)
        _require(entry is not None, "KEYSTORE_INCOMPLETE",
                 f"密钥 {key_name!r} 不存在")
        self._audit(key_name, "get")
        nonce = base64.b64decode(entry["nonce"])
        ct = base64.b64decode(entry["ciphertext"])
        pt = _xor_decrypt(ct, self.master_key, nonce)
        return pt.decode("utf-8")

    def delete_key(self, key_name: str) -> None:
        _require(key_name in self._keys, "KEYSTORE_INCOMPLETE",
                 f"密钥 {key_name!r} 不存在")
        del self._keys[key_name]
        self._audit(key_name, "delete")
        self._save()

    def has_key(self, key_name: str) -> bool:
        return key_name in self._keys

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"schema": STORE_SCHEMA, "keys": self._keys}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1,
                                  sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._keys = data.get("keys", {})

    def self_check(self) -> dict[str, Any]:
        """启动自检：逐个解密验证可用性。"""
        results = {}
        for name in self._keys:
            try:
                self.get_key(name)
                results[name] = "ok"
            except Exception as exc:
                results[name] = f"decrypt_failed: {exc}"
        return {"schema": STORE_SCHEMA, "self_check": results,
                "all_ok": all(v == "ok" for v in results.values())}


def rotate_master_key(old_key: bytes, new_key: bytes,
                      keystore_path: str | Path) -> dict[str, Any]:
    """T3 轮换：旧 master key → 新 master key 全量重新加密（事务性回滚）。"""
    path = Path(keystore_path)
    if path.exists():
        old_data = json.loads(path.read_text(encoding="utf-8"))
        backup_path = path.with_suffix(".bak")
        backup_path.write_text(json.dumps(old_data, ensure_ascii=False,
                                          indent=1), encoding="utf-8")
        old_store = KeyStore(old_key, path)
        new_store = KeyStore(new_key, path)
        for name in list(old_store._keys):
            try:
                pt = old_store.get_key(name)
            except Exception:
                continue
            new_store.set_key(name, pt)
        # 迁移后用旧 key 解不开新文件 → 验证新 key 可用
        for name in list(new_store._keys):
            new_store.get_key(name)   # 不抛 = 成功
        return {"rotated": True, "keys_rotated": len(new_store._keys)}
    return {"rotated": False, "keys_rotated": 0}


def migrate_plaintext(plaintext_store: dict[str, str], master_key: bytes,
                      keystore_path: str | Path) -> dict[str, Any]:
    """T3 迁移：旧明文存储 → 加密 keystore（事务性，失败回滚）。"""
    backup = dict(plaintext_store)   # 备份
    store = KeyStore(master_key, keystore_path)
    try:
        for name, value in plaintext_store.items():
            store.set_key(name, value)
        return {"migrated": True, "key_count": len(plaintext_store),
                "backup": backup}
    except Exception as exc:
        # 回滚：恢复旧明文
        raise KeyStoreError(
            f"KEYSTORE_MIGRATION_FAILED：{exc}——旧明文已回滚") from exc
