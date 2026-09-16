"""OpenClaw configuration projection for bounded specialist sub-agents."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .subagent_policy import SubagentPolicyError, SubagentStrategy, validate_subagent_strategy

_DELEGATION_TOOLS = ("sessions_spawn", "sessions_yield", "subagents")


def delegating_agent_id(profile_id: str, *, read_only: bool) -> str:
    return f"{profile_id}-readonly-delegate" if read_only else f"{profile_id}-delegate"


def specialist_agent_id(profile_id: str) -> str:
    return f"{profile_id}-specialist"


def apply_openclaw_subagent_projection(
    config: Mapping[str, Any],
    execution: Any,
    runtime_id: str,
    strategy: SubagentStrategy,
) -> dict[str, Any]:
    """Return a managed config with explicit bounded delegation identities.

    Ordinary managed-runtime identities are left unchanged. Delegation adds separate parents that
    can only spawn the matching specialist and one hard-read-only specialist
    identity.  There is intentionally no wildcard target allowance.
    """

    projected = deepcopy(dict(config))
    if not strategy.active:
        return projected
    validate_subagent_strategy(strategy, execution)
    agents = projected.get("agents")
    if not isinstance(agents, Mapping):
        raise SubagentPolicyError("OpenClaw projection has no agents section")
    entries_raw = agents.get("entries")
    if not isinstance(entries_raw, Mapping):
        raise SubagentPolicyError("OpenClaw projection has no agent entries")
    entries = deepcopy(dict(entries_raw))

    active_policies = []
    for profile in execution.agents:
        if not profile.enabled or profile.runtime_id != runtime_id:
            continue
        policy = strategy.for_profile(profile.id)
        if not policy.enabled:
            continue
        active_policies.append(policy)
        base_id = profile.id
        readonly_id = f"{profile.id}-readonly"
        base = _entry(entries, base_id)
        readonly = _entry(entries, readonly_id)
        child_id = specialist_agent_id(profile.id)
        child = deepcopy(readonly)
        child["name"] = f"{getattr(profile, 'name', profile.id)} specialist"
        child["subagents"] = {"allowAgents": [], "requireAgentId": True}
        _restrict_specialist_tools(child, policy.allowed_tools)
        if policy.child_model_route_id:
            child["model"] = _child_model_ref(
                projected, execution, policy.child_model_route_id
            )
        generated_ids = (
            child_id,
            delegating_agent_id(profile.id, read_only=False),
            delegating_agent_id(profile.id, read_only=True),
        )
        collisions = sorted(agent_id for agent_id in generated_ids if agent_id in entries)
        if collisions:
            raise SubagentPolicyError(
                "generated OpenClaw sub-agent id collision: " + ", ".join(collisions)
            )
        entries[child_id] = child
        entries[generated_ids[1]] = _delegating_parent(base, child_id)
        entries[generated_ids[2]] = _delegating_parent(readonly, child_id)

    if not active_policies:
        return projected
    agents_out = deepcopy(dict(agents))
    agents_out["entries"] = {key: entries[key] for key in sorted(entries)}
    defaults = deepcopy(dict(agents_out.get("defaults", {})))
    defaults["subagents"] = {
        "maxSpawnDepth": 1,
        "maxConcurrent": min(item.max_concurrent for item in active_policies),
        "maxChildrenPerAgent": min(item.max_children_per_parent for item in active_policies),
        "runTimeoutSeconds": min(item.run_timeout_seconds for item in active_policies),
        "archiveAfterMinutes": min(item.archive_after_minutes for item in active_policies),
        "requireAgentId": True,
    }
    agents_out["defaults"] = defaults
    projected["agents"] = agents_out
    return projected


def _entry(entries: Mapping[str, Any], agent_id: str) -> dict[str, Any]:
    value = entries.get(agent_id)
    if not isinstance(value, Mapping):
        raise SubagentPolicyError(f"OpenClaw projection is missing agent {agent_id!r}")
    return deepcopy(dict(value))


def _delegating_parent(source: Mapping[str, Any], child_id: str) -> dict[str, Any]:
    result = deepcopy(dict(source))
    tools = deepcopy(dict(result.get("tools", {})))
    allow = [str(item) for item in tools.get("allow", []) if str(item)]
    deny = [str(item) for item in tools.get("deny", []) if str(item)]
    for tool in _DELEGATION_TOOLS:
        # Managed runtime policy already projects an explicit allow-set. Extend that same contract
        # rather than relying on an additional profile-merging key whose support
        # depends on the concrete OpenClaw release.
        if tool not in allow:
            allow.append(tool)
        deny = [item for item in deny if item != tool]
    tools["allow"] = allow
    tools["deny"] = deny
    result["tools"] = tools
    result["subagents"] = {"allowAgents": [child_id], "requireAgentId": True}
    return result


def _restrict_specialist_tools(entry: dict[str, Any], allowed_tools: tuple[str, ...]) -> None:
    tools = deepcopy(dict(entry.get("tools", {})))
    tools["allow"] = list(allowed_tools)
    denied = [str(item) for item in tools.get("deny", []) if str(item)]
    for tool in (
        "write",
        "edit",
        "apply_patch",
        "exec",
        "process",
        "sessions_spawn",
        "sessions_yield",
        "sessions_send",
        "sessions_list",
        "sessions_history",
        "subagents",
        "gateway",
        "cron",
    ):
        if tool not in denied:
            denied.append(tool)
    tools["deny"] = denied
    exec_policy = deepcopy(dict(tools.get("exec", {})))
    exec_policy.update({"host": "sandbox", "mode": "deny", "strictInlineEval": True})
    tools["exec"] = exec_policy
    tools["elevated"] = {"enabled": False}
    entry["tools"] = tools


def _child_model_ref(config: Mapping[str, Any], execution: Any, route_id: str) -> str:
    route = execution.model_route(route_id)
    provider_id = str(getattr(route, "provider_alias", "") or getattr(route, "provider", ""))
    model = str(getattr(route, "model", ""))
    model_ref = f"{provider_id}/{model}" if provider_id and model else ""
    providers = (
        config.get("models", {}).get("providers", {})
        if isinstance(config.get("models"), Mapping)
        else {}
    )
    configured = isinstance(providers, Mapping) and provider_id in providers
    if not model_ref or not configured:
        raise SubagentPolicyError(
            f"sub-agent child model route {route_id!r} is not already projected for runtime; "
            "declare it on an enabled OpenClaw profile or omit child_model_route"
        )
    return model_ref
