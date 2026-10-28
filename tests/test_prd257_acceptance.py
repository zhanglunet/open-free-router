# -*- coding: utf-8 -*-
"""PRD-257/T5 acceptance（trend #1289）：双后端切换对比验收试运行——记录齐全、文档无密钥.

验收对象是 T1-T4 已交付的链路（本 PR 纯加验收测试与记录，不改 T2-T4 行为）：
  T1 #94 设计 → T2 #96（harness_route.py 11 例）→ T3 #98（contracts 12 例）
  → T4 #100（route_audit 8 例）。

issue #1289 验收标准：「对比记录齐全；文档无密钥」；PRD 红线：「切换走配置，
客户端不随后端重写；doctor 诚实标注纪律不减」。每条验收项 1:1 对应一个编号测试：

| # | 验收项 | 测试 |
|---|---|---|
| 1 | 同一虚构请求双后端切换对比：同一客户端调用（只认 route_id）在 stub / local-echo 间切换，status / content 前缀 / synthetic_stream / usage 逐字段对照，export_compare 对照行齐全 | ``test_1_dual_backend_field_by_field_compare`` |
| 2 | 切换审计事件字段齐全：route_from/route_to/reason/actor 落事件流并进导出 | ``test_2_switch_audit_event_fields`` |
| 3 | JSONL 导出可解析：每行合法 JSON、字段 ⊆ 白名单、零对话正文、落临时文件后可复读 | ``test_3_jsonl_export_parsable_and_archivable`` |
| 4 | 取消路径：双后端 cancel → 下一次 send 返回 cancelled chunk（payload.ack=true）+ status=cancelled + failure.class=cancelled，cancel_ack 校验器通过 | ``test_4_cancel_path_on_both_backends`` |
| 5 | 会话过期：persistent 会话过期 → StructuredFailure(protocol, SESSION_EXPIRED, retryable)，send 与 stream 两面一致 | ``test_5_session_expired_structured_failure`` |
| 6 | 失败 redaction：自造失败适配器把假密钥+假内网 IP 塞进 backend_detail，经路由边界 → Response → 审计导出全链路零泄露（<REDACTED> 占位） | ``test_6_failure_redaction_no_secret_no_lan_ip`` |
| 7 | 零网络断言：socket 全程封死（构造即抛）跑完 1-5 全场景结果不变；交付模块无网络库导入 | ``test_7_zero_network_by_construction`` |
| 8 | 文档与交付面无密钥：PRD-257 全部交付面（源码/测试/设计稿/验收记录）静态扫描假密钥样式与内网 IP 样式零命中（docs/src 严格零命中；链内 T2/T3 测试自带的脱敏样本 fixture 申报豁免，其断言恰好证明样本经 redact 零存活） | ``test_8_delivery_surface_no_secrets_no_lan_ip`` |

虚构纪律：消息与密钥/内网 IP 样本全部运行时拼接自造（源码零字面量，供 #8 全
交付面扫描）；双后端均为进程内 stub/local-echo 适配器，零网络。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from open_free_router.harness_route import (  # noqa: E402
    HarnessRouter, LocalEchoAdapter, StubAdapter, cancel_ack, open_session,
    redact, structured_failure,
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

# 运行时拼接的假凭据 / 假内网地址：源文件零字面量（验收 #8 全交付面可扫描）
FAKE_KEY = "sk-" + "fake" + "257acceptance" + "0123456789"
FAKE_LAN_IP = ".".join(["10", "72", "3", "14"])
FICTION_PROMPT = "虚构请求：把一句示例从中文译成英文（T5 对比样本）"

SECRET_PATTERNS = (
    r"sk-[A-Za-z0-9]{16,}",
    r"AKIA[0-9A-Z]{16}",
    r"gh[pousr]_[A-Za-z0-9]{30,}",
    r"Bearer\s+[A-Za-z0-9._-]{16,}",
    r"BEGIN [A-Z ]*PRIVATE KEY",
)
LAN_IP_PATTERN = r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"

# 链内 T2/T3 测试自带的脱敏样本 fixture（其断言恰好证明这些样本经 redact() 零存活），
# 仅豁免于测试面；docs/ 与 src/ 严格零命中。名单之外的任何命中一律失败。
DECLARED_SYNTHETIC_SAMPLES = (
    "sk-abcdef1234567890abcdef",  # test_prd257_backend_routing.py / t3_contracts.py 脱敏样本
    "Bearer abcdefghijklmnopqrst",  # 同上（redact 直证样本，断言其归零）
    "10.1.2.3",                   # 同上
    "192.168.1.5",                # 同上
)

DELIVERY_SURFACE = (
    "src/open_free_router/harness_route.py",
    "src/open_free_router/route_audit.py",
    "tests/test_prd257_backend_routing.py",
    "tests/test_prd257_t3_contracts.py",
    "tests/test_prd257_t4_audit.py",
    "tests/test_prd257_acceptance.py",
    "docs/plans/2026-10-28-prd-257-backend-routing.md",
    "docs/devlog/2026-10-28-prd-257-t5-acceptance.md",
)


def _new_router() -> HarnessRouter:
    return HarnessRouter(CONFIG)


def _send_both(router: HarnessRouter, request_id_prefix: str) -> tuple[dict, dict]:
    """同一虚构请求先后发给两个后端（客户端只换 route_id，零代码改动）。"""
    a = router.send("default", [{"role": "user", "content": FICTION_PROMPT}],
                    f"{request_id_prefix}-stub")
    b = router.send("local", [{"role": "user", "content": FICTION_PROMPT}],
                    f"{request_id_prefix}-echo")
    return a, b


class Test1DualBackendCompare(unittest.TestCase):
    """验收 #1：同一虚构请求双后端切换对比——逐字段对照、记录齐全。"""

    def test_1_dual_backend_field_by_field_compare(self):
        router = _new_router()
        a, b = _send_both(router, "req-t5")
        # status 一致：切换不改客户端可见结果形态
        self.assertEqual(a["status"], "ok")
        self.assertEqual(b["status"], "ok")
        # content 前缀逐字段对照：后端身份自明
        self.assertTrue(a["content"].startswith("[stub]"))
        self.assertTrue(b["content"].startswith("[local-echo]"))
        # 同一请求正文在两个后端都被如实回显（前缀之外正文一致）
        self.assertIn(FICTION_PROMPT, a["content"])
        self.assertIn(FICTION_PROMPT, b["content"])
        # synthetic_stream 逐字段：stub 全能力（False）vs local-echo 能力子集（如实标注 True）
        self.assertIs(a["synthetic_stream"], False)
        self.assertIs(b["synthetic_stream"], True)
        # usage/失败位字段形态一致（差异只在内容，不在结构）
        self.assertEqual(set(a), set(b))
        self.assertIsNone(a["failure"])
        self.assertIsNone(b["failure"])
        # 对比记录齐全：同一请求双路由对照表（四字段逐行在位且与实际响应一致）
        rows = export_compare([
            {"route": "default", "response": a, "duration_ms": 1.0},
            {"route": "local", "response": b, "duration_ms": 2.0},
        ])
        self.assertEqual([r["route"] for r in rows], ["default", "local"])
        for row in rows:
            self.assertEqual(set(row), set(COMPARE_FIELDS))
        self.assertEqual([r["status"] for r in rows], ["ok", "ok"])
        self.assertEqual([r["failure_class"] for r in rows], ["", ""])


