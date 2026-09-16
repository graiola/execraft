"""Managed subprocess execution with process-group lifecycle and telemetry.

Every child process is assigned a new OS process group so that the entire tree
can be killed atomically when a stage is cancelled or times out. Optional
heartbeats expose bounded process-state and aggregate I/O activity without
streaming provider stdout/stderr into operator logs.
"""

from __future__ import annotations

import os
import signal
import selectors
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO, Any

ProcessHeartbeatCallback = Callable[[Mapping[str, Any]], None]
ProcessOutputCallback = Callable[[str, str], None]
ProcessTerminalCallback = Callable[[], list[Mapping[str, Any]]]

_POST_EXIT_PIPE_DRAIN_SECONDS = 1.0

_STATE_LABELS = {
    "R": "running",
    "S": "sleeping",
    "D": "disk_sleep",
    "T": "stopped",
    "t": "tracing_stop",
    "Z": "zombie",
    "X": "dead",
    "I": "idle",
}

@dataclass(frozen=True)
class ProcessTerminationSignal:
    """Terminal condition detected while an agent process is still alive."""

    category: str
    summary: str
    retry_after_seconds: float | None = None
    stream: str = ""
    excerpt: str = ""
    persistent: bool = False

class ManagedProcessTerminated(RuntimeError):
    """Raised after the supervisor terminates a process group by policy."""

    def __init__(
        self,
        signal: ProcessTerminationSignal,
        *,
        args: list[str],
        stdout: str,
        stderr: str,
    ) -> None:
        super().__init__(signal.summary)
        self.signal = signal
        self.command = list(args)
        self.stdout = stdout
        self.stderr = stderr

ProcessOutputClassifier = Callable[[str, str], ProcessTerminationSignal | None]

@dataclass
class _InteractiveTerminal:
    """Parent-side handle for an agent stdin/control PTY."""

    descriptor: int
    mode: str
    pending: bytearray

    def close(self) -> None:
        try:
            os.close(self.descriptor)
        except OSError:
            pass

def _spawn_process(
    args: list[str],
    *,
    cwd: str | Path | None,
    stdin: int | IO[Any] | None,
    stdout: Any,
    stderr: Any,
    text: bool,
    bufsize: int,
    terminal_callback: ProcessTerminalCallback | None,
    environment: Mapping[str, str] | None,
) -> tuple[subprocess.Popen[Any], _InteractiveTerminal | None]:
    """Spawn an agent, optionally with a real controlling PTY on stdin.

    stdout/stderr deliberately remain pipes or regular files. Provider adapters
    therefore retain their structured-output contract while the child sees a
    TTY on stdin and may read `/dev/tty` for interactive prompts.
    """

    if terminal_callback is None:
        return (
            subprocess.Popen(
                args,
                cwd=str(cwd) if cwd else None,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                text=text,
                bufsize=bufsize,
                start_new_session=True,
                env=environment,
            ),
            None,
        )
    if stdin is not None and stdin != subprocess.DEVNULL:
        raise ValueError("interactive terminal cannot be combined with an explicit stdin")

    if os.name != "posix":
        read_fd, write_fd = os.pipe()
        try:
            proc = subprocess.Popen(
                args,
                cwd=str(cwd) if cwd else None,
                stdin=read_fd,
                stdout=stdout,
                stderr=stderr,
                text=False,
                bufsize=0,
                start_new_session=True,
                env=environment,
            )
        except Exception:
            os.close(write_fd)
            raise
        finally:
            os.close(read_fd)
        try:
            os.set_blocking(write_fd, False)
        except (AttributeError, OSError):
            pass
        return proc, _InteractiveTerminal(write_fd, "pipe", bytearray())

    import pty

    master_fd, slave_fd = pty.openpty()
    try:
        os.set_blocking(master_fd, False)
    except (AttributeError, OSError):
        pass
    _set_terminal_size(master_fd, rows=40, columns=120)
    launcher = [
        sys.executable,
        str(Path(__file__).with_name("pty_launcher.py")),
        "--",
        *args,
    ]
    try:
        proc = subprocess.Popen(
            launcher,
            cwd=str(cwd) if cwd else None,
            stdin=slave_fd,
            stdout=stdout,
            stderr=stderr,
            text=False,
            bufsize=bufsize,
            start_new_session=True,
            env=environment,
        )
    except Exception:
        os.close(master_fd)
        raise
    finally:
        os.close(slave_fd)
    return proc, _InteractiveTerminal(master_fd, "pty", bytearray())

