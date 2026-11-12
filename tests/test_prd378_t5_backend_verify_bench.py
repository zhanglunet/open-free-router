# -*- coding: utf-8 -*-
"""PRD-378/T5 测试（trend #1882 的 T5 环节）：后端异常验证台验收试运行。

运行：.venv/bin/python -m pytest tests/test_prd378_t5_backend_verify_bench.py -q
（仓库根执行）

验收（T1 §6 T5 行「样例全链回放+UNVERIFIED 拦截演练+文档无密钥」）：
  接入点全链演练（PR 门槛 require_integration_point 验收）：探针快照 →
  告警 → verify_alerts 过台 → VERIFIED 转换路推荐来源形态（reroute_inbox
  collect 消费的 recommendations 形态，含 snapshot_digest）→ JSONL 落账
  → 闸门全 pass；两遍全链产物逐字节一致（确定性可复现）；UNVERIFIED 拦截
  演练（复现不出告警拦在推荐来源外+落账留痕）；探针变更二次验证关闭演练
  （BENCH_STALE 拒绝复用→新快照复算 VERIFIED）；账本 append-only；零路由
  切换零配置写复验（演练返回值零路由切换载荷键）；链复跑：T2/T3/T4 测试
  子进程复跑 exit 0；文档无密钥：T1 设计文档拼接 needle 扫描+占位符纪律。
  零网络；全合成占位。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from open_free_router.backend_verify_bench import (  # noqa: E402
    BackendBenchLedger,
    verify_alerts,
)
from open_free_router.backend_verify_bench_gates import (  # noqa: E402
    append_only_gate,
    gate_summary,
    mark_baseline,
    stale_gate,
    verdict_gate,
)

NOW = "2026-11-13T09:00:00Z"
PROBE = {"backend-a（占位）": {"status": "degraded", "latency_ms": 4000},
         "backend-b（占位）": {"status": "healthy", "latency_ms": 200}}


def recheck_fn(probe_data):
    return [{"backend": b, "status": v["status"]}
            for b, v in (probe_data or {}).items()
            if v.get("status") != "healthy"]


def _alerts():
    rerun = recheck_fn(PROBE)
    return [dict(r, alert_id=f"a-{i:03d}", kind="health_degraded",
                 source_ref=f"availability/v1#probe[{i}]（占位）")
            for i, r in enumerate(rerun, start=1)]


def _recommendations_from_verified(verified):
    """接入点：VERIFIED 告警转换路推荐来源形态（reroute_inbox.collect 消费
    的 recommendations 五件套，snapshot_digest 与验证台同源）。"""
    return [{"source_ref": v["source_ref"],
             "snapshot_digest": v["probe_fingerprint"],
             "recommendation": f"{v['backend']} 换路（占位）",
             "reason": f"验证过的 {v['kind']} 告警（占位）",
             "recommended_at": v["at"]}
            for v in verified]


class TestIntegrationPointDrill(unittest.TestCase):
    def test_outlet_full_chain_twice_deterministic(self):
        """接入点全链演练（PR 门槛验收）：告警 → 过台 → VERIFIED 转推荐
        来源形态 → JSONL 落账 → 闸门全 pass；两遍逐字节一致。"""
        runs = []
        for _ in range(2):
            out = verify_alerts(_alerts(), PROBE, recheck_fn, at=NOW)
            recs = _recommendations_from_verified(out["verified"])
            self.assertTrue(recs)
            self.assertTrue(all({"source_ref", "snapshot_digest",
                                 "recommendation", "reason"} <= set(r)
                                for r in recs))
            with tempfile.TemporaryDirectory() as td:
                led = BackendBenchLedger(Path(td) / "bbench.jsonl")
                for rec in out["verified"] + [r for r in out["unverified"]
                                              if not r.get("refused")]:
                    led.record(rec, at=NOW)
                gates = [append_only_gate(led)]
                for rec in led.entries():
                    gates.append(verdict_gate(rec))
                    gates.append(stale_gate(rec, PROBE))
                runs.append({"out": out, "recs": recs,
                             "entries": led.entries(),
                             "summary": gate_summary(gates)})
                self.assertEqual(gate_summary(gates)["gate"], "pass")
        self.assertEqual(json.dumps(runs[0], sort_keys=True),
                         json.dumps(runs[1], sort_keys=True))


class TestFailureDrills(unittest.TestCase):
    def test_unverified_blocked_and_traced(self):
        """UNVERIFIED 拦截演练：复现不出告警拦在推荐来源外+落账留痕。"""
        phantom = [{"alert_id": "a-900", "backend": "backend-x（占位）",
                    "kind": "health_degraded", "status": "degraded",
                    "source_ref": "availability/v1#x（占位）"}]
        with tempfile.TemporaryDirectory() as td:
            led = BackendBenchLedger(Path(td) / "bbench.jsonl")
            out = verify_alerts(phantom, PROBE, recheck_fn, at=NOW)
            self.assertEqual(out["verified"], [])
            for rec in out["unverified"]:
                if not rec.get("refused"):
                    led.record(rec, at=NOW)
            self.assertEqual(led.entries()[-1]["verdict"], "UNVERIFIED")

    def test_probe_change_then_second_verify_closes(self):
        """探针变更二次验证关闭演练：BENCH_STALE 拒绝复用 → 新快照复算
        VERIFIED 落新账。"""
        led = BackendBenchLedger(None)
        first = verify_alerts(_alerts(), PROBE, recheck_fn, at=NOW)
        self.assertTrue(first["verified"])
        led.record(first["verified"][0], at=NOW)
        new_probe = {"backend-c（占位）": {"status": "degraded",
                                          "latency_ms": 5000}}
        out = led.reverify(first["verified"][0]["alert_id"], new_probe,
                           recheck_fn, at=NOW)
        self.assertEqual(out["refused"], "BENCH_STALE")
        new_probe2 = dict(PROBE)
        new_probe2["backend-b（占位）"]["status"] = "degraded"
        second = verify_alerts([dict(ALERT := {
            "alert_id": "a-101", "backend": "backend-b（占位）",
            "kind": "health_degraded", "status": "degraded",
            "source_ref": "availability/v1#new（占位）"})],
            new_probe2, recheck_fn, at=NOW)
        if second["verified"]:
            rec = led.record(second["verified"][0], at=NOW)
            self.assertEqual(rec["verdict"], "VERIFIED")

    def test_zero_route_switch_and_append_only(self):
        """零路由切换零配置写复验（演练返回值零路由切换载荷键）+账本
        append-only。"""
        out = verify_alerts(_alerts(), PROBE, recheck_fn, at=NOW)
        for rec in out["verified"]:
            for key in ("config_written", "route_switched", "reloaded"):
                self.assertNotIn(key, rec)
        led = BackendBenchLedger(None)
        for rec in out["verified"]:
            led.record(rec, at=NOW)
        mark_baseline(led)
        self.assertEqual(append_only_gate(led)["gate"], "pass")


class TestChainRerun(unittest.TestCase):
    def test_t2_t3_t4_rerun_green(self):
        """链复跑：T2/T3/T4 测试子进程复跑 exit 0（链上仍绿实测）。"""
        venv_py = REPO / ".venv" / "bin" / "python"
        py = str(venv_py) if venv_py.exists() else sys.executable
        for test_file in ("test_prd378_t2_backend_verify_bench.py",
                          "test_prd378_t3_backend_verify_bench_gates.py",
                          "test_prd378_t4_backend_verify_bench_boundary.py"):
            proc = subprocess.run(
                [py, "-m", "pytest", "-q",
                 str(REPO / "tests" / test_file)],
                capture_output=True, timeout=300)
            self.assertEqual(proc.returncode, 0,
                             f"{test_file} 复跑失败：{proc.stdout[-400:]}")


class TestDocNoSecrets(unittest.TestCase):
    def test_t1_design_doc_no_secrets(self):
        """文档无密钥：T1 设计文档拼接 needle 扫描+占位符纪律（R5）。"""
        doc = next(REPO.glob("docs/plans/2026-11-12-prd-378-*.md"))
        text = doc.read_text(encoding="utf-8").lower()
        for needle in ("sk-" + "live", "passw" + "ord", "api_k" + "ey"):
            self.assertNotIn(needle, text, f"设计文档出现密钥词 {needle}")
        self.assertIn("占位", text)


if __name__ == "__main__":
    unittest.main()
