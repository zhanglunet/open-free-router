"""Sync registry models to agent config files (Pi, OMP, OpenCode, Hermes).

Usage:
    open-free-router sync                  # sync all agents
    open-free-router sync --agent omp      # sync only OMP
    open-free-router sync --agent opencode # sync only OpenCode
    open-free-router sync --agent hermes    # sync only Hermes
    open-free-router sync --diff           # show diff, don't write
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

from open_free_router.registry import Registry, codex_model_alias

# ── Paths ──
OMP_MODELS = Path.home() / ".omp" / "agent" / "models.yml"
OMP_CONFIG = Path.home() / ".omp" / "agent" / "config.yml"
OPENCODE_CONFIG = Path.home() / ".config" / "opencode" / "opencode.jsonc"
HERMES_CONFIG = Path.home() / ".hermes" / "config.yaml"
PI_MODELS_PATH = Path.home() / ".pi" / "agent" / "models.json"
CODEX_PROFILE = Path.home() / ".codex" / "open-free-router.config.toml"
CODEX_MODEL_CATALOG = Path.home() / ".codex" / "open-free-router.models.json"
BACKUP_DIR = Path.home() / ".openclaw" / "agent-backup" / date.today().isoformat()

CODEX_BASE_INSTRUCTIONS = (
    "You are Codex, a coding agent working in the user's current workspace. "
    "Follow the user's instructions and repository guidance, use the available "
    "tools when useful, preserve unrelated changes, and verify completed work."
)


def write_pi_models(
    reg: Registry,
    do_write: bool = True,
    proxy_url: str = "http://127.0.0.1:8337/v1",
    proxy_token: str = "",
) -> list[str]:
    """Write registry models to Pi's models.json if Pi config dir exists.

    Pi expects {providers: {name: {baseUrl, models: [...]}}}
    All providers point to the local single-port proxy; routing is by model ID.
    Model IDs are prefixed with provider name (e.g. nv/glm-5.2)
    so users can distinguish which upstream provides the model.

    Always overwrites the entire file — no stale entries can accumulate.
    Returns list of provider names written.
    """
    if not PI_MODELS_PATH.parent.exists():
        return []
    try:
        from open_free_router.config import _PI_PROVIDER_NAMES

        providers = {}
        for name, p in reg.providers.items():
            pi_name = _PI_PROVIDER_NAMES.get(name, name)
            providers[pi_name] = {
                "baseUrl": proxy_url,
                "models": [
                    {
                        "id": f"{p.model_prefix}/{m.id}",
                        "name": m.name or m.id,
                        "contextWindow": m.context_window,
                        "maxTokens": m.max_tokens,
                        "reasoning": m.reasoning,
                    }
                    for m in p.models
                ],
            }
        if do_write:
            data = {"providers": providers}
            PI_MODELS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        total = sum(len(p["models"]) for p in providers.values())
        print(f"  ✓ wrote {total} models to Pi ({PI_MODELS_PATH})")
        return list(providers.keys())
    except Exception as e:
        print(f"  ⚠ failed to write Pi models: {e}")
        return []


def _backup():
    """Backup agent config files before overwriting."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for f in [
        OMP_MODELS,
        OMP_CONFIG,
        OPENCODE_CONFIG,
        HERMES_CONFIG,
        CODEX_PROFILE,
        CODEX_MODEL_CATALOG,
    ]:
        if f.exists():
            shutil.copy2(str(f), str(BACKUP_DIR / f.name))


def _local_proxy_key(proxy_token: str) -> str:
    """Credential downstream agents send to the local proxy."""
    return proxy_token or "sk-no-key"


