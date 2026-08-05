"""CLI-level tests for `sync --kimi-available-only`.

The flag's behaviour lives in cmd_sync, which no test reached: an adversarial
review found that deleting the up-front rejection, or replaying stale probe
evidence as current, left the whole suite green.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml

from open_free_router.cli import cmd_sync


def _args(**kw):
    base = dict(
        agent="kimi", diff=True, codex_model=None,
        claude_model=None, kimi_available_only=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _install_config(tmp_path, monkeypatch, checked_at=None):
    """Point the CLI at a throwaway config, optionally with probe evidence."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "registry": str(tmp_path / "registry.yaml"),
        "data_dir": str(data_dir),
    }))
    monkeypatch.setenv("OPEN_FREE_ROUTER_CONFIG", str(config_path))
    if checked_at is not None:
        (data_dir / "probe-results.json").write_text(json.dumps({
            "results": {"groq/gpt-oss-20b": {
                "provider": "groq", "model": "gpt-oss-20b",
                "display_id": "gq/gpt-oss-20b", "ok": True,
                "status": "http_200", "latency_ms": 10,
                "error": "", "checked_at": checked_at,
            }},
        }))


def test_flag_is_refused_when_kimi_is_not_among_the_agents(tmp_path, monkeypatch):
    _install_config(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        cmd_sync(_args(agent="codex"))
    assert "kimi" in str(excinfo.value).lower()


def test_stale_evidence_asks_for_a_re_probe_rather_than_a_first_probe(tmp_path, monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    _install_config(tmp_path, monkeypatch, checked_at=old)
    with pytest.raises(SystemExit) as excinfo:
        cmd_sync(_args())
    message = str(excinfo.value)
    # "re-run" and "run one first" are different asks; a week-old success must
    # never be replayed as a statement about right now.
    assert "too old" in message and "Re-run" in message


def test_absent_evidence_asks_for_a_first_probe(tmp_path, monkeypatch):
    _install_config(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        cmd_sync(_args())
    message = str(excinfo.value)
    assert "too old" not in message
    assert "Run the dashboard Live Status probe first" in message


def test_fresh_evidence_gets_past_the_gate(tmp_path, monkeypatch):
    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _install_config(tmp_path, monkeypatch, checked_at=fresh)
    cmd_sync(_args())  # must not raise
