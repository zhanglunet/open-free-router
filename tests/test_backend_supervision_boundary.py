# -*- coding: utf-8 -*-
"""PRD-447/T4 测试（trend #2158 的 T4 环节）：后端监督台划界断言。
运行：.venv/bin/python -m pytest tests/test_backend_supervision_boundary.py -q
覆盖（T1 §2.7 + §5 红线）：回路内四种动作放行/五条越界各有归属码/
未知与残缺动作 fail-closed/外来认领冒充被拦/学习网关只出推荐不自动执行/
人不下场改建议/放行须先过界再过闸/越界优先于闸门/放行与拦截审计留痕/
词表封闭/零网络/无路由切换执行路径/无人改建议入口/划界层自身不授予点头。
零网络；全合成占位。
"""
import ast
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
MOD_DIR = Path(__file__).resolve().parents[1] / "src/open_free_router"
sys.path.insert(0, str(MOD_DIR))

from backend_supervision_bench import (  # noqa: E402
    BenchError, SupervisionLedger, decide, report_suggestion,
)
from backend_supervision_boundary import (  # noqa: E402
    BOUNDARY_AUDIT_KINDS, BOUNDARY_CODES, BOUNDARY_LOOP,
    BOUNDARY_SCHEMA, IN_SCOPE_ACTIONS, assert_boundary,
    check_suggestion_scope, classify_action, guard_admission,
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


class TestClassifyAction(unittest.TestCase):
    def test_in_scope_actions_pass(self):
        for kind in ("watch_progress", "add_annotation",
                     "send_back_feedback", "decide_approval"):
            v = classify_action({"kind": kind})
            self.assertTrue(v["in_scope"], kind)
            self.assertEqual(v["boundary"], BOUNDARY_LOOP)
            self.assertEqual(v["reasons"], [])

    def test_review_config_out_of_scope_prd424(self):
        v = classify_action({"kind": "review_config"})
        self.assertFalse(v["in_scope"])
        self.assertEqual(v["boundary"], "prd-424")
        self.assertEqual(codes(v), ["REVIEW_CONFIG"])

    def test_verify_anomaly_out_of_scope_prd378(self):
        v = classify_action({"kind": "verify_anomaly"})
        self.assertFalse(v["in_scope"])
        self.assertEqual(v["boundary"], "prd-378")
        self.assertEqual(codes(v), ["VERIFY_ANOMALY"])

    def test_inbox_dispatch_out_of_scope_prd364(self):
        v = classify_action({"kind": "inbox_dispatch"})
        self.assertFalse(v["in_scope"])
        self.assertEqual(v["boundary"], "prd-364")
        self.assertEqual(codes(v), ["INBOX_DISPATCH"])

    def test_edit_suggestion_out_of_scope(self):
        v = classify_action({"kind": "edit_suggestion"})
        self.assertFalse(v["in_scope"])
        self.assertEqual(v["boundary"], "human-does-not-edit")
        self.assertEqual(codes(v), ["EDIT_SUGGESTION"])

    def test_execute_reroute_out_of_scope(self):
        v = classify_action({"kind": "execute_reroute"})
        self.assertFalse(v["in_scope"])
        self.assertEqual(v["boundary"], "gateway-recommendation-only")
        self.assertEqual(codes(v), ["EXECUTE_REROUTE"])

    def test_unknown_kind_out_of_scope(self):
        v = classify_action({"kind": "publish_route"})
        self.assertFalse(v["in_scope"])
        self.assertEqual(v["boundary"], "unknown")
        self.assertEqual(codes(v), ["UNKNOWN_ACTION"])

    def test_malformed_action_fail_closed_not_raised(self):
        for bad in (None, "x", {}, {"kind": ""}, {"kind": 123},
                    {"no_kind": "watch_progress"}):
            v = classify_action(bad)
            self.assertFalse(v["in_scope"])
            self.assertEqual(codes(v), ["UNKNOWN_ACTION"])


class TestAssertBoundary(unittest.TestCase):
    def test_in_scope_passes(self):
        v = assert_boundary({"kind": "add_annotation"})
        self.assertTrue(v["in_scope"])

    def test_out_of_scope_raises(self):
        for kind in ("review_config", "verify_anomaly", "inbox_dispatch",
                     "edit_suggestion", "execute_reroute"):
            with self.assertRaises(BenchError) as cm:
                assert_boundary({"kind": kind})
            self.assertIn("OUT_OF_SCOPE", str(cm.exception))


class TestCheckSuggestionScope(unittest.TestCase):
    def test_clean_suggestion_passes(self):
        v = check_suggestion_scope(approved())
        self.assertTrue(v["in_scope"])
        self.assertEqual(v["reasons"], [])

    def test_config_review_claim_blocked(self):
        s = approved()
        s["config_reviewed"] = True
        v = check_suggestion_scope(s)
        self.assertFalse(v["in_scope"])
        self.assertIn("REVIEW_CONFIG", codes(v))
        self.assertEqual(v["reasons"][0]["target"], "config_reviewed")

    def test_anomaly_claim_blocked(self):
        s = approved()
        s["anomaly_verified"] = True
        v = check_suggestion_scope(s)
        self.assertFalse(v["in_scope"])
        self.assertIn("VERIFY_ANOMALY", codes(v))

    def test_inbox_claim_blocked(self):
        s = approved()
        s["inbox_dispatched"] = True
        v = check_suggestion_scope(s)
        self.assertFalse(v["in_scope"])
        self.assertIn("INBOX_DISPATCH", codes(v))

    def test_edit_claim_blocked(self):
        s = approved()
        s["suggestion_edited"] = True
        v = check_suggestion_scope(s)
        self.assertFalse(v["in_scope"])
        self.assertIn("EDIT_SUGGESTION", codes(v))

    def test_route_switched_claim_blocked(self):
        s = approved()
        s["route_switched"] = True
        v = check_suggestion_scope(s)
        self.assertFalse(v["in_scope"])
        self.assertIn("EXECUTE_REROUTE", codes(v))

    def test_claims_subdict_flagged(self):
        s = approved()
        s["claims"] = {"anomaly_verified": True}
        v = check_suggestion_scope(s)
        self.assertFalse(v["in_scope"])
        self.assertIn("VERIFY_ANOMALY", codes(v))

    def test_falsy_claim_is_not_a_claim(self):
        s = approved()
        s["config_reviewed"] = False
        s["route_switched"] = None
        v = check_suggestion_scope(s)
        self.assertTrue(v["in_scope"])

    def test_malformed_suggestion_blocked(self):
        for bad in (None, "x", {}, {"schema": "other/v9",
                                     "status": "approved"}):
            v = check_suggestion_scope(bad)
            self.assertFalse(v["in_scope"])
            self.assertIn("BAD_SUGGESTION", codes(v))


class TestGuardAdmission(unittest.TestCase):
    def test_in_scope_approved_admitted(self):
        out = guard_admission({"kind": "decide_approval"}, approved(),
                              by=BY, at=NOW)
        self.assertEqual(out["admitted"]["suggestion_id"], "sug-001")
        self.assertTrue(out["verdict"]["allowed"])
        self.assertEqual(out["boundary"], BOUNDARY_LOOP)

    def test_out_of_scope_blocked_even_if_approved(self):
        # 已点头也不能把监督台当路由执行器用：越界先于闸门被拦。
        with self.assertRaises(BenchError) as cm:
            guard_admission({"kind": "execute_reroute"}, approved(),
                            by=BY, at=NOW)
        self.assertIn("OUT_OF_SCOPE", str(cm.exception))
        self.assertIn("EXECUTE_REROUTE", str(cm.exception))

    def test_in_scope_unapproved_blocked_by_gate(self):
        with self.assertRaises(BenchError) as cm:
            guard_admission({"kind": "watch_progress"}, fresh(),
                            by=BY, at=NOW)
        self.assertIn("GATE_BLOCKED", str(cm.exception))

    def test_out_of_scope_takes_precedence_over_gate(self):
        with self.assertRaises(BenchError) as cm:
            guard_admission({"kind": "review_config"}, fresh(),
                            by=BY, at=NOW)
        self.assertIn("OUT_OF_SCOPE", str(cm.exception))
        self.assertNotIn("GATE_BLOCKED", str(cm.exception))

    def test_foreign_claim_blocks_admission(self):
        s = approved()
        s["route_switched"] = True
        with self.assertRaises(BenchError) as cm:
            guard_admission({"kind": "decide_approval"}, s, by=BY, at=NOW)
        self.assertIn("OUT_OF_SCOPE", str(cm.exception))
        self.assertIn("EXECUTE_REROUTE", str(cm.exception))

    def test_admission_audits_both_outcomes(self):
        with tempfile.TemporaryDirectory() as d:
            led = SupervisionLedger(Path(d) / "sup.jsonl")
            guard_admission({"kind": "watch_progress"}, approved(),
                            ledger=led, by=BY, at=NOW)
            with self.assertRaises(BenchError):
                guard_admission({"kind": "inbox_dispatch"}, approved(),
                                ledger=led, by=BY, at=NOW)
            kinds = [e["kind"] for e in led.entries()]
            self.assertEqual(kinds, ["boundary_passed", "boundary_blocked"])
            self.assertEqual(led.entries()[1]["boundary"], "prd-364")

    def test_admitted_returns_copy_not_alias(self):
        out = guard_admission({"kind": "decide_approval"}, approved(),
                              by=BY, at=NOW)
        out["admitted"]["status"] = "rejected"
        self.assertEqual(approved()["status"], "approved")


class TestVocabularyNetworkAndRedLines(unittest.TestCase):
    def test_vocabularies_closed(self):
        self.assertEqual(set(IN_SCOPE_ACTIONS),
                         {"watch_progress", "add_annotation",
                          "send_back_feedback", "decide_approval"})
        self.assertEqual(set(BOUNDARY_CODES),
                         {"REVIEW_CONFIG", "VERIFY_ANOMALY", "INBOX_DISPATCH",
                          "EDIT_SUGGESTION", "EXECUTE_REROUTE",
                          "UNKNOWN_ACTION", "FOREIGN_CLAIM",
                          "BAD_SUGGESTION"})
        self.assertEqual(set(BOUNDARY_AUDIT_KINDS),
                         {"boundary_passed", "boundary_blocked"})
        self.assertEqual(BOUNDARY_SCHEMA,
                         "backend-supervision-boundary/v1")

    def test_zero_network_ast(self):
        src = (MOD_DIR / "backend_supervision_boundary.py").read_text(
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
        # 学习网关只出推荐不自动执行：划界层模块不含任何路由切换执行路径。
        src = (MOD_DIR / "backend_supervision_boundary.py").read_text(
            encoding="utf-8")
        for token in ("switch_route", "apply_route", "set_route",
                      "do_switch", "execute(", "perform_switch"):
            self.assertNotIn(token, src)

    def test_no_suggestion_edit_entry(self):
        # 人只批注和裁决，不下场改建议：划界层模块不提供改建议内容的入口。
        src = (MOD_DIR / "backend_supervision_boundary.py").read_text(
            encoding="utf-8")
        for token in ("route_to\" ] =", "route_to'] =", "update_suggestion"):
            self.assertNotIn(token, src)

    def test_boundary_grants_nothing_out_of_scope(self):
        # 划界层自身不授予点头：只有动作词表里出现邻仓名字（作越界映射），
        # 没有任何执行它们的入口。
        src = (MOD_DIR / "backend_supervision_boundary.py").read_text(
            encoding="utf-8")
        self.assertNotIn('"] = "approved"', src)
        self.assertNotIn("decide(", src)
        self.assertNotIn("def review_config", src)
        self.assertNotIn("def verify_anomaly", src)
        self.assertNotIn("def inbox_dispatch", src)
        self.assertNotIn("def execute_reroute", src)


if __name__ == "__main__":
    unittest.main()