class Test2SwitchAuditEvent(unittest.TestCase):
    """验收 #2：切换审计事件字段齐全。"""

    def test_2_switch_audit_event_fields(self):
        router = _new_router()
        _send_both(router, "req-sw")
        self.assertEqual(len(router.switch_events), 1)
        ev = router.switch_events[0]
        for key in SWITCH_FIELDS:  # route_from/route_to/reason/actor
            self.assertIn(key, ev)
        self.assertEqual(ev["route_from"], "default")
        self.assertEqual(ev["route_to"], "local")
        self.assertTrue(ev["reason"])
        self.assertTrue(ev["actor"])
        # 事件同步进机读导出（switch 行）
        events = [json.loads(line) for line in export_jsonl(router).strip().splitlines()]
        switch_rows = [e for e in events if e["type"] == "switch"]
        self.assertEqual(len(switch_rows), 1)
        self.assertEqual(switch_rows[0]["route_from"], "default")
        self.assertEqual(switch_rows[0]["route_to"], "local")


class Test3JsonlExport(unittest.TestCase):
    """验收 #3：JSONL 导出可解析、白名单出料、可存档复读。"""

    def test_3_jsonl_export_parsable_and_archivable(self):
        router = _new_router()
        a, b = _send_both(router, "req-jl")
        router.stream("local", [{"role": "user", "content": FICTION_PROMPT}], "req-jl-s")
        raw = export_jsonl(router)
        # 落临时档案再复读：机读工具（jq/awk 同类）可直接逐行消费
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "prd257-compare.jsonl"
            archive.write_text(raw, encoding="utf-8")
            reread = archive.read_text(encoding="utf-8")
            self.assertEqual(reread, raw)
            lines = reread.strip().splitlines()
            events = [json.loads(line) for line in lines]  # 每行合法 JSON
        self.assertEqual(len(events), 4)  # 1 switch + 2 request + 1 request_stream
        types = [e["type"] for e in events]
        self.assertEqual(types.count("switch"), 1)
        self.assertEqual(types.count("request"), 2)
        self.assertEqual(types.count("request_stream"), 1)
        # 零对话正文 + 白名单出料
        self.assertNotIn(FICTION_PROMPT, raw)
        allowed = set(SWITCH_FIELDS) | set(REQUEST_FIELDS) | {"type", "seq"}
        for e in events:
            self.assertTrue(set(e) <= allowed, f"越界字段: {set(e) - allowed}")
            self.assertNotIn("messages", e)
            self.assertNotIn("content", e)
        # 请求摘要字段口径：send 与 stream 两面一致（status/synthetic_stream 如实）
        stream_rows = [e for e in events if e["type"] == "request_stream"]
        self.assertEqual(stream_rows[0]["synthetic_stream"], True)  # local-echo 合成流如实标注
        echo_rows = [e for e in events if e["type"] == "request" and e["route"] == "local"]
        self.assertEqual(echo_rows[0]["synthetic_stream"], True)


