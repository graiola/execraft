"""WP2 regression coverage for versioned task-definition replanning."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from execraft.repository_sync.transaction import (
    RepositorySyncRepositoryState,
    RepositorySyncTransaction,
)
from execraft.onboarding.start_models import ProviderChoice
from execraft.onboarding.task_definition import (
    TaskDefinitionDriftError,
    TaskDefinitionInput,
    TaskDefinitionService,
)
from execraft.orchestrate.identity import resolve_storage_identity
from execraft.orchestrate.orchestrator import OrchestrationConfig, ProjectOrchestrator
from execraft.orchestrate.models import (
    AcceptanceCriterion,
    PlanGraph,
    TaskExecutionState,
    TaskExecutionStateRecord,
    OrchestrateError,
    WorkPackage,
    WorkPackageStage,
)
from execraft.project import ProjectDescriptor, ProjectRepository
from execraft.replan import ReplanConflictError, ReplanError
from execraft.replan.service import ReplanInputs, ReplanService
from execraft.workspace.task_git import RepositorySpec, TaskManifest, utc_now, write_manifest
from execraft.workspace.workspace_git import WorkspaceRecord, write_workspace


def _graph(*, wp2_requirement: str = "Implement second", include_wp2: bool = True, replacement: str = "") -> str:
    packages = [
        {
            "id": "WP1",
            "title": "First",
            "dependencies": [],
            "requirements": ["Implement first"],
            "acceptance_criteria": [{"id": "wp1_done", "description": "First works"}],
            "affected_repositories": ["app"],
            "risk": "low",
            "priority": 100,
            "verification_profile": "focused",
        }
    ]
    if include_wp2:
        packages.append(
            {
                "id": "WP2",
                "title": "Second",
                "dependencies": ["WP1"],
                "requirements": [wp2_requirement],
                "acceptance_criteria": [{"id": "wp2_done", "description": "Second works"}],
                "affected_repositories": ["app"],
                "risk": "medium",
                "priority": 50,
                "verification_profile": "focused",
            }
        )
    if replacement:
        packages.append(
            {
                "id": replacement,
                "title": "Replacement second",
                "dependencies": ["WP1"],
                "requirements": ["Implement replacement second"],
                "acceptance_criteria": [{"id": "r_done", "description": "Replacement works"}],
                "affected_repositories": ["app"],
                "risk": "medium",
                "priority": 60,
                "verification_profile": "focused",
            }
        )
    return yaml.safe_dump(
        {"schema_version": 1, "source_document": "PLAN.md", "work_packages": packages},
        sort_keys=False,
    )


def _package_wp1(*, completed: bool = False) -> WorkPackage:
    return WorkPackage(
        id="WP1",
        title="First",
        requirements=["Implement first"],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="wp1_done",
                description="First works",
                verified=completed,
                evidence="tests passed" if completed else "",
            )
        ],
        affected_repositories=["app"],
        stage=WorkPackageStage.COMPLETED if completed else WorkPackageStage.PREPARE,
        status="completed" if completed else "pending",
        risk="low",
        priority=100,
        verification_profile="focused",
        implementation_summary="durable completed evidence" if completed else "",
        last_implementation={"summary": "implemented WP1"} if completed else {},
    )


def _package_wp2(*, active: bool = False) -> WorkPackage:
    return WorkPackage(
        id="WP2",
        title="Second",
        dependencies=["WP1"],
        requirements=["Implement second"],
        acceptance_criteria=[AcceptanceCriterion(id="wp2_done", description="Second works")],
        affected_repositories=["app"],
        stage=WorkPackageStage.IMPLEMENT if active else WorkPackageStage.PREPARE,
        status="running" if active else "pending",
        risk="medium",
        priority=50,
        verification_profile="focused",
        last_invocation_id="inv-active" if active else "",
        last_implementation={"summary": "partial"} if active else {},
    )


def _init_git(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "task/demo"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("# app\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)


@pytest.fixture
def replan_env(tmp_path: Path):
    control = tmp_path / "control"
    dossier = control / "projects" / "app" / "tasks" / "demo"
    dossier.mkdir(parents=True)
    state_root = tmp_path / "state"
    project = ProjectDescriptor(
        id="app",
        directory=control / "projects" / "app",
        repositories=(
            ProjectRepository(
                id="app",
                path=".",
                workspace_name="app",
                role="component",
                required=True,
                base_branch="main",
            ),
        ),
    )
    manifest = TaskManifest(
        schema_version=2,
        id="demo",
        project="app",
        title="Demo",
        status="in_progress",
        created_at=utc_now(),
        last_updated=utc_now(),
        branch_name="task/demo",
        repositories=[
            RepositorySpec(
                id="app",
                base_branch="main",
                task_branch="task/demo",
                role="component",
                required=True,
            )
        ],
    )
    definition = TaskDefinitionInput.from_contents(
        brief_markdown="# Brief: Demo\n\nImplement the task.\n",
        plan_markdown="# Plan: Demo\n\nM1 then WP2.\n",
        plan_graph_yaml=_graph(),
    )
    defs = TaskDefinitionService()
    prepared = defs.prepare(
        definition=definition,
        title="Demo",
        fallback_brief="",
        request_sha256="abc",
        created_at=utc_now(),
        allowed_repositories={"app"},
    )
    defs.materialize(dossier, prepared)
    (dossier / "TASK.yaml").write_text(
        yaml.safe_dump(manifest.as_mapping(), sort_keys=False), encoding="utf-8"
    )
    identity = resolve_storage_identity(
        state_root, project_id="app", task_id="demo"
    )

    def write_state(*packages: WorkPackage, state: TaskExecutionState = TaskExecutionState.RUNNING) -> TaskExecutionStateRecord:
        record = TaskExecutionStateRecord(
            project_id="app",
            state=state,
            plan_graph=PlanGraph(work_packages=list(packages)),
            started_at=utc_now(),
            last_transition_at=utc_now(),
            completed_packages=sum(p.stage == WorkPackageStage.COMPLETED for p in packages),
            total_packages=len(packages),
        )
        identity.state_dir.mkdir(parents=True, exist_ok=True)
        (identity.state_dir / "state.json").write_text(
            json.dumps(record.as_mapping(), indent=2), encoding="utf-8"
        )
        return record

    service = ReplanService(
        control_root=control,
        state_root=state_root,
        project=project,
        manifest=manifest,
        dossier=dossier,
    )
    return {
        "control": control,
        "dossier": dossier,
        "state_root": state_root,
        "project": project,
        "manifest": manifest,
        "identity": identity,
        "service": service,
        "write_state": write_state,
    }


def test_definition_drift_is_detected_and_requires_replan(replan_env) -> None:
    dossier = replan_env["dossier"]
    definitions = TaskDefinitionService()
    assert definitions.integrity_report(dossier).ok
    (dossier / "PLAN.md").write_text("# Plan: Demo\n\nManual edit.\n", encoding="utf-8")

    report = definitions.integrity_report(dossier)
    assert not report.ok
    assert report.changed_documents == ("PLAN.md",)
    with pytest.raises(TaskDefinitionDriftError, match="TASK_DEFINITION_DRIFT"):
        definitions.require_integrity(dossier)


def test_pending_package_can_change_and_completed_evidence_is_preserved(replan_env) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(completed=True), _package_wp2())
    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=_graph(wp2_requirement="Implement improved second")
            )
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )

    assert candidate.impact.applicable
    assert any(item.classification == "completed_preserved" for item in candidate.impact.packages)
    assert any(item.classification == "pending_changed" for item in candidate.impact.packages)
    result = service.apply_candidate(candidate.candidate_id)
    assert result.revision == 2

    state = json.loads((replan_env["identity"].state_dir / "state.json").read_text())
    packages = {item["id"]: item for item in state["plan_graph"]["work_packages"]}
    assert packages["WP1"]["stage"] == "completed"
    assert packages["WP1"]["acceptance_criteria"][0]["verified"] is True
    assert packages["WP1"]["last_implementation"]["summary"] == "implemented WP1"
    assert packages["WP2"]["requirements"] == ["Implement improved second"]
    metadata = yaml.safe_load((replan_env["dossier"] / "DEFINITION.yaml").read_text())
    assert metadata["revision"] == 2
    assert (result.revision_path / "STATE.before.json").is_file()


def test_initial_plan_candidate_can_complete_brief_only_definition(replan_env) -> None:
    from execraft.replan.agent import AgentReplanProposal

    service: ReplanService = replan_env["service"]
    dossier: Path = replan_env["dossier"]
    definitions = TaskDefinitionService()
    accepted_brief = (dossier / "BRIEF.md").read_text(encoding="utf-8")

    (dossier / "PLAN.graph.yaml").unlink()
    shutil.rmtree(dossier / "revisions" / "revision-0001")
    metadata_path = dossier / "DEFINITION.yaml"
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    metadata["sources"].pop("PLAN.graph.yaml")
    metadata["current"] = definitions.current_hashes(dossier)
    metadata["definition_sha256"] = definitions.definition_sha256(dossier)
    metadata["package_ids_seen"] = []
    metadata_path.write_text(
        yaml.safe_dump(metadata, sort_keys=False), encoding="utf-8"
    )

    class _InitialPlanner:
        def propose(self, **_kwargs):
            return AgentReplanProposal(
                consistent=True,
                consistency_summary="brief, plan, and graph are coherent",
                brief_markdown="# Brief: provider attempted rewrite\n",
                plan_markdown="# Plan: Demo\n\nGenerated implementation plan.\n",
                plan_graph_yaml=_graph(),
                package_mapping={},
                change_summary="generated initial executable plan",
                provider_id="local-qwen",
            )

    service.agent_replanner = _InitialPlanner()
    provider = type("Provider", (), {"available": True})()
    candidate = service.create_candidate(
        ReplanInputs(
            requested_change="Generate the initial plan from BRIEF.md",
            allow_incomplete_definition=True,
            preserve_current_brief=True,
        ),
        provider=provider,
    )

    assert candidate.impact.applicable
    assert candidate.brief_markdown == accepted_brief
    assert candidate.document_origins["BRIEF.md"] == "carried_forward"
    baseline = dossier / "revisions" / "revision-0001"
    assert (baseline / "BRIEF.md").is_file()
    assert not (baseline / "PLAN.graph.yaml").exists()

    result = service.apply_candidate(candidate.candidate_id)

    assert result.revision == 2
    assert (dossier / "BRIEF.md").read_text(encoding="utf-8") == accepted_brief
    assert (dossier / "PLAN.graph.yaml").is_file()
    assert definitions.integrity_report(dossier).ok


def test_completed_package_semantics_cannot_change(replan_env) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(completed=True), _package_wp2())
    raw = yaml.safe_load(_graph())
    raw["work_packages"][0]["requirements"] = ["Rewrite completed behavior"]
    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=yaml.safe_dump(raw, sort_keys=False)
            )
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )

    assert not candidate.impact.applicable
    assert any("completed package WP1 changed semantic contract" in item for item in candidate.impact.blockers)
    with pytest.raises(ReplanConflictError, match="blocked"):
        service.apply_candidate(candidate.candidate_id)


def test_started_package_cannot_change_in_place(replan_env) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(completed=True), _package_wp2(active=True))
    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=_graph(wp2_requirement="Changed after start")
            )
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )
    assert not candidate.impact.applicable
    assert any("cannot change in place" in item for item in candidate.impact.blockers)


def test_started_package_supersession_requires_clean_registered_workspace(replan_env, tmp_path: Path) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(completed=True), _package_wp2(active=True))
    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=_graph(include_wp2=False, replacement="M2R")
            ),
            package_mapping={"WP2": "M2R"},
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )
    assert candidate.impact.applicable
    assert candidate.impact.requires_clean_workspace == ("WP2",)
    with pytest.raises(ReplanConflictError, match="registered workspace"):
        service.apply_candidate(candidate.candidate_id)

    repo = tmp_path / "workspace" / "app"
    _init_git(repo)
    workspace_root = repo.parent
    record = WorkspaceRecord(
        schema_version=1,
        task_id="demo",
        created_at=utc_now(),
        source_root=str(repo),
        workspace_root=str(workspace_root),
        compose_project="",
        ros_domain_id=-1,
        port_offset=0,
        env_file=".ai-task.env",
        repositories=[
            {
                "id": "app",
                "source_path": str(repo),
                "worktree_path": str(repo),
                "branch": "task/demo",
                "role": "component",
                "mutability": "task_owned",
            }
        ],
        capabilities=[],
    )
    write_workspace(replan_env["control"], record)

    result = service.apply_candidate(candidate.candidate_id)
    assert result.superseded_active_packages == ("WP2",)
    state = json.loads((replan_env["identity"].state_dir / "state.json").read_text())
    assert [item["id"] for item in state["plan_graph"]["work_packages"]] == ["WP1", "M2R"]
    archived = json.loads((result.revision_path / "STATE.before.json").read_text())
    old_wp2 = next(item for item in archived["plan_graph"]["work_packages"] if item["id"] == "WP2")
    assert old_wp2["last_invocation_id"] == "inv-active"


def test_started_package_supersession_rejects_dirty_workspace(replan_env, tmp_path: Path) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(completed=True), _package_wp2(active=True))
    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=_graph(include_wp2=False, replacement="M2R")
            ),
            package_mapping={"WP2": "M2R"},
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )
    repo = tmp_path / "dirty-workspace" / "app"
    _init_git(repo)
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    write_workspace(
        replan_env["control"],
        WorkspaceRecord(
            schema_version=1,
            task_id="demo",
            created_at=utc_now(),
            source_root=str(repo),
            workspace_root=str(repo.parent),
            compose_project="",
            ros_domain_id=-1,
            port_offset=0,
            env_file=".ai-task.env",
            repositories=[
                {
                    "id": "app",
                    "source_path": str(repo),
                    "worktree_path": str(repo),
                    "branch": "task/demo",
                    "role": "component",
                    "mutability": "task_owned",
                }
            ],
        ),
    )
    with pytest.raises(ReplanConflictError, match="dirty task worktrees"):
        service.apply_candidate(candidate.candidate_id)


def test_manual_definition_edits_can_be_adopted_only_through_from_current_files(replan_env) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(), _package_wp2())
    dossier = replan_env["dossier"]
    (dossier / "PLAN.md").write_text("# Plan: Demo\n\nManual approved change.\n", encoding="utf-8")
    (dossier / "PLAN.graph.yaml").write_text(_graph(wp2_requirement="Manual second"), encoding="utf-8")

    with pytest.raises(TaskDefinitionDriftError):
        service.create_candidate(
            ReplanInputs(
                definition=TaskDefinitionInput.from_contents(plan_graph_yaml=_graph())
            ),
            provider=ProviderChoice(None, None, "unavailable"),
        )
    candidate = service.create_candidate(
        ReplanInputs(from_current_files=True, allow_structural_consistency=True),
        provider=ProviderChoice(None, None, "unavailable"),
    )
    assert candidate.accepts_live_drift
    assert candidate.consistency_mode == "structural"
    result = service.apply_candidate(candidate.candidate_id)
    assert result.revision == 2
    assert TaskDefinitionService().integrity_report(dossier).ok


def test_context_capsules_are_invalidated_after_apply(replan_env) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(), _package_wp2())
    capsules = replan_env["identity"].state_dir / "context-capsules"
    capsules.mkdir(parents=True)
    (capsules / "WP1.json").write_text("{}", encoding="utf-8")
    (capsules / "WP2.json").write_text("{}", encoding="utf-8")
    (capsules / "WP2.json.lock").write_text("", encoding="utf-8")
    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=_graph(wp2_requirement="New pending")
            )
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )
    result = service.apply_candidate(candidate.candidate_id)
    assert result.invalidated_capsules == 2
    assert not list(capsules.glob("*.json"))
    assert (capsules / "WP2.json.lock").is_file()


def test_candidate_creation_refuses_active_orchestrator_lock(replan_env) -> None:
    fcntl = pytest.importorskip("fcntl")
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(), _package_wp2())
    lock = replan_env["identity"].state_dir / "orchestrator.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ReplanConflictError, match="orchestrator/daemon is active"):
            service.create_candidate(
                ReplanInputs(
                    definition=TaskDefinitionInput.from_contents(plan_graph_yaml=_graph())
                ),
                provider=ProviderChoice(None, None, "not needed"),
            )
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def test_semantic_document_change_requires_ai_or_explicit_structural_mode(replan_env) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(), _package_wp2())
    with pytest.raises(ReplanError, match="ai-replan semantic consistency"):
        service.create_candidate(
            ReplanInputs(
                definition=TaskDefinitionInput.from_contents(
                    brief_markdown="# Brief: Demo\n\nChanged intent.\n",
                    plan_graph_yaml=_graph(),
                )
            ),
            provider=ProviderChoice(None, None, "no provider"),
        )

    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                brief_markdown="# Brief: Demo\n\nChanged intent.\n",
                plan_graph_yaml=_graph(),
            ),
            allow_structural_consistency=True,
        ),
        provider=ProviderChoice(None, None, "no provider"),
    )
    assert candidate.consistency_mode == "structural"
    assert "structural-only" in candidate.consistency_summary


def test_candidate_document_tampering_is_rejected(replan_env) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(), _package_wp2())
    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=_graph(wp2_requirement="New pending")
            )
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )
    (candidate.path / "PLAN.graph.yaml").write_text(_graph(wp2_requirement="tampered"), encoding="utf-8")

    with pytest.raises(ReplanError, match="changed after staging"):
        service.load_candidate(candidate.candidate_id)
    with pytest.raises(ReplanError, match="changed after staging"):
        service.apply_candidate(candidate.candidate_id)


def test_active_supersession_requires_genuinely_new_package_id(replan_env) -> None:
    service: ReplanService = replan_env["service"]
    active = _package_wp2(active=True)
    pending = WorkPackage(
        id="WP3",
        title="Third",
        dependencies=["WP1"],
        requirements=["Existing pending work"],
        acceptance_criteria=[AcceptanceCriterion(id="wp3_done", description="Third works")],
        affected_repositories=["app"],
        stage=WorkPackageStage.PREPARE,
        status="pending",
    )
    replan_env["write_state"](_package_wp1(completed=True), active, pending)
    raw = yaml.safe_load(_graph(include_wp2=False))
    raw["work_packages"].append(
        {
            "id": "WP3",
            "title": "Third",
            "dependencies": ["WP1"],
            "requirements": ["Existing pending work"],
            "acceptance_criteria": [{"id": "wp3_done", "description": "Third works"}],
            "affected_repositories": ["app"],
        }
    )
    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=yaml.safe_dump(raw, sort_keys=False)
            ),
            package_mapping={"WP2": "WP3"},
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )

    assert not candidate.impact.applicable
    assert any("genuinely new package ID" in item for item in candidate.impact.blockers)


def test_replan_recovery_restores_candidate_after_revision_move(replan_env, monkeypatch) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(), _package_wp2())
    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=_graph(wp2_requirement="New pending")
            )
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )
    original_mark = service._mark_transaction_committed

    def fail_after_revision_move(transaction, **_kwargs):
        raise OSError("simulated commit-marker failure")

    monkeypatch.setattr(service, "_mark_transaction_committed", fail_after_revision_move)
    with pytest.raises(Exception, match="rolled back"):
        service.apply_candidate(candidate.candidate_id)
    monkeypatch.setattr(service, "_mark_transaction_committed", original_mark)

    assert candidate.path.is_dir()
    assert not (replan_env["dossier"] / "revisions" / "revision-0002").exists()
    assert TaskDefinitionService().integrity_report(replan_env["dossier"]).ok
    assert not (replan_env["identity"].state_dir / "replan-transaction.yaml").exists()


def test_orchestrator_load_state_blocks_definition_drift_and_replan_transaction(replan_env) -> None:
    replan_env["write_state"](_package_wp1(), _package_wp2())
    orchestrator = ProjectOrchestrator(
        "demo",
        config=OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=replan_env["state_root"],
        ),
        task_dossier_dir=replan_env["dossier"],
        project_namespace="app",
    )
    assert orchestrator.load_state().total_packages == 2

    plan = replan_env["dossier"] / "PLAN.md"
    original = plan.read_text(encoding="utf-8")
    plan.write_text(original + "\nmanual drift\n", encoding="utf-8")
    with pytest.raises(OrchestrateError, match="TASK_DEFINITION_DRIFT"):
        orchestrator.load_state()
    plan.write_text(original, encoding="utf-8")

    marker = replan_env["identity"].state_dir / "replan-transaction.yaml"
    marker.write_text("schema_version: 1\nstatus: prepared\n", encoding="utf-8")
    with pytest.raises(OrchestrateError, match="REPLAN_TRANSACTION_INCOMPLETE"):
        orchestrator.load_state()


def _runtime_shard(parent_id: str = "WP2") -> WorkPackage:
    return WorkPackage(
        id=f"{parent_id}__api",
        title="API shard",
        dependencies=["WP1"],
        requirements=["Implement second"],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="wp2_done",
                description="Second works",
                verified=True,
                evidence="shard tests passed",
            )
        ],
        affected_repositories=["app"],
        stage=WorkPackageStage.COMPLETED,
        status="completed",
        risk="low",
        priority=51,
        verification_profile="focused",
        parent_id=parent_id,
        shard_key="api",
        generated_by="planner",
        execution_mode="standard",
        last_implementation={"summary": "completed shard"},
    )


def test_replan_preserves_runtime_generated_shards_for_unchanged_parent(replan_env) -> None:
    service: ReplanService = replan_env["service"]
    parent = _package_wp2(active=True)
    child = _runtime_shard()
    parent.stage = WorkPackageStage.REGRESSION_VERIFY
    parent.execution_mode = "aggregate"
    parent.dependencies = ["WP1", child.id]
    parent.shard_ids = [child.id]
    parent.decomposition_status = "expanded"
    parent.decomposition_plan_hash = "hash"
    replan_env["write_state"](_package_wp1(completed=True), parent, child)

    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(plan_graph_yaml=_graph())
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )
    assert candidate.impact.applicable
    classifications = {item.package_id: item.classification for item in candidate.impact.packages}
    assert classifications["WP2"] == "active_preserved"
    assert classifications[child.id] == "generated_preserved"

    service.apply_candidate(candidate.candidate_id)
    state = json.loads((replan_env["identity"].state_dir / "state.json").read_text())
    packages = {item["id"]: item for item in state["plan_graph"]["work_packages"]}
    assert set(packages) == {"WP1", "WP2", child.id}
    assert packages["WP2"]["execution_mode"] == "aggregate"
    assert packages["WP2"]["shard_ids"] == [child.id]
    assert packages[child.id]["last_implementation"]["summary"] == "completed shard"


def test_replan_retires_generated_shards_with_superseded_parent(replan_env, tmp_path: Path) -> None:
    service: ReplanService = replan_env["service"]
    parent = _package_wp2(active=True)
    child = _runtime_shard()
    parent.stage = WorkPackageStage.REGRESSION_VERIFY
    parent.execution_mode = "aggregate"
    parent.dependencies = ["WP1", child.id]
    parent.shard_ids = [child.id]
    replan_env["write_state"](_package_wp1(completed=True), parent, child)
    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=_graph(include_wp2=False, replacement="M2R")
            ),
            package_mapping={"WP2": "M2R"},
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )
    classifications = {item.package_id: item.classification for item in candidate.impact.packages}
    assert classifications[child.id] == "generated_retired"

    repo = tmp_path / "sharded-workspace" / "app"
    _init_git(repo)
    write_workspace(
        replan_env["control"],
        WorkspaceRecord(
            schema_version=1,
            task_id="demo",
            created_at=utc_now(),
            source_root=str(repo),
            workspace_root=str(repo.parent),
            compose_project="",
            ros_domain_id=-1,
            port_offset=0,
            env_file=".ai-task.env",
            repositories=[
                {
                    "id": "app",
                    "source_path": str(repo),
                    "worktree_path": str(repo),
                    "branch": "task/demo",
                    "role": "component",
                    "mutability": "task_owned",
                }
            ],
        ),
    )
    result = service.apply_candidate(candidate.candidate_id)
    state = json.loads((replan_env["identity"].state_dir / "state.json").read_text())
    assert [item["id"] for item in state["plan_graph"]["work_packages"]] == ["WP1", "M2R"]
    archived = json.loads((result.revision_path / "STATE.before.json").read_text())
    assert child.id in {item["id"] for item in archived["plan_graph"]["work_packages"]}


def test_candidate_graph_rejects_runtime_and_fabricated_evidence_fields(replan_env) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(), _package_wp2())
    raw = yaml.safe_load(_graph())
    raw["work_packages"][1]["stage"] = "completed"
    with pytest.raises(ReplanError, match="non-declarative/runtime fields: stage"):
        service.create_candidate(
            ReplanInputs(
                definition=TaskDefinitionInput.from_contents(
                    plan_graph_yaml=yaml.safe_dump(raw, sort_keys=False)
                )
            ),
            provider=ProviderChoice(None, None, "not needed"),
        )

    raw = yaml.safe_load(_graph())
    raw["work_packages"][1]["acceptance_criteria"][0]["verified"] = True
    raw["work_packages"][1]["acceptance_criteria"][0]["evidence"] = "fabricated"
    with pytest.raises(ReplanError, match="runtime/evidence fields"):
        service.create_candidate(
            ReplanInputs(
                definition=TaskDefinitionInput.from_contents(
                    plan_graph_yaml=yaml.safe_dump(raw, sort_keys=False)
                )
            ),
            provider=ProviderChoice(None, None, "not needed"),
        )


def test_committed_replan_recovers_forward_after_post_commit_failure(replan_env, monkeypatch) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(), _package_wp2())
    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=_graph(wp2_requirement="Committed pending")
            )
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )
    original_complete = service._complete_committed_transaction

    def fail_post_commit(transaction, *, state):
        raise OSError("simulated post-commit bookkeeping failure")

    monkeypatch.setattr(service, "_complete_committed_transaction", fail_post_commit)
    with pytest.raises(Exception, match="durably committed"):
        service.apply_candidate(candidate.candidate_id)
    marker = replan_env["identity"].state_dir / "replan-transaction.yaml"
    transaction = yaml.safe_load(marker.read_text(encoding="utf-8"))
    assert transaction["status"] == "committed"
    assert (replan_env["dossier"] / "revisions" / "revision-0002").is_dir()
    assert not candidate.path.exists()
    metadata = yaml.safe_load((replan_env["dossier"] / "DEFINITION.yaml").read_text())
    assert metadata["revision"] == 2

    monkeypatch.setattr(service, "_complete_committed_transaction", original_complete)
    assert service.recover_incomplete() is True
    assert not marker.exists()
    state = json.loads((replan_env["identity"].state_dir / "state.json").read_text())
    packages = {item["id"]: item for item in state["plan_graph"]["work_packages"]}
    assert packages["WP2"]["requirements"] == ["Committed pending"]
    assert any(
        entry.event_type == "task_replan_applied"
        for entry in service.journal.read()
    )


def test_revision_one_is_snapshotted_before_replanning(replan_env) -> None:
    dossier = replan_env["dossier"]
    definitions = TaskDefinitionService()
    snapshot = definitions.require_revision_snapshot(dossier, 1)

    assert snapshot == dossier / "revisions" / "revision-0001"
    for name in ("BRIEF.md", "PLAN.md", "PLAN.graph.yaml"):
        assert (snapshot / name).read_bytes() == (dossier / name).read_bytes()
    marker = yaml.safe_load((snapshot / "REVISION.yaml").read_text(encoding="utf-8"))
    assert marker["revision"] == 1
    assert marker["kind"] == "baseline"
    assert marker["definition_sha256"] == definitions.definition_sha256(dossier)


def test_legacy_task_is_lazily_adopted_before_first_replan(replan_env) -> None:
    dossier = replan_env["dossier"]
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(), _package_wp2())
    (dossier / "DEFINITION.yaml").unlink()
    shutil.rmtree(dossier / "revisions")

    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=_graph(wp2_requirement="Legacy adopted change")
            )
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )

    metadata = yaml.safe_load((dossier / "DEFINITION.yaml").read_text(encoding="utf-8"))
    assert metadata["revision"] == 1
    assert metadata["migration"]["kind"] == "legacy_baseline_adoption"
    assert metadata["sources"]["BRIEF.md"]["origin"] == "legacy_baseline_adopted"
    assert (dossier / "revisions" / "revision-0001" / "REVISION.yaml").is_file()
    assert candidate.revision == 2


def test_tampered_accepted_revision_history_blocks_next_replan(replan_env) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(), _package_wp2())
    first = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=_graph(wp2_requirement="Revision two")
            )
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )
    result = service.apply_candidate(first.candidate_id)
    (result.revision_path / "PLAN.md").write_text("# tampered history\n", encoding="utf-8")

    with pytest.raises(ValueError, match="revision snapshot"):
        service.create_candidate(
            ReplanInputs(
                definition=TaskDefinitionInput.from_contents(
                    plan_graph_yaml=_graph(wp2_requirement="Revision three")
                )
            ),
            provider=ProviderChoice(None, None, "not needed"),
        )


def test_transaction_recovery_rejects_identity_tampering(replan_env) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(), _package_wp2())
    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=_graph(wp2_requirement="Safe transaction")
            )
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )
    transaction = service._begin_transaction(
        candidate,
        service._load_state_or_plan(),
        TaskDefinitionService().load_metadata(replan_env["dossier"]),
        superseded_active_packages=(),
    )
    marker = replan_env["identity"].state_dir / "replan-transaction.yaml"
    raw = yaml.safe_load(marker.read_text(encoding="utf-8"))
    raw["candidate_path"] = str(
        replan_env["dossier"] / "revisions" / "pending-some-other-candidate"
    )
    marker.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(Exception, match="identity mismatch"):
        service.recover_incomplete()

    # Restore the real marker so the fixture can be cleanly recovered and the
    # test does not leave a synthetic incomplete transaction behind.
    marker.write_text(yaml.safe_dump(transaction, sort_keys=False), encoding="utf-8")
    assert service.recover_incomplete()


def test_transaction_recovery_rejects_lifecycle_transition_tampering(replan_env) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(), _package_wp2())
    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=_graph(wp2_requirement="Safe lifecycle transaction")
            )
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )
    transaction = service._begin_transaction(
        candidate,
        service._load_state_or_plan(),
        TaskDefinitionService().load_metadata(replan_env["dossier"]),
        superseded_active_packages=(),
    )
    marker = replan_env["identity"].state_dir / "replan-transaction.yaml"
    raw = yaml.safe_load(marker.read_text(encoding="utf-8"))
    raw["manifest_status_after"] = "approved"
    marker.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(Exception, match="invalid lifecycle transition"):
        service.recover_incomplete()

    marker.write_text(yaml.safe_dump(transaction, sort_keys=False), encoding="utf-8")
    assert service.recover_incomplete()


def test_replan_rejects_running_provider_invocations(replan_env) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(), _package_wp2())
    service.invocations.begin(
        project_id="app",
        task_id="demo",
        package_id="WP2",
        stage="implement",
        capability="implement",
        attempt=1,
        agent_id="worker",
        handoff={"package": "WP2"},
    )

    with pytest.raises(ReplanConflictError, match="provider invocations are still running"):
        service.create_candidate(
            ReplanInputs(
                definition=TaskDefinitionInput.from_contents(
                    plan_graph_yaml=_graph(wp2_requirement="Unsafe while provider runs")
                )
            ),
            provider=ProviderChoice(None, None, "not needed"),
        )


def test_replan_apply_rejects_any_pending_commit_transaction(replan_env) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(), _package_wp2())
    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=_graph(wp2_requirement="Pending-only change")
            )
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )
    service.commit_journal.begin("tx-pending", utc_now(), "WP1")

    with pytest.raises(ReplanConflictError, match="commit transactions are pending"):
        service.apply_candidate(candidate.candidate_id)


def test_replan_apply_rejects_pending_repository_sync_transaction(replan_env) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(), _package_wp2())
    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=_graph(wp2_requirement="Blocked during sync")
            )
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )
    service.repository_sync_transactions.save(
        RepositorySyncTransaction(
            schema_version=1,
            transaction_id="sync-WP20-SYNC-pending",
            package_id="WP20-SYNC",
            package_fingerprint="fingerprint",
            created_at="2026-08-08T00:00:00+00:00",
            updated_at="2026-08-08T00:00:00+00:00",
            phase="fetched",
            repositories=[
                RepositorySyncRepositoryState(
                    repository_id="core",
                    remote="origin",
                    source_branch="master",
                    source_commit="a" * 40,
                    target_branch="task/demo",
                    target_before="b" * 40,
                )
            ],
        )
    )

    with pytest.raises(ReplanConflictError, match="repository-sync transactions are pending"):
        service.apply_candidate(candidate.candidate_id)


def test_replan_records_per_document_provenance(replan_env) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(), _package_wp2())
    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=_graph(wp2_requirement="Imported graph revision"),
                plan_graph_source="/operator/PLAN.graph.yaml",
            )
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )
    assert candidate.document_origins["PLAN.graph.yaml"] == "operator_supplied"
    assert candidate.document_origins["BRIEF.md"] == "carried_forward"
    assert candidate.document_sources["PLAN.graph.yaml"] == "/operator/PLAN.graph.yaml"

    service.apply_candidate(candidate.candidate_id)
    metadata = yaml.safe_load((replan_env["dossier"] / "DEFINITION.yaml").read_text())
    graph_source = metadata["sources"]["PLAN.graph.yaml"]
    assert graph_source["origin"] == "operator_supplied"
    assert graph_source["source"] == "/operator/PLAN.graph.yaml"
    assert metadata["sources"]["BRIEF.md"]["carried_forward_from_revision"] == 1
    assert metadata["revision_path"] == "revisions/revision-0002"
    assert metadata["history"][-1]["revision_path"] == "revisions/revision-0001"


def test_ai_replan_must_echo_explicit_operator_documents(replan_env) -> None:
    from execraft.replan.agent import AgentReplanProposal

    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(), _package_wp2())

    class _Rewriter:
        def propose(self, **_kwargs):
            return AgentReplanProposal(
                consistent=True,
                consistency_summary="claims coherent",
                brief_markdown="# Brief: Demo\n\nRewritten by provider.\n",
                plan_markdown="# Plan: Demo\n\nOperator replacement.\n",
                plan_graph_yaml=_graph(),
                package_mapping={},
                change_summary="rewrote explicit input",
                provider_id="planner",
            )

    service.agent_replanner = _Rewriter()
    provider = type("Provider", (), {"available": True})()
    explicit_brief = "# Brief: Demo\n\nOperator exact brief.\n"
    with pytest.raises(Exception, match="rewrote explicitly supplied BRIEF.md"):
        service.create_candidate(
            ReplanInputs(
                definition=TaskDefinitionInput.from_contents(
                    brief_markdown=explicit_brief,
                )
            ),
            provider=provider,
        )


def test_retired_package_ids_cannot_be_reused_in_later_revisions(replan_env) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["write_state"](_package_wp1(), _package_wp2())
    metadata_path = replan_env["dossier"] / "DEFINITION.yaml"
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    metadata["package_ids_seen"] = ["WP1", "WP2", "WP9_RETIRED"]
    metadata_path.write_text(yaml.safe_dump(metadata, sort_keys=False), encoding="utf-8")

    raw = yaml.safe_load(_graph())
    raw["work_packages"].append(
        {
            "id": "WP9_RETIRED",
            "title": "Illegally reused historical ID",
            "dependencies": ["WP1"],
            "requirements": ["Do new work"],
            "acceptance_criteria": [{"id": "wp9_done", "description": "Done"}],
            "affected_repositories": ["app"],
        }
    )
    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=yaml.safe_dump(raw, sort_keys=False)
            )
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )

    assert not candidate.impact.applicable
    assert any(
        "retired/historical package IDs: WP9_RETIRED" in blocker
        for blocker in candidate.impact.blockers
    )


def test_apply_replan_reopens_review_task_and_preserves_prior_manifest_snapshot(replan_env) -> None:
    service: ReplanService = replan_env["service"]
    manifest = replan_env["manifest"]
    manifest.status = "review"
    write_manifest(replan_env["control"], manifest)
    replan_env["write_state"](_package_wp1(completed=True), _package_wp2())

    raw = yaml.safe_load(_graph())
    raw["work_packages"][1]["requirements"] = ["Implement revised second"]
    candidate = service.create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=yaml.safe_dump(raw, sort_keys=False)
            )
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )
    staged_manifest = yaml.safe_load((candidate.path / "TASK.before.yaml").read_text(encoding="utf-8"))
    assert staged_manifest["status"] == "review"

    result = service.apply_candidate(candidate.candidate_id)

    assert result.previous_task_status == "review"
    assert result.task_status == "in_progress"
    accepted_manifest = yaml.safe_load((replan_env["dossier"] / "TASK.yaml").read_text(encoding="utf-8"))
    assert accepted_manifest["status"] == "in_progress"
    historical_manifest = yaml.safe_load((result.revision_path / "TASK.before.yaml").read_text(encoding="utf-8"))
    assert historical_manifest["status"] == "review"


@pytest.mark.parametrize("status", ["integrating", "merged", "closed", "abandoned"])
def test_replan_rejects_unsafe_task_lifecycle_statuses(replan_env, status: str) -> None:
    service: ReplanService = replan_env["service"]
    replan_env["manifest"].status = status
    replan_env["write_state"](_package_wp1(), _package_wp2())

    with pytest.raises(ReplanConflictError, match=f"lifecycle status {status!r}"):
        service.create_candidate(
            ReplanInputs(
                definition=TaskDefinitionInput.from_contents(plan_graph_yaml=_graph())
            ),
            provider=ProviderChoice(None, None, "not needed"),
        )
