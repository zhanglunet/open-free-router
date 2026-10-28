"""PRD-280/T3: automatic rollback on health-check failure tests.

Covers the T3 acceptance slice of trend issue #1403: health-check failure
triggers restore + ``route_switch_failed`` event; healthy outcome rolls
nothing back; restored routes carry the pre-change fingerprint; event
fields are complete and placeholder-safe; determinism; and the red-line
structural assertion -- ``maybe_auto_rollback`` has no decision inputs
other than the health-check objective signal. All examples fictional.
"""
import ast
import inspect
import json

import pytest

from open_free_router import auto_rollback as ar
from open_free_router import config_snapshot as cs

# 全虚构样例：保留域 .invalid、占位 env 名，无真实主机名/IP/账号/密钥。
ROUTES_BEFORE = {
    "placeholder-provider-alpha": {
        "base_url": "https://placeholder-alpha.invalid",
        "api_key_env": "PLACEHOLDER_ALPHA_KEY",
        "models": [{"id": "model-a", "context": 8192}],
    },
    "placeholder-provider-beta": {
        "base_url": "https://placeholder-beta.invalid",
        "api_key_env": "PLACEHOLDER_BETA_KEY",
        "models": [{"id": "model-b", "context": 32768}],
    },
}
ROUTES_AFTER = {
    "placeholder-provider-alpha": {
        "base_url": "https://placeholder-alpha-v2.invalid",  # 变更后的新配置
        "api_key_env": "PLACEHOLDER_ALPHA_KEY",
        "models": [{"id": "model-a", "context": 8192}],
    },
}

TAKEN_AT = "2026-10-29T09:00:00+00:00"
FIXED_AT = "2026-10-29T12:00:00+00:00"
RAW_ERROR_BODY = "raw probe stack trace should never reach the audit ledger"


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """Clean slot/events + fixed clock: every test fully deterministic."""
    monkeypatch.setattr(cs, "_active_snapshot", None)
    monkeypatch.setattr(cs, "_audit_events", [])
    monkeypatch.setattr(ar, "_now", lambda: FIXED_AT)


def _snapshot_of(routes):
    return cs.take_snapshot(routes, TAKEN_AT)


def test_health_failure_triggers_rollback_and_event():
    snapshot = _snapshot_of(ROUTES_BEFORE)
    health_result = {"healthy": False, "detail": RAW_ERROR_BODY}
    result = ar.maybe_auto_rollback(snapshot, health_result)
    assert result["rolled_back"] is True
    assert result["reason"] == "health_check_failed"
    assert result["at"] == FIXED_AT
    # 还原出的即改前配置；调用方负责落盘（本模块零 IO）
    assert result["restored_routes"] == ROUTES_BEFORE
    # 事件入账本：一条 route_switch_failed，reason 固定为健康检查失败
    events = cs.export_events()
    assert [e["event"] for e in events] == ["route_switch_failed"]
    assert events[0]["reason"] == "health_check_failed"
    # 原始错误正文/provider 名绝不入账本（占位化纪律）
    blob = json.dumps(events, ensure_ascii=False)
    assert RAW_ERROR_BODY not in blob
    assert "placeholder-provider" not in blob
    assert "key" not in blob.lower()


def test_health_ok_no_rollback():
    snapshot = _snapshot_of(ROUTES_BEFORE)
    for health_result in (
        {"healthy": True},
        {"healthy": True, "detail": RAW_ERROR_BODY},  # detail 永不被读
        {},       # 缺 healthy 字段：非客观信号，不回滚
        None,     # 结果缺失同上（fail-safe：只认客观信号）
    ):
        assert ar.maybe_auto_rollback(snapshot, health_result) is None
    # 无回滚、无事件、槽位原样保留
    assert cs.export_events() == []
    assert cs.active_snapshot() == snapshot


def test_fingerprint_restored_after_rollback():
    snapshot = _snapshot_of(ROUTES_BEFORE)
    fingerprint_before = snapshot["fingerprint"]
    result = ar.maybe_auto_rollback(snapshot, {"healthy": False})
    restored = result["restored_routes"]
    # 回滚后重算指纹 = 改前指纹（逐字节回到快照内容）
    assert cs.fingerprint_routes(restored) == fingerprint_before
    assert cs.fingerprint_routes(restored) == cs.fingerprint_routes(ROUTES_BEFORE)
    assert restored != ROUTES_AFTER  # 新配置被整体还原掉
    # 快照槽不清空：回滚本身可再回滚（T2 语义）
    assert cs.active_snapshot() == snapshot


