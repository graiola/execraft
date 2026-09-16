"""Tests for multi-repo Git transaction journal and repository snapshots."""

import json

import pytest

from execraft.orchestrate.models import OrchestrateError
from execraft.orchestrate.transactions import (
    CommitJournal,
    CommitTransaction,
    RepositorySnapshot,
)


class TestRepositorySnapshot:
    def test_create_snapshot(self):
        snap = RepositorySnapshot(
            repository_id="repo-a",
            branch="task/feature",
            head_commit="abc123def456",
            dirty=False,
            path="/tmp/test/repo-a",
        )
        assert snap.repository_id == "repo-a"
        assert snap.dirty is False

    def test_as_mapping_roundtrip(self):
        original = RepositorySnapshot(
            repository_id="core",
            branch="main",
            head_commit="deadbeef",
            dirty=True,
            path="/workspace/core",
        )
        mapping = original.as_mapping()
        restored = RepositorySnapshot.from_mapping(mapping)
        assert restored.repository_id == "core"
        assert restored.dirty is True
        assert restored.head_commit == "deadbeef"


class TestCommitTransaction:
    def test_create_transaction(self):
        tx = CommitTransaction(
            transaction_id="tx-001",
            timestamp="2026-01-01T00:00:00",
            work_package_id="wp-1",
            status="pending",
        )
        assert tx.transaction_id == "tx-001"
        assert tx.status == "pending"

    def test_with_snapshots(self):
        snap = RepositorySnapshot(
            repository_id="repo-a",
            branch="task/feature",
            head_commit="abc123",
            dirty=False,
            path="/tmp/repo-a",
        )
        tx = CommitTransaction(
            transaction_id="tx-002",
            timestamp="2026-06-15T12:00:00",
            work_package_id="wp-1",
            pre_snapshots=[snap],
        )
        assert len(tx.pre_snapshots) == 1

    def test_as_mapping_roundtrip(self):
        original = CommitTransaction(
            transaction_id="tx-003",
            timestamp="2026-06-01T00:00:00",
            work_package_id="wp-2",
            pre_snapshots=[
                RepositorySnapshot(repository_id="r1", branch="b1", head_commit="h1", dirty=False, path="/p1"),
            ],
            post_snapshots=[
                RepositorySnapshot(repository_id="r1", branch="b1", head_commit="h2", dirty=False, path="/p1"),
            ],
            status="committed",
        )
        mapping = original.as_mapping()
        restored = CommitTransaction.from_mapping(mapping)
        assert restored.transaction_id == "tx-003"
        assert restored.status == "committed"
        assert len(restored.pre_snapshots) == 1
        assert restored.post_snapshots[0].head_commit == "h2"


class TestCommitJournal:
    def test_begin_transaction(self, tmp_path):
        journal = CommitJournal(tmp_path / "commits.json")
        tx = journal.begin("tx-1", "2026-01-01T00:00:00", "wp-1")
        assert tx.status == "pending"
        assert tx.transaction_id == "tx-1"

    def test_commit_transaction(self, tmp_path):
        journal = CommitJournal(tmp_path / "commits.json")
        journal.begin("tx-1", "2026-01-01T00:00:00", "wp-1")
        tx = journal.commit("tx-1")
        assert tx.status == "committed"

    def test_fail_transaction(self, tmp_path):
        journal = CommitJournal(tmp_path / "commits.json")
        journal.begin("tx-1", "2026-01-01T00:00:00", "wp-1")
        tx = journal.fail("tx-1", "merge conflict")
        assert tx.status == "failed"
        assert tx.error_message == "merge conflict"

    def test_add_snapshots(self, tmp_path):
        journal = CommitJournal(tmp_path / "commits.json")
        journal.begin("tx-1", "2026-01-01T00:00:00", "wp-1")
        snap = RepositorySnapshot(repository_id="r1", branch="main", head_commit="abc", dirty=False, path="/p")
        journal.add_snapshots("tx-1", post_snapshots=[snap])
        tx = journal.get("tx-1")
        assert tx is not None
        assert len(tx.post_snapshots) == 1

    def test_get_nonexistent(self, tmp_path):
        journal = CommitJournal(tmp_path / "commits.json")
        assert journal.get("nonexistent") is None

    def test_pending_transactions(self, tmp_path):
        journal = CommitJournal(tmp_path / "commits.json")
        journal.begin("tx-1", "2026-01-01T00:00:00", "wp-1")
        journal.begin("tx-2", "2026-01-01T00:00:00", "wp-2")
        journal.commit("tx-1")
        pending = journal.pending_transactions()
        assert len(pending) == 1
        assert pending[0].transaction_id == "tx-2"

    def test_last_by_work_package(self, tmp_path):
        journal = CommitJournal(tmp_path / "commits.json")
        journal.begin("tx-1", "2026-01-01T00:00:00", "wp-1")
        journal.begin("tx-2", "2026-01-01T01:00:00", "wp-1")
        last = journal.last_by_work_package("wp-1")
        assert last is not None
        assert last.transaction_id == "tx-2"

    def test_last_by_work_package_none(self, tmp_path):
        journal = CommitJournal(tmp_path / "commits.json")
        assert journal.last_by_work_package("nonexistent") is None

    def test_persistence(self, tmp_path):
        path = tmp_path / "persist.json"
        j1 = CommitJournal(path)
        j1.begin("tx-1", "2026-01-01T00:00:00", "wp-1")
        j1.commit("tx-1")

        j2 = CommitJournal(path)
        tx = j2.get("tx-1")
        assert tx is not None
        assert tx.status == "committed"

    def test_corrupt_journal(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("not json", encoding="utf-8")
        journal = CommitJournal(path)
        with pytest.raises(OrchestrateError, match="corrupt"):
            journal.begin("tx-1", "", "")

    def test_corrupt_non_list(self, tmp_path):
        path = tmp_path / "not-list.json"
        path.write_text(json.dumps({"a": 1}), encoding="utf-8")
        journal = CommitJournal(path)
        with pytest.raises(OrchestrateError, match="must be a JSON array"):
            journal.begin("tx-1", "", "")
