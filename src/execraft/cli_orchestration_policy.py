"""CLI parsing for package-level orchestration execution policy."""

from __future__ import annotations

import argparse
import json
from typing import Any, Mapping

from execraft.orchestrate.execution_policy import ROLE_BY_ID
from execraft.workspace.task_git import TaskGitError


def copy_role_preferences(
    mapping: Mapping[str, list[str]] | None,
) -> dict[str, list[str]]:
    """Copy persisted role preferences without sharing mutable list state."""

    return {str(role): list(values) for role, values in (mapping or {}).items()}


def load_json_object(raw: str, *, option: str) -> dict[str, Any]:
    """Parse one hidden GUI/automation JSON payload with stable CLI errors."""

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TaskGitError(f"invalid {option} payload") from exc
    if not isinstance(payload, dict):
        raise TaskGitError(f"{option} must encode an object")
    return payload


def legacy_agent_preferences_from_args(
    args: argparse.Namespace, package: Any
) -> dict[str, list[str]]:
    """Resolve the deprecated ``orchestrate prefer`` argument surface."""

    if args.policy_json or args.preferred_skills or args.clear_skills or args.clear_agents:
        raise TaskGitError(
            "orchestrate prefer is the legacy agent-only command; use "
            "orchestrate policy for workflow skills"
        )
    if args.preferences_json:
        if args.role or args.preferred_agents or args.clear_preference:
            raise TaskGitError(
                "--preferences-json cannot be combined with role preference flags"
            )
        return load_json_object(args.preferences_json, option="--preferences-json")
    if not args.role:
        raise TaskGitError("orchestrate prefer requires --role")
    if args.clear_preference and args.preferred_agents:
        raise TaskGitError(
            "orchestrate prefer accepts either --clear-preference or --agent"
        )
    if not args.clear_preference and not args.preferred_agents:
        raise TaskGitError(
            "orchestrate prefer requires at least one --agent or --clear-preference"
        )
    preferences = copy_role_preferences(package.agent_preferences)
    if args.clear_preference:
        preferences.pop(args.role, None)
    else:
        preferences[args.role] = list(args.preferred_agents)
    return preferences


def execution_policy_from_args(
    args: argparse.Namespace, package: Any
) -> tuple[dict[str, list[str]], dict[str, list[str]], list[str]]:
    """Resolve the canonical package execution-policy CLI arguments."""

    if args.preferences_json or args.clear_preference:
        raise TaskGitError(
            "orchestrate policy uses --policy-json, --clear-agents, and "
            "--clear-skills; legacy preference flags are not accepted"
        )
    if args.policy_json:
        if any(
            (
                args.role,
                args.preferred_agents,
                args.preferred_skills,
                args.clear_agents,
                args.clear_skills,
            )
        ):
            raise TaskGitError("--policy-json cannot be combined with role policy flags")
        policy = load_json_object(args.policy_json, option="--policy-json")
        agents = policy.get("agents", {})
        skills = policy.get("skills", {})
        binding_roles = policy.get(
            "binding_agent_roles", package.agent_preference_binding_roles
        )
        if not isinstance(agents, dict) or not isinstance(skills, dict):
            raise TaskGitError("--policy-json agents and skills must be objects")
        if not isinstance(binding_roles, list):
            raise TaskGitError("--policy-json binding_agent_roles must be a list")
        return agents, skills, [str(item) for item in binding_roles if str(item)]

    if not args.role:
        raise TaskGitError("orchestrate policy requires --role")
    if args.role not in ROLE_BY_ID:
        raise TaskGitError(f"unknown execution role: {args.role!r}")
    if not any(
        (
            args.preferred_agents,
            args.preferred_skills,
            args.clear_agents,
            args.clear_skills,
        )
    ):
        raise TaskGitError(
            "orchestrate policy requires --agent, --skill, --clear-agents, or --clear-skills"
        )
    if args.clear_agents and args.preferred_agents:
        raise TaskGitError("--clear-agents cannot be combined with --agent")
    if args.clear_skills and args.preferred_skills:
        raise TaskGitError("--clear-skills cannot be combined with --skill")

    agents = copy_role_preferences(package.agent_preferences)
    skills = copy_role_preferences(package.skill_preferences)
    if args.clear_agents:
        agents.pop(args.role, None)
    elif args.preferred_agents:
        agents[args.role] = list(args.preferred_agents)
    if args.clear_skills:
        skills.pop(args.role, None)
    elif args.preferred_skills:
        skills[args.role] = list(args.preferred_skills)
    return agents, skills, list(package.agent_preference_binding_roles)
