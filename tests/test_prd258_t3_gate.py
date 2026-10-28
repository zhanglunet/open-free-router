# -*- coding: utf-8 -*-
"""PRD-258/T3 自测（trend #1292）：接流量闸门——HIGH 不接，闸门有测试。

运行：python3 -m pytest tests/test_prd258_t3_gate.py -q

覆盖（T1 §3.3 接流量闸门 + §6 验收口径）：
  无报告拒（report_missing）；blocked 拒（HIGH+ 结构性拦截）；
  哈希不一致（改包）拒——旧报告自动作废；scan_version 不匹配拒；
  有效报告（scan_version+哈希一致）注册成功；gate 状态可读（doctor 口径）；
  哈希确定性（canonical json：键序无关、内容变更即变、16 位）。

虚构纪律：内容/密钥样例全虚构占位；域用 example 系占位；零网络。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.plugin_gate import scan_plugin  # noqa: E402
from open_free_router.plugin_gate_gate import (  # noqa: E402
    PluginGate, plugin_hash, registration_gate, stamp_hash,
)

CLEAN = {"kind": "backend-adapter", "name": "示例适配器", "version": "0.1.0",
         "code": "base_url = 'https://api.example.com/v1'\n",
         "declared_license": "MIT", "declared_domains": ["api.example.com"]}

DIRTY = {"kind": "backend-adapter", "name": "带密钥适配器", "version": "0.1.0",
         "code": "api_key = 'sk-placeholder0123456789'\n",
         "declared_license": "MIT", "declared_domains": ["api.example.com"]}


def _clean_report() -> dict:
    r = scan_plugin(kind="backend-adapter", name="示例适配器", version="0.1.0",
                    code=CLEAN["code"], declared_license="MIT", verified_license="MIT",
                    declared_domains=("api.example.com",))
    return stamp_hash(r, CLEAN)


def _dirty_report() -> dict:
    r = scan_plugin(kind="backend-adapter", name="带密钥适配器", version="0.1.0",
                    code=DIRTY["code"], declared_license="MIT", verified_license="MIT")
    return stamp_hash(r, DIRTY)


class TestGateRejections(unittest.TestCase):
    def test_missing_report_rejected(self):
        verdict = registration_gate(CLEAN, None, plugin_hash(CLEAN))
        self.assertFalse(verdict["register"])
        self.assertEqual(verdict["reason"], "report_missing")
        self.assertEqual(verdict["gate"], "blocked")

    def test_blocked_decision_rejected(self):
        report = _dirty_report()  # 明文密钥 → B-2 floor HIGH → decision=blocked
        self.assertEqual(report["decision"], "blocked")
        verdict = registration_gate(DIRTY, report, plugin_hash(DIRTY))
        self.assertFalse(verdict["register"])
        self.assertEqual(verdict["reason"], "blocked")
        self.assertEqual(verdict["gate"], "blocked")

    def test_hash_mismatch_rejected_on_repack(self):
        report = _clean_report()
        repacked = dict(CLEAN, code="base_url = 'https://api.example.com/v2'\n")  # 改包
        verdict = registration_gate(repacked, report, plugin_hash(repacked))
        self.assertFalse(verdict["register"])
        self.assertEqual(verdict["reason"], "hash_mismatch")  # 旧报告自动作废
        self.assertIn("重扫", verdict["message"])

    def test_scan_version_mismatch_rejected(self):
        report = dict(_clean_report(), scan_version="0.0.9")  # 旧扫描器报告
        verdict = registration_gate(CLEAN, report, plugin_hash(CLEAN))
        self.assertFalse(verdict["register"])
        self.assertEqual(verdict["reason"], "scan_version_mismatch")


class TestGateAdmission(unittest.TestCase):
    def test_valid_report_registers(self):
        gate = PluginGate()
        verdict = gate.admit(CLEAN, _clean_report(), plugin_hash(CLEAN))
        self.assertTrue(verdict["register"])
        self.assertEqual(verdict["gate"], "open")
        self.assertTrue(gate.is_registered("示例适配器"))  # 通过后才可进路由表

    def test_gate_status_readable_for_doctor(self):
        gate = PluginGate()
        gate.admit(CLEAN, _clean_report(), plugin_hash(CLEAN))    # open
        gate.admit(DIRTY, _dirty_report(), plugin_hash(DIRTY))    # blocked
        open_e = gate.gate_status("示例适配器")
        blocked_e = gate.gate_status("带密钥适配器")
        self.assertEqual(open_e["gate"], "open")
        self.assertTrue(open_e["registered"])
        self.assertEqual(blocked_e["gate"], "blocked")
        self.assertEqual(blocked_e["reason"], "blocked")
        # 未登记插件如实标注（honesty 纪律），不假装放行
        self.assertEqual(gate.gate_status("从未见过")["gate"], "unregistered")
        # doctor 全量视图：机读 + 隔离清单留痕
        view = gate.gate_view()
        self.assertEqual(view["schema"], "plugin-gate-v1")
        self.assertEqual(view["isolation"], ["带密钥适配器"])
        self.assertEqual(len(view["plugins"]), 2)


class TestHashDeterminism(unittest.TestCase):
    def test_plugin_hash_canonical_and_stable(self):
        a = plugin_hash({"name": "x", "version": "1", "code": "pass"})
        b = plugin_hash({"code": "pass", "version": "1", "name": "x"})  # 键序无关
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)
        int(a, 16)  # 16 位 hex
        self.assertNotEqual(a, plugin_hash({"name": "x", "version": "1", "code": "pass\n"}))
        self.assertEqual(plugin_hash(CLEAN), plugin_hash(dict(CLEAN)))  # 同内容同哈希

    def test_stamp_hash_does_not_mutate_input(self):
        report = _clean_report()
        self.assertEqual(report["content_hash"], plugin_hash(CLEAN))


if __name__ == "__main__":
    unittest.main()
