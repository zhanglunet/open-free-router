"""Tests for Config class."""
from pathlib import Path
from open_free_router.config import Config


def test_config_defaults(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("")
    cfg = Config(config_path=cfg_path)
    assert cfg.proxy_host == "127.0.0.1"
    assert cfg.proxy_port == 8337
    assert cfg.ui_host == "127.0.0.1"
    assert cfg.ui_port == 9057
    assert cfg.refresh_interval_hours == 12
    assert cfg.discovery_enabled is True
    assert cfg.discovery_interval_hours == 24
    assert cfg.discovery_auto_test is False
    assert cfg.discovery_auto_adopt is False
    assert cfg.discovery_max_providers == 5
    assert cfg.discovery_max_models == 3
    assert cfg.routing.max_attempts == 3
    assert cfg.routing.explicit_model_fallback is False


def test_config_custom_values(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "proxy:\n  host: 0.0.0.0\n  port: 9000\n"
        "ui:\n  host: 0.0.0.0\n  port: 9001\n"
        "refresh_interval_hours: 6\n"
        "discovery:\n  enabled: false\n  interval_hours: 8\n"
        "  auto_test: true\n  auto_adopt: true\n"
        "  max_providers_per_cycle: 2\n  max_models_per_provider: 1\n"
        "routing:\n  fallback:\n    max_attempts: 5\n    explicit_model: true\n"
    )
    cfg = Config(config_path=cfg_path)
    assert cfg.proxy_host == "0.0.0.0"
    assert cfg.proxy_port == 9000
    assert cfg.ui_host == "0.0.0.0"
    assert cfg.ui_port == 9001
    assert cfg.refresh_interval_hours == 6
    assert cfg.discovery_enabled is False
    assert cfg.discovery_interval_hours == 8
    assert cfg.discovery_auto_test is True
    assert cfg.discovery_auto_adopt is True
    assert cfg.discovery_max_providers == 2
    assert cfg.discovery_max_models == 1
    assert cfg.routing.max_attempts == 5
    assert cfg.routing.explicit_model_fallback is True


def test_config_registry_relative_path(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("registry: registry.yaml\n")
    cfg = Config(config_path=cfg_path)
    assert cfg.registry_path == tmp_path / "registry.yaml"


def test_config_registry_absolute_path(tmp_path):
    reg_path = tmp_path / "custom" / "my-registry.yaml"
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"registry: {reg_path}\n")
    cfg = Config(config_path=cfg_path)
    assert cfg.registry_path == reg_path
