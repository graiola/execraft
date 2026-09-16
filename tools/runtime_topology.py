#!/usr/bin/env python3
"""Render Runtime / Model Route / Target inventory for operator surfaces."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from execraft.model_registry import load_model_route_registry
from execraft.runtime.config_migration import normalize_execution_for_operator
from execraft.runtime.topology import build_runtime_topology


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("agents", type=Path)
    parser.add_argument("--providers", type=Path)
    args = parser.parse_args()

    raw = yaml.safe_load(args.agents.read_text(encoding="utf-8")) or {}
    registry = (
        load_model_route_registry(args.providers, environ={}) if args.providers else None
    )
    execution, warnings = normalize_execution_for_operator(raw, model_registry=registry)
    payload = build_runtime_topology(execution)
    payload["warnings"] = list(warnings)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
