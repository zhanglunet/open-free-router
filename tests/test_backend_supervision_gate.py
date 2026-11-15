# -*- coding: utf-8 -*-
"""PRD-447/T3 测试（trend #2157 的 T3 环节）：后端监督台监督回路闸门。
运行：.venv/bin/python -m pytest tests/test_backend_supervision_gate.py -q
覆盖（T1 §2.3 + §5 红线）：强制复核四项缺一不可/手写 status 冒充被拦/
点头后发回再改回不继承点头/未结反馈拦截/放行与拦截审计留痕/
加固出口批量过闸/词表封闭/零网络/无路由切换执行路径/无人改建议入口/
闸门自身不授予点头。零网络；全合成占位。
"""
import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
MOD_DIR = Path(__file__).resolve().parents[1] / "src/open_free_router"
sys.path.insert(0, str(MOD_DIR))

from backend_supervision_bench import (  # noqa: E402
    BenchError, SupervisionLedger, close_feedback, decide,
    report_suggestion, rework_return, send_back,
)
from backend_supervision_gate import (  # noqa: E402
    BLOCK_REASON_CODES, GATE_AUDIT_KINDS, GATE_SCHEMA,
    admit_to_downstream, check_gate, enforce_gate, gate_outlet,
)

NOW = "2026-11-15T09:00:00Z"
BY = "reviewer（占位）"
STEPS = [{"name": "候选路评估（占位）", "conclusion": "路 A 更优（占位）",
          "input_summary": "延迟/配额快照（占位）"},
         {"name": "配额测算（占位）", "conclusion": "配额够用（占位）",
          "input_summary": "配额快照（占位）"}]


def fresh():
    return report_suggestion("sug-001", agent="router-agent（占位）",
                             task_ref="task-001", route_from="路 A（占位）",
                             route_to="路 B（占位）",
                             quota_change="配额 +10（占位）",
                             window="10:00-11:00（占位）",
                             reasoning_steps=STEPS,
                             by="router-agent（占位）", at=NOW)


def approved():
    s = fresh()
    decide(s, decision="approve", by=BY, at=NOW)
    return s


def codes(verdict):
    return [r["code"] for r in verdict["reasons"]]


class TestCheckGate(unittest.TestCase):
    def test_approved_suggestion_passes(self):
        v = check_gate(approved())
        self.assertTrue(v["allowed"])
        self.assertEqual(v["reasons"], [])

    def test_unapproved_blocked(self):
        v = check_gate(fresh())
        self.assertFalse(v["allowed"])
        self.assertIn("NOT_APPROVED", codes(v))
        self.assertIn("NO_DECISION", codes(v))
        self.assertIn("VERSION_NOT_APPROVED", codes(v))

    def test_handwritten_status_without_decision_blocked(self):
        s = fresh()
        s["status"] = "approved"
        s["versions"][-1]["status"] = "approved"
        v = check_gate(s)
        self.assertFalse(v["allowed"])
        self.assertIn("NO_DECISION", codes(v))
        self.assertNotIn("NOT_APPROVED", codes(v))
        self.assertNotIn("VERSION_NOT_APPROVED", codes(v))

    def test_send_back_after_approve_blocked(self):
        # 点头后又发回：建议回到 sent_back，decisions 里却还留着旧 approve。
        s = approved()
        send_back(s, feedback_type="reroute", demand="换路（占位）",
                  reason="理由（占位）", by=BY, at=NOW)
        self.assertEqual(s["status"], "sent_back")
        v = check_gate(s)
        self.assertFalse(v["allowed"])
        self.assertIn("NOT_APPROVED", codes(v))
        self.assertIn("OPEN_FEEDBACK", codes(v))

    def test_reworked_version_does_not_inherit_approval(self):
        # 发回→改回新版本：新版本重新待审，上一轮点头不继承，闸门须拦下。
        s = approved()
        t = send_back(s, feedback_type="keep_route", demand="保路（占位）",
                      reason="理由（占位）", by=BY, at=NOW)
        rework_return(s, t["ticket_id"], route_from="路 A（占位）",
                      route_to="路 C（占位）", changes="改走路 C（占位）",
                      reasoning_steps=STEPS, by="router-agent（占位）",
                      at=NOW)
        close_feedback(s, t["ticket_id"], by=BY, at=NOW)
        self.assertEqual(s["status"], "in_review")
        self.assertEqual(s["versions"][-1]["version"], 2)
        v = check_gate(s)
        self.assertFalse(v["allowed"])
        self.assertIn("NOT_APPROVED", codes(v))
        self.assertIn("VERSION_NOT_APPROVED", codes(v))
        self.assertNotIn("OPEN_FEEDBACK", codes(v))

    def test_open_feedback_blocks(self):
        s = fresh()
        send_back(s, feedback_type="add_note", demand="补说明（占位）",
                  reason="理由（占位）", by=BY, at=NOW)
        v = check_gate(s)
        self.assertFalse(v["allowed"])
        self.assertIn("OPEN_FEEDBACK", codes(v))

    def test_rejected_blocked(self):
        s = fresh()
        decide(s, decision="reject", by=BY, reason="否决（占位）", at=NOW)
        v = check_gate(s)
        self.assertFalse(v["allowed"])
        self.assertIn("NOT_APPROVED", codes(v))

    def test_malformed_suggestion_blocked_not_raised(self):
        for bad in (None, "x", {}, {"schema": "other/v9",
                                     "status": "approved"}):
            v = check_gate(bad)
            self.assertFalse(v["allowed"])
            self.assertIn("BAD_SUGGESTION", codes(v))


