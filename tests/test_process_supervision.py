"""Tests for managed subprocess execution with process-group lifecycle."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from execraft.process import kill_process_group, managed_run


def test_managed_run_returns_stdout():
    result = managed_run(
        [sys.executable, "-c", "print('hello')"],
        timeout=10,
    )
    assert result.returncode == 0
    assert "hello" in result.stdout


def test_managed_run_returns_stderr():
    result = managed_run(
        [sys.executable, "-c", "import sys; sys.stderr.write('err')"],
        timeout=10,
    )
    assert "err" in result.stderr


@pytest.mark.parametrize("capture_mode", ["pipe", "file"])
def test_managed_run_passes_explicit_environment(capture_mode):
    environment = dict(os.environ)
    environment["EXECRAFT_TEST_ENVIRONMENT"] = "configured"

    result = managed_run(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['EXECRAFT_TEST_ENVIRONMENT'])",
        ],
        timeout=10,
        capture_mode=capture_mode,
        environment=environment,
    )

    assert result.stdout.strip() == "configured"


def test_managed_run_raises_on_timeout():
    with pytest.raises(subprocess.TimeoutExpired):
        managed_run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.1,
            grace_period=0.1,
        )


def test_managed_run_creates_new_process_group():
    result = managed_run(
        [sys.executable, "-c", "import os; print(os.getpgid(0))"],
        timeout=10,
    )
    pgid = int(result.stdout.strip())
    assert pgid > 0


def test_kill_process_group_sends_sigterm():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    pgid = os.getpgid(proc.pid)
    assert _process_exists(pgid)

    kill_process_group(pgid, grace_period=0.1)
    proc.wait(timeout=5)
    assert not _process_exists(pgid)


def test_kill_process_group_noop_when_already_gone():
    kill_process_group(999999999, grace_period=0.1)


def test_kill_process_group_waits_then_kills():
    """Verify the grace period allows SIGTERM before SIGKILL."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, lambda *a: None); time.sleep(30)",
        ],
        start_new_session=True,
    )
    pgid = os.getpgid(proc.pid)

    kill_process_group(pgid, grace_period=0.3)
    proc.wait(timeout=5)
    assert not _process_exists(pgid)


def test_child_processes_killed_on_timeout():
    """Grandchild processes are also killed when the parent times out."""
    import uuid
    tag = uuid.uuid4().hex

    parent_script = f"""
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
sys.stderr.write(child.pid)
sys.stderr.flush()
time.sleep(30)
"""
    with pytest.raises(subprocess.TimeoutExpired):
        managed_run(
            [sys.executable, "-c", parent_script],
            timeout=1.0,
            grace_period=0.2,
        )


def _process_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def test_managed_run_emits_periodic_process_heartbeats():
    heartbeats: list[dict] = []
    result = managed_run(
        [
            sys.executable,
            "-c",
            "import sys,time; print('start', flush=True); time.sleep(0.18); print('end', flush=True)",
        ],
        timeout=5,
        heartbeat_interval=0.05,
        heartbeat_callback=lambda payload: heartbeats.append(dict(payload)),
    )

    assert result.returncode == 0
    assert len(heartbeats) >= 2
    assert all(item["pid"] > 0 for item in heartbeats)
    assert all(item["process_count"] >= 1 for item in heartbeats)
    assert all(item["process_state"] for item in heartbeats)
    assert heartbeats[-1]["elapsed_seconds"] > heartbeats[0]["elapsed_seconds"]
    assert all("io_active" in item for item in heartbeats)


def test_live_supervision_waits_for_same_group_worker_output():
    """A launcher exit must not discard output from its supervised worker."""

    started = time.monotonic()
    result = managed_run(
        [
            "/bin/sh",
            "-c",
            "(sleep 0.3; printf 'worker-result\n') &",
        ],
        timeout=5,
        heartbeat_interval=0.05,
        heartbeat_callback=lambda _payload: None,
    )

    assert 0.2 <= time.monotonic() - started < 3
    assert result.returncode == 0
    assert result.stdout.strip() == "worker-result"


