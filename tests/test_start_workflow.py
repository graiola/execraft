"""WP3 tests for one-command start and greenfield project creation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from execraft import bootstrap
from execraft.agents.config import AgentProviderConfig
from execraft.control_plane import ControlPlaneHome
from execraft.onboarding.greenfield import GreenfieldService, default_greenfield_catalog
from execraft.onboarding.providers import ProviderInventoryItem
from execraft.onboarding.start import (
    DraftPlanService,
    PlannerMode,
    ProviderChoice,
    ProviderSelector,
    RepositorySelector,
    StartRequest,
    StartWorkflowError,
    StartWorkflowService,
    TaskIntent,
)
from execraft.orchestrate.scheduler import (
    AgentAdapterCapabilities,
    AgentCapability,
    Availability,
)
from execraft.project import ProjectDescriptor, ProjectRepository, load_project
from execraft.workspace.task_git import load_manifest, project_task_directory
from execraft.workspace.workspace_git import load_workspace


def _init_git(path: Path, *, branch: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)


def _configure_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ControlPlaneHome:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    control_root = tmp_path / "data" / "execraft" / "control"
    monkeypatch.setenv("EXECRAFT_CONTROL_ROOT", str(control_root))
    monkeypatch.delenv("EXECRAFT_WORKFLOW_ROOT", raising=False)
    monkeypatch.delenv("AI_WORKFLOW_ROOT", raising=False)
    return ControlPlaneHome.resolve()


def test_task_intent_derives_stable_title_slug_and_digest() -> None:
    intent = TaskIntent.create("Add OIDC authentication with token revocation.")
    assert intent.task_id == "add-oidc-authentication-token-revocation"
    assert intent.title == "Add OIDC authentication with token revocation"
    assert len(intent.intent_sha256) == 64
    assert intent.explicit_task_id is False

    explicit = TaskIntent.create("anything", task_id="auth", title="Authentication")
    assert explicit.task_id == "auth"
    assert explicit.title == "Authentication"
    assert explicit.explicit_task_id is True


def test_repository_selector_preserves_required_and_infers_optional_scope() -> None:
    project = ProjectDescriptor(
        id="product",
        directory=Path("/tmp/product"),
        repositories=(
            ProjectRepository(
                id="api",
                path="api",
                workspace_name="api",
                role="component",
                required=True,
                base_branch="main",
            ),
            ProjectRepository(
                id="web-ui",
                path="web/ui",
                workspace_name="web-ui",
                role="component",
                required=False,
                base_branch="main",
            ),
            ProjectRepository(
                id="deployment",
                path="deploy",
                workspace_name="deployment",
                role="integration",
                required=False,
                base_branch="main",
            ),
        ),
    )
    selector = RepositorySelector()

    inferred = selector.select(project, "Update the web UI login screen")
    assert inferred.repository_ids == ("api", "web-ui", "deployment")
    assert inferred.explicit is False

    explicit = selector.select(project, "Anything", ("web-ui",))
    assert explicit.repository_ids == ("api", "web-ui")
    assert explicit.explicit is True


def _provider_config(*, name: str = "codex", enabled: bool = True) -> AgentProviderConfig:
    return AgentProviderConfig(
        name=name,
        adapter="codex",
        enabled=enabled,
        provider_id=name,
        binary="codex",
        capabilities=frozenset({AgentCapability.PLAN, AgentCapability.DECOMPOSE}),
        capability_weight=95,
        priority=100,
    )


class _FakeAdapter:
    availability = Availability.AVAILABLE
    execution_capabilities = AgentAdapterCapabilities(read_only_enforcement="hard")

    def __init__(self, response: str) -> None:
        self.response = response
        self.handoff = None

    def execute(self, handoff):
        self.handoff = handoff
        return {"ok": True, "final_message": self.response}


def test_agent_draft_uses_read_only_handoff_and_validates_graph(tmp_path: Path) -> None:
    response = json.dumps(
        {
            "plan_markdown": "# Plan\n\nImplement the endpoint.\n",
            "plan_graph": {
                "schema_version": 1,
                "work_packages": [
                    {
                        "id": "WP01",
                        "title": "Health endpoint",
                        "dependencies": [],
                        "requirements": ["Add a health endpoint"],
                        "acceptance_criteria": [
                            {"id": "health", "description": "Endpoint returns healthy"}
                        ],
                        "affected_repositories": ["app"],
                        "stage": "prepare",
                        "status": "pending",
                        "risk": "low",
                        "priority": 10,
                        "verification_profile": "focused",
                    }
                ],
            },
        }
    )
    adapter = _FakeAdapter(response)
    planner = DraftPlanService(adapter_builder=lambda *_args, **_kwargs: adapter)
    provider_item = ProviderInventoryItem(
        name="codex",
        provider_id="codex",
        adapter="codex",
        enabled=True,
        binary="codex",
        binary_path="/usr/bin/codex",
        model="",
        capabilities=("plan",),
        priority=100,
    )
    project = ProjectDescriptor(
        id="app",
        directory=tmp_path,
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
    intent = TaskIntent.create("Add a health endpoint")
    scope = RepositorySelector().select(project, intent.description)

    artifact = planner.agent_draft(
        intent=intent,
        project=project,
        repository_scope=scope,
        workspace_root=tmp_path,
        provider=ProviderChoice(provider_item, _provider_config(), "test"),
    )

    assert artifact.generated_by == "agent"
    assert artifact.provider_id == "codex"
    generated_package = artifact.graph["work_packages"][0]
    assert "stage" not in generated_package
    assert "status" not in generated_package
    assert adapter.handoff.read_only is True
    assert adapter.handoff.required_isolation == "provider_policy"
    assert adapter.handoff.stage == "plan"


def test_local_draft_is_nonempty_and_publishes_atomically(tmp_path: Path) -> None:
    planner = DraftPlanService()
    intent = TaskIntent.create("Add a health endpoint")
    artifact = planner.local_draft(intent=intent, repositories=("app",))
    dossier = tmp_path / "task"
    dossier.mkdir()
    (dossier / "PLAN.md").write_text("old\n", encoding="utf-8")

    markdown, graph = planner.publish(
        dossier=dossier,
        artifact=artifact,
        allowed_repositories={"app"},
    )

    assert "bootstrap draft" in markdown.read_text(encoding="utf-8")
    parsed = yaml.safe_load(graph.read_text(encoding="utf-8"))
    assert len(parsed["work_packages"]) == 1
    assert not list(dossier.glob(".*.tmp"))


def test_start_preview_is_side_effect_free_then_run_is_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _configure_xdg(monkeypatch, tmp_path)
    source = tmp_path / "source" / "sample"
    _init_git(source)
    (source / "pyproject.toml").write_text(
        "[project]\nname='sample'\nversion='0.1.0'\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "add project metadata"], cwd=source, check=True)
    service = StartWorkflowService(
        home=home,
        onboarding=bootstrap.create_onboarding_service(),
    )
    request = StartRequest(
        description="Add a health endpoint",
        source_root=source,
        planner_mode=PlannerMode.LOCAL,
        workspace_root=tmp_path / "workspaces" / "sample" / "health",
    )

    preview = service.preview(request)
    assert preview.applied is False
    assert not home.root.exists()
    assert preview.project_plan is not None
    assert preview.task_plan is not None

    outcome = service.run(request)
    assert outcome.ready is True
    assert outcome.journal_path and outcome.journal_path.is_file()
    dossier = project_task_directory(home.root, "sample", outcome.task_id)
    assert (dossier / "PLAN.graph.yaml").is_file()
    assert load_manifest(home.root, outcome.task_id).status == "planned"
    record = load_workspace(home.root, outcome.task_id)
    assert Path(record.workspace_root).is_dir()

    repeated = service.run(request)
    assert repeated.task_id == outcome.task_id
    assert [item.status.value for item in repeated.steps] == [
        "reused",
        "reused",
        "reused",
        "reused",
    ]


def test_start_uses_collision_suffix_for_different_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _configure_xdg(monkeypatch, tmp_path)
    source = tmp_path / "source" / "app"
    _init_git(source)
    service = StartWorkflowService(home=home, onboarding=bootstrap.create_onboarding_service())

    first = service.run(
        StartRequest(
            description="Add health endpoint",
            source_root=source,
            planner_mode=PlannerMode.LOCAL,
            no_workspace=True,
        )
    )
    second = service.run(
        StartRequest(
            description="ADD HEALTH ENDPOINT",
            source_root=source,
            planner_mode=PlannerMode.LOCAL,
            no_workspace=True,
        )
    )
    assert first.task_id == "add-health-endpoint"
    assert second.task_id == "add-health-endpoint-2"


def test_start_require_provider_fails_before_task_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _configure_xdg(monkeypatch, tmp_path)
    source = tmp_path / "source" / "app"
    _init_git(source)
    service = StartWorkflowService(home=home, onboarding=bootstrap.create_onboarding_service())
    request = StartRequest(
        description="Add health endpoint",
        source_root=source,
        planner_mode=PlannerMode.AUTO,
        require_provider=True,
        no_workspace=True,
    )

    with pytest.raises(StartWorkflowError, match="No enabled local provider"):
        service.run(request)
    assert not project_task_directory(home.root, "app", "add-health-endpoint").exists()
    assert not (home.root / ".registry" / "tasks" / "app" / "add-health-endpoint.yaml").exists()


def test_greenfield_dry_run_and_create_initialize_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _configure_xdg(monkeypatch, tmp_path)
    service = GreenfieldService(
        onboarding=bootstrap.create_onboarding_service(),
        templates=default_greenfield_catalog(),
    )

    preview = service.create(
        name="demo-service",
        parent=tmp_path / "sources",
        descriptor_output=home.projects_dir,
        source_template="python-service",
        dry_run=True,
    )
    assert preview.applied is False
    assert not (tmp_path / "sources" / "demo-service").exists()
    assert {item.path for item in preview.source_plan.files} >= {
        "pyproject.toml",
        "tests/test_smoke.py",
    }

    outcome = service.create(
        name="demo-service",
        parent=tmp_path / "sources",
        descriptor_output=home.projects_dir,
        source_template="python-service",
    )
    assert outcome.applied is True
    assert outcome.source_root is not None
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=outcome.source_root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert len(head) == 40
    project = load_project(outcome.project_outcome.path.parent)
    assert project.id == "demo-service"

class _StaticProviderSelector:
    def __init__(self, choice: ProviderChoice) -> None:
        self.choice = choice

    def select(self, *_args, **_kwargs) -> ProviderChoice:
        return self.choice


class _FailingAgentPlanner(DraftPlanService):
    def agent_draft(self, **_kwargs):
        raise StartWorkflowError("simulated planner failure")


def test_plan_failure_is_journaled_and_can_resume_locally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _configure_xdg(monkeypatch, tmp_path)
    source = tmp_path / "source" / "resumable"
    _init_git(source)
    provider_item = ProviderInventoryItem(
        name="codex",
        provider_id="codex",
        adapter="codex",
        enabled=True,
        binary="codex",
        binary_path="/usr/bin/codex",
        model="",
        capabilities=("plan",),
        priority=100,
    )
    choice = ProviderChoice(provider_item, _provider_config(), "test provider")
    request = StartRequest(
        description="Add resilient startup",
        source_root=source,
        planner_mode=PlannerMode.AGENT,
        workspace_root=tmp_path / "workspaces" / "resumable" / "startup",
    )
    failing = StartWorkflowService(
        home=home,
        onboarding=bootstrap.create_onboarding_service(),
        provider_selector=_StaticProviderSelector(choice),
        planner=_FailingAgentPlanner(),
    )

    with pytest.raises(StartWorkflowError, match="simulated planner failure"):
        failing.run(request)

    journal_path = (
        home.state_dir / "starts" / "resumable" / "add-resilient-startup.yaml"
    )
    journal = yaml.safe_load(journal_path.read_text(encoding="utf-8"))
    assert journal["steps"]["project"]["status"] == "completed"
    assert journal["steps"]["task"]["status"] == "completed"
    assert journal["steps"]["workspace"]["status"] == "completed"
    assert journal["steps"]["plan"]["status"] == "failed"
    dossier = project_task_directory(home.root, "resumable", "add-resilient-startup")
    assert dossier.is_dir()
    assert not (dossier / "PLAN.graph.yaml").exists()
    workspace = load_workspace(home.root, "add-resilient-startup")
    assert Path(workspace.workspace_root).is_dir()

    resumed = StartWorkflowService(
        home=home,
        onboarding=bootstrap.create_onboarding_service(),
    ).run(
        StartRequest(
            description="Add resilient startup",
            source_root=source,
            planner_mode=PlannerMode.LOCAL,
            workspace_root=tmp_path / "workspaces" / "resumable" / "startup",
        )
    )
    assert resumed.ready is True
    assert [item.status.value for item in resumed.steps] == [
        "reused",
        "reused",
        "reused",
        "completed",
    ]
    journal = yaml.safe_load(journal_path.read_text(encoding="utf-8"))
    assert journal["steps"]["plan"]["status"] == "completed"
    assert journal["steps"]["complete"]["status"] == "completed"


def test_start_journal_rejects_source_route_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _configure_xdg(monkeypatch, tmp_path)
    source = tmp_path / "source" / "journal-app"
    _init_git(source)
    service = StartWorkflowService(home=home, onboarding=bootstrap.create_onboarding_service())
    request = StartRequest(
        description="Add journal safety",
        source_root=source,
        planner_mode=PlannerMode.LOCAL,
        no_workspace=True,
    )
    outcome = service.run(request)
    assert outcome.journal_path is not None
    journal = yaml.safe_load(outcome.journal_path.read_text(encoding="utf-8"))
    journal["source_root"] = str(tmp_path / "different-source")
    outcome.journal_path.write_text(
        yaml.safe_dump(journal, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(StartWorkflowError, match="does not match this workflow"):
        service.run(request)


def test_provider_selector_rejects_advisory_read_only_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "agy"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    agents = tmp_path / "agents.yaml"
    agents.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "providers": {
                    "unsafe": {
                        "adapter": "antigravity-cli",
                        "enabled": True,
                        "provider_id": "unsafe",
                        "binary": str(binary),
                        "capabilities": ["plan"],
                        "priority": 100,
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    project = ProjectDescriptor(
        id="provider-test",
        directory=tmp_path,
        repositories=(
            ProjectRepository(
                id="provider-test",
                path=".",
                workspace_name="provider-test",
                base_branch="main",
            ),
        ),
        paths={"agents_file": "agents.yaml"},
        path_base="project_directory",
    )

    choice = ProviderSelector().select(project, workdir=tmp_path, requested="unsafe")
    assert choice.available is False
    assert "does not enforce read-only policy" in choice.reason


def test_greenfield_rolls_back_source_when_git_or_registration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _configure_xdg(monkeypatch, tmp_path)
    onboarding = bootstrap.create_onboarding_service()
    service = GreenfieldService(onboarding=onboarding)
    target = tmp_path / "sources" / "broken-service"

    import execraft.onboarding.greenfield as greenfield_module

    monkeypatch.setattr(
        greenfield_module,
        "_initialize_git",
        lambda _path: (_ for _ in ()).throw(StartWorkflowError("git failed")),
    )
    with pytest.raises(StartWorkflowError, match="git failed"):
        service.create(
            name="broken-service",
            parent=tmp_path / "sources",
            descriptor_output=home.projects_dir,
        )
    assert not target.exists()

    monkeypatch.undo()
    home = _configure_xdg(monkeypatch, tmp_path)
    onboarding = bootstrap.create_onboarding_service()
    service = GreenfieldService(onboarding=onboarding)

    def fail_registration(**_kwargs):
        raise StartWorkflowError("registration failed")

    monkeypatch.setattr(onboarding, "create_project", fail_registration)
    with pytest.raises(StartWorkflowError, match="registration failed"):
        service.create(
            name="broken-service",
            parent=tmp_path / "sources",
            descriptor_output=home.projects_dir,
        )
    assert not target.exists()


def test_explicit_existing_task_must_match_original_brief(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _configure_xdg(monkeypatch, tmp_path)
    source = tmp_path / "source" / "brief-app"
    _init_git(source)
    onboarding = bootstrap.create_onboarding_service()
    report = onboarding.inspect_project(source)
    project_outcome = onboarding.create_project(
        report=report,
        output_dir=home.projects_dir,
        register=True,
        dry_run=False,
    )
    assert project_outcome.path is not None
    project = load_project(project_outcome.path.parent)
    onboarding.create_task(
        control_root=home.root,
        project=project,
        task_id="fixed-task",
        title="Shared title",
        brief="A different original request",
        state_root=home.state_dir,
    )
    service = StartWorkflowService(home=home, onboarding=onboarding)

    with pytest.raises(StartWorkflowError, match="brief does not contain"):
        service.preview(
            StartRequest(
                description="Expected original request",
                source_root=source,
                task_id="fixed-task",
                title="Shared title",
                planner_mode=PlannerMode.LOCAL,
                no_workspace=True,
            )
        )


def test_invalid_existing_plan_is_replaced_instead_of_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _configure_xdg(monkeypatch, tmp_path)
    source = tmp_path / "source" / "plan-app"
    _init_git(source)
    service = StartWorkflowService(home=home, onboarding=bootstrap.create_onboarding_service())
    request = StartRequest(
        description="Add plan validation",
        source_root=source,
        planner_mode=PlannerMode.LOCAL,
        no_workspace=True,
    )
    initial = service.run(request)
    dossier = project_task_directory(home.root, "plan-app", initial.task_id)
    graph_path = dossier / "PLAN.graph.yaml"
    graph = yaml.safe_load(graph_path.read_text(encoding="utf-8"))
    graph["work_packages"][0]["affected_repositories"] = ["outside-scope"]
    graph_path.write_text(yaml.safe_dump(graph, sort_keys=False), encoding="utf-8")

    repaired = service.run(request)
    assert repaired.steps[-1].status.value == "completed"
    repaired_graph = yaml.safe_load(graph_path.read_text(encoding="utf-8"))
    assert repaired_graph["work_packages"][0]["affected_repositories"] == ["plan-app"]


def test_plan_publication_restores_both_files_when_graph_commit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import execraft.onboarding.planning as planning_module

    planner = DraftPlanService()
    dossier = tmp_path / "task"
    dossier.mkdir()
    markdown_path = dossier / "PLAN.md"
    graph_path = dossier / "PLAN.graph.yaml"
    markdown_path.write_text("old markdown\n", encoding="utf-8")
    graph_path.write_text("schema_version: 1\nwork_packages: []\n", encoding="utf-8")
    old_markdown = markdown_path.read_bytes()
    old_graph = graph_path.read_bytes()
    artifact = planner.local_draft(
        intent=TaskIntent.create("Add atomic planning"),
        repositories=("app",),
    )
    real_replace = planning_module.os.replace
    failed = False

    def fail_graph_commit(source, destination):
        nonlocal failed
        if Path(destination) == graph_path and not failed:
            failed = True
            raise OSError("simulated graph commit failure")
        return real_replace(source, destination)

    monkeypatch.setattr(planning_module.os, "replace", fail_graph_commit)
    with pytest.raises(OSError, match="simulated graph commit failure"):
        planner.publish(
            dossier=dossier,
            artifact=artifact,
            allowed_repositories={"app"},
        )
    assert markdown_path.read_bytes() == old_markdown
    assert graph_path.read_bytes() == old_graph
    assert not list(dossier.glob(".*.tmp"))


def test_workspace_failure_restores_previous_source_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execraft.project import load_project_binding
    import execraft.workspace.lifecycle as lifecycle_module

    home = _configure_xdg(monkeypatch, tmp_path)
    source = tmp_path / "source" / "binding-app"
    alternate = tmp_path / "alternate" / "binding-app"
    _init_git(source)
    _init_git(alternate)
    onboarding = bootstrap.create_onboarding_service()
    report = onboarding.inspect_project(source)
    project_outcome = onboarding.create_project(
        report=report,
        output_dir=home.projects_dir,
        register=True,
        dry_run=False,
    )
    assert project_outcome.path is not None
    project = load_project(project_outcome.path.parent)
    onboarding.create_task(
        control_root=home.root,
        project=project,
        task_id="binding-failure",
        title="Binding failure",
        brief="Test binding rollback",
        state_root=home.state_dir,
    )
    manifest = load_manifest(home.root, "binding-failure")
    before = load_project_binding("binding-app")
    assert before is not None and before.source_root == source.resolve()

    def fail_render(*_args, **_kwargs):
        raise RuntimeError("simulated render failure")

    monkeypatch.setattr(lifecycle_module, "render_workspace_record", fail_render)
    workspace_root = tmp_path / "workspaces" / "binding-app" / "binding-failure"
    with pytest.raises(RuntimeError, match="simulated render failure"):
        lifecycle_module.prepare_workspace(
            control_root=home.root,
            manifest=manifest,
            project=project,
            source_root_override=alternate,
            workspace_root=workspace_root,
        )

    restored = load_project_binding("binding-app")
    assert restored is not None and restored.source_root == source.resolve()
    assert not workspace_root.exists()
    branch = subprocess.run(
        ["git", "branch", "--list", "task/binding-failure"],
        cwd=alternate,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert branch == ""


def test_new_local_plans_are_declarative_and_replan_ready() -> None:
    from execraft.plan_contract import validate_declarative_plan_graph_mapping

    artifact = DraftPlanService().local_draft(
        intent=TaskIntent.create("Create a declarative task"),
        repositories=("app",),
    )
    package = artifact.graph["work_packages"][0]

    assert "stage" not in package
    assert "status" not in package
    assert "generated_by" not in package
    assert all(
        "verified" not in criterion and "evidence" not in criterion
        for criterion in package["acceptance_criteria"]
    )
    validate_declarative_plan_graph_mapping(artifact.graph)
