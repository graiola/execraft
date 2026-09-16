"""Project-independent application service for the local Execraft control center.

The browser shell must be useful before a project or task exists.  This facade
owns browser context and project-independent catalog operations, then composes
focused services for project-home reads, onboarding workflows, and the existing
task-bound dashboard. It deliberately contains no HTTP or DOM concerns.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from execraft.bootstrap import create_onboarding_service
from execraft.catalog_archive import CatalogArchiveError, CatalogArchiveManager
from execraft.control_plane import ControlPlaneHome
from execraft.gui.contracts import (
    ActiveTaskRef,
    ControlCenterError,
    TaskDashboard,
    TaskDashboardFactory,
    task_process_status,
)
from execraft.gui.errors import GuiError
from execraft.gui.onboarding_controller import GuiOnboardingController
from execraft.gui.project_home import ProjectHomeService
from execraft.gui.project_task_drivers import ProjectTaskDriverPool, start_dashboard_run
from execraft.gui.roadmap_facade import RoadmapGuiFacade
from execraft.gui.project_execution import ProjectExecutionGuiService
from execraft.export import ProjectExportService
from execraft.onboarding.service import OnboardingService
from execraft.permanent_removal import PermanentRemovalService
from execraft.removal_models import PermanentRemovalError
from execraft.orchestrate.identity import resolve_storage_identity
from execraft.persistence import LockUnavailableError, file_lock_is_held
from execraft.roadmap import RoadmapError, RoadmapService
from execraft.project_execution.task_port import TaskStartResult
from execraft.project import (
    ProjectError,
    load_registered_project,
    project_directory,
    validate_project_id,
)
from execraft.workspace.task_git import validate_task_id


class ControlCenterService(RoadmapGuiFacade):
    """Project-independent control-center application facade.

    It is safe to construct with an empty catalog. Task-specific operations are
    delegated only after :meth:`open_task` creates an isolated task dashboard.
    Mutations require explicit acknowledgement because the HTTP token proves
    request origin, not operator intent.
    """

    def __init__(
        self,
        *,
        home: ControlPlaneHome,
        task_dashboard_factory: TaskDashboardFactory,
        onboarding: OnboardingService | None = None,
        focused_project_id: str = "",
        initial_task: ActiveTaskRef | None = None,
        process_status_reader: Callable[[object], Mapping[str, Any]] = task_process_status,
    ) -> None:
        self.home = home
        self.root = home.root
        self.state_root = home.state_dir
        self.onboarding = onboarding or create_onboarding_service()
        self._task_dashboard_factory = task_dashboard_factory
        self._process_status_reader = process_status_reader
        self._task_dashboard: TaskDashboard | None = None
        self._project_task_drivers = ProjectTaskDriverPool(
            dashboard_factory=task_dashboard_factory,
            process_status_reader=process_status_reader,
        )
        self.focused_project_id = focused_project_id.strip()
        self.catalog_archive = CatalogArchiveManager(
            self.root, state_root=self.state_root
        )
        self.permanent_removal = PermanentRemovalService(
            self.root, self.state_root
        )
        self.project_home = ProjectHomeService(
            home=home,
            onboarding=self.onboarding,
            active_task=lambda: self.active_task,
        )
        self.roadmaps = RoadmapService(
            control_root=self.root,
            state_root=self.state_root,
            active_task=lambda: self.active_task,
        )
        self.exports = ProjectExportService(
            control_root=self.root,
            state_root=self.state_root,
        )
        self.project_execution_workspace = ProjectExecutionGuiService(
            control_root=self.root,
            state_root=self.state_root,
            task_starter=self._start_project_execution_task,
        )
        self.onboarding_controller = GuiOnboardingController(
            home=home,
            onboarding=self.onboarding,
            project_home=self.project_home,
        )
        if initial_task is not None:
            self.open_task(
                initial_task.project_id,
                initial_task.task_id,
                acknowledged=True,
            )

    @property
    def active_task(self) -> ActiveTaskRef | None:
        service = self._task_dashboard
        if service is None:
            return None
        return ActiveTaskRef(service.project_id, service.task_id)

    @property
    def project_id(self) -> str:
        active = self.active_task
        return active.project_id if active else self.focused_project_id

    @property
    def task_id(self) -> str:
        active = self.active_task
        return active.task_id if active else ""

    def close(self) -> None:
        self._project_task_drivers.close()
        if self._task_dashboard is not None:
            self._task_dashboard.close()
            self._task_dashboard = None

    def __getattr__(self, name: str) -> Any:
        """Delegate legacy task endpoints to the selected task dashboard."""

        service = self.__dict__.get("_task_dashboard")
        if service is None:
            raise ControlCenterError(
                f"operation {name!r} requires an open task; select a task first"
            )
        return getattr(service, name)

    def snapshot(self) -> dict[str, Any]:
        if self._task_dashboard is None:
            return self.home_snapshot()
        payload = self._task_dashboard.snapshot()
        payload["mode"] = "task"
        payload["application"] = self._application_metadata()
        return payload

    def _catalog_entry_driver_active(self, project_id: str, task_id: str) -> bool:
        if not task_id:
            tasks_root = project_directory(self.root, project_id) / "tasks"
            task_ids = (
                [
                    item.name
                    for item in tasks_root.iterdir()
                    if item.is_dir() and (item / "TASK.yaml").is_file()
                ]
                if tasks_root.is_dir()
                else []
            )
            return any(
                self._catalog_entry_driver_active(project_id, candidate)
                for candidate in task_ids
            )
        identity = resolve_storage_identity(
            self.state_root,
            project_id=project_id,
            task_id=task_id,
            create=False,
        )
        lock_path = identity.state_dir / "orchestrator-driver.lock"
        try:
            return file_lock_is_held(lock_path)
        except LockUnavailableError:  # pragma: no cover - non-POSIX status fallback
            return False

    def archive_catalog(self) -> dict[str, Any]:
        """Return the reversible catalog independently of task selection."""

        active = self.active_task
        return self.catalog_archive.catalog(
            current_project=(
                self.focused_project_id or (active.project_id if active else "")
            ),
            current_task=active.task_id if active else "",
        )

    def inspect_archive(
        self, kind: str, *, project_id: str, item_id: str
    ) -> dict[str, Any]:
        try:
            return self.catalog_archive.inspect(
                kind, project_id=project_id, item_id=item_id
            )
        except CatalogArchiveError as exc:
            raise GuiError(str(exc)) from exc

    def archive_catalog_entry(
        self, kind: str, *, project_id: str, item_id: str, reason: str = ""
    ) -> dict[str, Any]:
        """Archive an inactive catalog entry from project-level UI."""

        normalized_kind = str(kind).strip().lower()
        project_id = str(project_id).strip()
        item_id = str(item_id).strip()
        active = self.active_task
        if (
            normalized_kind == "task"
            and active is not None
            and project_id == active.project_id
            and item_id == active.task_id
        ):
            raise GuiError("the currently open task cannot be archived")
        if normalized_kind == "project" and (
            project_id == self.focused_project_id
            or (active is not None and project_id == active.project_id)
        ):
            raise GuiError("leave the project workspace before archiving that project")
        if self._catalog_entry_driver_active(
            project_id, item_id if normalized_kind == "task" else ""
        ):
            raise GuiError("stop the entry's orchestrator driver before archiving it")
        try:
            if normalized_kind == "task":
                return self.catalog_archive.archive_task(
                    project_id, item_id, reason=reason
                )
            if normalized_kind == "project":
                return self.catalog_archive.archive_project(
                    project_id, reason=reason
                )
            raise GuiError(f"unsupported archive kind: {normalized_kind!r}")
        except CatalogArchiveError as exc:
            raise GuiError(str(exc)) from exc

    def delete_catalog_entry(
        self,
        kind: str,
        *,
        project_id: str,
        item_id: str,
        confirmation: str,
        delete_branches: bool = False,
    ) -> dict[str, Any]:
        """Permanently remove one inactive catalog entry after exact-ID confirmation."""

        normalized_kind = str(kind).strip().lower()
        project_id = str(project_id).strip()
        item_id = str(item_id).strip()
        expected = item_id if normalized_kind == "task" else project_id
        if not expected or confirmation.strip() != expected:
            raise GuiError("permanent deletion requires typing the exact project/task ID")
        active = self.active_task
        if (
            normalized_kind == "task"
            and active is not None
            and project_id == active.project_id
            and item_id == active.task_id
        ):
            raise GuiError("leave the currently open task before deleting it")
        if normalized_kind == "project" and (
            project_id == self.focused_project_id
            or (active is not None and project_id == active.project_id)
        ):
            raise GuiError("leave the project workspace before deleting that project")
        if self._catalog_entry_driver_active(
            project_id, item_id if normalized_kind == "task" else ""
        ):
            raise GuiError("stop the entry's orchestrator driver before deleting it")
        if normalized_kind == "task":
            try:
                references = self.roadmaps.task_references(project_id, item_id)
            except (RoadmapError, ProjectError, OSError, ValueError) as exc:
                raise GuiError(str(exc)) from exc
            if references:
                labels = ", ".join(
                    f"{item['roadmap_title']} ({item['roadmap_id']})"
                    for item in references
                )
                raise GuiError(
                    "remove or detach roadmap references before permanently deleting "
                    f"task {item_id!r}: {labels}"
                )
        try:
            if normalized_kind == "task":
                return self.permanent_removal.remove_task(
                    project_id, item_id, delete_branches=delete_branches
                ).as_mapping()
            if normalized_kind == "project":
                return self.permanent_removal.remove_project(
                    project_id, delete_branches=delete_branches
                ).as_mapping()
            raise GuiError(f"unsupported delete kind: {normalized_kind!r}")
        except (PermanentRemovalError, ProjectError) as exc:
            raise GuiError(str(exc)) from exc

    def reactivate_catalog_entry(
        self, kind: str, *, project_id: str, item_id: str
    ) -> dict[str, Any]:
        """Verify and restore one archived project or task."""

        normalized_kind = str(kind).strip().lower()
        try:
            if normalized_kind == "task":
                return self.catalog_archive.reactivate_task(project_id, item_id)
            if normalized_kind == "project":
                return self.catalog_archive.reactivate_project(project_id)
            raise GuiError(f"unsupported archive kind: {normalized_kind!r}")
        except CatalogArchiveError as exc:
            raise GuiError(str(exc)) from exc

    def list_tasks(self) -> list[dict[str, Any]]:
        if self._task_dashboard is not None:
            return self._task_dashboard.list_tasks()
        if self.focused_project_id:
            return self.project_home.project_tasks(self.focused_project_id)
        return []

    def home_snapshot(self) -> dict[str, Any]:
        projects = self.project_home.projects()
        focused = self.focused_project_id
        if focused and not any(item["id"] == focused for item in projects):
            focused = ""
        return {
            "mode": "home",
            "generated_at": _utc_now(),
            "application": self._application_metadata(),
            "control_home": self.home.as_mapping(),
            "focused_project_id": focused,
            "projects": projects,
            "templates": self.project_home.template_catalog(),
            "onboarding_sessions": self.project_home.onboarding_sessions(),
        }

    def _application_metadata(self) -> dict[str, Any]:
        active = self.active_task
        return {
            "active_task": active.as_mapping() if active else None,
            "focused_project_id": self.focused_project_id,
            "home_available": True,
        }

    def open_task(
        self,
        project_id: str,
        task_id: str,
        *,
        acknowledged: bool = False,
    ) -> dict[str, Any]:
        if not acknowledged:
            raise ControlCenterError("opening a task requires explicit acknowledgement")
        project_id = project_id.strip()
        task_id = task_id.strip()
        if not project_id or not task_id:
            raise ControlCenterError("project_id and task_id are required")
        project_id = validate_project_id(project_id)
        task_id = validate_task_id(task_id)
        if self.active_task == ActiveTaskRef(project_id, task_id):
            return self.snapshot()
        self._ensure_task_switch_safe("switching tasks")
        replacement = self._task_dashboard_factory(project_id, task_id)
        previous = self._task_dashboard
        self._task_dashboard = replacement
        self.focused_project_id = project_id
        if previous is not None:
            previous.close()
        return self.snapshot()

    def open_home(self, *, acknowledged: bool = False) -> dict[str, Any]:
        """Leave the task dashboard and open the selected project workspace."""

        if not acknowledged:
            raise ControlCenterError("returning home requires explicit acknowledgement")
        self._ensure_task_switch_safe("leaving the task")
        if self._task_dashboard is not None:
            active = self.active_task
            if active is not None:
                self.focused_project_id = active.project_id
            self._task_dashboard.close()
            self._task_dashboard = None
        return self.home_snapshot()

    def open_catalog(self, *, acknowledged: bool = False) -> dict[str, Any]:
        """Leave any task and clear project focus to show the global catalog."""

        if not acknowledged:
            raise ControlCenterError("returning to the project catalog requires explicit acknowledgement")
        self._ensure_task_switch_safe("leaving the task")
        if self._task_dashboard is not None:
            self._task_dashboard.close()
            self._task_dashboard = None
        self.focused_project_id = ""
        return self.home_snapshot()

    def open_project(
        self,
        project_id: str,
        *,
        acknowledged: bool = False,
    ) -> dict[str, Any]:
        """Safely leave a task and open one project's management workspace."""

        if not acknowledged:
            raise ControlCenterError("opening a project workspace requires explicit acknowledgement")
        project_id = validate_project_id(project_id.strip())
        load_registered_project(self.root, project_id)
        self._ensure_task_switch_safe("switching project workspaces")
        if self._task_dashboard is not None:
            self._task_dashboard.close()
            self._task_dashboard = None
        self.focused_project_id = project_id
        return self.home_snapshot()


    @staticmethod
    def _start_dashboard_run(dashboard: TaskDashboard) -> dict[str, Any]:
        """Compatibility wrapper around the canonical Task Run helper."""

        return start_dashboard_run(dashboard)

    def _start_project_execution_task(
        self,
        project_id: str,
        task_id: str,
    ) -> TaskStartResult:
        """Start a Task without coupling Project Execution to browser focus.

        Project Execution may launch more than one Task in Automatic mode.
        Those driver dashboards are therefore retained in a dedicated pool
        instead of replacing ``self._task_dashboard``, which belongs solely to
        the operator's selected Task view.
        """

        return self._project_task_drivers.start(project_id, task_id)

    def _ensure_task_switch_safe(self, operation: str) -> None:
        if self._task_dashboard is None:
            return
        status = self._process_status_reader(self._task_dashboard)
        if status.get("owned_running"):
            raise ControlCenterError(
                f"stop the dashboard-owned orchestrator before {operation}"
            )

    def focus_project(self, project_id: str) -> dict[str, Any]:
        """Change project focus while already in the project-home application."""

        if self._task_dashboard is not None:
            raise ControlCenterError(
                "use the acknowledged session project transition while a task is open"
            )
        project_id = project_id.strip()
        if project_id:
            load_registered_project(self.root, project_id)
        self.focused_project_id = project_id
        return self.home_snapshot()

    def inspect_source(
        self,
        source_root: str,
        *,
        template_id: str = "standard",
        feature_ids: Sequence[str] = (),
        include_devcontainer: bool = False,
        accept_decisions: bool = False,
    ) -> dict[str, Any]:
        return self.onboarding_controller.inspect_source(
            source_root,
            template_id=template_id,
            feature_ids=tuple(feature_ids),
            include_devcontainer=include_devcontainer,
            accept_decisions=accept_decisions,
        )

    def create_project_from_source(
        self,
        source_root: str,
        *,
        template_id: str = "standard",
        feature_ids: Sequence[str] = (),
        include_devcontainer: bool = False,
        accept_decisions: bool = False,
        acknowledged: bool = False,
    ) -> dict[str, Any]:
        result = self.onboarding_controller.create_project_from_source(
            source_root,
            template_id=template_id,
            feature_ids=tuple(feature_ids),
            include_devcontainer=include_devcontainer,
            accept_decisions=accept_decisions,
            acknowledged=acknowledged,
        )
        self.focused_project_id = str(result.pop("project_id"))
        result["home"] = self.home_snapshot()
        return result

    def register_descriptor(
        self,
        descriptor: str,
        *,
        source_root: str = "",
        replace: bool = False,
        acknowledged: bool = False,
    ) -> dict[str, Any]:
        result = self.onboarding_controller.register_descriptor(
            descriptor,
            source_root=source_root,
            replace=replace,
            acknowledged=acknowledged,
        )
        self.focused_project_id = str(result.pop("project_id"))
        result["home"] = self.home_snapshot()
        return result

    def project_readiness(self, project_id: str) -> dict[str, Any]:
        return self.project_home.project_readiness(project_id)

    def project_providers(self, project_id: str) -> dict[str, Any]:
        return self.project_home.project_providers(project_id)

    def project_execution(self, project_id: str) -> dict[str, Any]:
        return self.project_home.project_execution(project_id)

    def verification_snapshot(self, project_id: str) -> dict[str, Any]:
        return self.project_home.verification_snapshot(project_id)

    def update_verification(
        self,
        project_id: str,
        *,
        expected_sha256: str,
        enabled_indexes: Sequence[int],
        require_commands: bool,
        acknowledged: bool = False,
    ) -> dict[str, Any]:
        return self.project_home.update_verification(
            project_id,
            expected_sha256=expected_sha256,
            enabled_indexes=enabled_indexes,
            require_commands=require_commands,
            acknowledged=acknowledged,
        )

    def preview_start(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.onboarding_controller.preview_start(payload)

    def apply_start(
        self,
        payload: Mapping[str, Any],
        *,
        acknowledged: bool = False,
    ) -> dict[str, Any]:
        result = self.onboarding_controller.apply_start(
            payload,
            acknowledged=acknowledged,
        )
        self.focused_project_id = str(result.pop("project_id"))
        result.pop("task_id", None)
        return result

    def preview_greenfield(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.onboarding_controller.preview_greenfield(payload)

    def apply_greenfield(
        self,
        payload: Mapping[str, Any],
        *,
        acknowledged: bool = False,
    ) -> dict[str, Any]:
        result = self.onboarding_controller.apply_greenfield(
            payload,
            acknowledged=acknowledged,
        )
        self.focused_project_id = str(result.pop("project_id"))
        result["home"] = self.home_snapshot()
        return result

    def task_review(self, project_id: str, task_id: str) -> dict[str, Any]:
        return self.project_home.task_review(project_id, task_id)

    def onboarding_sessions(self) -> list[dict[str, Any]]:
        return self.project_home.onboarding_sessions()

    def template_catalog(self) -> dict[str, Any]:
        return self.project_home.template_catalog()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


__all__ = [
    "ActiveTaskRef",
    "ControlCenterError",
    "ControlCenterService",
    "TaskDashboard",
    "TaskDashboardFactory",
]
