from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from execraft.roadmap import RoadmapConflictError, RoadmapError, RoadmapNotFoundError, RoadmapService
from execraft.roadmap.models import Roadmap, RoadmapItem, RoadmapRelation, RoadmapSchedule
from execraft.project_execution.models import ProjectTask
from execraft.project_execution.errors import ProjectExecutionConflictError
from execraft.project_execution.repository import ProjectExecutionRepository
from execraft.project_execution.service import ProjectExecutionService


def _project(root: Path) -> Path:
    project = root / "projects" / "sample"
    project.mkdir(parents=True)
    (project / "project.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "project": "sample",
                "repositories": [
                    {"id": "app", "path": "app", "base_branch": "main"}
                ],
            }
        ),
        encoding="utf-8",
    )
    return project


def _task(project: Path, task_id: str, *, title: str, status: str = "planned") -> Path:
    dossier = project / "tasks" / task_id
    dossier.mkdir(parents=True)
    (dossier / "TASK.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "id": task_id,
                "project": "sample",
                "title": title,
                "status": status,
                "created_at": "2026-09-09T00:00:00+00:00",
                "git": {
                    "branch_name": f"task/{task_id}",
                    "merge_strategy": "squash",
                },
                "repositories": [
                    {
                        "id": "app",
                        "base_branch": "main",
                        "task_branch": f"task/{task_id}",
                    }
                ],
                "integration": {"verify": []},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return dossier


def _service(tmp_path: Path) -> RoadmapService:
    return RoadmapService(
        control_root=tmp_path / "control",
        state_root=tmp_path / "state",
        active_task=lambda: None,
    )


def test_roadmap_model_rejects_invalid_dates_duplicate_tasks_and_block_cycles() -> None:
    with pytest.raises(RoadmapError, match="start cannot be after"):
        RoadmapSchedule(start="2026-10-02", target="2026-10-01")

    with pytest.raises(RoadmapError, match="order must be an integer"):
        RoadmapItem(id="bad-order", kind="milestone", title="MVP", order="10")  # type: ignore[arg-type]

    task_a = RoadmapItem(id="task-a", kind="task", task_id="alpha")
    task_b = RoadmapItem(id="task-b", kind="task", task_id="alpha")
    with pytest.raises(RoadmapError, match="linked only once"):
        Roadmap(id="main", title="Main", items=(task_a, task_b))

    one = RoadmapItem(id="one", kind="planned_task", title="One")
    two = RoadmapItem(id="two", kind="planned_task", title="Two")
    with pytest.raises(RoadmapError, match="acyclic"):
        Roadmap(
            id="main",
            title="Main",
            items=(one, two),
            relations=(
                RoadmapRelation("one", "two", "blocks"),
                RoadmapRelation("two", "one", "blocks"),
            ),
        )


def test_create_edit_link_and_project_task_state_without_duplication(tmp_path: Path) -> None:
    control = tmp_path / "control"
    project = _project(control)
    _task(project, "alpha", title="Canonical Alpha", status="in_progress")
    service = _service(tmp_path)

    created = service.create("sample", title="Platform 2026")
    assert created["id"] == "platform-2026"
    assert created["revision"] == 1

    planned = service.upsert_item(
        "sample",
        created["id"],
        expected_revision=1,
        raw_item={
            "kind": "planned_task",
            "title": "Future task",
            "description": "Convert me later",
            "lane": "Platform",
            "schedule": {"target": "2026-10-15"},
        },
    )
    item = planned["items"][0]
    assert item["title"] == "Future task"
    assert planned["revision"] == 2

    linked = service.link_task(
        "sample",
        created["id"],
        expected_revision=2,
        task_id="alpha",
        item_id=item["id"],
    )
    linked_item = linked["items"][0]
    assert "title" not in linked_item
    assert linked_item["task"]["title"] == "Canonical Alpha"
    assert linked_item["task"]["status"] == "in_progress"
    assert linked_item["schedule"]["target"] == "2026-10-15"

    persisted = yaml.safe_load(
        (project / "roadmaps" / "platform-2026.yaml").read_text(encoding="utf-8")
    )
    assert persisted["items"][0]["task_id"] == "alpha"
    assert "title" not in persisted["items"][0]
    assert "description" not in persisted["items"][0]


def test_unscheduled_tasks_runtime_progress_and_archived_projection(tmp_path: Path) -> None:
    control = tmp_path / "control"
    project = _project(control)
    _task(project, "alpha", title="Alpha")
    _task(project, "beta", title="Beta")
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Main")
    current = service.link_task(
        "sample",
        roadmap["id"],
        expected_revision=roadmap["revision"],
        task_id="alpha",
        target="2026-09-30",
    )

    state_dir = tmp_path / "state" / "projects" / "alpha"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "state": "running",
                "completed_packages": 3,
                "total_packages": 4,
            }
        ),
        encoding="utf-8",
    )
    projected = service.get("sample", current["id"])
    assert [task["id"] for task in projected["unscheduled_tasks"]] == ["beta"]
    assert projected["items"][0]["task"]["progress_percent"] == 75

    archive = control / "projects" / ".archive" / "tasks" / "sample" / "alpha"
    archive.parent.mkdir(parents=True)
    (project / "tasks" / "alpha").rename(archive)
    archived = service.get("sample", current["id"])
    assert archived["items"][0]["task"]["availability"] == "archived"


