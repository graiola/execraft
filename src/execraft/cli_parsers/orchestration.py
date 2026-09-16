"""Argument parser for orchestration commands."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from execraft.orchestrate.execution_policy import ROLE_BY_ID


def add_orchestration_command(sub: Any) -> None:
    orch = sub.add_parser("orchestrate", help="Project orchestration operations")
    orch.add_argument(
        "action",
        choices=[
            "init",
            "status",
            "trace",
            "context",
            "usage",
            "sync",
            "explain",
            "decompose",
            "scope",
            "supervisor",
            "accept-risk",
            "policy",
            "prefer",
            "pause",
            "run",
            "daemon",
            "transition",
        ],
    )
    orch.add_argument("--project", dest="project_id")
    orch.add_argument("--task-id", help="Task/workspace ID; defaults to the project ID")
    orch.add_argument("--plan-file", type=Path)
    orch.add_argument("--package-id", help="Work package to inspect, decompose, or repair")
    orch.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum invocation records for orchestrate trace (1..500)",
    )
    orch.add_argument(
        "--include-handoff",
        action="store_true",
        help="Include the exact structured handoff in orchestrate trace JSON",
    )
    orch.add_argument(
        "--refresh",
        action="store_true",
        help="Fetch configured upstream refs before repository-sync divergence inspection",
    )
    orch.add_argument(
        "--rollback",
        action="store_true",
        help="Abort an uncommitted repository-sync transaction and reopen its package",
    )
    orch.add_argument(
        "--accept-resolution",
        action="store_true",
        help="Stage an operator-edited repository-sync conflict resolution and continue verification",
    )
    orch.add_argument(
        "--rebuild-context",
        action="store_true",
        help="Regenerate the selected package context capsule before printing it",
    )
    orch.add_argument(
        "--role",
        choices=list(ROLE_BY_ID),
        help="For orchestrate policy/prefer, the Work Package role to configure",
    )
    orch.add_argument(
        "--agent",
        dest="preferred_agents",
        action="append",
        default=[],
        help="Ranked preferred provider ID; repeat to add fallbacks",
    )
    orch.add_argument(
        "--skill",
        dest="preferred_skills",
        action="append",
        default=[],
        help="Workflow skill ID for the selected role; repeat to compose skills",
    )
    orch.add_argument(
        "--clear-agents",
        action="store_true",
        help="For orchestrate policy, restore automatic agent order for the role",
    )
    orch.add_argument(
        "--clear-skills",
        action="store_true",
        help="For orchestrate policy, restore canonical default skills for the role",
    )
    orch.add_argument(
        "--clear-preference",
        action="store_true",
        help="For orchestrate prefer, clear the selected role preference",
    )
    orch.add_argument(
        "--apply-to-shards",
        action="store_true",
        help="Apply the requested Work Package policy or pause change to direct shards",
    )
    orch.add_argument(
        "--resume",
        action="store_true",
        help="For orchestrate pause, clear the operator hold instead of setting it",
    )
    orch.add_argument(
        "--pause-reason",
        default="",
        help="Optional operator note recorded with a Work Package pause",
    )
    orch.add_argument(
        "--preferences-json",
        default="",
        help=argparse.SUPPRESS,
    )
    orch.add_argument(
        "--policy-json",
        default="",
        help=argparse.SUPPRESS,
    )
    orch.add_argument(
        "--accept-scope",
        action="store_true",
        help=(
            "For orchestrate scope, explicitly acquire the exact current workspace "
            "candidates and configured repositories, then continue at verification"
        ),
    )
    orch.add_argument(
        "--json",
        action="store_true",
        help="For orchestrate scope, print the scope report as machine-readable JSON",
    )
    orch.add_argument(
        "--expected-scope-json",
        default="",
        help=argparse.SUPPRESS,
    )
    orch.add_argument(
        "--reconcile-scope",
        action="store_true",
        help=(
            "For orchestrate scope, clear a stale repository-scope check after "
            "the workspace and declaration have already been repaired"
        ),
    )
    orch.add_argument(
        "--answer",
        help="For orchestrate supervisor, select one option from the active human question",
    )
    orch.add_argument(
        "--message",
        default="",
        help="Optional plain-language guidance accompanying --answer",
    )
    orch.add_argument(
        "--accept-reason",
        default="",
        help="For orchestrate accept-risk, operator rationale recorded with the deferred check",
    )
    orch.add_argument(
        "--expected-action-sequence",
        type=int,
        help="For orchestrate accept-risk, reject the decision if the human-decision hold changed since preview",
    )
    orch.add_argument(
        "--acknowledge-unverified",
        action="store_true",
        help="For orchestrate accept-risk, acknowledge that waived criteria remain unverified",
    )
    orch.add_argument("--to-state")
    orch.add_argument("--state-dir", type=Path)
    orch.add_argument("--max-attempts", type=int)
    orch.add_argument("--initial-backoff-seconds", type=float, default=5.0)
    orch.add_argument("--max-backoff-seconds", type=float, default=300.0)
    orch.add_argument("--backoff-multiplier", type=float, default=2.0)
    orch.add_argument(
        "--no-wait-for-agents",
        action="store_true",
        help=(
            "Return when no compatible provider is currently available instead "
            "of polling in the foreground"
        ),
    )
    orch.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress live orchestration progress (the append-only log is still written)",
    )
    orch.add_argument(
        "--log-file",
        type=Path,
        help=(
            "Append human-readable progress to this file; defaults to "
            "<state-dir>/projects/<task-id>/orchestrator.log"
        ),
    )
    orch.add_argument(
        "--heartbeat-interval",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help=(
            "Emit agent subprocess heartbeats at this interval while run/daemon "
            "waits; use 0 to disable (default: 30)"
        ),
    )
