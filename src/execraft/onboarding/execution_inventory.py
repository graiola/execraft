"""Passive runtime-neutral execution inventory for onboarding and GUI readiness.

This module deliberately does *not* contact gateways or model endpoints.  Its
job is to answer whether the project's execution topology is structurally valid
and whether local runtime executables required by that topology are present.
Live health belongs to the explicit runtime diagnostics surface.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from execraft.model_registry import ModelRegistryError, load_model_route_registry
from execraft.onboarding.models import Evidence, Finding, FindingSeverity
from execraft.project import ProjectDescriptor
from execraft.runtime.config_migration import (
    RuntimeConfigMigrationError,
    normalize_execution_for_operator,
)
from execraft.runtime.product_support import (
    profile_product_support,
    runtime_product_support,
    target_product_support,
)
from execraft.runtime_config import OpenClawMode, RuntimeKind


@dataclass(frozen=True)
class RuntimeInventoryItem:
    """One configured runtime and its deterministic local prerequisite state."""

    id: str
    kind: str
    adapter: str
    binary: str
    binary_path: str
    mode: str = ""
    gateway: str = ""
    authentication_configured: bool = False
    support_status: str = "supported"
    product_supported: bool = True

    @property
    def ready(self) -> bool:
        # External gateways and registered extensions own live readiness at their
        # explicit diagnostic boundary. Passive onboarding must not invent local
        # binary requirements for either case.
        if self.support_status == "registered_extension":
            return True
        if self.kind == RuntimeKind.OPENCLAW.value and self.mode == OpenClawMode.EXTERNAL.value:
            return True
        return bool(self.binary_path)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "adapter": self.adapter,
            "binary": self.binary,
            "binary_path": self.binary_path,
            "ready": self.ready,
            "mode": self.mode,
            "gateway": self.gateway,
            "authentication_configured": self.authentication_configured,
            "support_status": self.support_status,
            "product_supported": self.product_supported,
        }


@dataclass(frozen=True)
class ExecutionProfileInventoryItem:
    """Logical scheduler candidate with independently visible placement."""

    id: str
    name: str
    enabled: bool
    runtime_id: str
    runtime_kind: str
    model_route_id: str
    target_id: str
    model: str
    capabilities: tuple[str, ...]
    runtime_ready: bool
    support_status: str = "supported"
    product_supported: bool = True

    @property
    def ready(self) -> bool:
        return self.enabled and self.runtime_ready and self.product_supported

    def as_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "runtime_id": self.runtime_id,
            "runtime_kind": self.runtime_kind,
            "model_route_id": self.model_route_id,
            "target_id": self.target_id,
            "model": self.model,
            "capabilities": list(self.capabilities),
            "runtime_ready": self.runtime_ready,
            "support_status": self.support_status,
            "product_supported": self.product_supported,
            "ready": self.ready,
        }


@dataclass(frozen=True)
class ExecutionInventoryReport:
    path: Path | None
    source_schema_version: int
    runtimes: tuple[RuntimeInventoryItem, ...]
    profiles: tuple[ExecutionProfileInventoryItem, ...]
    model_routes: tuple[dict[str, Any], ...] = ()
    execution_targets: tuple[dict[str, Any], ...] = ()
    findings: tuple[Finding, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def enabled_profiles(self) -> tuple[ExecutionProfileInventoryItem, ...]:
        return tuple(item for item in self.profiles if item.enabled)

    @property
    def ready_profiles(self) -> tuple[ExecutionProfileInventoryItem, ...]:
        return tuple(item for item in self.profiles if item.ready)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path is not None else "",
            "source_schema_version": self.source_schema_version,
            "runtimes": [item.as_mapping() for item in self.runtimes],
            "profiles": [item.as_mapping() for item in self.profiles],
            "model_routes": list(self.model_routes),
            "execution_targets": list(self.execution_targets),
            "configured_profiles": len(self.profiles),
            "enabled_profiles": len(self.enabled_profiles),
            "ready_profiles": len(self.ready_profiles),
            "findings": [item.as_mapping() for item in self.findings],
            "evidence": [item.as_mapping() for item in self.evidence],
            "warnings": list(self.warnings),
        }


class ExecutionInventory:
    """Inspect normalized execution configuration without executing provider code."""

    def inspect(self, project: ProjectDescriptor) -> ExecutionInventoryReport:
        path = project.configured_path("agents_file")
        if path is None or not path.is_file():
            finding = Finding(
                code="execution.configuration_missing",
                severity=FindingSeverity.ERROR,
                message="Project has no readable execution configuration",
                subject="execution",
                remediation="Configure agents_file and at least one execution profile.",
            )
            return ExecutionInventoryReport(path=path, source_schema_version=0, runtimes=(), profiles=(), findings=(finding,))

        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            return self._invalid(path, f"Cannot read execution configuration: {exc}")
        if not isinstance(raw, Mapping):
            return self._invalid(path, "agents.yaml must contain a mapping")
        try:
            source_schema_version = int(raw.get("schema_version", 1))
        except (TypeError, ValueError):
            return self._invalid(path, "agents schema_version must be an integer")

        registry = None
        registry_warning = ""
        opencode_dir = project.configured_path("opencode_dir")
        if opencode_dir is not None:
            try:
                registry = load_model_route_registry(opencode_dir / "providers.yaml")
            except ModelRegistryError as exc:
                # Registry validity is an independent readiness dimension.  A
                # schema-v4 topology is self-contained and must remain
                # inspectable even when a Native/OpenCode compatibility overlay
                # is broken.  ReadinessService decides whether that registry is
                # actually required by an enabled profile.
                registry_warning = f"Model endpoint registry is invalid: {exc}"

        try:
            execution, warnings = normalize_execution_for_operator(raw, model_registry=registry)
        except RuntimeConfigMigrationError as exc:
            return self._invalid(path, str(exc))
        if registry_warning:
            warnings = (*warnings, registry_warning)

        runtime_rows: list[RuntimeInventoryItem] = []
        runtime_ready: dict[str, bool] = {}
        findings: list[Finding] = []
        evidence: list[Evidence] = []
        for runtime in execution.runtimes:
            item = self._runtime_item(runtime)
            runtime_rows.append(item)
            runtime_ready[runtime.id] = item.ready
            evidence.append(
                Evidence(
                    id=f"runtime-{runtime.id}-local-prerequisite",
                    subject=f"runtime:{runtime.id}",
                    field="binary_path" if item.mode != OpenClawMode.EXTERNAL.value else "gateway",
                    value=item.binary_path if item.mode != OpenClawMode.EXTERNAL.value else item.gateway,
                    source="path-resolution" if item.mode != OpenClawMode.EXTERNAL.value else "configuration",
                    confidence=1.0,
                    rationale=(
                        "External Gateway connectivity is checked only by explicit diagnostics."
                        if item.mode == OpenClawMode.EXTERNAL.value
                        else (
                            f"Resolved executable {item.binary!r}."
                            if item.binary_path
                            else f"Executable {item.binary!r} was not found."
                        )
                    ),
                    location=str(path),
                )
            )
            if not item.ready:
                findings.append(
                    Finding(
                        code="execution.runtime_binary_missing",
                        severity=FindingSeverity.ERROR,
                        message=f"Runtime {runtime.id!r} requires missing executable {item.binary!r}",
                        subject=f"runtime:{runtime.id}",
                        remediation=(
                            "Install the runtime executable, correct its configured path, or disable profiles that depend on it."
                        ),
                        evidence_ids=(f"runtime-{runtime.id}-local-prerequisite",),
                    )
                )

        profile_rows, profile_findings = self._profile_rows(execution, runtime_ready)
        findings.extend(profile_findings)
        routes = self._route_rows(execution)
        targets = self._target_rows(execution)
        return ExecutionInventoryReport(
            path=path,
            source_schema_version=source_schema_version,
            runtimes=tuple(runtime_rows),
            profiles=tuple(profile_rows),
            model_routes=routes,
            execution_targets=targets,
            findings=tuple(findings),
            evidence=tuple(evidence),
            warnings=tuple(warnings),
        )


    @staticmethod
    def _profile_rows(
        execution: Any, runtime_ready: Mapping[str, bool]
    ) -> tuple[list[ExecutionProfileInventoryItem], list[Finding]]:
        rows: list[ExecutionProfileInventoryItem] = []
        findings: list[Finding] = []
        for profile in execution.agents:
            runtime = execution.runtime(profile.runtime_id)
            route = execution.model_route(profile.model_route_id) if profile.model_route_id else None
            target_id = profile.target_id or (route.default_target if route else "")
            support = profile_product_support(execution, profile)
            rows.append(
                ExecutionProfileInventoryItem(
                    id=profile.candidate_id,
                    name=profile.name or profile.candidate_id,
                    enabled=profile.enabled,
                    runtime_id=runtime.id,
                    runtime_kind=str(getattr(runtime.kind, "value", runtime.kind)),
                    model_route_id=profile.model_route_id,
                    target_id=target_id,
                    model=route.model if route else "",
                    capabilities=tuple(sorted(item.value for item in profile.capabilities)),
                    runtime_ready=runtime_ready.get(runtime.id, False),
                    support_status=support.status,
                    product_supported=support.supported,
                )
            )
            if profile.enabled and not support.supported:
                findings.append(
                    Finding(
                        code="execution.experimental_disabled",
                        severity=FindingSeverity.ERROR,
                        message=f"Execution profile {profile.id!r} selects disabled functionality",
                        subject=f"profile:{profile.id}",
                        remediation=support.reason,
                    )
                )
        if not any(item.enabled for item in rows):
            findings.append(
                Finding(
                    code="execution.none_enabled",
                    severity=FindingSeverity.WARNING,
                    message="No execution profile is enabled",
                    subject="execution",
                    remediation="Enable at least one execution profile before orchestration.",
                )
            )
        return rows, findings

    @staticmethod
    def _route_rows(execution: Any) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "id": route.id,
                "provider": route.provider,
                "model": route.model,
                "default_target": route.default_target,
                "credential_configured": bool(route.credential_ref.strip()),
            }
            for route in execution.model_routes
        )

    @staticmethod
    def _target_rows(execution: Any) -> tuple[dict[str, Any], ...]:
        rows = []
        for target in execution.targets:
            support = target_product_support(target)
            rows.append(
                {
                    "id": target.id,
                    "kind": target.kind.value,
                    "endpoint": target.endpoint,
                    "max_concurrency": target.max_concurrency,
                    "support_status": support.status,
                    "product_supported": support.supported,
                }
            )
        return tuple(rows)

    @staticmethod
    def _invalid(path: Path, message: str) -> ExecutionInventoryReport:
        finding = Finding(
            code="execution.configuration_invalid",
            severity=FindingSeverity.ERROR,
            message=message,
            subject="execution",
            remediation="Repair agents.yaml before orchestration.",
        )
        return ExecutionInventoryReport(path=path, source_schema_version=0, runtimes=(), profiles=(), findings=(finding,))

    @staticmethod
    def _runtime_item(runtime: Any) -> RuntimeInventoryItem:
        kind = str(getattr(runtime.kind, "value", runtime.kind))
        support = runtime_product_support(runtime)
        if runtime.kind == RuntimeKind.NATIVE:
            binary = str(runtime.binary or runtime.adapter)
            return RuntimeInventoryItem(
                id=runtime.id,
                kind=kind,
                adapter=runtime.adapter,
                binary=binary,
                binary_path=ExecutionInventory._resolve_binary(binary),
                support_status=support.status,
                product_supported=support.supported,
            )
        if runtime.kind != RuntimeKind.OPENCLAW:
            return RuntimeInventoryItem(
                id=runtime.id,
                kind=kind,
                adapter="",
                binary="",
                binary_path="",
                support_status=support.status,
                product_supported=support.supported,
            )
        options = runtime.openclaw
        if options is None:
            return RuntimeInventoryItem(
                id=runtime.id,
                kind=kind,
                adapter="",
                binary="openclaw",
                binary_path="",
                support_status=support.status,
                product_supported=support.supported,
            )
        executable = str(options.executable or "openclaw")
        external = options.mode == OpenClawMode.EXTERNAL
        return RuntimeInventoryItem(
            id=runtime.id,
            kind=kind,
            adapter="",
            binary=executable,
            binary_path="" if external else ExecutionInventory._resolve_binary(executable),
            mode=options.mode.value,
            gateway=options.gateway,
            authentication_configured=bool(options.auth_ref.strip()),
            support_status=support.status,
            product_supported=support.supported,
        )

    @staticmethod
    def _resolve_binary(binary: str) -> str:
        if not binary:
            return ""
        candidate = Path(binary).expanduser()
        if candidate.parent != Path(".") or os.sep in binary:
            resolved = candidate.resolve()
            return str(resolved) if resolved.is_file() and os.access(resolved, os.X_OK) else ""
        return shutil.which(binary) or ""


__all__ = [
    "ExecutionInventory",
    "ExecutionInventoryReport",
    "ExecutionProfileInventoryItem",
    "RuntimeInventoryItem",
]