def test_optimistic_revision_conflicts_do_not_overwrite_newer_edit(tmp_path: Path) -> None:
    control = tmp_path / "control"
    _project(control)
    service = _service(tmp_path)
    created = service.create("sample", title="Main")
    first = service.upsert_item(
        "sample",
        created["id"],
        expected_revision=1,
        raw_item={"kind": "milestone", "title": "MVP"},
    )
    assert first["revision"] == 2

    with pytest.raises(RoadmapConflictError, match="refresh before saving"):
        service.upsert_item(
            "sample",
            created["id"],
            expected_revision=1,
            raw_item={"kind": "milestone", "title": "Stale"},
        )
    assert [item["title"] for item in service.get("sample", created["id"])["items"]] == [
        "MVP"
    ]


def test_relations_validate_endpoints_cycles_and_are_removed_with_items(tmp_path: Path) -> None:
    control = tmp_path / "control"
    _project(control)
    service = _service(tmp_path)
    current = service.create("sample", title="Main")
    for title in ("A", "B"):
        current = service.upsert_item(
            "sample",
            current["id"],
            expected_revision=current["revision"],
            raw_item={"kind": "planned_task", "title": title},
        )
    first, second = [item["id"] for item in current["items"]]
    current = service.upsert_relation(
        "sample",
        current["id"],
        expected_revision=current["revision"],
        source=first,
        target=second,
        kind="blocks",
    )
    with pytest.raises(RoadmapError, match="acyclic"):
        service.upsert_relation(
            "sample",
            current["id"],
            expected_revision=current["revision"],
            source=second,
            target=first,
            kind="blocks",
        )
    current = service.delete_item(
        "sample",
        current["id"],
        expected_revision=current["revision"],
        item_id=first,
    )
    assert current["relations"] == []


def test_deleting_roadmap_never_deletes_linked_tasks(tmp_path: Path) -> None:
    control = tmp_path / "control"
    project = _project(control)
    _task(project, "alpha", title="Alpha")
    service = _service(tmp_path)
    current = service.create("sample", title="Main")
    current = service.link_task(
        "sample",
        current["id"],
        expected_revision=current["revision"],
        task_id="alpha",
    )
    result = service.delete(
        "sample",
        current["id"],
        expected_revision=current["revision"],
        acknowledged=True,
    )
    assert result["tasks_deleted"] == 0
    assert (project / "tasks" / "alpha" / "TASK.yaml").is_file()
    assert not (project / "roadmaps" / f"{current['id']}.yaml").exists()


def test_linked_task_uses_canonical_task_id_contract_including_dots(tmp_path: Path) -> None:
    control = tmp_path / "control"
    project = _project(control)
    _task(project, "release.1", title="Release One")
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Release")

    linked = service.link_task(
        "sample",
        roadmap["id"],
        expected_revision=roadmap["revision"],
        task_id="release.1",
    )

    assert linked["items"][0]["task_id"] == "release.1"
    assert linked["items"][0]["task"]["title"] == "Release One"


def test_invalid_task_dossiers_are_visible_but_not_offered_for_new_links(
    tmp_path: Path,
) -> None:
    control = tmp_path / "control"
    project = _project(control)
    invalid = project / "tasks" / "broken"
    invalid.mkdir(parents=True)
    (invalid / "TASK.yaml").write_text("schema_version: nope\n", encoding="utf-8")
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Main")

    assert roadmap["unscheduled_tasks"] == []
    with pytest.raises(RoadmapError, match="active valid task not found"):
        service.link_task(
            "sample",
            roadmap["id"],
            expected_revision=roadmap["revision"],
            task_id="broken",
        )


def test_task_link_conversion_only_accepts_planned_task_items(tmp_path: Path) -> None:
    control = tmp_path / "control"
    project = _project(control)
    _task(project, "alpha", title="Alpha")
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Main")
    with_milestone = service.upsert_item(
        "sample",
        roadmap["id"],
        expected_revision=roadmap["revision"],
        raw_item={"kind": "milestone", "title": "Field beta"},
    )
    milestone_id = with_milestone["items"][0]["id"]

    with pytest.raises(RoadmapError, match="only planned_task items"):
        service.link_task(
            "sample",
            with_milestone["id"],
            expected_revision=with_milestone["revision"],
            task_id="alpha",
            item_id=milestone_id,
        )


def test_gate_is_first_class_and_move_item_reorders_rows_atomically(tmp_path: Path) -> None:
    control = tmp_path / "control"
    _project(control)
    service = _service(tmp_path)
    current = service.create("sample", title="Main")
    for kind, title, lane in (("planned_task", "A", "Platform"), ("gate", "Review", "Platform"), ("planned_task", "B", "Core")):
        current = service.upsert_item(
            "sample",
            current["id"],
            expected_revision=current["revision"],
            raw_item={
                "kind": kind,
                "title": title,
                "lane": lane,
                "schedule": {"target": "2026-10-01"},
            },
        )

    first, gate, last = current["items"]
    moved = service.move_item(
        "sample",
        current["id"],
        expected_revision=current["revision"],
        item_id=last["id"],
        lane="Platform",
        target="2026-10-20",
        target_item_id=gate["id"],
        placement="before",
    )

    assert moved["statistics"]["gates"] == 1
    ordered = sorted(moved["items"], key=lambda item: item["order"])
    assert [item["id"] for item in ordered] == [first["id"], last["id"], gate["id"]]
    moved_last = next(item for item in moved["items"] if item["id"] == last["id"])
    assert moved_last["lane"] == "Platform"
    assert moved_last["schedule"]["target"] == "2026-10-20"
    assert [item["order"] for item in ordered] == [10, 20, 30]


