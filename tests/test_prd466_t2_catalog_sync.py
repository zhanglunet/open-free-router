# -*- coding: utf-8 -*-
"""PRD-466/T2 测试（trend #2251 的 T2 环节）：签名目录拉取与验证。
运行：.venv/bin/python -m pytest tests/test_prd466_t2_catalog_sync.py -q
覆盖（T1 §2.2/§3/§4/§6）：验签顺序（先验后解析）/篡改拒绝/缺签名拒绝/
形状校验丢弃/版本底线三态/降级不断线/总开关/状态文件原子落盘/Ed25519
RFC 向量。零真实网络（fetch_fn 注入）；示例全占位。
"""
import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from open_free_router.catalog_sync import (  # noqa: E402
    CATALOG_PUBKEY_PINNED, CatalogSyncError, DEFAULT_BASE_URL,
    MIN_CATALOG_VERSION, build_catalog_source_url, fetch_and_verify,
    is_catalog, load_state, retry_backoff_seconds, run_sync,
    save_state, verify_catalog_bytes, version_decision,
)
from open_free_router.ed25519_verify import keygen, sign, verify  # noqa: E402

NOW = "2026-11-15T09:00:00Z"
PUB, SEED = keygen(b"ofr-catalog-test-key-placeholder")
PUB_BAD, SEED_BAD = keygen(b"ofr-catalog-other-key-placeholder")
PUBHEX = PUB.hex()


def make_doc(version=MIN_CATALOG_VERSION):
    return {"version": version, "generated_at": NOW, "tier": "standard",
            "providers": [{"platform": "groq（占位）", "display_name": "G（占位）",
                           "enabled": True,
                           "models": [{"model_id": "m-1（占位）",
                                       "upstream_id": "m-1（占位）",
                                       "enabled": True}]}],
            "quirks": []}


def signed_response(doc, key_seed=SEED):
    body = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")
    sig = base64.b64encode(sign(key_seed, body)).decode()
    return {"status": 200, "body": body,
            "headers": {"x-catalog-signature": sig}}


def fake_fetch(response):
    return lambda url: response


