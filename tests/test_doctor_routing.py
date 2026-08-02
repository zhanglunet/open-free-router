import json
from types import SimpleNamespace

import pytest

from open_free_router.cli import cmd_doctor
from open_free_router.registry import Registry
from open_free_router.routing import diagnose_routing_config


def _registry():
    return Registry({
        "provider": {
            "prefix": "p", "api_key": "test-placeholder",
            "models": [{"id": "coder", "tool_calling": True}],
        }
    })


def test_routing_diagnostics_find_silent_normalization_and_bad_candidates():
    issues = diagnose_routing_config({
        "fallback": {"max_attempts": 99, "enabled": "false", "explicit_model": True},
        "aliases": {
            "preferred": {
                "candidates": ["missing", "missing", "preferred"],
                "require": {"tool_calling": "yes"},
            }
        },
    }, _registry())
    codes = {issue.code for issue in issues}
    assert "invalid_max_attempts" in codes
    assert "invalid_boolean" in codes
    assert "explicit_fallback_enabled" in codes
    assert "candidate_not_registered" in codes
    assert "duplicate_candidate" in codes
    assert "alias_candidate_reference" in codes
    assert "no_eligible_candidates" in codes
    assert all(issue.path and issue.message for issue in issues)


def test_doctor_json_reports_actionable_routing_error(tmp_path, monkeypatch, capsys):
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "provider:\n  prefix: p\n  api_key: test-placeholder\n"
        "  models:\n    - id: coder\n      tool_calling: true\n"
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "registry: registry.yaml\nproxy:\n  port: 65530\nui:\n  port: 65531\n"
        "routing:\n  aliases:\n    preferred:\n      candidates: [missing]\n"
    )
    monkeypatch.setenv("OPEN_FREE_ROUTER_CONFIG", str(config))
    with pytest.raises(SystemExit) as stopped:
        cmd_doctor(SimpleNamespace(json=True))
    assert stopped.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    issue = next(item for item in report["routing"] if item["code"] == "candidate_not_registered")
    assert issue["path"] == "routing.aliases.preferred.candidates[0]"
    assert "open-free-router models" in issue["fix"]


def test_doctor_json_accepts_valid_routing(tmp_path, monkeypatch, capsys):
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "provider:\n  prefix: p\n  api_key: test-placeholder\n"
        "  models:\n    - id: coder\n      tool_calling: true\n"
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "registry: registry.yaml\nproxy:\n  port: 65530\nui:\n  port: 65531\n"
        "routing:\n  fallback:\n    max_attempts: 3\n"
    )
    monkeypatch.setenv("OPEN_FREE_ROUTER_CONFIG", str(config))
    cmd_doctor(SimpleNamespace(json=True))
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert not [item for item in report["routing"] if item["severity"] == "error"]
    assert any(item["path"] == "routing.aliases.auto/free" for item in report["routing"])


def test_doctor_json_rejects_invalid_free_tier_evidence(tmp_path, monkeypatch, capsys):
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "provider:\n  prefix: p\n  api_key: test-placeholder\n"
        "  free_tier:\n    type: recurring_quota\n    limit: many\n"
        "    evidence_url: http://localhost/private\n"
        "  models:\n    - id: coder\n      tool_calling: true\n"
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "registry: registry.yaml\nproxy:\n  port: 65530\nui:\n  port: 65531\n"
    )
    monkeypatch.setenv("OPEN_FREE_ROUTER_CONFIG", str(config))
    with pytest.raises(SystemExit):
        cmd_doctor(SimpleNamespace(json=True))
    report = json.loads(capsys.readouterr().out)
    issue = report["free_tier"]["issues"][0]
    assert issue["code"] == "invalid_free_tier_evidence"
    assert issue["path"] == "providers.provider.free_tier"
    assert "limit must be" in issue["message"]
    assert "evidence_url" in issue["message"]
