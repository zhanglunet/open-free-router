import json
from types import SimpleNamespace

from open_free_router.cli import cmd_route_query


def _setup(tmp_path, monkeypatch, registry_text, config_text=""):
    (tmp_path / "registry.yaml").write_text(registry_text)
    config = tmp_path / "config.yaml"
    config.write_text("registry: registry.yaml\n" + config_text)
    monkeypatch.setenv("OPEN_FREE_ROUTER_CONFIG", str(config))


def _query(capsys, model):
    cmd_route_query(SimpleNamespace(model=model, json=True))
    return json.loads(capsys.readouterr().out)


_BASIC_REGISTRY = (
    "provider:\n"
    "  prefix: p\n"
    "  models:\n"
    "    - id: plain\n"
    "    - id: coder\n"
    "      tool_calling: true\n"
)


def test_route_query_explicit_model_resolves(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, _BASIC_REGISTRY)
    result = _query(capsys, "p/coder")
    assert result["from"] is None
    assert result["requested"] == "p/coder"
    assert result["resolved_to"] == "p/coder"
    assert result["provider"] == "provider"
    assert result["upstream_model"] == "coder"
    assert result["strategy"] == "explicit"
    assert "at" in result


def test_route_query_unknown_model_reports_not_found(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, _BASIC_REGISTRY)
    result = _query(capsys, "nope/model")
    assert result["resolved_to"] is None
    assert "MODEL_NOT_FOUND" in result["reason"]


def test_route_query_virtual_alias_uses_planner(tmp_path, monkeypatch, capsys):
    _setup(
        tmp_path, monkeypatch, _BASIC_REGISTRY,
        "routing:\n  aliases:\n    preferred:\n      candidates: [p/plain]\n",
    )
    result = _query(capsys, "preferred")
    assert result["resolved_to"] == "p/plain"
    assert result["strategy"] == "priority"
    assert result["candidates"] == ["p/plain"]


def test_route_query_virtual_without_candidates(tmp_path, monkeypatch, capsys):
    _setup(
        tmp_path, monkeypatch, _BASIC_REGISTRY,
        "routing:\n  aliases:\n    broken:\n      candidates: [missing]\n",
    )
    result = _query(capsys, "broken")
    assert result["resolved_to"] is None
    assert "没有可用候选" in result["reason"]


def test_route_query_empty_picks_highest_scored_available(
    tmp_path, monkeypatch, capsys
):
    # "fast/m2" is listed second but carries verified free-tier evidence, so
    # the explainable score must beat the registry-order favourite "slow/m1".
    # A registry-order-only implementation would resolve to "slow/m1".
    _setup(
        tmp_path, monkeypatch,
        "slow:\n"
        "  prefix: slow\n"
        "  models:\n"
        "    - id: m1\n"
        "fast:\n"
        "  prefix: fast\n"
        "  free_tier:\n"
        "    type: recurring_quota\n"
        "    limit: 200\n"
        "    evidence_url: https://example.com/free-tier\n"
        "    verified_at: \"2026-01-01T00:00:00+00:00\"\n"
        "    expires_at: \"2027-01-01T00:00:00+00:00\"\n"
        "  models:\n"
        "    - id: m2\n",
    )
    result = _query(capsys, None)
    assert result["requested"] is None
    assert result["resolved_to"] == "fast/m2"
    assert result["strategy"] == "best-scored"
    assert result["candidates"][0] == "fast/m2"
    assert result["score"]["factors"]["free_evidence"]["value"] == 1.0


def test_route_query_human_output_shows_resolution(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, _BASIC_REGISTRY)
    cmd_route_query(SimpleNamespace(model="p/plain", json=False))
    out = capsys.readouterr().out
    assert "resolved  : p/plain" in out
    assert "reason    :" in out
