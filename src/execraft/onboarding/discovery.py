"""Bounded, non-executing project discovery with explicit evidence.

Discovery inspects repository metadata and file names only.  It never imports,
builds, or executes code from the source tree.  Detector objects are small and
composable so additional ecosystems can be added without growing one bootstrap
function indefinitely.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence

from execraft.onboarding.models import Evidence, Finding, FindingSeverity
from execraft.project import ProjectError
from execraft.git import GitClient, GitError, GitPolicy, GitRequest, GitResult

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".venv",
    "venv",
    "node_modules",
    "build",
    "dist",
    "install",
    "log",
    "target",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
}
_MAX_DISCOVERY_DEPTH = 8
_MAX_FILES_PER_REPO = 25000


def _stable_id(*parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"ev-{digest}"


def _derive_project_id(source_root: Path) -> str:
    name = source_root.resolve().name
    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", name).strip("-").lower()
    return safe or "project"


def _is_git_worktree(path: Path) -> bool:
    marker = path / ".git"
    return marker.is_dir() or marker.is_file()


def _run_git(repo: Path, *args: str, timeout: int = 10) -> GitResult:
    try:
        return GitClient().execute(
            GitRequest(repo, tuple(args), GitPolicy(timeout_seconds=timeout, check=False))
        )
    except GitError:
        return GitResult(("git", *args), repo.resolve(), 127, "", "git unavailable")


@dataclass
class DiscoveredRepo:
    id: str
    path: str
    branch: str
    has_python: bool = False
    has_cpp: bool = False
    has_docker: bool = False
    has_ros: bool = False
    has_cmake: bool = False
    has_colcon: bool = False
    has_javascript: bool = False
    has_rust: bool = False
    has_go: bool = False
    has_java: bool = False
    is_nested: bool = False
    role: str = "component"
    base_branch: str = "main"
    dirty: bool = False
    operation: str = ""
    discovery_truncated: bool = False
    evidence_ids: list[str] = field(default_factory=list)

    def as_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "path": self.path,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "role": self.role,
            "nested": self.is_nested,
            "dirty": self.dirty,
            "operation": self.operation,
            "discovery_truncated": self.discovery_truncated,
            "technologies": sorted(
                name
                for name, enabled in {
                    "python": self.has_python,
                    "cpp": self.has_cpp,
                    "docker": self.has_docker,
                    "ros": self.has_ros,
                    "cmake": self.has_cmake,
                    "colcon": self.has_colcon,
                    "javascript": self.has_javascript,
                    "rust": self.has_rust,
                    "go": self.has_go,
                    "java": self.has_java,
                }.items()
                if enabled
            ),
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass
class DiscoveryReport:
    project_id: str
    source_root: str
    repositories: list[DiscoveredRepo] = field(default_factory=list)
    languages: set[str] = field(default_factory=set)
    has_docker_compose: bool = False
    has_makefile: bool = False
    has_package_json: bool = False
    unresolved_choices: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def can_scaffold(self) -> bool:
        return not any(item.blocks_apply for item in self.findings)

    def add_finding(self, finding: Finding, *, legacy_unresolved: bool = False) -> None:
        self.findings.append(finding)
        if legacy_unresolved:
            self.unresolved_choices.append(finding.message)

    def as_mapping(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "source_root": self.source_root,
            "can_scaffold": self.can_scaffold,
            "languages": sorted(self.languages),
            "features": {
                "docker_compose": self.has_docker_compose,
                "makefile": self.has_makefile,
                "package_json": self.has_package_json,
            },
            "repositories": [item.as_mapping() for item in self.repositories],
            "findings": [item.as_mapping() for item in self.findings],
            "evidence": [item.as_mapping() for item in self.evidence],
        }


@dataclass
class RepositoryFacts:
    """Mutable detector accumulator scoped to one repository inspection."""

    has_python: bool = False
    has_cpp: bool = False
    has_docker: bool = False
    has_ros: bool = False
    has_cmake: bool = False
    has_colcon: bool = False
    has_javascript: bool = False
    has_rust: bool = False
    has_go: bool = False
    has_java: bool = False


class RepositoryDetector(Protocol):
    """File-name detector contract used by :class:`DiscoveryEngine`."""

    def inspect(self, relative_path: Path, facts: RepositoryFacts) -> Sequence[str]:
        """Update *facts* and return the fields newly supported by this path."""


class EcosystemDetector:
    """Detect common language/build ecosystems from bounded path metadata."""

    _CPP_SUFFIXES = {".cpp", ".hpp", ".cc", ".cxx", ".c", ".h", ".hh"}

    def inspect(self, relative_path: Path, facts: RepositoryFacts) -> Sequence[str]:
        name = relative_path.name
        suffix = relative_path.suffix.lower()
        found: list[str] = []

        def mark(attribute: str) -> None:
            if not getattr(facts, attribute):
                setattr(facts, attribute, True)
                found.append(attribute)

        if suffix == ".py" or name in {
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "tox.ini",
            "noxfile.py",
        }:
            mark("has_python")
        if suffix in self._CPP_SUFFIXES:
            mark("has_cpp")
        if name == "Dockerfile" or name.startswith("Dockerfile."):
            mark("has_docker")
        if name == "CMakeLists.txt":
            mark("has_cmake")
        if name == "package.xml":
            mark("has_ros")
            mark("has_colcon")
        if name in {"package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json"}:
            mark("has_javascript")
        if name in {"Cargo.toml", "Cargo.lock"}:
            mark("has_rust")
        if name in {"go.mod", "go.sum"}:
            mark("has_go")
        if name in {
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
            "settings.gradle",
            "settings.gradle.kts",
        }:
            mark("has_java")
        return found


@dataclass(frozen=True)
class FileInventory:
    paths: tuple[Path, ...]
    source: str
    truncated: bool


class BoundedFileInventory:
    """Prefer Git's indexed inventory, falling back to a bounded directory walk."""

    def __init__(
        self,
        *,
        max_depth: int = _MAX_DISCOVERY_DEPTH,
        max_files: int = _MAX_FILES_PER_REPO,
    ) -> None:
        self.max_depth = max_depth
        self.max_files = max_files

    def collect(self, repo: Path) -> FileInventory:
        listed = _run_git(repo, "ls-files", "-co", "--exclude-standard", "-z", timeout=30)
        if listed.returncode == 0:
            raw_paths = [item for item in listed.stdout.split("\0") if item]
            truncated = len(raw_paths) > self.max_files
            selected = raw_paths[: self.max_files]
            return FileInventory(
                paths=tuple(Path(item) for item in selected),
                source="git-ls-files",
                truncated=truncated,
            )
        return self._walk(repo)

    def _walk(self, repo: Path) -> FileInventory:
        paths: list[Path] = []
        root_depth = len(repo.parts)
        truncated = False
        for current_raw, dirs, files in os.walk(str(repo), topdown=True):
            current = Path(current_raw)
            depth = len(current.parts) - root_depth
            dirs[:] = [
                name
                for name in dirs
                if name not in _SKIP_DIRS and depth < self.max_depth
            ]
            for name in files:
                if len(paths) >= self.max_files:
                    truncated = True
                    break
                paths.append((current / name).relative_to(repo))
            if truncated:
                break
        return FileInventory(tuple(paths), "bounded-walk", truncated)