def test_event_fields_complete():
    _snapshot_of(ROUTES_BEFORE)
    ar.maybe_auto_rollback(_snapshot_of(ROUTES_BEFORE), {"healthy": False})
    (event,) = cs.export_events()
    # 字段齐全且只含占位化三字段：事件类型 / 失败类别 / 时间
    assert set(event) == {"event", "reason", "at"}
    assert event == {
        "event": "route_switch_failed",
        "reason": "health_check_failed",
        "at": FIXED_AT,
    }
    assert all(event.values())  # 无空字段
    assert json.loads(json.dumps(event)) == event  # 可 JSON 序列化


def test_red_line_single_objective_trigger():
    """红线断言：自动回滚触发面仅健康检查这一个客观信号。

    结构性证明（T4 将做全仓扫描，这里锁定本模块）：
    1. maybe_auto_rollback 的决策输入参数只有 snapshot / health_result，
       无 *args/**kwargs/keyword-only 等其他决策入口；
    2. 模块公开函数只有 maybe_auto_rollback（无第二触发入口）；
    3. 源码中 restore / record_switch_failed 调用点全部位于
       maybe_auto_rollback 之内（模块其他位置零还原、零事件写入）；
    4. 全模块读取 "healthy" 字段的位置恰一处（唯一决策门）；
    5. 红线声明写进代码注释。
    """
    source = inspect.getsource(ar)
    tree = ast.parse(source)

    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    public = [f.name for f in functions if not f.name.startswith("_")]
    assert public == ["maybe_auto_rollback"]

    fn = next(f for f in functions if f.name == "maybe_auto_rollback")
    assert [a.arg for a in fn.args.args] == ["snapshot", "health_result"]
    assert fn.args.vararg is None and fn.args.kwarg is None
    assert fn.args.kwonlyargs == []

    def _rollback_call_sites(node):
        sites = []
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            name = (call.func.id if isinstance(call.func, ast.Name)
                    else call.func.attr if isinstance(call.func, ast.Attribute)
                    else None)
            if name in {"restore", "record_switch_failed"}:
                sites.append(call)
        return sites

    inside = _rollback_call_sites(fn)
    outside = []
    for top in tree.body:
        if top is fn:
            continue
        outside.extend(_rollback_call_sites(top))
    assert inside and not outside  # 触发与入账全部收在唯一入口内

    healthy_reads = [n for n in ast.walk(tree) if isinstance(n, ast.Constant)
                     and n.value == "healthy"]
    assert len(healthy_reads) == 1  # 唯一决策门：只读一次客观信号

    assert "客观信号" in source and "唯一" in source  # 红线在代码注释里
    assert "health_check_failed" in source  # 固定失败类别标签


def test_deterministic_decision(monkeypatch):
    # 同输入同决策：固定时钟下两次调用结果逐字段相等
    snapshot = _snapshot_of(ROUTES_BEFORE)
    first = ar.maybe_auto_rollback(snapshot, {"healthy": False})
    second = ar.maybe_auto_rollback(snapshot, {"healthy": False})
    assert first == second == {
        "rolled_back": True,
        "reason": "health_check_failed",
        "at": FIXED_AT,
        "fingerprint": cs.fingerprint_routes(ROUTES_BEFORE),
        "restored_routes": ROUTES_BEFORE,
    }
    # 决策与时刻无关：换个时钟、换份等价快照，决策仍一致（仅 at 不同）
    monkeypatch.setattr(ar, "_now", lambda: "2026-10-29T23:59:59+00:00")
    twin = cs.take_snapshot(ROUTES_BEFORE, "2026-10-29T20:00:00+00:00")
    other = ar.maybe_auto_rollback(twin, {"healthy": False})
    assert other["rolled_back"] is True
    assert other["reason"] == "health_check_failed"
    assert other["restored_routes"] == ROUTES_BEFORE
    assert other["at"] == "2026-10-29T23:59:59+00:00"


def test_health_failure_without_snapshot_records_event_only():
    # 客观信号成立但无快照可退：如实记事件，不假装回滚成功
    result = ar.maybe_auto_rollback(None, {"healthy": False})
    assert result["rolled_back"] is False
    assert result["reason"] == "no_snapshot"
    assert result["restored_routes"] is None
    assert [e["event"] for e in cs.export_events()] == ["route_switch_failed"]
    # 健康正常时连事件都不产生（对照）
    assert ar.maybe_auto_rollback(None, {"healthy": True}) is None
    assert len(cs.export_events()) == 1
