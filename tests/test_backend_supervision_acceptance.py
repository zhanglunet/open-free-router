# -*- coding: utf-8 -*-
"""PRD-447/T5 测试（trend #2159 的 T5 环节）：后端监督台验收试运行。

按 T5 issue 验收标准"全链跑通"，把 T1 设计、T2 最小实现、T3 闸门、T4 划界
串成一次端到端试运行（只读用例，全合成占位，不改 T1–T4 的设计与实现）：
  场景 A（主链）：不靠谱的换路建议在调度时出炉（带推理过程）→ 人在监督台
    看过程、批注第 0 步 → 一点把"换路"发回 agent → agent 改完回来（新版本）
    → 人复核关闭反馈 → 点头 → T3 闸门放行 → T4 划界放行 → gate_outlet
    只放行已点头的建议进换路批准收件箱，未点头的拦下；
  场景 B（未点头不进收件箱）：待审的建议在 T2 outlet、T3 enforce_gate、
    gate_outlet 三处都被拦下；已否决的建议同样拦下；
  场景 C（T3 回归）：已点头后又发回，闸门立即拦下（旗子失效），须重新
    改回、重新点头才能再进收件箱；
  场景 D（T4 划界）：配置评审/验异常真假/待办收发/人改建议/执行换路五个
    越界动作被拒；带外来认领（config_reviewed）的建议在 guard_admission
    被拦下；学习网关只出推荐不自动执行——监督台不触发路由切换；
  场景 E（留痕）：批注、发回、回炉、关闭、点头、过闸、过界全程记进
    SupervisionLedger，拦截尝试也留痕（gate_blocked / boundary_blocked）。
运行：.venv/bin/python -m pytest tests/test_backend_supervision_acceptance.py -q
零网络；全合成占位。
"""
import ast
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/open_free_router"))

from backend_supervision_bench import (  # noqa: E402
    BenchError, SupervisionLedger, add_annotation, close_feedback, decide,
    outlet, report_suggestion, rework_return, send_back,
)
from backend_supervision_boundary import (  # noqa: E402
    assert_boundary, check_suggestion_scope, guard_admission,
)
from backend_supervision_gate import (  # noqa: E402
    admit_to_downstream, check_gate, enforce_gate, gate_outlet,
)

NOW = "2026-11-15T09:00:00Z"
REVIEWER = "reviewer（占位）"
AGENT = "agent（占位）"
STEPS_V1 = [{"name": "看配额（占位）", "conclusion": "余量不足（占位）",
             "input_summary": "配额快照（占位）"},
            {"name": "选路（占位）", "conclusion": "换到 route-b（占位）",
             "input_summary": "候选路由（占位）"}]
STEPS_V2 = [{"name": "看配额（占位）", "conclusion": "余量不足（占位）",
             "input_summary": "配额快照（占位）"},
            {"name": "选路（占位）", "conclusion": "保 route-a，限流（占位）",
             "input_summary": "候选路由（占位）"}]


def fresh(suggestion_id="sug-001"):
    return report_suggestion(suggestion_id, agent=AGENT, task_ref="task-001",
                             route_from="route-a（占位）",
                             route_to="route-b（占位）",
                             reasoning_steps=STEPS_V1, by=AGENT, at=NOW)


def run_to_approved(sug):
    """主链：出炉 → 批注发回 → 改回 → 点头。返回建议。"""
    ann = add_annotation(sug, author=REVIEWER,
                         text="这条换路理由不充分，请换路（占位）",
                         step_index=0, at=NOW)
    ticket = send_back(sug, feedback_type="reroute",
                       demand="换路重算（占位）",
                       annotation_ids=[ann["annotation_id"]],
                       reason="理由不充分（占位）", by=REVIEWER, at=NOW)
    rework_return(sug, ticket["ticket_id"], route_from="route-a（占位）",
                  route_to="route-a（占位）", quota_change="限流（占位）",
                  reasoning_steps=STEPS_V2, changes="保路+限流（占位）",
                  by=AGENT, at=NOW)
    close_feedback(sug, ticket["ticket_id"], by=REVIEWER, at=NOW)
    decide(sug, decision="approve", by=REVIEWER,
           reason="同意建议（占位）", at=NOW)
    return sug


