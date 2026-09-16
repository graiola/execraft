"""Sandbox reach for handoffs whose repositories live outside the workspace.

A task workspace root and the product checkouts it drives are normally separate
trees.  When the declared roots omit the checkouts, a sandboxed provider reports
the repositories as read-only and the run escalates as an unrecoverable
environment incident instead of doing the work.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from execraft.orchestrate import (
    AcceptanceCriterion,
    AgentCapability,
    Availability,
    OrchestrationConfig,
    ProjectOrchestrator,
    TaskExecutionState,
    VerificationCommand,
    VerificationRegistry,
    normalize_work_packages,
)
from execraft.orchestrate.handoff_access import (
    access_roots,
    declare_access_roots,
    stage_is_project_wide,
)
from execraft.orchestrate.models import OrchestrateError, WorkPackage
from execraft.orchestrate.scheduler import StructuredHandoff


class _Host:
    """Minimal stand-in for the orchestration facade's resolver surface."""

    def __init__(self, repository_paths: dict[str, Path], dossier: Path | None = None):
        self._repository_paths = repository_paths
        self._task_dossier_dir = dossier

    def _resolve_repo_path_if_available(self, repository_id: str) -> Path | None:
        if repository_id not in self._repository_paths:
            raise OrchestrateError(f"required repository is not present: {repository_id}")
        return self._repository_paths[repository_id]


@pytest.fixture
def product(tmp_path: Path) -> dict[str, Path]:
    """A product tree and a task workspace that are siblings, as in production."""

    stack = tmp_path / "product" / "stack"
    core = stack / "source" / "core"
    ui = stack / "source" / "sample"
    for path in (stack, core, ui):
        path.mkdir(parents=True)
    workspace = tmp_path / "ai-workspaces" / "task"
    workspace.mkdir(parents=True)
    return {"stack": stack, "core": core, "ui": ui, "workspace": workspace}


def _package(repositories: list[str]) -> WorkPackage:
    return WorkPackage(id="wp1", title="t", affected_repositories=list(repositories))


def test_declared_scope_reaches_repositories_outside_the_working_directory(product):
    host = _Host({"core": product["core"], "frontend": product["ui"]})
    handoff = StructuredHandoff(
        work_package_id="wp1",
        stage="implement",
        summary="s",
        working_directory=str(product["workspace"]),
    )

    updated = declare_access_roots(host, handoff, _package(["core"]))

    assert updated.additional_writable_roots == [str(product["core"])]


def test_repositories_inside_the_working_directory_are_not_redeclared(product):
    host = _Host({"core": product["core"]})
    handoff = StructuredHandoff(
        work_package_id="wp1",
        stage="implement",
        summary="s",
        working_directory=str(product["stack"]),
    )

    updated = declare_access_roots(host, handoff, _package(["core"]))

    assert updated.additional_writable_roots == []
    assert updated is handoff


def test_supervision_reaches_every_configured_repository_and_the_dossier(product, tmp_path):
    dossier = tmp_path / "control" / "projects" / "sample" / "tasks" / "task"
    host = _Host(
        {"core": product["core"], "frontend": product["ui"]}, dossier=dossier
    )
    handoff = StructuredHandoff(
        work_package_id="wp1",
        stage="supervise",
        summary="s",
        working_directory=str(product["workspace"]),
    )

    # The package declares one repository; supervision may repair all of them.
    updated = declare_access_roots(host, handoff, _package(["core"]))

    assert updated.additional_writable_roots == [
        str(dossier.parents[1]),
        str(product["core"]),
        str(product["ui"]),
    ]


@pytest.mark.parametrize(
    "stage, project_wide",
    [
        ("implement", False),
        ("final_review", False),
        ("supervise", True),
        ("scope_recovery", True),
        ("supervisor_delegate_implement", True),
    ],
)
def test_project_wide_stages(stage, project_wide):
    assert stage_is_project_wide(stage) is project_wide


def test_nested_checkouts_declare_only_the_outermost_root(product):
    host = _Host(
        {
            "app": product["stack"],
            "core": product["core"],
            "frontend": product["ui"],
        }
    )
    handoff = StructuredHandoff(
        work_package_id="wp1",
        stage="scope_recovery",
        summary="s",
        working_directory=str(product["workspace"]),
    )

    updated = declare_access_roots(host, handoff, _package([]))

    assert updated.additional_writable_roots == [str(product["stack"])]


