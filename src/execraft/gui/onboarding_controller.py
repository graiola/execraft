"""Application workflows used by the GUI onboarding surfaces.

The controller translates transport-neutral mappings into the typed onboarding
services.  It deliberately does not own browser session state; the
``ControlCenterService`` decides which project/task is focused after a
successful operation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from execraft.control_plane import ControlPlaneHome
from execraft.gui.contracts import ControlCenterError
from execraft.gui.project_home import ProjectHomeService
from execraft.onboarding import (
    GreenfieldService,
    PlannerMode,
    StartRequest,
    StartWorkflowService,
    default_greenfield_catalog,
)
from execraft.onboarding.service import OnboardingService
from execraft.onboarding.task_definition import TaskDefinitionInput
from execraft.project import (
    ProjectError,
    load_project_registration,
    load_registered_project,
    register_project_descriptor,
    resolve_current_project,
    resolve_project_source_root,
)


class GuiOnboardingController:
    """Coordinate project creation and start workflows for the GUI."""

    def __init__(
        self,
        *,
        home: ControlPlaneHome,
        onboarding: OnboardingService,
        project_home: ProjectHomeService,
    ) -> None:
        self.home = home
        self.root = home.root
        self.onboarding = onboarding
        self.project_home = project_home
        self.start_workflow = StartWorkflowService(
            home=home,
            onboarding=onboarding,
        )
        self.greenfield = GreenfieldService(
            onboarding=onboarding,
            templates=default_greenfield_catalog(),
        )

    def inspect_source(
        self,
        source_root: str,
        *,
        template_id: str = "standard",
        feature_ids: Sequence[str] = (),
        include_devcontainer: bool = False,
        accept_decisions: bool = False,
    ) -> dict[str, Any]:
        source = existing_directory(source_root, label="source_root")
        report = self.onboarding.inspect_project(source)
        try:
            registered = resolve_current_project(self.root, cwd=source)
            registration = load_project_registration(registered.id)
            if registration is not None and registration.source_root == source:
                entry = next(
                    item
                    for item in self.project_home.catalog()
                    if item.project.id == registered.id
                )
                return {
                    "report": report.as_mapping(),
                    "creation": None,
                    "registered_project": self.project_home.project_summary(entry),
                }
        except (ProjectError, StopIteration):
            pass
        preview = self.onboarding.create_project(
            report=report,
            output_dir=self.home.projects_dir,
            template_id=template_id,
            dry_run=True,
            accept_decisions=accept_decisions,
            feature_ids=tuple(feature_ids),
            include_devcontainer=include_devcontainer,
        )
        return {
            "report": report.as_mapping(),
            "creation": preview.as_mapping(),
            "registered_project": None,
        }

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
        require_acknowledgement(acknowledged, "project creation")
        source = existing_directory(source_root, label="source_root")
        report = self.onboarding.inspect_project(source)
        outcome = self.onboarding.create_project(
            report=report,
            output_dir=self.home.projects_dir,
            template_id=template_id,
            register=True,
            dry_run=False,
            accept_decisions=accept_decisions,
            feature_ids=tuple(feature_ids),
            include_devcontainer=include_devcontainer,
        )
        return {
            "project_id": report.project_id,
            "outcome": outcome.as_mapping(),
        }

    def register_descriptor(
        self,
        descriptor: str,
        *,
        source_root: str = "",
        replace: bool = False,
        acknowledged: bool = False,
    ) -> dict[str, Any]:
        require_acknowledgement(acknowledged, "project registration")
        descriptor_path = Path(descriptor).expanduser().resolve()
        project_file = (
            descriptor_path / "project.yaml"
            if descriptor_path.is_dir()
            else descriptor_path
        )
        if not project_file.is_file():
            raise ControlCenterError(
                f"project descriptor does not exist: {project_file}"
            )
        source = (
            existing_directory(source_root, label="source_root")
            if source_root
            else None
        )
        registration = register_project_descriptor(
            project_file,
            source_root=source,
            replace=replace,
        )
        raw = yaml.safe_load(project_file.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, Mapping):
            raise ControlCenterError("project descriptor must contain a mapping")
        project = load_registered_project(self.root, str(raw.get("project", "")))
        return {
            "project_id": project.id,
            "project": project.id,
            "registration": str(registration),
        }

    def preview_start(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.start_workflow.preview(self.start_request(payload)).as_mapping()

    def apply_start(
        self,
        payload: Mapping[str, Any],
        *,
        acknowledged: bool = False,
    ) -> dict[str, Any]:
        require_acknowledgement(acknowledged, "task start")
        request = self.start_request(payload)
        preview = self.start_workflow.preview(request)
        if not preview.can_apply:
            raise ControlCenterError(
                "start preview is blocked; resolve or accept the reported decisions"
            )
        outcome = self.start_workflow.run(request)
        return {
            "project_id": outcome.project_id,
            "task_id": outcome.task_id,
            "outcome": outcome.as_mapping(),
            "task": self.project_home.task_review(
                outcome.project_id,
                outcome.task_id,
            ),
        }

    def start_request(self, payload: Mapping[str, Any]) -> StartRequest:
        source_value = str(payload.get("source_root", "")).strip()
        if not source_value:
            project_id = str(payload.get("project_id", "")).strip()
            if not project_id:
                raise ControlCenterError("source_root or project_id is required")
            project = load_registered_project(self.root, project_id)
            source = resolve_project_source_root(project)
        else:
            source = existing_directory(source_value, label="source_root")
        try:
            planner = PlannerMode(str(payload.get("planner", "auto")))
        except ValueError as exc:
            raise ControlCenterError("planner must be auto, agent, or local") from exc
        return StartRequest(
            description=str(payload.get("description", "")),
            source_root=source,
            project_id=str(payload.get("project_id", "")),
            task_id=str(payload.get("task_id", "")),
            title=str(payload.get("title", "")),
            repository_ids=string_tuple(payload.get("repositories")),
            provider_id=str(payload.get("provider_id", "")),
            planner_mode=planner,
            project_template=str(payload.get("project_template", "standard")),
            task_template=str(payload.get("task_template", "standard")),
            workspace_root=(
                Path(str(payload["workspace_root"])).expanduser().resolve()
                if str(payload.get("workspace_root", "")).strip()
                else None
            ),
            policy_profile=str(payload.get("policy", "")),
            no_workspace=json_bool(payload, "no_workspace", default=False),
            reuse_in_place=json_bool(payload, "reuse_in_place", default=False),
            accept_decisions=json_bool(
                payload,
                "accept_decisions",
                default=False,
            ),
            require_provider=json_bool(
                payload,
                "require_provider",
                default=False,
            ),
            force_plan=json_bool(payload, "force_plan", default=False),
            task_definition=TaskDefinitionInput.from_contents(
                brief_markdown=str(payload.get("brief_markdown", "")),
                plan_markdown=str(payload.get("plan_markdown", "")),
                plan_graph_yaml=str(payload.get("plan_graph_yaml", "")),
                brief_source=str(payload.get("brief_source", "")),
                plan_source=str(payload.get("plan_source", "")),
                plan_graph_source=str(payload.get("plan_graph_source", "")),
            ),
            expected_request_sha256=str(
                payload.get("expected_request_sha256", "")
            ).strip(),
        )

    def preview_greenfield(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name", "")).strip()
        parent = Path(str(payload.get("parent", Path.cwd()))).expanduser().resolve()
        outcome = self.greenfield.create(
            name=name,
            parent=parent,
            descriptor_output=self.home.projects_dir,
            source_template=str(payload.get("template", "python-service")),
            project_template=str(payload.get("project_template", "standard")),
            project_features=string_tuple(payload.get("features")),
            include_devcontainer=json_bool(payload, "devcontainer", default=False),
            dry_run=True,
        )
        result = outcome.as_mapping()
        if str(payload.get("description", "")).strip():
            result["first_task"] = {
                "description": str(payload["description"]),
                "planner": str(payload.get("planner", "auto")),
            }
        return result

    def apply_greenfield(
        self,
        payload: Mapping[str, Any],
        *,
        acknowledged: bool = False,
    ) -> dict[str, Any]:
        require_acknowledgement(acknowledged, "greenfield creation")
        name = str(payload.get("name", "")).strip()
        parent = Path(str(payload.get("parent", Path.cwd()))).expanduser().resolve()
        outcome = self.greenfield.create(
            name=name,
            parent=parent,
            descriptor_output=self.home.projects_dir,
            source_template=str(payload.get("template", "python-service")),
            project_template=str(payload.get("project_template", "standard")),
            project_features=string_tuple(payload.get("features")),
            include_devcontainer=json_bool(payload, "devcontainer", default=False),
            dry_run=False,
        )
        result: dict[str, Any] = {
            "project_id": name,
            "project": outcome.as_mapping(),
        }
        description = str(payload.get("description", "")).strip()
        if description:
            if outcome.source_root is None:
                raise ControlCenterError(
                    "greenfield project was created without a source root"
                )
            start_payload = dict(payload)
            start_payload.update(
                {
                    "source_root": str(outcome.source_root),
                    "project_id": name,
                    "description": description,
                }
            )
            start_outcome = self.start_workflow.run(
                self.start_request(start_payload)
            )
            result["first_task"] = start_outcome.as_mapping()
        return result


def require_acknowledgement(acknowledged: bool, operation: str) -> None:
    if not acknowledged:
        raise ControlCenterError(f"{operation} requires explicit acknowledgement")


def json_bool(payload: Mapping[str, Any], key: str, *, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ControlCenterError(f"{key} must be a JSON boolean")
    return value


def existing_directory(value: str, *, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ControlCenterError(
            f"{label} does not exist or is not a directory: {path}"
        )
    return path


def string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ControlCenterError("repositories must be a list of strings")
    result = tuple(str(item).strip() for item in value if str(item).strip())
    return tuple(dict.fromkeys(result))


__all__ = [
    "GuiOnboardingController",
    "existing_directory",
    "json_bool",
    "require_acknowledgement",
    "string_tuple",
]
