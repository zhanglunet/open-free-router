# -*- coding: utf-8 -*-
"""PRD-447/T2 测试（trend #2156 的 T2 环节）：后端监督台。
运行：.venv/bin/python -m pytest tests/test_backend_supervision_bench.py -q
覆盖（T1 §2 + §5 红线）：建议上报/批注锚定推理步骤/一键发回/改回新版本
不继承点头/裁决三选一理由必填/outlet 拦截未点头/JSONL 留痕/词表封闭/
零网络/无路由切换执行路径/无人改建议入口。零网络；全合成占位。
"""
import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
MOD_DIR = Path(__file__).resolve().parents[1] / "src/open_free_router"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/open_free_router"))

from backend_supervision_bench import (  # noqa: E402
    ANNOTATION_STATUSES, DECISIONS, ERROR_CODES, FEEDBACK_STATUSES,
    FEEDBACK_TYPES, SUGGESTION_STATUSES, BenchError, SupervisionLedger,
    add_annotation, close_feedback, decide, outlet, report_suggestion,
    rework_return, send_back,
)

NOW = "2026-11-15T09:00:00Z"
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


class TestReport(unittest.TestCase):
    def test_report_starts_in_review(self):
        s = fresh()
        self.assertEqual(s["status"], "in_review")
        self.assertEqual(len(s["versions"]), 1)
        self.assertEqual(s["versions"][0]["version"], 1)
        self.assertEqual(len(s["reasoning_steps"]), 2)
        self.assertEqual(s["progress"][0]["kind"], "reported")

    def test_report_missing_route_rejected(self):
        with self.assertRaises(BenchError):
            report_suggestion("s", agent="a", task_ref="t",
                              route_from="", route_to="路 B（占位）")


class TestAnnotation(unittest.TestCase):
    def test_annotation_anchors_reasoning_step(self):
        s = fresh()
        ann = add_annotation(s, author="reviewer（占位）",
                             text="配额测算存疑（占位）", step_index=1,
                             at=NOW)
        self.assertEqual(ann["step_index"], 1)
        self.assertEqual(ann["status"], "open")

    def test_annotation_on_whole_suggestion(self):
        s = fresh()
        ann = add_annotation(s, author="r", text="整体存疑（占位）", at=NOW)
        self.assertIsNone(ann["step_index"])

    def test_annotation_unknown_step_rejected(self):
        s = fresh()
        with self.assertRaises(BenchError):
            add_annotation(s, author="r", text="x", step_index=9, at=NOW)


class TestFeedbackLoop(unittest.TestCase):
    def test_send_back_rework_close_roundtrip(self):
        s = fresh()
        ann = add_annotation(s, author="reviewer（占位）",
                             text="保路更稳（占位）", step_index=0, at=NOW)
        t = send_back(s, feedback_type="keep_route",
                      demand="保路并补说明（占位）",
                      annotation_ids=[ann["annotation_id"]],
                      reason="路 B 未验证（占位）",
                      by="reviewer（占位）", at=NOW)
        self.assertEqual(t["status"], "open")
        self.assertEqual(t["annotation_ids"], [ann["annotation_id"]])
        self.assertEqual(s["status"], "sent_back")
        back = rework_return(s, t["ticket_id"], route_from="路 A（占位）",
                             route_to="路 A（占位）",
                             changes="改保路并补说明（占位）",
                             reasoning_steps=STEPS,
                             by="router-agent（占位）", at=NOW)
        self.assertEqual(back["status"], "returned")
        self.assertEqual(back["returned_version"], 2)
        self.assertEqual(s["status"], "in_review")
        self.assertEqual(len(s["versions"]), 2)
        closed = close_feedback(s, t["ticket_id"], by="reviewer（占位）",
                                at=NOW)
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(s["annotations"][0]["status"], "accepted")

    def test_new_version_does_not_inherit_approval(self):
        s = fresh()
        decide(s, decision="approve", by="reviewer（占位）", at=NOW)
        self.assertEqual(s["status"], "approved")
        t = send_back(s, feedback_type="reroute", demand="再看一路（占位）",
                      reason="复核发现疑点（占位）",
                      by="reviewer（占位）", at=NOW)
        rework_return(s, t["ticket_id"], route_from="路 A（占位）",
                      route_to="路 C（占位）", by="a", at=NOW)
        self.assertEqual(s["status"], "in_review")
        self.assertEqual(s["versions"][-1]["status"], "in_review")

    def test_send_back_requires_reason(self):
        s = fresh()
        with self.assertRaises(BenchError):
            send_back(s, feedback_type="reroute", demand="换路（占位）",
                      reason="", by="r")

    def test_send_back_bad_type_rejected(self):
        s = fresh()
        with self.assertRaises(BenchError):
            send_back(s, feedback_type="force_switch",
                      demand="x", reason="r", by="r")

    def test_close_before_return_rejected(self):
        s = fresh()
        t = send_back(s, feedback_type="add_note", demand="补说明（占位）",
                      reason="缺依据（占位）", by="r", at=NOW)
        with self.assertRaises(BenchError):
            close_feedback(s, t["ticket_id"], by="r")


