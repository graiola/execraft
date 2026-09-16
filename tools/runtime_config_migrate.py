#!/usr/bin/env python3
"""Preview/apply schema-v3 -> v4 runtime topology migration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from execraft.model_registry import load_model_route_registry
from execraft.runtime.config_migration import (
    apply_agents_v4_migration,
    preview_agents_v4_migration,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("agents", type=Path)
    parser.add_argument("--providers", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    registry = load_model_route_registry(args.providers, environ={}) if args.providers else None
    preview = preview_agents_v4_migration(args.agents, model_registry=registry)
    backup = None
    if args.apply and preview.changed:
        backup = apply_agents_v4_migration(args.agents, preview)
    if args.json:
        payload = preview.as_mapping()
        payload["applied"] = backup is not None
        payload["backup"] = str(backup) if backup is not None else ""
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for warning in preview.warnings:
            print(f"warning: {warning}")
        print(preview.diff or "agents configuration is already canonical schema v4")
        if backup is not None:
            print(f"backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
