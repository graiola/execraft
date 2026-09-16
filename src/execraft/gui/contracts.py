"""Small contracts shared by the project-independent GUI application layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol


class ControlCenterError(RuntimeError):
    """Raised when a control-center operation is invalid or unsafe."""


class TaskDashboard(Protocol):
    """Task-bound surface the project-independent application actually uses."""

    project_id: str
    task_id: str

    def snapshot(self) -> dict[str, Any]: ...

    def list_tasks(self) -> list[dict[str, Any]]: ...

    def start_run(self, *, no_wait_for_agents: bool = False) -> dict[str, Any]: ...

    def close(self) -> None: ...


TaskDashboardFactory = Callable[[str, str], TaskDashboard]


@dataclass(frozen=True)
class ActiveTaskRef:
    """Stable composite identity for the task currently opened in the GUI."""

    project_id: str
    task_id: str

    def as_mapping(self) -> dict[str, str]:
        return {"project_id": self.project_id, "task_id": self.task_id}


def task_process_status(service: object) -> Mapping[str, Any]:
    """Read optional process status from a task dashboard."""

    process = getattr(service, "process", None)
    status = getattr(process, "status", None)
    if not callable(status):
        return {}
    value = status()
    return value if isinstance(value, Mapping) else {}


__all__ = [
    "ActiveTaskRef",
    "ControlCenterError",
    "TaskDashboard",
    "TaskDashboardFactory",
    "task_process_status",
]
