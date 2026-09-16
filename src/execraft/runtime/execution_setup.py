"""Transactional schema-v4 execution topology editor.

The GUI uses this service for small, explicit routing changes.  It deliberately
owns no scheduler or runtime lifecycle: it only previews a validated YAML
mutation and applies the exact reviewed source hash with a backup.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from execraft.agents.config import AgentConfigError, parse_execution_config


class ExecutionSetupError(ValueError):
    """Raised when an execution-topology edit is invalid or stale."""


def redact_reference_diff(diff: str) -> str:
    """Hide credential-reference values from browser-facing YAML diffs."""

    redacted: list[str] = []
    for line in diff.splitlines(keepends=True):
        rendered = line
        for key in ("credential_ref", "auth_ref"):
            marker = f"{key}:"
            index = rendered.find(marker)
            if index < 0:
                continue
            newline = "\n" if rendered.endswith("\n") else ""
            rendered = f"{rendered[:index]}{marker} <configured>{newline}"
            break
        redacted.append(rendered)
    return "".join(redacted)


@dataclass(frozen=True)
class ExecutionConfigPreview:
    """Immutable preview used as the apply capability token."""

    source_sha256: str
    changed: bool
    diff: str
    rendered_yaml: str

    def as_mapping(self) -> dict[str, Any]:
        return {
            "source_sha256": self.source_sha256,
            "changed": self.changed,
            "diff": redact_reference_diff(self.diff),
        }


def _identifier(value: str, *, label: str) -> str:
    candidate = value.strip()
    if not candidate or not candidate.replace("-", "_").isidentifier():
        raise ExecutionSetupError(f"invalid {label}: {candidate!r}")
    return candidate


def _load_v4(path: Path) -> tuple[bytes, dict[str, Any]]:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ExecutionSetupError(f"agents configuration must not be a symlink: {candidate}")
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise ExecutionSetupError(f"agents configuration is missing or unsafe: {resolved}")
    source = resolved.read_bytes()
    try:
        raw = yaml.safe_load(source.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ExecutionSetupError(f"cannot parse agents configuration: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ExecutionSetupError("agents configuration must contain a mapping")
    if int(raw.get("schema_version", 0)) != 4:
        raise ExecutionSetupError("execution setup requires schema v4; migrate the project first")
    return source, dict(raw)


def _preview(path: Path, source: bytes, updated: Mapping[str, Any], *, label: str) -> ExecutionConfigPreview:
    try:
        parse_execution_config(updated, include_disabled=True)
    except (AgentConfigError, ValueError, KeyError) as exc:
        raise ExecutionSetupError(str(exc)) from exc
    rendered = yaml.safe_dump(dict(updated), sort_keys=False, width=1000).encode("utf-8")
    diff = "".join(
        difflib.unified_diff(
            source.decode("utf-8").splitlines(keepends=True),
            rendered.decode("utf-8").splitlines(keepends=True),
            fromfile=str(path),
            tofile=f"{path} ({label} preview)",
        )
    )
    return ExecutionConfigPreview(
        source_sha256=hashlib.sha256(source).hexdigest(),
        changed=source != rendered,
        diff=diff,
        rendered_yaml=rendered.decode("utf-8"),
    )


def preview_model_route_configuration(
    agents_path: Path,
    *,
    profile_id: str,
    route_id: str,
    provider: str,
    model: str,
    target_id: str = "",
    target_kind: str = "local",
    endpoint: str = "",
    credential_ref: str = "",
    api_family: str = "",
) -> ExecutionConfigPreview:
    """Preview one model route/target and bind it to an existing profile.

    This path is runtime-neutral.  It is intentionally sufficient for the
    common Native + Ollama/vLLM case and can also prepare a route later selected
    by OpenClaw.  Credentials remain references and are never resolved here.
    """

    source, raw = _load_v4(agents_path)
    profile_id = _identifier(profile_id, label="profile ID")
    route_id = _identifier(route_id, label="model route ID")
    provider = provider.strip()
    model = model.strip()
    if not provider or not model:
        raise ExecutionSetupError("model route requires provider and model")

    agents = dict(raw.get("agents") or {})
    if profile_id not in agents or not isinstance(agents[profile_id], Mapping):
        raise ExecutionSetupError(f"unknown execution profile: {profile_id}")
    routes = dict(raw.get("model_routes") or {})
    targets = dict(raw.get("execution_targets") or {})

    selected_target = target_id.strip()
    if endpoint.strip() and not selected_target:
        selected_target = f"{route_id}-target"
    if selected_target:
        selected_target = _identifier(selected_target, label="execution target ID")
        existing_target = targets.get(selected_target)
        target: dict[str, Any] = (
            dict(existing_target) if isinstance(existing_target, Mapping) else {}
        )
        target["kind"] = target_kind.strip() or str(target.get("kind", "local"))
        if endpoint.strip():
            target["endpoint"] = endpoint.strip()
        targets[selected_target] = target

    existing_route = routes.get(route_id)
    route: dict[str, Any] = (
        dict(existing_route) if isinstance(existing_route, Mapping) else {}
    )
    route.update({"provider": provider, "model": model})
    if endpoint.strip():
        route["endpoint"] = endpoint.strip()
    existing_credential_ref = (
        str(existing_route.get("credential_ref", "")).strip()
        if isinstance(existing_route, Mapping)
        else ""
    )
    selected_credential_ref = credential_ref.strip() or existing_credential_ref
    if selected_credential_ref:
        route["credential_ref"] = selected_credential_ref
    if api_family.strip():
        route["api_family"] = api_family.strip()
    if selected_target:
        route["default_target"] = selected_target
    routes[route_id] = route

    profile = dict(agents[profile_id])
    profile["model_route"] = route_id
    if selected_target:
        profile["target"] = selected_target
    else:
        profile.pop("target", None)
    agents[profile_id] = profile

    updated = dict(raw)
    updated.update(
        {
            "schema_version": 4,
            "execution_targets": targets,
            "model_routes": routes,
            "agents": agents,
        }
    )
    return _preview(agents_path, source, updated, label="execution setup")


def apply_execution_configuration(
    agents_path: Path,
    preview: ExecutionConfigPreview,
    *,
    backup_label: str = "execution-setup",
) -> Path | None:
    """Atomically apply the exact reviewed preview and return its backup."""

    candidate = agents_path.expanduser()
    if candidate.is_symlink():
        raise ExecutionSetupError(f"agents configuration must not be a symlink: {candidate}")
    path = candidate.resolve()
    if not path.is_file():
        raise ExecutionSetupError(f"agents configuration is missing or unsafe: {path}")
    current = path.read_bytes()
    if hashlib.sha256(current).hexdigest() != preview.source_sha256:
        raise ExecutionSetupError(
            "agents configuration changed after preview; generate a new preview"
        )
    if not preview.changed:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(f"{path.name}.pre-{backup_label}-{stamp}.bak")
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
    "ExecutionConfigPreview",
    "ExecutionSetupError",
    "apply_execution_configuration",
    "preview_model_route_configuration",
    "redact_reference_diff",
]
