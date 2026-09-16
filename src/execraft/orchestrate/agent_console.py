"""Durable agent consoles with bounded output and operator controls.

The orchestrator writes attributable stdout/stderr and provider-neutral semantic
events alongside atomic session metadata.  When enabled by project policy, a
running session may expose provider-native steering, an interactive PTY, or both.
Those transports remain independent: opening a browser console never changes the
child process into a TTY-capable session.

The durable control queue is deliberately not a general shell API.  It cannot
start processes, change repositories by itself, or address sessions outside the
current task.  Every control record is validated, bounded, and audited without
persisting clear-text operator input in the audit log.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from execraft.interaction import normalize_interaction_payload
from execraft.persistence.atomic import atomic_write_json

from .live_progress import LiveProgressTracker
from .terminal_screen import TerminalScreen

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_ANSI_SEQUENCE = re.compile(
    r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])"
)
_ALLOWED_SIGNALS = {"interrupt", "suspend", "continue", "window_change"}
_STREAMING_INTERACTION_KINDS = {
    "assistant_delta", "reasoning_delta", "tool_output", "tool_input_delta"
}


def _terminal_transcript(text: str) -> str:
    """Return searchable terminal text without expanding cursor redraws."""

    cleaned = _ANSI_SEQUENCE.sub("", str(text)).replace("\r\n", "\n")
    # A bare carriage return redraws the current line. Keep only the latest
    # rendition inside each output chunk rather than concatenating progress/TUI
    # frames or expanding them into artificial transcript lines.
    cleaned = "\n".join(segment.rsplit("\r", 1)[-1] for segment in cleaned.split("\n"))
    return "".join(
        character
        for character in cleaned
        if character in {"\n", "\t", "\b"} or ord(character) >= 32
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value).strip()).strip("-.")
    return cleaned or "unknown"


@dataclass(frozen=True)
class InteractiveTerminalPolicy:
    """Fail-closed limits for operator controls and optional PTY sessions."""

    enabled: bool = False
    max_input_event_bytes: int = 8 * 1024
    max_pending_input_bytes: int = 64 * 1024
    default_rows: int = 40
    default_columns: int = 120

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "InteractiveTerminalPolicy":
        raw = dict(value or {})
        allowed = {
            "enabled",
            "max_input_event_bytes",
            "max_pending_input_bytes",
            "default_rows",
            "default_columns",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(
                "interactive_console contains unsupported keys: " + ", ".join(unknown)
            )

        def integer(name: str, default: int, minimum: int, maximum: int) -> int:
            candidate = raw.get(name, default)
            if isinstance(candidate, bool):
                raise ValueError(f"interactive_console.{name} must be an integer")
            try:
                parsed = int(candidate)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"interactive_console.{name} must be an integer") from exc
            if not minimum <= parsed <= maximum:
                raise ValueError(
                    f"interactive_console.{name} must be between {minimum} and {maximum}"
                )
            return parsed

        enabled = raw.get("enabled", cls.enabled)
        if not isinstance(enabled, bool):
            raise ValueError("interactive_console.enabled must be a boolean")
        return cls(
            enabled=enabled,
            max_input_event_bytes=integer(
                "max_input_event_bytes", cls.max_input_event_bytes, 1, 64 * 1024
            ),
            max_pending_input_bytes=integer(
                "max_pending_input_bytes", cls.max_pending_input_bytes, 1024, 4 * 1024 * 1024
            ),
            default_rows=integer("default_rows", cls.default_rows, 10, 300),
            default_columns=integer("default_columns", cls.default_columns, 20, 500),
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_input_event_bytes": self.max_input_event_bytes,
            "max_pending_input_bytes": self.max_pending_input_bytes,
            "default_rows": self.default_rows,
            "default_columns": self.default_columns,
        }


class AgentConsoleStore:
    """Thread-safe store for live output and bounded operator controls."""

    def __init__(
        self,
        root: Path,
        *,
        max_session_bytes: int = 16 * 1024 * 1024,
        max_sessions_per_agent: int = 50,
        terminal_policy: InteractiveTerminalPolicy | None = None,
        event_flush_interval_seconds: float = 0.12,
        screen_flush_interval_seconds: float = 0.10,
        metadata_flush_interval_seconds: float = 0.50,
    ):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_session_bytes = max(4096, int(max_session_bytes))
        self.max_sessions_per_agent = max(1, int(max_sessions_per_agent))
        self.terminal_policy = terminal_policy or InteractiveTerminalPolicy()
        self.event_flush_interval_seconds = max(0.0, float(event_flush_interval_seconds))
        self.screen_flush_interval_seconds = max(0.0, float(screen_flush_interval_seconds))
        self.metadata_flush_interval_seconds = max(0.0, float(metadata_flush_interval_seconds))
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._active: dict[tuple[str, str, str], str] = {}
        self._truncated: set[str] = set()
        self._control_offsets: dict[str, int] = {}
        self._terminal_screens: dict[str, TerminalScreen] = {}
        self._screen_persisted_revision: dict[str, int] = {}
        self._screen_last_flush_at: dict[str, float] = {}
        self._event_buffers: dict[str, list[dict[str, Any]]] = {}
        self._event_buffer_bytes: dict[str, int] = {}
        self._event_last_flush_at: dict[str, float] = {}
        self._metadata_cache: dict[str, dict[str, Any]] = {}
        self._metadata_last_flush_at: dict[str, float] = {}
        self._session_versions: dict[str, int] = {}
        self._interaction_sequences: dict[str, int] = {}
        self._interaction_buffers: dict[str, list[dict[str, Any]]] = {}
        self._interaction_buffer_bytes: dict[str, int] = {}
        self._interaction_last_flush_at: dict[str, float] = {}
        self._interaction_flush_timers: dict[str, threading.Timer] = {}
        self._progress_trackers: dict[str, LiveProgressTracker] = {}

    def start(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        agent_id = str(payload.get("agent_id", "")).strip()
        package_id = str(payload.get("package_id", "")).strip()
        stage = str(payload.get("stage", "")).strip()
        if not agent_id or not package_id or not stage:
            return {}
        with self._lock:
            key = (agent_id, package_id, stage)
            previous = self._active.get(key)
            if previous:
                self._finish_locked(
                    previous, status="interrupted", detail="superseded by a new attempt"
                )
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            session_id = "-".join(
                [stamp, _safe(package_id), _safe(stage), _safe(agent_id), uuid.uuid4().hex[:8]]
            )
            directory = self.root / session_id
            directory.mkdir(parents=True, exist_ok=False)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
            steering_supported = bool(payload.get("steering_supported", False))
            interactive_pty = bool(payload.get("interactive_pty", False))
            controls_enabled = self.terminal_policy.enabled and (
                steering_supported or interactive_pty
            )
            metadata = {
                "schema_version": 5,
                "session_id": session_id,
                "agent_id": agent_id,
                "package_id": package_id,
                "stage": stage,
                "capability": str(payload.get("capability", "")),
                "adapter": str(payload.get("adapter", "")),
                "model": str(payload.get("model", "")),
                "attempt": int(payload.get("attempt", 1) or 1),
                "origin": str(payload.get("origin", "orchestrator")),
                "working_directory": str(payload.get("working_directory", "")),
                "command": [str(item) for item in payload.get("command", [])],
                "status": "running",
                "started_at": _utc_now(),
                "finished_at": "",
                "detail": "",
                "telemetry": {},
                "artifact": {},
                "interaction": {
                    "mode": str(payload.get("interaction_mode", "activity")),
                    "streaming": bool(payload.get("streaming_interaction", False)),
                    "steering_supported": steering_supported,
                    "session_resume": bool(payload.get("session_resume", False)),
                    "last_event_at": "",
                    "last_operator_message_at": "",
                    "transport": str(payload.get("transport", "")),
                    "control_mode": str(payload.get("control_mode", "none")),
                    "progress": LiveProgressTracker().as_mapping(),
                },
                "controls": {
                    "enabled": controls_enabled,
                    "provider_native_steering": steering_supported,
                    "interactive_pty": interactive_pty,
                    "control_offset": 0,
                    "last_operator_input_at": "",
                    "last_resize_at": "",
                },
                "terminal": {
                    **self.terminal_policy.as_mapping(),
                    "policy_enabled": self.terminal_policy.enabled,
                    "enabled": self.terminal_policy.enabled and interactive_pty,
                },
            }
            self._write_metadata(directory, metadata)
            self._metadata_cache[session_id] = dict(metadata)
            self._metadata_last_flush_at[session_id] = 0.0
            self._event_buffers[session_id] = []
            self._event_buffer_bytes[session_id] = 0
            # Flush the first event even when host uptime is shorter than the
            # interval; subsequent high-frequency chunks are coalesced normally.
            self._event_last_flush_at[session_id] = float("-inf")
            (directory / "events.jsonl").touch(mode=0o600, exist_ok=False)
            (directory / "interactions.jsonl").touch(mode=0o600, exist_ok=False)
            if controls_enabled:
                (directory / "control.jsonl").touch(mode=0o600, exist_ok=False)
            self._active[key] = session_id
            self._session_versions[session_id] = 1
            self._interaction_sequences[session_id] = 0
            self._interaction_buffers[session_id] = []
            self._interaction_buffer_bytes[session_id] = 0
            self._interaction_last_flush_at[session_id] = 0.0
            self._progress_trackers[session_id] = LiveProgressTracker()
            self._control_offsets[session_id] = 0
            if self.terminal_policy.enabled and interactive_pty:
                screen = TerminalScreen(
                    rows=self.terminal_policy.default_rows,
                    columns=self.terminal_policy.default_columns,
                )
                self._terminal_screens[session_id] = screen
                self._screen_persisted_revision[session_id] = -1
                self._screen_last_flush_at[session_id] = 0.0
                self._persist_terminal_screen_locked(session_id, force=True)
            self._prune_locked(agent_id)
            self._condition.notify_all()
            return metadata

    def append(self, payload: Mapping[str, Any]) -> None:
        """Append one console event and update the durable VT surface.

        Transcript text and terminal screen input are intentionally separate.
        Terminal events are interpreted once into a persistent VT surface and a
        conservative searchable transcript. This avoids duplicated TUI redraws
        without sacrificing an auditable event stream.
        """

        text = str(payload.get("text", ""))
        stream = str(payload.get("stream", "stdout"))
        if stream not in {"stdout", "stderr", "terminal", "system"}:
            stream = "stdout"
        if stream == "terminal":
            # Terminal producers submit raw PTY text once. The store owns both
            # representations: a VT surface for Terminal and a conservative,
            # searchable transcript for Activity. Custom producers may override
            # only the transcript while preserving the raw screen input.
            transcript_text = str(
                payload.get("terminal_transcript_text", _terminal_transcript(text))
            )
        else:
            transcript_text = text
        key = self._key(payload)
        with self._lock:
            session_id = self._active.get(key)
            if not session_id:
                return
            if stream == "terminal":
                screen_text = str(payload.get("terminal_screen_text", text))
                screen = self._terminal_screens.get(session_id)
                if screen is not None and screen_text and screen.feed(screen_text):
                    self._persist_terminal_screen_locked(session_id)
            if not transcript_text:
                return
            if session_id in self._truncated:
                return
            record = {
                "at": _utc_now(),
                "stream": stream,
                "text": transcript_text,
            }
            self._buffer_event_locked(session_id, record)
            self._touch_session_locked(session_id)

    def append_interaction(self, payload: Mapping[str, Any]) -> None:
        """Append one provider-neutral conversation/progress event.

        Semantic events are separate from raw stdout/stderr so the GUI can
        render an IDE-style chat, plan, tool activity, and diffs without
        reverse-engineering terminal text. Streaming deltas are coalesced in
        short bounded batches to keep disk and DOM work proportional.
        """

        key = self._key(payload)
        with self._lock:
            session_id = self._active.get(key)
            if not session_id:
                return
            self._append_interaction_locked(session_id, payload)

    def progress_snapshot(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return the latest semantic activity for a scoped running attempt.

        This is used by the human progress reporter to replace low-value kernel
        I/O counters with observable agent work such as commands, file targets,
        plans and repeated-action warnings.
        """

        key = self._key(payload)
        with self._lock:
            session_id = self._active.get(key)
            if not session_id:
                return {}
            metadata = self._metadata_for_session_locked(session_id)
            interaction = metadata.get("interaction")
            if not isinstance(interaction, Mapping):
                return {}
            progress = interaction.get("progress")
            if not isinstance(progress, Mapping):
                progress = {}
            return {
                "semantic_activity": str(progress.get("current_activity", "")),
                "semantic_target": str(progress.get("current_target", "")),
                "progress_state": str(progress.get("state", "")),
                "progress_warning": str(progress.get("warning", "")),
                "progress_events": int(progress.get("meaningful_events", 0) or 0),
                "progress_files": int(progress.get("files_touched", 0) or 0),
                "progress_commands": int(progress.get("commands", 0) or 0),
                "progress_completed_tools": int(progress.get("completed_tools", 0) or 0),
                "transport": str(interaction.get("transport", "")),
                "control_mode": str(interaction.get("control_mode", "none")),
            }

    def heartbeat(self, payload: Mapping[str, Any]) -> None:
        """Refresh live telemetry while coalescing high-frequency disk writes."""

        key = self._key(payload)
        with self._lock:
            session_id = self._active.get(key)
            if not session_id:
                return
            telemetry_keys = {
                "pid", "pgid", "process_state", "process_state_code",
                "process_count", "process_count_delta", "elapsed_seconds",
                "last_output_age_seconds", "cpu_ticks_delta",
                "read_bytes_delta", "write_bytes_delta",
                "read_chars_delta", "write_chars_delta", "io_active", "cpu_active",
                "input_bytes_delta", "output_bytes_delta",
                "input_active", "output_active", "disk_io_active", "stdio_active",
                "terminal_mode",
            }
            metadata = self._metadata_for_session_locked(session_id)
            metadata["telemetry"] = {
                name: payload[name] for name in telemetry_keys if name in payload
            }
            metadata["updated_at"] = _utc_now()
            self._metadata_cache[session_id] = metadata
            self._flush_event_buffer_locked(session_id)
            self._persist_terminal_screen_locked(session_id)
            self._persist_metadata_locked(session_id)
            self._touch_session_locked(session_id)

    def finish(self, payload: Mapping[str, Any]) -> None:
        key = self._key(payload)
        with self._lock:
            session_id = self._active.pop(key, None)
            if not session_id:
                return
            self._control_offsets.pop(session_id, None)
            self._finish_locked(
                session_id,
                status=str(payload.get("status", "completed")),
                detail=str(payload.get("detail", "")),
                artifact=payload.get("artifact"),
                duration_seconds=payload.get("duration_seconds"),
            )

    def sessions(self, agent_id: str, *, limit: int = 25) -> list[dict[str, Any]]:
        agent_id = str(agent_id).strip()
        rows: list[dict[str, Any]] = []
        if not self.root.is_dir():
            return rows
        candidates: list[tuple[float, Path]] = []
        for directory in self.root.iterdir():
            try:
                if directory.is_dir():
                    candidates.append((directory.stat().st_mtime, directory))
            except OSError:
                continue
        for _, directory in sorted(candidates, reverse=True):
            try:
                metadata = self._read_metadata(directory)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if metadata.get("agent_id") != agent_id:
                continue
            rows.append(metadata)
            if len(rows) >= max(1, min(100, int(limit))):
                break
        return rows

    def read_events(
        self,
        session_id: str,
        *,
        offset: int = 0,
        limit: int = 256_000,
        terminal_screen_token: str = "",
        include_events: bool = True,
        interaction_offset: int = 0,
        include_interactions: bool = True,
        since_version: int = -1,
        wait_seconds: float = 0.0,
    ) -> dict[str, Any]:
        """Read incremental activity and the latest terminal surface.

        Running sessions are served from the in-memory metadata and VT model.
        This avoids forcing three filesystem writes on every fast terminal poll.
        Activity is flushed only when explicitly requested; completed sessions
        continue to use their durable files as the source of truth.
        """

        directory = self._session_directory(session_id)
        with self._lock:
            active = self._owns_session_locked(session_id)
            current_version = self._session_versions.get(session_id, 0)
            bounded_wait = max(0.0, min(25.0, float(wait_seconds)))
            if active and bounded_wait and int(since_version) == current_version:
                self._condition.wait_for(
                    lambda: (
                        self._session_versions.get(session_id, 0) != current_version
                        or not self._owns_session_locked(session_id)
                    ),
                    timeout=bounded_wait,
                )
                active = self._owns_session_locked(session_id)
                current_version = self._session_versions.get(session_id, current_version)
            if include_events:
                self._flush_event_buffer_locked(session_id, force=True)
            if include_interactions:
                self._flush_interaction_buffer_locked(session_id, force=True)
            metadata = (
                self._metadata_for_session_locked(session_id)
                if active
                else self._read_metadata(directory)
            )
            screen = self._terminal_screens.get(session_id) if active else None
            if screen is not None:
                snapshot = screen.snapshot().as_mapping()
                current_screen_token = f"memory:{snapshot['revision']}:{snapshot['rows']}:{snapshot['columns']}"
                terminal_screen = (
                    {**snapshot, "updated_at": _utc_now()}
                    if current_screen_token != str(terminal_screen_token or "")
                    else {}
                )
            else:
                current_screen_token, terminal_screen = self._read_terminal_screen_file(
                    directory, terminal_screen_token
                )

        limit = max(1, min(1_000_000, int(limit)))
        activity = self._read_jsonl_window(
            directory / "events.jsonl", offset=offset, limit=limit, enabled=include_events
        )
        interactions = self._read_jsonl_window(
            directory / "interactions.jsonl",
            offset=interaction_offset,
            limit=limit,
            enabled=include_interactions,
        )

        return {
            "metadata": metadata,
            "events": activity["records"],
            "events_included": bool(include_events),
            "interactions": interactions["records"],
            "interactions_included": bool(include_interactions),
            "interaction_offset": interactions["offset"],
            "interaction_next_offset": interactions["next_offset"],
            "interaction_size_bytes": interactions["size_bytes"],
            "interaction_reset": interactions["reset"],
            "version": current_version,
            "terminal_screen": terminal_screen,
            "terminal_screen_token": current_screen_token,
            "terminal_screen_unchanged": bool(
                current_screen_token
                and current_screen_token == str(terminal_screen_token or "")
            ),
            "offset": activity["offset"],
            "next_offset": activity["next_offset"],
            "size_bytes": activity["size_bytes"],
            "reset": activity["reset"],
        }

    @staticmethod
    def _read_terminal_screen_file(
        directory: Path, terminal_screen_token: str
    ) -> tuple[str, dict[str, Any]]:
        screen_path = directory / "terminal-screen.json"
        if not screen_path.is_file():
            return "", {}
        try:
            screen_stat = screen_path.stat()
            token = f"file:{screen_stat.st_mtime_ns}:{screen_stat.st_size}"
            if token == str(terminal_screen_token or ""):
                return token, {}
            value = json.loads(screen_path.read_text(encoding="utf-8"))
            return token, value if isinstance(value, dict) else {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return "", {}

    def update_terminal_screen(
        self, payload: Mapping[str, Any], screen: Mapping[str, Any]
    ) -> None:
        """Persist an externally rendered screen for the active session.

        This compatibility hook remains useful for custom console owners. Core
        PTY paths should prefer :meth:`append` with raw terminal text so one
        reusable screen model serves standalone and orchestrated sessions.
        """

        key = self._key(payload)
        with self._lock:
            session_id = self._active.get(key)
            if not session_id:
                return
            value = dict(screen)
            value["updated_at"] = _utc_now()
            self._write_json(self.root / session_id / "terminal-screen.json", value)

    def resize_terminal_screen(
        self, payload: Mapping[str, Any], *, rows: int, columns: int
    ) -> None:
        """Resize the active session's shared terminal surface."""

        key = self._key(payload)
        with self._lock:
            session_id = self._active.get(key)
            screen = self._terminal_screens.get(session_id or "")
            if screen is None:
                return
            screen.resize(rows=rows, columns=columns)
            self._persist_terminal_screen_locked(session_id, force=True)

    def flush_terminal_screen(self, payload: Mapping[str, Any]) -> None:
        """Flush a throttled screen snapshot, typically from a heartbeat."""

        key = self._key(payload)
        with self._lock:
            session_id = self._active.get(key)
            if session_id:
                self._persist_terminal_screen_locked(session_id)

    def queue_control_event(
        self,
        session_id: str,
        *,
        action: str,
        data: str = "",
        rows: int | None = None,
        columns: int | None = None,
        signal_name: str = "",
    ) -> dict[str, Any]:
        """Append one validated operator control for a running session."""

        directory = self._session_directory(session_id)
        with self._lock:
            metadata = self._metadata_for_session_locked(session_id)
            terminal = dict(metadata.get("terminal") or {})
            interaction = dict(metadata.get("interaction") or {})
            controls = dict(metadata.get("controls") or {})
            if not self.terminal_policy.enabled or not controls.get("enabled"):
                raise ValueError("interactive agent controls are disabled for this session")
            if metadata.get("status") != "running":
                raise ValueError("operator controls are only valid for a running agent session")
            action = str(action).strip().lower()
            terminal_actions = {"input", "resize", "eof"}
            if action == "signal" and str(signal_name).strip().lower() != "interrupt":
                terminal_actions.add("signal")
            if action in terminal_actions and not terminal.get("enabled"):
                raise ValueError("this agent session does not expose an interactive PTY")
            record: dict[str, Any] = {"at": _utc_now(), "action": action}
            byte_count = 0
            digest = ""
            if action in {"input", "steer"}:
                payload = str(data)
                encoded = payload.encode("utf-8")
                if not encoded:
                    raise ValueError("operator input cannot be empty")
                if len(encoded) > self.terminal_policy.max_input_event_bytes:
                    raise ValueError(
                        "operator input exceeds the configured per-event byte limit"
                    )
                if action == "steer":
                    if not interaction.get("steering_supported"):
                        raise ValueError("this agent session does not support live steering")
                record["data"] = payload
                byte_count = len(encoded)
                digest = hashlib.sha256(encoded).hexdigest()
            elif action == "resize":
                parsed_rows = self._bounded_dimension(rows, "rows", 10, 300)
                parsed_columns = self._bounded_dimension(columns, "columns", 20, 500)
                record.update({"rows": parsed_rows, "columns": parsed_columns})
                screen = self._terminal_screens.get(session_id)
                if screen is not None:
                    screen.resize(rows=parsed_rows, columns=parsed_columns)
                    self._persist_terminal_screen_locked(session_id, force=True)
            elif action == "signal":
                signal_value = str(signal_name).strip().lower()
                if signal_value not in _ALLOWED_SIGNALS:
                    raise ValueError(
                        "terminal signal must be one of: "
                        + ", ".join(sorted(_ALLOWED_SIGNALS))
                    )
                record["signal"] = signal_value
            elif action == "eof":
                pass
            else:
                raise ValueError("unsupported operator control action")

            control_path = directory / "control.jsonl"
            consumed = int(
                controls.get(
                    "control_offset", terminal.get("control_offset", 0)
                )
                or 0
            )
            current_size = control_path.stat().st_size if control_path.is_file() else 0
            pending_bytes = max(0, current_size - consumed)
            queued_record_bytes = self._json_line_size(record)
            if (
                pending_bytes + queued_record_bytes
                > self.terminal_policy.max_pending_input_bytes
            ):
                raise ValueError(
                    "operator control queue is full; wait for the agent to consume it"
                )
            self._append_json_line(control_path, record, file_lock=True, sync=False)
            audit = {
                "at": record["at"],
                "session_id": session_id,
                "agent_id": metadata.get("agent_id", ""),
                "package_id": metadata.get("package_id", ""),
                "stage": metadata.get("stage", ""),
                "action": action,
                "bytes": byte_count,
                "sha256": digest,
                "rows": record.get("rows"),
                "columns": record.get("columns"),
                "signal": record.get("signal", ""),
            }
            self._append_json_line(
                self.root / "operator-input-audit.jsonl",
                audit,
                file_lock=True,
                sync=False,
            )
            if action == "steer":
                self._append_interaction_locked(
                    session_id,
                    {
                        "kind": "operator",
                        "text": str(data),
                        "status": "queued",
                        "provider": metadata.get("adapter", ""),
                    },
                )
                # _append_interaction_locked updates semantic interaction
                # metadata. Reload it rather than overwriting those changes
                # with the pre-append snapshot held by this method.
                metadata = self._metadata_for_session_locked(session_id)
                interaction = dict(metadata.get("interaction") or {})
                interaction["last_operator_message_at"] = record["at"]
                metadata["interaction"] = interaction
                self._metadata_cache[session_id] = metadata
            self._touch_session_locked(session_id)
            return {
                "accepted": True,
                "session_id": session_id,
                "action": action,
                "bytes": byte_count,
            }

    def consume_control_events(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Consume queued controls for the active attempt identified by *payload*."""

        key = self._key(payload)
        with self._lock:
            session_id = self._active.get(key)
            if not session_id or not self.terminal_policy.enabled:
                return []
            metadata = self._metadata_for_session_locked(session_id)
            controls = dict(metadata.get("controls") or {})
            if not controls.get("enabled"):
                return []
            directory = self.root / session_id
            path = directory / "control.jsonl"
            if not path.is_file():
                return []
            offset = self._control_offsets.get(session_id, 0)
            records: list[dict[str, Any]] = []
            with path.open("r+b") as handle:
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except (ImportError, OSError):
                    fcntl = None  # type: ignore[assignment]
                try:
                    handle.seek(offset)
                    while len(records) < 128:
                        line = handle.readline()
                        if not line:
                            break
                        offset = handle.tell()
                        try:
                            value = json.loads(line.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if isinstance(value, dict):
                            records.append(value)
                    file_size = os.fstat(handle.fileno()).st_size
                    if offset >= file_size:
                        # Remove delivered clear-text input as soon as the
                        # supervisor has consumed the complete queue. The audit
                        # retains only byte counts and hashes.
                        handle.seek(0)
                        handle.truncate(0)
                        offset = 0
                        handle.flush()
                finally:
                    if fcntl is not None:
                        try:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                        except OSError:
                            pass
            if not records:
                return []
            self._control_offsets[session_id] = offset
            metadata = self._metadata_for_session_locked(session_id)
            controls = dict(metadata.get("controls") or {})
            controls["control_offset"] = offset
            if any(item.get("action") in {"input", "steer"} for item in records):
                controls["last_operator_input_at"] = _utc_now()
            if any(item.get("action") == "resize" for item in records):
                controls["last_resize_at"] = _utc_now()
            metadata["controls"] = controls
            if self._owns_session_locked(session_id):
                self._metadata_cache[session_id] = metadata
                self._persist_metadata_locked(session_id)
            return records

    def read_artifact(self, path_value: str, *, limit: int = 1_000_000) -> dict[str, Any]:
        path = Path(path_value).expanduser().resolve()
        artifact_root = (self.root.parent / "agent-artifacts").resolve()
        try:
            path.relative_to(artifact_root)
        except ValueError as exc:
            raise ValueError(
                "artifact path is outside the task agent-artifacts directory"
            ) from exc
        if not path.is_file():
            raise ValueError("artifact does not exist")
        limit = max(1, min(2_000_000, int(limit)))
        with path.open("rb") as handle:
            content = handle.read(limit + 1)
        return {
            "path": str(path),
            "content": content[:limit].decode("utf-8", errors="replace"),
            "size_bytes": path.stat().st_size,
            "truncated": len(content) > limit,
        }

    def _append_interaction_locked(
        self, session_id: str, payload: Mapping[str, Any]
    ) -> None:
        normalized = normalize_interaction_payload(payload)
        kind = str(normalized.get("kind", "status")).strip().lower() or "status"
        text = str(normalized.get("text", ""))[:512_000]
        title = str(normalized.get("title", ""))[:8_000]
        summary = str(normalized.get("summary", ""))[:8_000]
        category = str(normalized.get("category", ""))[:128]
        operation = str(normalized.get("operation", ""))[:128]
        target = str(normalized.get("target", ""))[:2_000]
        command = str(normalized.get("command", ""))[:8_000]
        parent_item_id = str(normalized.get("parent_item_id", ""))[:512]
        status = str(normalized.get("status", ""))[:128]
        item_id = str(normalized.get("item_id", ""))[:512]
        data = normalized.get("data")
        if not isinstance(data, Mapping):
            data = {}
        # Bound arbitrary provider metadata independently of the visible text.
        encoded_data = json.dumps(dict(data), ensure_ascii=False)
        if len(encoded_data.encode("utf-8")) > 512_000:
            data = {"truncated": True, "preview": encoded_data[:64_000]}

        sequence = self._interaction_sequences.get(session_id, 0) + 1
        self._interaction_sequences[session_id] = sequence
        record = {
            "sequence": sequence,
            "at": _utc_now(),
            "kind": kind,
            "text": text,
            "title": title,
            "summary": summary,
            "category": category,
            "operation": operation,
            "target": target,
            "command": command,
            "parent_item_id": parent_item_id,
            "status": status,
            "item_id": item_id,
            "provider": str(normalized.get("provider", ""))[:128],
            "data": dict(data),
        }
        buffered = self._interaction_buffers.setdefault(session_id, [])
        buffered_bytes = self._interaction_buffer_bytes.get(session_id, 0)
        if (
            kind in _STREAMING_INTERACTION_KINDS
            and buffered
            and buffered[-1].get("kind") == kind
            and buffered[-1].get("item_id") == item_id
            and len(str(buffered[-1].get("text", ""))) + len(text) <= 32_000
        ):
            previous_size = self._json_line_size(buffered[-1])
            buffered[-1]["text"] = str(buffered[-1].get("text", "")) + text
            buffered[-1]["at"] = record["at"]
            # Later deltas often reveal the command/path only after the first
            # fragment. Preserve the newest non-empty presentation fields so a
            # coalesced record remains as informative as the full stream.
            for field in (
                "title", "summary", "category", "operation", "target",
                "command", "parent_item_id", "status", "provider",
            ):
                if record.get(field):
                    buffered[-1][field] = record[field]
            if record.get("data"):
                buffered[-1]["data"] = record["data"]
            buffered_bytes += self._json_line_size(buffered[-1]) - previous_size
            self._interaction_sequences[session_id] -= 1
        else:
            buffered.append(record)
            buffered_bytes += self._json_line_size(record)
        self._interaction_buffer_bytes[session_id] = buffered_bytes
        metadata = self._metadata_for_session_locked(session_id)
        interaction = dict(metadata.get("interaction") or {})
        interaction.update(
            {
                "mode": str(normalized.get("interaction_mode", "conversation")),
                "streaming": bool(normalized.get("streaming", True)),
                "steering_supported": bool(
                    normalized.get(
                        "steering_supported",
                        interaction.get("steering_supported", True),
                    )
                ),
                "transport": str(
                    normalized.get("transport", interaction.get("transport", ""))
                ),
                "control_mode": str(
                    normalized.get("control_mode", interaction.get("control_mode", "none"))
                ),
                "last_event_at": record["at"],
            }
        )
        tracker = self._progress_trackers.setdefault(session_id, LiveProgressTracker())
        interaction["progress"] = tracker.observe(record)
        metadata["interaction"] = interaction
        self._metadata_cache[session_id] = metadata
        now = time.monotonic()
        elapsed = now - self._interaction_last_flush_at.get(session_id, 0.0)
        should_flush = (
            kind not in _STREAMING_INTERACTION_KINDS
            or self._interaction_buffer_bytes[session_id] >= 64 * 1024
            or elapsed >= 0.08
        )
        if should_flush:
            self._cancel_interaction_flush_timer_locked(session_id)
            if self._flush_interaction_buffer_locked(session_id, force=True):
                self._touch_session_locked(session_id)
        else:
            self._schedule_interaction_flush_locked(session_id, max(0.001, 0.08 - elapsed))

    def _flush_interaction_buffer_locked(
        self, session_id: str, *, force: bool = False
    ) -> bool:
        records = self._interaction_buffers.get(session_id) or []
        if not records:
            return False
        now = time.monotonic()
        if (
            not force
            and now - self._interaction_last_flush_at.get(session_id, 0.0) < 0.08
        ):
            return False
        path = self.root / session_id / "interactions.jsonl"
        persisted = path.stat().st_size if path.is_file() else 0
        if persisted + self._interaction_buffer_bytes.get(session_id, 0) > self.max_session_bytes:
            sequence = self._interaction_sequences.get(session_id, 0) + 1
            self._interaction_sequences[session_id] = sequence
            records = [
                {
                    "sequence": sequence,
                    "at": _utc_now(),
                    "kind": "status",
                    "text": "Live interaction capture limit reached; consult the durable result artifact.",
                    "title": "Capture truncated",
                    "summary": "Live interaction capture limit reached",
                    "category": "status",
                    "operation": "status",
                    "target": "",
                    "command": "",
                    "parent_item_id": "",
                    "status": "truncated",
                    "item_id": "",
                    "provider": "execraft",
                    "data": {},
                }
            ]
        self._append_json_lines(path, records, sync=False)
        self._interaction_buffers[session_id] = []
        self._interaction_buffer_bytes[session_id] = 0
        self._interaction_last_flush_at[session_id] = now
        return True

    def _schedule_interaction_flush_locked(self, session_id: str, delay: float) -> None:
        current = self._interaction_flush_timers.get(session_id)
        if current is not None and current.is_alive():
            return

        def flush() -> None:
            with self._lock:
                self._interaction_flush_timers.pop(session_id, None)
                if not self._owns_session_locked(session_id):
                    return
                if self._flush_interaction_buffer_locked(session_id, force=True):
                    self._touch_session_locked(session_id)

        timer = threading.Timer(delay, flush)
        timer.daemon = True
        self._interaction_flush_timers[session_id] = timer
        timer.start()

    def _cancel_interaction_flush_timer_locked(self, session_id: str) -> None:
        timer = self._interaction_flush_timers.pop(session_id, None)
        if timer is not None:
            timer.cancel()

    def _touch_session_locked(self, session_id: str) -> None:
        self._session_versions[session_id] = self._session_versions.get(session_id, 0) + 1
        self._condition.notify_all()

    @staticmethod
    def _json_line_size(record: Mapping[str, Any]) -> int:
        return len((json.dumps(dict(record), ensure_ascii=False) + "\n").encode("utf-8"))

    @staticmethod
    def _read_jsonl_window(
        path: Path, *, offset: int, limit: int, enabled: bool
    ) -> dict[str, Any]:
        size = path.stat().st_size if path.is_file() else 0
        reset = offset < 0 or offset > size
        if reset:
            offset = 0
        records: list[dict[str, Any]] = []
        next_offset = offset
        if enabled and path.is_file():
            with path.open("rb") as handle:
                handle.seek(offset)
                consumed = 0
                while consumed < limit:
                    line = handle.readline()
                    if not line:
                        break
                    consumed += len(line)
                    next_offset = handle.tell()
                    try:
                        value = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(value, dict):
                        records.append(value)
        return {
            "records": records,
            "offset": offset,
            "next_offset": next_offset,
            "size_bytes": size,
            "reset": reset,
        }

    def _buffer_event_locked(self, session_id: str, record: Mapping[str, Any]) -> None:
        """Coalesce high-frequency terminal events into bounded durable batches."""

        if session_id in self._truncated:
            return
        encoded_size = len(
            (json.dumps(dict(record), ensure_ascii=False) + "\n").encode("utf-8")
        )
        path = self.root / session_id / "events.jsonl"
        buffered = self._event_buffer_bytes.get(session_id, 0)
        persisted = path.stat().st_size if path.is_file() else 0
        if persisted + buffered + encoded_size > self.max_session_bytes:
            self._flush_event_buffer_locked(session_id, force=True)
            self._mark_truncated_locked(session_id)
            return
        self._event_buffers.setdefault(session_id, []).append(dict(record))
        self._event_buffer_bytes[session_id] = buffered + encoded_size
        now = time.monotonic()
        if (
            self._event_buffer_bytes[session_id] >= 64 * 1024
            or now - self._event_last_flush_at.get(session_id, 0.0)
            >= self.event_flush_interval_seconds
        ):
            self._flush_event_buffer_locked(session_id, force=True)

    def _flush_event_buffer_locked(
        self, session_id: str, *, force: bool = False
    ) -> None:
        records = self._event_buffers.get(session_id) or []
        if not records:
            return
        now = time.monotonic()
        if (
            not force
            and now - self._event_last_flush_at.get(session_id, 0.0)
            < self.event_flush_interval_seconds
        ):
            return
        self._append_json_lines(
            self.root / session_id / "events.jsonl", records, sync=False
        )
        self._event_buffers[session_id] = []
        self._event_buffer_bytes[session_id] = 0
        self._event_last_flush_at[session_id] = now

    def _owns_session_locked(self, session_id: str) -> bool:
        return session_id in self._active.values()

    def _metadata_for_session_locked(self, session_id: str) -> dict[str, Any]:
        cached = self._metadata_cache.get(session_id)
        if cached is not None:
            return dict(cached)
        metadata = self._read_metadata(self.root / session_id)
        if self._owns_session_locked(session_id):
            self._metadata_cache[session_id] = dict(metadata)
        return metadata

    def _persist_metadata_locked(
        self, session_id: str, *, force: bool = False
    ) -> None:
        if not force and not self._owns_session_locked(session_id):
            return
        metadata = self._metadata_cache.get(session_id)
        if metadata is None:
            return
        now = time.monotonic()
        if (
            not force
            and now - self._metadata_last_flush_at.get(session_id, 0.0)
            < self.metadata_flush_interval_seconds
        ):
            return
        self._write_metadata(self.root / session_id, metadata)
        self._metadata_last_flush_at[session_id] = now

    def _drop_session_caches_locked(self, session_id: str) -> None:
        self._event_buffers.pop(session_id, None)
        self._event_buffer_bytes.pop(session_id, None)
        self._event_last_flush_at.pop(session_id, None)
        self._metadata_cache.pop(session_id, None)
        self._metadata_last_flush_at.pop(session_id, None)
        self._terminal_screens.pop(session_id, None)
        self._screen_persisted_revision.pop(session_id, None)
        self._screen_last_flush_at.pop(session_id, None)
        self._interaction_buffers.pop(session_id, None)
        self._interaction_buffer_bytes.pop(session_id, None)
        self._interaction_last_flush_at.pop(session_id, None)
        self._interaction_sequences.pop(session_id, None)
        self._progress_trackers.pop(session_id, None)
        self._session_versions.pop(session_id, None)
        self._cancel_interaction_flush_timer_locked(session_id)

    def _mark_truncated_locked(self, session_id: str) -> None:
        if session_id in self._truncated:
            return
        self._truncated.add(session_id)
        directory = self.root / session_id
        self._event_buffers[session_id] = []
        self._event_buffer_bytes[session_id] = 0
        metadata = self._metadata_for_session_locked(session_id)
        metadata["truncated"] = True
        self._metadata_cache[session_id] = metadata
        self._persist_metadata_locked(session_id, force=True)
        self._append_json_line(
            directory / "events.jsonl",
            {
                "at": _utc_now(),
                "stream": "system",
                "text": (
                    "\n[agent console capture limit reached; full provider result "
                    "may remain in the durable artifact]\n"
                ),
            },
        )

    def _prune_locked(self, agent_id: str) -> None:
        rows = self.sessions(agent_id, limit=100)
        removable = [item for item in rows if item.get("status") != "running"]
        keep_completed = max(0, self.max_sessions_per_agent - 1)
        for metadata in removable[keep_completed:]:
            session_id = str(metadata.get("session_id", ""))
            if not session_id or session_id in self._active.values():
                continue
            shutil.rmtree(self.root / session_id, ignore_errors=True)
            self._truncated.discard(session_id)
            self._control_offsets.pop(session_id, None)
            self._drop_session_caches_locked(session_id)

    def _key(self, payload: Mapping[str, Any]) -> tuple[str, str, str]:
        return (
            str(payload.get("agent_id", "")).strip(),
            str(payload.get("package_id", "")).strip(),
            str(payload.get("stage", "")).strip(),
        )

    def _session_directory(self, session_id: str) -> Path:
        session_id = str(session_id).strip()
        if not _SAFE_ID.fullmatch(session_id):
            raise ValueError("invalid agent console session id")
        directory = (self.root / session_id).resolve()
        try:
            directory.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("agent console session escapes the console root") from exc
        if not directory.is_dir():
            raise ValueError("unknown agent console session")
        return directory

    def _finish_locked(
        self,
        session_id: str,
        *,
        status: str,
        detail: str,
        artifact: Any = None,
        duration_seconds: Any = None,
    ) -> None:
        directory = self.root / session_id
        if not directory.is_dir():
            return
        metadata = self._metadata_for_session_locked(session_id)
        normalized_status = status or "completed"
        if normalized_status != "completed" or detail:
            interaction = dict(metadata.get("interaction") or {})
            self._append_interaction_locked(
                session_id,
                {
                    "kind": "status",
                    "title": f"Session {normalized_status}",
                    "text": detail or normalized_status,
                    "status": normalized_status,
                    "provider": metadata.get("adapter", "execraft"),
                    "interaction_mode": interaction.get("mode", "terminal"),
                    "streaming": interaction.get("streaming", False),
                    "steering_supported": interaction.get(
                        "steering_supported", False
                    ),
                },
            )
        self._flush_event_buffer_locked(session_id, force=True)
        self._cancel_interaction_flush_timer_locked(session_id)
        self._flush_interaction_buffer_locked(session_id, force=True)
        self._sync_path(directory / "events.jsonl")
        self._sync_path(directory / "interactions.jsonl")
        self._persist_terminal_screen_locked(session_id, force=True)
        metadata = self._metadata_for_session_locked(session_id)
        metadata["status"] = normalized_status
        metadata["detail"] = detail
        metadata["finished_at"] = _utc_now()
        if duration_seconds is not None:
            metadata["duration_seconds"] = float(duration_seconds)
        if isinstance(artifact, Mapping):
            metadata["artifact"] = dict(artifact)
        self._metadata_cache[session_id] = metadata
        self._persist_metadata_locked(session_id, force=True)
        self._touch_session_locked(session_id)
        self._drop_session_caches_locked(session_id)

    def _persist_terminal_screen_locked(
        self, session_id: str, *, force: bool = False
    ) -> None:
        screen = self._terminal_screens.get(session_id)
        if screen is None:
            return
        revision = screen.revision
        if revision == self._screen_persisted_revision.get(session_id, -1):
            return
        now = time.monotonic()
        if (
            not force
            and now - self._screen_last_flush_at.get(session_id, 0.0)
            < self.screen_flush_interval_seconds
        ):
            return
        value = screen.snapshot().as_mapping()
        value["updated_at"] = _utc_now()
        self._write_json(self.root / session_id / "terminal-screen.json", value)
        self._screen_persisted_revision[session_id] = revision
        self._screen_last_flush_at[session_id] = now

    @staticmethod
    def _bounded_dimension(value: Any, name: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool):
            raise ValueError(f"terminal {name} must be an integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"terminal {name} must be an integer") from exc
        if not minimum <= parsed <= maximum:
            raise ValueError(f"terminal {name} must be between {minimum} and {maximum}")
        return parsed

    @classmethod
    def _append_json_line(
        cls,
        path: Path,
        value: Mapping[str, Any],
        *,
        file_lock: bool = False,
        sync: bool = True,
    ) -> None:
        cls._append_json_lines(
            path, [value], file_lock=file_lock, sync=sync
        )

    @staticmethod
    def _append_json_lines(
        path: Path,
        values: list[Mapping[str, Any]],
        *,
        file_lock: bool = False,
        sync: bool = True,
    ) -> None:
        if not values:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = b"".join(
            (json.dumps(dict(value), ensure_ascii=False) + "\n").encode("utf-8")
            for value in values
        )
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if file_lock:
                try:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                except (ImportError, OSError):
                    pass
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            if sync:
                os.fsync(descriptor)
        finally:
            if file_lock:
                try:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
            os.close(descriptor)


    @staticmethod
    def _sync_path(path: Path) -> None:
        """Durably flush a completed session without syncing every output chunk."""

        if not path.is_file():
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_metadata(directory: Path) -> dict[str, Any]:
        value = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("agent console metadata must be an object")
        return value

    @classmethod
    def _write_metadata(cls, directory: Path, metadata: Mapping[str, Any]) -> None:
        cls._write_json(directory / "metadata.json", metadata, pretty=True)

    @staticmethod
    def _write_json(
        path: Path, value: Mapping[str, Any], *, pretty: bool = False
    ) -> None:
        atomic_write_json(
            path,
            dict(value),
            indent=2 if pretty else None,
            ensure_ascii=False,
            trailing_newline=True,
            mode=0o600,
        )
