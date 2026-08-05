"""Sync registry models to agent config files.

Supported agents: Pi, OMP, OpenCode, Hermes, Codex, Claude Code, Kimi CLI,
OpenClaw, WorkBuddy.

Usage:
    open-free-router sync                  # sync all detected agents
    open-free-router sync --agent omp      # sync only OMP
    open-free-router sync --agent claude   # sync only Claude Code
    open-free-router sync --agent kimi,openclaw,workbuddy
    open-free-router sync --diff           # show diff, don't write

Detection rule: an agent is synced automatically only when its config
directory (or file, for OpenClaw) already exists. Passing --agent NAME
explicitly creates the config even on a machine where the client hasn't
run yet.
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
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
KIMI_CONFIG = Path.home() / ".kimi-code" / "config.toml"
KIMI_LEGACY_CONFIG = Path.home() / ".kimi" / "config.toml"
OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
WORKBUDDY_MODELS = Path.home() / ".workbuddy" / "models.json"
BACKUP_DIR = Path.home() / ".openclaw" / "agent-backup" / date.today().isoformat()

# Markers delimiting the router-managed section of Kimi CLI's config.toml.
# TOML has no safe way to rewrite arbitrary tables without a round-trip
# parser dependency, so our providers/models live in one marked block that
# is replaced wholesale on each sync; user content outside it is untouched.
KIMI_BLOCK_BEGIN = "# >>> open-free-router managed >>>"
KIMI_BLOCK_END = "# <<< open-free-router managed <<<"
KIMI_SOURCE_LABELS = {
    "deepseek": "DeepSeek 开放平台",
    "google-ai-studio": "Google AI Studio",
    "groq": "GroqCloud",
    "nvidia-nim": "NVIDIA NIM",
    "nous": "Nous Research",
    "poolside": "Poolside AI",
    "opencode-zen-free": "OpenCode Zen",
    "openrouter": "OpenRouter",
    "sensenova": "SenseNova",
    "stepfun": "StepFun",
    "gitee-ai": "Gitee AI",
}
KIMI_HEAVY_PROMPT_UNSAFE_PROVIDERS = {
    # Kimi Code's bootstrap prompt and tool inventory can exceed Groq's
    # free-tier TPM even for a short user prompt, so these are kept out of
    # `--kimi-available-only` until the account tier is upgraded.
    "groq",
}
KIMI_DEFAULT_DISPLAY_IDS = (
    "nova/glm-5.2",
    "nova/deepseek-v4-flash",
    "nv/nvidia/nemotron-3-ultra-550b-a55b",
    "zen/deepseek-v4-flash-free",
)

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
        CLAUDE_SETTINGS,
        KIMI_CONFIG,
        OPENCLAW_CONFIG,
        WORKBUDDY_MODELS,
    ]:
        if f.exists():
            shutil.copy2(str(f), str(BACKUP_DIR / f.name))


def _strip_json_comments(raw_text: str) -> str:
    """Remove ``// line comments`` outside of strings.

    A plain regex would also eat the ``//`` inside every ``http://`` URL
    value, silently corrupting configs, so this walks the text tracking
    string state.
    """
    out: list[str] = []
    in_string = False
    escape = False
    i = 0
    n = len(raw_text)
    while i < n:
        c = raw_text[i]
        if in_string:
            out.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
        elif c == '"':
            in_string = True
            out.append(c)
            i += 1
        elif c == "/" and i + 1 < n and raw_text[i + 1] == "/":
            while i < n and raw_text[i] != "\n":
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _parse_json5(raw_text: str):
    """Best-effort parse of a JSON5-ish config (comments, trailing commas).

    Returns the parsed object, or None when nothing parseable remains.
    """
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass
    text = _strip_json_comments(raw_text)
    text = re.sub(r',\s*([}\]])', r'\1', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        try:
            data, _ = decoder.raw_decode(raw_text.lstrip())
            return data
        except json.JSONDecodeError:
            return None


def _restrict_permissions(path: Path) -> None:
    """These configs carry the local proxy token; keep them user-only."""
    try:
        path.chmod(0o600)
    except OSError:
        pass  # best-effort; not all platforms support POSIX perms


def _default_tool_model(reg: Registry, requested: str = "") -> str:
    """Pick a registry model for agents that need one default model ID.

    Returns the ``prefix/id`` form the proxy resolves. An explicit request
    is validated against every accepted ID form.
    """
    for p in reg.providers.values():
        for m in p.models:
            forms = {
                m.id,
                f"{p.model_prefix}/{m.id}",
                m.effective_upstream_id,
                f"{p.name}/{m.effective_upstream_id}",
            }
            if requested and requested in forms:
                return f"{p.model_prefix}/{m.id}"
    if requested:
        raise ValueError(f"Model '{requested}' is not present in the registry")
    for p in reg.providers.values():
        for m in p.models:
            if m.tool_calling:
                return f"{p.model_prefix}/{m.id}"
    for p in reg.providers.values():
        for m in p.models:
            return f"{p.model_prefix}/{m.id}"
    raise ValueError("Registry has no models to sync")


def _small_fast_model(reg: Registry, fallback: str) -> str:
    """Prefer an obviously small/fast model for background-task slots.

    Hints are matched against delimiter-split tokens, not raw substrings —
    otherwise "mini" matches "ge*mini*-2.5-pro" and a large model wins the
    small/fast slot.
    """
    hints = {"haiku", "mini", "small", "flash", "lite", "tiny", "8b", "9b"}
    for p in reg.providers.values():
        for m in p.models:
            tokens = set(re.split(r"[^a-z0-9]+", f"{m.id} {m.name}".lower()))
            if tokens & hints:
                return f"{p.model_prefix}/{m.id}"
    return fallback


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
        data = _parse_json5(OPENCODE_CONFIG.read_text())
        if data is None:
            # Rewriting from scratch here would wipe the user's hand-written
            # providers; refuse instead and let them fix the file.
            print(f"  ⚠ {OPENCODE_CONFIG} is unparseable; not touching it")
            return []
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
# Claude Code sync
# ══════════════════════════════════════
def sync_claude(
    reg: Registry,
    do_write: bool = True,
    proxy_url: str = "http://127.0.0.1:8337/v1",
    proxy_token: str = "",
    claude_model: str = "",
    explicit: bool = False,
) -> list[str]:
    """Sync registry → Claude Code ``~/.claude/settings.json`` env block.

    Claude Code speaks the Anthropic Messages API; the proxy serves it on
    ``/v1/messages``, so ANTHROPIC_BASE_URL is the proxy origin without the
    ``/v1`` suffix. Model discovery works automatically because Claude Code
    probes ``GET /v1/models`` on startup to populate its ``/model`` picker.

    Only the router-owned env keys are touched; every other setting in the
    file is preserved. An unparseable settings.json aborts the sync rather
    than clobbering the user's configuration.
    """
    if not explicit and not CLAUDE_SETTINGS.parent.exists():
        return []

    settings: dict = {}
    if CLAUDE_SETTINGS.exists():
        try:
            settings = json.loads(CLAUDE_SETTINGS.read_text() or "{}")
        except json.JSONDecodeError as e:
            print(f"  ⚠ {CLAUDE_SETTINGS} is not valid JSON ({e}); not touching it")
            return []
        if not isinstance(settings, dict):
            print(f"  ⚠ {CLAUDE_SETTINGS} is not a JSON object; not touching it")
            return []

    model = _default_tool_model(reg, claude_model)
    small = _small_fast_model(reg, model)
    base_url = proxy_url[:-3] if proxy_url.endswith("/v1") else proxy_url

    env = settings.get("env")
    if not isinstance(env, dict):
        env = {}
    env["ANTHROPIC_BASE_URL"] = base_url
    env["ANTHROPIC_AUTH_TOKEN"] = _local_proxy_key(proxy_token)
    env["ANTHROPIC_MODEL"] = model
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = small
    # Older Claude Code versions read the deprecated name; harmless on new ones.
    env["ANTHROPIC_SMALL_FAST_MODEL"] = small
    settings["env"] = env

    if do_write:
        CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        CLAUDE_SETTINGS.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
        )
        _restrict_permissions(CLAUDE_SETTINGS)
        print(f"  ✓ wrote Claude Code env ({CLAUDE_SETTINGS}); main model {model}")
    return [model]


# ══════════════════════════════════════
# Kimi CLI sync
# ══════════════════════════════════════
def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _strip_unmanaged_kimi_router_tables(text: str) -> str:
    """Remove pre-managed open-free-router Kimi tables before adding ours."""
    if "open-free-router" not in text:
        return text

    table_re = re.compile(r"(?m)^\s*\[[^\n]+]\s*$")
    matches = list(table_re.finditer(text))
    pieces = []
    cursor = 0
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        header = match.group(0).strip()
        body = text[start:end]
        remove = (
            header == "[providers.open-free-router]"
            or (
                header.startswith("[models.")
                and re.search(r'(?m)^\s*provider\s*=\s*"open-free-router"\s*$', body)
            )
        )
        if remove:
            pieces.append(text[cursor:start])
            cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def sync_kimi(
    reg: Registry,
    do_write: bool = True,
    proxy_url: str = "http://127.0.0.1:8337/v1",
    proxy_token: str = "",
    explicit: bool = False,
    include_model_ids: set[str] | None = None,
) -> list[str]:
    """Sync registry → Kimi Code ``~/.kimi-code/config.toml``.

    Writes one ``[providers.open-free-router]`` block plus one ``[models.<alias>]``
    entry per registry model, all inside a marked managed section that is
    replaced wholesale on each sync. ``default_model`` is only set when
    missing or already pointing at a router-managed alias.
    """
    config = KIMI_CONFIG
    if (
        not explicit
        and not config.exists()
        and not config.parent.exists()
        and KIMI_LEGACY_CONFIG.exists()
    ):
        config = KIMI_LEGACY_CONFIG
    if not explicit and not config.parent.exists():
        return []

    text = config.read_text() if config.exists() else ""

    # Drop the previous managed block (if any).
    if KIMI_BLOCK_BEGIN in text:
        pattern = re.compile(
            re.escape(KIMI_BLOCK_BEGIN) + r".*?" + re.escape(KIMI_BLOCK_END) + r"\n?",
            re.DOTALL,
        )
        text = pattern.sub("", text)
        if KIMI_BLOCK_BEGIN in text:
            # Orphan BEGIN without END (e.g. a truncated previous write).
            # The managed block is always written last, so dropping from the
            # marker to EOF removes only router-owned content.
            text = text[: text.index(KIMI_BLOCK_BEGIN)]
    text = _strip_unmanaged_kimi_router_tables(text)

    lines = [KIMI_BLOCK_BEGIN]
    lines.append("[providers.open-free-router]")
    lines.append('type = "openai"')
    lines.append(f"base_url = {_toml_str(proxy_url)}")
    lines.append(f"api_key = {_toml_str(_local_proxy_key(proxy_token))}")
    changes = []
    default_alias = ""
    preferred_default_alias = ""
    for p in reg.providers.values():
        for m in p.models:
            provider_model_id = f"{p.name}/{m.id}"
            display_id = f"{p.model_prefix}/{m.id}"
            if include_model_ids is not None and provider_model_id not in include_model_ids:
                continue
            if include_model_ids is not None and p.name in KIMI_HEAVY_PROMPT_UNSAFE_PROVIDERS:
                continue
            alias = codex_model_alias(p.model_prefix, m.id)
            lines.append("")
            lines.append(f"[models.{alias}]")
            lines.append('provider = "open-free-router"')
            lines.append(f"model = {_toml_str(display_id)}")
            lines.append(f"max_context_size = {m.context_window}")
            lines.append(f"max_output_size = {m.max_tokens}")
            caps = ["tool_use"]
            if m.reasoning:
                caps.insert(0, "thinking")
            lines.append(
                "capabilities = [ "
                + ", ".join(_toml_str(cap) for cap in caps)
                + " ]"
            )
            source = KIMI_SOURCE_LABELS.get(p.name, p.name)
            lines.append(f"display_name = {_toml_str(f'{m.name}（来源：{source}）')}")
            if not preferred_default_alias and display_id in KIMI_DEFAULT_DISPLAY_IDS:
                preferred_default_alias = alias
            if not default_alias and m.tool_calling:
                default_alias = alias
            changes.append(alias)
    if preferred_default_alias:
        default_alias = preferred_default_alias
    if not default_alias and changes:
        default_alias = changes[0]
    if len(changes) != len(set(changes)):
        raise ValueError("Kimi model aliases collide; use distinct provider prefixes/model IDs")
    lines.append(KIMI_BLOCK_END)
    block = "\n".join(lines) + "\n"

    # default_model is a top-level key: it must stay above any [table]
    # header, and only occurrences in that head region are top-level (the
    # same key inside a [table] belongs to the table). Replace an existing
    # router-managed value in place; otherwise only add one when the user
    # hasn't chosen a default themselves. Accept both TOML quote styles.
    first_table = re.search(r"^\s*\[", text, re.MULTILINE)
    head = text[: first_table.start()] if first_table else text
    tail = text[first_table.start():] if first_table else ""
    default_line = f"default_model = {_toml_str(default_alias)}"
    default_re = re.compile(r'^default_model\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', re.MULTILINE)
    existing_default = default_re.search(head)
    if existing_default:
        current = existing_default.group(1) or existing_default.group(2) or ""
        if (current.startswith("ofr-") or current.startswith("open-free-router/")) and default_alias:
            head = default_re.sub(default_line, head, count=1)
    elif default_alias:
        head = default_line + "\n" + head
    text = head + tail

    if text and not text.endswith("\n"):
        text += "\n"
    text += block

    if do_write:
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(text)
        _restrict_permissions(config)
        print(f"  ✓ wrote {len(changes)} Kimi CLI models ({config})")
    return changes


# ══════════════════════════════════════
# OpenClaw sync
# ══════════════════════════════════════
def sync_openclaw(
    reg: Registry,
    do_write: bool = True,
    proxy_url: str = "http://127.0.0.1:8337/v1",
    proxy_token: str = "",
    explicit: bool = False,
) -> list[str]:
    """Sync registry → OpenClaw ``~/.openclaw/openclaw.json``.

    OpenClaw does not auto-discover models from ``/v1/models``, so it gets
    a static model list under ``models.providers.open-free-router``. The
    default model (``agents.defaults.model.primary``) is only set when
    missing or already pointing at a local-proxy provider.

    Detection is based on the config *file* (not the directory) because the
    router's own sync backups live under ``~/.openclaw/agent-backup/``.
    """
    if not explicit and not OPENCLAW_CONFIG.exists():
        return []

    data = None
    if OPENCLAW_CONFIG.exists():
        data = _parse_json5(OPENCLAW_CONFIG.read_text())
        if data is None:
            print(f"  ⚠ {OPENCLAW_CONFIG} is unparseable; not touching it")
            return []
    if not isinstance(data, dict):
        data = {}

    models_section = data.setdefault("models", {})
    if not isinstance(models_section, dict):
        return []
    providers = models_section.setdefault("providers", {})
    if not isinstance(providers, dict):
        return []

    local_proxy_markers = ("127.0.0.1", "localhost")
    current_proxy = proxy_url.rstrip("/")
    removed = [
        pname for pname, block in list(providers.items())
        if isinstance(block, dict)
        and (
            any(m in str(block.get("baseUrl", "")) for m in local_proxy_markers)
            or str(block.get("baseUrl", "")).rstrip("/") == current_proxy
        )
    ]
    for pname in removed:
        del providers[pname]

    model_entries = []
    first_tool_model = ""
    changes = []
    for p in reg.providers.values():
        for m in p.models:
            model_id = f"{p.model_prefix}/{m.id}"
            entry = {
                "id": model_id,
                "name": m.name or m.id,
                "reasoning": bool(m.reasoning),
                "input": ["text"],
                "contextWindow": m.context_window,
                "maxTokens": m.max_tokens,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            }
            model_entries.append(entry)
            changes.append(model_id)
            if not first_tool_model and m.tool_calling:
                first_tool_model = model_id
    providers["open-free-router"] = {
        "baseUrl": proxy_url,
        "apiKey": _local_proxy_key(proxy_token),
        "api": "openai-completions",
        "models": model_entries,
    }

    default_model = first_tool_model or (changes[0] if changes else "")
    if default_model:
        agents = data.setdefault("agents", {})
        if isinstance(agents, dict):
            defaults = agents.setdefault("defaults", {})
            if isinstance(defaults, dict):
                model_cfg = defaults.setdefault("model", {})
                if isinstance(model_cfg, dict):
                    primary = str(model_cfg.get("primary", ""))
                    # A user-chosen router model stays as long as it still
                    # exists; only dangling references get re-pointed.
                    valid_primaries = {f"open-free-router/{model_id}" for model_id in changes}
                    stale = any(
                        primary.startswith(f"{name}/")
                        for name in removed
                        if name != "open-free-router"
                    ) or (
                        primary.startswith("open-free-router/")
                        and primary not in valid_primaries
                    )
                    if not primary or stale:
                        model_cfg["primary"] = f"open-free-router/{default_model}"

    if removed:
        print(f"  Removed {len(removed)} stale OpenClaw providers: {', '.join(removed)}")

    if do_write:
        OPENCLAW_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        OPENCLAW_CONFIG.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        _restrict_permissions(OPENCLAW_CONFIG)
        print(f"  ✓ wrote {len(changes)} OpenClaw models ({OPENCLAW_CONFIG})")
    return changes


# ══════════════════════════════════════
# WorkBuddy sync
# ══════════════════════════════════════
def sync_workbuddy(
    reg: Registry,
    do_write: bool = True,
    proxy_url: str = "http://127.0.0.1:8337/v1",
    proxy_token: str = "",
    explicit: bool = False,
) -> list[str]:
    """Sync registry → WorkBuddy ``~/.workbuddy/models.json``.

    WorkBuddy's custom-model file is a flat JSON array of model entries;
    each points directly at an OpenAI-compatible ``/v1`` base URL. Entries
    whose URL targets the local proxy are replaced on each sync; the user's
    own custom models are preserved. WorkBuddy must be restarted to pick up
    the change.
    """
    if not explicit and not WORKBUDDY_MODELS.parent.exists():
        return []

    entries = []
    if WORKBUDDY_MODELS.exists():
        parsed = _parse_json5(WORKBUDDY_MODELS.read_text())
        if isinstance(parsed, list):
            entries = parsed
        else:
            # None (unparseable) or a non-array object: rewriting would wipe
            # the user's custom models, so refuse to touch the file.
            print(f"  ⚠ {WORKBUDDY_MODELS} is unparseable or not a JSON array; not touching it")
            return []

    local_proxy_markers = ("127.0.0.1", "localhost")
    current_proxy = proxy_url.rstrip("/")
    entries = [
        e for e in entries
        if not (
            isinstance(e, dict)
            and (
                any(m in str(e.get("url", "")) for m in local_proxy_markers)
                or str(e.get("url", "")).rstrip("/") == current_proxy
            )
        )
    ]

    changes = []
    for p in reg.providers.values():
        for m in p.models:
            model_id = f"{p.model_prefix}/{m.id}"
            entries.append({
                "id": model_id,
                "name": m.name or model_id,
                "vendor": "Custom",
                "url": proxy_url,
                "apiKey": _local_proxy_key(proxy_token),
                "maxInputTokens": m.context_window,
                "maxOutputTokens": m.max_tokens,
                "supportsToolCall": bool(m.tool_calling),
                "supportsImages": False,
                "supportsReasoning": bool(m.reasoning),
                "useCustomProtocol": False,
            })
            changes.append(model_id)

    if do_write:
        WORKBUDDY_MODELS.parent.mkdir(parents=True, exist_ok=True)
        WORKBUDDY_MODELS.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n")
        _restrict_permissions(WORKBUDDY_MODELS)
        print(f"  ✓ wrote {len(changes)} WorkBuddy models ({WORKBUDDY_MODELS}); restart WorkBuddy to apply")
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
# Agents synced automatically (when detected) on serve/refresh cycles.
DEFAULT_AGENTS = ["pi", "omp", "opencode", "hermes", "claude", "kimi", "openclaw", "workbuddy"]

# Adapters that distinguish "detected install" from an explicit --agent request.
_EXPLICIT_AWARE = {"claude", "kimi", "openclaw", "workbuddy"}


def sync_all(
    reg: Registry,
    do_write: bool = True,
    agents: list[str] | None = None,
    proxy_url: str = "http://127.0.0.1:8337/v1",
    proxy_token: str = "",
    codex_model: str = "",
    claude_model: str = "",
    kimi_include_model_ids: set[str] | None = None,
) -> dict[str, list[str]]:
    """Sync registry to all agents. Returns {agent: [changed_providers]}.

    When ``agents`` is None, every default agent whose config is detected
    gets synced. An explicit agent list forces config creation even for
    clients that haven't run on this machine yet.
    """
    explicit = agents is not None
    if agents is None:
        agents = DEFAULT_AGENTS

    if do_write:
        _backup()

    results = {}
    sync_map = {
        "pi": write_pi_models,
        "omp": sync_omp,
        "opencode": sync_opencode,
        "hermes": sync_hermes,
        "codex": sync_codex,
        "claude": sync_claude,
        "kimi": sync_kimi,
        "openclaw": sync_openclaw,
        "workbuddy": sync_workbuddy,
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
                if agent == "claude":
                    kwargs["claude_model"] = claude_model
                if agent == "kimi":
                    kwargs["include_model_ids"] = kimi_include_model_ids
                if agent in _EXPLICIT_AWARE:
                    kwargs["explicit"] = explicit
                changes = fn(reg, **kwargs)
                results[agent] = changes
            except Exception as e:
                results[agent] = [f"ERROR: {e}"]

    return results
