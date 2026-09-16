"""Atomic filesystem transactions for project and task creation."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from execraft.onboarding.models import CreationPlan, Evidence, Finding, PlannedFile
from execraft.persistence import FileLock, LockLevel


class CreationTransactionError(RuntimeError):
    """Raised when a staged creation cannot be validated or committed."""


Materializer = Callable[[Path], None]
Finalizer = Callable[[Path], object]
Rollback = Callable[[Path], None]


@dataclass(frozen=True)
class CreationResult:
    path: Path
    plan: CreationPlan
    finalizer_results: tuple[object, ...] = ()


def _creation_lock(parent: Path) -> FileLock:
    """Serialize creation commits under one parent directory.

    The lock file is intentionally persistent; file locking, not deletion, owns
    synchronization and avoids races between unlink and open.
    """

    user_component = str(os.getuid()) if hasattr(os, "getuid") else "user"
    relative_root = Path(f"execraft-{user_component}") / "creation-locks"
    runtime_base = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
    lock_root = runtime_base.expanduser().resolve() / relative_root
    try:
        lock_root.mkdir(parents=True, exist_ok=True)
        probe, probe_name = tempfile.mkstemp(prefix=".write-test-", dir=str(lock_root))
        os.close(probe)
        Path(probe_name).unlink()
    except OSError:
        # Sandboxes and stale sessions may expose an unwritable runtime mount.
        # The per-user system temporary path remains process-shared and safe.
        lock_root = Path(tempfile.gettempdir()).resolve() / relative_root
        lock_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(parent.resolve()).encode("utf-8")).hexdigest()
    lock_path = lock_root / f"{digest}.lock"
    return FileLock(lock_path, level=LockLevel.LIFECYCLE)


class AtomicTreeTransaction:
    """Stage a complete directory tree and atomically publish it.

    A transaction may be prepared exactly once and applied at most once.  The
    staging directory lives under the nearest existing target ancestor so a
    dry-run does not create control-plane directories.  Publishing still uses
    ``os.replace`` on the same filesystem.
    """

    def __init__(
        self,
        *,
        kind: str,
        identifier: str,
        target: Path,
        materializer: Materializer,
        required_files: Sequence[str] = (),
        evidence: Sequence[Evidence] = (),
        findings: Sequence[Finding] = (),
        metadata: Mapping[str, object] | None = None,
        finalizers: Sequence[Finalizer] = (),
        rollback_finalizers: Sequence[Rollback] = (),
        accept_decisions: bool = False,
    ) -> None:
        self.kind = kind
        self.identifier = identifier
        self.target = target.expanduser().resolve()
        self._materializer = materializer
        self._required_files = tuple(required_files)
        self._evidence = tuple(evidence)
        self._findings = tuple(findings)
        self._metadata = dict(metadata or {})
        self._finalizers = tuple(finalizers)
        self._rollback_finalizers = tuple(rollback_finalizers)
        self._accept_decisions = accept_decisions
        self._staging_parent: Path | None = None
        self._staged_tree: Path | None = None
        self._plan: CreationPlan | None = None
        self._applied = False

    @property
    def plan(self) -> CreationPlan:
        if self._plan is None:
            raise CreationTransactionError("transaction has not been prepared")
        return self._plan

    def prepare(self) -> CreationPlan:
        if self._plan is not None:
            return self._plan
        if self.target.exists():
            raise CreationTransactionError(f"target already exists: {self.target}")
        safe_identifier = re.sub(r"[^a-zA-Z0-9_.-]+", "-", self.identifier).strip("-")
        staging_base = self._nearest_existing_directory(self.target.parent)
        staging_parent = Path(
            tempfile.mkdtemp(
                prefix=f".execraft-{safe_identifier or self.kind}-",
                dir=str(staging_base),
            )
        )
        staged_tree = staging_parent / self.target.name
        try:
            staged_tree.mkdir()
            self._materializer(staged_tree)
            self._validate_staged_tree(staged_tree)
            planned_files = tuple(self._inventory(staged_tree))
            self._plan = CreationPlan(
                kind=self.kind,
                identifier=self.identifier,
                target=self.target,
                files=planned_files,
                evidence=self._evidence,
                findings=self._findings,
                metadata=self._metadata,
                decisions_accepted=self._accept_decisions,
            )
            self._staging_parent = staging_parent
            self._staged_tree = staged_tree
            return self._plan
        except Exception:
            shutil.rmtree(staging_parent, ignore_errors=True)
            raise

    def apply(self) -> CreationResult:
        plan = self.prepare()
        if self._applied:
            raise CreationTransactionError("transaction has already been applied")
        if not plan.can_apply:
            pending = ", ".join(item.code for item in plan.pending_decisions)
            suffix = (
                f"; pending operator decisions: {pending}"
                if pending and not plan.decisions_accepted
                else ""
            )
            raise CreationTransactionError(
                f"creation plan for {self.identifier!r} contains blocking findings{suffix}"
            )
        assert self._staged_tree is not None
        assert self._staging_parent is not None
        finalizer_results: list[object] = []
        published = False
        with _creation_lock(self.target.parent):
            if self.target.exists():
                raise CreationTransactionError(f"target already exists: {self.target}")
            self.target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(self._staged_tree, self.target)
                published = True
                for finalizer in self._finalizers:
                    finalizer_results.append(finalizer(self.target))
            except Exception:
                for rollback in reversed(self._rollback_finalizers):
                    try:
                        rollback(self.target)
                    except Exception:
                        pass
                if published:
                    shutil.rmtree(self.target, ignore_errors=True)
                raise
            finally:
                shutil.rmtree(self._staging_parent, ignore_errors=True)
        self._applied = True
        return CreationResult(
            path=self.target,
            plan=plan,
            finalizer_results=tuple(finalizer_results),
        )

    def close(self) -> None:
        if not self._applied and self._staging_parent is not None:
            shutil.rmtree(self._staging_parent, ignore_errors=True)
        self._staging_parent = None
        self._staged_tree = None

    def __enter__(self) -> "AtomicTreeTransaction":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def _validate_staged_tree(self, staged_tree: Path) -> None:
        if not any(staged_tree.iterdir()):
            raise CreationTransactionError("template produced an empty tree")
        for relative in self._required_files:
            candidate = (staged_tree / relative).resolve()
            try:
                candidate.relative_to(staged_tree.resolve())
            except ValueError as exc:
                raise CreationTransactionError(
                    f"required file escapes staged tree: {relative!r}"
                ) from exc
            if not candidate.is_file():
                raise CreationTransactionError(
                    f"template did not produce required file: {relative}"
                )
        for path in staged_tree.rglob("*"):
            if path.is_symlink():
                raise CreationTransactionError(
                    f"templates may not create symlinks: {path.relative_to(staged_tree)}"
                )

    @staticmethod
    def _nearest_existing_directory(path: Path) -> Path:
        """Return the closest existing ancestor without mutating the target tree."""

        candidate = path.resolve()
        while not candidate.exists():
            parent = candidate.parent
            if parent == candidate:
                raise CreationTransactionError(
                    f"cannot locate an existing staging ancestor for {path}"
                )
            candidate = parent
        if not candidate.is_dir():
            raise CreationTransactionError(
                f"staging ancestor is not a directory: {candidate}"
            )
        return candidate

    @staticmethod
    def _inventory(staged_tree: Path) -> list[PlannedFile]:
        files: list[PlannedFile] = []
        for path in sorted(item for item in staged_tree.rglob("*") if item.is_file()):
            content = path.read_bytes()
            files.append(
                PlannedFile(
                    path=path.relative_to(staged_tree).as_posix(),
                    size_bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            )
        return files


class ProjectCreationTransaction(AtomicTreeTransaction):
    """Project-specific atomic tree transaction."""

    def __init__(
        self,
        *,
        kind: str,
        identifier: str,
        target: Path,
        materializer: Materializer,
        required_files: Sequence[str] = (),
        evidence: Sequence[Evidence] = (),
        findings: Sequence[Finding] = (),
        metadata: Mapping[str, object] | None = None,
        finalizers: Sequence[Finalizer] = (),
        rollback_finalizers: Sequence[Rollback] = (),
        accept_decisions: bool = False,
    ) -> None:
        super().__init__(
            kind=kind,
            identifier=identifier,
            target=target,
            materializer=materializer,
            required_files=("project.yaml", *required_files),
            evidence=evidence,
            findings=findings,
            metadata=metadata,
            finalizers=finalizers,
            rollback_finalizers=rollback_finalizers,
            accept_decisions=accept_decisions,
        )


class TaskCreationTransaction(AtomicTreeTransaction):
    """Task-specific atomic tree transaction."""

    _REQUIRED = (
        "TASK.yaml",
        "DEFINITION.yaml",
        "BRIEF.md",
        "PLAN.md",
        "HANDOFF.md",
        "REVIEW.md",
    )

    def __init__(
        self,
        *,
        kind: str,
        identifier: str,
        target: Path,
        materializer: Materializer,
        required_files: Sequence[str] = (),
        evidence: Sequence[Evidence] = (),
        findings: Sequence[Finding] = (),
        metadata: Mapping[str, object] | None = None,
        finalizers: Sequence[Finalizer] = (),
        rollback_finalizers: Sequence[Rollback] = (),
        accept_decisions: bool = False,
    ) -> None:
        super().__init__(
            kind=kind,
            identifier=identifier,
            target=target,
            materializer=materializer,
            required_files=(*self._REQUIRED, *required_files),
            evidence=evidence,
            findings=findings,
            metadata=metadata,
            finalizers=finalizers,
            rollback_finalizers=rollback_finalizers,
            accept_decisions=accept_decisions,
        )