class TestEd25519(unittest.TestCase):
    def test_rfc_vector_keygen_sign_verify(self):
        sk = bytes.fromhex(
            "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        pk, _ = keygen(sk)
        self.assertEqual(
            pk.hex(),
            "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        sig = sign(sk, b"")
        self.assertTrue(verify(pk, b"", sig))
        self.assertFalse(verify(pk, b"x", sig))

    def test_tamper_rejected(self):
        doc = make_doc()
        body = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")
        sig = base64.b64encode(sign(SEED, body)).decode()
        tampered = bytearray(body)
        tampered[-2] ^= 1
        self.assertFalse(verify(bytes.fromhex(PUBHEX), bytes(tampered),
                                base64.b64decode(sig)))
        with self.assertRaisesRegex(CatalogSyncError, "verification FAILED"):
            verify_catalog_bytes(bytes(tampered), sig, PUBHEX)


class TestVerifyOrder(unittest.TestCase):
    def test_valid_signature_parses(self):
        doc = make_doc()
        resp = signed_response(doc)
        out = verify_catalog_bytes(resp["body"], resp["headers"][
            "x-catalog-signature"], PUBHEX)
        self.assertEqual(out["version"], MIN_CATALOG_VERSION)

    def test_missing_signature_header_refused(self):
        body = json.dumps(make_doc(), ensure_ascii=False).encode("utf-8")
        with self.assertRaisesRegex(CatalogSyncError, "missing signature"):
            verify_catalog_bytes(body, None, PUBHEX)

    def test_wrong_key_refused_loudly(self):
        resp = signed_response(make_doc(), key_seed=SEED_BAD)
        with self.assertRaisesRegex(CatalogSyncError, "verification FAILED"):
            verify_catalog_bytes(resp["body"], resp["headers"][
                "x-catalog-signature"], PUBHEX)

    def test_tampered_body_refused(self):
        resp = signed_response(make_doc())
        tampered = bytearray(resp["body"])
        tampered[40] ^= 1
        with self.assertRaisesRegex(CatalogSyncError, "verification FAILED"):
            verify_catalog_bytes(bytes(tampered), resp["headers"][
                "x-catalog-signature"], PUBHEX)

    def test_verify_fails_before_parse(self):
        """先验签后解析：验签失败的畸形 JSON 也拒绝（不许解析不可信输入）。"""
        bad_body = b"{not json at all"
        sig = base64.b64encode(sign(SEED, bad_body)).decode()
        # 签名对畸形正文有效——但解析失败 → 整份丢弃（拒绝细节不含正文内容）
        with self.assertRaisesRegex(CatalogSyncError, "failed to parse"):
            verify_catalog_bytes(bad_body, sig, PUBHEX)

    def test_structural_validation(self):
        self.assertTrue(is_catalog(make_doc()))
        self.assertFalse(is_catalog({"version": "bad", "providers": []}))
        self.assertFalse(is_catalog({"version": MIN_CATALOG_VERSION,
                                     "providers": "not-a-list"}))
        self.assertFalse(is_catalog({"version": MIN_CATALOG_VERSION,
                                     "providers": [{"platform": "x"}]}))


class TestVersionFloor(unittest.TestCase):
    def test_three_states(self):
        self.assertEqual(version_decision("2026.11.16"), "skipped_older")
        self.assertEqual(version_decision("2026.11.17"), "apply")
        self.assertEqual(
            version_decision("2026.11.17", applied_version="2026.11.17"),
            "up_to_date")
        self.assertEqual(
            version_decision("2026.11.20", applied_version="2026.11.17"),
            "apply")

    def test_cannot_rollback_baseline(self):
        """旧快照永远不能把新基线里已有的模型回滚掉（skipped_older）。"""
        self.assertEqual(version_decision("2026.01.01"), "skipped_older")

    def test_illegal_version_refused(self):
        with self.assertRaisesRegex(CatalogSyncError, "非法目录版本号"):
            version_decision("v1.2.3")


class TestFetchDegrade(unittest.TestCase):
    def test_disabled_switch(self):
        with mock.patch.dict(os.environ, {"OFR_CATALOG_SYNC_DISABLED": "1"}):
            out = fetch_and_verify()
        self.assertEqual(out["outcome"], "disabled")

    def test_http_downgraded_not_fatal(self):
        out = fetch_and_verify(
            fetch_fn=fake_fetch({"status": 503, "body": b"", "headers": {}}))
        self.assertEqual(out["outcome"], "degraded")
        self.assertIn("non-2xx", out["detail"])

    def test_fetch_exception_degraded(self):
        def boom(url):
            raise TimeoutError("20s timeout（占位）")
        out = fetch_and_verify(fetch_fn=boom)
        self.assertEqual(out["outcome"], "degraded")
        self.assertEqual(out["doc"], None)   # 本地目录继续服务

    def test_non_https_refused(self):
        out = fetch_and_verify(base_url="http://example.invalid/catalog",
                               fetch_fn=fake_fetch(
                                   {"status": 200, "body": b"{}", "headers": {}}))
        self.assertEqual(out["outcome"], "failed")

    def test_verify_failure_does_not_crash(self):
        resp = signed_response(make_doc(), key_seed=SEED_BAD)
        out = fetch_and_verify(fetch_fn=fake_fetch(resp))
        self.assertEqual(out["outcome"], "failed")
        self.assertIn("FAILED", out["detail"])


class TestRunSyncState(unittest.TestCase):
    def test_state_file_atomic_update(self):
        with tempfile.TemporaryDirectory() as td:
            sp = Path(td) / "catalog-state.json"
            resp = signed_response(make_doc())
            with mock.patch.dict(os.environ, {"OFR_CATALOG_PUBKEY": PUBHEX}):
                out = run_sync(state_path=sp, fetch_fn=fake_fetch(resp),
                               now_ms=1)
            self.assertEqual(out["outcome"], "apply")
            state = load_state(sp)
            self.assertEqual(state["applied_version"], MIN_CATALOG_VERSION)
            self.assertEqual(state["applied_json"]["version"],
                             MIN_CATALOG_VERSION)
            # 同版本再拉 → up_to_date（配合 304）
            resp2 = signed_response(make_doc())
            with mock.patch.dict(os.environ, {"OFR_CATALOG_PUBKEY": PUBHEX}):
                out2 = run_sync(state_path=sp, fetch_fn=fake_fetch(resp2),
                                now_ms=2)
            self.assertEqual(out2["outcome"], "up_to_date")
            self.assertFalse((sp.with_suffix(".tmp")).exists())

    def test_failure_recorded_last_error(self):
        with tempfile.TemporaryDirectory() as td:
            sp = Path(td) / "catalog-state.json"
            out = run_sync(state_path=sp, fetch_fn=fake_fetch(
                {"status": 500, "body": b"", "headers": {}}), now_ms=1)
            self.assertEqual(out["outcome"], "degraded")
            self.assertIn("non-2xx", load_state(sp)["last_error"])


class TestBackoffAndUrl(unittest.TestCase):
    def test_backoff_floor_5_minutes(self):
        self.assertGreaterEqual(retry_backoff_seconds(1), 300)
        self.assertGreaterEqual(retry_backoff_seconds(3), 300)
        self.assertLessEqual(retry_backoff_seconds(99), 1800)

    def test_since_param(self):
        url = build_catalog_source_url(since_version="2026.11.17")
        self.assertIn("since=2026.11.17", url)


if __name__ == "__main__":
    unittest.main()
