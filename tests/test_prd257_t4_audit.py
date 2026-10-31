# -*- coding: utf-8 -*-
"""PRD-257/T4 自测（trend #1288）：切换前后对比记录与审计导出——记录可机读。

运行：python3 -m pytest tests/test_prd257_t4_audit.py -q

覆盖（issue #1288 验收口径）：
  导出可解析：export_jsonl 每行均为合法 JSON 对象，switch/request 行字段齐全；
  零对话正文：消息原文不出现在导出中，每行键 ⊆ 白名单；
  compare 对照字段齐全：route/status/duration_ms/failure_class 四字段逐行在位；
  空路由零导出：零事件路由器 → 空串，compare([]) → 空；
  JSONL 行数正确：事件数与行数一一对应；
  失败类留痕：失败请求的 failure_class 进摘要（session 过期 → protocol）。

虚构纪律：示例消息全虚构；零网络。
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.harness_route import (  # noqa: E402
    HarnessRouter, open_session,
)
from open_free_router.route_audit import (  # noqa: E402
    COMPARE_FIELDS, REQUEST_FIELDS, SWITCH_FIELDS, export_compare, export_jsonl,
)

CONFIG = {
    "schema": "backend-route-v1",
    "routes": [
        {"route_id": "default", "backend_id": "stub", "model": "stub-1",
         "base_url_ref": "ref/stub", "policy_ref": "policy/default"},
        {"route_id": "local", "backend_id": "local-echo", "model": "local-x",
         "base_url_ref": "ref/local", "policy_ref": "policy/default"},
    ],
    "default_route": "default",
}

SECRET_CONTENT = "虚构请求正文-EXPOSE-MARKER-绝不应出现在导出里"


class TestExportJsonl(unittest.TestCase):
    def test_switch_events_export_parsable(self):
        router = HarnessRouter(CONFIG)
        router.send("default", [{"role": "user", "content": "一"}], "req-a")
        router.send("local", [{"role": "user", "content": "二"}], "req-b")  # 触发切换
        raw = export_jsonl(router)
        lines = raw.strip().splitlines()
        events = [json.loads(line) for line in lines]  # 每行可解析为 JSON
        self.assertTrue(all(isinstance(e, dict) for e in events))
        switch = [e for e in events if e["type"] == "switch"]
        self.assertEqual(len(switch), 1)
        self.assertEqual(switch[0]["route_from"], "default")
        self.assertEqual(switch[0]["route_to"], "local")
        for key in SWITCH_FIELDS:
            self.assertIn(key, switch[0])

    def test_zero_conversation_content(self):
        router = HarnessRouter(CONFIG)
        router.send("default", [{"role": "user", "content": SECRET_CONTENT}], "req-x")
        raw = export_jsonl(router)
        self.assertNotIn("EXPOSE-MARKER", raw)          # 正文不外泄
        self.assertNotIn(SECRET_CONTENT, raw)
        for line in raw.strip().splitlines():
            event = json.loads(line)
            allowed = set(SWITCH_FIELDS) | set(REQUEST_FIELDS) | {"type", "seq"}
            self.assertTrue(set(event) <= allowed, f"越界字段: {set(event) - allowed}")
            self.assertNotIn("messages", event)
            self.assertNotIn("content", event)

    def test_jsonl_line_count_matches_events(self):
        router = HarnessRouter(CONFIG)
        router.send("default", [{"role": "user", "content": "一"}], "req-1")
        router.send("local", [{"role": "user", "content": "二"}], "req-2")   # 1 次切换
        router.send("local", [{"role": "user", "content": "三"}], "req-3")   # 不切换
        router.stream("default", [{"role": "user", "content": "四"}], "req-4")  # 又 1 次切换
        raw = export_jsonl(router)
        lines = raw.strip().splitlines()
        # 2 切换 + 3 send 摘要 + 1 stream 摘要 = 6 行
        self.assertEqual(len(lines), 6)
        types = [json.loads(line)["type"] for line in lines]
        self.assertEqual(types.count("switch"), 2)
        self.assertEqual(types.count("request"), 3)
        self.assertEqual(types.count("request_stream"), 1)

    def test_failure_class_recorded(self):
        router = HarnessRouter(CONFIG)
        expired = open_session("stub", "stub-1", session_id="sess-old",
                               now=1_000.0, ttl_s=60)
        router.send("default", [{"role": "user", "content": "过期会话"}],
                    "req-exp", session=expired)
        raw = export_jsonl(router)
        event = json.loads(raw.strip().splitlines()[0])
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["failure_class"], "protocol")


class TestEmptyAndCompare(unittest.TestCase):
    def test_empty_router_zero_export(self):
        router = HarnessRouter(CONFIG)
        self.assertEqual(export_jsonl(router), "")       # 零导出 = 空串
        self.assertEqual(export_compare([]), [])

    def test_compare_fields_complete(self):
        results = [
            {"route": "default", "duration_ms": 1.5,
             "response": {"request_id": "req-1", "status": "ok", "failure": None}},
            {"route": "local", "duration_ms": 0.8,
             "response": {"request_id": "req-1", "status": "failed",
                          "failure": {"class": "rate_limit", "code": "RL", "message": "x"}}},
        ]
        rows = export_compare(results)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(set(row), set(COMPARE_FIELDS))  # 四字段逐行齐全
        self.assertEqual(rows[0], {"route": "default", "status": "ok",
                                   "duration_ms": 1.5, "failure_class": ""})
        self.assertEqual(rows[1]["route"], "local")
        self.assertEqual(rows[1]["status"], "failed")
        self.assertEqual(rows[1]["failure_class"], "rate_limit")

    def test_compare_tolerates_bare_response(self):
        rows = export_compare([{"request_id": "r", "status": "ok", "failure": None}])
        self.assertEqual(rows[0]["status"], "ok")
        self.assertIsNone(rows[0]["duration_ms"])        # 缺计时如实留空，不编造


if __name__ == "__main__":
    unittest.main()
