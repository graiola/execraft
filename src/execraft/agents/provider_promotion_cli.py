"""CLI application service for temporary provider promotion operations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from execraft.agents.config import AgentProviderConfig
from execraft.orchestrate.identity import resolve_storage_identity
from execraft.orchestrate.journal import EventJournal
from execraft.orchestrate.provider_promotion import (
    ProviderPromotionStore,
    parse_promotion_duration,
)
from execraft.workspace.task_git import TaskGitError


PROMOTION_ACTIONS = frozenset({"promote", "revoke-promotion", "promotions"})


def run_provider_promotion_action(
    args: argparse.Namespace,
    *,
    project_id: str,
    configs: list[AgentProviderConfig],
    state_root: Path,
) -> int:
    """Execute one already-dispatched provider-promotion CLI action."""

    if args.action not in PROMOTION_ACTIONS:
        raise ValueError(f"unsupported promotion action: {args.action}")
    if not args.task_id:
        raise TaskGitError(f"agents {args.action} requires --task-id TASK_ID")
    if args.action in {"promote", "revoke-promotion"} and not args.agent_id:
        raise TaskGitError(f"agents {args.action} requires --agent PROVIDER_ID")
    if args.agent_id and len(configs) != 1:
        raise TaskGitError(f"provider selector is ambiguous: {args.agent_id}")

    identity = resolve_storage_identity(
        state_root, project_id=project_id, task_id=args.task_id
    )
    store = ProviderPromotionStore(identity.state_dir / "provider-promotions.json")
    journal = EventJournal(identity.journal_path)

    if args.action == "promotions":
        return _list_promotions(args, configs=configs, store=store)

    config = configs[0]
    capabilities = _requested_capabilities(args, config)
    if args.action == "revoke-promotion":
        return _revoke_promotions(
            args,
            project_id=project_id,
            config=config,
            capabilities=capabilities,
            store=store,
            journal=journal,
        )
    return _create_promotions(
        args,
        project_id=project_id,
        config=config,
        capabilities=capabilities,
        store=store,
        journal=journal,
    )


def _list_promotions(
    args: argparse.Namespace,
    *,
    configs: list[AgentProviderConfig],
    store: ProviderPromotionStore,
) -> int:
    provider_filter = configs[0].provider_id if args.agent_id else ""
    promotions = [
        item.as_mapping() for item in store.list(provider_id=provider_filter)
    ]
    if args.json:
        print(json.dumps(promotions, indent=2))
        return 0
    if not promotions:
        print("No active provider promotions.")
        return 0
    for item in promotions:
        package = item["package_id"] or "task-wide"
        print(
            f"{item['provider_id']} {item['capability']} "
            f"{item['base_max_complexity']}->{item['promoted_max_complexity']} "
            f"scope={package} fallback_only={str(item['fallback_only']).lower()} "
            f"expires={item['expires_at']}"
        )
    return 0


def _requested_capabilities(
    args: argparse.Namespace, config: AgentProviderConfig
) -> list[str]:
    requested = _unique_strings(args.promotion_capabilities)
    supported = {item.value for item in config.capabilities}
    if requested:
        unknown = sorted(set(requested) - supported)
        if unknown:
            raise TaskGitError(
                f"provider {config.provider_id} does not support: " + ", ".join(unknown)
            )
        return requested
    return sorted(supported)


def _revoke_promotions(
    args: argparse.Namespace,
    *,
    project_id: str,
    config: AgentProviderConfig,
    capabilities: list[str],
    store: ProviderPromotionStore,
    journal: EventJournal,
) -> int:
    removed = store.revoke(config.provider_id, capabilities=capabilities or None)
    for promotion in removed:
        journal.append(
            "provider_promotion_revoked",
            {
                **promotion.as_mapping(),
                "project_id": project_id,
                "task_id": args.task_id,
                "source": "cli",
            },
        )
    if args.json:
        print(json.dumps([item.as_mapping() for item in removed], indent=2))
    else:
        names = ", ".join(item.capability for item in removed) or "none"
        print(
            f"Revoked provider promotion: {config.provider_id} capabilities={names}"
        )
    return 0


def _create_promotions(
    args: argparse.Namespace,
    *,
    project_id: str,
    config: AgentProviderConfig,
    capabilities: list[str],
    store: ProviderPromotionStore,
    journal: EventJournal,
) -> int:
    try:
        duration_seconds = parse_promotion_duration(args.promotion_duration)
    except ValueError as exc:
        raise TaskGitError(str(exc)) from exc
    ceiling = int(args.promoted_max_complexity)
    if not 0 <= ceiling <= 100:
        raise TaskGitError("--max-complexity must be between 0 and 100")
    supported = {item.value: item for item in config.capabilities}
    reason = (
        "temporary provider promotion"
        if args.reason == "manual_cooldown"
        else str(args.reason).strip()
    )
    created = []
    for capability_name in capabilities:
        capability = supported[capability_name]
        base = config.max_complexity_for(capability)
        if ceiling <= base:
            continue
        promotion = store.promote(
            config.provider_id,
            capability_name,
            base_max_complexity=base,
            promoted_max_complexity=ceiling,
            duration_seconds=duration_seconds,
            fallback_only=bool(args.fallback_only),
            package_id=str(args.package_id).strip(),
            allow_final_review=bool(args.allow_final_review),
            reason=reason,
            source="cli",
        )
        created.append(promotion)
        journal.append(
            "provider_promotion_created",
            {
                **promotion.as_mapping(),
                "project_id": project_id,
                "task_id": args.task_id,
            },
        )
    if not created:
        raise TaskGitError(
            "the requested ceiling does not exceed the provider's configured limits"
        )
    if args.json:
        print(json.dumps([item.as_mapping() for item in created], indent=2))
    else:
        for item in created:
            mode = "fallback-only" if item.fallback_only else "immediate"
            print(
                f"Promoted {item.provider_id} {item.capability}: "
                f"{item.base_max_complexity}->{item.promoted_max_complexity} "
                f"mode={mode} expires={item.expires_at}"
            )
    return 0


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result
