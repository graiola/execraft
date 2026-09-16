"""WP1 regression tests for task-definition import and executable-plan preservation."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from execraft import bootstrap
from execraft.agents.config import AgentProviderConfig
from execraft.control_plane import ControlPlaneHome
from execraft.onboarding.planning import DraftPlanService
from execraft.onboarding.providers import ProviderInventoryItem
from execraft.onboarding.start import (
    PlannerMode,
    ProviderChoice,
    RepositorySelector,
    StartRequest,
    StartWorkflowError,
    StartWorkflowService,
    TaskIntent,
)
from execraft.onboarding.task_definition import (
    TaskDefinitionError,
    TaskDefinitionInput,
)
from execraft.orchestrate.scheduler import AgentCapability
from execraft.project import ProjectDescriptor, ProjectRepository, load_project
from execraft.workspace.task_git import project_task_directory


def _init_git(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)


def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ControlPlaneHome:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "data" / "execraft" / "control"
    monkeypatch.setenv("EXECRAFT_CONTROL_ROOT", str(root))
    return ControlPlaneHome.resolve()


def _registered_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[ControlPlaneHome, Path]:
    home = _home(monkeypatch, tmp_path)
    source = tmp_path / "source" / "app"
    _init_git(source)
    onboarding = bootstrap.create_onboarding_service()
    outcome = onboarding.create_project(
        report=onboarding.inspect_project(source),
        output_dir=home.projects_dir,
        register=True,
    )
    assert outcome.path is not None
    return home, source


def _valid_graph(repository: str = "app") -> str:
    return yaml.safe_dump(
        {
            "schema_version": 1,
            "source_document": "PLAN.md",
            "work_packages": [
                {
                    "id": "WP01",
                    "title": "Imported work",
                    "dependencies": [],
                    "requirements": ["Implement imported behavior"],
                    "acceptance_criteria": [
                        {"id": "done", "description": "Imported behavior works"}
                    ],
                    "affected_repositories": [repository],
                    "stage": "prepare",
                    "status": "pending",
                    "risk": "low",
                    "priority": 10,
                    "verification_profile": "focused",
                }
            ],
        },
        sort_keys=False,
    )


def test_definition_path_import_normalizes_newlines_and_rejects_symlink(
    tmp_path: Path,
) -> None:
    brief = tmp_path / "BRIEF.md"
    brief.write_bytes(b"# Brief: Existing\r\n\r\nBody\r\n")
    imported = TaskDefinitionInput.from_paths(brief_file=brief)

    assert imported.brief_markdown == "# Brief: Existing\n\nBody\n"
    assert imported.sources["BRIEF.md"] == str(brief.resolve())
    assert len(imported.source_fingerprint()) == 64

    alias = tmp_path / "brief-link.md"
    alias.symlink_to(brief)
    with pytest.raises(TaskDefinitionError, match="symbolic link"):
        TaskDefinitionInput.from_paths(brief_file=alias)


def test_task_creation_preserves_imported_documents_and_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, source = _registered_project(monkeypatch, tmp_path)
    project = load_project(home.projects_dir / "app")
    brief = "# Brief: Imported contract\n\n## Goal\nKeep this text exactly."
    plan = "# Plan: Imported contract\n\n## WP1\nDo the imported work."
    definition = TaskDefinitionInput.from_contents(
        brief_markdown=brief,
        plan_markdown=plan,
        brief_source="test:BRIEF.md",
        plan_source="test:PLAN.md",
    )

    outcome = bootstrap.create_onboarding_service().create_task(
        control_root=home.root,
        project=project,
        task_id="imported-contract",
        title="",
        state_root=home.state_dir,
        definition=definition,
    )
    assert outcome.path is not None
    dossier = outcome.path
    assert (dossier / "BRIEF.md").read_text(encoding="utf-8") == brief
    assert (dossier / "PLAN.md").read_text(encoding="utf-8") == plan
    assert (dossier / "imports/revision-0001/BRIEF.md").read_text() == brief
    assert (dossier / "imports/revision-0001/PLAN.md").read_text() == plan

    metadata = yaml.safe_load((dossier / "DEFINITION.yaml").read_text())
    assert metadata["revision"] == 1
    assert metadata["sources"]["BRIEF.md"]["origin"] == "imported"
    assert metadata["sources"]["PLAN.md"]["origin"] == "imported"
    assert metadata["sources"]["PLAN.md"]["source"] == "test:PLAN.md"
    assert metadata["current"]["PLAN.md"] == hashlib.sha256(plan.encode()).hexdigest()
    assert metadata["source_fingerprint"] == definition.source_fingerprint()
    assert source.is_dir()


def test_plan_only_start_preserves_plan_generates_brief_graph_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, source = _registered_project(monkeypatch, tmp_path)
    plan = "# Plan: Imported rollout\n\n## Scope\nAdd a health endpoint without changing auth."
    definition = TaskDefinitionInput.from_contents(
        plan_markdown=plan,
        plan_source="browser-upload:PLAN.md",
    )
    service = StartWorkflowService(
        home=home,
        onboarding=bootstrap.create_onboarding_service(),
    )
    request = StartRequest(
        description="",
        source_root=source,
        task_id="imported-rollout",
        planner_mode=PlannerMode.LOCAL,
        no_workspace=True,
        task_definition=definition,
    )

    preview = service.preview(request)
    assert preview.task_id == "imported-rollout"
    assert preview.plan_artifact is not None
    assert preview.plan_artifact.generated_by == "local-import"

    outcome = service.run(request)
    dossier = project_task_directory(home.root, "app", outcome.task_id)
    assert (dossier / "PLAN.md").read_text(encoding="utf-8") == plan
    assert "created from an imported implementation plan" in (
        dossier / "BRIEF.md"
    ).read_text(encoding="utf-8")
    graph = yaml.safe_load((dossier / "PLAN.graph.yaml").read_text())
    assert graph["source_document"] == "PLAN.md"

    metadata = yaml.safe_load((dossier / "DEFINITION.yaml").read_text())
    assert metadata["sources"]["PLAN.md"]["origin"] == "imported"
    assert metadata["sources"]["PLAN.graph.yaml"]["origin"] == "generated"
    assert metadata["consistency"]["mode"] == "structural"

    journal = yaml.safe_load(outcome.journal_path.read_text())
    expected = journal["request"]["expected_request_sha256"]
    assert journal["request"]["title"] == "Imported rollout"
    resumed = service.run(
        StartRequest(
            description=journal["description"],
            source_root=source,
            task_id=outcome.task_id,
            title=journal["request"]["title"],
            planner_mode=PlannerMode.LOCAL,
            no_workspace=True,
            expected_request_sha256=expected,
        )
    )
    assert any(step.id == "task" and step.status.value == "reused" for step in resumed.steps)
    assert any(step.id == "plan" and step.status.value == "reused" for step in resumed.steps)


def test_complete_import_runs_without_provider_even_in_agent_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, source = _registered_project(monkeypatch, tmp_path)
    plan = "# Plan: Offline import\n\nUse the supplied graph."
    definition = TaskDefinitionInput.from_contents(
        brief_markdown="# Brief: Offline import\n\nImplement the supplied plan.",
        plan_markdown=plan,
        plan_graph_yaml=_valid_graph(),
    )
    service = StartWorkflowService(
        home=home,
        onboarding=bootstrap.create_onboarding_service(),
    )

    outcome = service.run(
        StartRequest(
            description="",
            source_root=source,
            task_id="offline-import",
            planner_mode=PlannerMode.AGENT,
            no_workspace=True,
            task_definition=definition,
        )
    )
    assert outcome.ready is True
    assert outcome.plan_artifact is not None
    assert outcome.plan_artifact.generated_by == "existing"
    dossier = project_task_directory(home.root, "app", "offline-import")
    assert (dossier / "PLAN.md").read_text() == plan


def test_imported_graph_is_rejected_when_it_escapes_repository_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, source = _registered_project(monkeypatch, tmp_path)
    service = StartWorkflowService(
        home=home,
        onboarding=bootstrap.create_onboarding_service(),
    )
    definition = TaskDefinitionInput.from_contents(
        plan_markdown="# Plan: Invalid\n",
        plan_graph_yaml=_valid_graph("other-repository"),
    )

    with pytest.raises(TaskDefinitionError, match="outside task scope"):
        service.preview(
            StartRequest(
                description="",
                source_root=source,
                planner_mode=PlannerMode.LOCAL,
                no_workspace=True,
                task_definition=definition,
            )
        )


class _FakeAdapter:
    def __init__(self, response: str) -> None:
        self.response = response
        self.handoff = None

    def execute(self, handoff):
        self.handoff = handoff
        return {"ok": True, "final_message": self.response}


def _provider_choice() -> ProviderChoice:
    item = ProviderInventoryItem(
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
    config = AgentProviderConfig(
        name="codex",
        adapter="codex",
        enabled=True,
        provider_id="codex",
        binary="codex",
        capabilities=frozenset({AgentCapability.PLAN}),
        capability_weight=100,
        priority=100,
    )
    return ProviderChoice(item, config, "test")


def test_agent_import_planning_rejects_semantic_brief_plan_conflict(tmp_path: Path) -> None:
    response = json.dumps(
        {
            "consistent": False,
            "consistency_summary": "The brief requires deletion while the plan forbids deletion.",
            "plan_graph": {},
        }
    )
    adapter = _FakeAdapter(response)
    planner = DraftPlanService(adapter_builder=lambda *_args, **_kwargs: adapter)
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
    intent = TaskIntent.create("Resolve imported plan", title="Imported")
    scope = RepositorySelector().select(project, intent.description)

    with pytest.raises(StartWorkflowError, match="materially inconsistent"):
        planner.agent_graph_from_imported_plan(
            intent=intent,
            brief_markdown="# Brief\nDelete legacy data.",
            plan_markdown="# Plan\nNever delete legacy data.",
            project=project,
            repository_scope=scope,
            workspace_root=tmp_path,
            provider=_provider_choice(),
        )
    assert adapter.handoff is not None
    assert adapter.handoff.read_only is True
    assert "without rewriting either imported document" in adapter.handoff.summary


def test_definition_title_falls_back_to_plan_content_and_graph_title() -> None:
    from_plan = TaskDefinitionInput.from_contents(
        plan_markdown="Implement deterministic import behavior without an H1 heading.\n"
    )
    assert from_plan.suggested_title() == "Implement deterministic import behavior without an H1 heading"

    from_graph = TaskDefinitionInput.from_contents(plan_graph_yaml=_valid_graph())
    assert from_graph.suggested_title() == "Imported work"


def test_imported_graph_records_structural_consistency_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, _source = _registered_project(monkeypatch, tmp_path)
    project = load_project(home.projects_dir / "app")
    definition = TaskDefinitionInput.from_contents(
        brief_markdown="# Brief: Complete\n\nImported brief.",
        plan_markdown="# Plan: Complete\n\nImported plan.",
        plan_graph_yaml=_valid_graph(),
    )
    outcome = bootstrap.create_onboarding_service().create_task(
        control_root=home.root,
        project=project,
        task_id="complete-definition",
        title="",
        state_root=home.state_dir,
        definition=definition,
    )
    assert outcome.path is not None
    metadata = yaml.safe_load((outcome.path / "DEFINITION.yaml").read_text())
    assert metadata["consistency"]["mode"] == "structural"
    assert "repository-scope validation" in metadata["consistency"]["summary"]


def test_agent_import_planning_rejects_invalid_boolean_contract(tmp_path: Path) -> None:
    response = json.dumps(
        {
            "consistent": "false",
            "consistency_summary": "not actually a boolean",
            "plan_graph": {},
        }
    )
    adapter = _FakeAdapter(response)
    planner = DraftPlanService(adapter_builder=lambda *_args, **_kwargs: adapter)
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
    intent = TaskIntent.create("Resolve imported plan", title="Imported")
    scope = RepositorySelector().select(project, intent.description)

    with pytest.raises(StartWorkflowError, match="consistent must be a boolean"):
        planner.agent_graph_from_imported_plan(
            intent=intent,
            brief_markdown="# Brief\nDo work.",
            plan_markdown="# Plan\nDo work.",
            project=project,
            repository_scope=scope,
            workspace_root=tmp_path,
            provider=_provider_choice(),
        )


def test_agent_import_planning_enforces_provider_context_budget(tmp_path: Path) -> None:
    adapter = _FakeAdapter("{}")
    planner = DraftPlanService(adapter_builder=lambda *_args, **_kwargs: adapter)
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
    intent = TaskIntent.create("Resolve imported plan", title="Imported")
    scope = RepositorySelector().select(project, intent.description)

    with pytest.raises(StartWorkflowError, match="import consistency budget"):
        planner.agent_graph_from_imported_plan(
            intent=intent,
            brief_markdown="# Brief\n" + ("x" * (140 * 1024)),
            plan_markdown="# Plan\n" + ("y" * (140 * 1024)),
            project=project,
            repository_scope=scope,
            workspace_root=tmp_path,
            provider=_provider_choice(),
        )
    assert adapter.handoff is None


def test_brief_only_start_preserves_brief_and_generates_plan_and_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, source = _registered_project(monkeypatch, tmp_path)
    brief = "# Brief: Existing brief\n\n## Goal\nImplement a deterministic health check.\n"
    service = StartWorkflowService(
        home=home,
        onboarding=bootstrap.create_onboarding_service(),
    )
    outcome = service.run(
        StartRequest(
            description="",
            source_root=source,
            task_id="brief-only",
            planner_mode=PlannerMode.LOCAL,
            no_workspace=True,
            task_definition=TaskDefinitionInput.from_contents(
                brief_markdown=brief,
                brief_source="test:BRIEF.md",
            ),
        )
    )

    assert outcome.ready is True
    dossier = project_task_directory(home.root, "app", "brief-only")
    assert (dossier / "BRIEF.md").read_text(encoding="utf-8") == brief
    assert (dossier / "PLAN.md").is_file()
    assert (dossier / "PLAN.graph.yaml").is_file()
    metadata = yaml.safe_load((dossier / "DEFINITION.yaml").read_text())
    assert metadata["sources"]["BRIEF.md"]["origin"] == "imported"
    assert metadata["sources"]["PLAN.md"]["origin"] == "generated"
    assert metadata["sources"]["PLAN.graph.yaml"]["origin"] == "generated"


def test_plan_and_graph_import_derives_brief_without_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, source = _registered_project(monkeypatch, tmp_path)
    plan = "# Plan: Existing executable plan\n\nFollow the imported graph.\n"
    service = StartWorkflowService(
        home=home,
        onboarding=bootstrap.create_onboarding_service(),
    )
    outcome = service.run(
        StartRequest(
            description="",
            source_root=source,
            task_id="plan-and-graph",
            planner_mode=PlannerMode.AGENT,
            no_workspace=True,
            task_definition=TaskDefinitionInput.from_contents(
                plan_markdown=plan,
                plan_graph_yaml=_valid_graph(),
            ),
        )
    )

    assert outcome.ready is True
    assert outcome.plan_artifact is not None
    assert outcome.plan_artifact.generated_by == "existing"
    dossier = project_task_directory(home.root, "app", "plan-and-graph")
    assert (dossier / "PLAN.md").read_text(encoding="utf-8") == plan
    assert "created from an imported implementation plan" in (
        dossier / "BRIEF.md"
    ).read_text(encoding="utf-8")
    metadata = yaml.safe_load((dossier / "DEFINITION.yaml").read_text())
    assert metadata["sources"]["PLAN.md"]["origin"] == "imported"
    assert metadata["sources"]["PLAN.graph.yaml"]["origin"] == "imported"
    assert metadata["consistency"]["mode"] == "structural"
