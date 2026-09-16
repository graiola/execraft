#!/usr/bin/env python3
"""Run the mandatory fast local quality check used by CI.

The preflight intentionally stays smaller than the complete release suite.  It
catches import/name errors, architecture-boundary regressions, syntax errors,
and failures in the highest-signal integration tests before a developer spends
minutes on the full matrix or browser journeys.

Run from any directory with development dependencies installed::

    python tools/preflight.py

The script is also invoked by GitHub Actions.  Keep shared fast checks here
instead of duplicating command lists between documentation and CI workflows.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]

# These tests exercise the architecture checker plus the code paths most likely
# to reveal import/configuration regressions before the complete test matrix.
# Browser journeys are deliberately separate because Chromium is an external
# runtime dependency; see docs/ci-and-maintenance.md.
FOCUSED_TESTS: tuple[str, ...] = (
    "tests/test_architecture_boundaries.py",
    "tests/test_bootstrap.py",
    "tests/test_ci_contract.py",
    "tests/test_docs_contract.py",
    "tests/test_onboarding.py",
    "tests/test_preflight.py",
    "tests/test_start_workflow.py::test_task_intent_derives_stable_title_slug_and_digest",
    "tests/test_start_workflow.py::test_repository_selector_preserves_required_and_infers_optional_scope",
    "tests/test_start_workflow.py::test_local_draft_is_nonempty_and_publishes_atomically",
    "tests/test_start_workflow.py::test_new_local_plans_are_declarative_and_replan_ready",
    "tests/test_orchestrate_normalizer.py",
    "tests/test_orchestrate_sharding.py",
    "tests/test_sample_runtime_profiles.py",
    "tests/test_agent_eligibility.py",
    "tests/test_gui_dashboard_presenter.py",
    "tests/test_gui.py",
    "tests/test_gui_execution_view.py",
    "tests/test_gui_execution_lanes.py",
    "tests/test_gui_execution_health_view.py",
    "tests/test_gui_agent_workforce_view.py",
    "tests/test_runtime_gui_integration.py",
    "tests/test_gui_workbench_contracts.py",
    "tests/test_exports.py",
    "tests/test_gui_work_package_inspector_regression.py",
)


@dataclass(frozen=True)
class PreflightStep:
    """One deterministic preflight command and its optional environment."""

    name: str
    command: tuple[str, ...]
    environment: Mapping[str, str] = field(default_factory=dict)


def _ruff_command() -> tuple[str, ...]:
    """Resolve Ruff without assuming how the quality extra was installed."""

    if importlib.util.find_spec("ruff") is not None:
        return (sys.executable, "-m", "ruff")
    discovered = shutil.which("ruff")
    if discovered:
        return (discovered,)
    local_binary = ROOT / ".venv" / "bin" / "ruff"
    if local_binary.is_file() and os.access(local_binary, os.X_OK):
        return (str(local_binary),)
    # Keep the eventual error actionable: Python will explain that the quality
    # dependency is missing and docs show how to install it.
    return (sys.executable, "-m", "ruff")


def build_steps(*, bytecode_cache: Path) -> tuple[PreflightStep, ...]:
    """Return the mandatory A0 quality steps in execution order."""

    python = sys.executable
    return (
        PreflightStep(
            name="architecture boundaries",
            command=(python, "tools/check_architecture.py"),
        ),
        PreflightStep(
            name="documentation contracts",
            command=(python, "tools/check_docs.py"),
        ),
        PreflightStep(
            name="Ruff correctness lint",
            command=(*_ruff_command(), "check", "src", "tests", "tools"),
        ),
        PreflightStep(
            name="Ruff incremental bugbear lint",
            command=(
                *_ruff_command(),
                "check",
                "--select",
                "B",
                "tools",
                "src/execraft/orchestrate/acceptance.py",
                "src/execraft/orchestrate/package_finalization.py",
                "src/execraft/orchestrate/agent_wait.py",
            ),
        ),
        PreflightStep(
            name="Python compilation",
            command=(python, "-m", "compileall", "-q", "src", "tests", "tools"),
            environment={"PYTHONPYCACHEPREFIX": str(bytecode_cache)},
        ),
        PreflightStep(
            name="focused regression tests",
            command=(python, "-m", "pytest", "-q", *FOCUSED_TESTS),
        ),
    )


def _run_step(step: PreflightStep) -> int:
    print(f"\n==> {step.name}", flush=True)
    environment = os.environ.copy()
    environment.update(step.environment)
    try:
        completed = subprocess.run(
            step.command,
            cwd=ROOT,
            env=environment,
            check=False,
        )
    except OSError as exc:
        print(f"preflight error: cannot run {step.name}: {exc}", file=sys.stderr)
        return 1
    if completed.returncode != 0:
        print(
            f"preflight failed: {step.name} exited with code {completed.returncode}",
            file=sys.stderr,
        )
    return completed.returncode


def run_preflight(steps: Sequence[PreflightStep]) -> int:
    """Run *steps* fail-fast and return a process-style exit status."""

    for step in steps:
        returncode = _run_step(step)
        if returncode:
            return returncode
    print("\nA0 preflight: PASS")
    return 0


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="execraft-preflight-pyc-") as cache:
        return run_preflight(build_steps(bytecode_cache=Path(cache)))


if __name__ == "__main__":
    raise SystemExit(main())
