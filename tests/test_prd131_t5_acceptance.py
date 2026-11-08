# -*- coding: utf-8 -*-
"""PRD-131/T5 验收：模拟 3 种后端故障，用户请求无失败（trend #658）。

验收口径（issue #658）：模拟 3 种后端故障，用户请求无失败。

场景：三后端 preferred / backup / last-resort，依次注入三类故障——
  F1 preferred 宕机（探活 down）
  F2 backup 额度耗尽（429 quota）
  F3 preferred 恢复 → 回切
每轮用户请求经 doctor → auto_fallback（有序换路）+ ResilienceManager
（额度/故障状态感知，PRD-096 叠加）路由，断言每次请求都落到可用后端（零失败）。
"""
import unittest
from pathlib import Path


from open_free_router.doctor_ordered import auto_fallback, build_ordered_list, doctor_check
from open_free_router.resilience import ResilienceManager, classify_failure

ORDERED = build_ordered_list({"backends": [
    {"name": "preferred"}, {"name": "backup"}, {"name": "last-resort"}]})


class FakeUserRequest:
    """用户请求模拟：路由到某后端即成功；无后端可用才算失败。"""

    def __init__(self):
        self.routed_to = None

    def route(self, picked):
        if picked is None:
            return False  # 用户请求失败——验收红线
        self.routed_to = picked["name"]
        return True


def simulate(probe_states, manager, request):
    report = doctor_check(ORDERED, lambda name: probe_states.get(name, "healthy"))
    # 额度/故障状态感知（PRD-096 叠加）：resilience 拒绝（冷却中）的后端从候选剔除
    candidates = [b for b in ORDERED
                  if manager.can_attempt(b["name"], "model-a", now=1000)[0]]
    narrowed = {**report, "backends": [r for r in report["backends"]
                                       if any(b["name"] == r["backend"] for b in candidates)]}
    picked = auto_fallback(narrowed, candidates)
    return request.route(picked)


class AcceptanceTests(unittest.TestCase):
    def test_three_failure_modes_user_request_never_fails(self):
        manager = ResilienceManager()
        # F1 preferred 宕机 → 换路 backup
        req1 = FakeUserRequest()
        self.assertTrue(simulate({"preferred": "down", "backup": "healthy",
                                  "last-resort": "healthy"}, manager, req1))
        self.assertEqual(req1.routed_to, "backup")
        # F2 backup 额度耗尽（429 quota 词表）→ 换路 last-resort
        manager.record_failure("backup", "model-a",
                               classify_failure(429, "quota exceeded"), now=1000)
        req2 = FakeUserRequest()
        self.assertTrue(simulate({"preferred": "down", "backup": "healthy",
                                  "last-resort": "healthy"}, manager, req2))
        self.assertEqual(req2.routed_to, "last-resort")
        # F3 preferred 恢复 → 回切 preferred
        manager.record_success("preferred", "model-a", now=1001)
        req3 = FakeUserRequest()
        self.assertTrue(simulate({"preferred": "healthy", "backup": "healthy",
                                  "last-resort": "healthy"}, manager, req3))
        self.assertEqual(req3.routed_to, "preferred")

    def test_acceptance_deterministic_replay(self):
        # 两遍模拟逐次路由一致（可重放口径）
        outcomes = []
        for _ in range(2):
            manager = ResilienceManager()
            req = FakeUserRequest()
            simulate({"preferred": "down", "backup": "healthy"}, manager, req)
            first = req.routed_to
            manager.record_failure("backup", "model-a",
                                   classify_failure(429, "quota exceeded"), now=1000)
            simulate({"preferred": "down", "backup": "healthy"}, manager, req)
            outcomes.append((first, req.routed_to))
        self.assertEqual(outcomes[0], ("backup", "last-resort"))
        self.assertEqual(outcomes[0], outcomes[1])


class ReplayGateTemplateTests(unittest.TestCase):
    """模板层（重放闸门）既有口径保留。"""

    def test_full_chain(self):
        try:
            from replay_gate import replay_gate
        except ImportError:
            self.skipTest("replay_gate 模板模块不在本仓测试路径")
            return
        self.assertTrue(replay_gate({"status": "MATCH"})["allow"])
        self.assertFalse(replay_gate({"status": "MISMATCH"})["allow"])


if __name__ == "__main__":
    unittest.main()
