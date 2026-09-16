"""Lightweight progress and repetition tracking for live agent consoles."""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Mapping
from typing import Any


class LiveProgressTracker:
    """Track useful progress signals without interpreting private reasoning.

    The tracker deliberately relies on observable semantic actions: distinct
    commands/targets, completed tools, plans, diffs and file changes.  It emits
    a conservative ``possibly_stalled`` warning only after the same meaningful
    action recurs several times without a new target or completed change.
    """

    _MEANINGFUL_KINDS = {
        "tool", "tool_output", "tool_progress", "file_change", "plan", "diff",
        "assistant", "reasoning", "error", "approval", "question",
    }

    def __init__(self) -> None:
        self._recent_signatures: deque[str] = deque(maxlen=12)
        self._item_signatures: dict[str, str] = {}
        self._seen_targets: set[str] = set()
        self._seen_files: set[str] = set()
        self._completed_items: set[str] = set()
        self._failed_items: set[str] = set()
        self._commands: set[str] = set()
        self._events = 0
        self._meaningful_events = 0
        self._last_event_at = ""
        self._last_progress_at = ""
        self._current_activity = "Starting agent session"
        self._current_target = ""
        self._state = "starting"
        self._warning = ""
        self._repeat_count = 0

    def observe(self, record: Mapping[str, Any]) -> dict[str, Any]:
        self._events += 1
        kind = str(record.get("kind", "status"))
        status = str(record.get("status", "")).lower()
        item_id = str(record.get("item_id", ""))
        at = str(record.get("at", ""))
        summary = str(record.get("summary", ""))
        title = str(record.get("title", ""))
        target = str(record.get("target", ""))
        operation = str(record.get("operation", ""))
        command = str(record.get("command", ""))

        self._last_event_at = at or self._last_event_at
        if title or summary:
            self._current_activity = title or summary
        is_new_target = bool(target and target not in self._seen_targets)
        is_new_command = bool(command and command not in self._commands)
        if target:
            self._current_target = target
            self._seen_targets.add(target)
        if command:
            self._commands.add(command)

        self._collect_files(record)
        completion_status = status in {
            "completed", "success", "succeeded", "updated", "accepted", "auto_accepted"
        }
        newly_completed = bool(
            completion_status and item_id and item_id not in self._completed_items
        )
        if completion_status and item_id:
            self._completed_items.add(item_id)
        if status in {"failed", "error", "rejected", "declined"}:
            if item_id:
                self._failed_items.add(item_id)
            self._state = "attention"

        meaningful = kind in self._MEANINGFUL_KINDS and kind not in {"tool_output"}
        signature = self._signature(kind, operation, target, command, summary or title)
        if meaningful and signature:
            previous_for_item = self._item_signatures.get(item_id) if item_id else None
            if previous_for_item != signature:
                if item_id:
                    self._item_signatures[item_id] = signature
                self._meaningful_events += 1
                self._recent_signatures.append(signature)
                frequency = Counter(self._recent_signatures)[signature]
                self._repeat_count = frequency
                new_progress = (
                    is_new_target
                    or is_new_command
                    or newly_completed
                    or kind in {"file_change", "diff", "plan"}
                )
                if new_progress or frequency <= 2:
                    self._last_progress_at = at or self._last_progress_at
                if frequency >= 4 and kind in {"tool", "tool_progress"} and not new_progress:
                    self._state = "possibly_stalled"
                    self._warning = f"Repeated activity detected: {title or summary or operation} ×{frequency}"
                elif self._state != "attention":
                    self._state = "working"
                    self._warning = ""

        if newly_completed:
            self._last_progress_at = at or self._last_progress_at
            if self._state not in {"attention", "possibly_stalled"}:
                self._state = "working"

        if kind == "error":
            self._state = "attention"
            self._warning = summary or title or "Agent reported an error"
        elif status == "completed" and kind == "status":
            self._state = "completed"
            self._warning = ""

        return self.as_mapping()

    def as_mapping(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "current_activity": self._current_activity,
            "current_target": self._current_target,
            "last_event_at": self._last_event_at,
            "last_progress_at": self._last_progress_at,
            "warning": self._warning,
            "repeat_count": self._repeat_count,
            "events": self._events,
            "meaningful_events": self._meaningful_events,
            "distinct_targets": len(self._seen_targets),
            "files_touched": len(self._seen_files),
            "commands": len(self._commands),
            "completed_tools": len(self._completed_items),
            "failed_tools": len(self._failed_items),
        }

    def _collect_files(self, record: Mapping[str, Any]) -> None:
        target = str(record.get("target", ""))
        if target and ("/" in target or "." in target.rsplit("/", 1)[-1]):
            self._seen_files.add(target)
        data = record.get("data")
        if not isinstance(data, Mapping):
            return
        changes = data.get("changes")
        if not isinstance(changes, list):
            return
        for change in changes:
            if not isinstance(change, Mapping):
                continue
            for key in ("path", "file_path", "file", "filename"):
                value = change.get(key)
                if isinstance(value, str) and value:
                    self._seen_files.add(value)
                    break

    @staticmethod
    def _signature(kind: str, operation: str, target: str, command: str, summary: str) -> str:
        base = "|".join((kind, operation, target, command, summary[:240])).strip("|")
        return " ".join(base.lower().split())
