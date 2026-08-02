import json
from types import SimpleNamespace

from open_free_router.cli import cmd_resilience, cmd_resilience_reset, cmd_route_explain
from open_free_router.proxy import run_proxy
from open_free_router.registry import Registry
from open_free_router.resilience import ResilienceManager, classify_failure


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


def test_resilience_cli_reads_and_resets_running_proxy(tmp_path, monkeypatch, capsys):
    token = "local-cli-placeholder"
    manager = ResilienceManager(model_cooldown=60)
    manager.record_failure("provider", "model", classify_failure(404))
    server, _ = run_proxy(
        Registry({}), host="127.0.0.1", port=0,
        auth_token=token, resilience=manager,
    )
    try:
        config = tmp_path / "config.yaml"
        config.write_text(
            "registry: registry.yaml\n"
            f"proxy:\n  host: 127.0.0.1\n  port: {server.server_address[1]}\n"
        )
        (tmp_path / "proxy.token").write_text(token + "\n")
        monkeypatch.setenv("OPEN_FREE_ROUTER_CONFIG", str(config))

        capsys.readouterr()  # discard run_proxy startup banner
        cmd_resilience(SimpleNamespace(json=True))
        payload = json.loads(capsys.readouterr().out)
        assert "provider/model" in payload["models"]

        cmd_resilience_reset(SimpleNamespace(provider="provider", model="model"))
        assert "reset provider/model" in capsys.readouterr().out
        assert manager.snapshot()["models"] == {}
    finally:
        server.shutdown()


def test_route_explain_json_includes_score_factors(tmp_path, monkeypatch, capsys):
    (tmp_path / "registry.yaml").write_text(
        "provider:\n  prefix: p\n  models:\n    - id: model\n      tool_calling: true\n"
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "registry: registry.yaml\nrouting:\n  scoring:\n    enabled: true\n"
    )
    monkeypatch.setenv("OPEN_FREE_ROUTER_CONFIG", str(config))
    cmd_route_explain(SimpleNamespace(model="auto", json=True))
    result = json.loads(capsys.readouterr().out)
    assert result["strategy"] == "scored"
    assert result["scoring"]["enabled"] is True
    assert "latency" in result["scoring"]["candidates"][0]["factors"]
