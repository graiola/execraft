"""Standalone interactive agent terminals owned by the local dashboard.

Orchestrated attempts already expose a supervised PTY while they are running.
This module covers the complementary operator workflow: explicitly starting a
configured provider CLI when the orchestrator is idle and keeping that terminal
available until the operator exits or stops it.

The manager never invokes a shell.  It execs the configured provider binary with
adapter-specific interactive flags, uses the task workspace as cwd, and routes
all keyboard controls through :class:`AgentConsoleStore` so the existing bounds,
authorization, retention, and audit trail remain authoritative.
"""

from __future__ import annotations

import codecs
import errno
import os
import select
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from execraft.agents import AgentProviderConfig
from execraft.orchestrate.agent_console import AgentConsoleStore


class ManualAgentConsoleError(RuntimeError):
    """Raised when a standalone agent terminal cannot be started or controlled."""


CommandBuilder = Callable[[AgentProviderConfig, Path], list[str]]
ProviderLoader = Callable[[], Iterable[AgentProviderConfig]]
WorkdirLoader = Callable[[], Path]
EnvironmentLoader = Callable[[], dict[str, str]]

_SIGNAL_MAP = {
    "interrupt": signal.SIGINT,
    "suspend": signal.SIGTSTP,
    "continue": signal.SIGCONT,
    "window_change": signal.SIGWINCH,
}


def build_interactive_agent_command(
    provider: AgentProviderConfig, workdir: Path
) -> list[str]:
    """Build a no-shell interactive command for a configured provider.

    All supported CLIs have an interactive mode when invoked without their
    headless/print subcommand.  The process cwd carries the workspace selection;
    only stable model and permission flags are forwarded.  Keeping this builder
    small also makes provider-specific overrides straightforward in the future.
    """

    del workdir  # cwd is supplied directly to Popen.
    args = [provider.binary]
    adapter = provider.adapter
    if provider.model:
        args += ["--model" if adapter != "codex" else "-m", provider.model]
    if adapter in {"claude", "claude-code"} and provider.permission_mode:
        args += ["--permission-mode", provider.permission_mode]
    elif adapter == "codex" and provider.sandbox:
        args += ["--sandbox", provider.sandbox]
    elif adapter in {"antigravity", "antigravity-cli"}:
        if provider.sandbox_enabled:
            args.append("--sandbox")
        if provider.dangerously_skip_permissions:
            args.append("--dangerously-skip-permissions")
    return args


