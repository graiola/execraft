from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from execraft.project import load_project
from execraft.project_execution.repository import ProjectExecutionRepository
from execraft.roadmap.migration import RoadmapV1Migrator
from execraft.roadmap.models import Roadmap, RoadmapError, RoadmapItem, RoadmapSchedule
from execraft.roadmap.repository import RoadmapRepository


def _project(tmp_path: Path):
    project_dir = tmp_path / "projects" / "sample"
    project_dir.mkdir(parents=True)
    (project_dir / "project.yaml").write_text(
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
    project = load_project(project_dir)
    repository = RoadmapRepository(project, state_root=tmp_path / "state")
    return project_dir, project, repository


def test_v1_migration_creates_canonical_assets_and_reference_only_v2(tmp_path: Path):
    project_dir, project, repository = _project(tmp_path)
    repository.create(
        Roadmap(
            id="main",
            title="Main",
            schema_version=1,
            items=(
                RoadmapItem(
                    "phase-a",
                    "phase",
                    title="Phase A",
                    schedule=RoadmapSchedule(target="2026-11-01"),
                ),
                RoadmapItem("gate-a", "gate", title="Gate A"),
                RoadmapItem("milestone-a", "milestone", title="MVP"),
            ),
        )
    )

    migrator = RoadmapV1Migrator(project=project, roadmaps=repository)
    preview = migrator.preview("main")
    assert preview.can_apply
    assert not preview.resumed

    migrated = migrator.apply("main", expected_revision=1)
    assert migrated.schema_version == 2

    persisted = yaml.safe_load(repository.path("main").read_text(encoding="utf-8"))
    assert all("project_asset_id" in row for row in persisted["items"])
    assert all("title" not in row and "schedule" not in row for row in persisted["items"])

    definition = ProjectExecutionRepository(project_dir).load()
    assert definition.phase_index["phase-a"].schedule.target == "2026-11-01"
    assert definition.gate_index["gate-a"].criteria[0].type == "human_approval"


def test_identity_allocation_is_independent_of_migration_order(tmp_path: Path):
    _, project, repository = _project(tmp_path)
    for roadmap_id in ("alpha", "beta"):
        repository.create(
            Roadmap(
                id=roadmap_id,
                title=roadmap_id.title(),
                schema_version=1,
                items=(RoadmapItem("shared", "phase", title="Shared label"),),
            )
        )

    migrator = RoadmapV1Migrator(project=project, roadmaps=repository)
    alpha = migrator.preview("alpha")
    beta = migrator.preview("beta")

    assert alpha.items[0].project_asset_id == "alpha-shared"
    assert beta.items[0].project_asset_id == "beta-shared"

    # Migrating beta first must not alter alpha's identity decision.
    migrator.apply("beta", expected_revision=1)
    assert migrator.preview("alpha").items[0].project_asset_id == "alpha-shared"


def test_interrupted_migration_resumes_prepared_identity_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project_dir, project, repository = _project(tmp_path)
    repository.create(
        Roadmap(
            id="main",
            title="Main",
            schema_version=1,
            items=(RoadmapItem("phase-a", "phase", title="Phase A"),),
        )
    )
    migrator = RoadmapV1Migrator(project=project, roadmaps=repository)
    original_save = repository.save

    def fail_after_project_assets(*args, **kwargs):
        raise OSError("simulated crash before Roadmap rewrite")

    monkeypatch.setattr(repository, "save", fail_after_project_assets)
    with pytest.raises(OSError, match="simulated crash"):
        migrator.apply("main", expected_revision=1)

    marker = project_dir / ".migrations" / "roadmap-v1-main.json"
    prepared = json.loads(marker.read_text(encoding="utf-8"))
    assert prepared["status"] == "prepared"
    assert ProjectExecutionRepository(project_dir).load().phase_index["phase-a"]

    monkeypatch.setattr(repository, "save", original_save)
    resumed = RoadmapV1Migrator(project=project, roadmaps=repository)
    preview = resumed.preview("main")
    assert preview.resumed
    assert preview.items[0].project_asset_id == "phase-a"

    migrated = resumed.apply("main", expected_revision=1)
    assert migrated.schema_version == 2
    complete = json.loads(marker.read_text(encoding="utf-8"))
    assert complete["status"] == "complete"


def test_prepared_migration_rejects_out_of_band_source_edit(tmp_path: Path):
    _, project, repository = _project(tmp_path)
    repository.create(
        Roadmap(
            id="main",
            title="Main",
            schema_version=1,
            items=(RoadmapItem("phase-a", "phase", title="Phase A"),),
        )
    )
    migrator = RoadmapV1Migrator(project=project, roadmaps=repository)
    preview = migrator.preview("main")
    migrator._write_prepared(preview)  # exercise restart contract directly

    raw = yaml.safe_load(repository.path("main").read_text(encoding="utf-8"))
    raw["items"][0]["description"] = "changed outside optimistic save"
    repository.path("main").write_text(yaml.safe_dump(raw), encoding="utf-8")

    resumed = RoadmapV1Migrator(project=project, roadmaps=repository).preview("main")
    assert not resumed.can_apply
    assert any("content changed" in conflict for conflict in resumed.conflicts)
    with pytest.raises(RoadmapError, match="conflicts"):
        RoadmapV1Migrator(project=project, roadmaps=repository).apply(
            "main", expected_revision=1
        )


def test_roadmap_projection_exposes_project_execution_revision(tmp_path: Path) -> None:
    from execraft.project_execution.models import ProjectExecutionDefinition, ProjectPhase
    from execraft.roadmap.service import RoadmapService

    project_dir, project, repository = _project(tmp_path)
    ProjectExecutionRepository(project_dir).create(
        ProjectExecutionDefinition(
            project="sample",
            phases=(ProjectPhase("phase-a", "Phase A"),),
        )
    )
    repository.create(
        Roadmap(
            id="main",
            title="Main",
            schema_version=2,
            items=(
                RoadmapItem(
                    "phase-node",
                    "phase",
                    project_asset_id="phase-a",
                ),
            ),
        )
    )
    service = RoadmapService(
        control_root=tmp_path,
        state_root=tmp_path / "state",
        active_task=lambda: None,
    )

    projected = service.get("sample", "main")

    assert projected["project_execution_revision"] == 1
    assert projected["items"][0]["project_asset_id"] == "phase-a"
    assert projected["items"][0]["title"] == "Phase A"


def test_roadmap_canonical_schedule_move_honors_project_execution_revision(
    tmp_path: Path,
) -> None:
    from execraft.project_execution.models import (
        ProjectExecutionConflictError,
        ProjectExecutionDefinition,
        ProjectPhase,
    )
    from execraft.project_execution.service import ProjectExecutionService
    from execraft.roadmap.service import RoadmapService

    project_dir, project, repository = _project(tmp_path)
    definition_repository = ProjectExecutionRepository(project_dir)
    definition_repository.create(
        ProjectExecutionDefinition(
            project="sample",
            phases=(ProjectPhase("phase-a", "Phase A"),),
        )
    )
    repository.create(
        Roadmap(
            id="main",
            title="Main",
            schema_version=2,
            items=(
                RoadmapItem(
                    "phase-node",
                    "phase",
                    project_asset_id="phase-a",
                ),
            ),
        )
    )
    project_execution = ProjectExecutionService(definition_repository)
    current = project_execution.get()
    project_execution.upsert_phase(
        ProjectPhase("phase-a", "Phase A renamed"),
        expected_revision=current.revision,
    )
    service = RoadmapService(
        control_root=tmp_path,
        state_root=tmp_path / "state",
        active_task=lambda: None,
    )

    with pytest.raises(ProjectExecutionConflictError):
        service.move_item(
            "sample",
            "main",
            expected_revision=1,
            expected_project_execution_revision=1,
            item_id="phase-node",
            lane="General",
            start="2026-10-01",
            target="2026-10-31",
            placement="end",
        )

    moved = service.move_item(
        "sample",
        "main",
        expected_revision=1,
        expected_project_execution_revision=2,
        item_id="phase-node",
        lane="General",
        start="2026-10-01",
        target="2026-10-31",
        placement="end",
    )

    assert moved["project_execution_revision"] == 3
    phase = definition_repository.load().phase_index["phase-a"]
    assert phase.schedule.as_mapping() == {
        "start": "2026-10-01",
        "target": "2026-10-31",
    }