def _set_terminal_size(descriptor: int, *, rows: int, columns: int) -> None:
    if os.name != "posix":
        return
    try:
        import fcntl
        import struct
        import termios

        value = struct.pack("HHHH", int(rows), int(columns), 0, 0)
        fcntl.ioctl(descriptor, termios.TIOCSWINSZ, value)
    except (ImportError, OSError, ValueError):
        pass

def _poll_terminal_controls(
    terminal: _InteractiveTerminal | None,
    callback: ProcessTerminalCallback | None,
    *,
    pgid: int,
) -> bool:
    """Fetch operator controls and apply them without blocking supervision."""

    if terminal is None or callback is None:
        return False
    activity = False
    try:
        events = callback() or []
    except Exception:
        events = []
    signal_map = {
        "interrupt": signal.SIGINT,
        "suspend": signal.SIGTSTP,
        "continue": signal.SIGCONT,
        "window_change": signal.SIGWINCH,
    }
    for event in events:
        action = str(event.get("action", "")).strip().lower()
        if action == "input":
            terminal.pending.extend(str(event.get("data", "")).encode("utf-8"))
            activity = True
        elif action == "eof":
            terminal.pending.extend(b"\x04")
            activity = True
        elif action == "resize":
            _set_terminal_size(
                terminal.descriptor,
                rows=int(event.get("rows", 40)),
                columns=int(event.get("columns", 120)),
            )
            try:
                os.killpg(pgid, signal.SIGWINCH)
            except ProcessLookupError:
                pass
            activity = True
        elif action == "signal":
            target = signal_map.get(str(event.get("signal", "")).strip().lower())
            if target is not None:
                try:
                    os.killpg(pgid, target)
                except ProcessLookupError:
                    pass
                activity = True

    while terminal.pending:
        try:
            written = os.write(terminal.descriptor, terminal.pending)
        except BlockingIOError:
            break
        except OSError:
            terminal.pending.clear()
            break
        if written <= 0:
            break
        del terminal.pending[:written]
        activity = True
    return activity

def _read_terminal_output(
    terminal: _InteractiveTerminal | None,
    output_callback: ProcessOutputCallback | None,
) -> bool:
    """Drain PTY echo or `/dev/tty` prompts without contaminating provider JSON."""

    if terminal is None or terminal.mode != "pty":
        return False
    observed = False
    while True:
        try:
            chunk = os.read(terminal.descriptor, 65536)
        except BlockingIOError:
            break
        except OSError:
            break
        if not chunk:
            break
        observed = True
        if output_callback is not None:
            try:
                output_callback("terminal", chunk.decode("utf-8", errors="replace"))
            except Exception:
                pass
        if len(chunk) < 65536:
            break
    return observed