def test_move_lane_reorders_complete_swimlanes_and_preserves_internal_order(tmp_path: Path) -> None:
    control = tmp_path / "control"
    _project(control)
    service = _service(tmp_path)
    current = service.create("sample", title="Main")
    for title, lane in (("A1", "Alpha"), ("A2", "Alpha"), ("B1", "Bravo"), ("B2", "Bravo"), ("C1", "Charlie")):
        current = service.upsert_item(
            "sample",
            current["id"],
            expected_revision=current["revision"],
            raw_item={
                "kind": "planned_task",
                "title": title,
                "lane": lane,
                "schedule": {"target": "2026-10-01"},
            },
        )

    moved = service.move_lane(
        "sample",
        current["id"],
        expected_revision=current["revision"],
        lane="Charlie",
        target_lane="Alpha",
        placement="before",
    )

    ordered = sorted(moved["items"], key=lambda item: item["order"])
    assert [(item["lane"], item["title"]) for item in ordered] == [
        ("Charlie", "C1"),
        ("Alpha", "A1"),
        ("Alpha", "A2"),
        ("Bravo", "B1"),
        ("Bravo", "B2"),
    ]
    assert [item["order"] for item in ordered] == [10, 20, 30, 40, 50]


def test_move_lane_rejects_unknown_target_lane(tmp_path: Path) -> None:
    control = tmp_path / "control"
    _project(control)
    service = _service(tmp_path)
    current = service.create("sample", title="Main")
    current = service.upsert_item(
        "sample",
        current["id"],
        expected_revision=current["revision"],
        raw_item={"kind": "planned_task", "title": "A", "lane": "Alpha"},
    )

    with pytest.raises(RoadmapNotFoundError, match="target roadmap lane not found"):
        service.move_lane(
            "sample",
            current["id"],
            expected_revision=current["revision"],
            lane="Alpha",
            target_lane="Missing",
        )


def test_roadmap_schedule_duration_is_derived_not_persisted() -> None:
    from execraft.roadmap.models import RoadmapSchedule

    schedule = RoadmapSchedule(start="2026-09-11", target="2026-09-24")
    assert schedule.duration_days == 14
    assert schedule.as_mapping() == {"start": "2026-09-11", "target": "2026-09-24"}
    assert RoadmapSchedule(target="2026-09-11").duration_days == 1
    assert RoadmapSchedule().duration_days == 0


def test_project_phase_grouping_is_project_execution_projection_only(tmp_path: Path) -> None:
    control = tmp_path / "control"
    project = _project(control)
    _task(project, "alpha", title="Alpha")
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Main")
    with_phase = service.upsert_item(
        "sample",
        roadmap["id"],
        expected_revision=roadmap["revision"],
        raw_item={
            "kind": "phase",
            "title": "Integration",
            "schedule": {"start": "2026-10-01", "target": "2026-10-31"},
        },
    )
    phase_item = with_phase["items"][0]
    phase_id = phase_item["project_asset_id"]

    execution = ProjectExecutionService(ProjectExecutionRepository(project))
    definition = execution.get()
    execution.assign_task(
        ProjectTask(task_id="alpha", phase=phase_id),
        expected_revision=definition.revision,
    )
    linked = service.link_task(
        "sample",
        with_phase["id"],
        expected_revision=with_phase["revision"],
        task_id="alpha",
    )

    projected = service.get("sample", linked["id"])
    phase_row = next(item for item in projected["items"] if item["kind"] == "phase")
    task_row = next(item for item in projected["items"] if item["kind"] == "task")

    assert projected["project_execution_configured"] is True
    assert phase_row["project_phase"] == {
        "id": phase_id,
        "title": "Integration",
        "order": 0,
        "kind": "phase",
    }
    assert task_row["project_phase"] == phase_row["project_phase"]

    persisted = yaml.safe_load(
        (project / "roadmaps" / f"{linked['id']}.yaml").read_text(encoding="utf-8")
    )
    assert "project_phase" not in persisted["items"][0]
    assert "project_phase" not in persisted["items"][1]


def test_stale_roadmap_move_cannot_mutate_canonical_schedule_first(tmp_path: Path) -> None:
    control = tmp_path / "control"
    project = _project(control)
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Main")
    with_gate = service.upsert_item(
        "sample",
        roadmap["id"],
        expected_revision=roadmap["revision"],
        raw_item={
            "kind": "gate",
            "title": "Review",
            "schedule": {"target": "2026-10-01"},
        },
    )
    gate = with_gate["items"][0]
    execution_before = yaml.safe_load(
        (project / "PROJECT_EXECUTION.yaml").read_text(encoding="utf-8")
    )

    newer = service.upsert_item(
        "sample",
        with_gate["id"],
        expected_revision=with_gate["revision"],
        raw_item={"kind": "planned_task", "title": "Concurrent roadmap edit"},
    )
    with pytest.raises(RoadmapConflictError, match="refresh before saving"):
        service.move_item(
            "sample",
            with_gate["id"],
            expected_revision=with_gate["revision"],
            expected_project_execution_revision=with_gate["project_execution_revision"],
            item_id=gate["id"],
            lane=gate["lane"],
            target="2026-11-15",
        )

    execution_after = yaml.safe_load(
        (project / "PROJECT_EXECUTION.yaml").read_text(encoding="utf-8")
    )
    assert execution_after == execution_before
    assert service.get("sample", newer["id"])["revision"] == newer["revision"]