class DiscoveryEngine:
    """Inspect source topology using composable, non-executing detectors."""

    def __init__(
        self,
        *,
        detectors: Sequence[RepositoryDetector] | None = None,
        inventory: BoundedFileInventory | None = None,
        max_repository_depth: int = _MAX_DISCOVERY_DEPTH,
    ) -> None:
        self._detectors = tuple(detectors or (EcosystemDetector(),))
        self._inventory = inventory or BoundedFileInventory()
        self._max_repository_depth = max_repository_depth

    def inspect(self, source_root: Path) -> DiscoveryReport:
        source_root = source_root.expanduser().resolve()
        if not source_root.is_dir():
            raise ProjectError(f"source root does not exist: {source_root}")

        report = DiscoveryReport(
            project_id=_derive_project_id(source_root),
            source_root=str(source_root),
        )
        project_evidence = Evidence(
            id=_stable_id(str(source_root), "project_id", report.project_id),
            subject="project",
            field="project_id",
            value=report.project_id,
            source="directory-name",
            confidence=0.9,
            rationale="Normalized from the source-root directory name.",
            location=str(source_root),
        )
        report.evidence.append(project_evidence)

        repositories = self._find_repositories(source_root)
        for repo_path in repositories:
            discovered, evidence, findings = self._inspect_repository(repo_path, source_root)
            report.repositories.append(discovered)
            report.evidence.extend(evidence)
            report.findings.extend(findings)
            self._collect_languages(report, discovered)

        report.has_docker_compose = any(
            (source_root / name).is_file()
            for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
        )
        report.has_makefile = (source_root / "Makefile").is_file()
        report.has_package_json = (source_root / "package.json").is_file()

        if not report.repositories:
            report.add_finding(
                Finding(
                    code="discovery.no_git_repository",
                    severity=FindingSeverity.ERROR,
                    message="No Git repositories found at source root",
                    subject="project",
                    remediation=(
                        "Initialize a Git repository or select a source directory "
                        "containing one."
                    ),
                    evidence_ids=(project_evidence.id,),
                ),
                legacy_unresolved=True,
            )
        elif len(report.repositories) == 1 and not _is_git_worktree(source_root):
            report.add_finding(
                Finding(
                    code="discovery.source_root_not_repository",
                    severity=FindingSeverity.WARNING,
                    message="Source root is not itself a Git repository",
                    subject="project",
                    remediation="Confirm the nested checkout is the intended project source.",
                    evidence_ids=tuple(report.repositories[0].evidence_ids),
                ),
                legacy_unresolved=True,
            )
        if len(report.repositories) > 1:
            report.add_finding(
                Finding(
                    code="discovery.multiple_repositories",
                    severity=FindingSeverity.DECISION_REQUIRED,
                    message=(
                        "Multiple repositories found — verify repository roles and "
                        "required flags"
                    ),
                    subject="project",
                    remediation="Review repository roles before enabling orchestration.",
                    evidence_ids=tuple(
                        evidence_id
                        for repository in report.repositories
                        for evidence_id in repository.evidence_ids
                    ),
                ),
                legacy_unresolved=True,
            )
        self._report_duplicate_repository_ids(report)
        self._report_repository_overlaps(report)
        return report

    @staticmethod
    def _report_duplicate_repository_ids(report: DiscoveryReport) -> None:
        """Expose basename-derived ID collisions before template rendering."""

        by_id: dict[str, list[DiscoveredRepo]] = {}
        for repository in report.repositories:
            by_id.setdefault(repository.id, []).append(repository)
        for repository_id, duplicates in sorted(by_id.items()):
            if len(duplicates) < 2:
                continue
            paths = ", ".join(item.path for item in duplicates)
            report.add_finding(
                Finding(
                    code="discovery.duplicate_repository_id",
                    severity=FindingSeverity.DECISION_REQUIRED,
                    message=(
                        f"Repository ID {repository_id!r} is derived from multiple "
                        f"paths: {paths}"
                    ),
                    subject="project",
                    remediation=(
                        "Review the generated deterministic suffixes or provide "
                        "explicit repository IDs before orchestration."
                    ),
                    evidence_ids=tuple(
                        evidence_id
                        for repository in duplicates
                        for evidence_id in repository.evidence_ids
                    ),
                ),
                legacy_unresolved=True,
            )

    @staticmethod
    def _report_repository_overlaps(report: DiscoveryReport) -> None:
        """Flag nested Git roots whose source trees overlap.

        Overlapping repositories are not automatically rejected because some
        products intentionally vendor an independent checkout.  They do require
        an explicit role/scope decision before worktree creation, however, since
        otherwise one file may appear to belong to two repositories.
        """

        repositories = [
            (item, Path(item.path).resolve()) for item in report.repositories
        ]
        for parent_index, (parent, parent_path) in enumerate(repositories):
            for child, child_path in repositories[parent_index + 1 :]:
                if parent_path == child_path:
                    continue
                if child_path.is_relative_to(parent_path):
                    outer, inner = parent, child
                elif parent_path.is_relative_to(child_path):
                    outer, inner = child, parent
                else:
                    continue
                report.add_finding(
                    Finding(
                        code="discovery.repository_overlap",
                        severity=FindingSeverity.DECISION_REQUIRED,
                        message=(
                            f"Repository {inner.id} is nested inside repository "
                            f"{outer.id}; their source scopes overlap"
                        ),
                        subject=f"repository:{inner.id}",
                        remediation=(
                            "Exclude one repository or explicitly confirm the nested "
                            "repository ownership model before creating worktrees."
                        ),
                        evidence_ids=tuple(
                            dict.fromkeys(outer.evidence_ids + inner.evidence_ids)
                        ),
                    ),
                    legacy_unresolved=True,
                )


    def _find_repositories(self, root: Path) -> list[Path]:
        repositories: list[Path] = []
        root_depth = len(root.parts)
        for current_raw, dirs, _files in os.walk(str(root), topdown=True):
            current = Path(current_raw)
            depth = len(current.parts) - root_depth
            dirs[:] = [
                name
                for name in dirs
                if name not in _SKIP_DIRS and depth < self._max_repository_depth
            ]
            if _is_git_worktree(current):
                repositories.append(current.resolve())
        return sorted(set(repositories))

    def _inspect_repository(
        self, repo_path: Path, source_root: Path
    ) -> tuple[DiscoveredRepo, list[Evidence], list[Finding]]:
        relative = repo_path.relative_to(source_root)
        repository_id = repo_path.name if relative.parts else source_root.name
        repository_id = _derive_project_id(Path(repository_id))
        evidence: list[Evidence] = []
        findings: list[Finding] = []

        branch, branch_evidence = self._current_branch(repo_path, repository_id)
        evidence.append(branch_evidence)
        base_branch, base_evidence = self._base_branch(repo_path, repository_id, branch)
        evidence.append(base_evidence)
        dirty_status = _run_git(
            repo_path,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        dirty = bool(dirty_status.stdout.strip())
        dirty_evidence = Evidence(
            id=_stable_id(str(repo_path), "dirty", str(dirty)),
            subject=f"repository:{repository_id}",
            field="dirty",
            value=dirty,
            source="git-status",
            confidence=1.0,
            rationale="Derived from git status --porcelain.",
            location=str(repo_path),
        )
        evidence.append(dirty_evidence)
        operation = self._git_operation(repo_path)
        operation_evidence = Evidence(
            id=_stable_id(str(repo_path), "operation", operation or "none"),
            subject=f"repository:{repository_id}",
            field="git_operation",
            value=operation,
            source="git-metadata",
            confidence=1.0,
            rationale="Detected active Git operation markers in the repository metadata directory.",
            location=str(repo_path),
        )
        evidence.append(operation_evidence)

        inventory = self._inventory.collect(repo_path)
        facts = RepositoryFacts()
        technology_evidence: dict[str, Evidence] = {}
        for path in inventory.paths:
            for detector in self._detectors:
                for field_name in detector.inspect(path, facts):
                    if field_name in technology_evidence:
                        continue
                    technology_evidence[field_name] = Evidence(
                        id=_stable_id(str(repo_path), field_name, path.as_posix()),
                        subject=f"repository:{repository_id}",
                        field=field_name,
                        value=True,
                        source=inventory.source,
                        confidence=0.95 if inventory.source == "git-ls-files" else 0.8,
                        rationale=f"Detected from {path.as_posix()}.",
                        location=str(repo_path / path),
                    )
        evidence.extend(technology_evidence.values())

        if dirty:
            findings.append(
                Finding(
                    code="discovery.repository_dirty",
                    severity=FindingSeverity.WARNING,
                    message=f"Repository {repository_id} has uncommitted changes",
                    subject=f"repository:{repository_id}",
                    remediation=(
                        "Commit, stash, or explicitly accept the dirty checkout before "
                        "workspace creation."
                    ),
                    evidence_ids=(dirty_evidence.id,),
                )
            )
        if operation:
            findings.append(
                Finding(
                    code="discovery.git_operation_active",
                    severity=FindingSeverity.WARNING,
                    message=f"Repository {repository_id} has an active {operation} operation",
                    subject=f"repository:{repository_id}",
                    remediation="Complete or abort the Git operation before workspace creation.",
                    evidence_ids=(operation_evidence.id,),
                )
            )
        if inventory.truncated:
            findings.append(
                Finding(
                    code="discovery.file_inventory_truncated",
                    severity=FindingSeverity.WARNING,
                    message=(
                        f"Repository {repository_id} file discovery reached the "
                        f"{self._inventory.max_files}-file limit"
                    ),
                    subject=f"repository:{repository_id}",
                    remediation=(
                        "Review detected technologies; additional manifests may not "
                        "have been inspected."
                    ),
                )
            )
        if base_evidence.confidence < 0.5 and branch not in {"main", "master", "trunk"}:
            findings.append(
                Finding(
                    code="discovery.base_branch_low_confidence",
                    severity=FindingSeverity.DECISION_REQUIRED,
                    message=(
                        f"Default branch for {repository_id} fell back to current branch "
                        f"{branch!r}"
                    ),
                    subject=f"repository:{repository_id}",
                    remediation="Confirm the permanent base branch before creating task worktrees.",
                    evidence_ids=(base_evidence.id,),
                )
            )

        discovered = DiscoveredRepo(
            id=repository_id,
            path=str(repo_path),
            branch=branch,
            base_branch=base_branch,
            is_nested=repo_path != source_root,
            dirty=dirty,
            operation=operation,
            discovery_truncated=inventory.truncated,
            evidence_ids=[item.id for item in evidence],
            **facts.__dict__,
        )
        return discovered, evidence, findings

    @staticmethod
    def _current_branch(repo: Path, repository_id: str) -> tuple[str, Evidence]:
        result = _run_git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
        branch = result.stdout.strip() if result.returncode == 0 else ""
        if not branch:
            branch = "main"
        evidence = Evidence(
            id=_stable_id(str(repo), "branch", branch),
            subject=f"repository:{repository_id}",
            field="branch",
            value=branch,
            source="git-symbolic-ref" if result.returncode == 0 else "fallback",
            confidence=1.0 if result.returncode == 0 else 0.1,
            rationale=(
                "Current symbolic branch reported by Git."
                if result.returncode == 0
                else "Git did not report a symbolic branch; using the conventional fallback."
            ),
            location=str(repo),
        )
        return branch, evidence

    @staticmethod
    def _base_branch(repo: Path, repository_id: str, current_branch: str) -> tuple[str, Evidence]:
        remote_head = _run_git(
            repo,
            "symbolic-ref",
            "--quiet",
            "--short",
            "refs/remotes/origin/HEAD",
        )
        if remote_head.returncode == 0 and remote_head.stdout.strip():
            raw = remote_head.stdout.strip()
            branch = raw.split("/", 1)[1] if "/" in raw else raw
            return branch, Evidence(
                id=_stable_id(str(repo), "base_branch", branch, "remote-head"),
                subject=f"repository:{repository_id}",
                field="base_branch",
                value=branch,
                source="git-remote-head",
                confidence=1.0,
                rationale="Resolved from refs/remotes/origin/HEAD.",
                location=str(repo),
            )

        branches = _run_git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        available = {item.strip() for item in branches.stdout.splitlines() if item.strip()}
        for candidate in ("main", "master", "trunk"):
            if candidate in available:
                return candidate, Evidence(
                    id=_stable_id(str(repo), "base_branch", candidate, "conventional"),
                    subject=f"repository:{repository_id}",
                    field="base_branch",
                    value=candidate,
                    source="git-local-branches",
                    confidence=0.8,
                    rationale=f"Selected existing conventional branch {candidate!r}.",
                    location=str(repo),
                )
        return current_branch, Evidence(
            id=_stable_id(str(repo), "base_branch", current_branch, "current"),
            subject=f"repository:{repository_id}",
            field="base_branch",
            value=current_branch,
            source="current-branch-fallback",
            confidence=0.3,
            rationale="No remote HEAD or conventional local base branch was available.",
            location=str(repo),
        )

    @staticmethod
    def _git_operation(repo: Path) -> str:
        result = _run_git(repo, "rev-parse", "--git-dir")
        if result.returncode != 0 or not result.stdout.strip():
            return ""
        raw = Path(result.stdout.strip())
        git_dir = raw if raw.is_absolute() else (repo / raw).resolve()
        markers = (
            ("MERGE_HEAD", "merge"),
            ("CHERRY_PICK_HEAD", "cherry-pick"),
            ("REVERT_HEAD", "revert"),
            ("BISECT_LOG", "bisect"),
            ("rebase-merge", "rebase"),
            ("rebase-apply", "rebase"),
        )
        for marker, operation in markers:
            if (git_dir / marker).exists():
                return operation
        return ""

    @staticmethod
    def _collect_languages(report: DiscoveryReport, repo: DiscoveredRepo) -> None:
        mapping = {
            "python": repo.has_python,
            "cpp": repo.has_cpp,
            "docker": repo.has_docker,
            "ros": repo.has_ros,
            "javascript": repo.has_javascript,
            "rust": repo.has_rust,
            "go": repo.has_go,
            "java": repo.has_java,
        }
        report.languages.update(name for name, enabled in mapping.items() if enabled)


def discover(source_root: Path) -> DiscoveryReport:
    """Compatibility function backed by the default :class:`DiscoveryEngine`."""

    return DiscoveryEngine().inspect(source_root)


def suggest_verification(report: DiscoveryReport) -> list[dict[str, object]]:
    """Return conservative, disabled verification suggestions with provenance."""

    commands: list[dict[str, object]] = []
    for repo in report.repositories:
        if repo.has_python:
            commands.append(
                {
                    "id": f"{repo.id}-pytest",
                    "repository_id": repo.id,
                    "profile": "focused",
                    "command": "python3 -m pytest -q",
                    "timeout_seconds": 900,
                    "enabled": False,
                    "reason": (
                        "Discovered Python sources; approve after confirming the "
                        "project test command."
                    ),
                    "source": {"kind": "heuristic", "confidence": 0.5},
                }
            )
        if repo.has_cmake:
            commands.append(
                {
                    "id": f"{repo.id}-cmake-build",
                    "repository_id": repo.id,
                    "profile": "integration",
                    "command": "cmake -S . -B build && cmake --build build",
                    "timeout_seconds": 1800,
                    "enabled": False,
                    "reason": (
                        "Discovered CMake; generated disabled to avoid running "
                        "unreviewed commands."
                    ),
                    "source": {"kind": "heuristic", "confidence": 0.5},
                }
            )
        if repo.has_javascript:
            commands.append(
                {
                    "id": f"{repo.id}-package-test",
                    "repository_id": repo.id,
                    "profile": "focused",
                    "command": "npm test",
                    "timeout_seconds": 900,
                    "enabled": False,
                    "reason": (
                        "Discovered JavaScript package metadata; confirm package "
                        "manager and test script."
                    ),
                    "source": {"kind": "heuristic", "confidence": 0.4},
                }
            )
    return commands


__all__ = [
    "BoundedFileInventory",
    "DiscoveredRepo",
    "DiscoveryEngine",
    "DiscoveryReport",
    "EcosystemDetector",
    "RepositoryDetector",
    "discover",
    "suggest_verification",
]