def test_live_supervision_does_not_wait_on_inherited_pipes(tmp_path):
    """A detached helper must not keep a completed CLI invocation alive."""

    child_pid_file = tmp_path / "detached-child.pid"
    script = (
        "setsid /bin/sh -c 'sleep 30' & "
        "child=$!; "
        f'printf "%s" "$child" > {str(child_pid_file)!r}; '
        "printf 'completed\n'"
    )

    started = time.monotonic()
    try:
        result = managed_run(
            ["/bin/sh", "-c", script],
            timeout=5,
            heartbeat_interval=0.05,
            heartbeat_callback=lambda _payload: None,
        )
    finally:
        if child_pid_file.exists():
            child_pid = int(child_pid_file.read_text())
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert time.monotonic() - started < 3
    assert result.returncode == 0
    assert result.stdout.strip() == "completed"


def test_heartbeat_callback_failure_does_not_change_process_result():
    def broken_callback(_payload):
        raise RuntimeError("telemetry sink unavailable")

    result = managed_run(
        [sys.executable, "-c", "import time; time.sleep(0.12); print('ok')"],
        timeout=5,
        heartbeat_interval=0.05,
        heartbeat_callback=broken_callback,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "ok"


def test_live_classifier_terminates_long_internal_retry_quickly():
    from execraft.agents.output_classification import AgentOutputClassifier
    from execraft.process import ManagedProcessTerminated

    started = time.monotonic()
    with pytest.raises(ManagedProcessTerminated) as caught:
        managed_run(
            [
                sys.executable,
                "-c",
                (
                    "import time; "
                    "print('monthly usage limit reached; reset in 3 days 21 hours', file=__import__('sys').stderr, flush=True); "
                    "time.sleep(30)"
                ),
            ],
            timeout=60,
            grace_period=0.1,
            output_classifier=AgentOutputClassifier(
                adapter="opencode", provider_id="opencode-go"
            ),
        )

    assert time.monotonic() - started < 3
    assert caught.value.signal.category == "quota_exhausted"
    assert caught.value.signal.retry_after_seconds == 334800
    assert "monthly usage limit" in caught.value.stderr


def test_inactivity_watchdog_terminates_silent_process():
    from execraft.process import ManagedProcessTerminated

    with pytest.raises(ManagedProcessTerminated) as caught:
        managed_run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=60,
            inactivity_timeout=0.2,
            grace_period=0.1,
        )

    assert caught.value.signal.category == "inactive"


def test_classifier_termination_kills_child_process_group(tmp_path):
    from execraft.agents.output_classification import AgentOutputClassifier
    from execraft.process import ManagedProcessTerminated

    pid_file = tmp_path / "child.pid"
    script = f"""
import subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
open({str(pid_file)!r}, 'w').write(str(child.pid))
print('quota exhausted; reset in 1 day', file=sys.stderr, flush=True)
time.sleep(30)
"""
    with pytest.raises(ManagedProcessTerminated):
        managed_run(
            [sys.executable, "-c", script],
            timeout=60,
            grace_period=0.1,
            output_classifier=AgentOutputClassifier(
                adapter="opencode", provider_id="opencode-go"
            ),
        )

    child_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("child process survived process-group termination")