def test_stale_project_execution_move_cannot_mutate_roadmap(tmp_path: Path) -> None:
    control = tmp_path / "control"
    project = _project(control)
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Main")
    with_gate = service.upsert_item(
        "sample",
        roadmap["id"],
        expected_revision=roadmap["revision"],
        raw_item={
            "kind": "gate",
            "title": "Review",
            "schedule": {"target": "2026-10-01"},
        },
    )
    gate_row = with_gate["items"][0]
    stale_execution_revision = with_gate["project_execution_revision"]

    execution = ProjectExecutionService(ProjectExecutionRepository(project))
    definition = execution.get()
    gate = definition.gate_index[gate_row["project_asset_id"]]
    execution.upsert_gate(
        replace(gate, description="Concurrent Project Execution edit"),
        expected_revision=definition.revision,
    )

    with pytest.raises(ProjectExecutionConflictError, match="Project Execution changed"):
        service.move_item(
            "sample",
            with_gate["id"],
            expected_revision=with_gate["revision"],
            expected_project_execution_revision=stale_execution_revision,
            item_id=gate_row["id"],
            lane=gate_row["lane"],
            target="2026-11-15",
        )

    after = service.get("sample", with_gate["id"])
    assert after["revision"] == with_gate["revision"]
    current_gate = ProjectExecutionService(ProjectExecutionRepository(project)).get().gate_index[
        gate_row["project_asset_id"]
    ]
    assert current_gate.schedule.target == "2026-10-01"


def test_canonical_move_recovers_after_project_execution_write_before_roadmap_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execraft.roadmap.coordination import RoadmapCoordinationStore
    from execraft.roadmap.repository import RoadmapRepository

    control = tmp_path / "control"
    project = _project(control)
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Main")
    with_gate = service.upsert_item(
        "sample",
        roadmap["id"],
        expected_revision=roadmap["revision"],
        raw_item={
            "kind": "gate",
            "title": "Review",
            "schedule": {"target": "2026-10-01"},
        },
    )
    gate_row = with_gate["items"][0]
    original_save = RoadmapRepository.save
    failed = False

    def fail_once(self, roadmap, *, expected_revision):  # type: ignore[no-untyped-def]
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated crash before Roadmap durable write")
        return original_save(self, roadmap, expected_revision=expected_revision)

    monkeypatch.setattr(RoadmapRepository, "save", fail_once)
    with pytest.raises(OSError, match="simulated crash"):
        service.move_item(
            "sample",
            with_gate["id"],
            expected_revision=with_gate["revision"],
            expected_project_execution_revision=with_gate["project_execution_revision"],
            item_id=gate_row["id"],
            lane="Decision",
            target="2026-11-15",
        )

    # Canonical side completed, Roadmap side did not.  The durable intent must
    # survive the failed request so a fresh service instance can roll forward.
    pending = RoadmapCoordinationStore(tmp_path / "state", "sample").load_pending()
    assert pending is not None
    assert pending.phase == "project_execution_applied"
    gate = ProjectExecutionRepository(project).load().gate_index[gate_row["project_asset_id"]]
    assert gate.schedule.target == "2026-11-15"
    assert RoadmapRepository(
        service._project("sample"), state_root=tmp_path / "state"  # type: ignore[attr-defined]
    ).load(with_gate["id"]).revision == with_gate["revision"]

    monkeypatch.setattr(RoadmapRepository, "save", original_save)
    recovered = _service(tmp_path).get("sample", with_gate["id"])
    recovered_gate = next(item for item in recovered["items"] if item["kind"] == "gate")
    assert recovered_gate["lane"] == "Decision"
    assert recovered_gate["schedule"]["target"] == "2026-11-15"
    assert recovered["revision"] == with_gate["revision"] + 1
    assert recovered["project_execution_revision"] == with_gate["project_execution_revision"] + 1
    assert RoadmapCoordinationStore(tmp_path / "state", "sample").load_pending() is None
    journal = (
        tmp_path / "state" / "roadmap-coordination" / "sample" / "journal.jsonl"
    ).read_text(encoding="utf-8")
    assert pending.operation_id in journal


def test_canonical_asset_creation_recovers_without_orphaning_project_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execraft.roadmap.coordination import RoadmapCoordinationStore
    from execraft.roadmap.repository import RoadmapRepository

    control = tmp_path / "control"
    project = _project(control)
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Main")
    original_save = RoadmapRepository.save

    def fail_save(self, roadmap, *, expected_revision):  # type: ignore[no-untyped-def]
        raise OSError("simulated crash after canonical asset creation")

    monkeypatch.setattr(RoadmapRepository, "save", fail_save)
    with pytest.raises(OSError, match="canonical asset creation"):
        service.upsert_item(
            "sample",
            roadmap["id"],
            expected_revision=roadmap["revision"],
            raw_item={
                "id": "review-gate",
                "kind": "gate",
                "title": "Review",
                "lane": "Decision",
                "schedule": {"target": "2026-10-01"},
            },
        )

    definition = ProjectExecutionRepository(project).load()
    assert "review-gate" in definition.gate_index
    pending = RoadmapCoordinationStore(tmp_path / "state", "sample").load_pending()
    assert pending is not None

    monkeypatch.setattr(RoadmapRepository, "save", original_save)
    recovered = _service(tmp_path).get("sample", roadmap["id"])
    assert recovered["items"] == [
        {
            **next(item for item in recovered["items"] if item["kind"] == "gate")
        }
    ]
    gate_row = recovered["items"][0]
    assert gate_row["project_asset_id"] == "review-gate"
    assert gate_row["lane"] == "Decision"
    assert RoadmapCoordinationStore(tmp_path / "state", "sample").load_pending() is None


