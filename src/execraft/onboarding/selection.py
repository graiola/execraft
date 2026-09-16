"""Repository and planning-provider selection for one-command onboarding."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

import yaml

from execraft.agents import AgentConfigError, parse_agent_configs
from execraft.runtime.native import build_native_runtime
from execraft.agents.config import AgentProviderConfig
from execraft.onboarding.models import Evidence
from execraft.onboarding.providers import ProviderInventory, ProviderInventoryItem
from execraft.onboarding.start_models import (
    ProviderChoice,
    RepositoryScope,
    StartWorkflowError,
)
from execraft.orchestrate.scheduler import AgentCapability, Availability
from execraft.project import ProjectDescriptor


class RepositorySelector:
    """Infer a bounded repository scope from task intent and catalog metadata."""

    _TOKEN = re.compile(r"[a-zA-Z0-9]+")

    def select(
        self,
        project: ProjectDescriptor,
        description: str,
        explicit: Sequence[str] = (),
    ) -> RepositoryScope:
        known = {item.id: item for item in project.repositories}
        requested = tuple(dict.fromkeys(item.strip() for item in explicit if item.strip()))
        if requested:
            unknown = sorted(set(requested) - set(known))
            if unknown:
                raise StartWorkflowError(
                    "unknown project repositories: " + ", ".join(unknown)
                )
            selected = set(requested)
            selected.update(item.id for item in project.repositories if item.required)
            evidence = Evidence(
                id="repository-scope-explicit",
                subject="task",
                field="repository_scope",
                value=sorted(selected),
                source="operator",
                confidence=1.0,
                rationale="Explicit repositories plus required project repositories.",
            )
            return RepositoryScope(
                repository_ids=tuple(
                    item.id for item in project.repositories if item.id in selected
                ),
                evidence=(evidence,),
                explicit=True,
            )

        tokens = {item.lower() for item in self._TOKEN.findall(description)}
        scores: dict[str, int] = {}
        evidence: list[Evidence] = []
        for repository in project.repositories:
            haystack = " ".join(
                (
                    repository.id,
                    repository.path,
                    repository.workspace_name,
                    repository.role,
                )
            ).lower()
            repository_tokens = set(self._TOKEN.findall(haystack))
            score = len(tokens & repository_tokens) * 10
            if repository.required:
                score += 100
            if repository.role in {"integration", "deployment"}:
                score += 5
            scores[repository.id] = score
            evidence.append(
                Evidence(
                    id=f"repository-scope-{repository.id}",
                    subject=f"repository:{repository.id}",
                    field="scope_score",
                    value=score,
                    source="intent-token-match",
                    confidence=1.0 if repository.required else min(0.9, score / 20),
                    rationale=(
                        "Repository is required by the project descriptor."
                        if repository.required
                        else "Score derived from task tokens and repository metadata."
                    ),
                )
            )
        selected = [
            item.id
            for item in project.repositories
            if scores[item.id] > 0
        ]
        if not selected:
            selected = [item.id for item in project.repositories]
        return RepositoryScope(
            repository_ids=tuple(selected),
            evidence=tuple(evidence),
        )


class ProviderSelector:
    """Select the strongest enabled provider with safe read-only enforcement."""

    def __init__(self, inventory: ProviderInventory | None = None) -> None:
        self.inventory = inventory or ProviderInventory()

    def select(
        self,
        project: ProjectDescriptor,
        *,
        workdir: Path,
        requested: str = "",
    ) -> ProviderChoice:
        report = self.inventory.inspect(project)
        path = project.configured_path("agents_file")
        if path is None or not path.is_file():
            return ProviderChoice(None, None, "Provider configuration is unavailable")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        try:
            configs = parse_agent_configs(raw, read_only=True, include_disabled=True)
        except AgentConfigError as exc:
            return ProviderChoice(None, None, f"Invalid provider configuration: {exc}")
        inventory_by_name = {item.name: item for item in report.providers}
        ranked: list[
            tuple[tuple[int, int, int, str], ProviderInventoryItem, AgentProviderConfig]
        ] = []
        rejected_requested = ""
        for config in configs:
            inventory_item = inventory_by_name.get(config.name)
            if inventory_item is None:
                continue
            aliases = {config.name, config.provider_id, *config.aliases}
            if requested and requested not in aliases:
                continue
            if not config.enabled:
                if requested:
                    rejected_requested = f"Requested provider {requested!r} is disabled"
                continue
            if not inventory_item.binary_available:
                if requested:
                    rejected_requested = (
                        f"Requested provider {requested!r} executable {config.binary!r} is missing"
                    )
                continue
            capabilities = set(config.capabilities)
            planning_capability = (
                AgentCapability.PLAN
                if AgentCapability.PLAN in capabilities
                else AgentCapability.DECOMPOSE
                if AgentCapability.DECOMPOSE in capabilities
                else None
            )
            if planning_capability is None:
                if requested:
                    rejected_requested = f"Requested provider {requested!r} cannot plan"
                continue
            adapter = build_native_runtime(config, workdir=workdir, read_only=True)
            execution = adapter.execution_capabilities
            if not execution.satisfies_read_only("provider_policy"):
                if requested:
                    rejected_requested = (
                        f"Requested provider {requested!r} does not enforce read-only policy"
                    )
                continue
            if adapter.availability is not Availability.AVAILABLE:
                if requested:
                    rejected_requested = (
                        f"Requested provider {requested!r} is {adapter.availability.value}"
                    )
                continue
            isolation = 2 if execution.read_only_enforcement == "hard" else 1
            weight = config.weight_for_capability(planning_capability)
            ranked.append(
                ((isolation, weight, config.priority, config.name), inventory_item, config)
            )
        ranked.sort(key=lambda item: item[0], reverse=True)
        candidates = tuple(item[1] for item in ranked)
        if not ranked:
            reason = rejected_requested or (
                "No enabled local provider combines planning capability with "
                "provider-policy or hard read-only enforcement"
            )
            return ProviderChoice(None, None, reason, candidates=())
        _, provider, config = ranked[0]
        return ProviderChoice(
            provider=provider,
            config=config,
            reason=(
                "Selected by read-only enforcement, planning capability weight, "
                "and configured priority"
            ),
            candidates=candidates,
        )



__all__ = ["ProviderSelector", "RepositorySelector"]
