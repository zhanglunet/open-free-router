# -*- coding: utf-8 -*-
"""PRD-364/T5 测试（trend #1643 的 T5 环节）：换路推荐收件箱验收试运行。

运行：.venv/bin/python -m pytest tests/test_prd364_t5_reroute_inbox.py -q
（仓库根执行）

验收（T1 §5 T5 行「样例回放+文档无密钥」）：
  全链周期演练：双源只读汇聚（推荐换路项+回填破路复核）→ 入箱 → 人批
  → 人拒留痕 → 重复裁决演练（INBOX_DOUBLE_DECIDE 显形）→ AUTO_EXEC
  演练（config 载荷 INBOX_AUTO_EXEC_ATTEMPT 显形拒绝——零配置写，配置
  生效走既有显式 reload 流程）；两遍全链周期产物逐字节一致（确定性可
  复现）；账本 append-only；零配置写行为复验（批/拒返回值零配置写/路由
  切换载荷键）；链复跑：T2/T3/T4 测试子进程复跑 exit 0（链上仍绿实测，
  含 T3 ERROR_CODES 订正后全链）；文档无密钥：T1 设计文档拼接 needle
  扫描+占位符纪律。零网络；全合成占位。
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from open_free_router.reroute_inbox import (  # noqa: E402
    InboxError,
    InboxLedger,
    collect,
)

NOW = "2026-11-11T09:00:00Z"
RECS = [{"source_ref": "routing-replay-pack/v1（占位）",
         "snapshot_digest": "sha16:snap（占位）",
         "recommendation": "backend-a → backend-b（占位）",
         "reason": "健康+配额最优（占位）", "recommended_at": NOW}]
BROKEN = [{"event_index": 0, "snapshot_digest": "sha16:snap2（占位）",
           "detail": "双失败（占位）"}]


def _fresh_cycle():
    items = collect(recommendations=RECS, fallback_broken=BROKEN, at=NOW)
    led = InboxLedger()
    led.add(items)
    led.approve(items[0]["item_id"], by="operator（占位）", at=NOW)
    led.reject(items[1]["item_id"], by="operator（占位）", at=NOW,
               reason="不换（占位）")
    return items, led


class TestFullCycleDrill(unittest.TestCase):
    def test_cycle_deterministic_twice(self):
        """全链周期（收→批→拒）两遍：条目/events/状态逐字节一致。"""
        items1, led1 = _fresh_cycle()
        items2, led2 = _fresh_cycle()
        self.assertEqual(json.dumps(items1, sort_keys=True),
                         json.dumps(items2, sort_keys=True))
        self.assertEqual(json.dumps(led1.events, sort_keys=True),
                         json.dumps(led2.events, sort_keys=True))
        states = [it["state"] for it in led1.view()]
        self.assertEqual(sorted(states), ["approved", "rejected"])

    def test_reject_trace_and_zero_config_keys(self):
        """拒也留痕；批/拒返回值零配置写/路由切换键（R1 行为复验）。"""
        items, led = _fresh_cycle()
        rejected = next(it for it in led.view()
                        if it["item_id"] == items[1]["item_id"])
        self.assertEqual(rejected["reject_reason"], "不换（占位）")
        fresh = InboxLedger()
        fresh.add(collect(recommendations=RECS, fallback_broken=BROKEN,
                          at=NOW))
        approved = fresh.approve(items[0]["item_id"], by="operator（占位）",
                                 at=NOW)
        for key in ("config_written", "route_switched", "reloaded"):
            self.assertNotIn(key, approved)


class TestFailureDrills(unittest.TestCase):
    def test_double_decide_visible(self):
        """重复裁决演练：INBOX_DOUBLE_DECIDE 显形拒绝。"""
        items, led = _fresh_cycle()
        with self.assertRaisesRegex(InboxError, "INBOX_DOUBLE_DECIDE"):
            led.approve(items[0]["item_id"], by="operator（占位）", at=NOW)

    def test_auto_exec_payload_rejected(self):
        """AUTO_EXEC 演练：config 载荷 → INBOX_AUTO_EXEC_ATTEMPT 显形拒绝
        （零配置写——配置生效走既有显式 reload 流程）。"""
        items, led = _fresh_cycle()
        with self.assertRaisesRegex(InboxError, "INBOX_AUTO_EXEC_ATTEMPT"):
            led.approve(items[1]["item_id"], by="operator（占位）", at=NOW,
                        payload={"config": {"route": "b（占位）"}})

    def test_ledger_append_only_through_drills(self):
        """账本 append-only：全演练流程后 events 只增不删。"""
        _, led1 = _fresh_cycle()
        n0 = len(led1.events)
        items2, led2 = _fresh_cycle()
        try:
            led2.approve(items2[0]["item_id"], by="operator（占位）", at=NOW)
        except InboxError:
            pass   # 重复裁决被拒——events 不增
        self.assertEqual(len(led2.events), n0)


class TestChainRerun(unittest.TestCase):
    def test_t2_t3_t4_rerun_green(self):
        """链复跑：T2/T3/T4 测试子进程复跑 exit 0（链上仍绿实测，含 T3
        ERROR_CODES 订正后全链）。"""
        venv_py = REPO / ".venv" / "bin" / "python"
        py = str(venv_py) if venv_py.exists() else sys.executable
        for test_file in ("test_prd364_t2_reroute_inbox.py",
                          "test_prd364_t3_reroute_inbox_gates.py",
                          "test_prd364_t4_reroute_inbox_boundary.py"):
            proc = subprocess.run(
                [py, "-m", "pytest", "-q",
                 str(REPO / "tests" / test_file)],
                capture_output=True, timeout=300)
            self.assertEqual(proc.returncode, 0,
                             f"{test_file} 复跑失败：{proc.stdout[-400:]}")


class TestDocNoSecrets(unittest.TestCase):
    def test_t1_design_doc_no_secrets(self):
        """文档无密钥：T1 设计文档拼接 needle 扫描+占位符纪律。"""
        doc = next(REPO.glob("docs/plans/2026-11-11-prd-364-*.md"))
        text = doc.read_text(encoding="utf-8").lower()
        for needle in ("sk-" + "live", "passw" + "ord", "api_k" + "ey"):
            self.assertNotIn(needle, text, f"设计文档出现密钥词 {needle}")
        self.assertIn("占位", text)


if __name__ == "__main__":
    unittest.main()
