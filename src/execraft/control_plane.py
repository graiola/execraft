"""Host-local control-plane home discovery and layout management.

The Python package is executable code, not the user's data directory.  This
module keeps that distinction explicit by resolving a writable control-plane
home independently from the package installation location.  Source checkouts
remain supported as a legacy/development layout, but a wheel installation can
operate from any working directory without environment-specific knowledge.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


CONTROL_ROOT_ENV = "EXECRAFT_CONTROL_ROOT"
LEGACY_CONTROL_ROOT_ENVS = ("EXECRAFT_WORKFLOW_ROOT", "AI_WORKFLOW_ROOT")
CONFIG_HOME_ENV = "EXECRAFT_CONFIG_HOME"
STATE_HOME_ENV = "EXECRAFT_STATE_HOME"


def _resolved_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def xdg_config_home() -> Path:
    """Return Execraft's host-local configuration directory."""

    override = os.environ.get(CONFIG_HOME_ENV)
    if override:
        return _resolved_path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = _resolved_path(xdg) if xdg else Path.home() / ".config"
    return (base / "execraft").resolve()


def xdg_data_home() -> Path:
    """Return the XDG data base, without creating it."""

    xdg = os.environ.get("XDG_DATA_HOME")
    return _resolved_path(xdg) if xdg else (Path.home() / ".local" / "share").resolve()


def xdg_state_home() -> Path:
    """Return Execraft's host-local durable state directory."""

    override = os.environ.get(STATE_HOME_ENV)
    if override:
        return _resolved_path(override)
    xdg = os.environ.get("XDG_STATE_HOME")
    base = _resolved_path(xdg) if xdg else Path.home() / ".local" / "state"
    return (base / "execraft").resolve()


def default_control_root() -> Path:
    """Return the installable default control-plane root."""

    return (xdg_data_home() / "execraft" / "control").resolve()


def _is_legacy_checkout(path: Path) -> bool:
    """Return whether *path* looks like an Execraft source-control home.

    The check intentionally requires both the package source and project
    catalog.  A random repository containing a directory named ``projects``
    must not be selected as the control plane.
    """

    return (path / "src" / "execraft").is_dir() and (path / "projects").is_dir()


def find_legacy_checkout(start: Path | None = None) -> Path | None:
    """Find a legacy Execraft checkout at or above *start*."""

    current = (start or Path.cwd()).expanduser().resolve()
    for candidate in (current, *current.parents):
        if _is_legacy_checkout(candidate):
            return candidate
    return None


def legacy_environment_root() -> tuple[str, Path] | None:
    """Return the first configured legacy root and its environment variable."""

    for variable in LEGACY_CONTROL_ROOT_ENVS:
        value = os.environ.get(variable)
        if value:
            return variable, _resolved_path(value)
    return None


@dataclass(frozen=True)
class ControlPlaneHome:
    """Resolved control-plane directories and discovery provenance."""

    root: Path
    origin: str
    environment_variable: str = ""

    @classmethod
    def resolve(cls, *, cwd: Path | None = None) -> "ControlPlaneHome":
        """Resolve the active control plane using explicit, legacy, then XDG rules.

        Resolution order is deliberately stable:

        1. ``EXECRAFT_CONTROL_ROOT`` (public explicit contract),
        2. ``EXECRAFT_WORKFLOW_ROOT`` / ``AI_WORKFLOW_ROOT`` (legacy aliases),
        3. a source checkout containing the current directory (developer mode),
        4. the XDG data home (normal installed-package mode).
        """

        explicit = os.environ.get(CONTROL_ROOT_ENV)
        if explicit:
            return cls(
                root=_resolved_path(explicit),
                origin="explicit_environment",
                environment_variable=CONTROL_ROOT_ENV,
            )

        legacy = legacy_environment_root()
        if legacy is not None:
            variable, path = legacy
            return cls(
                root=path,
                origin="legacy_environment",
                environment_variable=variable,
            )

        checkout = find_legacy_checkout(cwd)
        if checkout is not None:
            return cls(root=checkout, origin="legacy_checkout")

        return cls(root=default_control_root(), origin="xdg")

    @classmethod
    def xdg(cls) -> "ControlPlaneHome":
        """Return the normal installed-package home regardless of current cwd."""

        return cls(root=default_control_root(), origin="xdg")

    @property
    def projects_dir(self) -> Path:
        return self.root / "projects"

    @property
    def registry_dir(self) -> Path:
        """Return private mutable indexes that are not project descriptors."""

        return self.root / ".registry"

    @property
    def task_index_dir(self) -> Path:
        return self.registry_dir / "tasks"

    @property
    def workspace_index_dir(self) -> Path:
        return self.registry_dir / "workspaces"

    @property
    def active_task_path(self) -> Path:
        return self.registry_dir / "active-task"

    @property
    def config_dir(self) -> Path:
        return xdg_config_home()

    @property
    def state_dir(self) -> Path:
        return xdg_state_home()

    @property
    def is_legacy(self) -> bool:
        return self.origin.startswith("legacy")

    def ensure_layout(self) -> "ControlPlaneHome":
        """Create the minimal writable layout and return ``self``."""

        for directory in (
            self.root,
            self.projects_dir,
            self.registry_dir,
            self.config_dir,
            self.state_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def as_mapping(self) -> dict[str, str | bool]:
        return {
            "root": str(self.root),
            "origin": self.origin,
            "environment_variable": self.environment_variable,
            "projects": str(self.projects_dir),
            "registry": str(self.registry_dir),
            "config": str(self.config_dir),
            "state": str(self.state_dir),
            "legacy": self.is_legacy,
        }


def candidate_legacy_roots(*, cwd: Path | None = None) -> Iterable[Path]:
    """Yield unique legacy roots useful for explicit migration tooling."""

    seen: set[Path] = set()
    configured = legacy_environment_root()
    if configured is not None:
        _, path = configured
        if path not in seen:
            seen.add(path)
            yield path
    checkout = find_legacy_checkout(cwd)
    if checkout is not None and checkout not in seen:
        yield checkout
