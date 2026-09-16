#!/usr/bin/env python3
"""Install/configure the optional OpenClaw runtime for Execraft.

This command is intentionally a thin compatibility shell over
``execraft.runtime.openclaw_setup``.  The same side-effect-controlled services are
used by the GUI, so install/configuration policy cannot drift between operator
surfaces.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from execraft.runtime.openclaw_protocol import OPENCLAW_PROTOCOL_VERSION, TESTED_OPENCLAW_VERSIONS
from execraft.runtime.openclaw_setup import (
    DEFAULT_AUTH_REF,
    DEFAULT_GATEWAY,
    MINIMUM_NODE_MAJOR,
    NPM_PACKAGE,
    OpenClawHostStatus,
    OpenClawSetupError,
    apply_openclaw_configuration,
    inspect_openclaw_host,
    install_openclaw,
    openclaw_install_command,
    pinned_version as _pinned_version,
    preview_openclaw_configuration,
)

ROOT = Path(__file__).resolve().parents[1]
BINARY = "openclaw"
DEFAULT_RUNTIME_ID = "openclaw-local"


class InstallError(RuntimeError):
    """Compatibility error type for existing automation around this tool."""


def pinned_version() -> str:
    try:
        return _pinned_version()
    except OpenClawSetupError as exc:
        raise InstallError(str(exc)) from exc


def inspect_host(prefix: Path | None = None) -> OpenClawHostStatus:
    return inspect_openclaw_host(prefix)


def install(prefix: Path, version: str, *, dry_run: bool) -> list[str]:
    try:
        command = list(openclaw_install_command(prefix, version=version))
        if not dry_run:
            install_openclaw(prefix, version=version)
        return command
    except OpenClawSetupError as exc:
        raise InstallError(str(exc)) from exc


def resolve_auth(mode: str, auth_kind: str, auth_ref: str) -> tuple[str, str]:
    """Resolve safe defaults while preserving the historical CLI contract."""

    del mode
    kind = auth_kind or "token"
    reference = auth_ref.strip()
    if kind == "none":
        if reference:
            raise InstallError("--auth-ref is meaningless with --auth-kind none")
        return "none", ""
    if kind not in {"token", "password"}:
        raise InstallError(f"unsupported authentication kind: {kind}")
    reference = reference or DEFAULT_AUTH_REF
    if not reference.startswith("env:") or not reference.removeprefix("env:").strip():
        raise InstallError(
            f"--auth-kind {kind} requires --auth-ref naming an environment variable "
            "(for example env:OPENCLAW_GATEWAY_TOKEN)"
        )
    return kind, reference


def configure(
    agents_path: Path,
    *,
    runtime_id: str,
    profile_id: str,
    gateway: str,
    auth_kind: str,
    auth_ref: str,
    mode: str,
    executable: str,
    capabilities: list[str],
    model_route: str,
    priority: int,
    max_complexity: int,
    apply: bool,
) -> tuple[dict[str, Any], Path | None]:
    """Compatibility wrapper around the canonical transactional setup service."""

    raw = yaml.safe_load(agents_path.read_text(encoding="utf-8")) or {}
    if int(raw.get("schema_version", 0)) != 4:
        raise InstallError(
            f"{agents_path} is schema v{raw.get('schema_version')}; migrate it first "
            "with tools/runtime_config_migrate.py --apply"
        )
    if runtime_id in (raw.get("runtimes") or {}):
        raise InstallError(f"runtime {runtime_id!r} already exists in {agents_path}")
    if profile_id in (raw.get("agents") or {}):
        raise InstallError(f"agent profile {profile_id!r} already exists in {agents_path}")
    routes = raw.get("model_routes") or {}
    if model_route and model_route not in routes:
        known = ", ".join(sorted(routes)) or "none"
        raise InstallError(f"unknown model route {model_route!r}; configured routes: {known}")

    auth_kind, auth_ref = resolve_auth(mode, auth_kind, auth_ref)
    try:
        preview = preview_openclaw_configuration(
            agents_path,
            runtime_id=runtime_id,
            profile_id=profile_id,
            mode=mode,
            gateway=gateway,
            executable=executable,
            auth_kind=auth_kind,
            auth_ref=auth_ref,
            model_route_id=model_route,
            capabilities=capabilities,
            priority=priority,
            max_complexity=max_complexity,
        )
        updated = yaml.safe_load(preview.rendered_yaml) or {}
        backup = apply_openclaw_configuration(agents_path, preview) if apply else None
        return dict(updated), backup
    except OpenClawSetupError as exc:
        raise InstallError(f"generated configuration is invalid: {exc}") from exc


def _render_status(status: OpenClawHostStatus) -> str:
    lines = [
        "OpenClaw host status",
        f"  npm:            {status.npm or 'NOT FOUND'}",
        f"  node major:     {status.node_major if status.node_major is not None else 'NOT FOUND'}",
        f"  {BINARY}:       {status.binary_path or 'NOT INSTALLED'}",
        f"  version:        {status.installed_version or '-'}",
        f"  pinned version: {status.pinned_version} (wire protocol {OPENCLAW_PROTOCOL_VERSION})",
    ]
    if not status.installed:
        lines.append("  -> run with --install to provision the pinned release")
    elif not status.version_matches_pin:
        lines.append(
            f"  -> WARNING: installed {status.installed_version} is not the validated "
            f"{status.pinned_version}; the pinned-compatible policy will refuse to connect"
        )
    else:
        lines.append("  -> matches the validated release")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install and configure the OpenClaw Gateway runtime for Execraft."
    )
    parser.add_argument("--install", action="store_true", help="install the pinned OpenClaw release")
    parser.add_argument("--configure", action="store_true", help="add an OpenClaw runtime/profile")
    parser.add_argument("--prefix", type=Path, default=Path("~/.local"), help="user-owned npm prefix")
    parser.add_argument("--version", default="", help="OpenClaw version; default is validated pin")
    parser.add_argument("--allow-unvalidated-version", action="store_true")
    parser.add_argument("--project", default="sample")
    parser.add_argument("--agents-file", type=Path, default=None)
    parser.add_argument("--runtime-id", default=DEFAULT_RUNTIME_ID)
    parser.add_argument("--profile-id", default="")
    parser.add_argument("--gateway", default=DEFAULT_GATEWAY)
    parser.add_argument("--auth-kind", choices=("none", "token", "password"), default="")
    parser.add_argument("--auth-ref", default="")
    parser.add_argument("--mode", choices=("managed", "external"), default="managed")
    parser.add_argument("--executable", default=BINARY)
    parser.add_argument("--capabilities", default="review")
    parser.add_argument("--model-route", default="")
    parser.add_argument("--priority", type=int, default=50)
    parser.add_argument("--max-complexity", type=int, default=40)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        version = args.version or pinned_version()
        if args.version and args.version not in TESTED_OPENCLAW_VERSIONS:
            if not args.allow_unvalidated_version:
                raise InstallError(
                    f"{args.version} is not a validated OpenClaw release "
                    f"({', '.join(sorted(TESTED_OPENCLAW_VERSIONS))})"
                )
            print(f"warning: installing unvalidated OpenClaw {args.version}", file=sys.stderr)

        if args.install:
            command = install(args.prefix, version, dry_run=not args.apply)
            if args.apply:
                print(f"installed {NPM_PACKAGE}@{version} into {args.prefix}")
            else:
                print("dry run; re-run with --apply to execute:")
                print("  " + " ".join(command))

        status = inspect_host(args.prefix)
        print(json.dumps(status.as_mapping(), indent=2, sort_keys=True) if args.json else _render_status(status))

        if args.configure:
            agents_path = args.agents_file or (ROOT / "projects" / args.project / "agents.yaml")
            capabilities = [item.strip() for item in args.capabilities.split(",") if item.strip()]
            profile_id = args.profile_id or f"{args.runtime_id}-{capabilities[0] if capabilities else 'review'}"
            updated, backup = configure(
                agents_path,
                runtime_id=args.runtime_id,
                profile_id=profile_id,
                gateway=args.gateway,
                auth_kind=args.auth_kind,
                auth_ref=args.auth_ref,
                mode=args.mode,
                executable=args.executable,
                capabilities=capabilities,
                model_route=args.model_route,
                priority=args.priority,
                max_complexity=args.max_complexity,
                apply=args.apply,
            )
            if args.json:
                print(json.dumps({"configured": True, "applied": bool(args.apply), "backup": str(backup or ""), "config": updated}, indent=2, sort_keys=True))
            elif args.apply:
                print(f"updated {agents_path}" + (f" (backup: {backup})" if backup else ""))
            else:
                print("\nconfiguration preview (use --apply to write):")
                print(yaml.safe_dump(updated, sort_keys=False, width=1000).rstrip())
        return 0
    except (InstallError, OSError, ValueError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
