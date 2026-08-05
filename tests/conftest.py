"""Keep the suite from writing into the developer's real home directory.

``sync.py`` resolves every client config path from ``Path.home()`` at import
time. Tests patch the constant for the client under test, which is enough right
up until a code path reaches for a *different* one -- the Kimi legacy-config
fallback did exactly that, and a test patching only ``KIMI_CONFIG`` rewrote the
real ``~/.kimi/config.toml`` on any machine that had one.

This fixture repoints all of them at a per-test temporary directory, so a test
that forgets to patch one fails harmlessly instead of editing real config. Tests
that patch a constant themselves still win: ``mock.patch`` applies on top.
"""
from __future__ import annotations

import pytest

_HOME_DERIVED = (
    "OMP_MODELS",
    "OMP_CONFIG",
    "OPENCODE_CONFIG",
    "HERMES_CONFIG",
    "PI_MODELS_PATH",
    "CODEX_PROFILE",
    "CODEX_MODEL_CATALOG",
    "CLAUDE_SETTINGS",
    "KIMI_CONFIG",
    "KIMI_LEGACY_CONFIG",
    "OPENCLAW_CONFIG",
    "WORKBUDDY_MODELS",
    "BACKUP_DIR",
)


@pytest.fixture(autouse=True)
def _isolate_agent_config_paths(tmp_path, monkeypatch):
    from open_free_router import sync

    sandbox = tmp_path / "fake-home"
    for name in _HOME_DERIVED:
        original = getattr(sync, name)
        # Keep each path's shape (parent directory and filename) so behaviour
        # that depends on whether a parent exists is unchanged; only the root
        # moves. Nothing is created: a missing parent still means "not
        # installed", which is what the unpatched constants signalled before.
        monkeypatch.setattr(sync, name, sandbox / original.parent.name / original.name)
