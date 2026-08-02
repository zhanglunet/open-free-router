import json
import stat
import threading
import time

from open_free_router.resilience import ResilienceManager, classify_failure


def test_runtime_state_is_owner_only_atomic_and_restored(tmp_path):
    path = tmp_path / "runtime-state.json"
    now = time.time()
    manager = ResilienceManager(
        provider_threshold=1, provider_cooldown=300,
        credential_cooldown=300, model_cooldown=300,
        state_path=path,
    )
    manager.record_failure("upstream", "model-a", classify_failure(503), now=now)
    manager.record_failure("upstream", "model-a", classify_failure(401), credential_slot=2, now=now)
    manager.record_failure("upstream", "model-b", classify_failure(404), now=now)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_text()
    assert "authorization" not in raw.lower()
    assert "api_key" not in raw.lower()
    restored = ResilienceManager(state_path=path).snapshot()
    assert restored["providers"]["upstream"]["state"] == "open"
    assert restored["credentials"]["upstream:slot-2"]["state"] == "terminal"
    assert "upstream/model-b" in restored["models"]


def test_corrupt_runtime_state_is_quarantined_and_recovered(tmp_path):
    path = tmp_path / "runtime-state.json"
    path.write_text("{not-json")
    manager = ResilienceManager(state_path=path)
    assert manager.snapshot()["providers"] == {}
    quarantined = list(tmp_path.glob("runtime-state.json.corrupt-*"))
    assert len(quarantined) == 1
    assert stat.S_IMODE(quarantined[0].stat().st_mode) == 0o600

    manager.record_failure("p", "m", classify_failure(401))
    assert json.loads(path.read_text())["schema_version"] == 1


def test_expired_runtime_state_is_not_restored(tmp_path):
    path = tmp_path / "runtime-state.json"
    document = {
        "schema_version": 1,
        "providers": {"p": {"state": "open", "failures": 4, "open_until": 1}},
        "credentials": [{"provider": "p", "slot": 0, "state": "cooldown", "cooldown_until": 1}],
        "models": [{"provider": "p", "model": "m", "failures": 1, "lockout_until": 1}],
    }
    path.write_text(json.dumps(document))
    snapshot = ResilienceManager(state_path=path).snapshot()
    assert snapshot["providers"] == {}
    assert snapshot["credentials"] == {}
    assert snapshot["models"] == {}


def test_concurrent_runtime_writes_leave_valid_document(tmp_path):
    path = tmp_path / "runtime-state.json"
    manager = ResilienceManager(state_path=path)
    threads = [
        threading.Thread(
            target=manager.record_failure,
            args=(f"provider-{index}", "model", classify_failure(401)),
            kwargs={"credential_slot": index},
        )
        for index in range(24)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    document = json.loads(path.read_text())
    assert len(document["credentials"]) == 24
    assert not list(tmp_path.glob(".runtime-state.json.*"))
