"""Local provider inventory used by onboarding readiness checks."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from execraft.agents.config import AgentConfigError, AgentProviderConfig, parse_agent_configs
from execraft.onboarding.models import Evidence, Finding, FindingSeverity
from execraft.project import ProjectDescriptor


@dataclass(frozen=True)
class ProviderInventoryItem:
    name: str
    provider_id: str
    adapter: str
    enabled: bool
    binary: str
    binary_path: str
    model: str
    capabilities: tuple[str, ...]
    priority: int

    @property
    def binary_available(self) -> bool:
        return bool(self.binary_path)

    @property
    def ready(self) -> bool:
        return self.enabled and self.binary_available

    def as_mapping(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider_id": self.provider_id,
            "adapter": self.adapter,
            "enabled": self.enabled,
            "binary": self.binary,
            "binary_path": self.binary_path,
            "binary_available": self.binary_available,
            "ready": self.ready,
            "model": self.model,
            "capabilities": list(self.capabilities),
            "priority": self.priority,
        }


@dataclass(frozen=True)
class ProviderInventoryReport:
    path: Path | None
    providers: tuple[ProviderInventoryItem, ...]
    findings: tuple[Finding, ...] = ()
    evidence: tuple[Evidence, ...] = ()

    @property
    def enabled(self) -> tuple[ProviderInventoryItem, ...]:
        return tuple(item for item in self.providers if item.enabled)

    @property
    def ready(self) -> tuple[ProviderInventoryItem, ...]:
        return tuple(item for item in self.providers if item.ready)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path is not None else "",
            "configured": len(self.providers),
            "enabled": len(self.enabled),
            "ready": len(self.ready),
            "providers": [item.as_mapping() for item in self.providers],
            "findings": [item.as_mapping() for item in self.findings],
            "evidence": [item.as_mapping() for item in self.evidence],
        }


class ProviderInventory:
    """Inspect provider declarations and local executable availability.

    Authentication is deliberately not probed here because doing so can execute
    provider-specific code or make network calls.  The inventory reports the
    deterministic local prerequisites; explicit agent diagnostics remain the
    live/authenticated check.
    """

    def inspect(self, project: ProjectDescriptor) -> ProviderInventoryReport:
        path = project.configured_path("agents_file")
        if path is None or not path.is_file():
            finding = Finding(
                code="providers.configuration_missing",
                severity=FindingSeverity.ERROR,
                message="Project has no readable agents configuration",
                subject="providers",
                remediation="Configure agents_file and at least one provider.",
            )
            return ProviderInventoryReport(path=path, providers=(), findings=(finding,))

        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            finding = Finding(
                code="providers.configuration_invalid",
                severity=FindingSeverity.ERROR,
                message=f"Cannot read provider configuration: {exc}",
                subject="providers",
                remediation="Repair agents.yaml before orchestration.",
            )
            return ProviderInventoryReport(path=path, providers=(), findings=(finding,))
        if not isinstance(raw, Mapping):
            finding = Finding(
                code="providers.configuration_invalid",
                severity=FindingSeverity.ERROR,
                message="agents.yaml must contain a mapping",
                subject="providers",
                remediation="Repair agents.yaml before orchestration.",
            )
            return ProviderInventoryReport(path=path, providers=(), findings=(finding,))

        try:
            configs = parse_agent_configs(raw, include_disabled=True)
        except AgentConfigError as exc:
            finding = Finding(
                code="providers.configuration_invalid",
                severity=FindingSeverity.ERROR,
                message=str(exc),
                subject="providers",
                remediation="Repair agents.yaml before orchestration.",
            )
            return ProviderInventoryReport(path=path, providers=(), findings=(finding,))

        providers: list[ProviderInventoryItem] = []
        findings: list[Finding] = []
        evidence: list[Evidence] = []
        for config in configs:
            binary_path = self._resolve_binary(config)
            item = ProviderInventoryItem(
                name=config.name,
                provider_id=config.provider_id,
                adapter=config.adapter,
                enabled=config.enabled,
                binary=config.binary,
                binary_path=binary_path,
                model=config.model,
                capabilities=tuple(sorted(item.value for item in config.capabilities)),
                priority=config.priority,
            )
            providers.append(item)
            item_evidence = Evidence(
                id=f"provider-{config.name}-binary",
                subject=f"provider:{config.name}",
                field="binary_path",
                value=binary_path,
                source="path-resolution",
                confidence=1.0,
                rationale=(
                    f"Resolved executable {config.binary!r} from the current environment."
                    if binary_path
                    else f"Executable {config.binary!r} was not found in PATH."
                ),
                location=str(path),
            )
            evidence.append(item_evidence)
            if config.enabled and not binary_path:
                findings.append(
                    Finding(
                        code="providers.binary_missing",
                        severity=FindingSeverity.ERROR,
                        message=(
                            f"Enabled provider {config.name!r} requires missing executable "
                            f"{config.binary!r}"
                        ),
                        subject=f"provider:{config.name}",
                        remediation="Install the provider CLI or disable the provider.",
                        evidence_ids=(item_evidence.id,),
                    )
                )

        if not any(item.enabled for item in providers):
            findings.append(
                Finding(
                    code="providers.none_enabled",
                    severity=FindingSeverity.WARNING,
                    message="No provider is enabled",
                    subject="providers",
                    remediation=(
                        "Enable and authenticate at least one provider before "
                        "orchestration."
                    ),
                )
            )
        return ProviderInventoryReport(
            path=path,
            providers=tuple(providers),
            findings=tuple(findings),
            evidence=tuple(evidence),
        )

    @staticmethod
    def _resolve_binary(config: AgentProviderConfig) -> str:
        candidate = Path(config.binary).expanduser()
        if candidate.parent != Path(".") or os.sep in config.binary:
            resolved = candidate.resolve()
            return str(resolved) if resolved.is_file() and os.access(resolved, os.X_OK) else ""
        return shutil.which(config.binary) or ""