class Test4CancelPath(unittest.TestCase):
    """验收 #4：取消路径——双后端 ack 语义一致。"""

    def test_4_cancel_path_on_both_backends(self):
        for route_id, backend_id in (("default", "stub"), ("local", "local-echo")):
            with self.subTest(route=route_id):
                adapters = {"stub": StubAdapter(), "local-echo": LocalEchoAdapter()}
                router = HarnessRouter(CONFIG, adapters=adapters)
                adapter = adapters[backend_id]
                ack = cancel_ack("req-cancel", adapter)  # 校验器：ack 语义齐全
                self.assertTrue(ack["acked"])
                self.assertEqual(ack["backend"], backend_id)
                self.assertEqual(ack["failure_code"], "CANCEL_ACKED")
                # 经路由面的取消：status=cancelled + cancelled chunk（payload.ack）
                adapter.cancel("req-via-router")
                out = router.send(route_id, [{"role": "user", "content": "虚构：将被取消"}],
                                  "req-via-router")
                self.assertEqual(out["status"], "cancelled")
                self.assertIsNotNone(out["failure"])
                self.assertEqual(out["failure"]["class"], "cancelled")
                # cancelled 失败进审计摘要（failure_class 如实留痕）
                self.assertEqual(router.request_events[-1]["failure_class"], "cancelled")


