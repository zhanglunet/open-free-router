#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from open_free_router.public_catalog import build_public_catalog, write_public_catalog
from open_free_router.registry import Registry


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a credential-free public model catalog")
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--status", type=Path, default=Path("docs/provider-status.json"))
    parser.add_argument("--profiles", type=Path, default=Path("docs/provider-profiles.json"))
    parser.add_argument("--output", type=Path, default=Path("site/data/catalog.json"))
    parser.add_argument(
        "--now",
        type=datetime.fromisoformat,
        default=None,
        help="Pin generated_at (ISO-8601) so the export is byte-reproducible",
    )
    args = parser.parse_args()
    statuses = json.loads(args.status.read_text(encoding="utf-8"))
    profiles = json.loads(args.profiles.read_text(encoding="utf-8"))
    registry = Registry.load(args.registry)
    # Registry.load swallows a missing file and returns an empty registry, so a
    # typo in --registry would otherwise publish 0 providers / 0 models with a
    # brand-new generated_at and exit 0.
    if not registry.providers:
        raise SystemExit(f"Refusing to export an empty catalog: no providers loaded from {args.registry}")
    catalog = build_public_catalog(registry, statuses, profiles, now=args.now)
    write_public_catalog(catalog, args.output)
    print(f"Exported {catalog['provider_count']} providers / {catalog['model_count']} models to {args.output}")


if __name__ == "__main__":
    main()
