# -*- coding: utf-8 -*-
"""PRD-257/T3 自测（trend #1287）：会话/流式/取消/结构化失败合同一致性。

运行：python3 -m pytest tests/test_prd257_t3_contracts.py -q

覆盖（T1 §3.3 合同 + §6 验收口径）：
  流式：seq 单调（正常通过 / 乱序·重复·非法 seq 抛 ValueError），路由边界强制；
  会话：ChatSession.expires_at 过期 → StructuredFailure(protocol, SESSION_EXPIRED)
        并提示重建（send 与 stream 两面一致）；
  取消：cancel 是协议操作——cancel 后下一次 send 返回 cancelled chunk（payload.ack=true）
        + status=cancelled；cancel_ack 校验 ack 语义；缺 ack 的 cancelled 失败被边界拒绝；
  失败：assert_structured_failure 校验器——七类封闭 + 字段类型（合法通过 / 非法拒绝）；
  脱敏：backend_detail redaction 在合同深化后保持；
  合同一致性冒烟：双后端事件形态一致（chunk/response 键形态、seq 单调、失败结构）。

虚构纪律：示例消息全虚构；密钥样例为占位形态；零网络。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_free_router.harness_route import (  # noqa: E402
    CHUNK_TYPES, FAILURE_CLASSES, BaseAdapter, ChatSession, HarnessRouter,
    StubAdapter, assert_monotonic_seq, assert_structured_failure, cancel_ack,
    open_session, redact, structured_failure,
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

MESSAGES = [{"role": "user", "content": "虚构请求：合同一致性探针"}]


class TestStreamSeqContract(unittest.TestCase):
    def test_seq_monotonic_passes(self):
        router = HarnessRouter(CONFIG)
        chunks = router.stream("default", MESSAGES, "req-seq-ok")
        self.assertEqual([c["seq"] for c in chunks], [1, 2, 3])  # 单调正整数
        self.assertEqual(assert_monotonic_seq(chunks), chunks)   # 校验器直证通过

    def test_seq_out_of_order_raises(self):
        good = {"request_id": "r", "seq": 1, "type": "delta", "payload": {}}
        cases = [
            [dict(good, seq=2), dict(good, seq=1)],   # 乱序
            [dict(good, seq=1), dict(good, seq=1)],   # 重复
            [dict(good, seq=0)],                      # 非正
            [dict(good, seq=True)],                   # bool 冒充 int
            [{"request_id": "r", "type": "delta"}],   # 缺 seq
        ]
        for chunks in cases:
            with self.assertRaises(ValueError):
                assert_monotonic_seq(chunks)
        with self.assertRaises(ValueError):
            assert_monotonic_seq("not-a-list")


class TestSessionExpiryContract(unittest.TestCase):
    def test_session_expired_structured_failure(self):
        router = HarnessRouter(CONFIG)
        expired = open_session("stub", "stub-1", session_id="sess-old", now=1_000.0, ttl_s=60)
        self.assertTrue(expired.expired(now=2_000.0))
        r = router.send("default", MESSAGES, "req-exp", session=expired)
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["failure"]["class"], "protocol")
        self.assertEqual(r["failure"]["code"], "SESSION_EXPIRED")
        self.assertTrue(r["failure"]["retryable"])          # 重建会话后可重试
        self.assertIn("重建", r["failure"]["message"])       # 提示重建（§3.3）
        # stream 面同合同：单条 failed chunk，payload 即同一 StructuredFailure
        chunks = router.stream("default", MESSAGES, "req-exp-s", session=expired)
        self.assertEqual(chunks[0]["type"], "failed")
        self.assertEqual(chunks[0]["payload"]["code"], "SESSION_EXPIRED")

    def test_fresh_session_passes(self):
        router = HarnessRouter(CONFIG)
        fresh = open_session("stub", "stub-1", session_id="sess-new")
        r = router.send("default", MESSAGES, "req-fresh", session=fresh)
        self.assertEqual(r["status"], "ok")
        self.assertIsNone(r["failure"])

    def test_expired_dict_session_also_caught(self):
        router = HarnessRouter(CONFIG)
        r = router.send("default", MESSAGES, "req-dict",
                        session={"session_id": "s", "expires_at": 1.0})
        self.assertEqual(r["failure"]["code"], "SESSION_EXPIRED")


class TestCancelAckContract(unittest.TestCase):
    def test_cancel_ack_path_on_stub(self):
        adapter = StubAdapter()
        ack = cancel_ack("req-cancel", adapter)
        self.assertEqual(ack["backend"], "stub")
        self.assertTrue(ack["acked"])
        self.assertEqual(ack["failure_code"], "CANCEL_ACKED")
        # 直接面：cancel 后 send 返回 cancelled chunk（ack 语义）而非内容
        adapter.cancel("req-x")
        out = adapter.send({"request_id": "req-x", "messages": MESSAGES, "session": {}})
        self.assertEqual(out["chunks"][0]["type"], "cancelled")
        self.assertTrue(out["chunks"][0]["payload"]["ack"])
        self.assertEqual(out["response"]["status"], "cancelled")
        self.assertEqual(out["response"]["failure"]["class"], "cancelled")
        # ack 一次性：再 send 恢复正常路径
        out2 = adapter.send({"request_id": "req-x", "messages": MESSAGES, "session": {}})
        self.assertEqual(out2["response"]["status"], "ok")

    def test_cancel_ack_on_both_backends(self):
        router = HarnessRouter(CONFIG)
        for rid in ("default", "local"):
            adapter = router.resolve(rid)["adapter"]
            ack = cancel_ack(f"req-{rid}", adapter)
            self.assertTrue(ack["acked"])

    def test_cancelled_failure_without_ack_rejected(self):
        # 违约适配器：cancelled 失败但 chunk 缺 ack 语义——路由边界必须拒绝（§3.3）
        class _BrokenCancelAdapter(BaseAdapter):
            capabilities = ("cancel",)

            def send(self, request):
                return {"ok": True,
                        "chunks": [{"request_id": request["request_id"], "seq": 1,
                                    "type": "cancelled", "payload": {"ack": False}}],
                        "response": {"request_id": request["request_id"], "status": "cancelled",
                                     "content": None, "usage": {}, "synthetic_stream": False,
                                     "failure": structured_failure("cancelled", "X", "m")}}

        cfg = {"schema": "backend-route-v1",
               "routes": [{"route_id": "bad", "backend_id": "broken-cancel", "model": "m"}],
               "default_route": "bad"}
        router = HarnessRouter(cfg, adapters={"broken-cancel": _BrokenCancelAdapter("broken-cancel")})
        with self.assertRaises(ValueError):
            router.send("bad", MESSAGES, "req-bad-cancel")


class TestStructuredFailureValidator(unittest.TestCase):
    def test_validator_accepts_all_seven_classes(self):
        for cls in FAILURE_CLASSES:
            f = structured_failure(cls, "CODE_X", "虚构失败",
                                   retryable=(cls == "rate_limit"), backend_detail="detail")
            self.assertEqual(assert_structured_failure(f), f)

    def test_validator_rejects_illegal(self):
        valid = structured_failure("protocol", "CODE_X", "m")
        cases = [
            "not-a-dict",                                   # 非 dict
            {k: v for k, v in valid.items() if k != "code"},  # 缺字段
            dict(valid, **{"class": "nope"}),               # class 出封闭枚举
            dict(valid, retryable="yes"),                   # retryable 非 bool
            dict(valid, code=7),                            # code 非 str
            dict(valid, code=""),                           # code 空
            dict(valid, backend_detail=None),               # backend_detail 非 str
        ]
        for obj in cases:
            with self.assertRaises(ValueError):
                assert_structured_failure(obj)


class TestRedactionPreserved(unittest.TestCase):
    def test_backend_detail_redacted_through_contract(self):
        f = structured_failure("backend_error", "E", "m",
                               backend_detail="上游 192.168.1.5 拒绝，密钥 sk-abcdef1234567890abcdef 失效")
        assert_structured_failure(f)  # 校验器不破坏脱敏
        self.assertNotIn("192.168.1.5", f["backend_detail"])
        self.assertNotIn("sk-abcdef1234567890abcdef", f["backend_detail"])
        self.assertIn("<REDACTED>", f["backend_detail"])
        self.assertEqual(redact("Bearer abcdefghijklmnopqrst"), "<REDACTED>")


class TestContractConsistencySmoke(unittest.TestCase):
    def test_both_backends_event_shape_identical(self):
        """双后端合同一致性：同一客户端调用事件形态一致（客户端不随后端重写）。"""
        router = HarnessRouter(CONFIG)
        chunk_keys, resp_keys = set(), set()
        for rid in ("default", "local"):
            chunks = router.stream(rid, MESSAGES, f"req-{rid}")
            assert_monotonic_seq(chunks)  # 双后端 seq 单调
            for c in chunks:
                self.assertEqual(set(c), {"request_id", "seq", "type", "payload"})
                self.assertIn(c["type"], CHUNK_TYPES)
                chunk_keys.add(frozenset(c))
            r = router.send(rid, MESSAGES, f"req-resp-{rid}")
            self.assertEqual(set(r), {"request_id", "status", "content", "usage",
                                      "synthetic_stream", "failure"})
            self.assertEqual(r["status"], "ok")
            resp_keys.add(frozenset(r))
            if r["failure"] is not None:
                assert_structured_failure(r["failure"])
        self.assertEqual(len(chunk_keys), 1)  # chunk 键形态唯一
        self.assertEqual(len(resp_keys), 1)   # response 键形态唯一


if __name__ == "__main__":
    unittest.main()
