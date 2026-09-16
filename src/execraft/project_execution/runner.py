"""Foreground driver loop for opt-in Automatic Project execution.

Like the Task daemon driver, this module is not a process manager.  It provides
an injectable polling loop that a CLI/service wrapper can run.  Every cycle is
durable before the loop sleeps, so process restarts resume from Project runtime
state and start intents rather than in-memory scheduler state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .engine import AutomaticExecutionCycle, ProjectExecutionEngine
from .task_port import TaskOutcome

_TERMINAL_OUTCOMES = {
    TaskOutcome.COMPLETED.value,
    TaskOutcome.FAILED.value,
    TaskOutcome.CANCELLED.value,
}


@dataclass(frozen=True)
class AutomaticRunnerConfig:
    """Polling policy for a foreground Automatic Project executor."""

    poll_seconds: float = 2.0
    max_cycles: int | None = None

    def __post_init__(self) -> None:
        if self.poll_seconds < 0:
            raise ValueError("poll_seconds cannot be negative")
        if self.max_cycles is not None and self.max_cycles < 1:
            raise ValueError("max_cycles must be at least 1 when provided")


@dataclass(frozen=True)
class AutomaticRunnerResult:
    """Why an Automatic runner stopped and the last durable cycle."""

    cycles: int
    tasks_terminal: bool
    tasks_successful: bool
    held: bool
    quiescent: bool
    exhausted: bool
    last_cycle: AutomaticExecutionCycle


def run_automatic(
    engine: ProjectExecutionEngine,
    *,
    config: AutomaticRunnerConfig | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> AutomaticRunnerResult:
    """Drive Automatic Project execution until completion or a control boundary.

    The runner stops when:

    * every Project Task is terminal;
    * Project Execution enters a Hold;
    * no Task is running and a cycle cannot start anything (for example because
      a human Gate decision is required); or
    * ``max_cycles`` is exhausted.

    It never bypasses Gate decisions and never retries uncertain start intents;
    those rules remain enforced by the engine/planner on every durable cycle.
    """

    cfg = config or AutomaticRunnerConfig()
    cycles = 0
    while True:
        cycle = engine.automatic_cycle()
        cycles += 1
        snapshot = cycle.snapshot
        outcomes = tuple(snapshot.task_outcomes.values())
        tasks_terminal = bool(outcomes) and all(
            outcome in _TERMINAL_OUTCOMES for outcome in outcomes
        )
        tasks_successful = bool(outcomes) and all(
            outcome == TaskOutcome.COMPLETED.value for outcome in outcomes
        )
        if tasks_terminal:
            return AutomaticRunnerResult(
                cycles=cycles,
                tasks_terminal=True,
                tasks_successful=tasks_successful,
                held=snapshot.held,
                quiescent=False,
                exhausted=False,
                last_cycle=cycle,
            )
        if snapshot.held:
            return AutomaticRunnerResult(
                cycles=cycles,
                tasks_terminal=False,
                tasks_successful=False,
                held=True,
                quiescent=False,
                exhausted=False,
                last_cycle=cycle,
            )
        if cycle.start_failures:
            return AutomaticRunnerResult(
                cycles=cycles,
                tasks_terminal=False,
                tasks_successful=False,
                held=False,
                quiescent=True,
                exhausted=False,
                last_cycle=cycle,
            )

        running = any(
            outcome == TaskOutcome.RUNNING.value
            for outcome in outcomes
        )
        if not cycle.started_tasks and not running:
            return AutomaticRunnerResult(
                cycles=cycles,
                tasks_terminal=False,
                tasks_successful=False,
                held=False,
                quiescent=True,
                exhausted=False,
                last_cycle=cycle,
            )
        if cfg.max_cycles is not None and cycles >= cfg.max_cycles:
            return AutomaticRunnerResult(
                cycles=cycles,
                tasks_terminal=False,
                tasks_successful=False,
                held=False,
                quiescent=False,
                exhausted=True,
                last_cycle=cycle,
            )
        sleep(cfg.poll_seconds)


__all__ = [
    "AutomaticRunnerConfig",
    "AutomaticRunnerResult",
    "run_automatic",
]