# ══════════════════════════════════════
# OMP sync
# ══════════════════════════════════════
def sync_omp(
    reg: Registry,
    do_write: bool = True,
    proxy_url: str = "http://127.0.0.1:8337/v1",
    proxy_token: str = "",
) -> list[str]:
    """Sync registry → OMP models.yml.

    First removes ALL providers that point to the local proxy, then writes
    fresh entries from the registry. Prevents stale duplicates.

    Uses ruamel.yaml's round-trip mode (not regex text surgery) so any
    provider blocks the user configured by hand — comments, formatting,
    unrelated entries — survive untouched. A previous regex-based
    implementation matched "2-space indent, colon at end" to find provider
    blocks; that breaks on anything the regex didn't anticipate (tabs,
    multi-line values, differently-indented comments), silently mangling
    or losing hand-edited config on the same pass that's supposed to only
    touch open-free-router's own entries.
    """
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)

    if OMP_MODELS.exists():
        with open(OMP_MODELS) as f:
            doc = None
            try:
                doc = yaml.load(f)
            except Exception as e:
                # The existing file is malformed YAML (e.g. an unquoted
                # model name containing a colon from an older regex-based
                # sync). Re-parse is impossible, so start from a clean
                # document rather than aborting the whole sync — the fresh
                # write below replaces the broken file. Hand-configured
                # entries in an unparseable file are already unrecoverable;
                # _backup() already has a copy.
                print(f"  ⚠ {OMP_MODELS} unparseable ({e}); rewriting from scratch")
                doc = None
    else:
        doc = None
    if not isinstance(doc, CommentedMap):
        doc = CommentedMap()
    if not isinstance(doc.get("providers"), CommentedMap):
        doc["providers"] = CommentedMap()
    providers = doc["providers"]

    local_proxy_markers = ("127.0.0.1", "localhost")

    def _points_to_local_proxy(block) -> bool:
        if not isinstance(block, dict):
            return False
        return any(marker in str(block.get("baseUrl", "")) for marker in local_proxy_markers)

    # Remove ALL provider blocks pointing to the local proxy (including
    # ones for providers no longer in the registry) so stale entries can't
    # accumulate; unrelated hand-configured providers are left alone.
    removed = [pname for pname in list(providers.keys()) if _points_to_local_proxy(providers[pname])]
    for pname in removed:
        del providers[pname]

    changes = []
    for name, p in reg.providers.items():
        key = _local_proxy_key(proxy_token)
        block = CommentedMap()
        block["baseUrl"] = proxy_url
        block["apiKey"] = key
        block["api"] = "openai-completions"
        model_blocks = CommentedSeq()
        for m in p.models:
            mblock = CommentedMap()
            mblock["id"] = m.id
            mblock["name"] = m.name or m.id
            mblock["reasoning"] = bool(m.reasoning)
            mblock["input"] = ["text"]
            mblock["contextWindow"] = m.context_window
            mblock["maxTokens"] = m.max_tokens
            mblock["cost"] = CommentedMap(
                [("input", 0), ("output", 0), ("cacheRead", 0), ("cacheWrite", 0)]
            )
            model_blocks.append(mblock)
        block["models"] = model_blocks
        providers[name] = block
        changes.append(name)

    if removed:
        print(f"  Removed {len(removed)} stale OMP providers: {', '.join(removed)}")

    if do_write:
        OMP_MODELS.parent.mkdir(parents=True, exist_ok=True)
        with open(OMP_MODELS, "w") as f:
            yaml.dump(doc, f)
    return changes


# ══════════════════════════════════════
# OpenCode sync
# ══════════════════════════════════════
def sync_opencode(
    reg: Registry,
    do_write: bool = True,
    proxy_url: str = "http://127.0.0.1:8337/v1",
    proxy_token: str = "",
) -> list[str]:
    """Sync registry → OpenCode opencode.json.

    First removes ALL providers that point to the local proxy (baseURL
    contains 127.0.0.1 or localhost), then writes fresh entries from the
    registry. This prevents duplicate local-* / provider-* entries from
    accumulating across syncs.
    """
    if OPENCODE_CONFIG.exists():
        raw_text = OPENCODE_CONFIG.read_text()
        data = None
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            text = re.sub(r'//[^\n]*', '', raw_text)
            text = re.sub(r',\s*([}\])]', r'\1', text, re.DOTALL)
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                decoder = json.JSONDecoder()
                try:
                    data, _ = decoder.raw_decode(raw_text.lstrip())
                except json.JSONDecodeError:
                    data = {"provider": {}}
    else:
        data = {"provider": {}}

    if "provider" not in data:
        data["provider"] = {}

    # Remove all providers pointing to local proxy (stale duplicates)
    local_proxy_markers = ("127.0.0.1", "localhost")
    removed = []
    for pname in list(data["provider"]):
        opts = data["provider"][pname].get("options", {})
        base_url = opts.get("baseURL", "")
        if any(marker in base_url for marker in local_proxy_markers):
            del data["provider"][pname]
            removed.append(pname)

    # Write fresh entries from registry
    changes = []
    for name, p in reg.providers.items():
        key = _local_proxy_key(proxy_token)
        models_map = {}
        for m in p.models:
            mkey = m.id.split("/")[-1].replace(":free", "")
            models_map[mkey] = {
                "name": m.name or m.id,
                "id": m.id,
                "reasoning": m.reasoning,
                "limit": {
                    "context": m.context_window,
                    "output": m.max_tokens,
                },
            }
        data["provider"][name] = {
            "name": name.replace("-", " ").title(),
            "npm": "@ai-sdk/openai-compatible",
            "models": models_map,
            "options": {"baseURL": proxy_url, "apiKey": key},
        }
        changes.append(name)

    if removed:
        print(f"  Removed {len(removed)} stale local-proxy providers: {', '.join(removed)}")

    if do_write:
        OPENCODE_CONFIG.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return changes


