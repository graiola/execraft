"""Safe schema-v3 -> schema-v4 execution-topology migration.

Migration is always previewed first.  Legacy agent configuration does not itself
contain enough information to reconstruct local/satellite placement, so this
module enriches OpenCode model routes from the canonical model registry when
one is supplied and otherwise emits an explicit warning rather than inventing a
target.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from execraft.agents.config import AgentConfigError, parse_execution_config


class RuntimeConfigMigrationError(ValueError):
    pass


_VALID_ID = re.compile(r"[^A-Za-z0-9_-]+")

_COLOCATED_ORCHESTRATION_KEYS = ("scheduling", "supervisor", "commit", "subagents")


def _is_v4_id(value: str) -> bool:
    return bool(value) and value.replace("-", "_").isidentifier()


def _preserve_or_normalize_id(value: str, *, prefix: str) -> str:
    return value if _is_v4_id(value) else _slug(value, prefix=prefix)


def _slug(value: str, *, prefix: str = "item") -> str:
    result = _VALID_ID.sub("-", value.strip()).strip("-_").lower()
    if not result:
        result = prefix
    if result[0].isdigit():
        result = f"{prefix}-{result}"
    return result


def _unique(base: str, used: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _strip_empty(mapping: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in mapping.items():
        if value in ("", None, (), [], {}):
            continue
        result[str(key)] = value
    return result


def _registry_route(model_registry: Any, runtime: Any, route: Any) -> tuple[Any, Any] | None:
    if model_registry is None or str(getattr(runtime, "adapter", "")) != "opencode":
        return None
    reference = route.reference_for_native_adapter("opencode")
    canonical = model_registry.route_for_model(reference)
    if canonical is None:
        return None
    return canonical, model_registry.endpoint_for_model(reference)


@dataclass(frozen=True)
class MigrationPreview:
    source_schema_version: int
    target_schema_version: int
    changed: bool
    source_sha256: str
    rendered_yaml: str
    diff: str
    warnings: tuple[str, ...]

    def as_mapping(self) -> dict[str, Any]:
        return {
            "source_schema_version": self.source_schema_version,
            "target_schema_version": self.target_schema_version,
            "changed": self.changed,
            "source_sha256": self.source_sha256,
            "warnings": list(self.warnings),
            "diff": self.diff,
        }


def _migrate_legacy(execution: Any, *, model_registry: Any = None) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    used_profile_ids: set[str] = set()
    profile_ids: dict[str, str] = {}
    for profile in execution.agents:
        profile_ids[profile.id] = _unique(
            _preserve_or_normalize_id(profile.id, prefix="profile"), used_profile_ids
        )
        if profile_ids[profile.id] != profile.id:
            warnings.append(
                f"legacy profile ID {profile.id!r} was normalized to {profile_ids[profile.id]!r}"
            )

    runtimes: dict[str, dict[str, Any]] = {}
    runtime_ids: dict[tuple[str, str, str], str] = {}
    used_runtime_ids: set[str] = set()
    routes: dict[str, dict[str, Any]] = {}
    route_ids: dict[tuple[str, str, str, str, str], str] = {}
    used_route_ids: set[str] = set()
    targets: dict[str, dict[str, Any]] = {}
    profiles: dict[str, dict[str, Any]] = {}

    for profile in execution.agents:
        runtime = execution.runtime(profile.runtime_id)
        runtime_key = (
            _enum_value(runtime.kind),
            str(getattr(runtime, "adapter", "")),
            str(getattr(runtime, "binary", "")),
        )
        runtime_id = runtime_ids.get(runtime_key)
        if runtime_id is None:
            adapter = str(getattr(runtime, "adapter", "")) or _enum_value(runtime.kind)
            runtime_id = _unique(f"native-{_slug(adapter)}", used_runtime_ids)
            runtime_ids[runtime_key] = runtime_id
            runtimes[runtime_id] = _strip_empty(runtime.as_mapping())

        model_route_id = ""
        target_id = ""
        if profile.model_route_id:
            old_route = execution.model_route(profile.model_route_id)
            canonical_pair = _registry_route(model_registry, runtime, old_route)
            if canonical_pair is not None:
                canonical, endpoint = canonical_pair
                route_mapping = _strip_empty(canonical.as_mapping())
                if endpoint is not None:
                    target = endpoint.target_config()
                    target_id = str(target.id)
                    targets.setdefault(target_id, _strip_empty(target.as_mapping()))
                    route_mapping["default_target"] = target_id
            else:
                route_mapping = _strip_empty(old_route.as_mapping())
                if str(getattr(runtime, "adapter", "")) == "opencode":
                    warnings.append(
                        f"profile {profile.id!r}: no canonical model endpoint matched "
                        f"{old_route.reference_for_native_adapter('opencode')!r}; "
                        "target placement remains unresolved"
                    )

            route_key = (
                str(route_mapping.get("provider", "")),
                str(route_mapping.get("provider_alias", "")),
                str(route_mapping.get("model", "")),
                str(route_mapping.get("endpoint", "")),
                str(route_mapping.get("default_target", "")),
            )
            model_route_id = route_ids.get(route_key, "")
            if not model_route_id:
                model_route_id = _unique(
                    f"model-{_slug(str(route_mapping.get('provider_alias') or route_mapping.get('provider') or profile.id))}-"
                    f"{_slug(str(route_mapping.get('model') or profile.id), prefix='model')}",
                    used_route_ids,
                )
                route_ids[route_key] = model_route_id
                routes[model_route_id] = route_mapping
            if not target_id:
                target_id = str(route_mapping.get("default_target", ""))

        profile_mapping = _strip_empty(profile.as_mapping())
        profile_mapping["runtime"] = runtime_id
        if model_route_id:
            profile_mapping["model_route"] = model_route_id
        else:
            profile_mapping.pop("model_route", None)
        if target_id:
            profile_mapping["target"] = target_id
        else:
            profile_mapping.pop("target", None)

        # Repair profile references when a legacy ID needed normalization.
        aliases = [str(alias) for alias in profile_mapping.get("aliases", []) if str(alias)]
        if profile_ids[profile.id] != profile.id and profile.id not in aliases:
            # Preserve the legacy scheduler/provider identity as an alias so
            # persisted preferences and operator commands keep resolving.
            aliases.insert(0, profile.id)
        if aliases:
            profile_mapping["aliases"] = aliases
        policy = profile_mapping.get("policy")
        if isinstance(policy, Mapping):
            policy = dict(policy)
            by_capability = policy.get("agent_by_capability")
            if isinstance(by_capability, Mapping):
                policy["agent_by_capability"] = {
                    str(key): profile_ids.get(str(value), str(value))
                    for key, value in by_capability.items()
                }
            repair = str(policy.get("format_repair_agent", ""))
            if repair:
                policy["format_repair_agent"] = profile_ids.get(repair, repair)
            profile_mapping["policy"] = policy
        profiles[profile_ids[profile.id]] = profile_mapping

    candidate = {
        "schema_version": 4,
        "runtimes": runtimes,
        "execution_targets": targets,
        "model_routes": routes,
        "agents": profiles,
    }
    # Validation is part of migration, not a best-effort post-step.
    parse_execution_config(candidate, include_disabled=True)
    return candidate, warnings


def _migration_candidate(
    raw: Mapping[str, Any],
    *,
    model_registry: Any = None,
) -> tuple[int, dict[str, Any], list[str]]:
    try:
        source_version = int(raw.get("schema_version", 1))
    except (TypeError, ValueError) as exc:
        raise RuntimeConfigMigrationError("agents schema_version must be an integer") from exc

    try:
        execution = parse_execution_config(raw, include_disabled=True)
        if source_version >= 4:
            candidate = execution.as_mapping()
            warnings: list[str] = []
        else:
            candidate, warnings = _migrate_legacy(execution, model_registry=model_registry)

        for key in _COLOCATED_ORCHESTRATION_KEYS:
            if key in raw:
                candidate[key] = raw[key]

        parse_execution_config(candidate, include_disabled=True)
    except AgentConfigError as exc:
        raise RuntimeConfigMigrationError(str(exc)) from exc
    return source_version, candidate, warnings


def normalize_execution_for_operator(
    raw: Mapping[str, Any],
    *,
    model_registry: Any = None,
) -> tuple[Any, tuple[str, ...]]:
    """Return a fully normalized operator topology without rewriting the project.

    Legacy v1-v3 projects therefore get the same Runtime / Model Route / Target
    inventory as a migrated project while remaining runnable through the existing
    compatibility path. Physical target enrichment uses the canonical model
    registry and never mutates ``agents.yaml``.
    """

    _source_version, candidate, warnings = _migration_candidate(
        raw, model_registry=model_registry
    )
    try:
        execution = parse_execution_config(candidate, include_disabled=True)
    except AgentConfigError as exc:  # pragma: no cover - candidate is validated above.
        raise RuntimeConfigMigrationError(str(exc)) from exc
    return execution, tuple(warnings)


def preview_agents_v4_migration(
    path: Path,
    *,
    model_registry: Any = None,
) -> MigrationPreview:
    path = path.expanduser().resolve()
    if path.is_symlink() or not path.is_file():
        raise RuntimeConfigMigrationError(f"agents configuration is missing or unsafe: {path}")
    source_bytes = path.read_bytes()
    source_text = source_bytes.decode("utf-8")
    raw = yaml.safe_load(source_text) or {}
    if not isinstance(raw, Mapping):
        raise RuntimeConfigMigrationError("agents configuration must contain a mapping")
    source_version, candidate, warnings = _migration_candidate(
        raw, model_registry=model_registry
    )

    rendered = yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True)
    diff = "".join(
        difflib.unified_diff(
            source_text.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=str(path),
            tofile=f"{path} (schema v4 preview)",
        )
    )
    return MigrationPreview(
        source_schema_version=source_version,
        target_schema_version=4,
        changed=source_text != rendered,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        rendered_yaml=rendered,
        diff=diff,
        warnings=tuple(warnings),
    )


def apply_agents_v4_migration(path: Path, preview: MigrationPreview) -> Path:
    """Atomically apply a previously reviewed preview and return its backup path."""

    path = path.expanduser().resolve()
    if path.is_symlink() or not path.is_file():
        raise RuntimeConfigMigrationError(f"agents configuration is missing or unsafe: {path}")
    current = path.read_bytes()
    if hashlib.sha256(current).hexdigest() != preview.source_sha256:
        raise RuntimeConfigMigrationError(
            "agents configuration changed after preview; generate a new migration preview"
        )
    if preview.target_schema_version != 4:
        raise RuntimeConfigMigrationError("unsupported migration target")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.pre-v4-{stamp}.bak")
    if backup.exists():
        raise RuntimeConfigMigrationError(f"migration backup already exists: {backup}")
    backup.write_bytes(current)
    temp = path.with_name(f".{path.name}.migration.tmp")
    try:
        temp.write_text(preview.rendered_yaml, encoding="utf-8")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
    return backup
