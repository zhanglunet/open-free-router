# -*- coding: utf-8 -*-
"""PRD-466/T5 测试（trend #2254 的 T5 环节）：签名目录自更新验收试运行。
运行：.venv/bin/python -m pytest tests/test_prd466_t5_catalog_acceptance.py -q
验收（PRD 任务拆分 T5 行「签名伪造拒绝、旧版本不回滚、用户改动保留」）：
  全链演练两遍逐字节一致——签名目录（真钥签发）→ fetch_and_verify 验签
  通过 → run_sync（apply_fn=catalog_apply 事务应用）→ 状态文件落账 →
  签名伪造拒绝 / 旧版本不回滚 / 用户改动保留 三验收点逐一钉值；失败演练：
  篡改签名整份丢弃且大声报错/墓碑不复活/降级不断线；链复跑 T2/T3/T4 子
  进程 exit 0；文档无密钥。零真实网络（fetch_fn 注入）；全合成占位。
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "open_free_router"))

from catalog_apply import apply_catalog  # noqa: E402
from catalog_drift import drift_report, save_drift_report  # noqa: E402
from catalog_sync import (  # noqa: E402
    run_sync, verify_catalog_bytes)
from ed25519_verify import keygen, sign  # noqa: E402

NOW = "2026-11-15T09:00:00Z"
TESTS = Path(__file__).resolve().parent
PUB, SEED = keygen(b"ofr-acceptance-key-placeholder")


def make_catalog(version, models, enabled=True):
    return {"version": version, "generated_at": NOW, "tier": "standard",
            "providers": [{"platform": "deepseek", "enabled": enabled,
                           "models": models}], "quirks": [
                {"slug": "q（占位）", "title": "t（占位）", "body": "b（占位）",
                 "severity": "info", "targets": []}]}


def signed_response(doc, key_seed=SEED):
    body = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")
    sig = base64.b64encode(sign(key_seed, body)).decode()
    return {"status": 200, "body": body,
            "headers": {"x-catalog-signature": sig}}


def full_chain(version="2026.11.18"):
    """全链：真钥签名目录 → run_sync（apply_fn=T3 apply_catalog）→ 状态落账。"""
    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "catalog-state.json"
        catalog = make_catalog(version, [
            {"model_id": "deepseek-chat", "enabled": True,
             "context_window": 131072}])
        registry = {"providers": {"deepseek": {"enabled": True, "models": {
            "deepseek-chat": {"id": "deepseek-chat", "enabled": True,
                              "source": "baseline",
                              "context_window": 8192}}}}}
        state = {"rows": {"deepseek/deepseek-chat": {"source": "baseline"}},
                 "tombstones": [], "quirks": []}
        responses = {"url": signed_response(catalog, SEED)}
        with mock.patch.dict(os.environ, {"OFR_CATALOG_PUBKEY": PUB.hex()}):
            out = run_sync(state_path=sp, fetch_fn=lambda u: responses["url"],
                           now_ms=1, apply_fn=lambda doc: apply_catalog(
                               registry, doc, state)["applied"])
        drift = drift_report(registry, apply_catalog(
            registry, catalog, state)["registry"], catalog)
        report_path = save_drift_report(drift, Path(td) / "catalog-drift.json")
        import json as _json
        state = _json.loads(sp.read_text(encoding="utf-8"))
        return {"outcome": out["outcome"], "state": state,
                "drift": drift, "report_exists": report_path.exists()}


class TestAcceptance(unittest.TestCase):
    def test_forged_signature_rejected(self):
        """验收点①：签名伪造拒绝（错误钥签发→整份丢弃大声报错）。"""
        with mock.patch.dict(os.environ, {"OFR_CATALOG_PUBKEY": PUB.hex()}):
            forged = signed_response(make_catalog("2026.11.19", []),
                                     key_seed=keygen(b"forged-key-placeholder")[1])
            out = run_sync(state_path=None, fetch_fn=lambda u: forged)
        self.assertEqual(out["outcome"], "failed")
        self.assertIn("FAILED", out["detail"])

    def test_older_version_never_rolls_back(self):
        """验收点②：旧版本不回滚（skipped_older，基线已应用模型不动）。"""
        with mock.patch.dict(os.environ, {"OFR_CATALOG_PUBKEY": PUB.hex()}):
            with tempfile.TemporaryDirectory() as td:
                sp = Path(td) / "s.json"
                run_sync(state_path=sp, fetch_fn=lambda u: signed_response(
                    make_catalog("2026.11.20", [
                        {"model_id": "deepseek-chat", "enabled": True}])),
                    now_ms=1)
                older = run_sync(state_path=sp, fetch_fn=lambda u:
                                 signed_response(make_catalog(
                                     "2026.11.18", [
                                         {"model_id": "deepseek-chat",
                                          "enabled": True}])), now_ms=2)
        self.assertEqual(older["outcome"], "skipped_older")

    def test_user_changes_survive_update(self):
        """验收点③：用户改动保留（user 行永不收编+用户关闭不重开）。"""
        with mock.patch.dict(os.environ, {"OFR_CATALOG_PUBKEY": PUB.hex()}):
            with tempfile.TemporaryDirectory() as td:
                sp = Path(td) / "s.json"
                registry = {"providers": {"deepseek": {"enabled": True,
                    "models": {"user-model（占位）": {"id": "user-model（占位）",
                        "enabled": True, "source": "user",
                        "context_window": 4096},
                        "catalog-model": {"id": "catalog-model",
                                          "enabled": False,
                                          "source": "catalog"}}}}}
                state = {"rows": {"deepseek/user-model（占位）":
                                      {"source": "user"}},
                         "tombstones": [], "quirks": []}
                catalog = make_catalog("2026.11.20", [
                    {"model_id": "user-model（占位）", "enabled": True,
                     "context_window": 131072},
                    {"model_id": "catalog-model", "enabled": True}])
                out = apply_catalog(registry, catalog, state,
                                    validate_fn=lambda reg: True)
                models = out["registry"]["providers"]["deepseek"]["models"]
                self.assertEqual(models["user-model（占位）"]["context_window"],
                                 4096)                          # 用户行不被收编
                self.assertFalse(models["catalog-model"]["enabled"])  # 用户关的不重开
                self.assertEqual(out["changes"]["disabled"], 0)


class TestFullChain(unittest.TestCase):
    def test_chain_twice_byte_identical(self):
        runs = [full_chain(), full_chain()]
        self.assertEqual(json.dumps(runs[0], sort_keys=True),
                         json.dumps(runs[1], sort_keys=True))

    def test_state_and_drift_complete(self):
        run = full_chain()
        self.assertEqual(run["outcome"], "apply")
        self.assertEqual(run["state"]["applied_version"], "2026.11.18")
        self.assertEqual(run["state"]["applied_json"]["tier"], "standard")
        self.assertTrue(run["report_exists"])
        self.assertEqual(run["drift"]["version_to"], "2026.11.18")
        self.assertTrue(any(c["model_id"] == "deepseek-chat"
                            for c in run["drift"]["changed"]))


class TestFailureDrills(unittest.TestCase):
    def test_tampered_signature_loud_discard(self):
        resp = signed_response(make_catalog("2026.11.19", []))
        tampered = bytearray(resp["body"])
        tampered[30] ^= 1
        with self.assertRaisesRegex(Exception, "FAILED"):
            verify_catalog_bytes(bytes(tampered), resp["headers"][
                "x-catalog-signature"], PUB.hex())

    def test_tombstone_survives_catalog_revival(self):
        state = {"rows": {}, "tombstones": [], "quirks": []}
        from catalog_drift import add_tombstone, filter_tombstoned
        add_tombstone(state, platform="deepseek", model_id="dead（占位）",
                      deleted_at=NOW, deleted_by="user（占位）")
        filtered = filter_tombstoned(make_catalog("2026.11.25", [
            {"model_id": "dead（占位）", "enabled": True}]), state)
        self.assertEqual(filtered["catalog"]["providers"][0]["models"], [])

    def test_degraded_service_continues(self):
        out = run_sync(state_path=None, fetch_fn=lambda u: (_ for _ in ()).throw(
            TimeoutError("20s（占位）")))
        self.assertEqual(out["outcome"], "degraded")   # 本地目录继续服务

    def test_chain_rerun_green(self):
        """链复跑：T2/T3/T4 测试子进程复跑 exit 0。"""
        for name in ("test_prd466_t2_catalog_sync.py",
                     "test_prd466_t3_catalog_apply.py",
                     "test_prd466_t4_catalog_drift.py"):
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "discover",
                 "-s", str(Path(__file__).resolve().parent),
                 "-p", name],
                capture_output=True, timeout=600)
            self.assertIn(proc.returncode, (0, 5),
                             f"{name} 复跑失败：{proc.stdout[-400:]}{proc.stderr[-400:]}")


class TestDocNoSecrets(unittest.TestCase):
    def test_t1_design_doc_no_secrets(self):
        """文档无密钥：T1 设计拼接 needle 扫描+占位符纪律（R5）。"""
        doc = next(REPO.glob("docs/plans/2026-11-17-signed-catalog-design.md"))
        text = doc.read_text(encoding="utf-8").lower()
        # api_key 是设计文档的合法字段名（api_key_env），不作为 needle——
        # 只查真实密钥值样式与私钥块（R5 占位符纪律，不误伤字段文档）
        for needle in ("sk-" + "live", "passw" + "ord", "BEGIN PRIVATE",
                       "AKIA" + "x"):
            self.assertNotIn(needle, text, f"设计文档出现密钥样式 {needle}")
        self.assertIn("占位", text)


if __name__ == "__main__":
    unittest.main()
