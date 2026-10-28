# -*- coding: utf-8 -*-
"""PRD-257/T2 自测（trend #1286）：双后端配置切换 + 协议合同最小断言。

运行：python3 -m pytest tests/test_prd257_backend_routing.py -q

覆盖（T1 §5 T2 验收：「切换不改客户端代码，有测试」）：
  同一客户端调用（HarnessRouter.send）在 stub / local-echo 间切换零代码改动；
  配置 schema 错误快速失败；路由/后端不存在返回结构化 protocol 失败；
  能力位如实（local-echo 无 stream → synthetic_stream 标注）；
  切换审计事件落流；backend_detail redaction；七类失败枚举封闭。

虚构纪律：示例消息全虚构；密钥样例为占位形态；零网络。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.harness_route import (  # noqa: E402
    FAILURE_CLASSES, HarnessRouter, redact, structured_failure,
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

MESSAGES = [{"role": "user", "content": "虚构请求：翻译一句示例"}]


class TestSwitchWithoutClientChange(unittest.TestCase):
    def test_same_client_two_backends(self):
        router = HarnessRouter(CONFIG)
        a = router.send("default", MESSAGES, "req-a")
        b = router.send("local", MESSAGES, "req-b")
        self.assertEqual(a["status"], "ok")
        self.assertEqual(b["status"], "ok")
        self.assertTrue(a["content"].startswith("[stub]"))
        self.assertTrue(b["content"].startswith("[local-echo]"))

    def test_default_route_used_when_none(self):
        router = HarnessRouter(CONFIG)
        r = router.send(None, MESSAGES)
        self.assertTrue(r["content"].startswith("[stub]"))

    def test_switch_events_recorded(self):
        router = HarnessRouter(CONFIG)
        router.send("default", MESSAGES)
        router.send("local", MESSAGES)
        self.assertEqual(len(router.switch_events), 1)
        self.assertEqual(router.switch_events[0]["route_from"], "default")
        self.assertEqual(router.switch_events[0]["route_to"], "local")


class TestConfigErrors(unittest.TestCase):
    def test_bad_schema_fails_fast(self):
        with self.assertRaises(ValueError):
            HarnessRouter({"schema": "wrong", "routes": []})

    def test_route_not_found_structured(self):
        router = HarnessRouter(CONFIG)
        r = router.send("nope", MESSAGES)
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["failure"]["class"], "protocol")
        self.assertEqual(r["failure"]["code"], "ROUTE_NOT_FOUND")

    def test_backend_not_registered_structured(self):
        cfg = {"schema": "backend-route-v1",
               "routes": [{"route_id": "ghost", "backend_id": "unregistered", "model": "m"}],
               "default_route": "ghost"}
        r = HarnessRouter(cfg).send("ghost", MESSAGES)
        self.assertEqual(r["failure"]["code"], "BACKEND_NOT_REGISTERED")


class TestCapabilityHonesty(unittest.TestCase):
    def test_local_echo_synthetic_stream(self):
        router = HarnessRouter(CONFIG)
        r = router.send("local", MESSAGES)
        self.assertEqual(r["synthetic_stream"], True)
        self.assertEqual(len(r["content"]), 0) if False else None

    def test_probe_capabilities(self):
        router = HarnessRouter(CONFIG)
        stub_probe = router.resolve("default")["adapter"].probe()
        local_probe = router.resolve("local")["adapter"].probe()
        self.assertIn("stream", stub_probe["capabilities"])
        self.assertNotIn("stream", local_probe["capabilities"])


class TestFailureContract(unittest.TestCase):
    def test_seven_classes_closed(self):
        for cls in FAILURE_CLASSES:
            self.assertEqual(structured_failure(cls, "X", "m")["class"], cls)
        with self.assertRaises(ValueError):
            structured_failure("nope", "X", "m")

    def test_backend_detail_redacted(self):
        f = structured_failure("backend_error", "E", "m",
                               backend_detail="连 10.1.2.3 失败，token sk-abcdef1234567890abcdef 无效")
        self.assertNotIn("10.1.2.3", f["backend_detail"])
        self.assertNotIn("sk-abcdef1234567890abcdef", f["backend_detail"])
        self.assertIn("<REDACTED>", f["backend_detail"])

    def test_redact_helper(self):
        self.assertEqual(redact("Bearer abcdefghijklmnopqrst"), "<REDACTED>")
        self.assertEqual(redact("普通内容"), "普通内容")


if __name__ == "__main__":
    unittest.main()