def test_coordination_recovery_never_overwrites_divergent_roadmap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execraft.roadmap.coordination import (
        RoadmapCoordinationConflictError,
        RoadmapCoordinationStore,
    )
    from execraft.roadmap.repository import RoadmapRepository

    control = tmp_path / "control"
    project_dir = _project(control)
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Main")
    with_gate = service.upsert_item(
        "sample",
        roadmap["id"],
        expected_revision=roadmap["revision"],
        raw_item={"kind": "gate", "title": "Review", "lane": "Decision"},
    )
    gate_row = with_gate["items"][0]
    original_save = RoadmapRepository.save
    failed = False

    def fail_once(self, roadmap, *, expected_revision):  # type: ignore[no-untyped-def]
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated split write")
        return original_save(self, roadmap, expected_revision=expected_revision)

    monkeypatch.setattr(RoadmapRepository, "save", fail_once)
    with pytest.raises(OSError, match="split write"):
        service.move_item(
            "sample",
            with_gate["id"],
            expected_revision=with_gate["revision"],
            expected_project_execution_revision=with_gate["project_execution_revision"],
            item_id=gate_row["id"],
            lane="Moved",
            target="2026-12-01",
        )
    monkeypatch.setattr(RoadmapRepository, "save", original_save)

    # Simulate an independent Roadmap-only edit that won the race after the
    # canonical write.  Recovery must not replace this newer durable document.
    descriptor = service._project("sample")  # type: ignore[attr-defined]
    repository = RoadmapRepository(descriptor, state_root=tmp_path / "state")
    current = repository.load(with_gate["id"])
    concurrent = replace(
        current,
        description="Concurrent planning edit",
        updated_at="2026-09-11T18:00:00+00:00",
    )
    saved_concurrent = repository.save(concurrent, expected_revision=current.revision)

    observed = _service(tmp_path).get("sample", with_gate["id"])
    coordination = observed["coordination"]
    assert coordination["pending"] is True
    assert coordination["divergent"] is True
    assert coordination["roadmap"]["state"] == "divergent"
    assert coordination["project_execution"]["state"] == "applied"
    assert coordination["safe_actions"] == []

    durable = repository.load(with_gate["id"])
    assert durable.revision == saved_concurrent.revision
    assert durable.description == "Concurrent planning edit"
    canonical_gate = ProjectExecutionRepository(project_dir).load().gate_index[
        gate_row["project_asset_id"]
    ]
    assert canonical_gate.schedule.target == "2026-12-01"
    assert RoadmapCoordinationStore(tmp_path / "state", "sample").load_pending() is not None

    with pytest.raises(RoadmapCoordinationConflictError, match="requires operator resolution"):
        _service(tmp_path).update_metadata(
            "sample",
            with_gate["id"],
            expected_revision=saved_concurrent.revision,
            title="Blocked by pending coordination",
        )


def test_project_execution_gui_snapshot_surfaces_divergent_coordination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execraft.gui.project_execution import ProjectExecutionGuiService
    from execraft.roadmap.repository import RoadmapRepository

    control = tmp_path / "control"
    _project(control)
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Main")
    with_gate = service.upsert_item(
        "sample",
        roadmap["id"],
        expected_revision=roadmap["revision"],
        raw_item={"kind": "gate", "title": "Review", "lane": "Decision"},
    )
    gate_row = with_gate["items"][0]
    original_save = RoadmapRepository.save
    failed = False

    def fail_once(self, roadmap, *, expected_revision):  # type: ignore[no-untyped-def]
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated split write")
        return original_save(self, roadmap, expected_revision=expected_revision)

    monkeypatch.setattr(RoadmapRepository, "save", fail_once)
    with pytest.raises(OSError):
        service.move_item(
            "sample",
            with_gate["id"],
            expected_revision=with_gate["revision"],
            expected_project_execution_revision=with_gate["project_execution_revision"],
            item_id=gate_row["id"],
            lane="Moved",
            target="2026-12-01",
        )
    monkeypatch.setattr(RoadmapRepository, "save", original_save)

    descriptor = service._project("sample")  # type: ignore[attr-defined]
    repository = RoadmapRepository(descriptor, state_root=tmp_path / "state")
    current = repository.load(with_gate["id"])
    repository.save(
        replace(current, description="Independent edit"),
        expected_revision=current.revision,
    )

    snapshot = ProjectExecutionGuiService(
        control_root=control,
        state_root=tmp_path / "state",
    ).snapshot("sample")
    assert snapshot["configured"] is True
    assert snapshot["coordination"]["pending"] is True
    assert snapshot["coordination"]["divergent"] is True
    assert snapshot["coordination"]["safe_actions"] == []

    from execraft.gui.errors import GuiError
    gui = ProjectExecutionGuiService(control_root=control, state_root=tmp_path / "state")
    with pytest.raises(GuiError, match="requires operator resolution"):
        gui.set_mode(
            "sample",
            mode="observe",
            expected_revision=snapshot["definition_revision"],
        )


def test_coordination_status_exposes_safe_partial_roll_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execraft.roadmap.coordination import RoadmapCanonicalCoordinator
    from execraft.roadmap.repository import RoadmapRepository

    control = tmp_path / "control"
    _project(control)
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Main")
    with_gate = service.upsert_item(
        "sample",
        roadmap["id"],
        expected_revision=roadmap["revision"],
        raw_item={"kind": "gate", "title": "Review", "lane": "Decision"},
    )
    gate_row = with_gate["items"][0]
    original_save = RoadmapRepository.save
    failed = False

    def fail_once(self, roadmap, *, expected_revision):  # type: ignore[no-untyped-def]
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated split write")
        return original_save(self, roadmap, expected_revision=expected_revision)

    monkeypatch.setattr(RoadmapRepository, "save", fail_once)
    with pytest.raises(OSError):
        service.move_item(
            "sample",
            with_gate["id"],
            expected_revision=with_gate["revision"],
            expected_project_execution_revision=with_gate["project_execution_revision"],
            item_id=gate_row["id"],
            lane="Moved",
            target="2026-12-01",
        )
    monkeypatch.setattr(RoadmapRepository, "save", original_save)

    descriptor = service._project("sample")  # type: ignore[attr-defined]
    repository = RoadmapRepository(descriptor, state_root=tmp_path / "state")
    status = RoadmapCanonicalCoordinator(state_root=tmp_path / "state").status(
        project=descriptor,
        roadmaps=repository,
    )
    assert status.pending is True
    assert status.project_execution.state == "applied"
    assert status.roadmap.state == "before"
    assert status.safe_actions == ("retry_roll_forward",)
    assert status.automatic_action_available is True


