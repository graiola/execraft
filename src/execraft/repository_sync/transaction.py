"""Durable transaction state for multi-repository upstream synchronization.

Git cannot atomically commit across independent repositories.  The transaction
therefore records every immutable source SHA and target checkpoint before the
first merge starts.  Until any merge commit is created the transaction remains
rollback-capable; after the first commit it is forward-only and recovery must
finish the remaining repositories rather than rewriting history.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from execraft.persistence.atomic import atomic_write_yaml


class RepositorySyncTransactionError(RuntimeError):
    """Raised when repository-sync transaction state is missing or corrupt."""


_ALLOWED_PHASES = {
    "fetched",
    "merging",
    "resolving",
    "ready_verify",
    "verified",
    "committing",
    "complete",
    "rolled_back",
}
_ALLOWED_REPOSITORY_STATUSES = {
    "pending",
    "noop",
    "merge_ready",
    "conflicted",
    "resolved",
    "committed",
    "rolled_back",
}


def _integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RepositorySyncTransactionError(f"{label} must be an integer")
    return value


def _boolean(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise RepositorySyncTransactionError(f"{label} must be a boolean")
    return value


def _string(value: object, *, label: str, required: bool = False) -> str:
    if not isinstance(value, str):
        raise RepositorySyncTransactionError(f"{label} must be a string")
    result = value.strip()
    if required and not result:
        raise RepositorySyncTransactionError(f"{label} cannot be empty")
    return result


@dataclass
class RepositorySyncRepositoryState:
    repository_id: str
    remote: str
    source_branch: str
    source_commit: str
    target_branch: str
    target_before: str
    configured_base_branch: str = ""
    source_selection: str = "configured_base"
    merge_base: str = ""
    ahead_before: int = 0
    behind_before: int = 0
    status: str = "pending"
    conflict_paths: list[str] = field(default_factory=list)
    target_after: str = ""
    verified_tree: str = ""
    error_message: str = ""

    def as_mapping(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "remote": self.remote,
            "source_branch": self.source_branch,
            "source_commit": self.source_commit,
            "target_branch": self.target_branch,
            "configured_base_branch": self.configured_base_branch,
            "source_selection": self.source_selection,
            "target_before": self.target_before,
            "merge_base": self.merge_base,
            "ahead_before": int(self.ahead_before),
            "behind_before": int(self.behind_before),
            "status": self.status,
            "conflict_paths": list(self.conflict_paths),
            "target_after": self.target_after,
            "verified_tree": self.verified_tree,
            "error_message": self.error_message,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RepositorySyncRepositoryState":
        conflicts = raw.get("conflict_paths", [])
        if not isinstance(conflicts, list) or any(
            not isinstance(item, str) for item in conflicts
        ):
            raise RepositorySyncTransactionError(
                "repository-sync conflict_paths must be a list of strings"
            )
        ahead = _integer(
            raw.get("ahead_before", 0), label="repository-sync ahead_before"
        )
        behind = _integer(
            raw.get("behind_before", 0), label="repository-sync behind_before"
        )
        if ahead < 0 or behind < 0:
            raise RepositorySyncTransactionError(
                "repository-sync ahead/behind counts cannot be negative"
            )
        status = _string(
            raw.get("status", "pending"), label="repository-sync repository status"
        )
        if status not in _ALLOWED_REPOSITORY_STATUSES:
            raise RepositorySyncTransactionError(
                f"unsupported repository-sync repository status: {status!r}"
            )
        state = cls(
            repository_id=_string(
                raw.get("repository_id", ""),
                label="repository-sync repository_id",
                required=True,
            ),
            remote=_string(
                raw.get("remote", ""), label="repository-sync remote", required=True
            ),
            source_branch=_string(
                raw.get("source_branch", ""),
                label="repository-sync source_branch",
                required=True,
            ),
            source_commit=_string(
                raw.get("source_commit", ""),
                label="repository-sync source_commit",
                required=True,
            ),
            target_branch=_string(
                raw.get("target_branch", ""),
                label="repository-sync target_branch",
                required=True,
            ),
            configured_base_branch=_string(
                raw.get("configured_base_branch", ""),
                label="repository-sync configured_base_branch",
            ),
            source_selection=_string(
                raw.get("source_selection", "configured_base"),
                label="repository-sync source_selection",
            ),
            target_before=_string(
                raw.get("target_before", ""),
                label="repository-sync target_before",
                required=True,
            ),
            merge_base=_string(
                raw.get("merge_base", ""), label="repository-sync merge_base"
            ),
            ahead_before=ahead,
            behind_before=behind,
            status=status,
            conflict_paths=list(conflicts),
            target_after=_string(
                raw.get("target_after", ""), label="repository-sync target_after"
            ),
            verified_tree=_string(
                raw.get("verified_tree", ""), label="repository-sync verified_tree"
            ),
            error_message=_string(
                raw.get("error_message", ""), label="repository-sync error_message"
            ),
        )
        if state.source_selection not in {"configured_base", "operator_override"}:
            raise RepositorySyncTransactionError(
                f"unsupported repository-sync source_selection: {state.source_selection!r}"
            )
        if state.status == "committed" and not state.target_after:
            raise RepositorySyncTransactionError(
                f"committed repository-sync state {state.repository_id!r} is missing target_after"
            )
        return state


@dataclass
class RepositorySyncTransaction:
    schema_version: int
    transaction_id: str
    package_id: str
    package_fingerprint: str
    created_at: str
    updated_at: str
    phase: str
    forward_only: bool = False
    repositories: list[RepositorySyncRepositoryState] = field(default_factory=list)
    error_message: str = ""

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "transaction_id": self.transaction_id,
            "package_id": self.package_id,
            "package_fingerprint": self.package_fingerprint,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "phase": self.phase,
            "forward_only": self.forward_only,
            "repositories": [item.as_mapping() for item in self.repositories],
            "error_message": self.error_message,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RepositorySyncTransaction":
        schema = _integer(
            raw.get("schema_version", 0), label="repository-sync schema_version"
        )
        if schema != 1:
            raise RepositorySyncTransactionError(
                f"unsupported repository-sync transaction schema: {schema}"
            )
        repositories_raw = raw.get("repositories") or []
        if not isinstance(repositories_raw, list):
            raise RepositorySyncTransactionError(
                "repository-sync transaction repositories must be a list"
            )
        if any(not isinstance(item, Mapping) for item in repositories_raw):
            raise RepositorySyncTransactionError(
                "repository-sync transaction repositories must contain mappings"
            )
        phase = _string(
            raw.get("phase", ""), label="repository-sync phase", required=True
        )
        if phase not in _ALLOWED_PHASES:
            raise RepositorySyncTransactionError(
                f"unsupported repository-sync transaction phase: {phase!r}"
            )
        tx = cls(
            schema_version=schema,
            transaction_id=_string(
                raw.get("transaction_id", ""),
                label="repository-sync transaction_id",
                required=True,
            ),
            package_id=_string(
                raw.get("package_id", ""),
                label="repository-sync package_id",
                required=True,
            ),
            package_fingerprint=_string(
                raw.get("package_fingerprint", ""),
                label="repository-sync package_fingerprint",
                required=True,
            ),
            created_at=_string(
                raw.get("created_at", ""),
                label="repository-sync created_at",
                required=True,
            ),
            updated_at=_string(
                raw.get("updated_at", ""),
                label="repository-sync updated_at",
                required=True,
            ),
            phase=phase,
            forward_only=_boolean(
                raw.get("forward_only", False), label="repository-sync forward_only"
            ),
            repositories=[
                RepositorySyncRepositoryState.from_mapping(item)
                for item in repositories_raw
            ],
            error_message=_string(
                raw.get("error_message", ""), label="repository-sync error_message"
            ),
        )
        if not tx.repositories:
            raise RepositorySyncTransactionError(
                "repository-sync transaction contains no repositories"
            )
        repository_ids = [item.repository_id for item in tx.repositories]
        if len(repository_ids) != len(set(repository_ids)):
            raise RepositorySyncTransactionError(
                "repository-sync transaction contains duplicate repository IDs"
            )
        if tx.phase == "rolled_back" and tx.forward_only:
            raise RepositorySyncTransactionError(
                "rolled-back repository-sync transaction cannot be forward-only"
            )
        return tx

    def repository(self, repository_id: str) -> RepositorySyncRepositoryState:
        for item in self.repositories:
            if item.repository_id == repository_id:
                return item
        raise RepositorySyncTransactionError(
            f"repository {repository_id!r} is absent from sync transaction {self.transaction_id}"
        )

    @property
    def complete(self) -> bool:
        return self.phase == "complete"

    @property
    def pending(self) -> bool:
        return self.phase not in {"complete", "rolled_back"}


class RepositorySyncTransactionStore:
    """Atomic per-package repository-sync journal."""

    def __init__(self, state_dir: Path):
        self.root = Path(state_dir).expanduser().resolve() / "repository-sync"

    @staticmethod
    def _component(package_id: str) -> str:
        value = str(package_id).strip()
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value).strip(".-")
        return f"{(safe or 'package')[:100]}-{digest}.yaml"

    def path_for(self, package_id: str) -> Path:
        return self.root / self._component(package_id)

    def load(self, package_id: str) -> RepositorySyncTransaction | None:
        path = self.path_for(package_id)
        if not path.is_file():
            return None
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise RepositorySyncTransactionError(
                f"cannot read repository-sync transaction {path}: {exc}"
            ) from exc
        if not isinstance(raw, Mapping):
            raise RepositorySyncTransactionError(
                f"repository-sync transaction must contain a mapping: {path}"
            )
        tx = RepositorySyncTransaction.from_mapping(raw)
        if tx.package_id != package_id:
            raise RepositorySyncTransactionError(
                f"repository-sync transaction identity mismatch: {tx.package_id!r} != {package_id!r}"
            )
        return tx

    def save(self, transaction: RepositorySyncTransaction) -> Path:
        path = self.path_for(transaction.package_id)
        atomic_write_yaml(path, transaction.as_mapping(), sort_keys=False, width=1000)
        return path

    def list(self) -> list[RepositorySyncTransaction]:
        if not self.root.is_dir():
            return []
        transactions: list[RepositorySyncTransaction] = []
        for path in sorted(self.root.glob("*.yaml")):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if isinstance(raw, Mapping):
                    transactions.append(RepositorySyncTransaction.from_mapping(raw))
            except (OSError, yaml.YAMLError, RepositorySyncTransactionError) as exc:
                raise RepositorySyncTransactionError(
                    f"invalid repository-sync transaction {path}: {exc}"
                ) from exc
        return transactions

    def pending_transactions(self) -> list[RepositorySyncTransaction]:
        return [transaction for transaction in self.list() if transaction.pending]


__all__ = [
    "RepositorySyncRepositoryState",
    "RepositorySyncTransaction",
    "RepositorySyncTransactionError",
    "RepositorySyncTransactionStore",
]
