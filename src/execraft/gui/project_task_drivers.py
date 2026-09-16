"""Parallel-safe coarse Task launcher used by Project Execution GUI actions.

Project Execution is allowed to start a canonical Task as a whole, but it must
not own Work Package scheduling.  The GUI already has the canonical Task Run
entry point on ``TaskDashboard``; this pool reuses that entry point without
forcing every automatically-started Task to become the browser's selected Task.

Keeping launcher ownership separate from the selected dashboard is important in
Automatic mode: multiple Tasks may run concurrently while the operator keeps a
different Task (or no Task) open in the workbench.
"""

from __future__ import annotations

from threading import RLock
from typing import Any, Callable, Mapping

from execraft.gui.contracts import ActiveTaskRef, TaskDashboard, TaskDashboardFactory
from execraft.project_execution.task_port import TaskStartResult

ProcessStatusReader = Callable[[object], Mapping[str, Any]]


def start_dashboard_run(dashboard: TaskDashboard) -> dict[str, Any]:
    """Initialize a Task when necessary, then invoke its canonical Run action.

    ``TaskDashboard.start_run`` deliberately owns both first-run initialization
    and normal orchestration startup.  Project Execution must preserve that
    lifecycle contract rather than calling lower-level orchestrator machinery.
    """

    result = dashboard.start_run()
    if result.get("initialized") and not (
        result.get("owned_running") or result.get("external_running")
    ):
        return dashboard.start_run()
    return result


class ProjectTaskDriverPool:
    """Own Task dashboards started by Project Execution independently of UI focus.

    One retained dashboard owns each locally launched Task driver.  This avoids
    accidental process termination when the operator switches the visible Task
    and permits Automatic mode to respect a parallel-Task policy.  Completed
    drivers are reaped opportunistically before subsequent starts and at GUI
    shutdown.
    """

    def __init__(
        self,
        *,
        dashboard_factory: TaskDashboardFactory,
        process_status_reader: ProcessStatusReader,
    ) -> None:
        self._dashboard_factory = dashboard_factory
        self._process_status_reader = process_status_reader
        self._dashboards: dict[ActiveTaskRef, TaskDashboard] = {}
        self._lock = RLock()

    def start(self, project_id: str, task_id: str) -> TaskStartResult:
        """Start one canonical Task without changing the selected GUI Task."""

        key = ActiveTaskRef(project_id, task_id)
        with self._lock:
            self._reap_completed_unlocked(exclude=key)
            existing = self._dashboards.get(key)
            if existing is not None:
                status = self._process_status_reader(existing)
                if status.get("owned_running") or status.get("external_running"):
                    return TaskStartResult(
                        accepted=True,
                        message="Task driver is already running",
                    )
                self._close_unlocked(key)

            dashboard = self._dashboard_factory(project_id, task_id)
            try:
                result = start_dashboard_run(dashboard)
            except Exception:
                dashboard.close()
                raise

            # Retain process ownership even when the immediate result does not
            # expose a running flag.  The canonical Task port observes durable
            # Task state after this callback and decides whether the start took.
            self._dashboards[key] = dashboard
            return TaskStartResult(
                accepted=True,
                message=str(result.get("message", "Task start requested")),
            )

    def close(self) -> None:
        """Close every dashboard owned by the Project Execution launcher."""

        with self._lock:
            for key in tuple(self._dashboards):
                self._close_unlocked(key)

    def owned_tasks(self) -> tuple[ActiveTaskRef, ...]:
        """Return retained launcher identities for diagnostics and tests."""

        with self._lock:
            return tuple(sorted(self._dashboards, key=lambda item: (item.project_id, item.task_id)))

    def _reap_completed_unlocked(self, *, exclude: ActiveTaskRef | None = None) -> None:
        for key, dashboard in tuple(self._dashboards.items()):
            if key == exclude:
                continue
            status = self._process_status_reader(dashboard)
            if status.get("owned_running") or status.get("external_running"):
                continue
            self._close_unlocked(key)

    def _close_unlocked(self, key: ActiveTaskRef) -> None:
        dashboard = self._dashboards.pop(key, None)
        if dashboard is not None:
            dashboard.close()


__all__ = [
    "ProjectTaskDriverPool",
    "ProcessStatusReader",
    "start_dashboard_run",
]
