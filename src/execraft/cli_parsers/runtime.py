"""Argument parsers for diagnostics, GUI, browser, plan, and guard operations."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any


def add_runtime_commands(sub: Any) -> None:
    agents = sub.add_parser("agents", help="Inspect configured execution agents")
    agents.add_argument(
        "action",
        choices=[
            "doctor",
            "status",
            "reset-health",
            "set-cooldown",
            "promote",
            "revoke-promotion",
            "promotions",
        ],
    )
    agents.add_argument("--project", dest="project_id")
    agents.add_argument(
        "--refresh-models",
        action="store_true",
        help="Refresh OpenCode's model cache before checking configured models",
    )
    agents.add_argument(
        "--smoke-test",
        action="store_true",
        help="Run an explicit minimal live call for each supported configured execution agent",
    )
    agents.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable diagnostic output",
    )
    agents.add_argument("--timeout-seconds", type=int, default=120)
    agents.add_argument(
        "--agent",
        dest="agent_id",
        help=(
            "Restrict doctor/status to one configured agent/profile ID, instance "
            "name, or alias; required by health mutations and promotion changes"
        ),
    )
    agents.add_argument(
        "--until",
        help=(
            "Timezone-aware ISO deadline for set-cooldown, for example "
            "2026-07-25T17:00:00+02:00"
        ),
    )
    agents.add_argument(
        "--reason",
        default="manual_cooldown",
        help="Operator reason for a manual cooldown or temporary promotion",
    )
    agents.add_argument("--detail", default="", help="Optional operator note")
    agents.add_argument("--state-dir", type=Path, help="Override Execraft state root")
    agents.add_argument("--task-id", help="Task scope for temporary agent-profile promotions")
    agents.add_argument(
        "--capability",
        dest="promotion_capabilities",
        action="append",
        default=[],
        help="Capability to promote/revoke; may be repeated (default: all supported)",
    )
    agents.add_argument(
        "--max-complexity",
        dest="promoted_max_complexity",
        type=int,
        default=100,
        help="Temporary complexity ceiling for promote (default: 100)",
    )
    agents.add_argument(
        "--for",
        dest="promotion_duration",
        default="4h",
        help="Promotion lifetime such as 30m, 4h, or 1d (max 7d)",
    )
    agents.add_argument(
        "--fallback-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use the raised ceiling only after normally eligible agent profiles are exhausted",
    )
    agents.add_argument(
        "--allow-final-review",
        action="store_true",
        help="Allow a promoted review ceiling to cover the final_review quality check",
    )
    agents.add_argument(
        "--package-id",
        default="",
        help="Optionally scope a promotion to one package within the task",
    )

    doctor = sub.add_parser("doctor", help="Check Execraft and execution-agent/runtime prerequisites")
    doctor.add_argument("--project", dest="project_id")
    doctor.add_argument("--task-id")

    openclaw = sub.add_parser("openclaw", help="Inspect optional OpenClaw Checkway runtimes")
    openclaw.add_argument("action", choices=["doctor"])
    openclaw.add_argument("--project", dest="project_id")
    openclaw.add_argument(
        "--runtime",
        dest="runtime_id",
        help="OpenClaw runtime ID (required when more than one is configured)",
    )
    openclaw.add_argument("--state-dir", type=Path, help="Override Execraft state root")
    openclaw.add_argument("--json", action="store_true", help="Print diagnostic JSON")

    gui = sub.add_parser("gui", help="Launch the local orchestration control center")
    gui.add_argument("--project", dest="project_id")
    gui.add_argument(
        "--task-id",
        help="Task to display; defaults to the most recently modified task state",
    )
    gui.add_argument("--host", default="127.0.0.1")
    gui.add_argument("--port", type=int, default=8765)
    gui.add_argument("--state-dir", type=Path)
    gui.add_argument(
        "--open-browser",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Open the dashboard in the default browser (default: enabled)",
    )
    gui.add_argument(
        "--allow-remote",
        action="store_true",
        help="Reserved for a future externally authenticated dashboard transport",
    )

    browser = sub.add_parser("browser", help="Browser agent operations")
    browser.add_argument("action", choices=["login", "probe", "prepare", "execute", "status", "apply"])
    browser.add_argument("--profile-dir", type=Path)
    browser.add_argument("--run-id")
    browser.add_argument("--task-id")
    browser.add_argument("--workspace-root", type=Path, default=Path.cwd())
    browser.add_argument("--verify-command", action="append")
    browser.add_argument("--adapter", choices=["fake", "playwright"], default="fake")

    plan = sub.add_parser("plan", help="Plan normalization and validation")
    plan.add_argument("action", choices=["normalize", "validate"])
    plan.add_argument("--file", type=Path, dest="plan_file", help="Path to PLAN.md or BRIEF.md")
    plan.add_argument("--task-id")
    plan.add_argument("--project", dest="project_id")

    guard_parser = sub.add_parser("guard", help="Boundary guardrail checks")
    guard_parser.add_argument("action", choices=["check"])
    guard_parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    guard_parser.add_argument("--content", action="store_true", help="Also scan file content for banned patterns")
    guard_parser.add_argument("--verbose", action="store_true")
