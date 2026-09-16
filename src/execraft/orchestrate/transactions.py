"""Multi-repository Git transaction journal with pre/post snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from execraft.persistence.atomic import atomic_write_json

from .models import OrchestrateError


@dataclass
class RepositorySnapshot:
    repository_id: str
    branch: str
    head_commit: str
    dirty: bool
    path: str

    def as_mapping(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "branch": self.branch,
            "head_commit": self.head_commit,
            "dirty": self.dirty,
            "path": self.path,
        }

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "RepositorySnapshot":
        return cls(
            repository_id=str(data["repository_id"]),
            branch=str(data.get("branch", "")),
            head_commit=str(data.get("head_commit", "")),
            dirty=bool(data.get("dirty", False)),
            path=str(data.get("path", "")),
        )


@dataclass
class CommitTransaction:
    transaction_id: str
    timestamp: str
    work_package_id: str
    pre_snapshots: list[RepositorySnapshot] = field(default_factory=list)
    post_snapshots: list[RepositorySnapshot] = field(default_factory=list)
    status: str = "pending"
    error_message: str = ""

    def as_mapping(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "timestamp": self.timestamp,
            "work_package_id": self.work_package_id,
            "pre_snapshots": [s.as_mapping() for s in self.pre_snapshots],
            "post_snapshots": [s.as_mapping() for s in self.post_snapshots],
            "status": self.status,
            "error_message": self.error_message,
        }

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "CommitTransaction":
        return cls(
            transaction_id=str(data["transaction_id"]),
            timestamp=str(data.get("timestamp", "")),
            work_package_id=str(data.get("work_package_id", "")),
            pre_snapshots=[
                RepositorySnapshot.from_mapping(s)
                for s in data.get("pre_snapshots", [])
            ],
            post_snapshots=[
                RepositorySnapshot.from_mapping(s)
                for s in data.get("post_snapshots", [])
            ],
            status=str(data.get("status", "pending")),
            error_message=str(data.get("error_message", "")),
        )


class CommitJournal:
    def __init__(self, path: Path):
        self.path = path.resolve()

    def begin(
        self, transaction_id: str, timestamp: str, work_package_id: str
    ) -> CommitTransaction:
        tx = CommitTransaction(
            transaction_id=transaction_id,
            timestamp=timestamp,
            work_package_id=work_package_id,
            status="pending",
        )
        self._upsert(tx)
        return tx

    def commit(self, transaction_id: str) -> CommitTransaction:
        tx = self._load(transaction_id)
        tx.status = "committed"
        self._upsert(tx)
        return tx

    def fail(self, transaction_id: str, error_message: str) -> CommitTransaction:
        tx = self._load(transaction_id)
        tx.status = "failed"
        tx.error_message = error_message
        self._upsert(tx)
        return tx

    def add_snapshots(
        self,
        transaction_id: str,
        pre_snapshots: list[RepositorySnapshot] | None = None,
        post_snapshots: list[RepositorySnapshot] | None = None,
    ) -> CommitTransaction:
        tx = self._load(transaction_id)
        if pre_snapshots:
            tx.pre_snapshots.extend(pre_snapshots)
        if post_snapshots:
            tx.post_snapshots.extend(post_snapshots)
        self._upsert(tx)
        return tx

    def get(self, transaction_id: str) -> CommitTransaction | None:
        try:
            return self._load(transaction_id)
        except OrchestrateError:
            return None

    def pending_transactions(self) -> list[CommitTransaction]:
        return [
            tx for tx in self._all() if tx.status == "pending"
        ]

    def last_by_work_package(self, work_package_id: str) -> CommitTransaction | None:
        transactions = [
            tx for tx in self._all()
            if tx.work_package_id == work_package_id
        ]
        return transactions[-1] if transactions else None

    def _load(self, transaction_id: str) -> CommitTransaction:
        transactions = self._all()
        for tx in transactions:
            if tx.transaction_id == transaction_id:
                return tx
        raise OrchestrateError(f"transaction not found: {transaction_id}")

    def _all(self) -> list[CommitTransaction]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise OrchestrateError(
                f"corrupt commit journal: {self.path}: {exc}"
            ) from exc
        if not isinstance(data, list):
            raise OrchestrateError(
                f"commit journal must be a JSON array: {self.path}"
            )
        return [CommitTransaction.from_mapping(item) for item in data]

    def _upsert(self, transaction: CommitTransaction) -> None:
        transactions = self._all()
        replaced = False
        for i, existing in enumerate(transactions):
            if existing.transaction_id == transaction.transaction_id:
                transactions[i] = transaction
                replaced = True
                break
        if not replaced:
            transactions.append(transaction)
        atomic_write_json(
            self.path,
            [tx.as_mapping() for tx in transactions],
            indent=2,
            ensure_ascii=False,
        )


def snapshot_repository(
    repository_id: str, path: Path
) -> RepositorySnapshot:
    from execraft.workspace.task_git import current_branch, head_commit, working_tree_dirty

    return RepositorySnapshot(
        repository_id=repository_id,
        branch=current_branch(path),
        head_commit=head_commit(path),
        dirty=working_tree_dirty(path),
        path=str(path),
    )