def test_missing_and_stale_repositories_are_skipped(product, tmp_path):
    host = _Host({"core": product["core"], "gone": tmp_path / "absent"})
    handoff = StructuredHandoff(
        work_package_id="wp1",
        stage="implement",
        summary="s",
        working_directory=str(product["workspace"]),
    )

    updated = declare_access_roots(host, handoff, _package(["core", "unknown"]))

    assert updated.additional_writable_roots == [str(product["core"])]
    assert access_roots(host._repository_paths, [], "", include_all_repositories=True) == [
        str(product["core"])
    ]


class _CapturingAgent:
    """Records the handoff each stage receives and satisfies the stage contract."""

    def __init__(self, repo: Path, provider_id: str = "capturing-agent"):
        self.repo = repo
        self.handoffs: list[StructuredHandoff] = []
        self._provider_id = provider_id

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def availability(self) -> Availability:
        return Availability.AVAILABLE

    @property
    def capabilities(self) -> set[AgentCapability]:
        return {
            AgentCapability.IMPLEMENT,
            AgentCapability.REVIEW,
            AgentCapability.FIX_REVIEW,
        }

    def execute(self, handoff):
        self.handoffs.append(handoff)
        if handoff.stage in {"implement", "fix_review"}:
            (self.repo / "implemented.txt").write_text("done\n", encoding="utf-8")
            return {
                "ok": True,
                "status": "implemented" if handoff.stage == "implement" else "fixed",
                "summary": "implemented fixture",
                "acceptance_evidence": {"works": "implemented.txt"},
            }
        return {"ok": True, "verdict": "approved", "findings": [], "summary": "approved"}


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, text=True, capture_output=True, check=True
    ).stdout.strip()


def test_implement_stage_declares_a_repository_outside_the_workspace_root(tmp_path):
    """The end-to-end regression: a sibling product checkout stays writable."""

    repo = tmp_path / "product" / "stack"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "execraft@example.invalid")
    _git(repo, "config", "user.name", "Execraft tests")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "baseline")
    workspace = tmp_path / "ai-workspaces" / "task"
    workspace.mkdir(parents=True)

    class _Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    orch = ProjectOrchestrator(
        "task",
        config=OrchestrationConfig(state_dir=tmp_path / "state"),
        registry=VerificationRegistry(
            commands=[
                VerificationCommand(
                    id="fixture",
                    command="fixture-check",
                    profile="focused",
                    repository_id="stack",
                )
            ],
            require_commands=True,
        ),
        command_runner=lambda command, *, cwd, timeout: _Completed(),
        repository_paths={"stack": repo},
        workspace_root=workspace,
    )
    agent = _CapturingAgent(repo)
    reviewer = _CapturingAgent(repo, provider_id="capturing-reviewer")
    orch.register_agent(agent)
    orch.register_agent(reviewer)
    graph, report = normalize_work_packages(
        [
            WorkPackage(
                id="wp1",
                title="Package",
                requirements=["Implement the fixture"],
                acceptance_criteria=[AcceptanceCriterion(id="works", description="Works")],
                affected_repositories=["stack"],
                verification_profile="focused",
            )
        ]
    )
    orch.initialize_graph(graph, report)

    orch.run_pipeline()

    assert orch.state == TaskExecutionState.COMPLETED
    implement = next(item for item in agent.handoffs if item.stage == "implement")
    # A single-repository package already runs with the checkout as its working
    # directory, so nothing extra is needed there.
    assert implement.working_directory == str(repo)
    review = next(item for item in reviewer.handoffs if item.stage == "review")
    assert review.working_directory == str(workspace)
    assert review.additional_writable_roots == [str(repo)]


def test_caller_declared_roots_are_carried_through(product, tmp_path):
    host = _Host({"core": product["core"]})
    pending = tmp_path / "not-created-yet"
    handoff = StructuredHandoff(
        work_package_id="wp1",
        stage="implement",
        summary="s",
        working_directory=str(product["workspace"]),
        additional_writable_roots=[str(pending)],
    )

    updated = declare_access_roots(host, handoff, _package(["core"]))

    assert updated.additional_writable_roots == [str(pending), str(product["core"])]
