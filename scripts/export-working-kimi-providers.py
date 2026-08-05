#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if SRC.exists():
    sys.path.insert(0, str(SRC))

from open_free_router.config import Config
from open_free_router.registry import Registry


WORKING_MODELS: dict[str, set[str]] = {
    "google-ai-studio": {
        "gemini-2.5-flash",
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.6-flash",
    },
    "groq": {
        "llama-3.1-8b-instant",
        "llama-3.3-70b-versatile",
        "openai/gpt-oss-20b",
        "openai/gpt-oss-120b",
    },
    "nvidia-nim": {
        "minimaxai/minimax-m3",
        "nvidia/nemotron-3-ultra-550b-a55b",
    },
    "nous": {
        "inclusionai/ling-3.0-flash:free",
        "poolside/laguna-s-2.1:free",
        "poolside/laguna-xs-2.1:free",
        "stepfun/step-3.7-flash:free",
        "tencent/hy3:free",
    },
    "poolside": {
        "laguna-s-2.1",
        "laguna-xs-2.1",
    },
    "sensenova": {
        "deepseek-v4-flash",
        "glm-5.2",
        "sensenova-6.7-flash-lite",
    },
    "stepfun": {
        "step-3.5-flash",
    },
}


def default_output_path() -> Path:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path.home() / f"open-free-router-working-kimi-providers-{timestamp}.yaml"


def provider_to_dict(provider, model_ids: set[str]) -> dict:
    data = {
        "base_url": provider.base_url,
        "auto_refresh": provider.auto_refresh,
        "refresh_method": provider.refresh_method,
    }
    if provider.upstream_url and provider.upstream_url != provider.base_url:
        data["upstream_url"] = provider.upstream_url
    if provider.api_key:
        data["api_key"] = provider.api_key
    if provider.api_key_env:
        data["api_key_env"] = provider.api_key_env
    if provider.auth_mode != "bearer":
        data["auth_mode"] = provider.auth_mode
    if provider.api_keys:
        data["api_keys"] = provider.api_keys
    if provider.prefix:
        data["prefix"] = provider.prefix
    if provider.free_tier.configured:
        data["free_tier"] = provider.free_tier.to_dict(preserve_invalid=True)
    data["models"] = [
        model.to_dict(preserve_invalid_evidence=True)
        for model in provider.models
        if model.id in model_ids
    ]
    return data


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export only the provider credentials and model entries that were "
            "verified as reachable from the local Kimi Code open-free-router config."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to open-free-router config.yaml. Defaults to the normal config lookup.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=default_output_path(),
        help="Destination YAML file. The file is created with 0600 permissions.",
    )
    args = parser.parse_args()

    config = Config(args.config)
    registry = Registry.load(config.registry_path)
    if not registry.providers:
        raise SystemExit(f"No providers loaded from {config.registry_path}")

    export: dict[str, dict] = {}
    missing: list[str] = []
    for provider_name, model_ids in WORKING_MODELS.items():
        provider = registry.get(provider_name)
        if provider is None:
            missing.append(provider_name)
            continue
        selected = provider_to_dict(provider, model_ids)
        if not selected.get("models"):
            missing.append(provider_name)
            continue
        export[provider_name] = selected

    if not export:
        raise SystemExit("No working providers found to export.")

    args.output = args.output.expanduser()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        yaml.safe_dump(export, f, allow_unicode=True, sort_keys=False)
    args.output.chmod(0o600)

    model_count = sum(len(provider.get("models", [])) for provider in export.values())
    print(f"Exported {len(export)} providers / {model_count} models to {args.output}")
    print("The output contains API keys. Transfer it with SSH, encrypted storage, or a password manager.")
    if missing:
        print(f"Skipped missing providers: {', '.join(missing)}")


if __name__ == "__main__":
    main()
