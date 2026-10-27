"""PRD-173/T5 acceptance: BYOS 样例接入端到端走通「BYOS 优先 + 换路/用量记账」.

trend #869, adopted PRD-173（``adopted/PRD-173-ofr-byos-access.md``）.  The
acceptance objects are the T1-T4 changes already merged into main -- the
``byos`` package (adapters/env/wiring), the subscribed-first bucketing and
the quota/audit wiring.  T5 discipline: tests and documentation only, so
nothing here fixes them; every criterion maps to one numbered test:

| # | 验收项 | 测试 |
|---|---|---|
| 1 | 本地演示 BYOS 优先：样例接入走通——env 三变量 → 适配注册入内存 → 订阅桶稳定前置 → 请求先走 BYOS 网关 | ``test_1_byos_gateway_registered_from_env_leads_the_plan`` |
| 2 | 换路记账对照：同一 429 形态，有/无结构化额度头各一次——两对 decision/outcome 完整成对落账，配额证据诚实分档（不猜订阅余额） | ``test_2_simulated_failover_pairs_record_honest_quota_evidence`` |
| 3 | 用量记账对照：BYOS 成功请求落既有 usage_events 与 PRD-096 额度通道，BYOS 不单开表 | ``test_3_byos_usage_lands_in_the_existing_channels`` |
| 4 | 密钥仅 env：registry 只存变量名不存值；请求时经 env 取值；落盘 registry 原始字节无密钥值 | ``test_4_keys_live_only_in_env`` |
| 5 | 无密钥入仓：账本文件原始字节零命中 + PRD-173 交付面（byos 源码/测试/文档）占位符纪律扫描 | ``test_5_no_secrets_in_ledger_or_repo_docs`` |

All simulations run the real ``RoutePlanner``（``prefer_subscribed=True``，
即生产 ``byos.prefer_subscribed`` 开关）and the real ``UpstreamExecutor``
with the factory-built audit hook into a real ``usage.db``.  The BYOS
upstreams are throwaway loopback HTTP servers; the only credential in play
is a fake key created by this module and delivered through the real
environment-variable path.  No real hostnames, accounts or keys appear
anywhere -- assertion 5 enforces that discipline on the repo itself.
"""
from __future__ import annotations

import itertools
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from open_free_router.analytics import (
    SENSITIVE_PATTERN,
    AnalyticsStore,
)
from open_free_router.byos.wiring import doctor_checks, register_byos_providers
from open_free_router.executor import OpenedRoute, UpstreamExecutor
from open_free_router.quota_view import quota_view
from open_free_router.registry import Registry
from open_free_router.resilience import ResilienceManager
from open_free_router.routing import RoutePlanner
from open_free_router.telemetry import RouteDecisionStore

FAKE_KEY = "test-key-0123456789abcdef"  # 本模块自造的假密钥，绝不代表任何真实凭据
KEY_ENV = "OFR_BYOS_OPENAI_COMPAT_API_KEY"
BASE_URL_ENV = "OFR_BYOS_OPENAI_COMPAT_BASE_URL"
MODEL_ENV = "OFR_BYOS_OPENAI_COMPAT_MODEL"
BYOS_NAME = "byos-openai-compat"
BYOS_MODEL = "byos-oc/sub-model"  # T1 §5: 候选以 byos-oc/<model> 前缀自明
FREE_NAME = "free-b"
FREE_MODEL = "fb/model"

DECISION_COLUMNS = (
    "decision_id", "at", "request_id", "from_candidate", "to_candidate",
    "reason", "attempt", "elapsed_ms", "ttfb_ms", "circuit",
    "requested_model", "strategy", "quota_band", "quota_source",
    "quota_classification", "doctor_verdict", "doctor_failed_checks", "doctor_at",
    "honesty",
)
OUTCOME_COLUMNS = (
    "outcome_id", "decision_id", "settled_at", "outcome", "status_code",
    "error_kind", "ttfb_ms", "total_latency_ms", "attempt",
)


class _ByosConfig:
    """Minimal stand-in for the parsed config.yaml ``byos`` block (T1 §3.4)."""

    def __init__(self, enabled=True, adapters=(BYOS_NAME,)):
        self.byos_enabled = enabled
        self.byos_adapters = list(adapters)


# ------------------------------------------------------------- fake upstreams ---

class _Handler(BaseHTTPRequestHandler):
    """Configurable throwaway upstream. Placeholder credentials only."""

    status = 200
    quota_headers = ()
    seen_auth: list[str] = []

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).seen_auth.append(self.headers.get("Authorization") or "")
        body = b'{"model": "sub-model", "ok": true}'
        if self.status >= 400:
            body = b'{"error": {"message": "synthetic failure"}}'
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for header, value in self.quota_headers:
            self.send_header(header, value)
        self.end_headers()
        self.wfile.write(body)


