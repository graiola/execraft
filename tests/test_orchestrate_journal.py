"""Tests for the append-only event journal."""

import json
import multiprocessing
from pathlib import Path

import pytest

import execraft.orchestrate.journal as journal_module
from execraft.orchestrate.journal import EventJournal, JournalEntry
from execraft.orchestrate.models import OrchestrateError


def _append_events(path: str, worker: int, count: int) -> None:
    journal = EventJournal(Path(path))
    for index in range(count):
        journal.append("concurrent", {"worker": worker, "index": index})



class TestJournalEntry:
    def test_create_entry(self):
        entry = JournalEntry(sequence=1, timestamp="2026-01-01T00:00:00", event_type="test")
        assert entry.sequence == 1
        assert entry.event_type == "test"

    def test_as_mapping_roundtrip(self):
        original = JournalEntry(
            sequence=5,
            timestamp="2026-06-15T12:00:00",
            event_type="transition",
            payload={"from": "running", "to": "completed"},
        )
        mapping = original.as_mapping()
        restored = JournalEntry.from_mapping(mapping)
        assert restored.sequence == 5
        assert restored.event_type == "transition"
        assert restored.payload["from"] == "running"


class TestEventJournal:
    def test_append_and_read(self, tmp_path):
        journal = EventJournal(tmp_path / "test-journal.json")
        entry = journal.append("test_event", {"key": "value"})
        assert entry.sequence == 1
        assert entry.event_type == "test_event"

        entries = journal.read()
        assert len(entries) == 1
        assert entries[0].sequence == 1

    def test_append_increments_sequence(self, tmp_path):
        journal = EventJournal(tmp_path / "seq-test.json")
        e1 = journal.append("first")
        e2 = journal.append("second")
        e3 = journal.append("third")
        assert e1.sequence == 1
        assert e2.sequence == 2
        assert e3.sequence == 3

    def test_read_from_sequence(self, tmp_path):
        journal = EventJournal(tmp_path / "from-seq.json")
        for i in range(5):
            journal.append(f"event_{i}")
        entries = journal.read(from_sequence=3)
        assert len(entries) == 3
        assert entries[0].sequence == 3
        assert entries[-1].sequence == 5

    def test_replay_with_handlers(self, tmp_path):
        journal = EventJournal(tmp_path / "replay.json")
        journal.append("start", {"id": 1})
        journal.append("middle", {"id": 2})
        journal.append("end", {"id": 3})

        seen: list[str] = []
        handlers = {
            "start": lambda e: seen.append(f"start-{e.payload['id']}"),
            "end": lambda e: seen.append(f"end-{e.payload['id']}"),
        }
        count = journal.replay(handlers)
        assert count == 2
        assert "start-1" in seen
        assert "end-3" in seen
        assert "middle-2" not in seen

    def test_replay_from_sequence(self, tmp_path):
        journal = EventJournal(tmp_path / "replay-from.json")
        for i in range(4):
            journal.append("event", {"i": i})
        seen: list[int] = []
        journal.replay({"event": lambda e: seen.append(e.payload["i"])}, from_sequence=3)
        assert seen == [2, 3]

    def test_last_sequence_empty(self, tmp_path):
        journal = EventJournal(tmp_path / "empty.json")
        assert journal.last_sequence() == 0

    def test_last_sequence(self, tmp_path):
        journal = EventJournal(tmp_path / "last-seq.json")
        journal.append("a")
        journal.append("b")
        assert journal.last_sequence() == 2

    def test_clear(self, tmp_path):
        journal = EventJournal(tmp_path / "clear.json")
        journal.append("test")
        assert journal.path.is_file()
        journal.clear()
        assert not journal.path.is_file()

    def test_clear_fsyncs_parent_with_shared_primitive(self, tmp_path, monkeypatch):
        journal = EventJournal(tmp_path / "clear-durable.json")
        journal.append("test")
        synced: list[Path] = []
        monkeypatch.setattr(journal_module, "fsync_directory", synced.append)

        journal.clear()

        assert synced == [tmp_path]

    def test_append_uses_shared_atomic_writer(self, tmp_path, monkeypatch):
        journal = EventJournal(tmp_path / "shared-writer.json")
        writes: list[tuple[Path, object]] = []

        def record_write(path, payload, **kwargs):
            writes.append((path, payload))

        monkeypatch.setattr(journal_module, "atomic_write_json", record_write)

        journal.append("shared", {"ready": True})

        assert writes[0][0] == journal.path
        assert writes[0][1][0]["event_type"] == "shared"

    def test_persistence_across_instances(self, tmp_path):
        path = tmp_path / "persist.json"
        j1 = EventJournal(path)
        j1.append("event_a")
        j1.append("event_b")

        j2 = EventJournal(path)
        entries = j2.read()
        assert len(entries) == 2
        assert entries[0].event_type == "event_a"
        assert entries[1].event_type == "event_b"

    def test_corrupt_journal_raises_error(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("not valid json", encoding="utf-8")
        journal = EventJournal(path)
        with pytest.raises(OrchestrateError, match="corrupt"):
            journal.append("test")

    def test_corrupt_non_list(self, tmp_path):
        path = tmp_path / "not-list.json"
        path.write_text(json.dumps({"key": "value"}), encoding="utf-8")
        journal = EventJournal(path)
        with pytest.raises(OrchestrateError, match="must be a JSON array"):
            journal.append("test")

    def test_missing_file_returns_empty(self, tmp_path):
        journal = EventJournal(tmp_path / "nonexistent.json")
        assert journal.read() == []

    def test_atomic_write_preserves_previous(self, tmp_path):
        path = tmp_path / "atomic.json"
        journal = EventJournal(path)
        journal.append("original")
        assert path.is_file()
        content_before = path.read_text(encoding="utf-8")
        journal.append("appended")
        content_after = path.read_text(encoding="utf-8")
        assert content_before != content_after
        assert "original" in content_after
        assert "appended" in content_after
    def test_concurrent_processes_preserve_every_event_and_sequence(self, tmp_path):
        path = tmp_path / "concurrent.json"
        context = multiprocessing.get_context("spawn")
        processes = [
            context.Process(target=_append_events, args=(str(path), worker, 12))
            for worker in range(4)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0

        entries = EventJournal(path).read()
        assert len(entries) == 48
        assert [entry.sequence for entry in entries] == list(range(1, 49))
        assert {
            (entry.payload["worker"], entry.payload["index"])
            for entry in entries
        } == {(worker, index) for worker in range(4) for index in range(12)}