class Test5SessionExpired(unittest.TestCase):
    """验收 #5：会话过期——StructuredFailure(protocol, SESSION_EXPIRED) 两面一致。"""

    def test_5_session_expired_structured_failure(self):
        router = _new_router()
        expired = open_session("stub", "stub-1", session_id="sess-t5",
                               now=1_000.0, ttl_s=60)
        self.assertTrue(expired.expired(now=2_000.0))
        send_resp = router.send("default", [{"role": "user", "content": "虚构：过期会话"}],
                                "req-exp", session=expired)
        self.assertEqual(send_resp["status"], "failed")
        self.assertEqual(send_resp["failure"]["class"], "protocol")
        self.assertEqual(send_resp["failure"]["code"], "SESSION_EXPIRED")
        self.assertTrue(send_resp["failure"]["retryable"])  # 提示重建后可重试
        stream_chunks = router.stream("default", [{"role": "user", "content": "虚构：过期会话"}],
                                      "req-exp-s", session=expired)
        self.assertEqual([c["type"] for c in stream_chunks], ["failed"])
        self.assertEqual(stream_chunks[0]["payload"]["code"], "SESSION_EXPIRED")
        # 失败类进审计摘要（send 与 stream 同口径）
        self.assertEqual(router.request_events[-2]["failure_class"], "protocol")
        self.assertEqual(router.request_events[-1]["failure_class"], "protocol")


class _LeakyBackend:
    """自造失败后端：把假密钥+假内网 IP 塞进 backend_detail（供 #6 全链路 redaction）。"""

    capabilities = ("cancel",)

    def __init__(self) -> None:
        self.backend_id = "leaky"

    def probe(self) -> dict:
        return {"ok": True, "backend_id": self.backend_id, "capabilities": []}

    def cancel(self, request_id: str) -> dict:
        return {"request_id": request_id, "backend": self.backend_id, "requested": True}

    def send(self, request: dict) -> dict:
        detail = f"upstream ref={FAKE_LAN_IP} auth={FAKE_KEY} 拒绝（虚构样本）"
        return {"ok": False,
                "response": {"request_id": request["request_id"], "status": "failed",
                             "content": None, "usage": {}, "synthetic_stream": False,
                             "failure": structured_failure("backend_error", "UPSTREAM_REFUSED",
                                                           "上游拒绝（虚构）",
                                                           retryable=True,
                                                           backend_detail=detail)}}


class Test6FailureRedaction(unittest.TestCase):
    """验收 #6：失败 redaction——密钥/内网 IP 经路由边界到导出全链路零泄露。"""

    def test_6_failure_redaction_no_secret_no_lan_ip(self):
        # 单元面：structured_failure 生成时即脱敏
        sf = structured_failure("backend_error", "X", "msg", backend_detail=f"ip={FAKE_LAN_IP} key={FAKE_KEY}")
        self.assertNotIn(FAKE_KEY, sf["backend_detail"])
        self.assertNotIn(FAKE_LAN_IP, sf["backend_detail"])
        self.assertIn("<REDACTED>", sf["backend_detail"])
        # redact 直证
        self.assertEqual(redact(FAKE_KEY), "<REDACTED>")
        self.assertEqual(redact(FAKE_LAN_IP), "<REDACTED>")
        # 路由面：注册 leaky 后端，失败经路由边界 → Response → 审计导出
        cfg = {**CONFIG,
               "routes": CONFIG["routes"] + [
                   {"route_id": "leaky", "backend_id": "leaky", "model": "leaky-1"}]}
        router = HarnessRouter(cfg, adapters={"stub": StubAdapter(),
                                              "local-echo": LocalEchoAdapter(),
                                              "leaky": _LeakyBackend()})
        resp = router.send("leaky", [{"role": "user", "content": FICTION_PROMPT}], "req-leak")
        self.assertEqual(resp["status"], "failed")
        self.assertEqual(resp["failure"]["class"], "backend_error")
        self.assertNotIn(FAKE_KEY, json.dumps(resp, ensure_ascii=False))
        self.assertNotIn(FAKE_LAN_IP, json.dumps(resp, ensure_ascii=False))
        self.assertIn("<REDACTED>", resp["failure"]["backend_detail"])
        raw = export_jsonl(router)
        self.assertNotIn(FAKE_KEY, raw)
        self.assertNotIn(FAKE_LAN_IP, raw)