class TestEnforceAndAdmit(unittest.TestCase):
    def test_enforce_passes_approved(self):
        self.assertTrue(enforce_gate(approved())["allowed"])

    def test_enforce_raises_gate_blocked(self):
        with self.assertRaises(BenchError) as cm:
            enforce_gate(fresh())
        self.assertIn("GATE_BLOCKED", str(cm.exception))

    def test_admit_passes_and_audits(self):
        with tempfile.TemporaryDirectory() as d:
            led = SupervisionLedger(Path(d) / "sup.jsonl")
            out = admit_to_downstream(approved(), ledger=led, by=BY, at=NOW)
            self.assertEqual(out["admitted"]["suggestion_id"], "sug-001")
            self.assertTrue(out["verdict"]["allowed"])
            self.assertEqual(led.latest_for("sug-001")["kind"],
                             "gate_passed")

    def test_admit_blocked_audits_then_raises(self):
        with tempfile.TemporaryDirectory() as d:
            led = SupervisionLedger(Path(d) / "sup.jsonl")
            with self.assertRaises(BenchError) as cm:
                admit_to_downstream(fresh(), ledger=led, by=BY, at=NOW)
            self.assertIn("GATE_BLOCKED", str(cm.exception))
            entry = led.latest_for("sug-001")
            self.assertEqual(entry["kind"], "gate_blocked")
            self.assertIn("NOT_APPROVED", entry["reasons"])

    def test_admit_without_ledger_still_gates(self):
        out = admit_to_downstream(approved(), by=BY, at=NOW)
        self.assertTrue(out["verdict"]["allowed"])
        with self.assertRaises(BenchError):
            admit_to_downstream(fresh(), by=BY, at=NOW)

    def test_admit_returns_copy_not_alias(self):
        out = admit_to_downstream(approved(), by=BY, at=NOW)
        out["admitted"]["status"] = "rejected"
        self.assertEqual(approved()["status"], "approved")


class TestGateOutlet(unittest.TestCase):
    def test_mixed_batch(self):
        out = gate_outlet([approved(), fresh(), None], by=BY, at=NOW)
        self.assertEqual(len(out["passed"]), 1)
        self.assertEqual(out["blocked_count"], 2)
        self.assertEqual(out["blocked"][0]["suggestion_id"], "sug-001")
        self.assertEqual(out["blocked"][1]["suggestion_id"], None)
        self.assertTrue(any(r["code"] == "BAD_SUGGESTION"
                            for r in out["blocked"][1]["reasons"]))
        self.assertEqual(out["schema"], GATE_SCHEMA)

    def test_outlet_audits_both_outcomes(self):
        with tempfile.TemporaryDirectory() as d:
            led = SupervisionLedger(Path(d) / "sup.jsonl")
            gate_outlet([approved(), fresh()], ledger=led, by=BY, at=NOW)
            kinds = [e["kind"] for e in led.entries()]
            self.assertEqual(kinds, ["gate_passed", "gate_blocked"])

    def test_outlet_empty_input(self):
        out = gate_outlet([], by=BY, at=NOW)
        self.assertEqual(out["passed"], [])
        self.assertEqual(out["blocked_count"], 0)


class TestVocabularyNetworkAndRedLines(unittest.TestCase):
    def test_vocabularies_closed(self):
        self.assertEqual(set(BLOCK_REASON_CODES),
                         {"BAD_SUGGESTION", "NOT_APPROVED", "NO_DECISION",
                          "NO_VERSION", "VERSION_NOT_APPROVED",
                          "OPEN_FEEDBACK"})
        self.assertEqual(set(GATE_AUDIT_KINDS),
                         {"gate_passed", "gate_blocked"})

    def test_zero_network_ast(self):
        src = (MOD_DIR / "backend_supervision_gate.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        banned = {"socket", "urllib", "requests", "http", "subprocess",
                  "smtplib", "ftplib"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.assertNotIn(a.name.split(".")[0], banned)
            if isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module.split(".")[0], banned)

    def test_no_route_switch_execution_path(self):
        # 学习网关只出推荐不自动执行：闸门模块不含任何路由切换执行路径。
        src = (MOD_DIR / "backend_supervision_gate.py").read_text(
            encoding="utf-8")
        for token in ("switch_route", "apply_route", "set_route",
                      "do_switch", "execute(", "perform_switch"):
            self.assertNotIn(token, src)

    def test_no_suggestion_edit_entry(self):
        # 人只批注和裁决，不下场改建议：闸门模块不提供改建议内容的入口。
        src = (MOD_DIR / "backend_supervision_gate.py").read_text(
            encoding="utf-8")
        for token in ("route_to\" ] =", "route_to'] =", "update_suggestion",
                      "edit_suggestion"):
            self.assertNotIn(token, src)

    def test_gate_cannot_grant_approval(self):
        src = (MOD_DIR / "backend_supervision_gate.py").read_text(
            encoding="utf-8")
        self.assertNotIn('"] = "approved"', src)
        self.assertNotIn("decide(", src)


if __name__ == "__main__":
    unittest.main()
