# -*- coding: utf-8 -*-
"""PRD-201/T2 自测（trend 前区间 #962 后见）：路由可观测——trace/评估集/视图。"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.route_obs import RouteObservatory, RouteTrace  # noqa: E402


def trace(rid, status="ok", latency=100.0, cost=0.01, fc=None):
    return RouteTrace(route_id=rid, backend_id="stub", model="stub-1",
                       request_id=f"req-{rid}-{status}", latency_ms=latency,
                       cost_usd=cost, status=status, failure_class=fc, at="t")


class TestTrace(unittest.TestCase):
    def test_record_and_view(self):
        obs = RouteObservatory()
        obs.record(trace("r1", "ok", 100, 0.01))
        obs.record(trace("r2", "ok", 200, 0.02))
        text = obs.view()
        self.assertIn("r1", text)
        self.assertIn("r2", text)
        self.assertEqual(len(obs.traces), 2)

    def test_quality_summary(self):
        obs = RouteObservatory()
        for _ in range(3):
            obs.record(trace("r1", "ok", 100, 0.01))
        obs.record(trace("r1", "failed", 500, 0, fc="timeout"))
        s = obs.quality_summary()["r1"]
        self.assertEqual(s["count"], 4)
        self.assertEqual(s["success"], 3)
        self.assertEqual(s["failure_classes"], ["timeout"])

    def test_jsonl_export(self):
        obs = RouteObservatory()
        obs.record(trace("r1"))
        lines = obs.export_jsonl().strip().splitlines()
        self.assertEqual(len(lines), 1)
        json.loads(lines[0])

    def test_zero_trace_empty_view(self):
        obs = RouteObservatory()
        self.assertIn("route_id", obs.view())

    def test_no_network_imports(self):
        import inspect
        src = inspect.getsource(RouteObservatory)
        for banned in ("requests", "urllib", "socket", "http.client"):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
