from __future__ import annotations

import re
import tarfile
import hashlib
from pathlib import Path
from typing import Any


class ArchiveValidator:
    """Validates archives, ownership boundaries, and output files."""

    SECRET_PATTERNS: list[re.Pattern] = [
        re.compile(r"-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----"),
        re.compile(r"(?i)api[_-]?key\s*[=:]\s*['\"][^'\"]{8,}['\"]"),
        re.compile(r"(?i)secret\s*[=:]\s*['\"][^'\"]{8,}['\"]"),
        re.compile(r"(?i)password\s*[=:]\s*['\"][^'\"]{4,}['\"]"),
        re.compile(r"(?i)token\s*[=:]\s*['\"][^'\"]{8,}['\"]"),
    ]

    MAX_DELETION_THRESHOLD = 0.2  # 20% max deletion ratio

    def __init__(self, runtime_repo_ids: set[str]) -> None:
        self._runtime_repo_ids = runtime_repo_ids

    def validate_archive(
        self,
        archive_path: Path,
        bundle_manifest: dict[str, Any],
    ) -> list[str]:
        """Validate a bundle archive for ownership, secrets, and structure.

        Returns a list of validation errors (empty = valid).
        """
        errors: list[str] = []

        if not archive_path.exists():
            errors.append(f"Archive not found: {archive_path}")
            return errors

        if not tarfile.is_tarfile(archive_path):
            errors.append(f"Not a valid tar archive: {archive_path}")
            return errors

        runtime_value = bundle_manifest.get("runtime_repos", {})
        runtime_repos = (
            set(runtime_value.keys())
            if isinstance(runtime_value, dict)
            else set(runtime_value)
        )
        task_owned_repos = set(bundle_manifest.get("repos", {}).keys())

        with tarfile.open(archive_path, "r:gz") as tar:
            members = tar.getmembers()
            arc_names: list[str] = []
            for member in members:
                member_path = Path(member.name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    errors.append(f"Unsafe archive member path: {member.name}")
                    continue
                if member.issym() or member.islnk():
                    errors.append(f"Archive links are not allowed: {member.name}")
                    continue
                if member.isfile():
                    arc_names.append(member.name)

            for arcname in arc_names:
                if arcname.startswith("repos/"):
                    parts = arcname.split("/")
                    if len(parts) >= 2:
                        repo_id = parts[1]
                        if repo_id in runtime_repos:
                            errors.append(
                                f"Runtime-only repo '{repo_id}' has writable content: {arcname}"
                            )

                member = tar.getmember(arcname)
                f = tar.extractfile(member)
                if f is None:
                    continue
                content = f.read()
                try:
                    text = content.decode("utf-8", errors="replace")
                except UnicodeDecodeError:
                    continue

                for pattern in self.SECRET_PATTERNS:
                    if pattern.search(text):
                        errors.append(
                            f"Possible secret in {arcname}: matches {pattern.pattern[:40]}..."
                        )

        if not self._validate_deletion_ratio(arc_names, bundle_manifest):
            errors.append(
                f"Deletion ratio exceeds threshold ({self.MAX_DELETION_THRESHOLD:.0%})"
            )

        return errors

    def validate_output(
        self,
        output_files: list[dict[str, Any]],
        task_repo_ids: set[str],
    ) -> list[str]:
        """Validate output file changes from a browser run.

        Ensures changes target only task-owned repositories.
        """
        errors: list[str] = []
        for change in output_files:
            path = str(change.get("path", ""))
            if not _belongs_to_repo(path, task_repo_ids):
                errors.append(
                    f"Change targets non-task-owned path: {path}"
                )
            if change.get("action") == "rename":
                new_path = str(change.get("new_path", ""))
                if not _belongs_to_repo(new_path, task_repo_ids):
                    errors.append(
                        f"Rename targets non-task-owned path: {new_path}"
                    )
                elif Path(path).parts[0] != Path(new_path).parts[0]:
                    errors.append("Cross-repository renames are not allowed")
        return errors

    def validate_fingerprints(
        self,
        output_files: list[dict[str, Any]],
        repository_roots: dict[str, Path],
        bundle_manifest: dict[str, Any],
    ) -> list[str]:
        """Reject candidates whose source files changed after bundle creation."""

        source_files = bundle_manifest.get("files") or {}
        errors: list[str] = []
        source_counts: dict[str, int] = {}
        for path in source_files:
            parts = Path(str(path)).parts
            if len(parts) >= 3 and parts[0] == "repos":
                source_counts[parts[1]] = source_counts.get(parts[1], 0) + 1
        delete_counts: dict[str, int] = {}
        for change in output_files:
            if change.get("action") == "delete":
                parts = Path(str(change.get("path", ""))).parts
                if parts:
                    delete_counts[parts[0]] = delete_counts.get(parts[0], 0) + 1
        repository_source_count = sum(source_counts.values())
        delete_count = sum(delete_counts.values())
        if (
            repository_source_count
            and delete_count / repository_source_count > self.MAX_DELETION_THRESHOLD
        ):
            errors.append(
                f"Candidate deletion ratio exceeds threshold ({self.MAX_DELETION_THRESHOLD:.0%})"
            )
        for repository_id, count in delete_counts.items():
            source_count = source_counts.get(repository_id, 0)
            if source_count and count / source_count > self.MAX_DELETION_THRESHOLD:
                errors.append(
                    f"Candidate deletion ratio for {repository_id} exceeds threshold "
                    f"({self.MAX_DELETION_THRESHOLD:.0%})"
                )
        for change in output_files:
            relative = Path(str(change.get("path", "")))
            if relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 2:
                continue
            repository_id = relative.parts[0]
            repository_root = repository_roots.get(repository_id)
            if repository_root is None:
                errors.append(f"No active repository root for {repository_id}")
                continue
            root = repository_root.resolve()
            target = (root / Path(*relative.parts[1:])).resolve(strict=False)
            try:
                target.relative_to(root)
            except ValueError:
                errors.append(f"Path escapes repository root: {relative.as_posix()}")
                continue
            archive_name = f"repos/{relative.as_posix()}"
            expected = source_files.get(archive_name)
            action = change.get("action", "modify")
            if action == "create":
                if expected is not None or target.exists():
                    errors.append(f"Create conflicts with existing file: {relative.as_posix()}")
                continue
            if expected is None:
                errors.append(f"No source fingerprint for {relative.as_posix()}")
                continue
            if not target.is_file():
                errors.append(f"Source file disappeared: {relative.as_posix()}")
                continue
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != expected:
                errors.append(f"Source fingerprint changed: {relative.as_posix()}")
            if action == "rename":
                new_relative = Path(str(change.get("new_path", "")))
                if len(new_relative.parts) >= 2:
                    new_target = (
                        root / Path(*new_relative.parts[1:])
                    ).resolve(strict=False)
                    try:
                        new_target.relative_to(root)
                    except ValueError:
                        errors.append(
                            f"Rename destination escapes repository root: {new_relative.as_posix()}"
                        )
                    else:
                        if new_target.exists():
                            errors.append(
                                f"Rename destination already exists: {new_relative.as_posix()}"
                            )
        return errors

    def _validate_deletion_ratio(
        self,
        current_files: list[str],
        bundle_manifest: dict[str, Any],
    ) -> bool:
        source_files = set(bundle_manifest.get("files", {}).keys())
        if not source_files:
            return True
        deleted = source_files - set(current_files)
        ratio = len(deleted) / len(source_files)
        return ratio <= self.MAX_DELETION_THRESHOLD


def _belongs_to_repo(path: str, task_repo_ids: set[str]) -> bool:
    candidate = Path(path)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        return False
    return candidate.parts[0] in task_repo_ids and candidate.parts[0] not in {".", ""}