# ══════════════════════════════════════
# Hermes sync
# ══════════════════════════════════════
def sync_hermes(
    reg: Registry,
    do_write: bool = True,
    proxy_url: str = "http://127.0.0.1:8337/v1",
    proxy_token: str = "",
) -> list[str]:
    """Ensure Hermes has a custom_providers entry pointing to the proxy.

    Hermes auto-discovers models from the /v1/models endpoint at runtime,
    so we don't need to write a static model list. We only need to ensure
    a custom_providers entry exists with the correct base_url.
    """
    if not HERMES_CONFIG.exists():
        return []

    import yaml

    try:
        text = HERMES_CONFIG.read_text()
        config = yaml.safe_load(text) or {}
    except Exception:
        return []

    if not isinstance(config, dict):
        return []

    custom_providers = config.get("custom_providers", [])
    if not isinstance(custom_providers, list):
        custom_providers = []

    changes = []
    # Check if we already have an entry pointing to our proxy
    has_entry = any(
        cp.get("base_url", "").rstrip("/") == proxy_url.rstrip("/")
        for cp in custom_providers
        if isinstance(cp, dict)
    )

    for cp in custom_providers:
        if not isinstance(cp, dict):
            continue
        if cp.get("base_url", "").rstrip("/") != proxy_url.rstrip("/"):
            continue
        local_key = _local_proxy_key(proxy_token)
        if cp.get("api_key") != local_key:
            cp["api_key"] = local_key
            changes.append(cp.get("name", "open-free-router"))

    if not has_entry:
        new_entry = {
            "name": "open-free-router",
            "api_mode": "chat_completions",
            "base_url": proxy_url,
            "api_key": _local_proxy_key(proxy_token),
            "model": "glm-5.2",  # default model
            "context_length": 262144,
            "max_tokens": 16384,
            "discover_models": True,  # Hermes will auto-discover from /v1/models
        }
        custom_providers.append(new_entry)
        config["custom_providers"] = custom_providers
        changes.append("open-free-router")

    if do_write and changes:
        try:
            # Write back preserving YAML format
            new_text = yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False)
            HERMES_CONFIG.write_text(new_text)
        except Exception:
            pass

    return changes


# ══════════════════════════════════════
# Codex profile sync
# ══════════════════════════════════════
def _codex_model_id(reg: Registry, requested: str = "") -> str:
    for p in reg.providers.values():
        for m in p.models:
            forms = {
                m.id,
                f"{p.model_prefix}/{m.id}",
                m.effective_upstream_id,
                f"{p.name}/{m.effective_upstream_id}",
                codex_model_alias(p.model_prefix, m.id),
            }
            if requested and requested in forms:
                return codex_model_alias(p.model_prefix, m.id)
    if requested:
        raise ValueError(f"Codex model '{requested}' is not present in the registry")
    for p in reg.providers.values():
        for m in p.models:
            if m.tool_calling:
                return codex_model_alias(p.model_prefix, m.id)
    raise ValueError(
        "No Codex-compatible model found. Mark a registry model with "
        "tool_calling: true or pass --codex-model."
    )