class TestAcceptanceMainChain(unittest.TestCase):
    def test_full_chain_to_inbox(self):
        sug = run_to_approved(fresh())
        self.assertEqual(sug["status"], "approved")
        self.assertEqual(len(sug["versions"]), 2)
        self.assertEqual(sug["versions"][-1]["status"], "approved")
        self.assertEqual(sug["feedback"][0]["status"], "closed")
        self.assertEqual(sug["annotations"][0]["status"], "accepted")
        # T3 闸门：从原始状态重推点头结论，通过。
        self.assertTrue(check_gate(sug)["allowed"])
        self.assertEqual(check_gate(sug)["reasons"], [])
        enforce_gate(sug)
        # T4 划界：回路内动作 + 无外来认领，通过。
        self.assertTrue(check_suggestion_scope(sug)["in_scope"])
        admitted = guard_admission(
            {"kind": "decide_approval"}, sug,
            ledger=SupervisionLedger(), by=REVIEWER, at=NOW)
        self.assertEqual(admitted["boundary"], "supervision-loop")
        self.assertEqual(admitted["admitted"]["suggestion_id"],
                         sug["suggestion_id"])
        # 出口：已点头的进换路批准收件箱。
        out = outlet([sug])
        self.assertEqual(len(out["passed"]), 1)
        self.assertEqual(out["blocked_count"], 0)

    def test_gate_outlet_mixed_batch(self):
        good = run_to_approved(fresh("sug-001"))
        bad = fresh("sug-002")
        decide(bad, decision="reject", by=REVIEWER,
               reason="不靠谱，否决（占位）", at=NOW)
        res = gate_outlet([good, bad], ledger=SupervisionLedger(),
                          by=REVIEWER, at=NOW)
        self.assertEqual(len(res["passed"]), 1)
        self.assertEqual(res["passed"][0]["suggestion_id"], "sug-001")
        self.assertEqual(res["blocked_count"], 1)
        self.assertEqual(res["blocked"][0]["suggestion_id"], "sug-002")
        self.assertTrue(any(r["code"] == "NOT_APPROVED"
                            for r in res["blocked"][0]["reasons"]))


class TestAcceptanceUnapprovedBlocked(unittest.TestCase):
    def test_unapproved_blocked_everywhere(self):
        sug = fresh()
        self.assertEqual(outlet([sug])["blocked_count"], 1)
        with self.assertRaises(BenchError) as ctx:
            enforce_gate(sug)
        self.assertIn("GATE_BLOCKED", str(ctx.exception))
        with self.assertRaises(BenchError):
            admit_to_downstream(sug, ledger=SupervisionLedger(),
                                by=REVIEWER, at=NOW)
        res = gate_outlet([sug], ledger=SupervisionLedger(),
                          by=REVIEWER, at=NOW)
        self.assertEqual(res["blocked_count"], 1)

    def test_rejected_blocked(self):
        sug = fresh()
        decide(sug, decision="reject", by=REVIEWER,
               reason="否决（占位）", at=NOW)
        self.assertEqual(outlet([sug])["blocked_count"], 1)
        self.assertFalse(check_gate(sug)["allowed"])

    def test_admit_blocked_is_audited(self):
        sug = fresh()
        led = SupervisionLedger()
        with self.assertRaises(BenchError):
            admit_to_downstream(sug, ledger=led, by=REVIEWER, at=NOW)
        kinds = [e["kind"] for e in led.entries()]
        self.assertIn("gate_blocked", kinds)


