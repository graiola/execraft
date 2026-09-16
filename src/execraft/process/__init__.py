"""Canonical OS process supervision boundary.

Agent adapters and runtime code depend on this package for managed subprocess
execution. Workflow orchestration deliberately does not own process lifecycle
implementation details.
"""

from .supervision import (
    ManagedProcessTerminated,
    ProcessHeartbeatCallback,
    ProcessOutputCallback,
    ProcessOutputClassifier,
    ProcessTerminalCallback,
    ProcessTerminationSignal,
    kill_process_group,
    managed_run,
    process_group_exists,
)

__all__ = [
    "ManagedProcessTerminated",
    "ProcessHeartbeatCallback",
    "ProcessOutputCallback",
    "ProcessOutputClassifier",
    "ProcessTerminalCallback",
    "ProcessTerminationSignal",
    "kill_process_group",
    "managed_run",
    "process_group_exists",
]
