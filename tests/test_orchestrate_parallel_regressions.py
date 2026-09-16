from __future__ import annotations

import subprocess
from pathlib import Path

from execraft.orchestrate.models import PlanGraph, TaskExecutionState, WorkPackage, WorkPackageStage
from execraft.orchestrate.normalizer import NormalizationReport
from execraft.orchestrate.orchestrator import OrchestrationConfig, ProjectOrchestrator
from execraft.orchestrate.scheduler import AgentCapability, Availability, StructuredHandoff


def _init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True
    )
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=path, check=True)


class _Writer:
    def __init__(self, provider_id: str, filename: str) -> None:
        self.provider_id = provider_id
        self.filename = filename
        self.availability = Availability.AVAILABLE
        self.capabilities = {AgentCapability.IMPLEMENT}

    def execute(self, handoff: StructuredHandoff) -> dict:
        (Path(handoff.working_directory) / self.filename).write_text(
            f"{self.provider_id}\n", encoding="utf-8"
        )
        return {
            "ok": True,
            "status": "implemented",
            "summary": f"created {self.filename}",
            "acceptance_evidence": {},
        }


def test_owned_dirty_sibling_does_not_block_next_parallel_wave(tmp_path: Path) -> None:
    repositories = {name: tmp_path / name for name in ("a", "b", "c")}
    for path in repositories.values():
        _init_repo(path)
    (repositories["a"] / "owned.txt").write_text("pending verification\n", encoding="utf-8")

    owner = WorkPackage(
        id="WP1__a",
        title="Owner",
        parent_id="WP1",
        execution_mode="standard_shard",
        stage=WorkPackageStage.FAST_VERIFY,
        affected_repositories=["a"],
        parallel_safe=True,
        write_scope=["owned.txt"],
    )
    siblings = [
        WorkPackage(
            id=f"WP1__{name}",
            title=name.upper(),
            parent_id="WP1",
            execution_mode="standard_shard",
            stage=WorkPackageStage.PREPARE,
            affected_repositories=[name],
            parallel_safe=True,
            write_scope=[f"{name}.txt"],
        )
        for name in ("b", "c")
    ]
    parent = WorkPackage(
        id="WP1",
        title="Aggregate",
        execution_mode="aggregate",
        stage=WorkPackageStage.REGRESSION_VERIFY,
        dependencies=[owner.id, *(item.id for item in siblings)],
    )
    orchestrator = ProjectOrchestrator(
        "owned-dirty-wave",
        config=OrchestrationConfig(
            state_dir=tmp_path / "state",
            auto_commit=False,
            require_verification=False,
            require_repository_changes=False,
            parallel_shards_enabled=True,
            parallel_shard_max_workers=2,
            rotate_agents=False,
        ),
        repository_paths=repositories,
        workspace_root=tmp_path,
    )
    orchestrator.initialize_graph(
        PlanGraph(work_packages=[parent, owner, *siblings]), NormalizationReport()
    )
    orchestrator.transition_to(TaskExecutionState.RUNNING)
    for name in ("b", "c"):
        writer = _Writer(f"writer-{name}", f"{name}.txt")
        setattr(writer, "_execraft_concurrency_group", f"writer-{name}")
        orchestrator.register_agent(writer)
    orchestrator._set_parallel_dirty_owners({"a": owner.id})

    wave = orchestrator._build_parallel_wave(siblings, set())
    assert len(wave) == 2
    assert all(item.stage == WorkPackageStage.PREPARE for item in siblings)

    assert orchestrator._shard_waves.run(siblings, set()) is True
    assert orchestrator.state == TaskExecutionState.RUNNING
    assert all(item.stage == WorkPackageStage.FAST_VERIFY for item in siblings)
    assert orchestrator._parallel_dirty_owners()["a"] == owner.id
