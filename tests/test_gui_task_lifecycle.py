"""WP4 GUI task-definition, replanning, and completion workbench coverage."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from execraft.gui.errors import GuiError
from execraft.gui.routes import GuiApiRouter
from execraft.gui.task_lifecycle import TaskLifecycleController
from execraft.onboarding.start_models import ProviderChoice
from execraft.onboarding.task_definition import TaskDefinitionInput, TaskDefinitionService
from execraft.orchestrate.identity import resolve_storage_identity
from execraft.orchestrate.models import (
    AcceptanceCriterion,
    PlanGraph,
    TaskExecutionState,
    TaskExecutionStateRecord,
    WorkPackage,
    WorkPackageStage,
)
from execraft.project import ProjectDescriptor, ProjectRepository
from execraft.replan import ReplanError
from execraft.replan.service import ReplanInputs
from execraft.workspace.task_git import RepositorySpec, TaskManifest, utc_now


def _graph(requirement: str = "Implement the task") -> str:
    return yaml.safe_dump(
        {
            "schema_version": 1,
            "source_document": "PLAN.md",
            "work_packages": [
                {
                    "id": "WP1",
                    "title": "Implementation",
                    "dependencies": [],
                    "requirements": [requirement],
                    "acceptance_criteria": [
                        {"id": "wp1_done", "description": "Implementation works"}
                    ],
                    "affected_repositories": ["app"],
                    "risk": "low",
                    "priority": 100,
                    "verification_profile": "focused",
                }
            ],
        },
        sort_keys=False,
    )


@pytest.fixture
def lifecycle_env(tmp_path: Path):
    control = tmp_path / "control"
    project_dir = control / "projects" / "app"
    dossier = project_dir / "tasks" / "demo"
    dossier.mkdir(parents=True)
    state_root = tmp_path / "state"
    project = ProjectDescriptor(
        id="app",
        directory=project_dir,
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
    (dossier / "TASK.yaml").write_text(
        yaml.safe_dump(manifest.as_mapping(), sort_keys=False), encoding="utf-8"
    )
    definitions = TaskDefinitionService()
    prepared = definitions.prepare(
        definition=TaskDefinitionInput.from_contents(
            brief_markdown="# Brief: Demo\n\nImplement the task.\n",
            plan_markdown="# Plan: Demo\n\nImplement WP1.\n",
            plan_graph_yaml=_graph(),
        ),
        title="Demo",
        fallback_brief="",
        request_sha256="request-sha",
        created_at=utc_now(),
        allowed_repositories={"app"},
    )
    definitions.materialize(dossier, prepared)
    package = WorkPackage(
        id="WP1",
        title="Implementation",
        requirements=["Implement the task"],
        acceptance_criteria=[
            AcceptanceCriterion(id="wp1_done", description="Implementation works")
        ],
        affected_repositories=["app"],
        stage=WorkPackageStage.PREPARE,
        status="pending",
        risk="low",
        priority=100,
        verification_profile="focused",
    )
    state = TaskExecutionStateRecord(
        project_id="app",
        state=TaskExecutionState.RUNNING,
        plan_graph=PlanGraph(work_packages=[package]),
        started_at=utc_now(),
        last_transition_at=utc_now(),
        completed_packages=0,
        total_packages=1,
    )
    identity = resolve_storage_identity(state_root, project_id="app", task_id="demo")
    identity.state_dir.mkdir(parents=True, exist_ok=True)
    (identity.state_dir / "state.json").write_text(
        json.dumps(state.as_mapping(), indent=2), encoding="utf-8"
    )
    controller = TaskLifecycleController(
        control_root=control,
        state_root=state_root,
        project=project,
        task_id="demo",
        task_dir=dossier,
    )
    return {
        "control": control,
        "state_root": state_root,
        "project": project,
        "dossier": dossier,
        "definitions": definitions,
        "controller": controller,
    }


def test_lifecycle_snapshot_is_lazy_complete_and_non_mutating(lifecycle_env) -> None:
    controller = lifecycle_env["controller"]
    dossier = lifecycle_env["dossier"]
    before = (dossier / "BRIEF.md").read_bytes()

    snapshot = controller.snapshot()

    assert snapshot["task_status"] == "in_progress"
    assert snapshot["definition"]["revision"] == 1
    assert snapshot["definition"]["integrity_ok"] is True
    assert snapshot["definition"]["executable"] is True
    assert snapshot["plan_generation"]["status"] == "idle"
    assert snapshot["documents"]["BRIEF.md"].startswith("# Brief: Demo")
    assert snapshot["revision_history"][0]["revision"] == 1
    assert snapshot["pending_candidates"] == []
    assert snapshot["completion_preview"]["eligible"] is False
    assert (dossier / "BRIEF.md").read_bytes() == before


def test_definition_summary_distinguishes_integrity_from_executability(
    lifecycle_env,
) -> None:
    controller = lifecycle_env["controller"]
    dossier = lifecycle_env["dossier"]
    definitions = lifecycle_env["definitions"]
    (dossier / "PLAN.graph.yaml").unlink()
    metadata_path = dossier / "DEFINITION.yaml"
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    metadata["sources"].pop("PLAN.graph.yaml")
    metadata["current"] = definitions.current_hashes(dossier)
    metadata["definition_sha256"] = definitions.definition_sha256(dossier)
    metadata_path.write_text(yaml.safe_dump(metadata, sort_keys=False), encoding="utf-8")

    definition = controller.summary()["definition"]

    assert definition["integrity_ok"] is True
    assert definition["executable"] is False
    assert definition["execution_missing_documents"] == ["PLAN.graph.yaml"]


def test_gui_replan_stages_diff_and_applies_revision(lifecycle_env) -> None:
    controller = lifecycle_env["controller"]
    dossier = lifecycle_env["dossier"]
    current = (dossier / "BRIEF.md").read_text(encoding="utf-8")
    edited = current + "\n## New constraint\nPreserve compatibility.\n"

    candidate = controller.create_candidate(
        requested_change="",
        brief_markdown=edited,
        plan_markdown="",
        plan_graph_yaml="",
        package_mapping={},
        allow_structural_consistency=True,
    )

    assert candidate["candidate_id"].startswith("r0002-")
    assert candidate["impact"]["applicable"] is True
    brief_diff = next(item for item in candidate["diffs"] if item["name"] == "BRIEF.md")
    assert "+Preserve compatibility." in brief_diff["text"]
    assert (dossier / "BRIEF.md").read_text(encoding="utf-8") == current

    applied = controller.apply_candidate(candidate["candidate_id"])

    assert applied["result"]["revision"] == 2
    assert (dossier / "BRIEF.md").read_text(encoding="utf-8") == edited
    assert applied["lifecycle"]["definition"]["revision"] == 2
    assert applied["lifecycle"]["definition"]["integrity_ok"] is True
    assert applied["lifecycle"]["pending_candidates"] == []
    assert applied["lifecycle"]["revision_history"][0]["revision"] == 2


def test_candidate_view_diffs_generated_graph_against_missing_live_graph(
    lifecycle_env,
) -> None:
    controller = lifecycle_env["controller"]
    dossier = lifecycle_env["dossier"]
    candidate = controller._replan_service().create_candidate(
        ReplanInputs(
            definition=TaskDefinitionInput.from_contents(
                plan_graph_yaml=_graph("Generated initial graph")
            )
        ),
        provider=ProviderChoice(None, None, "not needed"),
    )
    (dossier / "PLAN.graph.yaml").unlink()

    view = controller._candidate_view(candidate.candidate_id)

    graph_diff = next(
        item for item in view["diffs"] if item["name"] == "PLAN.graph.yaml"
    )
    assert "+work_packages:" in graph_diff["text"]


def test_gui_can_adopt_out_of_band_definition_drift(lifecycle_env, monkeypatch) -> None:
    controller = lifecycle_env["controller"]
    dossier = lifecycle_env["dossier"]
    plan = dossier / "PLAN.md"
    plan.write_text("# Plan: Demo\n\nManual operator edit.\n", encoding="utf-8")

    drift = controller.snapshot()
    assert drift["definition"]["integrity_ok"] is False
    assert drift["definition"]["changed_documents"] == ["PLAN.md"]

    def fail_provider_selection(*_args, **_kwargs):
        raise AssertionError("structural-only adoption must not select a provider")

    monkeypatch.setattr(
        "execraft.gui.task_lifecycle.ProviderSelector.select",
        fail_provider_selection,
    )

    candidate = controller.create_candidate(
        requested_change="",
        brief_markdown="",
        plan_markdown="",
        plan_graph_yaml="",
        package_mapping={},
        from_current_files=True,
        allow_structural_consistency=True,
    )
    assert candidate["accepts_live_drift"] is True

    result = controller.apply_candidate(candidate["candidate_id"])
    assert result["result"]["revision"] == 2
    assert result["lifecycle"]["definition"]["integrity_ok"] is True


def test_gui_generate_plan_from_brief_stages_semantic_candidate(lifecycle_env, monkeypatch) -> None:
    controller = lifecycle_env["controller"]
    captured = {}

    class _Candidate:
        candidate_id = "r0002-generated"

    class _ReplanService:
        def create_candidate(self, inputs, *, provider, workdir):
            captured["inputs"] = inputs
            captured["provider"] = provider
            captured["workdir"] = workdir
            return _Candidate()

    provider = object()
    monkeypatch.setattr(controller, "_replan_service", lambda: _ReplanService())
    monkeypatch.setattr(
        "execraft.gui.task_lifecycle.ProviderSelector.select",
        lambda *_args, **_kwargs: provider,
    )
    monkeypatch.setattr(
        controller,
        "_candidate_view",
        lambda candidate_id: {"candidate_id": candidate_id},
    )

    result = controller.generate_plan_from_brief(provider_id="local-qwen")

    assert result == {"candidate_id": "r0002-generated"}
    inputs = captured["inputs"]
    assert inputs.allow_structural_consistency is False
    assert inputs.allow_incomplete_definition is True
    assert inputs.preserve_current_brief is True
    assert not inputs.definition.supplied
    assert "accepted BRIEF.md" in inputs.requested_change
    assert "PLAN.graph.yaml" in inputs.requested_change
    assert captured["provider"] is provider
    assert controller.plan_generation_status()["status"] == "candidate_ready"
    assert controller.plan_generation_status()["candidate_id"] == "r0002-generated"


def test_gui_plan_generation_failure_remains_visible(lifecycle_env, monkeypatch) -> None:
    controller = lifecycle_env["controller"]

    class _ReplanService:
        def create_candidate(self, *_args, **_kwargs):
            raise ReplanError("provider returned invalid output")

    monkeypatch.setattr(controller, "_replan_service", lambda: _ReplanService())
    monkeypatch.setattr(
        "execraft.gui.task_lifecycle.ProviderSelector.select",
        lambda *_args, **_kwargs: object(),
    )

    with pytest.raises(GuiError, match="provider returned invalid output"):
        controller.generate_plan_from_brief(provider_id="codex")

    status = controller.summary()["plan_generation"]
    assert status["status"] == "failed"
    assert status["provider_id"] == "codex"
    assert status["error"] == "provider returned invalid output"


def test_polling_summary_never_runs_deep_git_inspection(lifecycle_env, monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("dashboard polling must not execute Git inspection")

    monkeypatch.setattr("execraft.gui.task_lifecycle.current_branch", fail)
    monkeypatch.setattr("execraft.gui.task_lifecycle.working_tree_dirty", fail)
    monkeypatch.setattr("execraft.gui.task_lifecycle.git_operation", fail)

    summary = lifecycle_env["controller"].summary()

    assert summary["task_status"] == "in_progress"
    assert summary["definition"]["integrity_ok"] is True


def test_lifecycle_summary_tolerates_legacy_dashboard_manifest(lifecycle_env) -> None:
    dossier = lifecycle_env["dossier"]
    raw = yaml.safe_load((dossier / "TASK.yaml").read_text(encoding="utf-8"))
    raw["schema_version"] = 1
    raw["integration"] = {"verify": []}
    raw.pop("integration_branch", None)
    (dossier / "TASK.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")

    summary = lifecycle_env["controller"].summary()

    assert summary["task_status"] == "in_progress"
    assert "integration branch" in summary["manifest_error"]


def test_gui_stages_repository_sync_before_pending_work_package(lifecycle_env) -> None:
    controller = lifecycle_env["controller"]
    result = controller.create_repository_sync_before(
        before_package_id="WP1",
        repositories=["app"],
        source_branches={},
        apply=False,
    )
    assert result["repository_sync"]["package_id"] == "WP1-SYNC"
    candidate = result["candidate"]
    graph = yaml.safe_load(candidate["documents"]["PLAN.graph.yaml"])
    packages = graph["work_packages"]
    sync = next(item for item in packages if item["id"] == "WP1-SYNC")
    target = next(item for item in packages if item["id"] == "WP1")
    assert sync["kind"] == "repository_sync"
    assert sync["affected_repositories"] == ["app"]
    assert target["dependencies"] == ["WP1-SYNC"]


def test_gui_final_sync_previews_then_appends_verified_trailing_gate(
    lifecycle_env, monkeypatch
) -> None:
    controller = lifecycle_env["controller"]
    state_path = resolve_storage_identity(
        lifecycle_env["state_root"], project_id="app", task_id="demo"
    ).state_dir / "state.json"
    state = TaskExecutionStateRecord.from_mapping(json.loads(state_path.read_text()))
    package = state.plan_graph.package_by_id("WP1")
    package.stage = WorkPackageStage.COMPLETED
    package.status = "completed"
    state.state = TaskExecutionState.COMPLETED
    state.completed_packages = state.total_packages
    state_path.write_text(json.dumps(state.as_mapping(), indent=2), encoding="utf-8")

    divergence = SimpleNamespace(
        behind=3,
        as_mapping=lambda: {
            "repository_id": "app",
            "remote": "origin",
            "source_branch": "main",
            "source_commit": "a" * 40,
            "target_branch": "task/demo",
            "target_commit": "b" * 40,
            "merge_base": "c" * 40,
            "ahead": 2,
            "behind": 3,
            "dirty": False,
            "git_operation": "",
            "error": "",
        }
    )
    service = SimpleNamespace(
        divergence_for_branch=lambda *args, **kwargs: divergence,
        divergence=lambda *args, **kwargs: [divergence],
        transaction_report=lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(controller, "_repository_sync_service", lambda: service)

    preview = controller.final_repository_sync_preview(
        repositories=[], source_branches={}
    )
    assert preview["sync_required"] is True
    assert preview["behind_commits"] == 3

    result = controller.create_final_repository_sync(
        repositories=[], source_branches={}
    )
    assert result["applied"] is True
    assert result["package_id"] == "FINAL-SYNC"
    migrated = TaskExecutionStateRecord.from_mapping(json.loads(state_path.read_text()))
    assert migrated.state == TaskExecutionState.RUNNING
    final = migrated.plan_graph.package_by_id("FINAL-SYNC")
    assert final.dependencies == ["WP1"]
    assert final.stage == WorkPackageStage.PREPARE


def test_gui_final_sync_noop_keeps_completed_definition_unchanged(
    lifecycle_env, monkeypatch
) -> None:
    controller = lifecycle_env["controller"]
    state_path = resolve_storage_identity(
        lifecycle_env["state_root"], project_id="app", task_id="demo"
    ).state_dir / "state.json"
    state = TaskExecutionStateRecord.from_mapping(json.loads(state_path.read_text()))
    package = state.plan_graph.package_by_id("WP1")
    package.stage = WorkPackageStage.COMPLETED
    package.status = "completed"
    state.state = TaskExecutionState.COMPLETED
    state.completed_packages = state.total_packages
    state_path.write_text(json.dumps(state.as_mapping(), indent=2), encoding="utf-8")
    divergence = SimpleNamespace(
        as_mapping=lambda: {
            "repository_id": "app", "remote": "origin", "source_branch": "main",
            "source_commit": "a" * 40, "target_branch": "task/demo",
            "target_commit": "b" * 40, "merge_base": "a" * 40,
            "ahead": 2, "behind": 0, "dirty": False, "git_operation": "", "error": "",
        }
    )
    monkeypatch.setattr(
        controller,
        "_repository_sync_service",
        lambda: SimpleNamespace(divergence_for_branch=lambda *args, **kwargs: divergence),
    )
    before = (lifecycle_env["dossier"] / "DEFINITION.yaml").read_bytes()

    result = controller.create_final_repository_sync(
        repositories=[], source_branches={}
    )

    assert result["applied"] is False
    assert result["sync_required"] is False
    assert (lifecycle_env["dossier"] / "DEFINITION.yaml").read_bytes() == before


class _RouteService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def task_lifecycle_snapshot(self):
        self.calls.append(("snapshot", None))
        return {"definition": {"revision": 2}}

    def task_repository_sync(self, *, refresh: bool = False):
        self.calls.append(("repository_sync", refresh))
        return {"available": True, "refresh": refresh}

    def task_repository_sync_options(self, package_id: str, *, refresh: bool = False):
        self.calls.append(("repository_sync_options", {"package_id": package_id, "refresh": refresh}))
        return {"package_id": package_id, "mode": "before", "repositories": []}

    def task_repository_sync_preview(self, **kwargs):
        self.calls.append(("repository_sync_preview", kwargs))
        return {"package_id": kwargs["package_id"], "repositories": []}

    def task_repository_sync_request(self, **kwargs):
        self.calls.append(("repository_sync_request", kwargs))
        return {"package_id": kwargs["package_id"], "kind": "pause_for_repository_sync"}

    def task_final_sync_preview(self, **kwargs):
        self.calls.append(("final_sync_preview", kwargs))
        return {"sync_required": True, "repositories": kwargs["repositories"]}

    def task_final_sync_apply(self, **kwargs):
        self.calls.append(("final_sync_apply", kwargs))
        return {"applied": True, "package_id": "FINAL-SYNC"}

    def task_repository_sync_before(self, **kwargs):
        self.calls.append(("repository_sync_before", kwargs))
        return {"repository_sync": {"package_id": "WP20-SYNC"}}

    def task_repository_sync_rollback(self, package_id: str):
        self.calls.append(("repository_sync_rollback", package_id))
        return {"rollback": {"package_id": package_id}}

    def task_repository_sync_accept_resolution(self, package_id: str):
        self.calls.append(("repository_sync_accept_resolution", package_id))
        return {"resolution": {"package_id": package_id}}

    def task_replan_candidate_view(self, candidate_id: str):
        self.calls.append(("candidate_view", candidate_id))
        return {"candidate_id": candidate_id}

    def task_replan_candidate(self, **kwargs):
        self.calls.append(("candidate", kwargs))
        return {"candidate_id": "r0002-test"}

    def task_generate_plan(self, *, provider_id: str = ""):
        self.calls.append(("generate_plan", provider_id))
        return {"candidate_id": "r0002-generated", "provider_id": provider_id}

    def task_replan_apply(self, candidate_id: str):
        self.calls.append(("apply", candidate_id))
        return {"applied": candidate_id}

    def task_replan_recover(self):
        self.calls.append(("recover", None))
        return {"recovered": True}

    def task_complete(self, *, dry_run: bool):
        self.calls.append(("complete", dry_run))
        return {"dry_run": dry_run}


def test_dashboard_routes_expose_wp4_lifecycle_contract() -> None:
    service = _RouteService()
    router = GuiApiRouter(service)

    assert router.get("/api/task/lifecycle", {})["definition"]["revision"] == 2
    assert router.get("/api/task/repository-sync", {"refresh": ["1"]})["refresh"] is True
    assert router.post(
        "/api/task/repository-sync/before",
        {
            "before_package_id": "WP20",
            "repositories": ["core"],
            "source_branches": {"core": "mission_planner"},
            "apply": False,
        },
    )["repository_sync"]["package_id"] == "WP20-SYNC"
    assert router.post(
        "/api/task/repository-sync/rollback", {"package_id": "WP20-SYNC"}
    )["rollback"]["package_id"] == "WP20-SYNC"
    assert router.post(
        "/api/task/repository-sync/accept-resolution", {"package_id": "WP20-SYNC"}
    )["resolution"]["package_id"] == "WP20-SYNC"
    assert router.get(
        "/api/task/repository-sync/options", {"package_id": ["WP20"], "refresh": ["1"]}
    )["mode"] == "before"
    assert router.post(
        "/api/task/repository-sync/preview",
        {"package_id": "WP20", "repositories": ["core"], "source_branches": {"core": "release/test"}},
    )["package_id"] == "WP20"
    assert router.post(
        "/api/task/repository-sync/request",
        {"package_id": "WP20", "repositories": ["core"], "source_branches": {}, "auto_resume": False},
    )["kind"] == "pause_for_repository_sync"
    assert router.post(
        "/api/task/final-sync/preview",
        {"repositories": ["core"], "source_branches": {}},
    )["sync_required"] is True
    assert router.post(
        "/api/task/final-sync/apply",
        {"repositories": ["core"], "source_branches": {}, "sync_package_id": "FINAL-SYNC"},
    )["package_id"] == "FINAL-SYNC"
    assert router.get(
        "/api/task/replan/candidate", {"candidate_id": ["r0002-test"]}
    )["candidate_id"] == "r0002-test"
    assert router.post(
        "/api/task/replan/candidate",
        {
            "requested_change": "change it",
            "brief_markdown": "",
            "plan_markdown": "",
            "plan_graph_yaml": "",
            "package_mapping": {"WP1": "M1R1"},
            "provider_id": "",
            "from_current_files": False,
            "allow_structural_consistency": True,
        },
    )["candidate_id"] == "r0002-test"
    assert router.post(
        "/api/task/replan/generate", {"provider_id": "local-qwen"}
    ) == {"candidate_id": "r0002-generated", "provider_id": "local-qwen"}
    assert router.post(
        "/api/task/replan/apply", {"candidate_id": "r0002-test"}
    ) == {"applied": "r0002-test"}
    assert router.post("/api/task/replan/recover", {}) == {"recovered": True}
    assert router.post("/api/task/complete", {"dry_run": True}) == {"dry_run": True}


def test_dashboard_routes_reject_truthy_strings_for_wp4_flags() -> None:
    router = GuiApiRouter(_RouteService())
    with pytest.raises(GuiError, match="from_current_files must be a JSON boolean"):
        router.post(
            "/api/task/replan/candidate",
            {"from_current_files": "true"},
        )
    with pytest.raises(GuiError, match="dry_run must be a JSON boolean"):
        router.post("/api/task/complete", {"dry_run": "false"})


def test_gui_assets_are_packaged_and_reference_safe_endpoints() -> None:
    from execraft.gui.server import _dashboard_asset, _dashboard_html

    html = _dashboard_html("token")
    js, content_type = _dashboard_asset("task-lifecycle.js")
    source = js.decode("utf-8")
    app_source = _dashboard_asset("app.js")[0].decode("utf-8")

    assert content_type == "text/javascript; charset=utf-8"
    assert "TaskLifecycleView" in source
    assert "/api/task/replan/candidate" in source
    assert "/api/task/replan/generate" in source
    assert "Generation in progress" in source
    assert "Plan generation required" in source
    assert "setContext(projectId, taskId)" in source
    assert "taskLifecycleView.setContext" in app_source
    assert "taskLifecycleView.setProviders" in app_source
    assert 'left.id === "codex"' in source
    assert "/api/task/replan/apply" in source
    assert "/api/task/replan/recover" in source
    assert "/api/task/repository-sync" in source
    assert "/api/task/repository-sync/before" in source
    assert "/api/task/repository-sync/rollback" in source
    assert "/api/task/repository-sync/accept-resolution" in source
    assert "/api/task/final-sync/preview" in source
    assert "/api/task/final-sync/apply" in source
    assert "/api/task/complete" in source
    for identifier in (
        "planTab",
        "planView",
        "taskBriefEditor",
        "taskPlanEditor",
        "generatePlanBtn",
        "planGenerationStatus",
        "replanImpact",
        "completionPreview",
        "finalSyncBtn",
        "workspaceOwnership",
        "repositorySyncStatus",
        "repositorySyncBeforeInput",
    ):
        assert f'id="{identifier}"' in html


def test_task_definition_lower_layout_uses_explicit_balanced_areas() -> None:
    from execraft.gui.server import _dashboard_asset

    css, content_type = _dashboard_asset("gui.css")
    source = css.decode("utf-8")

    assert content_type == "text/css; charset=utf-8"
    assert '"repository ownership"' in source
    assert '"completion ownership"' in source
    assert '"history ownership"' in source
    assert ".repository-sync-panel { grid-area: repository; }" in source
    assert ".ownership-panel { grid-area: ownership; }" in source
    assert '"repository"\n      "completion"\n      "ownership"\n      "history"' in source


def test_work_package_card_sync_options_default_to_task_base_and_fixed_target(
    lifecycle_env, monkeypatch
) -> None:
    from execraft.repository_sync.service import RemoteBranchOption, RepositoryDivergence

    class FakeSyncService:
        def remote_branches(self, repository_id, *, remote, refresh):
            assert repository_id == "app"
            assert remote == "origin"
            return (
                RemoteBranchOption(
                    repository_id="app",
                    remote="origin",
                    branch="main",
                    commit="a" * 40,
                    configured_base=True,
                    locally_tracked=True,
                ),
                RemoteBranchOption(
                    repository_id="app",
                    remote="origin",
                    branch="release/test",
                    commit="b" * 40,
                ),
            )

        def divergence_for_branch(self, repository_id, *, remote, source_branch, refresh):
            return RepositoryDivergence(
                repository_id=repository_id,
                remote=remote,
                source_branch=source_branch,
                source_commit="a" * 40,
                target_branch="task/demo",
                target_commit="c" * 40,
                merge_base="d" * 40,
                ahead=3,
                behind=7,
            )

    controller = lifecycle_env["controller"]
    monkeypatch.setattr(controller, "_repository_sync_service", lambda: FakeSyncService())

    options = controller.repository_sync_options("WP1", refresh=True)

    assert options["mode"] == "before"
    repo = options["repositories"][0]
    assert repo["configured_base_branch"] == "main"
    assert repo["task_branch"] == "task/demo"
    assert repo["selected"] is True
    assert [item["branch"] for item in repo["branches"]] == ["main", "release/test"]
    assert options["summary"] == {
        "behind_repositories": 1,
        "largest_behind": 7,
        "mode": "before",
        "refreshed": True,
    }
    assert controller.repository_sync_card_summaries()["WP1"]["largest_behind"] == 7


def test_work_package_card_sync_excludes_retired_project_repository(
    lifecycle_env, monkeypatch
) -> None:
    controller = lifecycle_env["controller"]
    task_path = lifecycle_env["dossier"] / "TASK.yaml"
    raw = yaml.safe_load(task_path.read_text(encoding="utf-8"))
    raw["repositories"].append(
        {
            "id": "retired",
            "base_branch": "main",
            "task_branch": "task/demo",
            "role": "legacy",
            "required": False,
        }
    )
    task_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    state_path = resolve_storage_identity(
        lifecycle_env["state_root"], project_id="app", task_id="demo"
    ).state_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["plan_graph"]["work_packages"][0]["affected_repositories"].append(
        "retired"
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")

    class FakeSyncService:
        def remote_branches(self, *_args, **_kwargs):
            return ()

        def divergence_for_branch(self, *_args, **_kwargs):
            raise AssertionError("no divergence is needed for this regression")

    monkeypatch.setattr(controller, "_repository_sync_service", lambda: FakeSyncService())

    options = controller.repository_sync_options("WP1")

    assert [item["id"] for item in options["repositories"]] == ["app"]
    with pytest.raises(GuiError, match="no longer in the project: retired"):
        controller.prepare_repository_sync_card_request(
            package_id="WP1",
            repositories=["retired"],
            source_branches={},
            remote="origin",
            conflict_policy="ai_resolve",
            sync_package_id="",
            auto_resume=True,
        )


def test_work_package_card_sync_preview_marks_non_base_branch_override(
    lifecycle_env, monkeypatch
) -> None:
    from execraft.repository_sync.service import RepositoryDivergence

    class FakeSyncService:
        def divergence_for_branch(self, repository_id, *, remote, source_branch, refresh):
            assert refresh is True
            return RepositoryDivergence(
                repository_id=repository_id,
                remote=remote,
                source_branch=source_branch,
                source_commit="b" * 40,
                target_branch="task/demo",
                target_commit="c" * 40,
                merge_base="d" * 40,
                ahead=2,
                behind=4,
            )

    controller = lifecycle_env["controller"]
    monkeypatch.setattr(controller, "_repository_sync_service", lambda: FakeSyncService())
    preview = controller.repository_sync_preview_selection(
        package_id="WP1",
        repositories=["app"],
        source_branches={"app": "release/test"},
    )

    row = preview["repositories"][0]
    assert row["source_branch"] == "release/test"
    assert row["source_selection"] == "operator_override"
    assert row["configured_base_branch"] == "main"
    assert preview["summary"]["largest_behind"] == 4


def test_work_package_card_request_uses_before_for_pending_and_after_for_started(
    lifecycle_env,
) -> None:
    controller = lifecycle_env["controller"]
    before = controller.prepare_repository_sync_card_request(
        package_id="WP1",
        repositories=["app"],
        source_branches={},
        remote="origin",
        conflict_policy="ai_resolve",
        sync_package_id="",
        auto_resume=True,
    )
    assert before.mode == "before"
    assert before.source_branches == {}

    state_path = resolve_storage_identity(
        lifecycle_env["state_root"], project_id="app", task_id="demo"
    ).state_dir / "state.json"
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    raw["plan_graph"]["work_packages"][0]["stage"] = "implement"
    raw["plan_graph"]["work_packages"][0]["status"] = "running"
    state_path.write_text(json.dumps(raw), encoding="utf-8")

    after = controller.prepare_repository_sync_card_request(
        package_id="WP1",
        repositories=["app"],
        source_branches={"app": "release/test"},
        remote="origin",
        conflict_policy="human",
        sync_package_id="",
        auto_resume=False,
    )
    assert after.mode == "after"
    assert after.source_branches == {"app": "release/test"}
    assert after.conflict_policy == "human"
    assert after.auto_resume is False


def test_card_assets_expose_pause_sync_dialog_and_safe_endpoints() -> None:
    from execraft.gui.server import _dashboard_asset, _dashboard_html

    html = _dashboard_html("token")
    app_js, content_type = _dashboard_asset("app.js")
    workflow_js, _ = _dashboard_asset("workflow.js")
    app_source = app_js.decode("utf-8")
    workflow_source = workflow_js.decode("utf-8")

    assert content_type == "text/javascript; charset=utf-8"
    for identifier in (
        "syncWorkPackageBtn",
        "workPackageSyncDialog",
        "workPackageSyncRepositories",
        "workPackageSyncConflictPolicy",
        "workPackageSyncAutoResume",
        "confirmWorkPackageSync",
    ):
        assert f'id="{identifier}"' in html
    assert "/api/task/repository-sync/options" in app_source
    assert "/api/task/repository-sync/preview" in app_source
    assert "/api/task/repository-sync/request" in app_source
    # GUI-C moves administrative sync into the Work Package inspector rather than
    # duplicating it on every Graph card. The API contract remains unchanged.
    assert 'id="syncWorkPackageBtn"' in html
    assert "Pause & Sync" in html
    assert 'data-work-package-action="repository-sync"' in workflow_source
    assert '$("syncWorkPackageBtn").addEventListener("click"' in app_source
    # The target branch is presentation-only; no target branch value is posted.
    assert "target_branch:" not in app_source


def test_card_sync_coordinator_applies_wp2_revision_at_safe_boundary(lifecycle_env) -> None:
    from execraft.repository_sync.card_request import RepositorySyncCardRequest
    from execraft.repository_sync.coordinator import RepositorySyncCoordinator
    dossier = lifecycle_env["dossier"]
    manifest = TaskManifest.from_mapping(
        yaml.safe_load((dossier / "TASK.yaml").read_text(encoding="utf-8"))
    )
    request = RepositorySyncCardRequest.create(
        manifest=manifest,
        package_id="WP1",
        mode="before",
        repositories=["app"],
        source_branches={"app": "release/test"},
        auto_resume=False,
    )
    coordinator = RepositorySyncCoordinator(
        control_root=lifecycle_env["control"],
        state_root=lifecycle_env["state_root"],
        project=lifecycle_env["project"],
        manifest=manifest,
        dossier=dossier,
    )
    result = coordinator.apply_waiting(
        {
            "kind": "pause_for_repository_sync",
            "package_id": "WP1",
            "command_id": "command-test-wp1",
            "repository_sync": request.as_parameters(),
        }
    )

    assert result.replan.revision == 2
    assert result.insertion.sync_package_id == "WP1-SYNC"
    assert result.request.auto_resume is False
    graph = yaml.safe_load((dossier / "PLAN.graph.yaml").read_text(encoding="utf-8"))
    packages = graph["work_packages"]
    sync = next(item for item in packages if item["id"] == "WP1-SYNC")
    target = next(item for item in packages if item["id"] == "WP1")
    assert sync["repository_sync"]["repositories"]["app"]["source_branch"] == "release/test"
    assert target["dependencies"] == ["WP1-SYNC"]


def test_dashboard_sync_options_restore_pending_card_selection(tmp_path) -> None:
    from types import SimpleNamespace
    from execraft.gui.server import DashboardService
    from execraft.orchestrate.directives import (
        PAUSE_FOR_REPOSITORY_SYNC,
        WorkPackageDirectiveQueue,
    )

    lifecycle = SimpleNamespace(
        repository_sync_options=lambda package_id, refresh=False: {
            "package_id": package_id,
            "mode": "before",
            "repositories": [
                {
                    "id": "core",
                    "selected": True,
                    "configured_base_branch": "main",
                    "task_branch": "task/demo",
                    "branches": [],
                },
                {
                    "id": "ui",
                    "selected": True,
                    "configured_base_branch": "master",
                    "task_branch": "task/demo",
                    "branches": [],
                },
            ],
        }
    )
    queue = WorkPackageDirectiveQueue(tmp_path / "directives.json")
    queued = queue.enqueue(
        package_id="WP20",
        kind=PAUSE_FOR_REPOSITORY_SYNC,
        enabled=True,
        parameters={
            "mode": "before",
            "repositories": ["core"],
            "source_branches": {"core": "release/test"},
            "conflict_policy": "human",
            "auto_resume": False,
        },
    )
    service = object.__new__(DashboardService)
    service.task_lifecycle = lifecycle
    service.work_package_directives = queue

    result = service.task_repository_sync_options("WP20")

    core, ui = result["repositories"]
    assert core["selected"] is True
    assert core["selected_source_branch"] == "release/test"
    assert ui["selected"] is False
    assert ui["selected_source_branch"] == "master"
    assert result["pending_request"]["id"] == queued.id
    assert result["pending_request"]["auto_resume"] is False


def test_dashboard_card_request_persists_branch_and_resume_policy(tmp_path) -> None:
    from types import SimpleNamespace
    from execraft.gui.server import DashboardService
    from execraft.orchestrate.directives import WorkPackageDirectiveQueue
    from execraft.repository_sync.card_request import RepositorySyncCardRequest

    request = RepositorySyncCardRequest(
        package_id="WP20",
        mode="after",
        repositories=("core",),
        source_branches={"core": "release/test"},
        remote="origin",
        conflict_policy="human",
        auto_resume=False,
    )
    lifecycle = SimpleNamespace(
        prepare_repository_sync_card_request=lambda **kwargs: request
    )
    service = object.__new__(DashboardService)
    service.task_lifecycle = lifecycle
    service.work_package_directives = WorkPackageDirectiveQueue(tmp_path / "directives.json")
    service.process = SimpleNamespace(
        status=lambda: {"owned_running": True, "external_running": False}
    )

    result = service.task_repository_sync_request(
        package_id="WP20",
        repositories=["core"],
        source_branches={"core": "release/test"},
        conflict_policy="human",
        auto_resume=False,
    )

    assert result["queued_while_running"] is True
    pending = service.work_package_directives.pending()[0]
    assert pending.parameters["mode"] == "after"
    assert pending.parameters["source_branches"] == {"core": "release/test"}
    assert pending.parameters["conflict_policy"] == "human"
    assert pending.parameters["auto_resume"] is False


def test_card_sync_intent_recovers_pause_policy_after_replan_crash(lifecycle_env) -> None:
    from types import SimpleNamespace
    from execraft.repository_sync.card_request import RepositorySyncCardRequest
    from execraft.repository_sync.coordinator import RepositorySyncCoordinator

    dossier = lifecycle_env["dossier"]
    manifest = TaskManifest.from_mapping(
        yaml.safe_load((dossier / "TASK.yaml").read_text(encoding="utf-8"))
    )
    request = RepositorySyncCardRequest.create(
        manifest=manifest,
        package_id="WP1",
        mode="before",
        repositories=["app"],
        auto_resume=False,
    )
    coordinator = RepositorySyncCoordinator(
        control_root=lifecycle_env["control"],
        state_root=lifecycle_env["state_root"],
        project=lifecycle_env["project"],
        manifest=manifest,
        dossier=dossier,
    )
    result = coordinator.apply_waiting(
        {
            "kind": "pause_for_repository_sync",
            "package_id": "WP1",
            "command_id": "crash-window-command",
            "repository_sync": request.as_parameters(),
        }
    )
    intent = coordinator.intents.load("crash-window-command")
    assert intent is not None and intent.phase == "replanned"

    scheduled = []
    released = []
    fake_orchestrator = SimpleNamespace(
        load_state=lambda: None,
        schedule_pause_after_completion=lambda package_id, reason: scheduled.append(
            (package_id, reason)
        ),
        acknowledge_repository_sync_boundary=lambda **kwargs: released.append(kwargs),
    )
    recovered = coordinator.recover_execution_policies(fake_orchestrator)

    assert recovered == ["crash-window-command"]
    assert scheduled and scheduled[0][0] == result.insertion.sync_package_id
    assert released == [
        {
            "command_id": "crash-window-command",
            "sync_package_id": result.insertion.sync_package_id,
        }
    ]
    intent = coordinator.intents.load("crash-window-command")
    assert intent is not None and intent.phase == "complete"


def test_card_sync_intent_auto_resume_needs_no_pause_gate(lifecycle_env) -> None:
    from types import SimpleNamespace
    from execraft.repository_sync.card_request import RepositorySyncCardRequest
    from execraft.repository_sync.coordinator import RepositorySyncCoordinator

    dossier = lifecycle_env["dossier"]
    manifest = TaskManifest.from_mapping(
        yaml.safe_load((dossier / "TASK.yaml").read_text(encoding="utf-8"))
    )
    request = RepositorySyncCardRequest.create(
        manifest=manifest,
        package_id="WP1",
        mode="before",
        repositories=["app"],
        auto_resume=True,
    )
    coordinator = RepositorySyncCoordinator(
        control_root=lifecycle_env["control"],
        state_root=lifecycle_env["state_root"],
        project=lifecycle_env["project"],
        manifest=manifest,
        dossier=dossier,
    )
    coordinator.apply_waiting(
        {
            "kind": "pause_for_repository_sync",
            "package_id": "WP1",
            "command_id": "auto-resume-command",
            "repository_sync": request.as_parameters(),
        }
    )
    released = []
    fake_orchestrator = SimpleNamespace(
        load_state=lambda: None,
        schedule_pause_after_completion=lambda *_args, **_kwargs: pytest.fail(
            "auto-resume must not install a hold"
        ),
        acknowledge_repository_sync_boundary=lambda **kwargs: released.append(kwargs),
    )

    assert coordinator.recover_execution_policies(fake_orchestrator) == [
        "auto-resume-command"
    ]
    assert released and released[0]["command_id"] == "auto-resume-command"
    intent = coordinator.intents.load("auto-resume-command")
    assert intent is not None and intent.phase == "complete"


def test_card_sync_coordinator_migrates_legacy_hybrid_graph(lifecycle_env) -> None:
    """Regression for Pause & Sync on older tasks such as sample_task."""

    from execraft.repository_sync.card_request import RepositorySyncCardRequest
    from execraft.repository_sync.coordinator import RepositorySyncCoordinator

    control = lifecycle_env["control"]
    state_root = lifecycle_env["state_root"]
    project = lifecycle_env["project"]
    dossier = control / "projects" / "app" / "tasks" / "legacy-sync"
    dossier.mkdir(parents=True)
    manifest = TaskManifest(
        schema_version=2,
        id="legacy-sync",
        project="app",
        title="Legacy sync",
        status="in_progress",
        created_at=utc_now(),
        last_updated=utc_now(),
        branch_name="task/legacy-sync",
        repositories=[
            RepositorySpec(
                id="app",
                base_branch="main",
                task_branch="task/legacy-sync",
                role="component",
                required=True,
            )
        ],
    )
    (dossier / "TASK.yaml").write_text(
        yaml.safe_dump(manifest.as_mapping(), sort_keys=False), encoding="utf-8"
    )
    legacy_graph = yaml.safe_dump(
        {
            "schema_version": 1,
            "source_document": "PLAN.md",
            "work_packages": [
                {
                    "id": "M00",
                    "title": "Historical",
                    "dependencies": [],
                    "requirements": ["Historical work"],
                    "acceptance_criteria": [
                        {
                            "id": "m00_done",
                            "description": "Historical work done",
                            "verified": True,
                            "evidence": "imported history",
                        }
                    ],
                    "affected_repositories": ["app"],
                    "stage": "completed",
                    "status": "completed",
                    "risk": "low",
                    "priority": 20,
                    "verification_profile": "focused",
                },
                {
                    "id": "WP20",
                    "title": "Next",
                    "dependencies": ["M00"],
                    "requirements": ["Next work"],
                    "acceptance_criteria": [
                        {
                            "id": "wp20_done",
                            "description": "Next work done",
                            "verified": False,
                            "evidence": "",
                        }
                    ],
                    "affected_repositories": ["app"],
                    "stage": "prepare",
                    "status": "pending",
                    "risk": "medium",
                    "priority": 10,
                    "verification_profile": "focused",
                },
            ],
        },
        sort_keys=False,
    )
    definitions = TaskDefinitionService()
    prepared = definitions.prepare(
        definition=TaskDefinitionInput.from_contents(
            brief_markdown="# Brief: Legacy sync\n\nKeep history.\n",
            plan_markdown="# Plan: Legacy sync\n\nM00 then WP20.\n",
            plan_graph_yaml=legacy_graph,
        ),
        title="Legacy sync",
        fallback_brief="",
        request_sha256="legacy-request",
        created_at=utc_now(),
        allowed_repositories={"app"},
    )
    definitions.materialize(dossier, prepared)
    state = TaskExecutionStateRecord(
        project_id="app",
        state=TaskExecutionState.RUNNING,
        plan_graph=PlanGraph(
            work_packages=[
                WorkPackage(
                    id="M00",
                    title="Historical",
                    requirements=["Historical work"],
                    acceptance_criteria=[
                        AcceptanceCriterion(
                            id="m00_done",
                            description="Historical work done",
                            verified=True,
                            evidence="imported history",
                        )
                    ],
                    affected_repositories=["app"],
                    stage=WorkPackageStage.COMPLETED,
                    status="completed",
                    risk="low",
                    priority=20,
                    verification_profile="focused",
                ),
                WorkPackage(
                    id="WP20",
                    title="Next",
                    dependencies=["M00"],
                    requirements=["Next work"],
                    acceptance_criteria=[
                        AcceptanceCriterion(
                            id="wp20_done", description="Next work done"
                        )
                    ],
                    affected_repositories=["app"],
                    stage=WorkPackageStage.PREPARE,
                    status="pending",
                    risk="medium",
                    priority=10,
                    verification_profile="focused",
                ),
            ]
        ),
        started_at=utc_now(),
        last_transition_at=utc_now(),
        completed_packages=1,
        total_packages=2,
    )
    identity = resolve_storage_identity(
        state_root, project_id="app", task_id="legacy-sync"
    )
    identity.state_dir.mkdir(parents=True, exist_ok=True)
    (identity.state_dir / "state.json").write_text(
        json.dumps(state.as_mapping(), indent=2), encoding="utf-8"
    )
    request = RepositorySyncCardRequest.create(
        manifest=manifest,
        package_id="WP20",
        mode="before",
        repositories=["app"],
    )
    coordinator = RepositorySyncCoordinator(
        control_root=control,
        state_root=state_root,
        project=project,
        manifest=manifest,
        dossier=dossier,
    )

    result = coordinator.apply_waiting(
        {
            "kind": "pause_for_repository_sync",
            "package_id": "WP20",
            "command_id": "legacy-sync-command",
            "repository_sync": request.as_parameters(),
        }
    )

    assert result.replan.revision == 2
    accepted = yaml.safe_load((dossier / "PLAN.graph.yaml").read_text(encoding="utf-8"))
    packages = {item["id"]: item for item in accepted["work_packages"]}
    assert set(packages) == {"M00", "WP20-SYNC", "WP20"}
    assert "stage" not in packages["M00"]
    assert "status" not in packages["M00"]
    assert packages["M00"]["acceptance_criteria"] == [
        {"id": "m00_done", "description": "Historical work done"}
    ]
    assert packages["WP20"]["dependencies"] == ["WP20-SYNC"]
