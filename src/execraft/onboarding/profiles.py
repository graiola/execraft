"""Versioned project profiles and composable ecosystem features.

Profiles define conservative orchestration defaults. Features contribute only
portable project-shell configuration: capabilities, verification suggestions,
editor recommendations, provider-neutral instructions, and optional generated
workspace assets. Product repositories are never modified by profile rendering.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from execraft.persistence import sha256_file
from execraft.onboarding.discovery import DiscoveryReport, suggest_verification


class ProfileCatalogError(ValueError):
    """Raised when a profile or feature reference is invalid."""


def _package_version() -> str:
    try:
        return metadata.version("execraft")
    except metadata.PackageNotFoundError:
        return "0.1.0"



def _reference_parts(reference: str) -> tuple[str, int | None]:
    name, separator, raw_version = str(reference).strip().partition("@")
    if not name or not name.replace("-", "_").isidentifier():
        raise ProfileCatalogError(f"invalid catalog reference: {reference!r}")
    if not separator:
        return name, None
    if not raw_version.isdigit() or int(raw_version) < 1:
        raise ProfileCatalogError(f"invalid catalog reference: {reference!r}")
    return name, int(raw_version)


@dataclass(frozen=True)
class FeatureDescriptor:
    id: str
    version: int
    description: str
    technologies: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    editor_extensions: tuple[str, ...] = ()
    editor_settings: Mapping[str, Any] = field(default_factory=dict)
    instruction_lines: tuple[str, ...] = ()
    verification_commands: tuple[Mapping[str, Any], ...] = ()
    devcontainer_features: Mapping[str, Any] = field(default_factory=dict)

    @property
    def reference(self) -> str:
        return f"{self.id}@{self.version}"


@dataclass(frozen=True)
class ProjectProfile:
    id: str
    version: int
    description: str
    default_features: tuple[str, ...]
    default_policy: str
    autonomous: bool
    parallelism: bool
    automatic_commits: bool
    new_project_selectable: bool = True

    @property
    def reference(self) -> str:
        return f"{self.id}@{self.version}"


class ProjectProfileCatalog:
    """Resolve built-in profile and feature versions deterministically."""

    def __init__(self) -> None:
        self._profiles: dict[tuple[str, int], ProjectProfile] = {}
        self._features: dict[tuple[str, int], FeatureDescriptor] = {}

    def register_profile(self, profile: ProjectProfile) -> None:
        key = (profile.id, profile.version)
        if profile.version < 1 or key in self._profiles:
            raise ProfileCatalogError(f"invalid or duplicate profile {profile.reference}")
        self._profiles[key] = profile

    def register_feature(self, feature: FeatureDescriptor) -> None:
        key = (feature.id, feature.version)
        if feature.version < 1 or key in self._features:
            raise ProfileCatalogError(f"invalid or duplicate feature {feature.reference}")
        self._features[key] = feature

    def profile(self, reference: str) -> ProjectProfile:
        name, version = _reference_parts(reference)
        candidates = [
            item for (item_id, _), item in self._profiles.items() if item_id == name
        ]
        selected = self._profiles.get((name, version)) if version is not None else (
            max(candidates, key=lambda item: item.version) if candidates else None
        )
        if selected is None:
            available = ", ".join(item.reference for item in self.profiles()) or "none"
            raise ProfileCatalogError(
                f"unknown project profile {reference!r}; available: {available}"
            )
        return selected

    def feature(self, reference: str) -> FeatureDescriptor:
        name, version = _reference_parts(reference)
        candidates = [
            item for (item_id, _), item in self._features.items() if item_id == name
        ]
        selected = self._features.get((name, version)) if version is not None else (
            max(candidates, key=lambda item: item.version) if candidates else None
        )
        if selected is None:
            available = ", ".join(item.reference for item in self.features()) or "none"
            raise ProfileCatalogError(
                f"unknown project feature {reference!r}; available: {available}"
            )
        return selected

    def profiles(self) -> tuple[ProjectProfile, ...]:
        return tuple(sorted(self._profiles.values(), key=lambda item: (item.id, item.version)))

    def new_project_profiles(self) -> tuple[ProjectProfile, ...]:
        """Return profiles that are valid choices for a newly adopted/created project.

        Compatibility profiles remain resolvable through :meth:`profile` so old
        descriptors and migrations stay reproducible, but they must not leak into
        greenfield project selectors.
        """

        return tuple(item for item in self.profiles() if item.new_project_selectable)

    def features(self) -> tuple[FeatureDescriptor, ...]:
        return tuple(sorted(self._features.values(), key=lambda item: (item.id, item.version)))

    def latest_profile_reference(self, reference: str) -> str:
        name, _version = _reference_parts(reference)
        return self.profile(name).reference

    def latest_feature_reference(self, reference: str) -> str:
        name, _version = _reference_parts(reference)
        return self.feature(name).reference

    def detect_features(
        self,
        report: DiscoveryReport,
        *,
        profile: ProjectProfile,
        requested: Sequence[str] = (),
        include_devcontainer: bool = False,
        excluded: Sequence[str] = (),
    ) -> tuple[FeatureDescriptor, ...]:
        references: list[str] = list(profile.default_features)
        technologies = set(report.languages)
        for repository in report.repositories:
            technologies.update(
                name
                for name, enabled in {
                    "python": repository.has_python,
                    "cpp": repository.has_cpp,
                    "cmake": repository.has_cmake,
                    "ros": repository.has_ros or repository.has_colcon,
                    "docker": repository.has_docker,
                    "javascript": repository.has_javascript,
                    "rust": repository.has_rust,
                    "go": repository.has_go,
                    "java": repository.has_java,
                }.items()
                if enabled
            )
        if len(report.repositories) > 1:
            technologies.add("multi-repo")
        if report.has_docker_compose:
            technologies.add("docker")
        for feature in self.features():
            if feature.technologies and technologies.intersection(feature.technologies):
                references.append(feature.reference)
        references.extend(requested)
        if include_devcontainer:
            references.append("devcontainer")
        excluded_ids = {self.feature(reference).id for reference in excluded}
        required_ids = {self.feature(reference).id for reference in profile.default_features}
        forbidden = sorted(required_ids.intersection(excluded_ids))
        if forbidden:
            raise ProfileCatalogError(
                "profile-required features cannot be removed: " + ", ".join(forbidden)
            )
        resolved: dict[str, FeatureDescriptor] = {}
        for reference in references:
            feature = self.feature(reference)
            if feature.id not in excluded_ids:
                resolved[feature.id] = feature
        return tuple(resolved[key] for key in sorted(resolved))


@dataclass(frozen=True)
class ProjectTemplateContext:
    report: DiscoveryReport
    profile_reference: str
    requested_features: tuple[str, ...] = ()
    include_devcontainer: bool = False
    excluded_features: tuple[str, ...] = ()

    def __getattr__(self, name: str) -> Any:
        """Preserve the renderer contract by proxying report fields."""

        return getattr(self.report, name)


@dataclass(frozen=True)
class RenderedProfile:
    profile: ProjectProfile
    features: tuple[FeatureDescriptor, ...]
    provenance_file: Path


class ProjectProfileRenderer:
    """Render one self-contained project descriptor from profile contributions."""

    def __init__(self, catalog: ProjectProfileCatalog) -> None:
        self.catalog = catalog

    def render(self, context: ProjectTemplateContext, destination: Path) -> RenderedProfile:
        profile = self.catalog.profile(context.profile_reference)
        features = self.catalog.detect_features(
            context.report,
            profile=profile,
            requested=context.requested_features,
            include_devcontainer=context.include_devcontainer,
            excluded=context.excluded_features,
        )
        self._create_directories(destination, include_devcontainer=any(f.id == "devcontainer" for f in features))
        descriptor = self._descriptor(context.report, profile, features)
        self._write_yaml(destination / "project.yaml", descriptor)
        self._write_yaml(destination / "agents.yaml", self._agents(profile))
        self._write_yaml(destination / "resources.yaml", self._resources(profile))
        self._write_yaml(
            destination / "verification.yaml",
            self._verification(context.report, features),
        )
        self._write_policies(destination / "policies")
        self._write_task_templates(destination / "task_templates")
        (destination / "instructions" / "AGENTS.md").write_text(
            self._agents_markdown(context.report, profile, features), encoding="utf-8"
        )
        self._write_provider_overlays(destination)
        self._write_vscode(destination / "vscode", features)
        if any(feature.id == "devcontainer" for feature in features):
            self._write_devcontainer(destination / "devcontainer", features)
        provenance = self._write_provenance(destination, profile, features)
        return RenderedProfile(profile, features, provenance)

    @staticmethod
    def _create_directories(destination: Path, *, include_devcontainer: bool) -> None:
        directories = (
            "task_templates",
            "instructions",
            "skills",
            "provider_overrides/opencode/agents",
            "provider_overrides/opencode/commands",
            "provider_overrides/claude/agents",
            "provider_overrides/claude/commands",
            "provider_overrides/codex",
            "vscode",
            "policies",
            "tasks",
        )
        for relative in directories:
            (destination / relative).mkdir(parents=True, exist_ok=True)
        if include_devcontainer:
            (destination / "devcontainer").mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _repository_rows(report: DiscoveryReport) -> list[dict[str, Any]]:
        source_root = Path(report.source_root).resolve()
        used: set[str] = set()
        rows: list[dict[str, Any]] = []
        for index, repo in enumerate(report.repositories, start=1):
            path = Path(repo.path).resolve()
            try:
                relative = path.relative_to(source_root).as_posix() or "."
            except ValueError:
                relative = path.name
            repository_id = repo.id if repo.id not in used else f"{repo.id}_{index}"
            used.add(repository_id)
            rows.append(
                {
                    "id": repository_id,
                    "path": relative,
                    "role": repo.role,
                    "required": True,
                    "base_branch": repo.base_branch,
                    "workspace_name": repository_id,
                    "mutability": "task_owned",
                }
            )
        return rows

    def _descriptor(
        self,
        report: DiscoveryReport,
        profile: ProjectProfile,
        features: Sequence[FeatureDescriptor],
    ) -> dict[str, Any]:
        capabilities = {"git.worktrees"}
        extensions: set[str] = set()
        settings: dict[str, Any] = {}
        for feature in features:
            capabilities.update(feature.capabilities)
            extensions.update(feature.editor_extensions)
            settings.update(feature.editor_settings)
        descriptor: dict[str, Any] = {
            "schema_version": 3,
            "project": report.project_id,
            "description": f"Project bootstrapped from {Path(report.source_root).name}",
            "path_base": "project_directory",
            "profile": profile.reference,
            "features": [item.reference for item in features],
            "generated_with": f"execraft {_package_version()}",
            "provenance_file": ".execraft-template.yaml",
            "repositories": self._repository_rows(report),
            "task_templates": "task_templates",
            "instructions_dir": "instructions",
            "skills_dir": "skills",
            "opencode_dir": "provider_overrides/opencode",
            "claude_dir": "provider_overrides/claude",
            "codex_dir": "provider_overrides/codex",
            "vscode_dir": "vscode",
            "policies_dir": "policies",
            "verification_file": "verification.yaml",
            "agents_file": "agents.yaml",
            "resources_file": "resources.yaml",
            "default_policy": profile.default_policy,
            "policy_profiles": {
                "read-only": {"description": "Planning and review only."},
                "workspace-write": {"description": "Edits limited to task worktrees."},
                "host-integration": {"description": "Explicit host integration opt-in."},
            },
            "capabilities": sorted(capabilities),
            "editor": {
                "extensions": sorted(extensions),
                "settings": settings,
                "exclude": [],
            },
        }
        if any(feature.id == "devcontainer" for feature in features):
            descriptor["devcontainer_dir"] = "devcontainer"
        return descriptor

    @staticmethod
    def _agents(profile: ProjectProfile) -> dict[str, Any]:
        """Render new projects directly in the runtime-neutral schema-v4 shape.

        The generated project remains Native-only by default: OpenClaw is an
        optional runtime configured later through setup.  Keeping the default
        topology explicit avoids forcing OpenClaw onto users while ensuring new
        projects never need the provider-shaped v3 migration just to adopt it.
        """

        capabilities = ["decompose", "implement", "review", "fix_review"]
        if profile.autonomous:
            capabilities.append("supervise")

        policy = {
            "timeout_seconds": 3600,
            "inactivity_timeout_seconds": 900,
            "max_internal_retry_delay_seconds": 120,
            "auto_approve": False,
            "sandbox": "workspace-write",
            "dangerously_skip_permissions": False,
            "sandbox_enabled": True,
        }
        agents = {
            "codex": {
                "runtime": "native-codex",
                "enabled": False,
                "capabilities": list(capabilities),
                "priority": 200,
                "capability_weight": 70,
                "policy": dict(policy),
            },
            "claude": {
                "runtime": "native-claude-code",
                "model_route": "claude-sonnet",
                "enabled": False,
                "capabilities": list(capabilities),
                "priority": 150,
                "capability_weight": 70,
                "policy": dict(policy),
            },
            "opencode": {
                "runtime": "native-opencode",
                "model_route": "opencode-free",
                "enabled": False,
                "capabilities": list(capabilities),
                "priority": 100,
                "capability_weight": 70,
                "policy": dict(policy),
            },
        }
        return {
            "schema_version": 4,
            "runtimes": {
                "native-codex": {
                    "kind": "native",
                    "adapter": "codex",
                    "binary": "codex",
                },
                "native-claude-code": {
                    "kind": "native",
                    "adapter": "claude-code",
                    "binary": "claude",
                },
                "native-opencode": {
                    "kind": "native",
                    "adapter": "opencode",
                    "binary": "opencode",
                },
            },
            "execution_targets": {},
            "model_routes": {
                "claude-sonnet": {
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-5",
                },
                "opencode-free": {
                    "provider": "opencode",
                    "provider_alias": "opencode",
                    "model": "deepseek-v4-flash-free",
                },
            },
            "agents": agents,
            "scheduling": {
                "allow_same_provider_review": True,
                "decomposition": {
                    "enabled": profile.parallelism,
                    "minimum_complexity": 70,
                    "maximum_shard_complexity": 55,
                    "max_shards": 4,
                },
                "parallel_shards": {
                    "enabled": profile.parallelism,
                    "stages": ["implement", "review", "fix_review"],
                    "max_workers": 2,
                },
            },
            "supervisor": {
                "enabled": profile.autonomous,
                "agents": ["codex", "claude"],
                "skill": "ai-supervise",
                "ask_human_when_uncertain": True,
                "require_human_for_destructive_actions": True,
                "auto_decision": {
                    "enabled": profile.autonomous,
                    "minimum_weight": 80,
                    "minimum_margin": 25,
                    "max_per_incident": 1,
                },
            },
            "commit": {
                # Automatic commits are a framework invariant.  The runtime
                # deliberately rejects manual/approval modes so a generated
                # project must never weaken or contradict that contract.
                "mode": "automatic",
                "require_verification": True,
            },
        }

    @staticmethod
    def _resources(profile: ProjectProfile) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "warning_percent": 75,
            "pause_percent": 90,
            "resume_percent": 80,
            "minimum_free_gb": 15,
            "providers": [],
            "profile": profile.reference,
        }

    @staticmethod
    def _verification(
        report: DiscoveryReport, features: Sequence[FeatureDescriptor]
    ) -> dict[str, Any]:
        commands: dict[str, Mapping[str, Any]] = {
            str(item["id"]): item for item in suggest_verification(report)
        }
        repository_ids = [repo.id for repo in report.repositories]
        default_repo = repository_ids[0] if len(repository_ids) == 1 else ""
        for feature in features:
            for raw in feature.verification_commands:
                command = dict(raw)
                command_id = str(command["id"])
                if default_repo and not command.get("repository_id"):
                    command["repository_id"] = default_repo
                command.setdefault("enabled", False)
                command.setdefault("source", {"kind": "feature", "feature": feature.reference})
                commands.setdefault(command_id, command)
        return {
            "schema_version": 1,
            "require_commands": True,
            "commands": list(commands.values()),
            "known_failures": [],
        }

    @staticmethod
    def _write_policies(destination: Path) -> None:
        bodies = {
            "read-only": {"filesystem": "read-only", "network": "deny", "docker": "deny", "hardware": "deny"},
            "workspace-write": {"filesystem": "task-worktrees", "network": "ask", "docker": "deny", "hardware": "deny"},
            "host-integration": {"filesystem": "task-worktrees", "network": "allow", "docker": "ask", "hardware": "ask"},
        }
        for policy_id, body in bodies.items():
            ProjectProfileRenderer._write_yaml(
                destination / f"{policy_id}.yaml",
                {"schema_version": 1, "id": policy_id, **body, "destructive_git": "deny"},
            )

    @staticmethod
    def _write_task_templates(destination: Path) -> None:
        files = {
            "README.md": "# Task dossier: {{ title }}\n\nSee the brief, executable plan, definition provenance, review ledger, and engineering history.\n",
            ".gitignore": "/RUNTIME_STATUS.md\n",
            "BRIEF.md": "# Brief: {{ title }}\n\n## Intent\n{{ brief }}\n",
            "PLAN.md": "# Plan: {{ title }}\n\nSee `PLAN.graph.yaml` for the executable graph.\n",
            "HANDOFF.md": "# Engineering history: {{ title }}\n\n> Append-only engineering record.\n",
            "REVIEW.md": "# Review: {{ title }}\n\n## Findings\n",
        }
        for name, content in files.items():
            (destination / name).write_text(content, encoding="utf-8")

    @staticmethod
    def _agents_markdown(
        report: DiscoveryReport,
        profile: ProjectProfile,
        features: Sequence[FeatureDescriptor],
    ) -> str:
        lines = [
            "# Project agent instructions",
            "",
            "This workspace is generated by `execraft`. Edit product code only in registered task worktrees.",
            "Treat `BRIEF.md`, `PLAN.graph.yaml`, and `TASK.yaml` as the task contract.",
            "Never weaken verification, ownership, or destructive-Git safeguards to make a run pass.",
            "",
            "## Project profile",
            "",
            f"- Profile: `{profile.reference}`",
            f"- Repositories: {', '.join(repo.id for repo in report.repositories)}",
            f"- Features: {', '.join(feature.reference for feature in features) or 'none'}",
            "",
            "## Ecosystem guidance",
            "",
        ]
        guidance = [line for feature in features for line in feature.instruction_lines]
        lines.extend(f"- {line}" for line in guidance or ["Follow repository-owned build and test documentation."])
        lines.extend(
            [
                "",
                "## Provider compatibility",
                "",
                "This file is provider-neutral and is the canonical shared instruction source.",
                "Provider-specific shells may reference it, but must not duplicate or contradict it.",
                "",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _write_provider_overlays(destination: Path) -> None:
        ProjectProfileRenderer._write_yaml(
            destination / "provider_overrides" / "opencode" / "providers.yaml",
            {"schema_version": 1, "endpoints": {}},
        )
        for relative in (
            "provider_overrides/opencode/agents/.gitkeep",
            "provider_overrides/opencode/commands/.gitkeep",
            "provider_overrides/claude/agents/.gitkeep",
            "provider_overrides/claude/commands/.gitkeep",
            "provider_overrides/codex/.gitkeep",
            "skills/.gitkeep",
        ):
            (destination / relative).touch()

    @staticmethod
    def _write_vscode(destination: Path, features: Sequence[FeatureDescriptor]) -> None:
        extensions = sorted({item for feature in features for item in feature.editor_extensions})
        (destination / "extensions.json").write_text(
            json.dumps({"recommendations": extensions}, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_devcontainer(destination: Path, features: Sequence[FeatureDescriptor]) -> None:
        contributions: dict[str, Any] = {}
        for feature in features:
            contributions.update(feature.devcontainer_features)
        payload = {
            "name": "execraft task workspace",
            "image": "mcr.microsoft.com/devcontainers/base:ubuntu",
            "features": contributions,
            "remoteUser": "vscode",
            "customizations": {
                "vscode": {
                    "extensions": sorted(
                        {item for feature in features for item in feature.editor_extensions}
                    )
                }
            },
        }
        (destination / "devcontainer.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        (destination / "README.md").write_text(
            "# Optional development container\n\n"
            "This definition is copied into disposable task workspace shells. "
            "It never modifies the product repository automatically.\n",
            encoding="utf-8",
        )

    def _write_provenance(
        self,
        destination: Path,
        profile: ProjectProfile,
        features: Sequence[FeatureDescriptor],
    ) -> Path:
        provenance_path = destination / ".execraft-template.yaml"
        managed = {
            path.relative_to(destination).as_posix(): sha256_file(path)
            for path in sorted(destination.rglob("*"))
            if path.is_file() and path != provenance_path and "tasks" not in path.relative_to(destination).parts
        }
        self._write_yaml(
            provenance_path,
            {
                "schema_version": 1,
                "profile": profile.reference,
                "features": [item.reference for item in features],
                "generated_with": f"execraft {_package_version()}",
                "managed_files": managed,
            },
        )
        return provenance_path

    @staticmethod
    def _write_yaml(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(dict(payload), sort_keys=False, width=1000), encoding="utf-8"
        )


def default_profile_catalog() -> ProjectProfileCatalog:
    catalog = ProjectProfileCatalog()
    catalog.register_profile(ProjectProfile("starter", 1, "Minimal, approval-first project policy.", ("core",), "workspace-write", False, False, True))
    catalog.register_profile(
        ProjectProfile(
            "standard",
            1,
            "Compatibility-only standard profile baseline for existing projects and migrations.",
            ("core",),
            "workspace-write",
            True,
            True,
            True,
            new_project_selectable=False,
        )
    )
    catalog.register_profile(ProjectProfile("standard", 2, "Balanced approval-first multi-agent policy.", ("core",), "workspace-write", False, False, True))
    catalog.register_profile(ProjectProfile("standard", 3, "Balanced multi-agent policy with bounded parallelism.", ("core",), "workspace-write", False, True, True))
    catalog.register_profile(ProjectProfile("autonomous", 1, "Supervisor-driven policy with conservative destructive-action checks.", ("core",), "workspace-write", True, True, True))

    catalog.register_feature(FeatureDescriptor("core", 1, "Base worktree and task-dossier behavior.", capabilities=("git.worktrees",), instruction_lines=("Keep changes scoped to the active task and repository ownership map.",)))
    catalog.register_feature(FeatureDescriptor("python", 1, "Python project support.", technologies=("python",), capabilities=("language.python",), editor_extensions=("ms-python.python",), instruction_lines=("Use the repository's Python environment and prefer `python -m` entry points.",), verification_commands=({"id": "python-tests", "profile": "focused", "command": "python3 -m pytest -q", "timeout_seconds": 900, "reason": "Python feature suggestion; approve after review."},), devcontainer_features={"ghcr.io/devcontainers/features/python:1": {}}))
    catalog.register_feature(FeatureDescriptor("cmake", 1, "CMake/C++ support.", technologies=("cpp", "cmake"), capabilities=("language.cpp", "build.cmake"), editor_extensions=("ms-vscode.cmake-tools", "ms-vscode.cpptools"), instruction_lines=("Use out-of-tree CMake builds and repository-owned presets when present.",), verification_commands=({"id": "cmake-build", "profile": "integration", "command": "cmake -S . -B build && cmake --build build", "timeout_seconds": 1800, "reason": "CMake feature suggestion; approve after review."},)))
    catalog.register_feature(FeatureDescriptor("ros2", 1, "ROS/colcon support.", technologies=("ros",), capabilities=("runtime.ros", "build.colcon"), editor_extensions=("ms-iot.vscode-ros",), instruction_lines=("Preserve ROS workspace overlays and source the intended environment explicitly.",), verification_commands=({"id": "colcon-test", "profile": "integration", "command": "colcon test --event-handlers console_direct+", "timeout_seconds": 3600, "reason": "ROS feature suggestion; approve after review."},)))
    catalog.register_feature(FeatureDescriptor("docker", 1, "Docker and Compose support.", technologies=("docker",), capabilities=("runtime.docker",), editor_extensions=("ms-azuretools.vscode-docker",), instruction_lines=("Treat container execution as an explicit policy decision; do not run unreviewed images.",), devcontainer_features={"ghcr.io/devcontainers/features/docker-in-docker:2": {"version": "latest"}}))
    catalog.register_feature(FeatureDescriptor("javascript", 1, "JavaScript/TypeScript support.", technologies=("javascript",), capabilities=("language.javascript",), editor_extensions=("dbaeumer.vscode-eslint",), instruction_lines=("Use the lockfile-selected package manager and repository scripts.",), verification_commands=({"id": "javascript-tests", "profile": "focused", "command": "npm test", "timeout_seconds": 900, "reason": "JavaScript feature suggestion; approve after review."},), devcontainer_features={"ghcr.io/devcontainers/features/node:1": {}}))
    catalog.register_feature(FeatureDescriptor("rust", 1, "Rust/Cargo support.", technologies=("rust",), capabilities=("language.rust",), editor_extensions=("rust-lang.rust-analyzer",), instruction_lines=("Use Cargo workspace commands and preserve lockfile policy.",), verification_commands=({"id": "cargo-test", "profile": "focused", "command": "cargo test --workspace", "timeout_seconds": 1800, "reason": "Rust feature suggestion; approve after review."},), devcontainer_features={"ghcr.io/devcontainers/features/rust:1": {}}))
    catalog.register_feature(FeatureDescriptor("go", 1, "Go module support.", technologies=("go",), capabilities=("language.go",), editor_extensions=("golang.go",), instruction_lines=("Run Go commands from the module or workspace root.",), verification_commands=({"id": "go-test", "profile": "focused", "command": "go test ./...", "timeout_seconds": 1200, "reason": "Go feature suggestion; approve after review."},), devcontainer_features={"ghcr.io/devcontainers/features/go:1": {}}))
    catalog.register_feature(FeatureDescriptor("java", 1, "Java build-system support.", technologies=("java",), capabilities=("language.java",), editor_extensions=("vscjava.vscode-java-pack",), instruction_lines=("Use the repository wrapper (`mvnw` or `gradlew`) when present.",)))
    catalog.register_feature(FeatureDescriptor("multi-repo", 1, "Multi-repository ownership guidance.", technologies=("multi-repo",), capabilities=("topology.multi-repo",), instruction_lines=("Respect repository ownership, required integration repositories, and cross-repository dependency order.",)))
    catalog.register_feature(FeatureDescriptor("devcontainer", 1, "Optional disposable workspace dev-container definition.", capabilities=("workspace.devcontainer",), instruction_lines=("Dev-container use is optional and must remain confined to the disposable task workspace.",)))
    return catalog


__all__ = [
    "FeatureDescriptor",
    "ProfileCatalogError",
    "ProjectProfile",
    "ProjectProfileCatalog",
    "ProjectProfileRenderer",
    "ProjectTemplateContext",
    "RenderedProfile",
    "default_profile_catalog",
]