class TestDecide(unittest.TestCase):
    def test_approve(self):
        s = fresh()
        gate = decide(s, decision="approve", by="reviewer（占位）", at=NOW)
        self.assertEqual(gate["decision"], "approve")
        self.assertEqual(s["status"], "approved")

    def test_reject_requires_reason(self):
        s = fresh()
        with self.assertRaises(BenchError):
            decide(s, decision="reject", by="r", reason="", at=NOW)

    def test_reject_archives(self):
        s = fresh()
        gate = decide(s, decision="reject", by="reviewer（占位）",
                      reason="建议不可行（占位）", at=NOW)
        self.assertEqual(gate["decision"], "reject")
        self.assertEqual(s["status"], "rejected")

    def test_decide_blocked_by_open_feedback(self):
        s = fresh()
        send_back(s, feedback_type="add_note", demand="补说明（占位）",
                  reason="缺依据（占位）", by="r", at=NOW)
        rework_return(s, "fb-1", route_from="路 A（占位）",
                      route_to="路 B（占位）", by="a", at=NOW)
        with self.assertRaises(BenchError):
            decide(s, decision="approve", by="r", at=NOW)

    def test_decide_send_back_redirects_to_send_back(self):
        s = fresh()
        with self.assertRaises(BenchError):
            decide(s, decision="send_back", by="r", reason="x", at=NOW)


class TestOutlet(unittest.TestCase):
    def test_outlet_blocks_all_non_approved(self):
        pending = fresh()
        sent = fresh()
        send_back(sent, feedback_type="reroute", demand="换路（占位）",
                  reason="复核（占位）", by="r", at=NOW)
        rejected = fresh()
        decide(rejected, decision="reject", by="r",
               reason="不可行（占位）", at=NOW)
        ok = fresh()
        decide(ok, decision="approve", by="r", at=NOW)
        out = outlet([pending, sent, rejected, ok])
        self.assertEqual(len(out["passed"]), 1)
        self.assertEqual(out["passed"][0]["suggestion_id"], "sug-001")
        self.assertEqual(out["blocked_count"], 3)
        self.assertEqual({b["status"] for b in out["blocked"]},
                         {"in_review", "sent_back", "rejected"})


class TestLedger(unittest.TestCase):
    def test_jsonl_append_and_latest(self):
        with tempfile.TemporaryDirectory() as d:
            led = SupervisionLedger(Path(d) / "sup.jsonl")
            s = fresh()
            led.record(s, {"kind": "reported", "by": "a", "at": NOW})
            led.record(s, {"kind": "annotation", "by": "r", "at": NOW})
            rows = (Path(d) / "sup.jsonl").read_text(
                encoding="utf-8").strip().split("\n")
            self.assertEqual(len(rows), 2)
            self.assertEqual(json.loads(rows[0])["kind"], "reported")
            self.assertEqual(led.latest_for("sug-001")["kind"],
                             "annotation")

    def test_ledger_missing_fields_rejected(self):
        led = SupervisionLedger()
        s = fresh()
        with self.assertRaises(BenchError):
            led.record(s, {"kind": "reported", "by": "a"})


class TestVocabularyAndNetwork(unittest.TestCase):
    def test_vocabularies_closed(self):
        self.assertEqual(set(SUGGESTION_STATUSES),
                         {"draft", "in_review", "approved", "sent_back",
                          "rejected"})
        self.assertEqual(set(FEEDBACK_TYPES),
                         {"reroute", "keep_route", "add_note"})
        self.assertEqual(set(DECISIONS), {"approve", "send_back", "reject"})
        self.assertIn("REASON_REQUIRED", ERROR_CODES)

    def test_no_route_switch_execution_path(self):
        src = (MOD_DIR / "backend_supervision_bench.py").read_text(
            encoding="utf-8")
        for token in ("switch_route(", "apply_route(", "def execute",
                      "def apply_switch", "def do_switch"):
            self.assertNotIn(token, src)

    def test_no_human_edit_entry(self):
        src = (MOD_DIR / "backend_supervision_bench.py").read_text(
            encoding="utf-8")
        self.assertNotIn("def update_suggestion", src)
        self.assertNotIn("def edit_suggestion", src)

    def test_zero_network_ast(self):
        src = (MOD_DIR / "backend_supervision_bench.py").read_text(
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


if __name__ == "__main__":
    unittest.main()
