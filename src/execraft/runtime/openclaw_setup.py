"""Reusable, side-effect-controlled OpenClaw provisioning services.

The CLI helper and GUI both use this module.  Configuration mutations are
previewed from the current file hash, validated through the public schema-v4
parser, and applied only when the operator submits the reviewed hash.  Secrets
are represented only by references; this service never resolves them.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from execraft.agents.config import AgentConfigError, parse_execution_config
from execraft.runtime.execution_setup import redact_reference_diff
from execraft.runtime.openclaw_protocol import OPENCLAW_PROTOCOL_VERSION, TESTED_OPENCLAW_VERSIONS

NPM_PACKAGE = "openclaw"
DEFAULT_GATEWAY = "ws://127.0.0.1:18789"
DEFAULT_AUTH_REF = "env:OPENCLAW_GATEWAY_TOKEN"
MINIMUM_NODE_MAJOR = 20


class OpenClawSetupError(ValueError):
    """Raised when a provisioning request is invalid or became stale."""


@dataclass(frozen=True)
class OpenClawHostStatus:
    npm: str
    node_major: int | None
    binary_path: str
    installed_version: str
    pinned_version: str

    @property
    def installed(self) -> bool:
        return bool(self.binary_path)

    @property
    def version_matches_pin(self) -> bool:
        return bool(self.installed_version) and self.installed_version == self.pinned_version

    def as_mapping(self) -> dict[str, Any]:
        return {
            "npm": self.npm,
            "node_major": self.node_major,
            "binary_path": self.binary_path,
            "installed_version": self.installed_version,
            "pinned_version": self.pinned_version,
            "installed": self.installed,
            "version_matches_pin": self.version_matches_pin,
            "protocol_version": OPENCLAW_PROTOCOL_VERSION,
        }


@dataclass(frozen=True)
class OpenClawConfigPreview:
    source_sha256: str
    changed: bool
    diff: str
    warnings: tuple[str, ...]
    rendered_yaml: str

    def as_mapping(self) -> dict[str, Any]:
        return {
            "source_sha256": self.source_sha256,
            "changed": self.changed,
            "diff": redact_reference_diff(self.diff),
            "warnings": list(self.warnings),
        }


def pinned_version() -> str:
    if not TESTED_OPENCLAW_VERSIONS:
        raise OpenClawSetupError("no validated OpenClaw version is recorded")
    return sorted(TESTED_OPENCLAW_VERSIONS)[-1]


def _run(command: Sequence[str], *, timeout: int = 60) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            list(command), capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def _node_major() -> int | None:
    if shutil.which("node") is None:
        return None
    code, output = _run(["node", "--version"], timeout=5)
    if code != 0:
        return None
    token = output.strip().lstrip("v").split(".", 1)[0]
    return int(token) if token.isdigit() else None


def _installed_version(binary: str) -> str:
    code, output = _run([binary, "--version"], timeout=5)
    if code != 0:
        return ""
    for token in output.replace("\n", " ").split():
        cleaned = token.strip().lstrip("v")
        if cleaned and cleaned[0].isdigit():
            return cleaned
    return ""


def inspect_openclaw_host(prefix: Path | None = None) -> OpenClawHostStatus:
    search_path = os.environ.get("PATH", "")
    if prefix is not None:
        search_path = f"{prefix.expanduser().resolve() / 'bin'}{os.pathsep}{search_path}"
    binary_path = shutil.which("openclaw", path=search_path) or ""
    return OpenClawHostStatus(
        npm=shutil.which("npm") or "",
        node_major=_node_major(),
        binary_path=binary_path,
        installed_version=_installed_version(binary_path) if binary_path else "",
        pinned_version=pinned_version(),
    )


def openclaw_install_command(prefix: Path, *, version: str = "") -> tuple[str, ...]:
    npm = shutil.which("npm") or ""
    if not npm:
        raise OpenClawSetupError(
            f"npm is required; install Node.js {MINIMUM_NODE_MAJOR}+ first"
        )
    major = _node_major()
    if major is not None and major < MINIMUM_NODE_MAJOR:
        raise OpenClawSetupError(
            f"OpenClaw requires Node.js {MINIMUM_NODE_MAJOR}+, found major version {major}"
        )
    selected = version.strip() or pinned_version()
    return (
        npm,
        "install",
        "--global",
        "--prefix",
        str(prefix.expanduser().resolve()),
        f"{NPM_PACKAGE}@{selected}",
    )


def install_openclaw(prefix: Path, *, version: str = "") -> dict[str, Any]:
    """Install into a user-owned prefix. Never invokes sudo or a shell."""

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise OpenClawSetupError("refusing to install OpenClaw as root")
    command = openclaw_install_command(prefix, version=version)
    prefix.expanduser().mkdir(parents=True, exist_ok=True)
    code, output = _run(command, timeout=900)
    if code != 0:
        raise OpenClawSetupError(f"npm install failed: {output}")
    return {
        "command": list(command),
        "status": inspect_openclaw_host(prefix).as_mapping(),
    }


def _auth(
    auth_kind: str,
    auth_ref: str,
    *,
    preserved_ref: str = "",
) -> tuple[str, str]:
    """Validate an authentication reference without resolving credentials.

    A blank reference preserves an existing reference when an operator edits an
    already-configured runtime.  This lets the GUI remain redacted: it never
    needs to read a credential reference back merely to avoid overwriting it.
    """

    kind = (auth_kind or "token").strip()
    reference = auth_ref.strip()
    if kind not in {"token", "password", "none"}:
        raise OpenClawSetupError("auth_kind must be token, password, or none")
    if kind == "none":
        if reference:
            raise OpenClawSetupError("auth_ref must be empty when auth_kind is none")
        return kind, ""
    reference = reference or preserved_ref.strip() or DEFAULT_AUTH_REF
    if not reference.startswith("env:") or not reference.removeprefix("env:").strip():
        raise OpenClawSetupError(
            "GUI-managed OpenClaw credentials must use an env: reference; plaintext secrets are not accepted"
        )
    return kind, reference


def _identifier(value: str, *, label: str) -> str:
    candidate = value.strip()
    if not candidate or not candidate.replace("-", "_").isidentifier():
        raise OpenClawSetupError(f"invalid {label}: {candidate!r}")
    return candidate


def _load_v4(path: Path) -> tuple[str, dict[str, Any], str]:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise OpenClawSetupError(f"agents configuration must not be a symlink: {candidate}")
    path = candidate.resolve()
    if not path.is_file():
        raise OpenClawSetupError(f"agents configuration is missing or unsafe: {path}")
    source_bytes = path.read_bytes()
    try:
        source = source_bytes.decode("utf-8")
        raw = yaml.safe_load(source) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise OpenClawSetupError(f"cannot parse agents configuration: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise OpenClawSetupError("agents configuration must contain a mapping")
    if int(raw.get("schema_version", 0)) != 4:
        raise OpenClawSetupError("OpenClaw setup requires schema v4; preview and apply migration first")
    return source, dict(raw), hashlib.sha256(source_bytes).hexdigest()




def _openclaw_runtime_block(
    existing_runtime: object,
    *,
    mode: str,
    gateway: str,
    executable: str,
    auth_kind: str,
    auth_ref: str,
) -> dict[str, Any]:
    block: dict[str, Any] = (
        dict(existing_runtime) if isinstance(existing_runtime, Mapping) else {}
    )
    block.update(
        {
            "kind": "openclaw",
            "mode": mode,
            "gateway": gateway.strip() or DEFAULT_GATEWAY,
            "version_policy": "pinned-compatible",
            "auth_kind": auth_kind,
        }
    )
    if auth_ref:
        block["auth_ref"] = auth_ref
    else:
        block.pop("auth_ref", None)
    selected_executable = executable.strip()
    if selected_executable and selected_executable != "openclaw":
        block["executable"] = selected_executable
    elif not isinstance(existing_runtime, Mapping):
        block.pop("executable", None)
    return block


def _ensure_model_route(
    routes: dict[str, Any],
    targets: dict[str, Any],
    *,
    model_route_id: str,
    route_id: str,
    provider: str,
    model: str,
    target_id: str,
    endpoint: str,
    target_kind: str,
) -> str:
    selected_route = model_route_id.strip()
    if not selected_route:
        raise OpenClawSetupError("select an existing model route or create one")
    if selected_route in routes:
        return selected_route

    created_route_id = _identifier(route_id or selected_route, label="model route ID")
    if created_route_id != selected_route:
        raise OpenClawSetupError(
            "model_route_id and route_id must match when creating a route"
        )
    if not provider.strip() or not model.strip():
        raise OpenClawSetupError("new model routes require provider and model")

    route: dict[str, Any] = {"provider": provider.strip(), "model": model.strip()}
    selected_target = target_id.strip()
    if endpoint.strip() or selected_target:
        selected_target = _identifier(
            selected_target or "local-model", label="execution target ID"
        )
        existing_target = targets.get(selected_target)
        target_block = (
            dict(existing_target) if isinstance(existing_target, Mapping) else {}
        )
        target_block["kind"] = target_kind.strip() or "local"
        if endpoint.strip():
            target_block["endpoint"] = endpoint.strip()
        targets[selected_target] = target_block
        if endpoint.strip():
            route["endpoint"] = endpoint.strip()
        route["default_target"] = selected_target
    routes[created_route_id] = route
    return created_route_id


def _openclaw_profile_block(
    existing_profile: object,
    *,
    runtime_id: str,
    model_route_id: str,
    target_id: str,
    capabilities: Sequence[str],
    priority: int,
    max_complexity: int,
) -> dict[str, Any]:
    profile: dict[str, Any] = (
        dict(existing_profile) if isinstance(existing_profile, Mapping) else {}
    )
    existing_policy = profile.get("policy")
    policy = dict(existing_policy) if isinstance(existing_policy, Mapping) else {}
    policy.update(
        {
            "sandbox": "workspace-write",
            "sandbox_enabled": True,
            "auto_approve": False,
            "dangerously_skip_permissions": False,
        }
    )
    profile.update(
        {
            "runtime": runtime_id,
            "model_route": model_route_id,
            "enabled": True,
            "capabilities": sorted(
                {str(item).strip() for item in capabilities if str(item).strip()}
            ),
            "priority": int(priority),
            "max_complexity": int(max_complexity),
            "concurrency_group": target_id or runtime_id,
            "policy": policy,
        }
    )
    if target_id:
        profile["target"] = target_id
    else:
        profile.pop("target", None)
    return profile


def preview_openclaw_configuration(
    agents_path: Path,
    *,
    runtime_id: str,
    profile_id: str,
    mode: str,
    gateway: str,
    executable: str,
    auth_kind: str,
    auth_ref: str,
    model_route_id: str,
    target_id: str = "",
    capabilities: Sequence[str] = ("implement", "review", "fix_review"),
    priority: int = 50,
    max_complexity: int = 70,
    route_id: str = "",
    provider: str = "",
    model: str = "",
    endpoint: str = "",
    target_kind: str = "local",
) -> OpenClawConfigPreview:
    """Preview a validated OpenClaw runtime/profile and optional model route."""

    source, raw, source_sha = _load_v4(agents_path)
    runtime_id = _identifier(runtime_id, label="runtime ID")
    profile_id = _identifier(profile_id, label="profile ID")
    if mode not in {"managed", "external"}:
        raise OpenClawSetupError("OpenClaw mode must be managed or external")

    runtimes = dict(raw.get("runtimes") or {})
    routes = dict(raw.get("model_routes") or {})
    targets = dict(raw.get("execution_targets") or {})
    agents = dict(raw.get("agents") or {})

    existing_runtime = runtimes.get(runtime_id)
    preserved_auth_ref = ""
    if isinstance(existing_runtime, Mapping):
        existing_kind = str(existing_runtime.get("auth_kind", "token")).strip()
        if existing_kind == (auth_kind or "token").strip():
            preserved_auth_ref = str(existing_runtime.get("auth_ref", "")).strip()
    selected_auth_kind, selected_auth_ref = _auth(
        auth_kind, auth_ref, preserved_ref=preserved_auth_ref
    )
    runtimes[runtime_id] = _openclaw_runtime_block(
        existing_runtime,
        mode=mode,
        gateway=gateway,
        executable=executable,
        auth_kind=selected_auth_kind,
        auth_ref=selected_auth_ref,
    )

    selected_route = _ensure_model_route(
        routes,
        targets,
        model_route_id=model_route_id,
        route_id=route_id,
        provider=provider,
        model=model,
        target_id=target_id,
        endpoint=endpoint,
        target_kind=target_kind,
    )
    selected_target = target_id.strip()
    if selected_target and selected_target not in targets:
        raise OpenClawSetupError(f"unknown execution target: {selected_target}")
    agents[profile_id] = _openclaw_profile_block(
        agents.get(profile_id),
        runtime_id=runtime_id,
        model_route_id=selected_route,
        target_id=selected_target,
        capabilities=capabilities,
        priority=priority,
        max_complexity=max_complexity,
    )

    updated = dict(raw)
    updated.update(
        {
            "schema_version": 4,
            "runtimes": runtimes,
            "execution_targets": targets,
            "model_routes": routes,
            "agents": agents,
        }
    )
    try:
        parse_execution_config(updated, include_disabled=True)
    except (AgentConfigError, ValueError, KeyError) as exc:
        raise OpenClawSetupError(str(exc)) from exc

    rendered = yaml.safe_dump(updated, sort_keys=False, width=1000)
    diff = "".join(
        difflib.unified_diff(
            source.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=str(agents_path),
            tofile=f"{agents_path} (OpenClaw setup preview)",
        )
    )
    return OpenClawConfigPreview(
        source_sha256=source_sha,
        changed=source != rendered,
        diff=diff,
        warnings=(),
        rendered_yaml=rendered,
    )


def apply_openclaw_configuration(
    agents_path: Path,
    preview: OpenClawConfigPreview,
) -> Path | None:
    """Apply a reviewed preview atomically and return its backup path."""

    candidate = agents_path.expanduser()
    if candidate.is_symlink():
        raise OpenClawSetupError(f"agents configuration must not be a symlink: {candidate}")
    path = candidate.resolve()
    if not path.is_file():
        raise OpenClawSetupError(f"agents configuration is missing or unsafe: {path}")
    current = path.read_bytes()
    if hashlib.sha256(current).hexdigest() != preview.source_sha256:
        raise OpenClawSetupError(
            "agents configuration changed after preview; generate a new preview"
        )
    if not preview.changed:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(f"{path.name}.pre-openclaw-{stamp}.bak")
    if backup.exists():
        raise OpenClawSetupError(f"backup already exists: {backup}")
    backup.write_bytes(current)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    temp = Path(temp_name)
    try:
        temp.write_text(preview.rendered_yaml, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return backup


__all__ = [
    "DEFAULT_AUTH_REF",
    "DEFAULT_GATEWAY",
    "MINIMUM_NODE_MAJOR",
    "OpenClawConfigPreview",
    "OpenClawHostStatus",
    "OpenClawSetupError",
    "apply_openclaw_configuration",
    "inspect_openclaw_host",
    "install_openclaw",
    "openclaw_install_command",
    "pinned_version",
    "preview_openclaw_configuration",
]
