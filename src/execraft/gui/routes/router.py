"""Composition root for GUI API route groups."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import dashboard, exports, onboarding, project_execution, roadmaps, runtime


@dataclass(frozen=True)
class RouteGroup:
    """One domain-owned pair of HTTP-independent dispatch functions."""

    get: Callable[[object, str, Mapping[str, list[str]]], Any]
    post: Callable[[object, str, Mapping[str, Any]], Any]


class RouteNotFound(LookupError):
    """Raised when no GUI API route owns the requested path."""


class GuiApiRouter:
    """Delegate HTTP API operations to domain-owned route modules."""

    protected_get_paths = dashboard.PROTECTED_GET_PATHS | exports.PROTECTED_GET_PATHS

    def __init__(
        self,
        service: object,
        route_groups: Sequence[RouteGroup] | None = None,
    ):
        self._service = service
        self._route_groups = tuple(
            route_groups
            or (
                RouteGroup(onboarding.dispatch_get, onboarding.dispatch_post),
                RouteGroup(exports.dispatch_get, exports.dispatch_post),
                RouteGroup(project_execution.dispatch_get, project_execution.dispatch_post),
                RouteGroup(roadmaps.dispatch_get, roadmaps.dispatch_post),
                RouteGroup(runtime.dispatch_get, runtime.dispatch_post),
                RouteGroup(dashboard.dispatch_get, dashboard.dispatch_post),
            )
        )

    def get(self, path: str, query: Mapping[str, list[str]]) -> Any:
        for group in self._route_groups:
            value = group.get(self._service, path, query)
            if value is not onboarding._MISSING:
                return value
        raise RouteNotFound(path)

    def post(self, path: str, payload: Mapping[str, Any]) -> Any:
        for group in self._route_groups:
            value = group.post(self._service, path, payload)
            if value is not onboarding._MISSING:
                return value
        raise RouteNotFound(path)