class TestAcceptanceGateRegression(unittest.TestCase):
    def test_send_back_after_approve_reblocks(self):
        sug = run_to_approved(fresh())
        self.assertTrue(check_gate(sug)["allowed"])
        # 已点头后又发回：旗子失效，闸门立即拦下。
        send_back(sug, feedback_type="keep_route", demand="保路重算（占位）",
                  reason="再确认一次（占位）", by=REVIEWER, at=NOW)
        verdict = check_gate(sug)
        self.assertFalse(verdict["allowed"])
        codes = {r["code"] for r in verdict["reasons"]}
        self.assertIn("NOT_APPROVED", codes)
        self.assertIn("VERSION_NOT_APPROVED", codes)
        self.assertIn("OPEN_FEEDBACK", codes)
        with self.assertRaises(BenchError):
            admit_to_downstream(sug, ledger=SupervisionLedger(),
                                by=REVIEWER, at=NOW)
        # 改回、关闭、重新点头后才能再进收件箱。
        ticket = sug["feedback"][-1]
        rework_return(sug, ticket["ticket_id"],
                      route_from="route-a（占位）",
                      route_to="route-a（占位）",
                      reasoning_steps=STEPS_V2, changes="保路（占位）",
                      by=AGENT, at=NOW)
        close_feedback(sug, ticket["ticket_id"], by=REVIEWER, at=NOW)
        decide(sug, decision="approve", by=REVIEWER, at=NOW)
        self.assertTrue(check_gate(sug)["allowed"])
        self.assertEqual(len(sug["versions"]), 3)


class TestAcceptanceBoundary(unittest.TestCase):
    def test_out_of_scope_actions_rejected(self):
        for kind in ("review_config", "verify_anomaly", "inbox_dispatch",
                     "edit_suggestion", "execute_reroute"):
            with self.assertRaises(BenchError) as ctx:
                assert_boundary({"kind": kind})
            self.assertIn("OUT_OF_SCOPE", str(ctx.exception))

    def test_foreign_claim_blocks_guard(self):
        sug = run_to_approved(fresh())
        sug["claims"] = {"config_reviewed": True}
        self.assertFalse(check_suggestion_scope(sug)["in_scope"])
        with self.assertRaises(BenchError) as ctx:
            guard_admission({"kind": "decide_approval"}, sug,
                            ledger=SupervisionLedger(),
                            by=REVIEWER, at=NOW)
        self.assertIn("OUT_OF_SCOPE", str(ctx.exception))

    def test_guard_rejects_out_of_scope_action_even_if_approved(self):
        sug = run_to_approved(fresh())
        with self.assertRaises(BenchError) as ctx:
            guard_admission({"kind": "execute_reroute"}, sug,
                            ledger=SupervisionLedger(),
                            by=REVIEWER, at=NOW)
        self.assertIn("OUT_OF_SCOPE", str(ctx.exception))


class TestAcceptanceLedgerTrail(unittest.TestCase):
    def test_full_audit_trail(self):
        sug = fresh()
        led = SupervisionLedger()
        for entry in sug["progress"]:
            led.record(sug, entry)
        run_to_approved(sug)
        for entry in sug["progress"][1:]:
            led.record(sug, entry)
        admit_to_downstream(sug, ledger=led, by=REVIEWER, at=NOW)
        guard_admission({"kind": "decide_approval"}, sug, ledger=led,
                        by=REVIEWER, at=NOW)
        kinds = [e["kind"] for e in led.entries()]
        for expected in ("reported", "annotation", "send_back",
                         "rework_return", "feedback_closed", "approve",
                         "gate_passed", "boundary_passed"):
            self.assertIn(expected, kinds)
        bad = fresh("sug-002")
        with self.assertRaises(BenchError):
            guard_admission({"kind": "execute_reroute"}, bad, ledger=led,
                            by=REVIEWER, at=NOW)
        kinds = [e["kind"] for e in led.entries()]
        self.assertIn("boundary_blocked", kinds)

    def test_ledger_jsonl_roundtrip(self):
        sug = run_to_approved(fresh())
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "trial.jsonl"
            led = SupervisionLedger(path)
            for entry in sug["progress"]:
                led.record(sug, entry)
            rows = path.read_text(encoding="utf-8").strip().split("\n")
            self.assertEqual(len(rows), len(sug["progress"]))
            self.assertEqual(led.latest_for(sug["suggestion_id"])["kind"],
                             "approve")


class TestAcceptanceZeroNetwork(unittest.TestCase):
    def test_zero_network_ast(self):
        src = (Path(__file__).resolve().read_text(encoding="utf-8"))
        tree = ast.parse(src)
        banned = {"socket", "urllib", "requests", "http", "subprocess",
                  "smtplib", "ftplib"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.assertNotIn(a.name.split(".")[0], banned)
            if isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module.split(".")[0], banned)


if __name__ == "__main__":
    unittest.main()
