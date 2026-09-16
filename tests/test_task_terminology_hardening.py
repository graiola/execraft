from __future__ import annotations

import importlib
from pathlib import Path

from execraft.orchestrate.event_compat import canonical_task_event_type, task_event_work_package_id
from execraft.orchestrate.normalizer import parse_plan_document
from execraft.repository_sync.planning import build_sync_before_definition
from execraft.repository_sync.policy import RepositorySyncPolicy


def test_retired_task_api_symbols_are_not_exported() -> None:
    orchestrate = importlib.import_module("execraft.orchestrate")
    for retired in (
        "ProjectState",
        "ProjectStateRecord",
        "MilestoneDirectiveCommand",
        "MilestoneDirectiveQueue",
        "MilestoneDirectiveError",
    ):
        assert not hasattr(orchestrate, retired), retired


def test_historical_gate_events_project_to_canonical_check_events() -> None:
    aliases = {
        "repository_scope_gate_reconciled": "repository_scope_check_reconciled",
        "repository_sync_commit_gate_superseded": "repository_sync_commit_check_superseded",
        "repository_sync_commit_gate_recovery": "repository_sync_commit_check_recovery",
    }
    for historical, canonical in aliases.items():
        assert canonical_task_event_type(historical) == canonical
        assert canonical_task_event_type(canonical) == canonical



def test_historical_milestone_payload_id_is_confined_to_compatibility_reader() -> None:
    assert task_event_work_package_id({"work_package_id": "WP1"}) == "WP1"
    assert task_event_work_package_id({"package_id": "WP2"}) == "WP2"
    assert task_event_work_package_id({"milestone_id": "WP3"}) == "WP3"

def test_repository_sync_plan_writer_emits_only_work_package_terminology() -> None:
    graph = """schema_version: 1
work_packages:
  - id: WP19
    title: predecessor
    affected_repositories: [core]
  - id: WP20
    title: target
    dependencies: [WP19]
    affected_repositories: [core]
    requirements: [work]
    acceptance_criteria:
      - id: done
        description: done
"""
    insertion = build_sync_before_definition(
        brief_markdown="# Brief\n",
        plan_markdown="# Plan\n",
        plan_graph_yaml=graph,
        before_package_id="WP20",
        repositories=["core"],
    )
    plan = insertion.definition.plan_markdown
    assert "## Work Package: WP20-SYNC" in plan
    assert "milestone" not in plan.lower()
    assert "final gate" not in plan.lower()


def test_legacy_task_plan_milestone_heading_is_read_only_compatibility() -> None:
    graph, report = parse_plan_document(
        """## Milestone: Legacy package
- [ ] remains readable
"""
    )
    assert len(graph.work_packages) == 1
    assert graph.work_packages[0].title == "Legacy package"
    assert any("legacy Task PLAN 'Milestone'" in item for item in report.warnings)


def test_legacy_repository_sync_config_reads_but_serializes_canonical_key() -> None:
    policy = RepositorySyncPolicy.from_mapping(
        {"divergence": {"refresh_before_gate": True}}
    )
    assert policy.refresh_before_check is True
    emitted = policy.as_mapping()
    divergence = emitted["divergence"]
    assert divergence["refresh_before_check"] is True
    assert "refresh_before_gate" not in divergence


def test_retired_task_milestone_gui_modules_do_not_exist() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "execraft" / "assets" / "gui"
    assert not list(root.glob("milestone-*.js"))
    for name in (
        "work-package-inspector.js",
        "work-package-execution.js",
        "work-package-presenter.js",
    ):
        assert (root / name).is_file()
