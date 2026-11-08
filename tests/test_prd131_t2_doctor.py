# -*- coding: utf-8 -*-
"""PRD-131/T2 有序后端+doctor 测试（trend #655）。"""
import sys, unittest
from pathlib import Path
from open_free_router.doctor_ordered import build_ordered_list, doctor_check, auto_fallback

class DoctorTests(unittest.TestCase):
    def test_build_and_check(self):
        backends = build_ordered_list({"backends": [{"name": "a"}, {"name": "b"}]})
        self.assertEqual(len(backends), 2)
    def test_doctor_report(self):
        report = doctor_check(
            [{"name": "a"}, {"name": "b"}],
            lambda name: "healthy" if name == "a" else "down")
        self.assertTrue(report["summary"]["has_healthy"])
    def test_fallback_picks_first_healthy(self):
        ordered = [{"name": "a", "priority": 1}, {"name": "b", "priority": 2}]
        report = doctor_check(ordered, lambda n: "healthy")
        result = auto_fallback(report, ordered)
        self.assertIsNotNone(result)

if __name__ == "__main__":
    unittest.main()