class Test7ZeroNetwork(unittest.TestCase):
    """验收 #7：零网络——socket 全程封死跑完全场景；交付模块无网络库导入。"""

    def test_7_zero_network_by_construction(self):
        import socket

        blocked = []

        class _NoNetwork:
            def __init__(self, *args, **kwargs):
                blocked.append("socket")
                raise AssertionError("PRD-257/T5 验收场景禁止任何网络套接字")

        old = socket.socket
        socket.socket = _NoNetwork  # type: ignore[misc]
        try:
            router = _new_router()
            a, b = _send_both(router, "req-net")           # 场景 1：双后端切换
            self.assertEqual(a["status"], "ok")
            self.assertEqual(b["status"], "ok")
            chunks = router.stream("default", [{"role": "user", "content": "虚构"}], "req-net-s")
            self.assertEqual(chunks[-1]["type"], "done")    # 场景 2：流式
            cancel_ack("req-net-c", router._adapters["stub"])  # 场景 3：取消
            expired = open_session("stub", "stub-1", now=1.0, ttl_s=1)
            failed = router.send("default", [{"role": "user", "content": "虚构"}], "req-net-e",
                                 session=expired)
            self.assertEqual(failed["failure"]["code"], "SESSION_EXPIRED")  # 场景 4：会话过期
            raw = export_jsonl(router)
            self.assertTrue(raw)                            # 场景 5：导出
        finally:
            socket.socket = old  # type: ignore[misc]
        self.assertEqual(blocked, [])  # 全程零次套接字构造尝试
        # 静态面：PRD-257 交付模块不导入任何网络库
        for mod in ("harness_route.py", "route_audit.py"):
            src = (REPO_ROOT / "src" / "open_free_router" / mod).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"^\s*(?:import|from)\s+(socket|urllib|http|requests|aiohttp)\b",
                                        src, re.MULTILINE), f"{mod} 出现网络库导入")


class Test8DocsNoSecrets(unittest.TestCase):
    """验收 #8：文档与交付面无密钥/内网 IP（红线：占位符纪律）。"""

    def test_8_delivery_surface_no_secrets_no_lan_ip(self):
        for rel in DELIVERY_SURFACE:
            path = REPO_ROOT / rel
            self.assertTrue(path.exists(), f"交付面缺失: {rel}")
            text = path.read_text(encoding="utf-8")
            for pat in SECRET_PATTERNS:
                hits = [h for h in re.findall(pat, text) if h not in DECLARED_SYNTHETIC_SAMPLES]
                self.assertEqual(hits, [], f"{rel} 命中未申报密钥样式 {pat}: {hits}")
            lan_hits = [h for h in re.findall(LAN_IP_PATTERN, text)
                        if h not in DECLARED_SYNTHETIC_SAMPLES]
            self.assertEqual(lan_hits, [], f"{rel} 命中未申报内网 IP 样式: {lan_hits}")
            # 红线主面严格化：docs/ 与 src/ 连已申报合成样本也不允许（占位符纪律）
            if rel.startswith(("docs/", "src/")):
                for sample in DECLARED_SYNTHETIC_SAMPLES:
                    self.assertNotIn(sample, text, f"{rel} 出现合成样本（docs/src 应零命中）")
                self.assertNotRegex(text, r"https?://(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)")


if __name__ == "__main__":
    unittest.main()