_HANDLER_SEQ = itertools.count(1)


def _handler(status=200, quota_headers=()):
    return type(
        f"ByosAcceptanceHandler{next(_HANDLER_SEQ)}",
        (_Handler,),
        {"status": status, "quota_headers": quota_headers, "seen_auth": []},
    )


def _start(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _free_provider(port):
    """One ordinary free-tier registry provider: loopback URL, placeholder key."""
    return {
        "prefix": "fb",
        "upstream_url": f"http://127.0.0.1:{port}/v1",
        "api_key": f"placeholder-{FREE_NAME}",
        "models": [{"id": "model", "tool_calling": True}],
    }


class _Sleeper:
    """Records backoff sleeps without waiting."""

    def __init__(self):
        self.delays = []

    def __call__(self, delay):
        self.delays.append(delay)


def _byos_env(port):
    return {
        KEY_ENV: FAKE_KEY,
        BASE_URL_ENV: f"http://127.0.0.1:{port}/v1",
        MODEL_ENV: "sub-model",
    }


def _build(registry, tmp_path, name="usage.db"):
    """Production wiring: real planner (subscribed first) over the real executor."""
    store = AnalyticsStore(tmp_path / name, retention_days=30)
    decisions = RouteDecisionStore(analytics=store)
    resilience = ResilienceManager()
    planner = RoutePlanner(
        registry,
        {"fallback": {"max_attempts": 5}, "policy": {"backoff_jitter": 0}},
        prefer_subscribed=True,
    )
    executor = UpstreamExecutor(
        planner, resilience, decisions=decisions,
        sleeper=_Sleeper(), timeout=5,
    )
    return store, decisions, resilience, planner, executor


def _registry(byos_port, free_port):
    """One env-resolved BYOS gateway plus one ordinary free provider."""
    registry = Registry({})
    assert register_byos_providers(registry, _ByosConfig(), env=_byos_env(byos_port)) == [
        BYOS_NAME,
    ]
    registry.add_provider(Registry({FREE_NAME: _free_provider(free_port)}).providers[FREE_NAME])
    return registry


# ----------------------------------------------------------- ledger readers ---

def _ledger(store):
    connection = sqlite3.connect(str(store.path))
    try:
        decisions = [
            dict(zip(DECISION_COLUMNS, row))
            for row in connection.execute(
                f"SELECT {', '.join(DECISION_COLUMNS)} FROM failover_decisions ORDER BY rowid")
        ]
        outcomes = [
            dict(zip(OUTCOME_COLUMNS, row))
            for row in connection.execute(
                f"SELECT {', '.join(OUTCOME_COLUMNS)} FROM failover_outcomes ORDER BY rowid")
        ]
        return decisions, outcomes
    finally:
        connection.close()


def _ledger_files(store):
    """Raw bytes of usage.db plus its WAL/SHM siblings (zero-hit scan input)."""
    paths = [store.path, Path(f"{store.path}-wal"), Path(f"{store.path}-shm")]
    return [path.read_bytes() for path in paths if path.exists()]


# ------------------------------------------------------- criterion tests ---

def test_1_byos_gateway_registered_from_env_leads_the_plan(tmp_path, monkeypatch):
    """本地演示 BYOS 优先：样例接入 env → 注册 → 订阅桶前置 → 请求先走 BYOS."""
    monkeypatch.setenv(KEY_ENV, FAKE_KEY)  # 请求时经 env 取值（密钥仅 env）
    serving_h = _handler(200)
    serving = _start(serving_h)
    free_h = _handler(200)
    free_peer = _start(free_h)
    try:
        registry = _registry(serving.server_address[1], free_peer.server_address[1])

        # 真实 planner：无任何 pin，订阅桶整体前置且桶内相对顺序不变。
        store, decisions, resilience, planner, executor = _build(registry, tmp_path)
        plan = planner.plan("auto")
        assert [target.canonical_id for target in plan.candidates] == [
            BYOS_MODEL, FREE_MODEL,
        ]
        assert plan.candidates[0].byos_class == "subscribed"
        assert plan.candidates[1].byos_class != "subscribed"
        assert plan.strategy == "priority"  # 稳定分桶发生在策略之后，不换策略

        result = executor.execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        assert result.target.provider_name == BYOS_NAME  # 请求先走 BYOS 网关
        assert serving_h.seen_auth == [f"Bearer {FAKE_KEY}"]  # 网关收到 env 中的密钥
        assert free_h.seen_auth == []  # 免费候选未被触及
        result.close()

        # 样例接入的观察面：doctor byos 组两条检查全绿，detail 只含状态枚举。
        items = doctor_checks(_ByosConfig(), env=_byos_env(serving.server_address[1]),
                              probe=lambda provider: {"ok": True})
        assert [(item["label"], item["severity"]) for item in items] == [
            ("byos env present", "ok"), ("byos probe reachable", "ok"),
        ]
        assert FAKE_KEY not in repr(items)  # detail 绝不含 env 值
    finally:
        serving.shutdown()
        free_peer.shutdown()


def test_2_simulated_failover_pairs_record_honest_quota_evidence(tmp_path, monkeypatch):
    """换路记账对照：同一 429，有无结构化额度头 → 两对完整成对、证据诚实分档."""
    monkeypatch.setenv(KEY_ENV, FAKE_KEY)
    headed = _start(_handler(429, quota_headers=(
        ("X-RateLimit-Limit-Requests", "100"),
        ("X-RateLimit-Remaining-Requests", "0"),
    )))
    bare = _start(_handler(429))
    free_a = _start(_handler(200))
    free_b = _start(_handler(200))
    try:
        # --- 对照组 A：BYOS 网关给了数字额度头 → 走既有 PRD-096 归一化 --------
        registry = _registry(headed.server_address[1], free_a.server_address[1])
        store, decisions, resilience, planner, executor = _build(registry, tmp_path)
        result = executor.execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        assert result.target.provider_name == FREE_NAME
        result.close()
        decisions_a, outcomes_a = _ledger(store)
        assert [(d["from_candidate"], d["to_candidate"], d["reason"])
                for d in decisions_a] == [(BYOS_MODEL, FREE_MODEL, "rate_limited")]
        decision, outcome = decisions_a[0], outcomes_a[0]
        assert outcome["decision_id"] == decision["decision_id"]  # 成对完整
        assert outcome["outcome"] == "success" and outcome["attempt"] == 2
        assert decision["quota_band"] == "exhausted"
        assert decision["quota_source"] == "upstream_headers"
        assert decision["quota_classification"] == "rate_limited"
        store.close()

        # --- 对照组 B：同样的 429 但没有任何头 → 诚实 unknown/event，不猜余额 -
        registry = _registry(bare.server_address[1], free_b.server_address[1])
        store, decisions, resilience, planner, executor = _build(
            registry, tmp_path, name="usage-bare.db")
        result = executor.execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        assert result.target.provider_name == FREE_NAME
        result.close()
        decisions_b, outcomes_b = _ledger(store)
        decision, outcome = decisions_b[0], outcomes_b[0]
        assert outcome["decision_id"] == decision["decision_id"]
        assert outcome["outcome"] == "success"
        assert decision["quota_band"] == "unknown"  # 不猜订阅余额（T1 §5）
        assert decision["quota_source"] == "event"
        assert decision["quota_classification"] == "rate_limited"
        store.close()
    finally:
        for server in (headed, bare, free_a, free_b):
            server.shutdown()


def test_3_byos_usage_lands_in_the_existing_channels(tmp_path, monkeypatch):
    """用量记账对照：成功请求落既有 usage_events；额度面板读同一 PRD-096 通道."""
    monkeypatch.setenv(KEY_ENV, FAKE_KEY)
    serving = _start(_handler(200, quota_headers=(
        ("x-ratelimit-limit", "100"),
        ("x-ratelimit-remaining", "50"),
    )))
    free_peer = _start(_handler(200))
    try:
        registry = _registry(serving.server_address[1], free_peer.server_address[1])
        store, decisions, resilience, planner, executor = _build(registry, tmp_path)
        result = executor.execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        assert result.target.provider_name == BYOS_NAME

        # proxy 关闭路由走 complete：BYOS 用量落既有 usage_events，不单开表。
        decisions.complete(result.request_id, 12.5)
        rows = store._rows()
        assert [row["provider"] for row in rows] == [BYOS_NAME]
        assert rows[0]["model"] == BYOS_MODEL
        assert rows[0]["status"] == "success"

        # 同一轮的额度视图：数字头 → sufficient / upstream_headers。
        panel = quota_view(resilience.snapshot(), providers=[BYOS_NAME])
        row = panel["items"][0]
        assert (row["provider"], row["band"], row["source"]) == (
            BYOS_NAME, "sufficient", "upstream_headers",
        )
        result.close()
        store.close()
    finally:
        serving.shutdown()
        free_peer.shutdown()


def test_4_keys_live_only_in_env(tmp_path, monkeypatch):
    """密钥仅 env：registry 只存变量名；值只在请求时经环境变量进入内存."""
    monkeypatch.setenv(KEY_ENV, FAKE_KEY)  # effective_key 请求时经 env 读取
    registry = Registry({})
    registered = register_byos_providers(registry, _ByosConfig(),
                                         env=_byos_env(9))
    assert registered == [BYOS_NAME]
    provider = registry.providers[BYOS_NAME]
    # T1 §6 redline 1: 内存中的配置也只有变量名，值留在 env。
    assert provider.api_key == ""
    assert provider.api_key_env == KEY_ENV
    assert provider.effective_key == FAKE_KEY  # effective_key 即 env 读取

    # 落盘对照：registry.save 写出的文件没有值，只有变量名（T1 §6 验收口径）。
    saved = tmp_path / "registry.yaml"
    registry.save(saved)
    blob = saved.read_bytes()
    assert FAKE_KEY.encode() not in blob
    assert KEY_ENV.encode() in blob
    assert b"refresh_method: byos" in blob  # BYOS 标记照常落盘（无敏感信息）

    # 请求时经 env 取值：换一个 loopback 网关发真请求，Bearer 值与 env 一致。
    serving_h = _handler(200)
    serving = _start(serving_h)
    free_peer = _start(_handler(200))
    try:
        registry = _registry(serving.server_address[1], free_peer.server_address[1])
        store, decisions, resilience, planner, executor = _build(registry, tmp_path)
        result = executor.execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        assert serving_h.seen_auth == [f"Bearer {FAKE_KEY}"]
        result.close()
        store.close()
    finally:
        serving.shutdown()
        free_peer.shutdown()


def test_5_no_secrets_in_ledger_or_repo_docs(tmp_path, monkeypatch):
    """无密钥入仓：账本原始字节零命中 + PRD-173 交付面占位符纪律扫描."""
    # --- A) 账本：换路对 + 用量行都落进同一 usage.db 后做原始字节扫描 --------
    monkeypatch.setenv(KEY_ENV, FAKE_KEY)
    byos_up_h = _handler(429, quota_headers=(
        ("X-RateLimit-Limit-Requests", "100"),
        ("X-RateLimit-Remaining-Requests", "0"),
    ))
    byos_up = _start(byos_up_h)
    free_up = _start(_handler(200))
    try:
        registry = _registry(byos_up.server_address[1], free_up.server_address[1])
        store, decisions, resilience, planner, executor = _build(registry, tmp_path)
        result = executor.execute("auto", "chat/completions", {"messages": []})
        assert isinstance(result, OpenedRoute)
        result.close()
        # 凭据确实在场上：BYOS 上游看到了假密钥（写入侧有密钥可用）。
        assert byos_up_h.seen_auth == [f"Bearer {FAKE_KEY}"]
        decision_rows, _ = _ledger(store)
        assert decision_rows  # 换路对已落账
        store.close()

        blob = b"".join(_ledger_files(
            SimpleNamespace(path=tmp_path / "usage.db")))
        assert blob
        decoded = blob.decode("utf-8", "ignore")
        # a) 仓库红名单模式本身零命中。
        assert SENSITIVE_PATTERN.search(decoded) is None
        # b) 场上真实用过的假密钥与其变量名都不在账本字节里（变量名也不入账）。
        for banned in (FAKE_KEY, KEY_ENV, BASE_URL_ENV, MODEL_ENV,
                       "Authorization", "Bearer"):
            assert banned not in decoded, banned
    finally:
        byos_up.shutdown()
        free_up.shutdown()

    # --- B) 仓库占位符纪律：PRD-173 交付面无真实主机名/IP/账号/密钥样式 ------
    repo = Path(__file__).resolve().parents[1]
    surfaces = sorted((repo / "src" / "open_free_router" / "byos").rglob("*.py"))
    surfaces += [
        repo / "tests" / "test_byos_adapters.py",
        repo / "tests" / "test_byos_routing.py",
        repo / "tests" / "test_byos_wiring.py",
        repo / "tests" / "test_byos_acceptance.py",
        repo / "docs" / "plans" / "2026-10-25-byos-access.md",
    ]
    readme = (repo / "README.md").read_text(encoding="utf-8")
    byos_readme = "## BYOS：" + readme.split("## BYOS：", 1)[1].split("\n## ", 1)[0]
    surfaces.append(byos_readme)
    # 私网地址 / 常见密钥样式 / 内网主机名后缀一律不允许；环回 127.0.0.1 与
    # <byos-*-host> 占位符、公开站点 URL 不在扫描模式内。
    banned_patterns = {
        "private-ip": re.compile(
            r"\b(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+"
            r"|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)\b"),
        "key-material": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|AKIA[0-9A-Z]{16})\b"),
        "internal-host": re.compile(
            r"https?://[^\s\"'<>)]*\.(?:local|lan|internal|corp|intranet|priv)\b"),
    }
    for surface in surfaces:
        text = surface.read_text(encoding="utf-8") if isinstance(surface, Path) else surface
        for name, pattern in banned_patterns.items():
            hits = pattern.findall(text)
            assert hits == [], f"{name} in {getattr(surface, 'name', 'README BYOS')}: {hits}"
