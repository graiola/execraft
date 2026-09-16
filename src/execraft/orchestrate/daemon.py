"""Daemon-style retry driver for ProjectOrchestrator.

Daemon/service mode supports automatic restart and
resume. Waiting for an agent quota reset, rate limit, temporary environment
outage, or build resource must be a normal durable state, not a fatal
error." This module does not implement a background service process (no
process manager, no signal handling) — it implements the retry/backoff loop
a service wrapper would call: repeatedly invoke
`ProjectOrchestrator.run_pipeline()` until a terminal state is reached,
backing off between attempts so a paused project resumes automatically once
its blocking condition (disk pressure, agent unavailability surfaced as a
paused/blocked state) clears.

Every attempt's outcome is durable (state.json + event journal) before this
loop ever sleeps, so a process crash between attempts loses nothing: a
fresh `run_until_terminal()` call against the same orchestrator resumes
from exactly where the last attempt left off — this is what "daemon restart
... resume behavior" means here, since `ProjectOrchestrator` itself has no
in-memory-only state that isn't already persisted by `transition_to()`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .models import TaskExecutionState
from .orchestrator import ProjectOrchestrator

# HUMAN_REQUIRED normally stops the daemon loop (per PLAN's
# human-intervention policy).  The orchestrator may explicitly identify a
# narrow, policy-controlled escalation as self-reconcilable; only those cases
# are allowed to enter ``run_pipeline()`` again.
TERMINAL_STATES = frozenset({
    TaskExecutionState.COMPLETED,
    TaskExecutionState.FAILED,
    TaskExecutionState.CANCELLED,
    TaskExecutionState.HUMAN_REQUIRED,
    TaskExecutionState.WAITING_FOR_HUMAN_DECISION,
    TaskExecutionState.OPERATOR_PAUSED,
})


@dataclass
class DaemonConfig:
    max_attempts: int | None = None
    initial_backoff_seconds: float = 5.0
    max_backoff_seconds: float = 300.0
    backoff_multiplier: float = 2.0


@dataclass
class DaemonResult:
    final_state: TaskExecutionState
    attempts: int
    exhausted: bool


def run_until_terminal(
    orchestrator: ProjectOrchestrator,
    *,
    config: DaemonConfig | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> DaemonResult:
    config = config or DaemonConfig()
    backoff = config.initial_backoff_seconds
    attempt = 0

    resume_operator_pause = orchestrator.state == TaskExecutionState.OPERATOR_PAUSED
    with orchestrator.exclusive_driver_lock():
        while True:
            if (
                orchestrator.state in TERMINAL_STATES
                and not orchestrator.can_auto_resume_terminal_state()
                and not (resume_operator_pause and attempt == 0)
            ):
                return DaemonResult(
                    final_state=orchestrator.state,
                    attempts=attempt,
                    exhausted=False,
                )

            attempt += 1
            if config.max_attempts is not None and attempt > config.max_attempts:
                return DaemonResult(
                    final_state=orchestrator.state,
                    attempts=attempt - 1,
                    exhausted=True,
                )

            orchestrator.run_pipeline()
            resume_operator_pause = False

            if (
                orchestrator.state in TERMINAL_STATES
                and not orchestrator.can_auto_resume_terminal_state()
            ):
                return DaemonResult(
                    final_state=orchestrator.state,
                    attempts=attempt,
                    exhausted=False,
                )

            if orchestrator.state == TaskExecutionState.WAITING_FOR_AGENT:
                delay = orchestrator.next_poll_delay_seconds(
                    default=backoff,
                    maximum=config.max_backoff_seconds,
                )
            else:
                delay = backoff
                backoff = min(
                    backoff * config.backoff_multiplier,
                    config.max_backoff_seconds,
                )
            sleep(delay)
