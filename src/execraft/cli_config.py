"""Shared CLI configuration loading and orchestration policy interpretation.

This module owns configuration semantics only. CLI commands remain responsible
for discovering workspaces and composing runtime objects, while domain policy
classes retain validation of their own structures.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from execraft.agents import (
    OpenCodeProviderRegistry,
    OpenCodeRegistryError,
    load_opencode_provider_registry,
)
from execraft.completion import task_completion_policy_from_scheduling
from execraft.orchestrate import (
    AgentCapability,
    InteractiveTerminalPolicy,
    OrchestrationConfig,
    RecoveryPlaybookConfigError,
    ResourcePolicy,
    ScopePolicy,
    SupervisorConfigError,
    SupervisorPolicy,
    budgets_from_mapping,
    recovery_playbook_policy_from_scheduling,
    scope_recovery_policy_from_scheduling,
    workspace_finalization_policy_from_scheduling,
)
from execraft.project import ProjectDescriptor
from execraft.repository_sync.policy import RepositorySyncPolicy
from execraft.workspace.task_git import TaskGitError


def project_config_path(
    root: Path, project: ProjectDescriptor, key: str
) -> Path | None:
    """Resolve one project configuration path.

    ``root`` remains accepted because existing command call sites carry it and
    preserving that shape avoids mixing API churn into configuration cleanup.
    """

    del root
    return project.configured_path(key)


def load_yaml_mapping(path: Path | None, *, label: str) -> dict[str, Any]:
    """Load one required YAML mapping with CLI-friendly validation errors."""

    if path is None or not path.is_file():
        raise TaskGitError(f"{label} is not configured or missing: {path or '<unset>'}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise TaskGitError(f"{label} must contain a YAML mapping: {path}")
    return data


def load_project_opencode_registry(
    root: Path, project: ProjectDescriptor
) -> OpenCodeProviderRegistry:
    """Load the legacy OpenCode registry for Native compatibility callers."""

    del root
    opencode_dir = project.configured_path("opencode_dir") or project.directory / "opencode"
    try:
        return load_opencode_provider_registry(opencode_dir / "providers.yaml")
    except OpenCodeRegistryError as exc:
        raise TaskGitError(str(exc)) from exc


def load_resource_policy(path: Path | None) -> ResourcePolicy:
    """Load the optional project resource policy."""

    if path is None or not path.is_file():
        return ResourcePolicy()
    raw = load_yaml_mapping(path, label="resource policy")
    policy = raw.get("policy", raw)
    if not isinstance(policy, dict):
        raise TaskGitError("resource policy must be a mapping")
    return ResourcePolicy.from_mapping(policy)


def apply_commit_policy(config: OrchestrationConfig, raw_config: Mapping[str, Any]) -> None:
    """Apply the deterministic commit policy declared in ``agents.yaml``."""

    commit_policy = raw_config.get("commit", {})
    if not commit_policy:
        return
    if not isinstance(commit_policy, dict):
        raise TaskGitError("orchestration commit policy must be a mapping")

    mode_value = commit_policy.get("mode", "automatic")
    if not isinstance(mode_value, str) or not mode_value.strip():
        raise TaskGitError("orchestration commit mode must be a non-empty string")
    mode = mode_value.strip().lower()
    if mode != "automatic":
        raise TaskGitError(
            f"unsupported orchestration commit mode: {mode_value!r}; "
            "supported modes: automatic"
        )

    allow_noop = commit_policy.get("allow_verified_noop", False)
    if not isinstance(allow_noop, bool):
        raise TaskGitError("orchestration commit allow_verified_noop must be a boolean")

    config.auto_commit = True
    config.allow_verified_noop_commits = allow_noop


def build_orchestration_config(
    agents_mapping: Mapping[str, Any],
    *,
    state_dir: Path | None = None,
    heartbeat_interval: float = 0.0,
) -> OrchestrationConfig:
    """Interpret project agent/scheduling YAML into one orchestration config."""

    config = OrchestrationConfig()
    if agents_mapping:
        apply_commit_policy(config, agents_mapping)
        try:
            config.supervisor_policy = SupervisorPolicy.from_mapping(
                agents_mapping.get("supervisor")
            )
        except SupervisorConfigError as exc:
            raise TaskGitError(f"invalid supervisor policy: {exc}") from exc

    scheduling = agents_mapping.get("scheduling", {}) if agents_mapping else {}
    if scheduling and not isinstance(scheduling, dict):
        raise TaskGitError("agent scheduling policy must be a mapping")
    if isinstance(scheduling, dict):
        _apply_scheduling_policy(config, scheduling)

    if state_dir is not None:
        config.state_dir = state_dir.expanduser().resolve()
    if heartbeat_interval < 0:
        raise TaskGitError("--heartbeat-interval must be zero or positive")
    config.agent_heartbeat_interval_seconds = float(heartbeat_interval)
    return config


def _apply_scheduling_policy(
    config: OrchestrationConfig, scheduling: Mapping[str, Any]
) -> None:
    allow_same_review = scheduling.get(
        "allow_same_provider_review", config.allow_same_provider_review
    )
    if not isinstance(allow_same_review, bool):
        raise TaskGitError("agent scheduling allow_same_provider_review must be a boolean")
    config.allow_same_provider_review = allow_same_review

    token_budgets = scheduling.get("token_budgets")
    if token_budgets is not None:
        if not isinstance(token_budgets, Mapping):
            raise TaskGitError("agent scheduling token_budgets must be a mapping")
        try:
            config.token_budgets = budgets_from_mapping(token_budgets)
        except ValueError as exc:
            raise TaskGitError(f"invalid agent scheduling token_budgets: {exc}") from exc

    config.rotate_agents = bool(scheduling.get("rotate_agents", config.rotate_agents))
    config.allow_cross_package_progress = bool(
        scheduling.get(
            "allow_cross_package_progress", config.allow_cross_package_progress
        )
    )
    config.prefer_capable_agents = bool(
        scheduling.get("prefer_capable_agents", config.prefer_capable_agents)
    )
    _apply_read_only_level(config, scheduling)
    _apply_positive_float_settings(config, scheduling)
    _apply_complexity_settings(config, scheduling)

    repository_sync = scheduling.get("repository_sync")
    if repository_sync is not None:
        try:
            config.repository_sync_policy = RepositorySyncPolicy.from_mapping(repository_sync)
        except ValueError as exc:
            raise TaskGitError(f"invalid agent scheduling repository_sync: {exc}") from exc

    _apply_decomposition_policy(config, scheduling.get("decomposition", {}))
    _apply_parallel_shard_policy(config, scheduling.get("parallel_shards", {}))
    _apply_domain_policies(config, scheduling)


def _apply_read_only_level(
    config: OrchestrationConfig, scheduling: Mapping[str, Any]
) -> None:
    if "minimum_read_only_enforcement" not in scheduling:
        return
    level = str(scheduling["minimum_read_only_enforcement"]).strip()
    if level not in {"advisory", "provider_policy", "hard"}:
        raise TaskGitError(
            "agent scheduling minimum_read_only_enforcement must be "
            "advisory, provider_policy, or hard"
        )
    config.minimum_read_only_enforcement = level


def _apply_positive_float_settings(
    config: OrchestrationConfig, scheduling: Mapping[str, Any]
) -> None:
    for key in (
        "agent_wait_poll_max_seconds",
        "agent_known_deadline_poll_max_seconds",
        "blocking_agent_wait_max_seconds",
    ):
        if key not in scheduling:
            continue
        try:
            value = float(scheduling[key])
        except (TypeError, ValueError) as exc:
            raise TaskGitError(f"agent scheduling {key} must be numeric") from exc
        if value <= 0:
            raise TaskGitError(f"agent scheduling {key} must be positive")
        setattr(config, key, value)


def _apply_complexity_settings(
    config: OrchestrationConfig, scheduling: Mapping[str, Any]
) -> None:
    for key in (
        "complexity_medium_threshold",
        "complexity_high_threshold",
        "capability_weight_band",
    ):
        if key not in scheduling:
            continue
        try:
            value = int(scheduling[key])
        except (TypeError, ValueError) as exc:
            raise TaskGitError(f"agent scheduling {key} must be an integer") from exc
        if not 0 <= value <= 100:
            raise TaskGitError(f"agent scheduling {key} must be between 0 and 100")
        setattr(config, key, value)
    if config.complexity_medium_threshold > config.complexity_high_threshold:
        raise TaskGitError(
            "agent scheduling complexity_medium_threshold cannot exceed "
            "complexity_high_threshold"
        )


def _apply_decomposition_policy(config: OrchestrationConfig, raw: Any) -> None:
    if raw and not isinstance(raw, dict):
        raise TaskGitError("agent scheduling decomposition must be a mapping")
    if not isinstance(raw, dict):
        return

    config.auto_decompose_enabled = bool(raw.get("enabled", config.auto_decompose_enabled))
    config.decompose_when_no_eligible_provider = bool(
        raw.get(
            "trigger_when_no_eligible_provider",
            config.decompose_when_no_eligible_provider,
        )
    )
    for source_key, target_key in (
        ("complexity_threshold", "decompose_complexity_threshold"),
        ("target_shard_complexity", "decompose_target_shard_complexity"),
        ("maximum_shard_complexity", "decompose_maximum_shard_complexity"),
        ("maximum_shards", "decompose_maximum_shards"),
    ):
        if source_key not in raw:
            continue
        try:
            value = int(raw[source_key])
        except (TypeError, ValueError) as exc:
            raise TaskGitError(
                f"agent scheduling decomposition.{source_key} must be an integer"
            ) from exc
        if source_key == "maximum_shards":
            if not 2 <= value <= 32:
                raise TaskGitError(
                    "agent scheduling decomposition.maximum_shards must be between 2 and 32"
                )
        elif not 1 <= value <= 100:
            raise TaskGitError(
                f"agent scheduling decomposition.{source_key} must be between 1 and 100"
            )
        setattr(config, target_key, value)
    if config.decompose_target_shard_complexity > config.decompose_maximum_shard_complexity:
        raise TaskGitError(
            "agent scheduling decomposition.target_shard_complexity cannot exceed "
            "maximum_shard_complexity"
        )


def _apply_parallel_shard_policy(config: OrchestrationConfig, raw: Any) -> None:
    if raw and not isinstance(raw, dict):
        raise TaskGitError("agent scheduling parallel_shards must be a mapping")
    if not isinstance(raw, dict):
        return

    config.parallel_shards_enabled = bool(raw.get("enabled", config.parallel_shards_enabled))
    config.parallel_require_disjoint_repositories_for_writes = bool(
        raw.get(
            "require_disjoint_repositories_for_writes",
            config.parallel_require_disjoint_repositories_for_writes,
        )
    )
    if "max_workers" in raw:
        try:
            workers = int(raw["max_workers"])
        except (TypeError, ValueError) as exc:
            raise TaskGitError(
                "agent scheduling parallel_shards.max_workers must be an integer"
            ) from exc
        if not 1 <= workers <= 16:
            raise TaskGitError(
                "agent scheduling parallel_shards.max_workers must be between 1 and 16"
            )
        config.parallel_shard_max_workers = workers

    if "stages" not in raw:
        return
    stages_raw = raw["stages"]
    if not isinstance(stages_raw, list) or not stages_raw:
        raise TaskGitError(
            "agent scheduling parallel_shards.stages must be a non-empty list"
        )
    allowed = {
        AgentCapability.IMPLEMENT,
        AgentCapability.REVIEW,
        AgentCapability.FIX_REVIEW,
    }
    stages: list[str] = []
    for item in stages_raw:
        try:
            capability = AgentCapability(str(item))
        except ValueError as exc:
            raise TaskGitError(f"unsupported parallel shard stage: {item!r}") from exc
        if capability not in allowed:
            raise TaskGitError(
                f"parallel shard stage is not supported: {capability.value}"
            )
        if capability.value not in stages:
            stages.append(capability.value)
    config.parallel_shard_stages = tuple(stages)


def _apply_domain_policies(
    config: OrchestrationConfig, scheduling: Mapping[str, Any]
) -> None:
    scope_policy = scheduling.get("scope_policy", {})
    if scope_policy and not isinstance(scope_policy, dict):
        raise TaskGitError("agent scheduling scope_policy must be a mapping")
    try:
        config.scope_policy = ScopePolicy.from_mapping(scope_policy)
    except ValueError as exc:
        raise TaskGitError(f"invalid agent scheduling scope_policy: {exc}") from exc

    try:
        config.scope_recovery_policy = scope_recovery_policy_from_scheduling(scheduling)
    except ValueError as exc:
        raise TaskGitError(f"invalid agent scheduling recovery policy: {exc}") from exc
    try:
        config.workspace_finalization_policy = workspace_finalization_policy_from_scheduling(
            scheduling
        )
    except ValueError as exc:
        raise TaskGitError(
            f"invalid agent scheduling workspace finalization policy: {exc}"
        ) from exc
    try:
        config.task_completion_policy = task_completion_policy_from_scheduling(scheduling)
    except ValueError as exc:
        raise TaskGitError(f"invalid agent scheduling task completion policy: {exc}") from exc
    try:
        config.recovery_playbook_policy = recovery_playbook_policy_from_scheduling(scheduling)
    except RecoveryPlaybookConfigError as exc:
        raise TaskGitError(f"invalid agent scheduling recovery playbooks: {exc}") from exc

    interactive_console = scheduling.get("interactive_console", {})
    if interactive_console and not isinstance(interactive_console, dict):
        raise TaskGitError("agent scheduling interactive_console must be a mapping")
    try:
        config.interactive_terminal_policy = InteractiveTerminalPolicy.from_mapping(
            interactive_console
        )
    except ValueError as exc:
        raise TaskGitError(f"invalid agent scheduling interactive_console: {exc}") from exc
