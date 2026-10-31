# -*- coding: utf-8 -*-
"""PRD-258/T4 自测（trend #1293）：误报白名单与复核——可审计。

运行：python3 -m pytest tests/test_prd258_t4_whitelist.py -q

覆盖（issue #1293 验收口径 + T1 §3.4）：
  合法添加：reason/reviewer 齐全 → 登记 + audit_log 留痕 + 默认 90 天保质期；
  缺 reason / 缺 reviewer → ValueError（无人复核不得豁免）；
  过期失效：expires_at 一过 check 即 False（复核有保质期）；
  命中放行留痕：blocked 报告命中单条规则 → 放行 + whitelist_applied 留痕；
  不命中不放行：rule_id 或 scope 不匹配 → 仍结构性拦截；
  部分豁免仍拦：另一条未豁免发现仍达 HIGH → 维持 blocked（白名单不豁免整个报告）。

红线：HIGH+ 默认不接流量不变——白名单是显式人工复核例外且留痕可审计。
虚构纪律：内容/密钥样例全虚构占位；零网络。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.plugin_gate import scan_plugin  # noqa: E402
from open_free_router.plugin_gate_gate import (  # noqa: E402
    PluginGate, plugin_hash, stamp_hash,
)
from open_free_router.plugin_whitelist import WhitelistManager, apply_whitelist  # noqa: E402

NOW = 1_700_000_000.0
DAY = 24 * 60 * 60

DIRTY = {"kind": "backend-adapter", "name": "带密钥适配器", "version": "0.1.0",
         "code": "api_key = 'sk-placeholder0123456789'\n"}


def _dirty_report() -> dict:
    r = scan_plugin(kind="backend-adapter", name="带密钥适配器", version="0.1.0",
                    code=DIRTY["code"], declared_license="MIT", verified_license="MIT")
    return stamp_hash(r, DIRTY)


class TestWhitelistManager(unittest.TestCase):
    def test_add_with_reason_and_reviewer_audited(self):
        wl = WhitelistManager()
        entry = wl.add("B-2-01", "带密钥适配器", reason="样例占位密钥属测试夹具误报",
                       reviewer="复核人甲", now=NOW)
        self.assertEqual(entry["expires_at"], NOW + 90 * DAY)     # 默认 90 天
        self.assertTrue(wl.check("B-2-01", "带密钥适配器", now=NOW))
        log = wl.audit_log_view()
        self.assertEqual(len(log), 1)                             # 审计留痕
        self.assertEqual(log[0]["action"], "add")
        self.assertEqual(log[0]["reviewer"], "复核人甲")
        self.assertIn("误报", log[0]["reason"])

    def test_missing_reason_or_reviewer_rejected(self):
        wl = WhitelistManager()
        with self.assertRaises(ValueError):
            wl.add("B-2-01", "带密钥适配器", reason="  ", reviewer="复核人甲")  # 缺 reason
        with self.assertRaises(ValueError):
            wl.add("B-2-01", "带密钥适配器", reason="有依据", reviewer="")       # 缺 reviewer
        self.assertEqual(wl.audit_log_view(), [])                 # 拒绝不留任何登记

    def test_expired_entry_no_longer_hits(self):
        wl = WhitelistManager()
        wl.add("B-2-01", "带密钥适配器", reason="临时豁免", reviewer="复核人乙",
               expires_days=1, now=NOW)
        self.assertTrue(wl.check("B-2-01", "带密钥适配器", now=NOW + 0.5 * DAY))
        self.assertFalse(wl.check("B-2-01", "带密钥适配器", now=NOW + 1.5 * DAY))  # 过期失效


class TestGateWhitelistIntegration(unittest.TestCase):
    def test_matched_rule_released_with_trail(self):
        wl = WhitelistManager()
        wl.add("B-2-01", "带密钥适配器", reason="占位密钥误报", reviewer="复核人丙", now=NOW)
        gate = PluginGate(whitelist=wl)
        report = _dirty_report()
        verdict = gate.admit(DIRTY, report, plugin_hash(DIRTY), now=NOW)
        self.assertTrue(verdict["register"])                      # 命中单条规则 → 放行
        self.assertEqual(verdict["reason"], "whitelisted")
        applied = verdict["whitelist_applied"]
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["rule_id"], "B-2-01")
        self.assertEqual(applied[0]["waived_by"], "复核人丙")      # 留痕：谁豁免的
        status = gate.gate_status("带密钥适配器")
        self.assertTrue(status["registered"])
        self.assertEqual(status["whitelist_applied"][0]["rule_id"], "B-2-01")

    def test_unmatched_still_blocked(self):
        wl = WhitelistManager()
        wl.add("B-3-01", "带密钥适配器", reason="规则不匹配", reviewer="复核人丁", now=NOW)
        wl.add("B-2-01", "另一个插件", reason="范围不匹配", reviewer="复核人丁", now=NOW)
        gate = PluginGate(whitelist=wl)
        verdict = gate.admit(DIRTY, _dirty_report(), plugin_hash(DIRTY), now=NOW)
        self.assertFalse(verdict["register"])                     # 不命中 → 不放行
        self.assertEqual(verdict["reason"], "blocked")
        self.assertEqual(gate.isolation_list(), ["带密钥适配器"])

    def test_partial_waive_still_blocked(self):
        # 双 HIGH：明文密钥 + license 不符；只豁免其一 → 另一条仍结构性拦截
        two_high = scan_plugin(kind="backend-adapter", name="双高插件", version="0.1.0",
                               code=DIRTY["code"], declared_license="MIT",
                               verified_license="Apache-2.0")
        two_high = stamp_hash(two_high, DIRTY)
        self.assertEqual(two_high["decision"], "blocked")
        wl = WhitelistManager()
        wl.add("B-2-01", "双高插件", reason="密钥误报", reviewer="复核人戊", now=NOW)
        gate = PluginGate(whitelist=wl)
        verdict = gate.admit({"name": "双高插件", "kind": "backend-adapter"},
                             two_high, plugin_hash(DIRTY), now=NOW)
        self.assertFalse(verdict["register"])                     # 剩余 B-4-02 仍 HIGH
        self.assertEqual(verdict["reason"], "blocked")
        self.assertEqual(len(verdict["whitelist_applied"]), 1)    # 豁免留痕仍在
        self.assertIn("白名单不豁免整个报告", verdict["message"])

    def test_apply_whitelist_untouched_report_and_admitted(self):
        wl = WhitelistManager()
        wl.add("B-2-01", "带密钥适配器", reason="误报", reviewer="复核人己", now=NOW)
        report = _dirty_report()
        original_decision = report["decision"]
        reviewed = apply_whitelist(report, "带密钥适配器", wl, now=NOW)
        self.assertEqual(report["decision"], original_decision)   # 传入报告不被改动
        self.assertEqual(reviewed["decision"], "admitted")
        admitted = scan_plugin(kind="backend-adapter", name="干净插件", version="0.1.0",
                               code="x = 1\n", declared_license="MIT",
                               verified_license="MIT")
        self.assertIs(apply_whitelist(admitted, "干净插件", wl), admitted)  # 非 blocked 原样


if __name__ == "__main__":
    unittest.main()
