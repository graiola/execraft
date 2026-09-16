"""Contract tests for the typed process infrastructure boundary."""

from __future__ import annotations

import subprocess
import signal
import sys

import pytest

from execraft.process import managed_run


def test_typed_boundary_preserves_completed_process_contract() -> None:
    completed = managed_run(
        [sys.executable, "-c", "print('typed-boundary')"],
        timeout=5,
    )

    assert isinstance(completed, subprocess.CompletedProcess)
    assert completed.returncode == 0
    assert completed.stdout.strip() == "typed-boundary"


def test_typed_boundary_preserves_timeout_contract() -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        managed_run(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout=0.05,
            grace_period=0.05,
        )


def test_typed_boundary_preserves_child_crash_contract() -> None:
    completed = managed_run(
        [
            sys.executable,
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
        ],
        timeout=5,
    )

    assert completed.returncode == -signal.SIGTERM
