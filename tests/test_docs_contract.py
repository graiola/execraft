from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _checker_module():
    spec = importlib.util.spec_from_file_location("check_docs", ROOT / "tools" / "check_docs.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_documentation_contract_is_green() -> None:
    checker = _checker_module()
    assert checker.run_checks() == []


def test_documentation_index_covers_every_top_level_topic() -> None:
    checker = _checker_module()
    assert checker._check_index() == []


def test_primary_documented_cli_examples_parse() -> None:
    import shlex

    from execraft.cli_parsers import build_parser

    parser = build_parser()
    examples = (
        'start "Add rate limiting" --dry-run --json',
        'start "Add rate limiting" --planner local',
        'start "Add rate limiting" --planner agent --require-agent',
        'start "Update login" --repositories backend frontend',
        'start --plan-file ./PLAN.md --planner local',
        'home --json',
        'project inspect',
        'init --dry-run',
        'init --template standard',
        'project validate sample',
        'project doctor sample',
        'task new feature_auth --project sample --title "Add authentication" '
        '--brief "Add token-based authentication." --repositories backend frontend',
        'workspace start feature_auth --workspace-root /tmp/execraft-docs-workspace '
        '--policy workspace-write',
        'workspace status feature_auth',
        'workspace verify feature_auth --profile focused',
        'code feature_auth',
        'orchestrate init --project sample --task-id feature_auth '
        '--plan-file projects/sample/tasks/feature_auth/PLAN.graph.yaml',
        'orchestrate status --project sample --task-id feature_auth',
        'orchestrate run --project sample --task-id feature_auth',
        'orchestrate explain --project sample --task-id feature_auth',
        'orchestrate trace --project sample --task-id feature_auth --limit 20',
        'orchestrate usage --project sample --task-id feature_auth',
        'orchestrate context --project sample --task-id feature_auth --package-id implementation',
        'gui --project sample --task-id feature_auth',
        'agents status --project sample',
        'agents doctor --project sample --smoke-test',
        'task complete feature_auth --project sample --check',
        'task complete feature_auth --project sample --archive',
        'archive list --project sample',
        'archive verify feature_auth --project sample',
    )
    for example in examples:
        parsed = parser.parse_args(shlex.split(example))
        assert parsed.command, example
