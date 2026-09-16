from __future__ import annotations

import json
import tarfile
from pathlib import Path
from typing import Any

from execraft.browser.adapter import sha256_data


class ArchiveBundler:
    """Bundles task-owned repositories and dossier files into a reproducible archive."""

    def __init__(
        self,
        workspace_root: Path,
        runs_dir: Path,
    ) -> None:
        self._workspace_root = workspace_root
        self._runs_dir = runs_dir

    def bundle(
        self,
        task_id: str,
        task_repos: dict[str, Path],
        runtime_repos: dict[str, Path],
        dossier_files: dict[str, Path],
        manifest: dict[str, Any] | None = None,
    ) -> tuple[Path, dict[str, Any]]:
        """Create a tar archive containing task-owned repos and dossier files.

        Returns:
            Tuple of (archive_path, bundle_manifest).
        """
        run_id = _generate_run_id(task_id)
        archive_path = self._runs_dir / run_id / "bundle.tar.gz"
        archive_path.parent.mkdir(parents=True, exist_ok=True)

        file_index: dict[str, str] = {}

        with tarfile.open(archive_path, "w:gz") as tar:
            for repo_id, repo_path in task_repos.items():
                if not repo_path.exists():
                    continue
                for fpath in sorted(repo_path.rglob("*")):
                    if fpath.is_file() and not _is_ignored(fpath):
                        arcname = f"repos/{repo_id}/{fpath.relative_to(repo_path)}"
                        tar.add(fpath, arcname=arcname)
                        file_index[arcname] = sha256_data(fpath.read_bytes())

            for name, fpath in dossier_files.items():
                if fpath.exists():
                    arcname = f"dossier/{name}"
                    tar.add(fpath, arcname=arcname)
                    file_index[arcname] = sha256_data(fpath.read_bytes())

        bundle_manifest = {
            "schema_version": 1,
            "task_id": task_id,
            "run_id": run_id,
            "archive_path": "bundle.tar.gz",
            "files": file_index,
            "repos": {rid: {"is_runtime": False} for rid in task_repos},
            "runtime_repos": sorted(runtime_repos),
            "source_manifest": manifest,
        }

        (archive_path.parent / "bundle-manifest.json").write_text(
            json.dumps(bundle_manifest, indent=2) + "\n"
        )

        return archive_path, bundle_manifest


def _generate_run_id(task_id: str) -> str:
    import hashlib
    import time

    raw = f"{task_id}-{time.time_ns()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _is_ignored(path: Path) -> bool:
    ignored = {
        "__pycache__",
        ".git",
        ".venv",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".tox",
        ".execraft",
        ".ai-workspace",
        ".ssh",
        ".env",
        ".ai-task.env",
        "credentials.json",
        "secrets.yaml",
    }
    return any(part in ignored or path.name.endswith(".pyc") for part in path.parts)
