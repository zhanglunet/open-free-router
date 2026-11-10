# -*- coding: utf-8 -*-
"""PRD-364/T2 测试（trend #1643 的 T2 环节）：换路批准收件箱。

运行：.venv/bin/python -m pytest tests/test_prd364_t2_reroute_inbox.py -q

覆盖（T1 §3.1/§3.2/§4 + R1–R4）：collect 两源只读汇聚（与 PRD-350 重放包
指纹同源/PRD-336 fallback_broken 自动入箱——五件套缺一 INBOX_ITEM_INCOMPLETE
显形拒绝入箱）；InboxLedger 状态机（approve/reject 人批留痕/重复裁决/未知 id/
AUTO_EXEC 携带待生效配置载荷拒绝——零配置写 R1 生效走既有显式 reload/推荐
过期 STALE 显形拒绝须重新推荐 §6.1）；append-only；view kind 过滤；AST 零
网络零子进程。零网络；全合成占位。
"""
import ast
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "open_free_router"))

from reroute_inbox import (  # noqa: E402
    ERROR_CODES,
    ITEM_KINDS,
    InboxError,
    InboxLedger,
    collect,
)

NOW = "2026-11-11T09:00:00Z"
LATER = "2026-11-11T12:00:00Z"
RECS = [{"source_ref": "routing-replay-pack/v1（占位）",
         "snapshot_digest": "sha16:snap（占位）",
         "recommendation": "backend-a → backend-b（占位）",
         "reason": "健康+配额最优（占位）", "recommended_at": NOW}]
BROKEN = [{"event_index": 0, "snapshot_digest": "sha16:snap2（占位）",
           "detail": "双失败（占位）"}]


class TestCollect(unittest.TestCase):
    def test_two_sources_closed_vocab(self):
        items = collect(recommendations=RECS, fallback_broken=BROKEN, at=NOW)
        self.assertEqual([it["kind"] for it in items],
                         ["reroute_recommendation", "fallback_broken_review"])
        self.assertEqual(set(ITEM_KINDS),
                         {"reroute_recommendation", "fallback_broken_review"})

    def test_snapshot_same_source_as_350(self):
        """R3：snapshot_digest 与 PRD-350 重放包指纹同源（调用方注入）。"""
        items = collect(recommendations=RECS, at=NOW)
        self.assertEqual(items[0]["snapshot_digest"], "sha16:snap（占位）")
        self.assertEqual(items[0]["recommended_at"], NOW)

    def test_incomplete_raises_visible(self):
        """R3：snapshot_digest 缺失 → INBOX_ITEM_INCOMPLETE 显形拒绝入箱。"""
        with self.assertRaisesRegex(InboxError, "INBOX_ITEM_INCOMPLETE"):
            collect(recommendations=[{"recommendation": "x（占位）"}], at=NOW)

    def test_fallback_broken_auto_inbox(self):
        """PRD-336 fallback_broken 事件自动入箱供人工介入（§4.2）。"""
        items = collect(fallback_broken=BROKEN, at=NOW)
        self.assertIn("零伪装", items[0]["reason"])


class TestLedger(unittest.TestCase):
    def _ledger(self):
        led = InboxLedger()
        led.add(collect(recommendations=RECS, fallback_broken=BROKEN, at=NOW))
        return led

    def test_approve_reject_state_machine(self):
        led = self._ledger()
        a = led.approve("item-001", by="admin（占位）", at=LATER)
        self.assertEqual(a["state"], "approved")
        r = led.reject("item-002", by="admin（占位）", at=LATER,
                       reason="维持现状（占位）")
        self.assertEqual(r["state"], "rejected")

    def test_double_and_unknown_visible(self):
        led = self._ledger()
        led.approve("item-001", by="a（占位）", at=LATER)
        with self.assertRaisesRegex(InboxError, "INBOX_DOUBLE_DECIDE"):
            led.reject("item-001", by="x（占位）", at=LATER)
        with self.assertRaisesRegex(InboxError, "未知"):
            led.approve("item-999", by="x（占位）", at=LATER)

    def test_auto_exec_config_payload_rejected(self):
        """R1：裁决携带待生效配置载荷 → AUTO_EXEC 显形拒绝（零配置写）。"""
        led = self._ledger()
        with self.assertRaisesRegex(InboxError, "INBOX_AUTO_EXEC_ATTEMPT"):
            led.approve("item-001", by="a（占位）", at=LATER,
                        payload={"config": {"backend": "b（占位）"}})
        with self.assertRaisesRegex(InboxError, "INBOX_AUTO_EXEC_ATTEMPT"):
            led.reject("item-001", by="a（占位）", at=LATER,
                       payload={"reload": True})
        self.assertEqual(led.view()[0]["state"], "pending")   # 零状态变化

    def test_stale_recommendation_rejected(self):
        """§6.1：approve 时间早于推荐时间 → STALE 显形拒绝须重新推荐。"""
        led = self._ledger()
        with self.assertRaisesRegex(InboxError, "INBOX_RECOMMENDATION_STALE"):
            led.approve("item-001", by="a（占位）", at="2026-11-10T00:00:00Z")
        a = led.approve("item-001", by="a（占位）", at=LATER)   # 晚于推荐 OK
        self.assertEqual(a["state"], "approved")

    def test_append_only_and_view_filter(self):
        led = self._ledger()
        led.approve("item-001", by="a（占位）", at=LATER)
        before = [dict(e) for e in led.events]
        led.reject("item-002", by="a（占位）", at=LATER, reason="r（占位）")
        self.assertEqual(led.events[:len(before)], before)
        self.assertEqual(len(led.events), len(before) + 1)
        self.assertEqual(len(led.view("fallback_broken_review")), 1)
        self.assertEqual(len(led.view()), 2)


class TestRedline(unittest.TestCase):
    def test_error_codes_closed(self):
        self.assertEqual(set(ERROR_CODES),
                         {"INBOX_ITEM_INCOMPLETE", "INBOX_DOUBLE_DECIDE",
                          "INBOX_AUTO_EXEC_ATTEMPT",
                          "INBOX_RECOMMENDATION_STALE"})

    def test_ast_zero_network_zero_subprocess(self):
        """R1 AST：零网络零子进程（零配置写导入——生效归既有流程）。"""
        banned = {"urllib", "requests", "socket", "http", "subprocess",
                  "config_write", "reload_config"}
        tree = ast.parse((REPO / "src" / "open_free_router" /
                          "reroute_inbox.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module.split(".")[0]]
            for mod in mods:
                self.assertNotIn(mod, banned)


if __name__ == "__main__":
    unittest.main()
