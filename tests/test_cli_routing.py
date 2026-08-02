import json
from types import SimpleNamespace

from open_free_router.cli import cmd_route_explain


def test_route_explain_json_uses_configured_candidates(tmp_path, monkeypatch, capsys):
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "provider:\n"
        "  prefix: p\n"
        "  models:\n"
        "    - id: plain\n"
        "    - id: coder\n"
        "      tool_calling: true\n"
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "registry: registry.yaml\n"
        "routing:\n"
        "  aliases:\n"
        "    preferred:\n"
        "      candidates: [p/coder, p/plain]\n"
        "      require:\n"
        "        tool_calling: true\n"
    )
    monkeypatch.setenv("OPEN_FREE_ROUTER_CONFIG", str(config))

    cmd_route_explain(SimpleNamespace(model="preferred", json=True))

    result = json.loads(capsys.readouterr().out)
    assert result["selected"] == "p/coder"
    assert result["candidates"] == ["p/coder"]
    assert result["rejected"] == [{"model": "p/plain", "reason": "tool_calling_required"}]
