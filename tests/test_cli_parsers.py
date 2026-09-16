"""Regression tests for the domain-owned CLI parser tree."""

from __future__ import annotations

from execraft.cli_parsers import build_parser


def test_parser_exposes_all_top_level_commands() -> None:
    parser = build_parser()
    action = next(item for item in parser._actions if item.dest == "command")
    assert set(action.choices) == {
        "project",
        "init",
        "start",
        "new",
        "home",
        "projects",
        "task",
        "archive",
        "render",
        "workspace",
        "code",
        "agents",
        "doctor",
        "export",
        "openclaw",
        "gui",
        "browser",
        "plan",
        "guard",
        "orchestrate",
    }


def test_onboarding_and_orchestration_arguments_remain_compatible() -> None:
    parser = build_parser()
    start = parser.parse_args(
        [
            "start",
            "Add OIDC authentication",
            "--planner",
            "local",
            "--repositories",
            "api",
            "auth",
            "--no-workspace",
        ]
    )
    assert start.command == "start"
    assert start.description == "Add OIDC authentication"
    assert start.repositories == ["api", "auth"]
    assert start.no_workspace is True

    orchestrate = parser.parse_args(
        [
            "orchestrate",
            "run",
            "--project",
            "sample",
            "--agent",
            "codex",
            "--agent",
            "claude",
        ]
    )
    assert orchestrate.command == "orchestrate"
    assert orchestrate.preferred_agents == ["codex", "claude"]


def test_task_replan_parser_exposes_revision_controls() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "task",
            "replan",
            "demo",
            "--request",
            "Split the remaining work",
            "--provider",
            "planner",
            "--supersede",
            "WP2=M2R",
            "--apply",
            "--state-dir",
            "/tmp/execraft-state",
        ]
    )
    assert args.action == "replan"
    assert args.task_id == "demo"
    assert args.request == "Split the remaining work"
    assert args.provider == "planner"
    assert args.supersede == ["WP2=M2R"]
    assert args.apply is True


def test_orchestrate_accept_risk_parser_requires_explicit_ack_surface() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "orchestrate",
            "accept-risk",
            "--project",
            "demo",
            "--task-id",
            "task",
            "--package-id",
            "WP24",
            "--accept-reason",
            "Defer real simulation validation to the host campaign.",
            "--expected-action-sequence",
            "42",
            "--acknowledge-unverified",
        ]
    )
    assert args.action == "accept-risk"
    assert args.package_id == "WP24"
    assert args.expected_action_sequence == 42
    assert args.acknowledge_unverified is True