def test_project_execution_gui_snapshot_reconciles_pending_roadmap_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execraft.gui.project_execution import ProjectExecutionGuiService
    from execraft.roadmap.coordination import RoadmapCoordinationStore
    from execraft.roadmap.repository import RoadmapRepository

    control = tmp_path / "control"
    _project(control)
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Main")
    with_gate = service.upsert_item(
        "sample",
        roadmap["id"],
        expected_revision=roadmap["revision"],
        raw_item={"kind": "gate", "title": "Review", "lane": "Decision"},
    )
    gate_row = with_gate["items"][0]
    original_save = RoadmapRepository.save
    failed = False

    def fail_once(self, roadmap, *, expected_revision):  # type: ignore[no-untyped-def]
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated restart window")
        return original_save(self, roadmap, expected_revision=expected_revision)

    monkeypatch.setattr(RoadmapRepository, "save", fail_once)
    with pytest.raises(OSError):
        service.move_item(
            "sample",
            with_gate["id"],
            expected_revision=with_gate["revision"],
            expected_project_execution_revision=with_gate["project_execution_revision"],
            item_id=gate_row["id"],
            lane="Moved",
            target="2026-12-01",
        )
    monkeypatch.setattr(RoadmapRepository, "save", original_save)

    assert RoadmapCoordinationStore(tmp_path / "state", "sample").load_pending() is not None
    gui = ProjectExecutionGuiService(
        control_root=control,
        state_root=tmp_path / "state",
    )
    snapshot = gui.snapshot("sample")
    assert snapshot["configured"] is True
    assert RoadmapCoordinationStore(tmp_path / "state", "sample").load_pending() is None
    recovered = service.get("sample", with_gate["id"])
    assert next(item for item in recovered["items"] if item["kind"] == "gate")["lane"] == "Moved"


def test_coordination_abandons_prepared_intent_when_first_canonical_write_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execraft.roadmap.coordination import RoadmapCoordinationStore

    control = tmp_path / "control"
    project = _project(control)
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Main")
    with_gate = service.upsert_item(
        "sample",
        roadmap["id"],
        expected_revision=roadmap["revision"],
        raw_item={"kind": "gate", "title": "Review", "lane": "Decision"},
    )
    gate_row = with_gate["items"][0]
    original_save = ProjectExecutionRepository.save

    def conflict(self, definition, *, expected_revision):  # type: ignore[no-untyped-def]
        raise ProjectExecutionConflictError("simulated canonical race")

    monkeypatch.setattr(ProjectExecutionRepository, "save", conflict)
    with pytest.raises(ProjectExecutionConflictError, match="canonical race"):
        service.move_item(
            "sample",
            with_gate["id"],
            expected_revision=with_gate["revision"],
            expected_project_execution_revision=with_gate["project_execution_revision"],
            item_id=gate_row["id"],
            lane="Moved",
            target="2026-12-01",
        )
    monkeypatch.setattr(ProjectExecutionRepository, "save", original_save)

    assert RoadmapCoordinationStore(tmp_path / "state", "sample").load_pending() is None
    durable = service.get("sample", with_gate["id"])
    assert durable["revision"] == with_gate["revision"]
    gate = ProjectExecutionRepository(project).load().gate_index[gate_row["project_asset_id"]]
    assert gate.schedule.target == ""
    journal = (
        tmp_path / "state" / "roadmap-coordination" / "sample" / "journal.jsonl"
    ).read_text(encoding="utf-8")
    assert '"phase":"aborted"' in journal


def test_coordination_operator_abort_is_allowed_only_before_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execraft.roadmap.coordination import RoadmapCanonicalCoordinator, RoadmapCoordinationStore
    from execraft.roadmap.repository import RoadmapRepository

    control = tmp_path / "control"
    _project(control)
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Main")
    with_gate = service.upsert_item(
        "sample",
        roadmap["id"],
        expected_revision=roadmap["revision"],
        raw_item={"kind": "gate", "title": "Review", "lane": "Decision"},
    )
    gate_row = with_gate["items"][0]
    original_save = ProjectExecutionRepository.save

    def crash_before_first_write(self, definition, *, expected_revision):  # type: ignore[no-untyped-def]
        raise OSError("simulated crash before first coordinated write")

    monkeypatch.setattr(ProjectExecutionRepository, "save", crash_before_first_write)
    with pytest.raises(OSError, match="before first coordinated write"):
        service.move_item(
            "sample",
            with_gate["id"],
            expected_revision=with_gate["revision"],
            expected_project_execution_revision=with_gate["project_execution_revision"],
            item_id=gate_row["id"],
            lane="Moved",
            target="2026-12-01",
        )
    monkeypatch.setattr(ProjectExecutionRepository, "save", original_save)

    descriptor = service._project("sample")  # type: ignore[attr-defined]
    repository = RoadmapRepository(descriptor, state_root=tmp_path / "state")
    coordinator = RoadmapCanonicalCoordinator(state_root=tmp_path / "state")
    status = coordinator.status(project=descriptor, roadmaps=repository)
    assert status.safe_actions == ("retry_roll_forward", "abort")

    resolved = coordinator.resolve(project=descriptor, roadmaps=repository, action="abort")
    assert resolved.pending is False
    assert RoadmapCoordinationStore(tmp_path / "state", "sample").load_pending() is None


