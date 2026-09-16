"""Tests for the shared WP2 onboarding domain and transactions."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from execraft import bootstrap
from execraft.onboarding import (
    AtomicTreeTransaction,
    CreationTransactionError,
    DiscoveryEngine,
    Evidence,
    Finding,
    FindingSeverity,
    ProviderInventory,
    ReadinessService,
    TemplateCatalog,
    TemplateCatalogError,
    TemplateDescriptor,
)
from execraft.project import load_project, load_project_registration
from execraft.workspace.task_git import (
    TaskGitError,
    load_manifest,
    local_manifest_registry_path,
)


def _init_git(path: Path, *, branch: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


def _configure_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("EXECRAFT_CONTROL_ROOT", raising=False)
    monkeypatch.delenv("EXECRAFT_WORKFLOW_ROOT", raising=False)
    monkeypatch.delenv("AI_WORKFLOW_ROOT", raising=False)
    return tmp_path / "data" / "execraft" / "control"


def test_discovery_records_evidence_and_non_executing_ecosystems(tmp_path: Path) -> None:
    source = tmp_path / "sample"
    _init_git(source, branch="feature/work")
    (source / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")
    (source / "package.json").write_text('{"scripts":{"test":"node test.js"}}', encoding="utf-8")
    (source / "Cargo.toml").write_text("[package]\nname='sample'\n", encoding="utf-8")
    (source / "dirty.txt").write_text("dirty", encoding="utf-8")

    report = DiscoveryEngine().inspect(source)

    assert report.project_id == "sample"
    assert {"python", "javascript", "rust"} <= report.languages
    assert report.repositories[0].dirty is True
    assert report.repositories[0].base_branch == "feature/work"
    assert any(item.field == "base_branch" for item in report.evidence)
    assert any(item.code == "discovery.base_branch_low_confidence" for item in report.findings)
    assert any(item.code == "discovery.repository_dirty" for item in report.findings)
    assert report.as_mapping()["can_scaffold"] is True


def test_discovery_empty_directory_is_blocking(tmp_path: Path) -> None:
    report = DiscoveryEngine().inspect(tmp_path)
    assert report.can_scaffold is False
    assert report.findings[0].severity is FindingSeverity.ERROR
    assert report.unresolved_choices == ["No Git repositories found at source root"]


def test_discovery_flags_overlapping_nested_repositories(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _init_git(source)
    nested = source / "vendor" / "nested"
    _init_git(nested)

    report = DiscoveryEngine().inspect(source)

    overlap = [
        item for item in report.findings
        if item.code == "discovery.repository_overlap"
    ]
    assert len(overlap) == 1
    assert overlap[0].severity is FindingSeverity.DECISION_REQUIRED
    assert "nested inside" in overlap[0].message
    assert overlap[0].message in report.unresolved_choices


def test_discovery_flags_duplicate_repository_ids(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _init_git(source / "team-a" / "shared")
    _init_git(source / "team-b" / "shared")

    report = DiscoveryEngine().inspect(source)

    duplicate = next(
        item
        for item in report.findings
        if item.code == "discovery.duplicate_repository_id"
    )
    assert duplicate.severity is FindingSeverity.DECISION_REQUIRED
    assert "team-a" in duplicate.message
    assert "team-b" in duplicate.message


def test_template_catalog_is_versioned_and_rejects_duplicates(tmp_path: Path) -> None:
    catalog = TemplateCatalog()
    descriptor = TemplateDescriptor(
        id="minimal",
        version=2,
        kind="project",
        description="minimal",
        renderer=lambda _context, destination: (destination / "project.yaml").write_text("x"),
    )
    catalog.register(descriptor)
    assert catalog.get(kind="project", template_id="minimal").reference == "minimal@2"
    with pytest.raises(TemplateCatalogError, match="duplicate"):
        catalog.register(descriptor)
    with pytest.raises(TemplateCatalogError, match="unknown"):
        catalog.get(kind="task", template_id="minimal")


def test_atomic_tree_transaction_previews_and_publishes(tmp_path: Path) -> None:
    target = tmp_path / "published"
    with AtomicTreeTransaction(
        kind="project",
        identifier="example/project",
        target=target,
        materializer=lambda destination: (destination / "a.txt").write_text("hello"),
        required_files=("a.txt",),
        metadata={"template": "test@1"},
    ) as transaction:
        plan = transaction.prepare()
        assert not target.exists()
        assert plan.identifier == "example/project"
        assert plan.files[0].path == "a.txt"
        result = transaction.apply()

    assert result.path == target
    assert (target / "a.txt").read_text() == "hello"
    assert plan.as_mapping()["metadata"] == {"template": "test@1"}
    assert not (tmp_path / ".execraft-create.lock").exists()


def test_atomic_tree_transaction_rolls_back_published_tree_on_finalizer_failure(
    tmp_path: Path,
) -> None:
    target = tmp_path / "published"

    def fail(_published: Path) -> object:
        raise RuntimeError("finalizer failed")

    with AtomicTreeTransaction(
        kind="task",
        identifier="project/task",
        target=target,
        materializer=lambda destination: (destination / "TASK.yaml").write_text("task"),
        required_files=("TASK.yaml",),
        finalizers=(fail,),
    ) as transaction:
        transaction.prepare()
        with pytest.raises(RuntimeError, match="finalizer failed"):
            transaction.apply()

    assert not target.exists()


def test_atomic_tree_transaction_refuses_blocking_findings(tmp_path: Path) -> None:
    target = tmp_path / "blocked"
    finding = Finding(
        code="blocked",
        severity=FindingSeverity.ERROR,
        message="cannot apply",
    )
    with AtomicTreeTransaction(
        kind="project",
        identifier="blocked",
        target=target,
        materializer=lambda destination: (destination / "project.yaml").write_text("x"),
        findings=(finding,),
    ) as transaction:
        assert transaction.prepare().can_apply is False
        with pytest.raises(CreationTransactionError, match="blocking"):
            transaction.apply()
    assert not target.exists()


def test_atomic_tree_transaction_requires_explicit_decision_acceptance(
    tmp_path: Path,
) -> None:
    finding = Finding(
        code="decision",
        severity=FindingSeverity.DECISION_REQUIRED,
        message="operator must choose",
    )
    blocked_target = tmp_path / "blocked-decision"
    with AtomicTreeTransaction(
        kind="project",
        identifier="blocked-decision",
        target=blocked_target,
        materializer=lambda destination: (destination / "project.yaml").write_text("x"),
        findings=(finding,),
    ) as transaction:
        plan = transaction.prepare()
        assert plan.can_apply is False
        assert plan.pending_decisions == (finding,)
        with pytest.raises(CreationTransactionError, match="pending operator decisions"):
            transaction.apply()

    accepted_target = tmp_path / "accepted-decision"
    with AtomicTreeTransaction(
        kind="project",
        identifier="accepted-decision",
        target=accepted_target,
        materializer=lambda destination: (destination / "project.yaml").write_text("x"),
        findings=(finding,),
        accept_decisions=True,
    ) as transaction:
        plan = transaction.prepare()
        assert plan.can_apply is True
        assert plan.decisions_accepted is True
        assert plan.pending_decisions == ()
        transaction.apply()
    assert accepted_target.is_dir()


def test_project_onboarding_dry_run_and_atomic_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control_root = _configure_xdg(monkeypatch, tmp_path)
    source = tmp_path / "source" / "app"
    _init_git(source)
    (source / "app.py").write_text("print('ok')\n", encoding="utf-8")
    service = bootstrap.create_onboarding_service()
    report = service.inspect_project(source)

    preview = service.create_project(
        report=report,
        output_dir=control_root / "projects",
        register=False,
        dry_run=True,
    )
    target = control_root / "projects" / "app"
    assert preview.applied is False
    assert not target.exists()
    assert {item.path for item in preview.plan.files} >= {
        "project.yaml",
        "agents.yaml",
        "verification.yaml",
    }

    outcome = service.create_project(
        report=report,
        output_dir=control_root / "projects",
        register=True,
        dry_run=False,
    )
    assert outcome.path == target / "project.yaml"
    registration = load_project_registration("app")
    assert registration is not None
    assert registration.descriptor == outcome.path
    assert registration.source_root == source.resolve()


def test_project_onboarding_does_not_publish_blocked_discovery(tmp_path: Path) -> None:
    service = bootstrap.create_onboarding_service()
    report = service.inspect_project(tmp_path)
    with pytest.raises(CreationTransactionError, match="blocking"):
        service.create_project(
            report=report,
            output_dir=tmp_path / "projects",
            dry_run=False,
        )
    assert not (tmp_path / "projects" / report.project_id).exists()


def test_task_onboarding_previews_then_commits_dossier_and_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control_root = _configure_xdg(monkeypatch, tmp_path)
    source = tmp_path / "source" / "app"
    _init_git(source)
    service = bootstrap.create_onboarding_service()
    report = service.inspect_project(source)
    project_outcome = service.create_project(
        report=report,
        output_dir=control_root / "projects",
        register=True,
    )
    project = load_project(project_outcome.path.parent)

    preview = service.create_task(
        control_root=control_root,
        project=project,
        task_id="first-task",
        title="First task",
        brief="Implement the first capability.",
        state_root=tmp_path / "state" / "execraft",
        dry_run=True,
    )
    dossier = project.directory / "tasks" / "first-task"
    assert preview.applied is False
    assert not dossier.exists()
    assert "TASK.yaml" in {item.path for item in preview.plan.files}

    outcome = service.create_task(
        control_root=control_root,
        project=project,
        task_id="first-task",
        title="First task",
        brief="Implement the first capability.",
        state_root=tmp_path / "state" / "execraft",
    )
    assert outcome.path == dossier
    assert "Implement the first capability." in (dossier / "BRIEF.md").read_text()
    manifest = load_manifest(control_root, "first-task")
    assert manifest.project == "app"
    assert local_manifest_registry_path(control_root, "first-task").is_file()
    assert Path(outcome.details["runtime_status"]).is_file()


def test_task_onboarding_rejects_empty_title_before_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control_root = _configure_xdg(monkeypatch, tmp_path)
    source = tmp_path / "source" / "app"
    _init_git(source)
    service = bootstrap.create_onboarding_service()
    project_outcome = service.create_project(
        report=service.inspect_project(source),
        output_dir=control_root / "projects",
        register=True,
    )
    project = load_project(project_outcome.path.parent)

    with pytest.raises(TaskGitError, match="title cannot be empty"):
        service.create_task(
            control_root=control_root,
            project=project,
            task_id="untitled",
            title="   ",
            state_root=tmp_path / "state",
        )

    assert not (project.directory / "tasks" / "untitled").exists()
    assert not local_manifest_registry_path(control_root, "untitled").exists()


def test_task_onboarding_honors_optional_repository_selection(tmp_path: Path) -> None:
    source = tmp_path / "source"
    repo_a = source / "a"
    repo_b = source / "b"
    _init_git(repo_a)
    _init_git(repo_b)
    report = DiscoveryEngine().inspect(source)
    project_file = bootstrap.scaffold_project(report, tmp_path / "projects")
    raw = yaml.safe_load(project_file.read_text())
    raw["repositories"][1]["required"] = False
    project_file.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    project = load_project(project_file.parent)

    outcome = bootstrap.create_onboarding_service().create_task(
        control_root=tmp_path,
        project=project,
        task_id="scoped",
        title="Scoped",
        repository_ids=(project.repositories[0].id,),
        state_root=tmp_path / "state",
    )
    manifest = yaml.safe_load((outcome.path / "TASK.yaml").read_text())
    assert [item["id"] for item in manifest["repositories"]] == [project.repositories[0].id]


def test_provider_inventory_and_readiness_are_independent_dimensions(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _init_git(source)
    report = DiscoveryEngine().inspect(source)
    project_file = bootstrap.scaffold_project(report, tmp_path / "projects")
    project = load_project(project_file.parent)

    agents_path = project.configured_path("agents_file")
    assert agents_path is not None
    agents_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "providers": {
                    "local": {
                        "adapter": "codex",
                        "enabled": True,
                        "binary": sys.executable,
                        "capabilities": ["implement"],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    verification_path = project.configured_path("verification_file")
    assert verification_path is not None
    verification_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "require_commands": True,
                "commands": [
                    {
                        "id": "test",
                        "command": "python -m pytest -q",
                        "profile": "focused",
                        "enabled": True,
                    }
                ],
                "known_failures": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    inventory = ProviderInventory().inspect(project)
    assert len(inventory.ready) == 1
    readiness = ReadinessService().evaluate(project, source_root=source)
    by_id = {item.id: item for item in readiness.checks}
    assert by_id["repositories"].status.value == "ready"
    assert by_id["verification"].status.value == "ready"
    assert by_id["execution"].status.value == "ready"
    assert readiness.ready is True


def test_creation_plan_json_is_stable_and_complete(tmp_path: Path) -> None:
    evidence = Evidence(
        id="e1",
        subject="project",
        field="id",
        value="sample",
        source="test",
        confidence=1.0,
    )
    with AtomicTreeTransaction(
        kind="project",
        identifier="sample",
        target=tmp_path / "sample",
        materializer=lambda destination: (destination / "project.yaml").write_text("a: 1\n"),
        evidence=(evidence,),
    ) as transaction:
        payload = transaction.prepare().as_mapping()
    encoded = json.dumps(payload, sort_keys=True)
    assert '"can_apply": true' in encoded
    assert '"sha256"' in encoded
    assert payload["evidence"][0]["source"] == "test"


def test_template_catalog_resolves_latest_or_explicit_version() -> None:
    catalog = TemplateCatalog()
    for version in (1, 3, 2):
        catalog.register(
            TemplateDescriptor(
                id="standard",
                version=version,
                kind="project",
                description=f"v{version}",
                renderer=lambda _context, _destination: None,
            )
        )
    assert catalog.get(kind="project", template_id="standard").version == 3
    assert catalog.get(kind="project", template_id="standard@1").version == 1
    assert catalog.ids(kind="project") == (
        "standard@1",
        "standard@2",
        "standard@3",
    )


def test_task_transaction_rolls_back_registry_when_runtime_sync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_root = _configure_xdg(monkeypatch, tmp_path)
    source = tmp_path / "source" / "app"
    _init_git(source)
    service = bootstrap.create_onboarding_service()
    project_outcome = service.create_project(
        report=service.inspect_project(source),
        output_dir=control_root / "projects",
        register=True,
    )
    project = load_project(project_outcome.path.parent)

    def fail_sync(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("status sync failed")

    monkeypatch.setattr("execraft.onboarding.service.sync_runtime_status", fail_sync)
    dossier = project.directory / "tasks" / "rollback"
    registry = local_manifest_registry_path(control_root, "rollback")
    with pytest.raises(RuntimeError, match="status sync failed"):
        service.create_task(
            control_root=control_root,
            project=project,
            task_id="rollback",
            title="Rollback",
            state_root=tmp_path / "state",
        )
    assert not dossier.exists()
    assert not registry.exists()


def test_provider_inventory_reports_enabled_missing_binary(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _init_git(source)
    project_file = bootstrap.scaffold_project(
        DiscoveryEngine().inspect(source), tmp_path / "projects"
    )
    project = load_project(project_file.parent)
    agents = project.configured_path("agents_file")
    assert agents is not None
    agents.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "providers": {
                    "missing": {
                        "adapter": "codex",
                        "enabled": True,
                        "binary": "definitely-not-an-execraft-provider-binary",
                        "capabilities": ["implement"],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    inventory = ProviderInventory().inspect(project)
    assert inventory.enabled
    assert not inventory.ready
    assert any(item.code == "providers.binary_missing" for item in inventory.findings)


def test_cli_onboarding_dry_runs_are_machine_readable_and_non_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execraft.cli import main

    control_root = _configure_xdg(monkeypatch, tmp_path)
    source = tmp_path / "source" / "cli-app"
    _init_git(source)
    monkeypatch.chdir(source)

    assert not control_root.exists()
    assert main(["project", "inspect", "--json"]) == 0
    inspection = json.loads(capsys.readouterr().out)
    assert inspection["project_id"] == "cli-app"
    assert inspection["evidence"]
    assert not control_root.exists()

    assert main(["init", "--dry-run", "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["creation"]["applied"] is False
    assert preview["creation"]["plan"]["files"]
    project_dir = control_root / "projects" / "cli-app"
    assert not project_dir.exists()
    assert not control_root.exists()

    assert main(["init", "--json"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["creation"]["applied"] is True
    assert project_dir.is_dir()

    assert main(
        [
            "task",
            "new",
            "preview-task",
            "--title",
            "Preview task",
            "--brief",
            "Do not publish this preview.",
            "--dry-run",
            "--json",
        ]
    ) == 0
    task_preview = json.loads(capsys.readouterr().out)
    assert task_preview["applied"] is False
    assert not (project_dir / "tasks" / "preview-task").exists()

    assert main(["project", "doctor", "--json"]) == 1
    readiness = json.loads(capsys.readouterr().out)
    assert readiness["ready"] is False
    statuses = {item["id"]: item["status"] for item in readiness["checks"]}
    assert statuses["descriptor"] == "ready"
    assert statuses["execution"] == "blocked"
    assert statuses["verification"] == "blocked"

    assert main(["project", "templates", "--json"]) == 0
    templates = json.loads(capsys.readouterr().out)
    assert {item["kind"] for item in templates} == {"project", "task"}

def test_cli_requires_explicit_acceptance_for_discovery_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execraft.cli import main

    control_root = _configure_xdg(monkeypatch, tmp_path)
    source = tmp_path / "source" / "feature-app"
    _init_git(source, branch="feature/onboarding")
    monkeypatch.chdir(source)

    assert main(["init", "--dry-run", "--json"]) == 1
    blocked = json.loads(capsys.readouterr().out)
    blocked_plan = blocked["creation"]["plan"]
    assert blocked_plan["can_apply"] is False
    assert blocked_plan["decisions_accepted"] is False
    assert {item["code"] for item in blocked_plan["pending_decisions"]} == {
        "discovery.base_branch_low_confidence"
    }
    assert not control_root.exists()

    assert main(["init", "--dry-run", "--accept-decisions", "--json"]) == 0
    accepted = json.loads(capsys.readouterr().out)
    accepted_plan = accepted["creation"]["plan"]
    assert accepted_plan["can_apply"] is True
    assert accepted_plan["decisions_accepted"] is True
    assert accepted_plan["pending_decisions"] == []
    assert not control_root.exists()

    assert main(["init", "--accept-decisions", "--json"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["creation"]["applied"] is True
    assert (control_root / "projects" / "feature-app" / "project.yaml").is_file()