def test_file_capture_mode_returns_stdout_and_stderr():
    result = managed_run(
        [
            sys.executable,
            "-c",
            "import sys; print('file-out', flush=True); print('file-err', file=sys.stderr, flush=True)",
        ],
        timeout=5,
        heartbeat_interval=0.05,
        heartbeat_callback=lambda _payload: None,
        capture_mode="file",
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "file-out"
    assert result.stderr.strip() == "file-err"


def test_file_capture_mode_classifies_incremental_output():
    from execraft.agents.output_classification import AgentOutputClassifier
    from execraft.process import ManagedProcessTerminated

    with pytest.raises(ManagedProcessTerminated) as caught:
        managed_run(
            [
                sys.executable,
                "-c",
                (
                    "import sys,time; "
                    "print('monthly usage limit reached; reset in 1 day', file=sys.stderr, flush=True); "
                    "time.sleep(30)"
                ),
            ],
            timeout=60,
            grace_period=0.1,
            output_classifier=AgentOutputClassifier(
                adapter="antigravity-cli", provider_id="antigravity"
            ),
            capture_mode="file",
        )

    assert caught.value.signal.category == "quota_exhausted"
    assert "monthly usage limit" in caught.value.stderr


def test_output_silence_watchdog_ignores_periodic_file_io(tmp_path):
    from execraft.process import ManagedProcessTerminated

    activity_file = tmp_path / "activity.log"
    script = f"while :; do printf 'tick\n' >> {str(activity_file)!r}; sleep 0.03; done"
    with pytest.raises(ManagedProcessTerminated) as caught:
        managed_run(
            ["/bin/sh", "-c", script],
            timeout=10,
            inactivity_timeout=2,
            output_silence_timeout=0.8,
            startup_grace_period=0,
            heartbeat_interval=0.05,
            heartbeat_callback=lambda _payload: None,
            grace_period=0.1,
        )

    assert caught.value.signal.category == "output_silence"
    assert activity_file.is_file()


def test_output_silence_watchdog_resets_on_stdout():
    result = managed_run(
        [
            sys.executable,
            "-c",
            "import time; print('one', flush=True); time.sleep(.2); print('two', flush=True)",
        ],
        timeout=3,
        output_silence_timeout=0.5,
    )

    assert result.stdout.splitlines() == ["one", "two"]


def test_output_silence_watchdog_allows_slow_process_startup():
    result = managed_run(
        ["/bin/sh", "-c", "sleep 0.7; printf 'ready\n'"],
        timeout=3,
        output_silence_timeout=0.5,
        startup_grace_period=1.5,
    )

    assert result.stdout.strip() == "ready"

def test_first_output_timeout_fails_before_general_silence_timeout():
    from execraft.process import ManagedProcessTerminated

    started = time.monotonic()
    with pytest.raises(ManagedProcessTerminated) as caught:
        managed_run(
            ["/bin/sh", "-c", "sleep 5"],
            timeout=10,
            first_output_timeout=0.3,
            output_silence_timeout=4,
            startup_grace_period=0,
            grace_period=0.1,
        )

    assert time.monotonic() - started < 2
    assert caught.value.signal.category == "first_output_timeout"


@pytest.mark.skipif(os.name != "posix", reason="PTY control is POSIX-only")
def test_operator_pty_input_does_not_mask_stdout_silence():
    from execraft.process import ManagedProcessTerminated

    controls = [[{"action": "input", "data": "operator text\n"}], []]
    with pytest.raises(ManagedProcessTerminated) as caught:
        managed_run(
            ["/bin/sh", "-c", "sleep 5"],
            timeout=10,
            output_silence_timeout=0.4,
            startup_grace_period=0,
            grace_period=0.1,
            terminal_callback=lambda: controls.pop(0) if controls else [],
        )

    assert caught.value.signal.category == "output_silence"



def test_managed_run_streams_decoded_output_to_observer():
    observed: list[tuple[str, str]] = []
    result = managed_run(
        [
            sys.executable,
            "-c",
            "import sys; print('hello', flush=True); print('problem', file=sys.stderr, flush=True)",
        ],
        timeout=5,
        output_callback=lambda stream, text: observed.append((stream, text)),
    )

    assert result.returncode == 0
    assert any(stream == "stdout" and "hello" in text for stream, text in observed)
    assert any(stream == "stderr" and "problem" in text for stream, text in observed)


def test_output_observer_failure_does_not_change_process_result():
    def broken(_stream, _text):
        raise RuntimeError("console storage unavailable")

    result = managed_run(
        [sys.executable, "-c", "print('still succeeds')"],
        timeout=5,
        output_callback=broken,
    )

    assert result.returncode == 0
    assert "still succeeds" in result.stdout


@pytest.mark.skipif(os.name != "posix", reason="PTY control is POSIX-only")
def test_managed_run_interactive_pty_accepts_stdin_and_preserves_stdout():
    controls = [[{"action": "input", "data": "hello-agent\n"}], []]
    heartbeats = []

    def terminal_callback():
        return controls.pop(0) if controls else []

    result = managed_run(
        [
            sys.executable,
            "-c",
                (
                    "import os,sys,time; "
                    "print('isatty='+str(os.isatty(0)), flush=True); "
                    "print('received='+sys.stdin.readline().strip(), flush=True); "
                    "time.sleep(.08)"
                ),
        ],
        timeout=5,
        heartbeat_interval=0.05,
        heartbeat_callback=heartbeats.append,
        terminal_callback=terminal_callback,
    )

    assert result.returncode == 0
    assert "isatty=True" in result.stdout
    assert "received=hello-agent" in result.stdout
    assert any(item.get("terminal_mode") == "pty" for item in heartbeats)


@pytest.mark.skipif(os.name != "posix", reason="controlling PTY is POSIX-only")
def test_managed_run_interactive_pty_exposes_dev_tty_prompt():
    controls = [[{"action": "input", "data": "approved\n"}], []]
    terminal_output = []

    def terminal_callback():
        return controls.pop(0) if controls else []

    result = managed_run(
        [
            sys.executable,
            "-c",
            (
                "import os; "
                "fd=os.open('/dev/tty', os.O_RDWR); "
                "os.write(fd,b'approve? '); "
                "answer=os.read(fd,128).decode().strip(); "
                "print('answer='+answer, flush=True)"
            ),
        ],
        timeout=5,
        output_callback=lambda stream, text: terminal_output.append((stream, text)),
        terminal_callback=terminal_callback,
    )

    assert result.returncode == 0
    assert "answer=approved" in result.stdout
    assert any(stream == "terminal" and "approve?" in text for stream, text in terminal_output)


@pytest.mark.skipif(os.name != "posix", reason="PTY control is POSIX-only")
def test_file_capture_mode_supports_interactive_pty_input():
    controls = [[{"action": "input", "data": "file-mode\n"}], []]

    result = managed_run(
        [sys.executable, "-c", "import sys; print(sys.stdin.readline().strip())"],
        timeout=5,
        terminal_callback=lambda: controls.pop(0) if controls else [],
        capture_mode="file",
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "file-mode"


def test_managed_run_streams_multi_megabyte_stdin_without_argv_growth():
    payload = "x" * (3 * 1024 * 1024)
    result = managed_run(
        [
            sys.executable,
            "-c",
            "import sys; data=sys.stdin.buffer.read(); print(len(data), flush=True)",
        ],
        input_data=payload,
        timeout=10,
        output_callback=lambda _stream, _text: None,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == str(len(payload))
    assert max(len(item) for item in result.args) < 1024


def test_managed_run_rejects_prompt_stdin_with_interactive_terminal():
    with pytest.raises(ValueError, match="cannot be combined with an interactive terminal"):
        managed_run(
            [sys.executable, "-c", "print('unused')"],
            input_data="prompt",
            terminal_callback=lambda: [],
            timeout=5,
        )


def test_output_limit_terminates_noisy_process_and_bounds_captured_data():
    from execraft.process import ManagedProcessTerminated

    limit = 64 * 1024
    script = (
        "import sys,time; "
        "sys.stdout.write('x' * 300000); sys.stdout.flush(); time.sleep(30)"
    )
    started = time.monotonic()
    with pytest.raises(ManagedProcessTerminated) as caught:
        managed_run(
            [sys.executable, "-c", script],
            timeout=60,
            grace_period=0.1,
            max_output_bytes=limit,
        )

    assert time.monotonic() - started < 3
    assert caught.value.signal.category == "output_limit"
    assert len(caught.value.stdout.encode()) + len(caught.value.stderr.encode()) <= limit
    assert "65536 bytes" in caught.value.signal.summary


def test_file_capture_output_limit_is_enforced_incrementally():
    from execraft.process import ManagedProcessTerminated

    with pytest.raises(ManagedProcessTerminated) as caught:
        managed_run(
            [
                sys.executable,
                "-c",
                "import sys,time; sys.stderr.write('e' * 200000); sys.stderr.flush(); time.sleep(30)",
            ],
            timeout=60,
            grace_period=0.1,
            max_output_bytes=32 * 1024,
            capture_mode="file",
        )

    assert caught.value.signal.category == "output_limit"
    assert len(caught.value.stdout.encode()) + len(caught.value.stderr.encode()) <= 32 * 1024
