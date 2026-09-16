from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from execraft.browser.adapter import (
    ApplyResult,
    BrowserAdapter,
    ExecuteResult,
    PrepareResult,
    ProbeResult,
    RunStatus,
)
from execraft.browser.apply import FileApplier
from execraft.browser.archive import ArchiveBundler
from execraft.browser.validator import ArchiveValidator


class BrowserAgent:
    """Orchestrates the browser-based AI agent lifecycle.

    Flow:
        login → probe → prepare → execute → status → apply
    """

    def __init__(
        self,
        adapter: BrowserAdapter,
        workspace_root: Path,
        runs_dir: Path | None = None,
        backup_dir: Path | None = None,
    ) -> None:
        self._adapter = adapter
        self._workspace_root = workspace_root
        self._runs_dir = runs_dir or workspace_root / ".execraft" / "runs"
        backup_dir = backup_dir or self._runs_dir / "backups"
        self._bundler = ArchiveBundler(workspace_root, self._runs_dir)
        self._applier = FileApplier(backup_dir)

    async def login(
        self, profile_dir: Path | None = None
    ) -> dict[str, Any]:
        """Authenticate with the browser-based service."""
        return await self._adapter.login(profile_dir)

    async def probe(self) -> ProbeResult:
        """Check if the browser adapter is available."""
        return await self._adapter.probe()

    async def prepare(
        self,
        task_id: str,
        task_repos: dict[str, Path],
        dossier_dir: Path | None = None,
        manifest: dict[str, Any] | None = None,
        runtime_repos: dict[str, Path] | None = None,
    ) -> PrepareResult:
        """Prepare a bundle archive for browser-based execution.

        Args:
            task_id: Unique task identifier.
            task_repos: Dict of task-owned repo IDs to their local paths.
            dossier_dir: Optional path to dossier files (BRIEF, PLAN, etc.).
            manifest: Optional workspace manifest.

        Returns:
            PrepareResult with run_id and bundle metadata.
        """
        runtime_repos = runtime_repos or {}
        dossier_files: dict[str, Path] = {}
        if dossier_dir and dossier_dir.exists():
            for f in dossier_dir.iterdir():
                if f.is_file():
                    dossier_files[f.name] = f

        archive_path, bundle_manifest = self._bundler.bundle(
            task_id=task_id,
            task_repos=task_repos,
            runtime_repos=runtime_repos,
            dossier_files=dossier_files,
            manifest=manifest,
        )
        errors = ArchiveValidator(set(runtime_repos)).validate_archive(
            archive_path, bundle_manifest
        )
        if errors:
            raise ValueError(f"browser input bundle rejected: {'; '.join(errors)}")
        return await self._adapter.prepare(archive_path, bundle_manifest)

    async def execute(self, run_id: str) -> ExecuteResult:
        """Execute a browser-based interaction for the given run."""
        return await self._adapter.execute(run_id)

    async def status(self, run_id: str) -> RunStatus:
        """Check the status of a browser agent run."""
        return await self._adapter.status(run_id)

    async def apply(
        self,
        run_id: str,
        repository_roots: dict[str, Path],
        output_files: list[dict[str, Any]],
        task_repo_ids: set[str],
        runtime_repo_ids: set[str],
        bundle_manifest: dict[str, Any],
        verify_commands: list[str | tuple[Path, str]] | None = None,
        handoff_path: Path | None = None,
    ) -> ApplyResult:
        """Validate and apply file changes from a browser run.

        Args:
            run_id: Unique run identifier.
            repository_roots: Exact task-owned repository roots keyed by stable ID.
            output_files: List of file changes from the execute step.
            task_repo_ids: Set of task-owned repository IDs for ownership validation.
            runtime_repo_ids: Set of runtime-only repository IDs.
            verify_commands: Optional list of verification shell commands.
            handoff_path: Optional path to HANDOFF.md to update.

        Returns:
            ApplyResult with applied/backed-up files and verification status.
        """
        validator = ArchiveValidator(runtime_repo_ids)
        ownership_errors = validator.validate_output(output_files, task_repo_ids)
        manifest_repositories = set((bundle_manifest.get("repos") or {}).keys())
        if manifest_repositories != set(repository_roots):
            ownership_errors.append(
                "Active repository mapping does not match the prepared bundle"
            )
        ownership_errors.extend(
            validator.validate_fingerprints(
                output_files, repository_roots, bundle_manifest
            )
        )
        if ownership_errors:
            return ApplyResult(
                run_id=run_id,
                message=f"Ownership validation failed: {'; '.join(ownership_errors)}",
            )

        grouped: dict[str, list[dict[str, Any]]] = {}
        for change in output_files:
            path = Path(str(change["path"]))
            repository_id = path.parts[0]
            rewritten = dict(change)
            rewritten["path"] = Path(*path.parts[1:]).as_posix()
            if change.get("action") == "rename":
                new_path = Path(str(change["new_path"]))
                rewritten["new_path"] = Path(*new_path.parts[1:]).as_posix()
            grouped.setdefault(repository_id, []).append(rewritten)

        applied_results: list[tuple[str, ApplyResult]] = []
        try:
            for repository_id, changes in grouped.items():
                result = self._applier.apply(
                    run_id=f"{run_id}-{repository_id}",
                    changes=changes,
                    target_root=repository_roots[repository_id],
                )
                if not result.verification_passed:
                    raise RuntimeError(result.message)
                applied_results.append((repository_id, result))
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            for applied_repository, _ in reversed(applied_results):
                self._applier.rollback(
                    f"{run_id}-{applied_repository}",
                    repository_roots[applied_repository],
                )
            return ApplyResult(
                run_id=run_id,
                message=f"Candidate apply failed and was rolled back: {exc}",
            )

        if not verify_commands:
            for repository_id, _ in reversed(applied_results):
                self._applier.rollback(
                    f"{run_id}-{repository_id}", repository_roots[repository_id]
                )
            return ApplyResult(
                run_id=run_id,
                message="Verification commands are required; candidate changes were rolled back",
            )

        verification_passed = True
        for item in verify_commands:
            cwd, command = item if isinstance(item, tuple) else (self._workspace_root, item)
            result = subprocess.run(
                command, shell=True, cwd=cwd, capture_output=True, text=True
            )
            if result.returncode != 0:
                verification_passed = False
                break
        if not verification_passed:
            for repository_id, _ in reversed(applied_results):
                self._applier.rollback(
                    f"{run_id}-{repository_id}", repository_roots[repository_id]
                )
            return ApplyResult(
                run_id=run_id,
                message="Verification failed; candidate changes were rolled back",
            )

        files_applied = [
            path for _, result in applied_results for path in result.files_applied
        ]
        files_backed_up = [
            path for _, result in applied_results for path in result.files_backed_up
        ]
        handoff_updated = False
        if handoff_path and handoff_path.is_file():
            existing = handoff_path.read_text(encoding="utf-8").rstrip()
            handoff_path.write_text(
                existing
                + f"\n\n## Browser run {run_id[:8]}\n\n"
                + f"Applied and verified {len(files_applied)} files.\n",
                encoding="utf-8",
            )
            handoff_updated = True
        return ApplyResult(
            run_id=run_id,
            files_applied=files_applied,
            files_backed_up=files_backed_up,
            verification_passed=True,
            handoff_updated=handoff_updated,
            message=f"Applied and verified {len(files_applied)} files",
        )