def test_coordination_accepts_only_exact_already_applied_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execraft.roadmap.coordination import (
        RoadmapCanonicalCoordinator,
        RoadmapCoordinationConflictError,
        RoadmapCoordinationStore,
    )
    from execraft.roadmap.repository import RoadmapRepository

    control = tmp_path / "control"
    _project(control)
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Main")
    with_gate = service.upsert_item(
        "sample",
        roadmap["id"],
        expected_revision=roadmap["revision"],
        raw_item={"kind": "gate", "title": "Review", "lane": "Decision"},
    )
    gate_row = with_gate["items"][0]
    original_complete = RoadmapCoordinationStore.complete
    failed = False

    def crash_before_finalize(self, intent):  # type: ignore[no-untyped-def]
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated crash before intent finalize")
        return original_complete(self, intent)

    monkeypatch.setattr(RoadmapCoordinationStore, "complete", crash_before_finalize)
    with pytest.raises(OSError, match="intent finalize"):
        service.move_item(
            "sample",
            with_gate["id"],
            expected_revision=with_gate["revision"],
            expected_project_execution_revision=with_gate["project_execution_revision"],
            item_id=gate_row["id"],
            lane="Moved",
            target="2026-12-01",
        )
    monkeypatch.setattr(RoadmapCoordinationStore, "complete", original_complete)

    descriptor = service._project("sample")  # type: ignore[attr-defined]
    repository = RoadmapRepository(descriptor, state_root=tmp_path / "state")
    coordinator = RoadmapCanonicalCoordinator(state_root=tmp_path / "state")
    status = coordinator.status(project=descriptor, roadmaps=repository)
    assert status.roadmap.state == "applied"
    assert status.project_execution.state == "applied"
    assert status.safe_actions == ("accept_applied",)

    with pytest.raises(RoadmapCoordinationConflictError, match="not safe"):
        coordinator.resolve(project=descriptor, roadmaps=repository, action="force")

    resolved = coordinator.resolve(
        project=descriptor,
        roadmaps=repository,
        action="accept_applied",
    )
    assert resolved.pending is False


def test_terminal_aborted_intent_is_finalized_without_replaying_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execraft.roadmap.coordination import RoadmapCanonicalCoordinator, RoadmapCoordinationStore
    from execraft.roadmap.repository import RoadmapRepository

    control = tmp_path / "control"
    project_dir = _project(control)
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Main")
    with_gate = service.upsert_item(
        "sample",
        roadmap["id"],
        expected_revision=roadmap["revision"],
        raw_item={"kind": "gate", "title": "Review", "lane": "Decision"},
    )
    gate_row = with_gate["items"][0]
    original_save = ProjectExecutionRepository.save

    def crash_before_first_write(self, definition, *, expected_revision):  # type: ignore[no-untyped-def]
        raise OSError("simulated crash before first coordinated write")

    monkeypatch.setattr(ProjectExecutionRepository, "save", crash_before_first_write)
    with pytest.raises(OSError):
        service.move_item(
            "sample",
            with_gate["id"],
            expected_revision=with_gate["revision"],
            expected_project_execution_revision=with_gate["project_execution_revision"],
            item_id=gate_row["id"],
            lane="Moved",
            target="2026-12-01",
        )
    monkeypatch.setattr(ProjectExecutionRepository, "save", original_save)

    descriptor = service._project("sample")  # type: ignore[attr-defined]
    repository = RoadmapRepository(descriptor, state_root=tmp_path / "state")
    coordinator = RoadmapCanonicalCoordinator(state_root=tmp_path / "state")
    original_append = RoadmapCoordinationStore._append_journal

    def crash_during_abort(self, intent, *, reason=""):  # type: ignore[no-untyped-def]
        raise OSError("simulated crash during terminal cleanup")

    monkeypatch.setattr(RoadmapCoordinationStore, "_append_journal", crash_during_abort)
    with pytest.raises(OSError, match="terminal cleanup"):
        coordinator.resolve(project=descriptor, roadmaps=repository, action="abort")
    monkeypatch.setattr(RoadmapCoordinationStore, "_append_journal", original_append)

    pending = RoadmapCoordinationStore(tmp_path / "state", "sample").load_pending()
    assert pending is not None
    assert pending.phase == "aborted"
    status = coordinator.status(project=descriptor, roadmaps=repository)
    assert status.safe_actions == ("finalize_terminal",)

    recovered = _service(tmp_path).get("sample", with_gate["id"])
    assert recovered["coordination"]["pending"] is False
    assert recovered["revision"] == with_gate["revision"]
    gate = ProjectExecutionRepository(project_dir).load().gate_index[gate_row["project_asset_id"]]
    assert gate.schedule.target == ""


