from __future__ import annotations

from pathlib import Path

import yaml

from execraft.gui.project_execution import ProjectExecutionGuiService
from execraft.project_execution.task_port import TaskStartResult


def _project(root: Path) -> Path:
    project = root / "projects" / "sample"
    task = project / "tasks" / "build"
    task.mkdir(parents=True)
    (project / "project.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "project": "sample",
                "description": "Sample project",
                "repositories": [
                    {
                        "id": "app",
                        "path": ".",
                        "base_branch": "main",
                        "workspace_name": "app",
                        "required": True,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (task / "TASK.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "id": "build",
                "project": "sample",
                "title": "Build feature",
                "status": "in_progress",
                "git": {"branch_name": "task/build"},
                "repositories": [
                    {
                        "id": "app",
                        "path": ".",
                        "base_branch": "main",
                        "task_branch": "task/build",
                    }
                ],
                "integration": {"verify": []},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (task / "PLAN.graph.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "work_packages": []}),
        encoding="utf-8",
    )
    return project


def test_project_execution_gui_initializes_and_projects_assets(tmp_path: Path) -> None:
    _project(tmp_path)
    gui = ProjectExecutionGuiService(
        control_root=tmp_path,
        state_root=tmp_path / "state",
    )

    assert gui.snapshot("sample")["configured"] is False
    initialized = gui.initialize("sample")
    assert initialized["configured"] is True
    assert initialized["mode"] == "assisted"

    phase = gui.upsert_phase(
        "sample",
        {"id": "foundation", "title": "Foundation"},
        expected_revision=initialized["definition_revision"],
    )
    assigned = gui.assign_task(
        "sample",
        "build",
        {"phase": "foundation", "required": True, "requires": {"tasks": [], "gates": []}},
        expected_revision=phase["definition_revision"],
    )

    assert assigned["phases"][0]["state"] == "ready"
    assert assigned["tasks"][0]["title"] == "Build feature"
    assert assigned["tasks"][0]["eligibility"]["eligible"] is True
    assert assigned["ready_tasks"] == ["build"]


def test_project_execution_gui_human_gate_decision_updates_projection(tmp_path: Path) -> None:
    _project(tmp_path)
    gui = ProjectExecutionGuiService(
        control_root=tmp_path,
        state_root=tmp_path / "state",
    )
    current = gui.initialize("sample", mode="observe")
    current = gui.upsert_gate(
        "sample",
        {
            "id": "architecture-ready",
            "title": "Architecture Ready",
            "criteria": {"all": [{"type": "human_approval"}]},
        },
        expected_revision=current["definition_revision"],
    )
    assert current["gates"][0]["state"] == "awaiting_decision"

    decided = gui.decide_gate(
        "sample",
        "architecture-ready",
        actor="operator",
        decision="approved",
        reason="reviewed",
    )
    snapshot = decided["project_execution"]
    assert snapshot["gates"][0]["state"] == "passed"
    assert snapshot["gates"][0]["decision_history"][-1]["actor"] == "operator"
    assert any(event["type"] == "project_gate_approved" for event in snapshot["journal_tail"])


def test_project_execution_gui_assisted_start_uses_injected_task_port(tmp_path: Path) -> None:
    _project(tmp_path)
    started: list[tuple[str, str]] = []

    def starter(project_id: str, task_id: str) -> TaskStartResult:
        started.append((project_id, task_id))
        return TaskStartResult(True, message="started")

    gui = ProjectExecutionGuiService(
        control_root=tmp_path,
        state_root=tmp_path / "state",
        task_starter=starter,
    )
    current = gui.initialize("sample")
    current = gui.upsert_phase(
        "sample",
        {"id": "foundation", "title": "Foundation"},
        expected_revision=current["definition_revision"],
    )
    current = gui.assign_task(
        "sample",
        "build",
        {"phase": "foundation"},
        expected_revision=current["definition_revision"],
    )

    result = gui.start_task("sample", "build")
    assert result["result"]["accepted"] is True
    assert started == [("sample", "build")]


def test_project_execution_gui_attention_separates_holds_and_gate_decisions(tmp_path: Path) -> None:
    _project(tmp_path)
    gui = ProjectExecutionGuiService(
        control_root=tmp_path,
        state_root=tmp_path / "state",
    )
    current = gui.initialize("sample")
    current = gui.upsert_gate(
        "sample",
        {
            "id": "approval",
            "title": "Approval",
            "criteria": {"all": [{"type": "human_approval"}]},
        },
        expected_revision=current["definition_revision"],
    )
    assert {row["kind"] for row in current["attention"]} == {"gate_awaiting_decision"}

    held = gui.pause("sample", reason="release freeze")
    assert {row["kind"] for row in held["attention"]} == {
        "gate_awaiting_decision",
        "project_hold",
    }


def test_project_execution_projection_round_trips_task_requirements(tmp_path: Path) -> None:
    _project(tmp_path)
    gui = ProjectExecutionGuiService(
        control_root=tmp_path,
        state_root=tmp_path / "state",
    )
    current = gui.initialize("sample")
    current = gui.upsert_phase(
        "sample",
        {"id": "foundation", "title": "Foundation"},
        expected_revision=current["definition_revision"],
    )
    current = gui.upsert_gate(
        "sample",
        {
            "id": "approval",
            "title": "Approval",
            "criteria": {"all": [{"type": "human_approval"}]},
        },
        expected_revision=current["definition_revision"],
    )
    current = gui.assign_task(
        "sample",
        "build",
        {
            "phase": "foundation",
            "required": True,
            "requires": {"tasks": [], "gates": ["approval"]},
        },
        expected_revision=current["definition_revision"],
    )

    assert current["tasks"][0]["requires"] == {
        "tasks": [],
        "gates": ["approval"],
    }


def test_project_execution_metadata_update_preserves_gate_criteria(tmp_path: Path) -> None:
    _project(tmp_path)
    gui = ProjectExecutionGuiService(
        control_root=tmp_path,
        state_root=tmp_path / "state",
    )
    current = gui.initialize("sample")
    current = gui.upsert_phase(
        "sample",
        {"id": "foundation", "title": "Foundation"},
        expected_revision=current["definition_revision"],
    )
    current = gui.assign_task(
        "sample",
        "build",
        {"phase": "foundation"},
        expected_revision=current["definition_revision"],
    )
    current = gui.upsert_gate(
        "sample",
        {
            "id": "verification-ready",
            "title": "Verification Ready",
            "criteria": {
                "all": [
                    {
                        "type": "task_verification",
                        "task_id": "build",
                        "outcome": "passed",
                    }
                ]
            },
        },
        expected_revision=current["definition_revision"],
    )
    updated = gui.update_gate_metadata(
        "sample",
        "verification-ready",
        {
            "title": "Verification Accepted",
            "description": "Renamed from the Roadmap view.",
            "schedule": {"target": "2026-10-20"},
        },
        expected_revision=current["definition_revision"],
    )

    gate = updated["gates"][0]
    assert gate["title"] == "Verification Accepted"
    assert gate["schedule"] == {"target": "2026-10-20"}
    assert gate["criteria"] == {
        "all": [
            {
                "type": "task_verification",
                "task_id": "build",
                "outcome": "passed",
            }
        ]
    }


def test_project_execution_metadata_update_preserves_milestone_requirements(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    gui = ProjectExecutionGuiService(
        control_root=tmp_path,
        state_root=tmp_path / "state",
    )
    current = gui.initialize("sample")
    current = gui.upsert_phase(
        "sample",
        {"id": "foundation", "title": "Foundation"},
        expected_revision=current["definition_revision"],
    )
    current = gui.assign_task(
        "sample",
        "build",
        {"phase": "foundation"},
        expected_revision=current["definition_revision"],
    )
    current = gui.upsert_milestone(
        "sample",
        {
            "id": "mvp",
            "title": "MVP",
            "requires": {"tasks": ["build"], "gates": [], "milestones": []},
            "delivery": {"policy": "candidate"},
        },
        expected_revision=current["definition_revision"],
    )
    updated = gui.update_milestone_metadata(
        "sample",
        "mvp",
        {"title": "MVP Candidate", "schedule": {"target": "2026-11-01"}},
        expected_revision=current["definition_revision"],
    )

    milestone = updated["milestones"][0]
    assert milestone["title"] == "MVP Candidate"
    assert milestone["target"] == "2026-11-01"
    assert milestone["requires"]["tasks"] == ["build"]
    assert milestone["delivery"]["policy"] == "candidate"


def test_project_execution_gui_configures_and_runs_explicit_automatic_cycle(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    started: list[tuple[str, str]] = []

    def starter(project_id: str, task_id: str) -> TaskStartResult:
        started.append((project_id, task_id))
        return TaskStartResult(True, message="driver launched")

    gui = ProjectExecutionGuiService(
        control_root=tmp_path,
        state_root=tmp_path / "state",
        task_starter=starter,
    )
    current = gui.initialize("sample", mode="automatic")
    current = gui.upsert_phase(
        "sample",
        {"id": "foundation", "title": "Foundation"},
        expected_revision=current["definition_revision"],
    )
    current = gui.assign_task(
        "sample",
        "build",
        {"phase": "foundation"},
        expected_revision=current["definition_revision"],
    )
    current = gui.set_policy(
        "sample",
        {
            "maximum_parallel_tasks": 2,
            "maximum_parallel_tasks_per_phase": 1,
            "maximum_active_phases": 1,
            "task_failure_behavior": "stop_new",
        },
        expected_revision=current["definition_revision"],
    )

    # Status remains an observational read even while the mode is Automatic.
    before = gui.snapshot("sample")
    assert started == []
    assert before["policy"]["maximum_parallel_tasks"] == 2

    result = gui.automatic_cycle("sample")
    assert result["cycle"]["started_tasks"] == ["build"]
    assert started == [("sample", "build")]
    assert result["project_execution"]["automatic"]["started_tasks"] == ["build"]
