"""Configuration loader for open-free-router."""
import os
from pathlib import Path
from typing import Optional

import yaml


DEFAULT_CONFIG_PATHS = [
    Path.home() / ".config" / "open-free-router" / "config.yaml",
    Path.cwd() / "config.yaml",
]

# PI Telegram expects certain provider names in models.json.
# Map registry provider names → PI-compatible names.
_PI_PROVIDER_NAMES: dict[str, str] = {
    "openrouter": "local-free",
    "groq": "local-groq",
}


class Config:
    """Central config: registry path, proxy host/port, UI host/port."""

    def __init__(self, config_path: Optional[Path] = None):
        self._raw = {}
        if config_path:
            self.path = config_path
        else:
            self.path = self._find_config()
        if self.path and self.path.exists():
            with open(self.path) as f:
                self._raw = yaml.safe_load(f) or {}

        # registry.yaml (single source of truth for providers + models)
        self.registry_path = Path(self._raw.get("registry", "registry.yaml"))
        if not self.registry_path.is_absolute():
            base = self.path.parent if self.path else Path.home() / ".config" / "open-free-router"
            self.registry_path = base / self.registry_path

        # proxy
        self.proxy_host = self._raw.get("proxy", {}).get("host", "127.0.0.1")
        self.proxy_port = int(self._raw.get("proxy", {}).get("port", 8337))

        # scheduler
        self.refresh_interval_hours = int(self._raw.get("refresh_interval_hours", 12))

        # Candidate discovery is review-only and never mutates registry.yaml.
        discovery = self._raw.get("discovery", {})
        self.discovery_enabled = bool(discovery.get("enabled", True))
        self.discovery_interval_hours = int(discovery.get("interval_hours", 24))
        self.discovery_auto_test = bool(discovery.get("auto_test", False))
        self.discovery_auto_adopt = bool(discovery.get("auto_adopt", False))
        self.discovery_max_providers = int(discovery.get("max_providers_per_cycle", 5))
        self.discovery_max_models = int(discovery.get("max_models_per_provider", 3))

        # Agents that `serve` must not auto-sync. Listed clients keep whatever the
        # user configured by hand; `sync --agent <name>` still writes them on demand.
        # Empty by default, so the daemon keeps syncing everything it detects.
        self.sync_exclude = list(self._raw.get("sync", {}).get("exclude", []) or [])

        # proxy upstream timeout
        self.upstream_timeout = int(self._raw.get("upstream_timeout", 120))

        # Virtual-model routing and opt-in explainable scoring. The planner owns validation so
        # config loading stays backwards compatible with unknown future keys.
        from open_free_router.routing import RoutingConfig
        self.routing = RoutingConfig.from_dict(self._raw.get("routing", {}))

        analytics = self._raw.get("analytics", {})
        if not isinstance(analytics, dict):
            analytics = {}
        try:
            self.analytics_retention_days = min(365, max(0, int(analytics.get("retention_days", 30))))
        except (TypeError, ValueError):
            self.analytics_retention_days = 30

        mcp = self._raw.get("mcp", {})
        if not isinstance(mcp, dict):
            mcp = {}
        self.mcp_allow_write_tools = mcp.get("allow_write_tools") is True

        # ui
        self.ui_host = self._raw.get("ui", {}).get("host", "127.0.0.1")
        self.ui_port = int(self._raw.get("ui", {}).get("port", 9057))

        # Codex profile defaults. An explicit CLI --codex-model overrides this.
        self.codex_model = self._raw.get("codex", {}).get("model", "")

        # Claude Code defaults. An explicit CLI --claude-model overrides this.
        self.claude_model = self._raw.get("claude", {}).get("model", "")

    @property
    def config_dir(self) -> Path:
        return self.path.parent if self.path else Path.home() / ".config" / "open-free-router"

    @staticmethod
    def _find_config() -> Optional[Path]:
        # Explicit override first: cwd-launched commands (mcp under an MCP
        # host, status/doctor in scripts) must be able to pin the same
        # instance `serve` uses regardless of their working directory.
        override = os.environ.get("OPEN_FREE_ROUTER_CONFIG", "")
        if override:
            return Path(override).expanduser()
        for p in DEFAULT_CONFIG_PATHS:
            if p.exists():
                return p
        return None

    @property
    def data_dir(self) -> Path:
        """Directory for logs, backups, etc."""
        d = Path(self._raw.get("data_dir", Path.home() / ".local" / "share" / "open-free-router"))
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def discovery_path(self) -> Path:
        return self.data_dir / "discovery.json"

    @property
    def analytics_path(self) -> Path:
        return self.data_dir / "usage.db"


def load_registry(registry_path: Path):
    """Load registry.yaml and return dict."""
    import yaml
    with open(registry_path) as f:
        return yaml.safe_load(f) or {}


def save_registry(registry_path: Path, data: dict):
    """Save registry.yaml with backup.

    Note: `Registry.save()` in registry.py is the path actually used by
    serve/ui/cli — this free function is kept only for API compatibility
    with any external caller that imports it directly. It shares the same
    backup-retention behavior so it doesn't reintroduce unbounded .bak
    accumulation if something does start using it.
    """
    import shutil, datetime
    from open_free_router.registry import prune_backups
    if registry_path.exists():
        backup = registry_path.with_suffix(
            f".yaml.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}"
        )
        shutil.copy2(registry_path, backup)
        try:
            backup.chmod(0o600)
        except OSError:
            pass
        prune_backups(registry_path)
    with open(registry_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    try:
        registry_path.chmod(0o600)
    except OSError:
        pass
