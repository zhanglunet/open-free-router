"""PRD-280/T2: route-config snapshot minimal implementation tests.

Covers the T2 acceptance slice of trend issue #1402: snapshot fields, deep
copy isolation, fingerprint determinism, restore round-trip (including nested
payloads), tamper rejection (fingerprint *and* payload), the
``route_switch_failed`` audit event, event serializability and the single-slot
overwrite semantics. Pure functions, zero network, all examples fictional.
"""
import copy
import json

import pytest

from open_free_router import config_snapshot as cs


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """Give every test a clean single slot and empty audit event list."""
    monkeypatch.setattr(cs, "_active_snapshot", None)
    monkeypatch.setattr(cs, "_audit_events", [])


# 全虚构样例：保留域 .invalid、占位 env 名，无真实主机名/IP/账号/密钥。
ROUTES = {
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

TAKEN_AT = "2026-10-29T09:00:00Z"


def test_snapshot_fields():
    snapshot = cs.take_snapshot(ROUTES, TAKEN_AT)
    assert snapshot["schema"] == "route-config-snapshot-v1"
    assert snapshot["routes"] == ROUTES
    assert snapshot["taken_at"] == TAKEN_AT
    assert snapshot["fingerprint"] == cs.fingerprint_routes(ROUTES)
    assert len(snapshot["fingerprint"]) == 16
    assert set(snapshot) == {"schema", "routes", "fingerprint", "taken_at"}


def test_snapshot_deep_copies_input_and_slot():
    routes = copy.deepcopy(ROUTES)
    snapshot = cs.take_snapshot(routes, TAKEN_AT)

    routes["placeholder-provider-alpha"]["models"].append({"id": "model-x"})
    snapshot["routes"]["placeholder-provider-beta"]["models"].append({"id": "model-y"})

    # 输入被改、返回值被改，槽内快照都不受影响——还原凭据不被污染
    assert cs.active_snapshot()["routes"] == ROUTES
    assert cs.fingerprint_routes(cs.active_snapshot()["routes"]) == snapshot["fingerprint"]


def test_fingerprint_deterministic():
    reordered = {
        "placeholder-provider-beta": ROUTES["placeholder-provider-beta"],
        "placeholder-provider-alpha": ROUTES["placeholder-provider-alpha"],
    }
    reordered["placeholder-provider-alpha"] = {
        "models": [{"context": 8192, "id": "model-a"}],
        "api_key_env": "PLACEHOLDER_ALPHA_KEY",
        "base_url": "https://placeholder-alpha.invalid",
    }
    # 内容相等（键插入序不同）→ 指纹一致；内容不同 → 指纹不同
    assert cs.fingerprint_routes(reordered) == cs.fingerprint_routes(ROUTES)
    changed = copy.deepcopy(ROUTES)
    changed["placeholder-provider-alpha"]["models"][0]["context"] = 4096
    assert cs.fingerprint_routes(changed) != cs.fingerprint_routes(ROUTES)


def test_restore_roundtrip_nested():
    snapshot = cs.take_snapshot(ROUTES, TAKEN_AT)
    restored = cs.restore(snapshot)
    assert restored == ROUTES
    # 还原是深拷贝：改还原结果不影响快照本身（回滚可再回滚的前提）
    restored["placeholder-provider-alpha"]["models"][0]["id"] = "mutated"
    assert cs.restore(cs.active_snapshot()) == ROUTES


def test_restore_rejects_tampered_fingerprint():
    snapshot = cs.take_snapshot(ROUTES, TAKEN_AT)
    snapshot["fingerprint"] = "0" * 16
    with pytest.raises(cs.FingerprintMismatch):
        cs.restore(snapshot)


def test_restore_rejects_tampered_routes():
    snapshot = cs.take_snapshot(ROUTES, TAKEN_AT)
    snapshot["routes"]["placeholder-provider-alpha"]["base_url"] = (
        "https://tampered.invalid"
    )
    with pytest.raises(cs.FingerprintMismatch):
        cs.restore(snapshot)


def test_switch_failed_event_recorded():
    event = cs.record_switch_failed("provider_unavailable", TAKEN_AT)
    assert event == {
        "event": "route_switch_failed",
        "reason": "provider_unavailable",
        "at": TAKEN_AT,
    }
    events = cs.export_events()
    assert len(events) == 1
    assert events[0]["event"] == "route_switch_failed"
    # 事件只留占位化的 reason，不留正文与凭据
    assert "key" not in json.dumps(events).lower()


def test_events_json_serializable():
    cs.record_switch_failed("credential_invalid", TAKEN_AT)
    cs.record_switch_failed("rate_limited", "2026-10-29T09:01:00Z")
    events = cs.export_events()
    assert json.loads(json.dumps(events)) == events
    # 导出是深拷贝：改导出结果不影响账本
    events.clear()
    assert len(cs.export_events()) == 2


def test_single_slot_overwrites():
    first = cs.take_snapshot(ROUTES, TAKEN_AT)
    changed = copy.deepcopy(ROUTES)
    changed.pop("placeholder-provider-beta")
    second = cs.take_snapshot(changed, "2026-10-29T09:05:00Z")

    active = cs.active_snapshot()
    assert active == second
    assert active["taken_at"] == "2026-10-29T09:05:00Z"
    assert active["routes"] == changed
    assert first["taken_at"] == TAKEN_AT  # 旧快照对象仍可独立 restore
    assert cs.restore(first) == ROUTES
