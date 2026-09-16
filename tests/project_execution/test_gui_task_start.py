from __future__ import annotations

from execraft.gui.application import ControlCenterService
from execraft.gui.contracts import ActiveTaskRef
from execraft.gui.project_task_drivers import ProjectTaskDriverPool


class _Dashboard:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def start_run(self):
        result = self.results[self.calls]
        self.calls += 1
        return result


def test_project_execution_task_start_preserves_dashboard_initialization_contract() -> None:
    dashboard = _Dashboard(
        [
            {"initialized": True, "message": "initialized"},
            {"owned_running": True, "message": "running"},
        ]
    )

    result = ControlCenterService._start_dashboard_run(dashboard)

    assert dashboard.calls == 2
    assert result["owned_running"] is True


def test_project_execution_task_start_does_not_duplicate_running_start() -> None:
    dashboard = _Dashboard([{"owned_running": True, "message": "already running"}])

    result = ControlCenterService._start_dashboard_run(dashboard)

    assert dashboard.calls == 1
    assert result["owned_running"] is True


class _PooledDashboard(_Dashboard):
    def __init__(self, project_id: str, task_id: str, results):
        super().__init__(results)
        self.project_id = project_id
        self.task_id = task_id
        self.closed = False
        self.running = False

    def start_run(self):
        result = super().start_run()
        self.running = bool(result.get("owned_running"))
        return result

    def close(self):
        self.closed = True
        self.running = False


def test_project_task_driver_pool_keeps_parallel_tasks_independent() -> None:
    dashboards: dict[tuple[str, str], _PooledDashboard] = {}

    def factory(project_id: str, task_id: str):
        dashboard = _PooledDashboard(
            project_id,
            task_id,
            [{"owned_running": True, "message": f"running {task_id}"}],
        )
        dashboards[(project_id, task_id)] = dashboard
        return dashboard

    pool = ProjectTaskDriverPool(
        dashboard_factory=factory,
        process_status_reader=lambda dashboard: {
            "owned_running": dashboard.running,
            "external_running": False,
        },
    )

    first = pool.start("demo", "task-a")
    second = pool.start("demo", "task-b")

    assert first.accepted is True
    assert second.accepted is True
    assert pool.owned_tasks() == (
        ActiveTaskRef("demo", "task-a"),
        ActiveTaskRef("demo", "task-b"),
    )
    assert not dashboards[("demo", "task-a")].closed
    assert not dashboards[("demo", "task-b")].closed

    pool.close()
    assert dashboards[("demo", "task-a")].closed
    assert dashboards[("demo", "task-b")].closed


def test_project_task_driver_pool_reaps_completed_driver_before_next_start() -> None:
    dashboards: dict[tuple[str, str], _PooledDashboard] = {}

    def factory(project_id: str, task_id: str):
        dashboard = _PooledDashboard(
            project_id,
            task_id,
            [{"owned_running": True, "message": "running"}],
        )
        dashboards[(project_id, task_id)] = dashboard
        return dashboard

    pool = ProjectTaskDriverPool(
        dashboard_factory=factory,
        process_status_reader=lambda dashboard: {
            "owned_running": dashboard.running,
            "external_running": False,
        },
    )
    pool.start("demo", "task-a")
    dashboards[("demo", "task-a")].running = False

    pool.start("demo", "task-b")

    assert dashboards[("demo", "task-a")].closed is True
    assert pool.owned_tasks() == (ActiveTaskRef("demo", "task-b"),)