@dataclass
class _RunningConsole:
    agent_id: str
    session_id: str
    payload: dict[str, Any]
    process: subprocess.Popen[bytes]
    master_fd: int
    thread: threading.Thread
    started_monotonic: float
    last_output_monotonic: float
    last_heartbeat_monotonic: float
    requested_stop: bool = False
    control_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class ManualAgentConsoleManager:
    """Own standalone provider PTYs for one dashboard/task instance."""

    def __init__(
        self,
        *,
        store: AgentConsoleStore,
        provider_loader: ProviderLoader,
        workdir_loader: WorkdirLoader,
        environment_loader: EnvironmentLoader,
        command_builder: CommandBuilder = build_interactive_agent_command,
    ) -> None:
        self._store = store
        self._provider_loader = provider_loader
        self._workdir_loader = workdir_loader
        self._environment_loader = environment_loader
        self._command_builder = command_builder
        self._lock = threading.RLock()
        self._running: dict[str, _RunningConsole] = {}

    def any_running(self) -> bool:
        with self._lock:
            # Keep the workspace reserved until the supervisor has persisted the
            # final metadata and removed the session from the active registry.
            return bool(self._running)

    def status(self, agent_id: str) -> dict[str, Any]:
        agent_id = str(agent_id).strip()
        with self._lock:
            item = self._running.get(agent_id)
            if item is None:
                return {"running": False, "session_id": "", "pid": None}
            code = item.process.poll()
            return {
                # ``running`` means the standalone session still owns the
                # workspace, including the very short finalization window after
                # the child exits. This prevents a new session from superseding
                # its AgentConsoleStore key before metadata is finalized.
                "running": True,
                "process_running": code is None,
                "session_id": item.session_id,
                "pid": item.process.pid if code is None else None,
                "exit_code": code,
            }

    def start(self, agent_id: str) -> dict[str, Any]:
        if os.name != "posix":
            raise ManualAgentConsoleError(
                "standalone interactive agent terminals currently require POSIX PTY support"
            )
        if not self._store.terminal_policy.enabled:
            raise ManualAgentConsoleError(
                "interactive agent console is disabled by project policy"
            )
        agent_id = str(agent_id).strip()
        providers = {item.provider_id: item for item in self._provider_loader()}
        provider = providers.get(agent_id)
        if provider is None:
            raise ManualAgentConsoleError(f"unknown configured provider: {agent_id}")
        if not provider.enabled:
            raise ManualAgentConsoleError(f"configured provider is disabled: {agent_id}")

        workdir = self._workdir_loader().expanduser().resolve()
        if not workdir.is_dir():
            raise ManualAgentConsoleError(
                f"task workspace does not exist or is not a directory: {workdir}"
            )
        command = self._command_builder(provider, workdir)
        if not command or not str(command[0]).strip():
            raise ManualAgentConsoleError(
                f"interactive command is empty for provider {agent_id}"
            )
        binary = str(command[0])
        if not Path(binary).is_file() and shutil.which(binary) is None:
            raise ManualAgentConsoleError(
                f"provider binary is not available for interactive console: {binary}"
            )

        with self._lock:
            current = self._running.get(agent_id)
            if current is not None:
                raise ManualAgentConsoleError(
                    f"provider already has a standalone console: {agent_id}"
                )

            payload = {
                "package_id": "manual",
                "stage": "interactive",
                "capability": "interactive",
                "agent_id": agent_id,
                "adapter": provider.adapter,
                "model": provider.model,
                "attempt": 1,
                "origin": "manual",
                "interaction_mode": "terminal",
                "interactive_pty": True,
                "working_directory": str(workdir),
                "command": list(command),
            }
            metadata = self._store.start(payload)
            if not metadata:
                raise ManualAgentConsoleError(
                    f"could not create interactive console session for {agent_id}"
                )
            session_id = str(metadata["session_id"])
            master_fd, slave_fd = os.openpty()
            self._set_terminal_size(
                master_fd,
                rows=self._store.terminal_policy.default_rows,
                columns=self._store.terminal_policy.default_columns,
            )
            try:
                os.set_blocking(master_fd, False)
            except (AttributeError, OSError):
                pass
            launcher = [
                sys.executable,
                str(Path(__file__).parents[1] / "process" / "pty_launcher.py"),
                "--",
                *command,
            ]
            env = self._environment_loader()
            env.setdefault("TERM", "xterm-256color")
            env["LINES"] = str(self._store.terminal_policy.default_rows)
            env["COLUMNS"] = str(self._store.terminal_policy.default_columns)
            env["EXECRAFT_MANUAL_AGENT_CONSOLE"] = "1"
            try:
                process = subprocess.Popen(
                    launcher,
                    cwd=str(workdir),
                    env=env,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    text=False,
                    bufsize=0,
                    start_new_session=True,
                )
            except Exception as exc:
                os.close(master_fd)
                self._store.finish(
                    {
                        **payload,
                        "status": "failed",
                        "detail": f"could not start interactive provider: {exc}",
                    }
                )
                raise ManualAgentConsoleError(str(exc)) from exc
            finally:
                os.close(slave_fd)

            now = time.monotonic()
            placeholder = threading.Thread()
            running = _RunningConsole(
                agent_id=agent_id,
                session_id=session_id,
                payload=payload,
                process=process,
                master_fd=master_fd,
                thread=placeholder,
                started_monotonic=now,
                last_output_monotonic=now,
                last_heartbeat_monotonic=0.0,
            )
            thread = threading.Thread(
                target=self._supervise,
                args=(running,),
                name=f"execraft-manual-console-{agent_id}",
                daemon=True,
            )
            running.thread = thread
            self._running[agent_id] = running
            self._store.append(
                {
                    **payload,
                    "stream": "system",
                    "text": (
                        f"Standalone agent console started in {workdir}.\n"
                        f"Command: {' '.join(command)}\n"
                    ),
                }
            )
            self._heartbeat(running, force=True)
            thread.start()
            return {
                "started": True,
                "agent_id": agent_id,
                "session_id": session_id,
                "pid": process.pid,
                "working_directory": str(workdir),
                "command": command,
            }

    def stop(
        self,
        *,
        agent_id: str = "",
        session_id: str = "",
        timeout_seconds: float = 3.0,
    ) -> dict[str, Any]:
        with self._lock:
            running = self._find_running_locked(agent_id=agent_id, session_id=session_id)
            if running is None:
                raise ManualAgentConsoleError("standalone agent console is not running")
            running.requested_stop = True
            process = running.process
        self._signal_process_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=max(0.1, float(timeout_seconds)))
        except subprocess.TimeoutExpired:
            self._signal_process_group(process, signal.SIGKILL)
            process.wait(timeout=3)
        if running.thread is not threading.current_thread():
            running.thread.join(timeout=3)
        return {
            "stopped": True,
            "agent_id": running.agent_id,
            "session_id": running.session_id,
            "exit_code": process.returncode,
        }

    def close(self) -> None:
        with self._lock:
            sessions = list(self._running.values())
        for item in sessions:
            if item.process.poll() is None:
                try:
                    self.stop(agent_id=item.agent_id, timeout_seconds=1.0)
                except (ManualAgentConsoleError, OSError, subprocess.SubprocessError):
                    pass

    def _supervise(self, running: _RunningConsole) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        process = running.process
        try:
            while process.poll() is None:
                # Apply queued controls both before and after reading output. This
                # keeps raw-mode TUIs responsive even when they are silent or
                # continuously redrawing the screen.
                self._apply_controls(running)
                self._read_available(running, decoder, timeout=0.025)
                self._apply_controls(running)
                self._heartbeat(running)
            drain_deadline = time.monotonic() + 0.35
            while time.monotonic() < drain_deadline:
                if not self._read_available(running, decoder, timeout=0.03):
                    break
            tail = decoder.decode(b"", final=True)
            if tail:
                self._append_terminal_text(running, tail)
        finally:
            try:
                os.close(running.master_fd)
            except OSError:
                pass
            code = process.poll()
            if code is None:
                self._signal_process_group(process, signal.SIGKILL)
                code = process.wait(timeout=3)
            status = "stopped" if running.requested_stop else (
                "completed" if code == 0 else "failed"
            )
            detail = (
                "stopped by operator"
                if running.requested_stop
                else f"interactive provider exited with code {code}"
            )
            duration = max(0.0, time.monotonic() - running.started_monotonic)
            self._store.finish(
                {
                    **running.payload,
                    "status": status,
                    "detail": detail,
                    "duration_seconds": duration,
                }
            )
            with self._lock:
                current = self._running.get(running.agent_id)
                if current is running:
                    self._running.pop(running.agent_id, None)

    def _read_available(
        self,
        running: _RunningConsole,
        decoder: codecs.IncrementalDecoder,
        *,
        timeout: float,
    ) -> bool:
        try:
            readable, _, _ = select.select([running.master_fd], [], [], timeout)
        except (OSError, ValueError):
            return False
        if not readable:
            return False
        observed = False
        while True:
            try:
                chunk = os.read(running.master_fd, 65_536)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF}:
                    break
                raise
            if not chunk:
                break
            observed = True
            running.last_output_monotonic = time.monotonic()
            text = decoder.decode(chunk)
            if text:
                self._append_terminal_text(running, text)
            if len(chunk) < 65_536:
                break
        return observed

    def _append_terminal_text(self, running: _RunningConsole, text: str) -> None:
        """Forward raw PTY output to the shared screen/transcript store."""

        self._store.append(
            {**running.payload, "stream": "terminal", "text": text}
        )

    def dispatch_pending(self, session_id: str) -> bool:
        """Deliver queued controls immediately for a dashboard-owned session.

        The durable control queue remains authoritative and audited. This method
        only removes the avoidable polling delay when the GUI server and PTY
        owner live in the same process. Orchestrated sessions continue to be
        consumed by their own supervisor.
        """

        with self._lock:
            running = self._find_running_locked(agent_id="", session_id=session_id)
        if running is None or running.process.poll() is not None:
            return False
        self._apply_controls(running)
        return True

    def _apply_controls(self, running: _RunningConsole) -> None:
        # The GUI request thread may request immediate delivery while the PTY
        # supervisor is polling. Serialize consumption and writes so escape
        # sequences preserve the exact browser order.
        with running.control_lock:
            for event in self._store.consume_control_events(running.payload):
                action = str(event.get("action", "")).strip().lower()
                if action == "input":
                    self._write_master(
                        running.master_fd, str(event.get("data", "")).encode()
                    )
                elif action == "eof":
                    self._write_master(running.master_fd, b"\x04")
                elif action == "resize":
                    rows = int(
                        event.get("rows", self._store.terminal_policy.default_rows)
                    )
                    columns = int(
                        event.get("columns", self._store.terminal_policy.default_columns)
                    )
                    self._store.resize_terminal_screen(
                        running.payload, rows=rows, columns=columns
                    )
                    self._set_terminal_size(
                        running.master_fd, rows=rows, columns=columns
                    )
                    self._signal_process_group(running.process, signal.SIGWINCH)
                elif action == "signal":
                    target = _SIGNAL_MAP.get(
                        str(event.get("signal", "")).strip().lower()
                    )
                    if target is not None:
                        self._signal_process_group(running.process, target)

    def _heartbeat(self, running: _RunningConsole, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - running.last_heartbeat_monotonic < 0.25:
            return
        running.last_heartbeat_monotonic = now
        self._store.flush_terminal_screen(running.payload)
        self._store.heartbeat(
            {
                **running.payload,
                "pid": running.process.pid,
                "pgid": running.process.pid,
                "process_state": "running" if running.process.poll() is None else "exited",
                "process_count": 1,
                "elapsed_seconds": max(0.0, now - running.started_monotonic),
                "last_output_age_seconds": max(0.0, now - running.last_output_monotonic),
                "terminal_mode": "pty",
            }
        )

    @staticmethod
    def _write_master(descriptor: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                written = os.write(descriptor, view)
            except BlockingIOError:
                time.sleep(0.01)
                continue
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF}:
                    return
                raise
            view = view[written:]

    @staticmethod
    def _set_terminal_size(descriptor: int, *, rows: int, columns: int) -> None:
        try:
            import fcntl
            import termios

            packed = struct.pack("HHHH", int(rows), int(columns), 0, 0)
            fcntl.ioctl(descriptor, termios.TIOCSWINSZ, packed)
        except (ImportError, OSError, ValueError):
            pass

    @staticmethod
    def _signal_process_group(process: subprocess.Popen[bytes], target: signal.Signals) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, target)
        except ProcessLookupError:
            pass

    def _find_running_locked(
        self, *, agent_id: str, session_id: str
    ) -> _RunningConsole | None:
        normalized_agent = str(agent_id).strip()
        normalized_session = str(session_id).strip()
        if normalized_agent:
            item = self._running.get(normalized_agent)
            if item is not None and (
                not normalized_session or item.session_id == normalized_session
            ):
                return item
        if normalized_session:
            return next(
                (
                    item
                    for item in self._running.values()
                    if item.session_id == normalized_session
                ),
                None,
            )
        return None
