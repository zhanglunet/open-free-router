# -*- coding: utf-8 -*-
"""PRD-201/T5 自测（trend #1009）：验收试运行——一周数据回放，结论可复核。

运行：python3 -m unittest tests.test_prd201_t5_acceptance -v（仓库根执行）

覆盖（验收「一周数据回放，结论可复核」）：
  合成一周（7×24 小时）trace 流水 → rollup 小时桶 → board_summary 一周窗
  结论可复核：分布/失败 Top/成本曲线逐项对账（同 T3 口径扩展到 168 桶）
  评估集漂移：基线 vs 漂移轮（成功率下降）→ drift 报告生成且可序列化
虚构纪律：全合成；零网络。
"""
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.route_board import board_summary, rollup_traces, trace_drawer  # noqa: E402
from open_free_router.route_eval_drift import run_and_compare  # noqa: E402
from open_free_router.route_obs import RouteObservatory, RouteTrace  # noqa: E402

NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)


def week_traces():
    """一周合成 trace：每天 24 条，成功率 0.9，双后端分布。"""
    out = []
    for day in range(7):
        for hour in range(24):
            at = NOW - timedelta(days=7 - day) + timedelta(hours=hour)
            backend = "prov-a" if (day + hour) % 2 == 0 else "prov-b"
            status = "failed" if hour == 0 else "ok"
            out.append({"route_id": "r", "backend_id": backend,
                        "model": "m-1" if backend == "prov-a" else "m-2",
                        "request_id": f"req-{day}-{hour}",
                        "latency_ms": 100.0 + hour, "cost_usd": 0.01,
                        "status": status,
                        "failure_class": "timeout" if status == "failed" else None,
                        "at": at.isoformat(timespec="seconds")})
    return out


class WeekReplayTests(unittest.TestCase):
    def test_week_board_consistency(self):
        traces = week_traces()
        self.assertEqual(len(traces), 168)
        rolls = rollup_traces(traces)
        self.assertEqual(sum(r["requests"] for r in rolls), 168)
        board = board_summary(traces, window="7d", now=NOW)
        self.assertEqual(board["total_requests"], 168)
        self.assertEqual(sum(r["requests"] for r in board["route_distribution"]), 168)
        self.assertEqual(sum(f["count"] for f in board["failure_top"]), 7)
        curve_cost = round(sum(c["cost_units"] for c in board["cost_curve"]), 4)
        raw_cost = round(sum(float(t["cost_usd"]) for t in traces), 4)
        self.assertEqual(curve_cost, raw_cost)
        json.dumps(board, ensure_ascii=False)  # 可序列化

    def test_drawer_point_query(self):
        traces = week_traces()
        drawer = trace_drawer(traces, window="7d", provider="prov-a", limit=50, now=NOW)
        self.assertTrue(all(t["backend_id"] == "prov-a" for t in drawer["traces"]))
        self.assertLessEqual(len(drawer["traces"]), 50)

    def test_eval_drift_report_generatable(self):
        obs = RouteObservatory()
        prompts = [f"p{i}" for i in range(1, 21)]

        def make_predict(pass_n):
            def predict(p):
                ok = int(p[1:]) <= pass_n
                return {"route_id": "r", "status": "ok" if ok else "failed",
                        "latency_ms": 100.0, "cost_usd": 0.01}
            return predict

        base = run_and_compare(obs, prompts, make_predict(20), "week-replay")
        drifted = run_and_compare(obs, prompts, make_predict(17), "week-replay",
                                  previous_run=base)
        self.assertEqual(base["drift"]["verdict"], "no_drift")
        self.assertEqual(drifted["drift"]["verdict"], "drift")
        report = {"base": base, "drift": drifted["drift"]}
        json.dumps(report, ensure_ascii=False)  # 报告可落盘


if __name__ == "__main__":
    unittest.main(verbosity=2)
