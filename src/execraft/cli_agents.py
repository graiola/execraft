"""Implementation of the ``execraft agents`` operator command.

The top-level CLI remains a dispatcher/composition boundary.  This module owns
Native compatibility health/diagnostic presentation without changing the
underlying provider-health or promotion contracts.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from execraft.agents import (
    AgentConfigError,
    AgentProviderConfig,
    diagnose_agents,
    project_native_agent_configs,
)
from execraft.agents.provider_promotion_cli import (
    PROMOTION_ACTIONS,
    run_provider_promotion_action,
)
from execraft.cli_config import (
    load_project_opencode_registry,
    load_yaml_mapping,
    project_config_path,
)
from execraft.control_plane import xdg_state_home
from execraft.orchestrate.provider_health import ProviderHealthStore
from execraft.project import resolve_current_project
from execraft.workspace.task_git import TaskGitError, repository_root


def run_agents_command(args: argparse.Namespace) -> int:
    """Execute one ``agents`` subcommand using current compatibility contracts."""

    root = repository_root()
    project = resolve_current_project(root, project_id=args.project_id)
    args.project_id = project.id
    agents_path = project_config_path(root, project, "agents_file")
    mapping = load_yaml_mapping(agents_path, label="agent registry")
    try:
        all_configs, runtime_note = project_native_agent_configs(mapping)
    except AgentConfigError as exc:
        raise TaskGitError(str(exc)) from exc
    if not all_configs:
        raise TaskGitError(f"no configured Native agents in {agents_path}")
    if runtime_note:
        print(runtime_note, file=sys.stderr)

    state_root = (
        args.state_dir.expanduser().resolve() if args.state_dir else xdg_state_home()
    )
    health_store = ProviderHealthStore(state_root / "provider-health.json")

    if args.action in PROMOTION_ACTIONS:
        selected = filter_agent_configs(all_configs, args.agent_id)
        return run_provider_promotion_action(
            args, project_id=project.id, configs=selected, state_root=state_root
        )
    if args.action == "set-cooldown":
        return _set_cooldown(args, all_configs, health_store)
    if args.action == "reset-health":
        return _reset_health(args, all_configs, health_store)

    selected_configs = filter_agent_configs(all_configs, args.agent_id)
    opencode_registry = load_project_opencode_registry(root, project)
    if args.action == "status":
        return _render_status(args, selected_configs, health_store, opencode_registry)
    return _diagnose(
        args, selected_configs, health_store, opencode_registry, root, agents_path
    )


def parse_provider_cooldown_deadline(value: str) -> datetime:
    """Parse the retained provider-health cooldown timestamp contract."""

    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        deadline = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise TaskGitError(
            "invalid --until timestamp; use timezone-aware ISO 8601, for example "
            "2026-07-25T17:00:00+02:00"
        ) from exc
    if deadline.tzinfo is None:
        raise TaskGitError("--until must include an explicit timezone offset")
    deadline = deadline.astimezone(timezone.utc)
    if deadline <= datetime.now(timezone.utc):
        raise TaskGitError("--until must be in the future")
    return deadline


def filter_agent_configs(
    configs: list[AgentProviderConfig], agent_id: str | None
) -> list[AgentProviderConfig]:
    """Return configs matching one provider ID, instance name, or alias."""

    if not agent_id:
        return configs
    matches = [
        config
        for config in configs
        if agent_id in {config.provider_id, config.name, *config.aliases}
    ]
    if not matches:
        raise TaskGitError(f"unknown configured provider or alias: {agent_id}")
    return matches


def _configured_ids(configs: list[AgentProviderConfig]) -> set[str]:
    return {item.provider_id for item in configs}


def _set_cooldown(
    args: argparse.Namespace,
    configs: list[AgentProviderConfig],
    health_store: ProviderHealthStore,
) -> int:
    if not args.agent_id:
        raise TaskGitError("agents set-cooldown requires --agent PROVIDER_ID")
    if not args.until:
        raise TaskGitError("agents set-cooldown requires --until ISO_TIMESTAMP")
    if args.agent_id not in _configured_ids(configs):
        raise TaskGitError(f"unknown configured provider: {args.agent_id}")
    deadline = parse_provider_cooldown_deadline(args.until)
    health = health_store.set_cooldown(
        args.agent_id,
        unavailable_until=deadline,
        reason=args.reason,
        detail=args.detail,
    )
    local_deadline = datetime.fromisoformat(health.unavailable_until).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S%z"
    )
    print(
        f"Execution-agent cooldown set: {args.agent_id} "
        f"until_local={local_deadline} reason={health.reason}"
    )
    return 0


def _reset_health(
    args: argparse.Namespace,
    configs: list[AgentProviderConfig],
    health_store: ProviderHealthStore,
) -> int:
    if not args.agent_id:
        raise TaskGitError("agents reset-health requires --agent PROVIDER_ID")
    if args.agent_id not in _configured_ids(configs):
        raise TaskGitError(f"unknown configured provider: {args.agent_id}")
    health_store.reset(args.agent_id)
    print(f"Reset execution-agent health: {args.agent_id}")
    print("This does not restore quota or credentials; use only after resolving the cause.")
    return 0


def _status_rows(configs, health_store, opencode_registry) -> list[dict]:
    model_registry = opencode_registry.model_registry
    rows = []
    for config in configs:
        health = health_store.get(config.provider_id)
        endpoint = model_registry.endpoint_for_model(config.model)
        route = model_registry.route_for_model(config.model)
        rows.append(
            {
                "provider_id": config.provider_id,
                "adapter": config.adapter,
                "model": config.model,
                "endpoint_id": endpoint.endpoint_id if endpoint else "",
                "endpoint_url": endpoint.base_url if endpoint else "",
                "target_id": endpoint.target_id if endpoint else "",
                "target_kind": endpoint.target_kind.value if endpoint else "",
                "provider_family": route.provider if route else "",
                "provider_alias": route.provider_alias if route else "",
                "api_family": route.api_family if route else "",
                "target_concurrency_group": endpoint.concurrency_group if endpoint else "",
                "enabled": config.enabled,
                "concurrency_group": config.concurrency_group,
                "timeout_seconds": config.timeout_seconds,
                "inactivity_timeout_seconds": config.inactivity_timeout_seconds,
                "first_output_timeout_seconds": config.first_output_timeout_seconds,
                "output_silence_timeout_seconds": config.output_silence_timeout_seconds,
                "status": health.status,
                "reason": health.reason,
                "unavailable_until": health.unavailable_until,
                "consecutive_failures": health.consecutive_failures,
                "capability_weight": config.capability_weight,
                "capability_weights": {
                    capability.value: config.weight_for_capability(capability)
                    for capability in sorted(config.capabilities, key=lambda item: item.value)
                },
                "max_complexity": config.max_complexity,
                "max_complexity_by_capability": {
                    capability.value: config.max_complexity_for(capability)
                    for capability in sorted(config.capabilities, key=lambda item: item.value)
                },
            }
        )
    return rows


def _render_status(args, configs, health_store, opencode_registry) -> int:
    rows = _status_rows(configs, health_store, opencode_registry)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            _print_status_row(row)
    return 0 if all(row["status"] == "available" for row in rows) else 1


def _print_status_row(row: dict) -> None:
    print(row["provider_id"])
    print(f"  adapter: {row['adapter']}")
    print(f"  enabled: {'yes' if row['enabled'] else 'no'}")
    if row["model"]:
        print(f"  model: {row['model']}")
    if row["endpoint_id"]:
        print(f"  endpoint: {row['endpoint_id']} ({row['endpoint_url']})")
        print(
            f"  target: {row['target_id']} ({row['target_kind']}, "
            f"provider={row['provider_family']}, alias={row['provider_alias']})"
        )
    print(f"  concurrency group: {row['concurrency_group']}")
    print(f"  total timeout: {row['timeout_seconds']}s")
    print(f"  inactivity timeout: {row['inactivity_timeout_seconds']}s")
    first_output = row["first_output_timeout_seconds"]
    print(f"  first output timeout: {f'{first_output}s' if first_output else 'disabled'}")
    silence = row["output_silence_timeout_seconds"]
    print(f"  output silence timeout: {f'{silence}s' if silence else 'disabled'}")
    print(f"  capability weight: {row['capability_weight']}")
    print(
        "  capability weights: "
        + ", ".join(
            f"{capability}={weight}"
            for capability, weight in row["capability_weights"].items()
        )
    )
    print(
        "  max complexity: "
        + ", ".join(
            f"{capability}={maximum}"
            for capability, maximum in row["max_complexity_by_capability"].items()
        )
    )
    print(f"  status: {row['status']}")
    if row["reason"]:
        print(f"  reason: {row['reason']}")
    if row["unavailable_until"]:
        print(f"  available again: {row['unavailable_until']}")


def _diagnose(args, selected_configs, health_store, opencode_registry, root, agents_path) -> int:
    configs = (
        selected_configs
        if args.agent_id
        else [item for item in selected_configs if item.enabled]
    )
    if not configs:
        raise TaskGitError(f"no enabled agents in {agents_path}")
    if args.timeout_seconds <= 0:
        raise TaskGitError("--timeout-seconds must be positive")
    diagnostics = diagnose_agents(
        configs,
        refresh_models=args.refresh_models,
        smoke_test=args.smoke_test,
        timeout_seconds=args.timeout_seconds,
        health_store=health_store,
        opencode_registry=opencode_registry,
        workdir=root,
    )
    if args.json:
        print(json.dumps([item.as_mapping() for item in diagnostics], indent=2))
    else:
        for item in diagnostics:
            _print_diagnostic(item)
    return 0 if all(item.healthy for item in diagnostics) else 1


def _print_diagnostic(item) -> None:
    print(f"{item.provider_id}")
    print(f"  adapter: {item.adapter}")
    print(f"  binary: {'available' if item.binary_available else 'missing'} ({item.binary})")
    if item.model:
        model_state = (
            "available"
            if item.model_available is True
            else "missing"
            if item.model_available is False
            else "not verified"
        )
        print(f"  model: {item.model} ({model_state})")
    if item.endpoint_id:
        endpoint_state = (
            "reachable"
            if item.endpoint_reachable is True
            else "unreachable"
            if item.endpoint_reachable is False
            else "not probed"
        )
        print(
            f"  endpoint: {item.endpoint_id} ({endpoint_state}, "
            f"{item.endpoint_url}, {item.endpoint_url_source})"
        )
        print(
            f"  target: {item.target_id} ({item.target_kind}, "
            f"provider={item.provider_family}, alias={item.provider_alias})"
        )
    print(f"  execution-agent health: {item.health_status}")
    if item.health_reason:
        print(f"  health reason: {item.health_reason}")
    if item.unavailable_until:
        print(f"  available again: {item.unavailable_until}")
    print(f"  smoke test: {item.smoke_test}")
    for detail in item.details:
        print(f"  detail: {detail}")
