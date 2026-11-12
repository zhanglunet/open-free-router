# -*- coding: utf-8 -*-
"""PRD-378/T2 测试（trend #1882 的 T2 环节）：后端异常验证台。

运行：.venv/bin/python -m pytest tests/test_prd378_t2_backend_verify_bench.py -q
（仓库根执行）

覆盖（T1 §4 + R1~R4）：verify_alert 八件套校验（缺一 BENCH_INCOMPLETE）；
真实拨测判定串联（探针快照重跑复现 → VERIFIED；复现不出 → UNVERIFIED 词
表封闭）；verify_alerts 换路推荐来源包装（UNVERIFIED 拦下）；
BackendBenchLedger JSONL 真实落库+八件套缺一/词表外/REPRO_MISMATCH 声称
拒绝；reverify 探针变更二次验证（指纹不符 BENCH_STALE 拒绝复用/一致正常
复验）；AST 零 LLM 零网络零子进程零配置写零路由切换+探针指纹与 350
snapshots_digest 同族口径。零网络；全合成占位。
"""
import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from open_free_router.backend_verify_bench import (  # noqa: E402
    BENCH_SCHEMA,
    BackendBenchError,
    BackendBenchLedger,
    ERROR_CODES,
    VERDICTS,
    probe_fingerprint,
    verify_alert,
    verify_alerts,
)

NOW = "2026-11-13T09:00:00Z"
PROBE = {"backend-a（占位）": {"status": "degraded", "latency_ms": 4000},
         "backend-b（占位）": {"status": "healthy", "latency_ms": 200}}
ALERT = {"alert_id": "a-001", "backend": "backend-a（占位）",
         "kind": "health_degraded", "status": "degraded",
         "source_ref": "availability/v1#probe[0]（占位）"}


def recheck_fn(probe_data):
    """注入式拨测判定（对既有快照复算，零外呼）：status != healthy 即异常。"""
    return [{"backend": b, "status": v["status"]}
            for b, v in (probe_data or {}).items()
            if v.get("status") != "healthy"]


class TestVerifyAlert(unittest.TestCase):
    def test_verified_with_probe_rerun(self):
        """真实拨测判定串联：告警与重跑结果一致 → VERIFIED；探针指纹与
        350 snapshots_digest 同族口径（sha16: 前缀确定性）。"""
        out = verify_alert(ALERT, PROBE, recheck_fn, at=NOW)
        self.assertEqual(out["verdict"], "VERIFIED")
        self.assertEqual(out["schema"], BENCH_SCHEMA)
        fp = probe_fingerprint(PROBE)
        self.assertTrue(fp.startswith("sha16:"))
        self.assertEqual(out["probe_fingerprint"], fp)

    def test_unverified_when_not_reproduced(self):
        out = verify_alert(dict(ALERT, status="quota_low"), PROBE,
                           recheck_fn, at=NOW)
        self.assertEqual(out["verdict"], "UNVERIFIED")
        self.assertFalse(out["reproduce"]["match"])

    def test_incomplete_refused(self):
        bad = dict(ALERT)
        del bad["status"]
        out = verify_alert(bad, PROBE, recheck_fn, at=NOW)
        self.assertEqual(out["refused"], "BENCH_INCOMPLETE")
        out2 = verify_alert(ALERT, PROBE, None, at=NOW)
        self.assertEqual(out2["refused"], "BENCH_INCOMPLETE")

    def test_verdict_vocab_closed(self):
        self.assertEqual(set(VERDICTS), {"VERIFIED", "UNVERIFIED"})


class TestVerifyAlerts(unittest.TestCase):
    def test_outlet_blocks_unverified(self):
        """换路推荐来源包装：UNVERIFIED 拦下不进推荐来源（R1）。"""
        alerts = [ALERT, dict(ALERT, alert_id="a-002",
                              backend="backend-b（占位）")]
        out = verify_alerts(alerts, PROBE, recheck_fn, at=NOW)
        self.assertEqual(len(out["verified"]), 1)
        self.assertEqual(out["unverified_count"], 1)
        self.assertEqual(out["unverified"][0]["alert_id"], "a-002")


class TestBackendBenchLedger(unittest.TestCase):
    def test_jsonl_persistence_append_only(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bbench" / "ledger.jsonl"
            led = BackendBenchLedger(path)
            led.record(verify_alert(ALERT, PROBE, recheck_fn, at=NOW),
                       at=NOW)
            led.record(verify_alert(dict(ALERT, alert_id="a-002"), PROBE,
                                    recheck_fn, at=NOW), at=NOW)
            self.assertEqual(len(led.entries()), 2)
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["alert_id"], "a-001")

    def test_record_refusals(self):
        led = BackendBenchLedger(None)
        with self.assertRaisesRegex(BackendBenchError, "BENCH_INCOMPLETE"):
            led.record({"schema": BENCH_SCHEMA}, at=NOW)
        with self.assertRaisesRegex(BackendBenchError, "BENCH_REPRO_MISMATCH"):
            bad = verify_alert(ALERT, PROBE, recheck_fn, at=NOW)
            bad = dict(bad, verdict="VERIFIED",
                       reproduce={"match": False, "detail": "改（占位）"})
            led.record(bad, at=NOW)


class TestReverify(unittest.TestCase):
    def test_stale_refuses_reuse(self):
        """探针快照变更 → BENCH_STALE 拒绝复用旧 VERIFIED。"""
        led = BackendBenchLedger(None)
        led.record(verify_alert(ALERT, PROBE, recheck_fn, at=NOW), at=NOW)
        new_probe = {"backend-a（占位）": {"status": "healthy",
                                          "latency_ms": 200}}
        out = led.reverify("a-001", new_probe, recheck_fn, at=NOW)
        self.assertEqual(out["refused"], "BENCH_STALE")

    def test_same_probe_reverify_ok(self):
        led = BackendBenchLedger(None)
        led.record(verify_alert(ALERT, PROBE, recheck_fn, at=NOW), at=NOW)
        out = led.reverify("a-001", PROBE, recheck_fn, at=NOW)
        self.assertEqual(out["verdict"], "VERIFIED")


class TestRedline(unittest.TestCase):
    def test_ast_zero_llm_zero_network_zero_route_switch(self):
        """AST：零 LLM 零网络零子进程零配置写零路由切换词面（学习网关只出
        推荐红线不变）。"""
        banned = {"urllib", "requests", "socket", "http", "subprocess",
                  "openai", "anthropic", "config_snapshot"}
        src = REPO / "src" / "open_free_router" / "backend_verify_bench.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & banned, imported & banned)
        names = {n.id.lower() for n in ast.walk(tree)
                 if isinstance(n, ast.Name)}
        names |= {n.attr.lower() for n in ast.walk(tree)
                  if isinstance(n, ast.Attribute)}
        for word in ("write_config", "switch_route", "reload_now"):
            self.assertNotIn(word, names, f"出现路由切换词面 {word}")

    def test_error_codes_closed(self):
        self.assertEqual(set(ERROR_CODES),
                         {"BENCH_INCOMPLETE", "BENCH_REPRO_MISMATCH",
                          "BENCH_STALE"})


if __name__ == "__main__":
    unittest.main()
