from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from execraft.browser.adapter import ApplyResult


class FileApplier:
    """Applies file-level changes with backup and rollback."""

    def __init__(self, backup_dir: Path) -> None:
        self._backup_dir = backup_dir

    def apply(
        self,
        run_id: str,
        changes: list[dict[str, Any]],
        target_root: Path,
        verify_commands: list[str] | None = None,
        handoff_path: Path | None = None,
    ) -> ApplyResult:
        """Apply file changes from a browser run with backup and rollback support.

        Args:
            run_id: Unique run identifier.
            changes: List of file changes (path, action, content).
            target_root: Root directory for applying changes.
            verify_commands: Optional list of shell commands to run for verification.
            handoff_path: Optional path to HANDOFF.md to update.

        Returns:
            ApplyResult with applied/backed-up files and verification status.
        """
        applied: list[Path] = []
        backed_up: list[Path] = []
        run_backup = self._backup_dir / run_id
        run_backup.mkdir(parents=True, exist_ok=True)
        target_root = target_root.resolve()
        journal: list[dict[str, Any]] = []
        snapshotted: set[str] = set()

        def snapshot(path: Path) -> None:
            relative = path.relative_to(target_root).as_posix()
            if relative in snapshotted:
                return
            entry: dict[str, Any] = {"path": relative, "existed": path.exists()}
            if path.exists():
                if not path.is_file():
                    raise ValueError(f"browser apply supports files only: {relative}")
                backup_path = self._backup(path, run_backup, relative)
                entry["backup"] = backup_path.name
                backed_up.append(backup_path)
            journal.append(entry)
            snapshotted.add(relative)

        try:
            for change in changes:
                target_path = self._safe_target(target_root, str(change.get("path", "")))
                action = change.get("action", "modify")
                content = change.get("content")
                snapshot(target_path)

                if action == "delete":
                    if target_path.exists():
                        target_path.unlink()
                        applied.append(target_path)
                    continue

                if action in ("create", "modify"):
                    if content is None:
                        raise ValueError(f"{action} has no content: {change.get('path', '')}")
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.write_text(str(content), encoding="utf-8")
                    applied.append(target_path)
                elif action == "rename":
                    new_path = self._safe_target(
                        target_root, str(change.get("new_path", ""))
                    )
                    snapshot(new_path)
                    if not target_path.exists():
                        raise ValueError(f"rename source does not exist: {change.get('path', '')}")
                    new_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.rename(new_path)
                    applied.append(new_path)
                else:
                    raise ValueError(f"unsupported browser change action: {action!r}")
        except (OSError, ValueError) as exc:
            self._write_journal(run_backup, journal)
            self._rollback(run_backup, target_root)
            return ApplyResult(
                run_id=run_id,
                message=f"Rolled back due to apply error: {exc}",
            )

        self._write_journal(run_backup, journal)

        verification_passed = True
        if verify_commands:
            for cmd in verify_commands:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=target_root,
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    verification_passed = False
                    break

        if not verification_passed:
            self._rollback(run_backup, target_root)
            return ApplyResult(
                run_id=run_id,
                files_backed_up=backed_up,
                verification_passed=False,
                message="Verification failed; candidate changes were rolled back",
            )

        handoff_updated = False
        if handoff_path and verification_passed:
            self._update_handoff(handoff_path, run_id, len(applied))
            handoff_updated = True

        return ApplyResult(
            run_id=run_id,
            files_applied=applied,
            files_backed_up=backed_up,
            verification_passed=verification_passed,
            handoff_updated=handoff_updated,
            message=f"Applied {len(applied)} files, backed up {len(backed_up)}",
        )

    def rollback(self, run_id: str, target_root: Path) -> list[Path]:
        """Restore files from a specific run backup."""
        run_backup = self._backup_dir / run_id
        return self._rollback(run_backup, target_root)

    @staticmethod
    def _safe_target(target_root: Path, raw_path: str) -> Path:
        relative = Path(raw_path)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError(f"unsafe browser output path: {raw_path!r}")
        candidate = (target_root / relative).resolve(strict=False)
        try:
            candidate.relative_to(target_root)
        except ValueError as exc:
            raise ValueError(f"browser output escapes target root: {raw_path!r}") from exc
        return candidate

    def _backup(self, src: Path, backup_dir: Path, relative: str) -> Path:
        import hashlib

        backup_path = backup_dir / hashlib.sha256(relative.encode()).hexdigest()[:16]
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, backup_path)
        return backup_path

    @staticmethod
    def _write_journal(backup_dir: Path, journal: list[dict[str, Any]]) -> None:
        (backup_dir / "journal.json").write_text(
            json.dumps(journal, indent=2) + "\n", encoding="utf-8"
        )

    def _rollback(self, backup_dir: Path, target_root: Path) -> list[Path]:
        restored: list[Path] = []
        if not backup_dir.exists():
            return restored
        journal_path = backup_dir / "journal.json"
        if not journal_path.is_file():
            return restored
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        root = target_root.resolve()
        for entry in reversed(journal):
            target = self._safe_target(root, str(entry["path"]))
            if entry.get("existed"):
                backup = backup_dir / str(entry["backup"])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
                restored.append(target)
            elif target.exists():
                if target.is_file() or target.is_symlink():
                    target.unlink()
                    restored.append(target)
        return restored

    def _update_handoff(
        self, handoff_path: Path, run_id: str, file_count: int
    ) -> None:
        if not handoff_path.exists():
            return
        existing = handoff_path.read_text()
        entry = f"\n## Browser run {run_id[:8]}\n\nApplied {file_count} files.\n"
        handoff_path.write_text(existing + entry)