def sync_codex(
    reg: Registry,
    do_write: bool = True,
    proxy_url: str = "http://127.0.0.1:8337/v1",
    proxy_token: str = "",
    codex_model: str = "",
) -> list[str]:
    """Write an isolated Codex profile and model catalog for the proxy.

    Codex does not discover custom-provider models from ``/v1/models``. It
    needs ``model_catalog_json`` both to show them in ``/model`` and to avoid
    falling back to guessed capability metadata.
    """
    model = _codex_model_id(reg, codex_model)
    catalog_models = []
    priority = 0
    for provider in reg.providers.values():
        for registry_model in provider.models:
            slug = codex_model_alias(provider.model_prefix, registry_model.id)
            tools_verified = registry_model.tool_calling or slug == model
            catalog_models.append({
                "slug": slug,
                "display_name": registry_model.name or registry_model.id,
                "description": (
                    f"open-free-router via {provider.name}; "
                    + ("tool calling enabled" if tools_verified else "tool calling not verified")
                ),
                "default_reasoning_level": None,
                "supported_reasoning_levels": [],
                "shell_type": "shell_command" if tools_verified else "disabled",
                "visibility": "list",
                "supported_in_api": True,
                "priority": priority,
                "additional_speed_tiers": [],
                "service_tiers": [],
                "availability_nux": None,
                "upgrade": None,
                "base_instructions": CODEX_BASE_INSTRUCTIONS,
                "model_messages": None,
                "include_skills_usage_instructions": False,
                "default_reasoning_summary": "none",
                "support_verbosity": False,
                "default_verbosity": None,
                "apply_patch_tool_type": "freeform" if tools_verified else None,
                "web_search_tool_type": "text",
                "truncation_policy": {
                    "mode": "tokens",
                    "limit": min(10_000, registry_model.max_tokens),
                },
                "supports_parallel_tool_calls": tools_verified,
                "supports_image_detail_original": False,
                "context_window": registry_model.context_window,
                "max_context_window": registry_model.context_window,
                "effective_context_window_percent": 95,
                "experimental_supported_tools": [],
                "input_modalities": ["text"],
                "supports_search_tool": False,
                "use_responses_lite": False,
                "tool_mode": "direct" if tools_verified else None,
            })
            priority += 1

    slugs = [item["slug"] for item in catalog_models]
    if len(slugs) != len(set(slugs)):
        raise ValueError("Codex model aliases collide; use distinct provider prefixes/model IDs")
    catalog = {"models": catalog_models}
    profile = f'''# Managed by open-free-router. Run with: codex --profile open-free-router
model = {json.dumps(model)}
model_provider = "open_free_router"
model_catalog_json = {json.dumps(str(CODEX_MODEL_CATALOG))}
include_apps_instructions = false

# This isolated third-party profile does not depend on ChatGPT Apps or the
# OpenAI Docs MCP. Disable them here so a connector handshake failure cannot
# interrupt local-provider startup; the user's normal Codex profile is intact.
[features]
apps = false
plugins = false
memories = false

[mcp_servers.openaiDeveloperDocs]
url = "https://developers.openai.com/mcp"
enabled = false

# The user's global skill/plugin inventory is large enough to exceed Codex's
# fixed 2% skill-description budget. Keep this router-specific profile lean;
# the normal Codex profile still exposes every installed skill.
[skills]
include_instructions = false

[model_providers.open_free_router]
name = "open-free-router"
base_url = {json.dumps(proxy_url)}
wire_api = "responses"
request_max_retries = 2
stream_max_retries = 1

[model_providers.open_free_router.auth]
command = {json.dumps(sys.executable)}
args = ["-m", "open_free_router.cli", "token"]
timeout_ms = 5000
refresh_interval_ms = 300000
'''
    if do_write:
        CODEX_PROFILE.parent.mkdir(parents=True, exist_ok=True)
        CODEX_MODEL_CATALOG.write_text(
            json.dumps(catalog, indent=2, ensure_ascii=False) + "\n"
        )
        CODEX_PROFILE.write_text(profile)
        try:
            CODEX_PROFILE.chmod(0o600)
            CODEX_MODEL_CATALOG.chmod(0o600)
        except OSError:
            pass
        print(
            f"  ✓ wrote Codex profile and {len(catalog_models)} model metadata entries "
            f"({CODEX_PROFILE})"
        )
    return [model]


# ══════════════════════════════════════
# Main
# ══════════════════════════════════════
def sync_all(
    reg: Registry,
    do_write: bool = True,
    agents: list[str] | None = None,
    proxy_url: str = "http://127.0.0.1:8337/v1",
    proxy_token: str = "",
    codex_model: str = "",
) -> dict[str, list[str]]:
    """Sync registry to all agents. Returns {agent: [changed_providers]}."""
    if agents is None:
        agents = ["pi", "omp", "opencode", "hermes"]

    if do_write:
        _backup()

    results = {}
    sync_map = {
        "pi": write_pi_models,
        "omp": sync_omp,
        "opencode": sync_opencode,
        "hermes": sync_hermes,
        "codex": sync_codex,
    }

    for agent in agents:
        fn = sync_map.get(agent)
        if fn:
            try:
                kwargs = {
                    "do_write": do_write,
                    "proxy_url": proxy_url,
                    "proxy_token": proxy_token,
                }
                if agent == "codex":
                    kwargs["codex_model"] = codex_model
                changes = fn(reg, **kwargs)
                results[agent] = changes
            except Exception as e:
                results[agent] = [f"ERROR: {e}"]

    return results
