"""Greenfield source templates and safe project creation.

``execraft new`` is intentionally distinct from ``execraft init``: an empty source
folder is an error for existing-project discovery, while greenfield creation
materializes a versioned source template, initializes a usable Git history, and
then registers the generated source through the normal onboarding domain.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence, Any, Callable, Mapping

from execraft.onboarding.models import CreationPlan
from execraft.onboarding.service import OnboardingOutcome, OnboardingService
from execraft.onboarding.transactions import AtomicTreeTransaction
from execraft.project import validate_project_id


class GreenfieldError(RuntimeError):
    """Raised when a greenfield template cannot be resolved or published."""


@dataclass(frozen=True)
class GreenfieldTemplate:
    id: str
    version: int
    description: str
    renderer: Callable[[str, Path], None]

    @property
    def reference(self) -> str:
        return f"{self.id}@{self.version}"


@dataclass(frozen=True)
class GreenfieldOutcome:
    source_plan: CreationPlan
    project_outcome: OnboardingOutcome
    source_root: Path | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def applied(self) -> bool:
        return self.source_root is not None and self.project_outcome.applied

    def as_mapping(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "source_root": str(self.source_root) if self.source_root else "",
            "source_creation": self.source_plan.as_mapping(),
            "project_creation": self.project_outcome.as_mapping(),
            "details": dict(self.details),
        }


class GreenfieldTemplateCatalog:
    """Versioned source-template catalog with deterministic latest resolution."""

    def __init__(self) -> None:
        self._templates: dict[tuple[str, int], GreenfieldTemplate] = {}

    def register(self, template: GreenfieldTemplate) -> None:
        if template.version < 1:
            raise GreenfieldError("greenfield template version must be positive")
        key = (template.id, template.version)
        if key in self._templates:
            raise GreenfieldError(f"duplicate greenfield template {template.reference}")
        self._templates[key] = template

    def get(self, reference: str) -> GreenfieldTemplate:
        template_id, separator, raw_version = reference.partition("@")
        if separator:
            if not raw_version.isdigit() or int(raw_version) < 1:
                raise GreenfieldError(f"invalid greenfield template reference: {reference!r}")
            template = self._templates.get((template_id, int(raw_version)))
        else:
            candidates = [
                item for (item_id, _version), item in self._templates.items()
                if item_id == template_id
            ]
            template = max(candidates, key=lambda item: item.version) if candidates else None
        if template is None:
            available = ", ".join(item.reference for item in self.templates()) or "none"
            raise GreenfieldError(
                f"unknown greenfield template {reference!r}; available: {available}"
            )
        return template

    def templates(self) -> tuple[GreenfieldTemplate, ...]:
        return tuple(sorted(self._templates.values(), key=lambda item: (item.id, item.version)))


def _python_package_name(project_id: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_]", "_", project_id).strip("_").lower()
    if not value or value[0].isdigit():
        value = f"project_{value}" if value else "project"
    return value


def _write(destination: Path, relative: str, content: str) -> None:
    path = destination / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def render_minimal(project_id: str, destination: Path) -> None:
    _write(destination, "README.md", f"# {project_id}\n\nCreated with `execraft new`.\n")
    _write(destination, ".gitignore", ".venv/\n__pycache__/\n*.py[cod]\n")


def render_python_library(project_id: str, destination: Path) -> None:
    package = _python_package_name(project_id)
    render_minimal(project_id, destination)
    _write(
        destination,
        "pyproject.toml",
        "\n".join(
            [
                "[build-system]",
                'requires = ["setuptools>=68"]',
                'build-backend = "setuptools.build_meta"',
                "",
                "[project]",
                f'name = "{project_id}"',
                'version = "0.1.0"',
                'description = ""',
                'requires-python = ">=3.11"',
                "dependencies = []",
                "",
                "[tool.pytest.ini_options]",
                'testpaths = ["tests"]',
                "",
            ]
        ),
    )
    _write(destination, f"src/{package}/__init__.py", '__version__ = "0.1.0"\n')
    _write(
        destination,
        "tests/test_smoke.py",
        (
            "def test_import() -> None:\n"
            f"    import {package}\n"
            f'    assert {package}.__version__ == "0.1.0"\n'
        ),
    )


def render_python_service(project_id: str, destination: Path) -> None:
    package = _python_package_name(project_id)
    render_python_library(project_id, destination)
    _write(
        destination,
        f"src/{package}/main.py",
        "def main() -> int:\n"
        f"    print(\"{project_id} is ready\")\n"
        "    return 0\n\n"
        "if __name__ == \"__main__\":\n"
        "    raise SystemExit(main())\n",
    )
    pyproject = (destination / "pyproject.toml").read_text(encoding="utf-8")
    pyproject += f'\n[project.scripts]\n{project_id} = "{package}.main:main"\n'
    (destination / "pyproject.toml").write_text(pyproject, encoding="utf-8")


def default_greenfield_catalog() -> GreenfieldTemplateCatalog:
    catalog = GreenfieldTemplateCatalog()
    catalog.register(
        GreenfieldTemplate(
            id="minimal",
            version=1,
            description="Minimal Git repository with README and ignore rules.",
            renderer=render_minimal,
        )
    )
    catalog.register(
        GreenfieldTemplate(
            id="python-library",
            version=1,
            description="Python src-layout library with a smoke test.",
            renderer=render_python_library,
        )
    )
    catalog.register(
        GreenfieldTemplate(
            id="python-service",
            version=1,
            description="Python src-layout command-line service with tests.",
            renderer=render_python_service,
        )
    )
    return catalog


def _initialize_git(source_root: Path) -> str:
    """Create a main branch and initial commit without persisting fake identity."""

    def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["git", *args],
            cwd=source_root,
            text=True,
            capture_output=True,
            check=False,
        )
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise GreenfieldError(f"git {' '.join(args)} failed: {detail}")
        return completed

    run("init", "-q", "-b", "main")
    run("add", "-A")
    name = run("config", "user.name", check=False).stdout.strip() or "Execraft bootstrap"
    email = run("config", "user.email", check=False).stdout.strip() or "execraft@localhost"
    run(
        "-c",
        f"user.name={name}",
        "-c",
        f"user.email={email}",
        "commit",
        "-qm",
        "Initialize project",
    )
    return run("rev-parse", "HEAD").stdout.strip()


class GreenfieldService:
    """Create a source tree and register it through normal project onboarding."""

    def __init__(
        self,
        *,
        onboarding: OnboardingService,
        templates: GreenfieldTemplateCatalog | None = None,
    ) -> None:
        self.onboarding = onboarding
        self.templates = templates or default_greenfield_catalog()

    def create(
        self,
        *,
        name: str,
        parent: Path,
        descriptor_output: Path,
        source_template: str = "python-service",
        project_template: str = "standard",
        project_features: Sequence[str] = (),
        include_devcontainer: bool = False,
        dry_run: bool = False,
    ) -> GreenfieldOutcome:
        project_id = validate_project_id(name)
        descriptor = self.templates.get(source_template)
        target = parent.expanduser().resolve() / project_id

        def materialize(destination: Path) -> None:
            descriptor.renderer(project_id, destination)

        with AtomicTreeTransaction(
            kind="source",
            identifier=project_id,
            target=target,
            materializer=materialize,
            required_files=("README.md",),
            metadata={"template": descriptor.reference},
            finalizers=(() if dry_run else (_initialize_git,)),
        ) as transaction:
            source_plan = transaction.prepare()
            if dry_run:
                with tempfile.TemporaryDirectory(prefix="execraft-new-preview-") as temporary:
                    preview_source = Path(temporary) / project_id
                    descriptor.renderer(project_id, preview_source)
                    _initialize_git(preview_source)
                    report = self.onboarding.inspect_project(preview_source)
                    project_outcome = self.onboarding.create_project(
                        report=report,
                        output_dir=descriptor_output,
                        template_id=project_template,
                        register=False,
                        dry_run=True,
                        feature_ids=tuple(project_features),
                        include_devcontainer=include_devcontainer,
                    )
                return GreenfieldOutcome(
                    source_plan=source_plan,
                    project_outcome=project_outcome,
                    details={"source_template": descriptor.reference},
                )

            result = transaction.apply()

        try:
            report = self.onboarding.inspect_project(result.path)
            project_outcome = self.onboarding.create_project(
                report=report,
                output_dir=descriptor_output,
                template_id=project_template,
                register=True,
                dry_run=False,
                feature_ids=tuple(project_features),
                include_devcontainer=include_devcontainer,
            )
        except Exception:
            shutil.rmtree(result.path, ignore_errors=True)
            raise
        return GreenfieldOutcome(
            source_plan=source_plan,
            project_outcome=project_outcome,
            source_root=result.path,
            details={
                "source_template": descriptor.reference,
                "initial_commit": str(result.finalizer_results[0]),
            },
        )
