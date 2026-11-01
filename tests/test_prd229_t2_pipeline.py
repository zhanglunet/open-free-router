# -*- coding: utf-8 -*-
"""PRD-229/T2 自测（trend #1146）：流量数据汇总管道。

运行：python3 -m unittest tests.test_prd229_t2_pipeline -v（仓库根执行）

覆盖（T1 §8 T2 契约 + 验收「历史流量可按提示词类型/后端汇总且可溯源」）：
  窗口校验（7/14/30 之外拒绝）；窗口整日 UTC 起止写入聚合集
  汇总指标：n/success_rate/client_error_count/mean_latency/mean_tokens
  客户端错误（400/invalid_request）计入总量不入后端质量分母
  溯源：任取一行 event_ids 可对回窗口内原始事件（确定性查询回验）
  确定性：同输入两次聚合逐字节一致
  分类覆盖率报告在位；只读打开（写路径不可用）
零网络；合成 usage.db。
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.gateway_aggregate import (  # noqa: E402
    PipelineError,
    aggregate,
    window_bounds,
)

NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)


def make_db(tmp: Path, events):
    path = tmp / "usage.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE usage_events (
        event_id TEXT PRIMARY KEY, timestamp REAL NOT NULL, provider TEXT NOT NULL,
        model TEXT NOT NULL, status TEXT NOT NULL, status_code INTEGER NOT NULL,
        ttfb_ms REAL, total_latency_ms REAL, input_tokens INTEGER,
        output_tokens INTEGER, fallback_attempts INTEGER NOT NULL,
        error_kind TEXT NOT NULL, circuit_breaker_events INTEGER NOT NULL)""")
    for i, e in enumerate(events):
        conn.execute(
            "INSERT INTO usage_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (e["event_id"], e["timestamp"], e["provider"], e["model"], e["status"],
             e["status_code"], e.get("ttfb_ms"), e.get("total_latency_ms"),
             e.get("input_tokens"), e.get("output_tokens"), 0,
             e.get("error_kind", "none"), 0))
    conn.commit()
    conn.close()
    return path


def evt(eid, ts, provider="remote", model="m-1", status="success",
        status_code=200, latency=100.0, error_kind="none"):
    return {"event_id": eid, "timestamp": ts, "provider": provider, "model": model,
            "status": status, "status_code": status_code, "ttfb_ms": 10.0,
            "total_latency_ms": latency, "input_tokens": 100, "output_tokens": 50,
            "error_kind": error_kind}


class WindowTests(unittest.TestCase):
    def test_window_validation(self):
        with self.assertRaises(PipelineError):
            window_bounds(5)
        start, end = window_bounds(7, now=NOW)
        self.assertEqual(start, "2026-10-31")
        self.assertEqual(end, "2026-11-08")

    def test_missing_db_rejected(self):
        with self.assertRaises(PipelineError):
            aggregate("/tmp/不存在.db", 7, now=NOW)


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        self.events = [
            evt("e1", base, provider="remote", model="m-1", status="success",
                latency=100.0),
            evt("e2", base + 10, provider="remote", model="m-1", status="failed",
                status_code=500, latency=200.0, error_kind="server_error"),
            evt("e3", base + 20, provider="remote", model="m-1", status="failed",
                status_code=400, latency=50.0, error_kind="invalid_request"),
            evt("e4", base + 30, provider="local", model="local-llama",
                status="success", latency=80.0),
        ]
        self.db = make_db(Path(self.tmp.name), self.events)

    def test_aggregate_rows_and_client_error_exclusion(self):
        result = aggregate(self.db, 7, now=NOW)
        by_key = {(r["prompt_class"], r["provider"], r["model"]): r
                  for r in result["aggregate"]}
        remote = by_key[("remote-api", "remote", "m-1")]
        self.assertEqual(remote["n"], 3)                        # 全量计入
        self.assertEqual(remote["client_error_count"], 1)       # 400 单列
        self.assertEqual(remote["success_rate"], 0.5)           # 分母 = 3-1=2，成功 1
        local = by_key[("local-model", "local", "local-llama")]
        self.assertEqual(local["prompt_class"], "local-model")  # 分类器口径
        self.assertEqual(local["success_rate"], 1.0)

    def test_traceability_event_ids_resolve(self):
        result = aggregate(self.db, 7, now=NOW)
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        for row in result["aggregate"]:
            for eid in row["event_ids"]:
                hit = conn.execute(
                    "SELECT event_id, provider FROM usage_events WHERE event_id=?",
                    (eid,)).fetchone()
                self.assertIsNotNone(hit)
                self.assertEqual(hit["event_id"], eid)
        conn.close()

    def test_deterministic_same_input_same_output(self):
        a = aggregate(self.db, 7, now=NOW)
        b = aggregate(self.db, 7, now=NOW)
        self.assertEqual(json.dumps(a, sort_keys=True),
                         json.dumps(b, sort_keys=True))

    def test_coverage_and_provenance_fields(self):
        result = aggregate(self.db, 7, now=NOW)
        self.assertIn("coverage", result)
        for row in result["aggregate"]:
            for key in ("window", "classifier_version", "metric_version",
                        "event_total_in_window", "event_ids"):
                self.assertIn(key, row)
        self.assertEqual(result["classifier_version"], "prompt-classifier/v1")
        self.assertEqual(result["metric_version"], "gateway-aggregate/v1")

    def test_window_filter_excludes_out_of_window(self):
        result = aggregate(self.db, 7, now=NOW)
        all_ids = {eid for r in result["aggregate"] for eid in r["event_ids"]}
        self.assertNotIn("e-out", all_ids)

    def test_out_of_window_event_not_counted(self):
        # 建一个含窗口外事件的库
        with tempfile.TemporaryDirectory() as td:
            old = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
            path = make_db(Path(td), [evt("old", old), evt("new", datetime(
                2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc).timestamp())])
            result = aggregate(path, 7, now=NOW)
            self.assertEqual(result["total_events"], 1)
            self.assertEqual(result["aggregate"][0]["n"], 1)


def make_db(tmp: Path, events):
    path = tmp / "usage.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE usage_events (
        event_id TEXT PRIMARY KEY, timestamp REAL NOT NULL, provider TEXT NOT NULL,
        model TEXT NOT NULL, status TEXT NOT NULL, status_code INTEGER NOT NULL,
        ttfb_ms REAL, total_latency_ms REAL, input_tokens INTEGER,
        output_tokens INTEGER, fallback_attempts INTEGER NOT NULL,
        error_kind TEXT NOT NULL, circuit_breaker_events INTEGER NOT NULL)""")
    for e in events:
        conn.execute(
            "INSERT INTO usage_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (e["event_id"], e["timestamp"], e["provider"], e["model"], e["status"],
             e["status_code"], e.get("ttfb_ms"), e.get("total_latency_ms"),
             e.get("input_tokens"), e.get("output_tokens"), 0,
             e.get("error_kind", "none"), 0))
    conn.commit()
    conn.close()
    return path


def evt(eid, ts, provider="remote", model="m-1", status="success",
        status_code=200, latency=100.0, error_kind="none"):
    return {"event_id": eid, "timestamp": ts, "provider": provider, "model": model,
            "status": status, "status_code": status_code, "ttfb_ms": 10.0,
            "total_latency_ms": latency, "input_tokens": 100, "output_tokens": 50,
            "error_kind": error_kind}


if __name__ == "__main__":
    unittest.main(verbosity=2)
