"""Bidirectional JSONL process supervision for rich agent clients.

The one-shot provider runners remain appropriate for CI-style automation, but
an IDE-like console needs a long-lived stdin/stdout channel: provider events
must stream while an operator can enqueue steering messages.  This module owns
that transport without leaking Codex or Claude protocol details into the
orchestrator.

The process is always isolated in its own process group.  Input is bounded and
written through a non-blocking pipe, stdout is decoded as newline-delimited JSON,
and stderr remains available for diagnostics.  Provider-specific controllers
translate JSON messages into the stable interaction event model exposed by the
GUI.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from execraft.process import (
    ManagedProcessTerminated,
    ProcessHeartbeatCallback,
    ProcessOutputCallback,
    ProcessOutputClassifier,
    ProcessTerminationSignal,
    process_group_exists,
)

JsonObject = Mapping[str, Any]
JsonMessageCallback = Callable[[dict[str, Any]], "LiveSessionUpdate | None"]
JsonInputCallback = Callable[[], Iterable[JsonObject]]

_MAX_JSON_LINE_BYTES = 16 * 1024 * 1024
_MAX_CAPTURE_BYTES = 16 * 1024 * 1024
_POLL_SECONDS = 0.05
_POST_EXIT_DRAIN_SECONDS = 1.0


class LiveSessionUnavailable(RuntimeError):
    """Raised when a provider does not support the requested live protocol.

    Adapters may safely fall back to their legacy one-shot command only when
    this exception is raised before the provider started model work.
    """


@dataclass(frozen=True)
class LiveSessionUpdate:
    """Instructions returned by a provider protocol controller."""

    outbound: tuple[dict[str, Any], ...] = ()
    completed: bool = False
    terminate_process: bool = False


@dataclass(frozen=True)
class LiveProcessResult:
    """Bounded process result returned after a live session terminates."""

    returncode: int
    stdout: str
    stderr: str
    completed_by_protocol: bool = False


@dataclass
class _PendingInput:
    buffer: bytearray = field(default_factory=bytearray)
    closed: bool = False

    def append_messages(self, messages: Iterable[JsonObject]) -> None:
        if self.closed:
            return
        for message in messages:
            encoded = (json.dumps(dict(message), ensure_ascii=False) + "\n").encode("utf-8")
            self.buffer.extend(encoded)


def managed_jsonl_session(
    args: list[str],
    *,
    cwd: str | Path | None,
    timeout: float | None,
    startup_timeout: float | None = None,
    initial_messages: Iterable[JsonObject],
    message_callback: JsonMessageCallback,
    input_callback: JsonInputCallback | None = None,
    heartbeat_interval: float = 0.0,
    heartbeat_callback: ProcessHeartbeatCallback | None = None,
    output_classifier: ProcessOutputClassifier | None = None,
    output_callback: ProcessOutputCallback | None = None,
    inactivity_timeout: float | None = None,
    output_silence_timeout: float | None = None,
    refresh_timeout_on_protocol_progress: bool = False,
    close_stdin_on_complete: bool = True,
    terminate_on_complete: bool = False,
    environment: Mapping[str, str] | None = None,
) -> LiveProcessResult:
    """Run a bidirectional JSONL protocol under bounded supervision.

    ``message_callback`` receives complete JSON objects from stdout and may
    enqueue protocol replies or mark the session complete.  ``input_callback``
    is polled while the process is alive and is intended for operator steering.
    It returns provider-native JSON messages, keeping protocol state inside the
    adapter/controller rather than this transport.

    ``startup_timeout`` bounds the wait for the first valid provider protocol
    message.  Once the protocol starts, ``timeout`` becomes the rolling
    progress window when ``refresh_timeout_on_protocol_progress`` is enabled.
    Independent inactivity/output-silence watchdogs remain in force.
    """

    if startup_timeout is not None and float(startup_timeout) <= 0:
        raise ValueError("startup_timeout must be positive or None")

    proc = subprocess.Popen(
        args,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        bufsize=0,
        start_new_session=True,
        env=dict(environment) if environment is not None else None,
    )
    if proc.stdin is None or proc.stdout is None or proc.stderr is None:
        raise RuntimeError("live provider process did not expose stdio pipes")

    pgid = os.getpgid(proc.pid)
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        try:
            os.set_blocking(pipe.fileno(), False)
        except (AttributeError, OSError):
            pass

    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, data="stdout")
    selector.register(proc.stderr, selectors.EVENT_READ, data="stderr")

    pending = _PendingInput()
    pending.append_messages(initial_messages)
    if pending.buffer:
        selector.register(proc.stdin, selectors.EVENT_WRITE, data="stdin")

    stdout_capture = bytearray()
    stderr_capture = bytearray()
    stdout_lines = bytearray()
    started = time.monotonic()
    deadline_window = startup_timeout if startup_timeout is not None else timeout
    deadline = started + float(deadline_window) if deadline_window is not None else None
    inactivity_deadline = (
        started + float(inactivity_timeout)
        if inactivity_timeout is not None and inactivity_timeout > 0
        else None
    )
    silence_deadline = (
        started + float(output_silence_timeout)
        if output_silence_timeout is not None and output_silence_timeout > 0
        else None
    )
    heartbeat_every = max(0.0, float(heartbeat_interval))
    next_heartbeat = started + heartbeat_every if heartbeat_every else float("inf")
    last_output_at = started
    last_activity_at = started
    protocol_completed = False
    protocol_started = False
    terminate_requested = False
    termination_signal: ProcessTerminationSignal | None = None
    root_exit_at: float | None = None
    previous_cpu_ticks = 0
    output_bytes_since_heartbeat = 0
    input_bytes_since_heartbeat = 0

    def capture(target: bytearray, chunk: bytes) -> None:
        remaining = _MAX_CAPTURE_BYTES - len(target)
        if remaining > 0:
            target.extend(chunk[:remaining])

    def close_stdin() -> None:
        if pending.closed:
            return
        pending.closed = True
        pending.buffer.clear()
        try:
            selector.unregister(proc.stdin)
        except Exception:
            pass
        try:
            proc.stdin.close()
        except OSError:
            pass

    def register_stdin_if_needed() -> None:
        if pending.closed or not pending.buffer:
            return
        try:
            selector.get_key(proc.stdin)
        except KeyError:
            try:
                selector.register(proc.stdin, selectors.EVENT_WRITE, data="stdin")
            except (OSError, ValueError):
                pending.closed = True

    def apply_update(update: LiveSessionUpdate | None) -> None:
        nonlocal protocol_completed, terminate_requested
        if update is None:
            return
        pending.append_messages(update.outbound)
        register_stdin_if_needed()
        if update.completed:
            protocol_completed = True
            if close_stdin_on_complete:
                close_stdin()
            if update.terminate_process or terminate_on_complete:
                terminate_requested = True

    def process_json_lines() -> None:
        nonlocal deadline, protocol_started
        while True:
            newline = stdout_lines.find(b"\n")
            if newline < 0:
                if len(stdout_lines) > _MAX_JSON_LINE_BYTES:
                    raise RuntimeError("provider emitted an oversized JSONL record")
                return
            raw = bytes(stdout_lines[:newline])
            del stdout_lines[: newline + 1]
            if not raw.strip():
                continue
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "provider live session emitted invalid JSONL: "
                    + raw[:400].decode("utf-8", errors="replace")
                ) from exc
            if not isinstance(value, dict):
                continue
            protocol_started = True
            if refresh_timeout_on_protocol_progress and timeout is not None:
                deadline = time.monotonic() + float(timeout)
            apply_update(message_callback(value))

    try:
        while True:
            now = time.monotonic()
            returncode = proc.poll()
            group_alive = returncode is None or process_group_exists(pgid)

            if input_callback is not None and group_alive and not protocol_completed:
                try:
                    pending.append_messages(input_callback() or ())
                except Exception:
                    # Console steering is observational/operator control. A UI
                    # read failure must not crash the provider execution.
                    pass
                register_stdin_if_needed()

            if terminate_requested and group_alive:
                _terminate_group(pgid, grace_seconds=1.0)
                terminate_requested = False

            if group_alive:
                if deadline is not None and now >= deadline:
                    if not protocol_started and startup_timeout is not None:
                        termination_signal = ProcessTerminationSignal(
                            category="first_output_timeout",
                            summary=(
                                "provider emitted no valid JSONL protocol message during "
                                f"startup for {float(startup_timeout):.0f}s"
                            ),
                            persistent=False,
                        )
                        break
                    _terminate_group(pgid, grace_seconds=1.0)
                    proc.wait()
                    raise subprocess.TimeoutExpired(args, timeout)
                if inactivity_deadline is not None and now >= inactivity_deadline:
                    termination_signal = ProcessTerminationSignal(
                        category="inactive",
                        summary=(
                            "agent produced no output, input, or process activity for "
                            f"{float(inactivity_timeout):.0f}s"
                        ),
                        persistent=False,
                    )
                    break
                if silence_deadline is not None and now >= silence_deadline:
                    termination_signal = ProcessTerminationSignal(
                        category="output_silence",
                        summary=(
                            "agent produced no stdout/stderr for "
                            f"{float(output_silence_timeout):.0f}s"
                        ),
                        persistent=True,
                    )
                    break
            else:
                if root_exit_at is None:
                    root_exit_at = now
                if len(selector.get_map()) == 0 or now - root_exit_at >= _POST_EXIT_DRAIN_SECONDS:
                    break

            wait_until = min(
                deadline if group_alive and deadline is not None else float("inf"),
                inactivity_deadline
                if group_alive and inactivity_deadline is not None
                else float("inf"),
                silence_deadline if group_alive and silence_deadline is not None else float("inf"),
                next_heartbeat,
                now + _POLL_SECONDS,
            )
            selected = selector.select(max(0.001, wait_until - time.monotonic()))
            for key, mask in selected:
                stream = key.data
                pipe = key.fileobj
                if stream == "stdin" and mask & selectors.EVENT_WRITE:
                    if not pending.buffer:
                        try:
                            selector.unregister(pipe)
                        except Exception:
                            pass
                        continue
                    try:
                        written = os.write(pipe.fileno(), pending.buffer)
                    except BlockingIOError:
                        continue
                    except OSError:
                        close_stdin()
                        continue
                    if written > 0:
                        del pending.buffer[:written]
                        input_bytes_since_heartbeat += written
                        last_activity_at = time.monotonic()
                        if inactivity_timeout is not None and inactivity_timeout > 0:
                            inactivity_deadline = last_activity_at + float(inactivity_timeout)
                    if not pending.buffer:
                        try:
                            selector.unregister(pipe)
                        except Exception:
                            pass
                    continue

                try:
                    chunk = os.read(pipe.fileno(), 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    try:
                        selector.unregister(pipe)
                    except Exception:
                        pass
                    try:
                        pipe.close()
                    except OSError:
                        pass
                    continue

                output_bytes_since_heartbeat += len(chunk)
                now_value = time.monotonic()
                last_output_at = now_value
                last_activity_at = now_value
                if inactivity_timeout is not None and inactivity_timeout > 0:
                    inactivity_deadline = now_value + float(inactivity_timeout)
                if output_silence_timeout is not None and output_silence_timeout > 0:
                    silence_deadline = now_value + float(output_silence_timeout)
                decoded = chunk.decode("utf-8", errors="replace")
                if output_callback is not None:
                    try:
                        output_callback(stream, decoded)
                    except Exception:
                        pass
                if output_classifier is not None:
                    classified = output_classifier(stream, decoded)
                    if classified is not None and classified.category:
                        termination_signal = classified
                        break
                if stream == "stdout":
                    capture(stdout_capture, chunk)
                    stdout_lines.extend(chunk)
                    process_json_lines()
                else:
                    capture(stderr_capture, chunk)

            if termination_signal is not None:
                break

            now = time.monotonic()
            if now >= next_heartbeat:
                cpu_ticks, process_count = _linux_group_cpu_ticks(pgid)
                cpu_delta = max(0, cpu_ticks - previous_cpu_ticks)
                previous_cpu_ticks = cpu_ticks
                if cpu_delta > 0:
                    last_activity_at = now
                    if inactivity_timeout is not None and inactivity_timeout > 0:
                        inactivity_deadline = now + float(inactivity_timeout)
                if heartbeat_callback is not None and heartbeat_every:
                    snapshot = {
                        "pid": proc.pid,
                        "pgid": pgid,
                        "process_state": "running" if group_alive else "exited",
                        "process_state_code": "R" if group_alive else "X",
                        "process_count": process_count,
                        "elapsed_seconds": max(0.0, now - started),
                        "last_output_age_seconds": max(0.0, now - last_output_at),
                        "cpu_ticks_delta": cpu_delta,
                        "cpu_active": cpu_delta > 0,
                        "input_bytes_delta": input_bytes_since_heartbeat,
                        "output_bytes_delta": output_bytes_since_heartbeat,
                        "input_active": input_bytes_since_heartbeat > 0,
                        "output_active": output_bytes_since_heartbeat > 0,
                        "io_active": bool(
                            input_bytes_since_heartbeat or output_bytes_since_heartbeat
                        ),
                        "terminal_mode": "jsonl",
                    }
                    try:
                        heartbeat_callback(snapshot)
                    except Exception:
                        pass
                input_bytes_since_heartbeat = 0
                output_bytes_since_heartbeat = 0
                next_heartbeat = now + heartbeat_every

        if termination_signal is not None:
            _terminate_group(pgid, grace_seconds=1.0)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _terminate_group(pgid, grace_seconds=0.0)
                proc.wait()
            raise ManagedProcessTerminated(
                termination_signal,
                args=args,
                stdout=stdout_capture.decode("utf-8", errors="replace"),
                stderr=stderr_capture.decode("utf-8", errors="replace"),
            )

        if proc.poll() is None:
            if protocol_completed and terminate_on_complete:
                _terminate_group(pgid, grace_seconds=1.0)
            else:
                try:
                    proc.wait(timeout=2 if protocol_completed else None)
                except subprocess.TimeoutExpired:
                    # A protocol can complete before a long-lived server exits
                    # (Codex app-server is the common case).  Do not leak that
                    # child process merely because the provider ignored EOF.
                    _terminate_group(pgid, grace_seconds=1.0)
                    proc.wait()
        process_json_lines()
        return LiveProcessResult(
            returncode=0 if protocol_completed and terminate_on_complete else (proc.returncode or 0),
            stdout=stdout_capture.decode("utf-8", errors="replace"),
            stderr=stderr_capture.decode("utf-8", errors="replace"),
            completed_by_protocol=protocol_completed,
        )
    except KeyboardInterrupt:
        _terminate_group(pgid, grace_seconds=1.0)
        proc.wait()
        raise
    finally:
        selector.close()
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is None:
                continue
            try:
                pipe.close()
            except OSError:
                pass


def _terminate_group(pgid: int, *, grace_seconds: float) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        if not process_group_exists(pgid):
            return
        time.sleep(0.02)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _linux_group_cpu_ticks(pgid: int) -> tuple[int, int]:
    """Return aggregate CPU ticks/process count with a portable fallback."""

    proc_root = Path("/proc")
    if os.name != "posix" or not proc_root.is_dir():
        return 0, 1 if process_group_exists(pgid) else 0
    ticks = 0
    count = 0
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
            closing = stat.rfind(")")
            fields = stat[closing + 2 :].split()
            # fields after comm start at kernel field 3; pgrp is field 5.
            if int(fields[2]) != pgid:
                continue
            ticks += int(fields[11]) + int(fields[12])
            count += 1
        except (OSError, ValueError, IndexError):
            continue
    return ticks, count
