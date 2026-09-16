from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from execraft.agents import AgentProviderConfig
from execraft.gui.manual_agent_console import (
    ManualAgentConsoleError,
    ManualAgentConsoleManager,
)
from execraft.orchestrate.agent_console import (
    AgentConsoleStore,
    InteractiveTerminalPolicy,
)
from execraft.orchestrate.scheduler import AgentCapability


def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError("condition was not reached before timeout")


def _provider() -> AgentProviderConfig:
    return AgentProviderConfig(
        name="fake",
        adapter="codex",
        enabled=True,
        provider_id="fake-agent",
        binary=sys.executable,
        capabilities=frozenset({AgentCapability.IMPLEMENT}),
    )


@pytest.mark.skipif(os.name != "posix", reason="standalone console requires PTY")
def test_manual_agent_console_runs_interactively_and_persists_session(tmp_path: Path):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    store = AgentConsoleStore(
        tmp_path / "console",
        terminal_policy=InteractiveTerminalPolicy(enabled=True),
    )
    command = [
        sys.executable,
        "-u",
        "-c",
        (
            "import sys; "
            "print('\\x1b[32mREADY\\x1b[0m', flush=True); "
            "exec(\"for line in sys.stdin:\\n"
            "    value=line.strip()\\n"
            "    if value == 'exit': break\\n"
            "    print('ECHO:'+value, flush=True)\")"
        ),
    ]
    manager = ManualAgentConsoleManager(
        store=store,
        provider_loader=lambda: [_provider()],
        workdir_loader=lambda: workdir,
        environment_loader=lambda: os.environ.copy(),
        command_builder=lambda provider, cwd: list(command),
    )

    started = manager.start("fake-agent")
    payload = {
        "package_id": "manual",
        "stage": "interactive",
        "agent_id": "fake-agent",
    }

    def captured(fragment: str) -> bool:
        rows = store.read_events(started["session_id"])
        return fragment in "".join(item.get("text", "") for item in rows["events"])

    _wait_until(lambda: captured("READY"))
    assert not captured("\x1b[32m")

    store.queue_control_event(
        started["session_id"], action="input", data="hello\n"
    )
    _wait_until(lambda: captured("ECHO:hello"))

    store.queue_control_event(
        started["session_id"], action="input", data="exit\n"
    )
    _wait_until(lambda: not manager.status("fake-agent")["running"])

    captured_session = store.read_events(started["session_id"])
    metadata = captured_session["metadata"]
    assert "READY" in captured_session["terminal_screen"]["content"]
    assert "ECHO:hello" in captured_session["terminal_screen"]["content"]
    assert metadata["origin"] == "manual"
    assert metadata["working_directory"] == str(workdir)
    assert metadata["command"] == command
    assert metadata["status"] == "completed"
    assert store.consume_control_events(payload) == []
    manager.close()


@pytest.mark.skipif(os.name != "posix", reason="standalone console requires PTY")
def test_manual_agent_console_rejects_duplicate_and_can_be_stopped(tmp_path: Path):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    store = AgentConsoleStore(
        tmp_path / "console",
        terminal_policy=InteractiveTerminalPolicy(enabled=True),
    )
    manager = ManualAgentConsoleManager(
        store=store,
        provider_loader=lambda: [_provider()],
        workdir_loader=lambda: workdir,
        environment_loader=lambda: os.environ.copy(),
        command_builder=lambda provider, cwd: [
            sys.executable,
            "-u",
            "-c",
            "import time; print('WAIT', flush=True); time.sleep(60)",
        ],
    )

    started = manager.start("fake-agent")
    with pytest.raises(ManualAgentConsoleError, match="already has"):
        manager.start("fake-agent")

    stopped = manager.stop(session_id=started["session_id"])
    assert stopped["stopped"] is True
    _wait_until(lambda: not manager.status("fake-agent")["running"])
    metadata = store.read_events(started["session_id"])["metadata"]
    assert metadata["status"] == "stopped"
    manager.close()


def test_manual_agent_console_respects_disabled_terminal_policy(tmp_path: Path):
    manager = ManualAgentConsoleManager(
        store=AgentConsoleStore(tmp_path / "console"),
        provider_loader=lambda: [_provider()],
        workdir_loader=lambda: tmp_path,
        environment_loader=lambda: os.environ.copy(),
    )
    with pytest.raises(ManualAgentConsoleError, match="disabled"):
        manager.start("fake-agent")

@pytest.mark.skipif(os.name != "posix", reason="standalone console requires PTY")
def test_manual_agent_console_dispatches_raw_tui_controls_immediately(tmp_path: Path):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    script = tmp_path / "raw_menu.py"
    script.write_text(
        """
import os
import sys
import termios
import tty

fd = sys.stdin.fileno()
previous = termios.tcgetattr(fd)
tty.setraw(fd)
selection = 0
options = ["one", "two"]

def render():
    sys.stdout.write("\\x1b[2J\\x1b[HChoose\\r\\n")
    for index, option in enumerate(options):
        sys.stdout.write(("> " if index == selection else "  ") + option + "\\r\\n")
    sys.stdout.flush()

render()
try:
    while True:
        value = os.read(fd, 1)
        if value == b"\\x1b" and os.read(fd, 2) == b"[B":
            selection = 1
            render()
        elif value == b"\\r":
            sys.stdout.write("\\x1b[2J\\x1b[HSELECTED:" + options[selection] + "\\r\\n")
            sys.stdout.flush()
            break
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, previous)
""".lstrip(),
        encoding="utf-8",
    )
    store = AgentConsoleStore(
        tmp_path / "console",
        terminal_policy=InteractiveTerminalPolicy(enabled=True),
    )
    manager = ManualAgentConsoleManager(
        store=store,
        provider_loader=lambda: [_provider()],
        workdir_loader=lambda: workdir,
        environment_loader=lambda: os.environ.copy(),
        command_builder=lambda provider, cwd: [sys.executable, "-u", str(script)],
    )

    started = manager.start("fake-agent")
    _wait_until(
        lambda: "Choose" in store.read_events(started["session_id"])[
            "terminal_screen"
        ].get("content", "")
    )

    store.queue_control_event(
        started["session_id"], action="input", data="\x1b[B\r"
    )
    assert manager.dispatch_pending(started["session_id"]) is True
    _wait_until(
        lambda: "SELECTED:two" in store.read_events(started["session_id"])[
            "terminal_screen"
        ].get("content", "")
    )
    _wait_until(lambda: not manager.status("fake-agent")["running"])
    assert manager.dispatch_pending(started["session_id"]) is False
    manager.close()