def test_coordination_forensics_capture_three_way_semantic_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execraft.roadmap.coordination import RoadmapCanonicalCoordinator
    from execraft.roadmap.repository import RoadmapRepository

    control = tmp_path / "control"
    _project(control)
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Main")
    with_gate = service.upsert_item(
        "sample",
        roadmap["id"],
        expected_revision=roadmap["revision"],
        raw_item={
            "id": "review-gate",
            "kind": "gate",
            "title": "Review",
            "lane": "Decision",
            "schedule": {"target": "2026-10-01"},
        },
    )
    gate_row = with_gate["items"][0]
    original_save = RoadmapRepository.save
    failed = False

    def fail_once(self, roadmap, *, expected_revision):  # type: ignore[no-untyped-def]
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated split write for forensic inspection")
        return original_save(self, roadmap, expected_revision=expected_revision)

    monkeypatch.setattr(RoadmapRepository, "save", fail_once)
    with pytest.raises(OSError, match="forensic inspection"):
        service.move_item(
            "sample",
            with_gate["id"],
            expected_revision=with_gate["revision"],
            expected_project_execution_revision=with_gate["project_execution_revision"],
            item_id=gate_row["id"],
            lane="Command",
            target="2026-11-15",
        )
    monkeypatch.setattr(RoadmapRepository, "save", original_save)

    descriptor = service._project("sample")  # type: ignore[attr-defined]
    repository = RoadmapRepository(descriptor, state_root=tmp_path / "state")
    status = RoadmapCanonicalCoordinator(state_root=tmp_path / "state").status(
        project=descriptor,
        roadmaps=repository,
    )
    forensic = status.as_mapping()["forensics"]
    assert forensic["available"] is True
    assert forensic["truncated"] is False

    roadmap_subject = forensic["roadmap"]["subjects"][0]
    assert roadmap_subject["identity"] == {"item_id": gate_row["id"]}
    assert roadmap_subject["before"]["lane"] == "Decision"
    assert roadmap_subject["desired"]["lane"] == "Command"
    assert roadmap_subject["current"]["lane"] == "Decision"

    execution_subject = forensic["project_execution"]["subjects"][0]
    assert execution_subject["identity"] == {"kind": "gate", "asset_id": "review-gate"}
    assert execution_subject["before"]["schedule"]["target"] == "2026-10-01"
    assert execution_subject["desired"]["schedule"]["target"] == "2026-11-15"
    assert execution_subject["current"]["schedule"]["target"] == "2026-11-15"


def test_coordination_recognizes_later_revisioned_manual_convergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execraft.roadmap.coordination import RoadmapCanonicalCoordinator, RoadmapCoordinationStore
    from execraft.roadmap.models import Roadmap
    from execraft.roadmap.repository import RoadmapRepository

    control = tmp_path / "control"
    _project(control)
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Main")
    with_gate = service.upsert_item(
        "sample",
        roadmap["id"],
        expected_revision=roadmap["revision"],
        raw_item={"id": "review-gate", "kind": "gate", "title": "Review", "lane": "Decision"},
    )
    gate_row = with_gate["items"][0]
    original_save = RoadmapRepository.save
    failed = False

    def fail_once(self, roadmap, *, expected_revision):  # type: ignore[no-untyped-def]
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated split write before manual convergence")
        return original_save(self, roadmap, expected_revision=expected_revision)

    monkeypatch.setattr(RoadmapRepository, "save", fail_once)
    with pytest.raises(OSError, match="manual convergence"):
        service.move_item(
            "sample",
            with_gate["id"],
            expected_revision=with_gate["revision"],
            expected_project_execution_revision=with_gate["project_execution_revision"],
            item_id=gate_row["id"],
            lane="Moved",
            target="2026-12-01",
        )
    monkeypatch.setattr(RoadmapRepository, "save", original_save)

    descriptor = service._project("sample")  # type: ignore[attr-defined]
    repository = RoadmapRepository(descriptor, state_root=tmp_path / "state")
    current = repository.load(with_gate["id"])
    divergent = replace(current, description="independent planning edit")
    saved_divergent = repository.save(divergent, expected_revision=current.revision)

    coordinator = RoadmapCanonicalCoordinator(state_root=tmp_path / "state")
    assert coordinator.status(project=descriptor, roadmaps=repository).roadmap.state == "divergent"
    pending = RoadmapCoordinationStore(tmp_path / "state", "sample").load_pending()
    assert pending is not None

    # A normal revisioned correction restores the exact recorded desired
    # semantic content, but necessarily lands at a later revision.
    desired = Roadmap.from_mapping(pending.desired_roadmap)
    converged = repository.save(desired, expected_revision=saved_divergent.revision)
    assert converged.revision == pending.expected_roadmap_revision + 2

    status = coordinator.status(project=descriptor, roadmaps=repository)
    assert status.roadmap.state == "converged"
    assert status.project_execution.state == "applied"
    assert status.safe_actions == ("accept_applied",)
    assert "convergence" in status.message

    resolved = coordinator.resolve(project=descriptor, roadmaps=repository, action="accept_applied")
    assert resolved.pending is False


def test_coordination_journal_history_is_bounded_and_terminal(tmp_path: Path) -> None:
    from execraft.roadmap.coordination import RoadmapCoordinationStore

    control = tmp_path / "control"
    _project(control)
    service = _service(tmp_path)
    roadmap = service.create("sample", title="Main")
    first = service.upsert_item(
        "sample",
        roadmap["id"],
        expected_revision=roadmap["revision"],
        raw_item={"id": "gate-a", "kind": "gate", "title": "Gate A"},
    )
    second = service.upsert_item(
        "sample",
        first["id"],
        expected_revision=first["revision"],
        expected_project_execution_revision=first["project_execution_revision"],
        raw_item={"id": "gate-b", "kind": "gate", "title": "Gate B"},
    )
    assert second["revision"] > first["revision"]

    store = RoadmapCoordinationStore(tmp_path / "state", "sample")
    history = store.read_journal(limit=1)
    assert len(history) == 1
    assert history[0]["phase"] == "complete"
    assert history[0]["operation"] == "upsert_gate"
    assert history[0]["roadmap_revision"] == second["revision"]
    assert history[0]["project_execution_revision"] == second["project_execution_revision"]
    assert history[0]["timestamp"]