def managed_run(
    args: list[str],
    *,
    cwd: str | Path | None = None,
    stdin: int | IO[Any] | None = subprocess.DEVNULL,
    input_data: str | bytes | None = None,
    capture_output: bool = True,
    text: bool = True,
    timeout: float | None = None,
    grace_period: float = 5.0,
    heartbeat_interval: float = 0.0,
    heartbeat_callback: ProcessHeartbeatCallback | None = None,
    output_classifier: ProcessOutputClassifier | None = None,
    output_callback: ProcessOutputCallback | None = None,
    terminal_callback: ProcessTerminalCallback | None = None,
    inactivity_timeout: float | None = None,
    output_silence_timeout: float | None = None,
    first_output_timeout: float | None = None,
    startup_grace_period: float = 5.0,
    max_output_bytes: int | None = None,
    capture_mode: str = "pipe",
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run *args* under process-group and optional PTY supervision.

    A configured terminal callback creates a real PTY for stdin while keeping
    stdout/stderr separately captured for deterministic adapter parsing. Browser
    input is consumed from the durable console queue; no GUI thread owns the
    subprocess file descriptors.
    """
    if capture_mode not in {"pipe", "file"}:
        raise ValueError(f"unsupported capture_mode: {capture_mode!r}")
    if max_output_bytes is not None and int(max_output_bytes) <= 0:
        raise ValueError("max_output_bytes must be a positive integer or None")
    if float(startup_grace_period) < 0:
        raise ValueError("startup_grace_period must be non-negative")
    if first_output_timeout is not None and float(first_output_timeout) <= 0:
        raise ValueError("first_output_timeout must be positive or None")
    if input_data is not None:
        if terminal_callback is not None:
            raise ValueError(
                "input_data cannot be combined with an interactive terminal; "
                "headless provider prompts must own stdin until EOF"
            )
        if stdin is not None and stdin != subprocess.DEVNULL:
            raise ValueError("input_data cannot be combined with an explicit stdin")
        payload = (
            input_data.encode("utf-8")
            if isinstance(input_data, str)
            else bytes(input_data)
        )
        # A seekable temporary file avoids both ARG_MAX and pipe backpressure:
        # the child receives a normal stdin stream and may read the prompt at
        # its own pace while the parent continues supervised stdout/stderr I/O.
        with tempfile.TemporaryFile(mode="w+b") as prompt_stream:
            prompt_stream.write(payload)
            prompt_stream.seek(0)
            return managed_run(
                args,
                cwd=cwd,
                stdin=prompt_stream,
                capture_output=capture_output,
                text=text,
                timeout=timeout,
                grace_period=grace_period,
                heartbeat_interval=heartbeat_interval,
                heartbeat_callback=heartbeat_callback,
                output_classifier=output_classifier,
                output_callback=output_callback,
                terminal_callback=None,
                inactivity_timeout=inactivity_timeout,
                output_silence_timeout=output_silence_timeout,
                first_output_timeout=first_output_timeout,
                startup_grace_period=startup_grace_period,
                max_output_bytes=max_output_bytes,
                capture_mode=capture_mode,
                environment=environment,
            )
    if capture_mode == "file" and capture_output:
        return _managed_run_file_capture(
            args,
            cwd=cwd,
            stdin=stdin,
            timeout=timeout,
            grace_period=grace_period,
            heartbeat_interval=heartbeat_interval,
            heartbeat_callback=heartbeat_callback,
            output_classifier=output_classifier,
            output_callback=output_callback,
            terminal_callback=terminal_callback,
            inactivity_timeout=inactivity_timeout,
            output_silence_timeout=output_silence_timeout,
            first_output_timeout=first_output_timeout,
            startup_grace_period=startup_grace_period,
            max_output_bytes=max_output_bytes,
            environment=environment,
        )

    live_supervision = bool(
        output_classifier
        or output_callback
        or terminal_callback
        or (heartbeat_callback is not None and heartbeat_interval > 0)
        or (inactivity_timeout is not None and inactivity_timeout > 0)
        or (output_silence_timeout is not None and output_silence_timeout > 0)
        or (first_output_timeout is not None and first_output_timeout > 0)
        or (max_output_bytes is not None and max_output_bytes > 0)
    )
    proc, terminal = _spawn_process(
        args,
        cwd=cwd,
        stdin=stdin,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        text=False if live_supervision and capture_output else text,
        bufsize=0 if live_supervision else -1,
        terminal_callback=terminal_callback,
        environment=environment,
    )
    pgid = os.getpgid(proc.pid)
    started = time.monotonic()

    if not live_supervision:
        try:
            stdout_value, stderr_value = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(pgid, grace_period=grace_period)
            proc.wait()
            raise
        return subprocess.CompletedProcess(
            args=args,
            returncode=proc.returncode or 0,
            stdout=_as_text(stdout_value),
            stderr=_as_text(stderr_value),
        )

    import selectors

    deadline = started + timeout if timeout is not None else None
    inactivity_deadline = (
        started + float(inactivity_timeout)
        if inactivity_timeout is not None and inactivity_timeout > 0
        else None
    )
    output_silence_deadline = (
        started
        + max(float(output_silence_timeout), float(startup_grace_period))
        if output_silence_timeout is not None and output_silence_timeout > 0
        else None
    )
    first_output_deadline = (
        started + float(first_output_timeout)
        if first_output_timeout is not None and first_output_timeout > 0
        else None
    )
    interval = max(0.0, float(heartbeat_interval))
    monitor_interval = (
        interval
        if interval > 0
        else (
            min(1.0, max(0.1, float(inactivity_timeout) / 4.0))
            if inactivity_timeout is not None and inactivity_timeout > 0
            else (0.1 if terminal is not None else float("inf"))
        )
    )
    next_heartbeat = (
        started + monitor_interval if monitor_interval != float("inf") else float("inf")
    )
    previous_sample: dict[str, Any] | None = None
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    captured_output_bytes = 0
    selector = selectors.DefaultSelector()
    streams: dict[int, tuple[str, Any]] = {}
    for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        if pipe is None:
            continue
        selector.register(pipe, selectors.EVENT_READ, data=name)
        streams[pipe.fileno()] = (name, pipe)

    termination_signal: ProcessTerminationSignal | None = None
    last_output_at = started
    last_activity_at = started
    root_exit_observed_at: float | None = None
    try:
        while True:
            now = time.monotonic()
            returncode = proc.poll()
            root_is_running = returncode is None
            process_group_is_running = root_is_running or process_group_exists(pgid)

            terminal_control_activity = _poll_terminal_controls(
                terminal, terminal_callback, pgid=pgid
            )
            terminal_output_activity = _read_terminal_output(
                terminal, output_callback
            )
            terminal_activity = terminal_control_activity or terminal_output_activity
            if terminal_activity:
                last_activity_at = now
                if inactivity_timeout is not None and inactivity_timeout > 0:
                    inactivity_deadline = now + float(inactivity_timeout)
            # PTY traffic may be local echo of operator input. Only captured
            # stdout/stderr proves provider progress for output watchdogs.

            if process_group_is_running:
                if deadline is not None and now >= deadline:
                    _kill_process_group(pgid, grace_period=grace_period)
                    proc.wait()
                    raise subprocess.TimeoutExpired(args, timeout)
                if first_output_deadline is not None and now >= first_output_deadline:
                    termination_signal = ProcessTerminationSignal(
                        category="first_output_timeout",
                        summary=(
                            "agent produced no stdout/stderr during startup for "
                            f"{float(first_output_timeout):.0f}s"
                        ),
                        persistent=False,
                    )
                    break
                if inactivity_deadline is not None and now >= inactivity_deadline:
                    termination_signal = ProcessTerminationSignal(
                        category="inactive",
                        summary=(
                            f"agent produced no output or CPU activity for "
                            f"{float(inactivity_timeout):.0f}s"
                        ),
                        persistent=False,
                    )
                    break
                if output_silence_deadline is not None and now >= output_silence_deadline:
                    termination_signal = ProcessTerminationSignal(
                        category="output_silence",
                        summary=(
                            f"agent produced no stdout/stderr for "
                            f"{float(output_silence_timeout):.0f}s"
                        ),
                        persistent=True,
                    )
                    break
            else:
                if root_exit_observed_at is None:
                    root_exit_observed_at = now
                if not streams:
                    break
                if now - root_exit_observed_at >= _POST_EXIT_PIPE_DRAIN_SECONDS:
                    break

            wake_at = min(
                deadline if process_group_is_running and deadline is not None else float("inf"),
                inactivity_deadline
                if process_group_is_running and inactivity_deadline is not None
                else float("inf"),
                output_silence_deadline
                if process_group_is_running and output_silence_deadline is not None
                else float("inf"),
                first_output_deadline
                if process_group_is_running and first_output_deadline is not None
                else float("inf"),
                next_heartbeat if process_group_is_running else float("inf"),
                (
                    root_exit_observed_at + _POST_EXIT_PIPE_DRAIN_SECONDS
                    if root_exit_observed_at is not None
                    else float("inf")
                ),
                now + (0.1 if terminal is not None else 0.25),
            )
            selected = selector.select(max(0.001, wake_at - now))
            activity_observed = terminal_activity
            for key, _ in selected:
                stream_name = str(key.data)
                pipe = key.fileobj
                try:
                    chunk = os.read(pipe.fileno(), 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    _unregister_stream(selector, streams, pipe)
                    continue
                activity_observed = True
                first_output_deadline = None
                last_output_at = time.monotonic()
                last_activity_at = last_output_at
                if output_silence_timeout is not None and output_silence_timeout > 0:
                    output_silence_deadline = last_output_at + float(output_silence_timeout)
                target = stdout_buffer if stream_name == "stdout" else stderr_buffer
                remaining = (
                    int(max_output_bytes) - captured_output_bytes
                    if max_output_bytes is not None
                    else len(chunk)
                )
                retained = chunk[: max(0, remaining)]
                if retained:
                    target.extend(retained)
                    captured_output_bytes += len(retained)
                decoded = chunk.decode("utf-8", errors="replace")
                if output_callback is not None:
                    try:
                        output_callback(stream_name, decoded)
                    except Exception:
                        pass
                if output_classifier is not None:
                    signal_value = output_classifier(stream_name, decoded)
                    if signal_value is not None and signal_value.category:
                        termination_signal = signal_value
                        break
                if (
                    max_output_bytes is not None
                    and captured_output_bytes >= int(max_output_bytes)
                    and len(chunk) > len(retained)
                ):
                    termination_signal = ProcessTerminationSignal(
                        category="output_limit",
                        summary=(
                            "agent output exceeded the configured capture limit "
                            f"of {int(max_output_bytes)} bytes"
                        ),
                        stream=stream_name,
                        excerpt=decoded[-2000:],
                        persistent=False,
                    )
                    break
            if termination_signal is not None:
                break

            now = time.monotonic()
            root_is_running = proc.poll() is None
            process_group_is_running = root_is_running or process_group_exists(pgid)
            if not process_group_is_running and root_exit_observed_at is None:
                root_exit_observed_at = now
            elif process_group_is_running:
                root_exit_observed_at = None
            if process_group_is_running and now >= next_heartbeat:
                snapshot, previous_sample = _process_group_heartbeat(
                    proc=proc, pgid=pgid, started=started, previous=previous_sample
                )
                snapshot["terminal_mode"] = terminal.mode if terminal else "none"
                process_activity = bool(snapshot.get("io_active") or snapshot.get("cpu_active"))
                activity_observed = activity_observed or process_activity
                if process_activity:
                    last_activity_at = now
                snapshot["last_output_age_seconds"] = max(0.0, now - last_output_at)
                if heartbeat_callback is not None and interval > 0:
                    try:
                        heartbeat_callback(snapshot)
                    except Exception:
                        pass
                next_heartbeat = (
                    now + monitor_interval
                    if monitor_interval != float("inf")
                    else float("inf")
                )

            if activity_observed and inactivity_timeout is not None and inactivity_timeout > 0:
                inactivity_deadline = last_activity_at + float(inactivity_timeout)

        if termination_signal is not None:
            _kill_process_group(pgid, grace_period=min(grace_period, 1.0))
            try:
                remainder_out, remainder_err = proc.communicate(timeout=grace_period + 1)
            except subprocess.TimeoutExpired:
                _kill_process_group(pgid, grace_period=0)
                remainder_out, remainder_err = proc.communicate()
            remainder_stdout = _as_bytes(remainder_out)
            remainder_stderr = _as_bytes(remainder_err)
            if max_output_bytes is None:
                stdout_buffer.extend(remainder_stdout)
                stderr_buffer.extend(remainder_stderr)
            else:
                remaining = max(
                    0,
                    int(max_output_bytes)
                    - len(stdout_buffer)
                    - len(stderr_buffer),
                )
                stdout_part = remainder_stdout[:remaining]
                stdout_buffer.extend(stdout_part)
                remaining -= len(stdout_part)
                stderr_buffer.extend(remainder_stderr[:remaining])
            raise ManagedProcessTerminated(
                termination_signal,
                args=args,
                stdout=stdout_buffer.decode("utf-8", errors="replace"),
                stderr=stderr_buffer.decode("utf-8", errors="replace"),
            )

        if proc.poll() is None:
            proc.wait()
        return subprocess.CompletedProcess(
            args=args,
            returncode=proc.returncode or 0,
            stdout=stdout_buffer.decode("utf-8", errors="replace"),
            stderr=stderr_buffer.decode("utf-8", errors="replace"),
        )
    except KeyboardInterrupt:
        _kill_process_group(pgid, grace_period=grace_period)
        proc.wait()
        raise
    finally:
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                _unregister_stream(selector, streams, pipe)
        selector.close()
        if terminal is not None:
            terminal.close()

def _managed_run_file_capture(
    args: list[str],
    *,
    cwd: str | Path | None,
    stdin: int | IO[Any] | None,
    timeout: float | None,
    grace_period: float,
    heartbeat_interval: float,
    heartbeat_callback: ProcessHeartbeatCallback | None,
    output_classifier: ProcessOutputClassifier | None,
    output_callback: ProcessOutputCallback | None,
    terminal_callback: ProcessTerminalCallback | None,
    inactivity_timeout: float | None,
    output_silence_timeout: float | None,
    first_output_timeout: float | None,
    startup_grace_period: float,
    max_output_bytes: int | None,
    environment: Mapping[str, str] | None,
) -> subprocess.CompletedProcess[str]:
    """Supervise a command whose stdout/stderr are redirected to files."""
    with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
        mode="w+b"
    ) as stderr_file:
        proc, terminal = _spawn_process(
            args,
            cwd=cwd,
            stdin=stdin,
            stdout=stdout_file,
            stderr=stderr_file,
            text=False,
            bufsize=0,
            terminal_callback=terminal_callback,
            environment=environment,
        )
        pgid = os.getpgid(proc.pid)
        started = time.monotonic()
        deadline = started + timeout if timeout is not None else None
        interval = max(0.0, float(heartbeat_interval))
        monitor_interval = (
            interval
            if interval > 0
            else (
                min(1.0, max(0.1, float(inactivity_timeout) / 4.0))
                if inactivity_timeout is not None and inactivity_timeout > 0
                else 0.1
            )
        )
        next_heartbeat = started + monitor_interval
        inactivity_deadline = (
            started + float(inactivity_timeout)
            if inactivity_timeout is not None and inactivity_timeout > 0
            else None
        )
        output_silence_deadline = (
            started
            + max(float(output_silence_timeout), float(startup_grace_period))
            if output_silence_timeout is not None and output_silence_timeout > 0
            else None
        )
        first_output_deadline = (
            started + float(first_output_timeout)
            if first_output_timeout is not None and first_output_timeout > 0
            else None
        )
        previous_sample: dict[str, Any] | None = None
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        stdout_offset = 0
        stderr_offset = 0
        last_output_at = started
        last_activity_at = started
        termination_signal: ProcessTerminationSignal | None = None
        captured_output_bytes = 0

        def collect_growth() -> bool:
            nonlocal stdout_offset, stderr_offset
            nonlocal last_output_at, last_activity_at, termination_signal
            nonlocal output_silence_deadline, first_output_deadline
            nonlocal captured_output_bytes
            observed = False
            for stream_name, handle, offset, target in (
                ("stdout", stdout_file, stdout_offset, stdout_buffer),
                ("stderr", stderr_file, stderr_offset, stderr_buffer),
            ):
                chunk = _read_file_growth(handle.fileno(), offset)
                if stream_name == "stdout":
                    stdout_offset += len(chunk)
                else:
                    stderr_offset += len(chunk)
                if not chunk:
                    continue
                observed = True
                first_output_deadline = None
                now_value = time.monotonic()
                last_output_at = now_value
                last_activity_at = now_value
                if output_silence_timeout is not None and output_silence_timeout > 0:
                    output_silence_deadline = now_value + float(output_silence_timeout)
                remaining = (
                    int(max_output_bytes) - captured_output_bytes
                    if max_output_bytes is not None
                    else len(chunk)
                )
                retained = chunk[: max(0, remaining)]
                if retained:
                    target.extend(retained)
                    captured_output_bytes += len(retained)
                decoded = chunk.decode("utf-8", errors="replace")
                if output_callback is not None:
                    try:
                        output_callback(stream_name, decoded)
                    except Exception:
                        pass
                if output_classifier is not None:
                    signal_value = output_classifier(stream_name, decoded)
                    if signal_value is not None and signal_value.category:
                        termination_signal = signal_value
                        break
                if (
                    max_output_bytes is not None
                    and captured_output_bytes >= int(max_output_bytes)
                    and len(chunk) > len(retained)
                ):
                    termination_signal = ProcessTerminationSignal(
                        category="output_limit",
                        summary=(
                            "agent output exceeded the configured capture limit "
                            f"of {int(max_output_bytes)} bytes"
                        ),
                        stream=stream_name,
                        excerpt=decoded[-2000:],
                        persistent=False,
                    )
                    break
            return observed

        try:
            while True:
                now = time.monotonic()
                root_is_running = proc.poll() is None
                process_group_is_running = root_is_running or process_group_exists(pgid)
                terminal_control_activity = _poll_terminal_controls(
                    terminal, terminal_callback, pgid=pgid
                )
                terminal_output_activity = _read_terminal_output(terminal, output_callback)
                terminal_activity = terminal_control_activity or terminal_output_activity
                if terminal_activity:
                    last_activity_at = now
                    if inactivity_timeout is not None and inactivity_timeout > 0:
                        inactivity_deadline = now + float(inactivity_timeout)
                # PTY traffic may be local echo of operator input. Only file
                # capture growth proves provider stdout/stderr progress.

                collect_growth()
                if termination_signal is not None:
                    break

                if process_group_is_running:
                    if deadline is not None and now >= deadline:
                        _kill_process_group(pgid, grace_period=grace_period)
                        proc.wait()
                        raise subprocess.TimeoutExpired(args, timeout)
                    if first_output_deadline is not None and now >= first_output_deadline:
                        termination_signal = ProcessTerminationSignal(
                            category="first_output_timeout",
                            summary=(
                                "agent produced no stdout/stderr during startup for "
                                f"{float(first_output_timeout):.0f}s"
                            ),
                            persistent=False,
                        )
                        break
                    if inactivity_deadline is not None and now >= inactivity_deadline:
                        termination_signal = ProcessTerminationSignal(
                            category="inactive",
                            summary=(
                                f"agent produced no output or CPU activity for "
                                f"{float(inactivity_timeout):.0f}s"
                            ),
                            persistent=False,
                        )
                        break
                    if output_silence_deadline is not None and now >= output_silence_deadline:
                        termination_signal = ProcessTerminationSignal(
                            category="output_silence",
                            summary=(
                                f"agent produced no stdout/stderr for "
                                f"{float(output_silence_timeout):.0f}s"
                            ),
                            persistent=True,
                        )
                        break
                else:
                    collect_growth()
                    break

                now = time.monotonic()
                if now >= next_heartbeat:
                    snapshot, previous_sample = _process_group_heartbeat(
                        proc=proc, pgid=pgid, started=started, previous=previous_sample
                    )
                    snapshot["terminal_mode"] = terminal.mode if terminal else "none"
                    process_activity = bool(snapshot.get("io_active") or snapshot.get("cpu_active"))
                    if process_activity:
                        last_activity_at = now
                    snapshot["last_output_age_seconds"] = max(0.0, now - last_output_at)
                    if heartbeat_callback is not None and interval > 0:
                        try:
                            heartbeat_callback(snapshot)
                        except Exception:
                            pass
                    next_heartbeat = now + monitor_interval
                    if (
                        inactivity_timeout is not None
                        and inactivity_timeout > 0
                        and process_activity
                    ):
                        inactivity_deadline = last_activity_at + float(inactivity_timeout)

                wake_at = min(
                    deadline if deadline is not None else float("inf"),
                    inactivity_deadline if inactivity_deadline is not None else float("inf"),
                    output_silence_deadline
                    if output_silence_deadline is not None
                    else float("inf"),
                    first_output_deadline
                    if first_output_deadline is not None
                    else float("inf"),
                    next_heartbeat,
                    now + 0.1,
                )
                time.sleep(max(0.001, wake_at - time.monotonic()))

            if termination_signal is not None:
                _kill_process_group(pgid, grace_period=min(grace_period, 1.0))
                try:
                    proc.wait(timeout=grace_period + 1)
                except subprocess.TimeoutExpired:
                    _kill_process_group(pgid, grace_period=0)
                    proc.wait()
                collect_growth()
                raise ManagedProcessTerminated(
                    termination_signal,
                    args=args,
                    stdout=stdout_buffer.decode("utf-8", errors="replace"),
                    stderr=stderr_buffer.decode("utf-8", errors="replace"),
                )

            if proc.poll() is None:
                proc.wait()
            collect_growth()
            return subprocess.CompletedProcess(
                args=args,
                returncode=proc.returncode or 0,
                stdout=stdout_buffer.decode("utf-8", errors="replace"),
                stderr=stderr_buffer.decode("utf-8", errors="replace"),
            )
        except KeyboardInterrupt:
            _kill_process_group(pgid, grace_period=grace_period)
            proc.wait()
            raise
        finally:
            if terminal is not None:
                terminal.close()


def _read_file_growth(file_descriptor: int, offset: int) -> bytes:
    """Read bytes appended after *offset* without disturbing file position."""
    try:
        size = os.fstat(file_descriptor).st_size
    except OSError:
        return b""
    if size <= offset:
        return b""
    try:
        return os.pread(file_descriptor, size - offset, offset)
    except OSError:
        return b""

def _unregister_stream(selector, streams: dict[int, tuple[str, Any]], pipe: Any) -> None:
    """Stop supervising *pipe* and close it without masking process results."""
    try:
        file_descriptor = pipe.fileno()
    except (AttributeError, OSError, ValueError):
        file_descriptor = None
    if file_descriptor is not None:
        try:
            selector.unregister(pipe)
        except Exception:
            pass
        streams.pop(file_descriptor, None)
    try:
        pipe.close()
    except Exception:
        pass


def _as_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8", errors="replace")


def process_group_exists(pgid: int) -> bool:
    """Return whether any process still belongs to *pgid*.

    A CLI launcher may exit before a same-process-group worker has finished.
    Treat that worker as part of the supervised command so its output is not
    discarded by the bounded post-exit pipe drain.
    """
    linux_result = _linux_process_group_has_live_members(pgid)
    if linux_result is not None:
        return linux_result
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _linux_process_group_has_live_members(pgid: int) -> bool | None:
    """Return non-zombie membership on Linux, or ``None`` off Linux.

    ``killpg(pgid, 0)`` reports process groups containing only unreaped zombies
    as alive.  Treating those groups as active makes a completed launcher wait
    until its absolute timeout even after every output pipe reached EOF.
    """

    proc_root = Path("/proc")
    if os.name != "posix" or not proc_root.is_dir():
        return None
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return None

    incomplete = False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat_text = (entry / "stat").read_text(encoding="utf-8")
            _, state, process_group, _ = _parse_proc_stat(stat_text)
        except PermissionError:
            incomplete = True
            continue
        except (OSError, ValueError, IndexError):
            # Processes can disappear between directory enumeration and stat
            # reads. Those races do not make the complete scan inconclusive.
            continue
        if process_group == pgid and state not in {"Z", "X"}:
            return True
    return None if incomplete else False


def kill_process_group(
    pgid: int,
    grace_period: float = 5.0,
) -> None:
    """Send SIGTERM to process group *pgid*, then SIGKILL after grace."""
    _kill_process_group(pgid, grace_period=grace_period)


def _process_group_heartbeat(
    *,
    proc: subprocess.Popen[Any],
    pgid: int,
    started: float,
    previous: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    elapsed = max(0.0, time.monotonic() - started)
    current = _sample_linux_process_group(pgid, root_pid=proc.pid)
    if current is None:
        current = {
            "process_count": 1 if proc.poll() is None else 0,
            "cpu_ticks": 0,
            "read_chars": 0,
            "write_chars": 0,
            "read_bytes": 0,
            "write_bytes": 0,
            "root_state_code": "R" if proc.poll() is None else "X",
        }

    prior = previous or current
    read_chars_delta = max(0, current["read_chars"] - prior["read_chars"])
    write_chars_delta = max(0, current["write_chars"] - prior["write_chars"])
    read_bytes_delta = max(0, current["read_bytes"] - prior["read_bytes"])
    write_bytes_delta = max(0, current["write_bytes"] - prior["write_bytes"])
    cpu_ticks_delta = max(0, current["cpu_ticks"] - prior["cpu_ticks"])
    process_count_delta = current["process_count"] - prior["process_count"]
    stdio_active = read_chars_delta > 0 or write_chars_delta > 0
    disk_io_active = read_bytes_delta > 0 or write_bytes_delta > 0
    io_active = stdio_active or disk_io_active

    state_code = str(current.get("root_state_code", "?"))
    snapshot: dict[str, Any] = {
        "pid": proc.pid,
        "pgid": pgid,
        "elapsed_seconds": elapsed,
        "process_state": _STATE_LABELS.get(state_code, "unknown"),
        "process_state_code": state_code,
        "process_count": current["process_count"],
        "process_count_delta": process_count_delta,
        "io_active": io_active,
        "stdio_active": stdio_active,
        "disk_io_active": disk_io_active,
        "cpu_active": cpu_ticks_delta > 0,
        "cpu_ticks_delta": cpu_ticks_delta,
        "read_chars_delta": read_chars_delta,
        "write_chars_delta": write_chars_delta,
        "read_bytes_delta": read_bytes_delta,
        "write_bytes_delta": write_bytes_delta,
    }
    return snapshot, current


def _sample_linux_process_group(
    pgid: int, *, root_pid: int
) -> dict[str, Any] | None:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return None

    totals = {
        "process_count": 0,
        "cpu_ticks": 0,
        "read_chars": 0,
        "write_chars": 0,
        "read_bytes": 0,
        "write_bytes": 0,
        "root_state_code": ord("?"),
    }
    found = False
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat_text = (entry / "stat").read_text(encoding="utf-8")
            pid, state, process_group, cpu_ticks = _parse_proc_stat(stat_text)
        except (OSError, ValueError, IndexError):
            continue
        if process_group != pgid:
            continue
        found = True
        totals["process_count"] += 1
        totals["cpu_ticks"] += cpu_ticks
        if pid == root_pid:
            totals["root_state_code"] = ord(state)
        try:
            io_values = _parse_proc_io((entry / "io").read_text(encoding="utf-8"))
        except OSError:
            io_values = {}
        for key in ("read_chars", "write_chars", "read_bytes", "write_bytes"):
            totals[key] += int(io_values.get(key, 0))

    if not found:
        return None
    return {
        **totals,
        "root_state_code": chr(totals["root_state_code"]),
    }


def _parse_proc_stat(text: str) -> tuple[int, str, int, int]:
    # ``comm`` may contain spaces or parentheses; split only after its final ')'.
    open_paren = text.find("(")
    close_paren = text.rfind(")")
    if open_paren <= 0 or close_paren <= open_paren:
        raise ValueError("invalid /proc stat")
    pid = int(text[:open_paren].strip())
    fields = text[close_paren + 2 :].split()
    state = fields[0]
    process_group = int(fields[2])
    user_ticks = int(fields[11])
    system_ticks = int(fields[12])
    return pid, state, process_group, user_ticks + system_ticks


def _parse_proc_io(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    aliases = {
        "rchar": "read_chars",
        "wchar": "write_chars",
        "read_bytes": "read_bytes",
        "write_bytes": "write_bytes",
    }
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        target = aliases.get(key.strip())
        if not separator or target is None:
            continue
        try:
            result[target] = int(value.strip())
        except ValueError:
            continue
    return result


def _as_text(value: str | bytes | None) -> str:
    if isinstance(value, str):
        return value
    return value.decode(errors="replace") if value else ""


def _kill_process_group(pgid: int, grace_period: float) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    if grace_period > 0:
        deadline = time.monotonic() + grace_period
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
