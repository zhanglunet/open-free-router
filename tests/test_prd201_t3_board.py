# -*- coding: utf-8 -*-
"""PRD-201/T3 自测（trend #1007）：实时看板——路由分布/失败原因 Top/成本曲线。

运行：python3 -m unittest tests.test_prd201_t3_board -v（仓库根执行）

覆盖（T1 §4/§5 + 验收「看板数据与 trace 一致」）：
  rollup：小时桶维度/度量；p50/p95 最近邻序；失败原因 Top 计数
  看板与 trace 对账：route 分布 requests 合计 == 窗口内 trace 数；
    failure_top 计数 == 原始计数；cost_curve 合计 == 窗口内成本合计
  路由分布：占比/成功率/换路率数值正确
  失败原因 Top：环比 delta（上一窗有/无基线两种）
  窗口语义：1h/24h 之外拒绝；窗口外 trace 不计入
  明细抽屉：维度过滤 + limit 硬上限 200 + matched 计数

虚构纪律：全部合成 trace；零网络。
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.route_board import (  # noqa: E402
    board_summary,
    rollup_traces,
    trace_drawer,
)

NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)


def trace(at: datetime, backend="prov-a", model="m-1", status="ok",
          latency=100.0, cost=0.01, fc=None):
    return {"route_id": "r", "backend_id": backend, "model": model,
            "request_id": f"req-{at.isoformat()}-{status}", "latency_ms": latency,
            "cost_usd": cost, "status": status, "failure_class": fc,
            "at": at.isoformat(timespec="seconds")}


def sample_traces():
    """12 条合成 trace：窗口内 10 条（prov-a 6 / prov-b 4），窗外 2 条。"""
    traces = []
    for i in range(6):
        traces.append(trace(NOW - timedelta(minutes=10 + i), backend="prov-a",
                            model="m-1", status="ok" if i < 5 else "failed",
                            latency=100 + i * 10, cost=0.01,
                            fc="rate_limit" if i == 5 else None))
    for i in range(4):
        traces.append(trace(NOW - timedelta(minutes=20 + i), backend="prov-b",
                            model="m-2", status="failed" if i < 2 else "ok",
                            latency=200 + i * 10, cost=0.02,
                            fc="timeout" if i < 2 else None))
    # 窗外：25h 前 1 条 + 8 天前 1 条
    traces.append(trace(NOW - timedelta(hours=25), backend="prov-a", model="m-1",
                        status="failed", fc="rate_limit"))
    traces.append(trace(NOW - timedelta(days=8), backend="prov-a", model="m-1",
                        status="ok"))
    return traces


class RollupTests(unittest.TestCase):
    def test_hourly_bucket_dimensions_and_metrics(self):
        rolls = rollup_traces(sample_traces())
        in_window = [r for r in rolls if r["bucket_at"] >= (NOW - timedelta(hours=1)).isoformat()]
        self.assertEqual(sum(r["requests"] for r in in_window), 10)  # 窗外 2 条各自成桶
        failed = [r for r in rolls if r["status"] == "failed"]
        self.assertTrue(all(r["error_kind"] for r in failed))
        top = failed[0]["failover_reasons"]
        self.assertTrue(set(top.values()) <= {1, 2})

    def test_percentiles_nearest_rank(self):
        rolls = rollup_traces([trace(NOW, latency=100.0),
                               trace(NOW - timedelta(minutes=1), latency=200.0),
                               trace(NOW - timedelta(minutes=2), latency=300.0)])
        self.assertEqual(rolls[0]["ttfb_p50_ms"], 200.0)
        self.assertEqual(rolls[0]["ttfb_p95_ms"], 300.0)  # ceil(0.95*3)=3 → 最大值


class ConsistencyTests(unittest.TestCase):
    """验收「看板数据与 trace 一致」：聚合值逐项对账。"""

    def test_board_totals_match_traces(self):
        traces = sample_traces()
        board = board_summary(traces, window="24h", now=NOW)
        in_window = [t for t in traces
                     if NOW - datetime.fromisoformat(t["at"]) <= timedelta(hours=24)]
        self.assertEqual(board["total_requests"], len(in_window))
        self.assertEqual(sum(r["requests"] for r in board["route_distribution"]),
                         len(in_window))
        self.assertEqual(sum(f["count"] for f in board["failure_top"]),
                         sum(1 for t in in_window if t.get("failure_class")))
        curve_cost = round(sum(c["cost_units"] for c in board["cost_curve"]), 6)
        raw_cost = round(sum(float(t["cost_usd"]) for t in in_window), 6)
        self.assertEqual(curve_cost, raw_cost)

    def test_distribution_metrics(self):
        board = board_summary(sample_traces(), window="24h", now=NOW)
        a = next(r for r in board["route_distribution"] if r["provider_model"] == "prov-a/m-1")
        self.assertEqual(a["requests"], 6)
        self.assertEqual(a["share"], 0.6)
        self.assertAlmostEqual(a["success_rate"], 5 / 6, places=4)
        self.assertAlmostEqual(a["failover_rate"], 1 / 6, places=4)

    def test_failure_delta_vs_prev_window(self):
        traces = sample_traces()
        board = board_summary(traces, window="1h", now=NOW)
        top = {f["error_kind"]: f for f in board["failure_top"]}
        self.assertEqual(top["rate_limit"]["count"], 1)
        self.assertEqual(top["rate_limit"]["delta_vs_prev"], 1.0)  # 上一 1h 窗无该原因
        # 上一窗有基线：把 2h 前也放一条 rate_limit
        traces.append(trace(NOW - timedelta(minutes=90),  # 落在上一窗 (NOW-2h, NOW-1h]
                            backend="prov-a", model="m-1", status="failed", fc="rate_limit"))
        board2 = board_summary(traces, window="1h", now=NOW)
        top2 = {f["error_kind"]: f for f in board2["failure_top"]}
        self.assertEqual(top2["rate_limit"]["delta_vs_prev"], 0.0)  # 1 vs 1 → 持平

    def test_window_validation_and_exclusion(self):
        with self.assertRaises(ValueError):
            board_summary(sample_traces(), window="72h", now=NOW)
        board = board_summary(sample_traces(), window="1h", now=NOW)
        self.assertEqual(board["total_requests"], 10)  # 25h 前与 8 天前不计入


class DrawerTests(unittest.TestCase):
    def test_filters_and_limit_cap(self):
        traces = sample_traces()
        drawer = trace_drawer(traces, window="24h", provider="prov-b", error_kind="timeout",
                              now=NOW)
        self.assertEqual(drawer["matched"], 2)
        self.assertTrue(all(t["backend_id"] == "prov-b" for t in drawer["traces"]))
        many = [trace(NOW - timedelta(minutes=i % 50), backend="p", model="m",
                      status="ok", cost=0.001) for i in range(500)]
        capped = trace_drawer(many, window="24h", limit=999, now=NOW)
        self.assertEqual(capped["limit"], 200)  # 硬上限截断
        self.assertEqual(len(capped["traces"]), 200)
        with self.assertRaises(ValueError):
            trace_drawer(traces, window="bad", now=NOW)


if __name__ == "__main__":
    unittest.main(verbosity=2)
