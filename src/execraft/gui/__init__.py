"""Local web control center for Execraft orchestration and onboarding."""

from .application import ActiveTaskRef, ControlCenterError, ControlCenterService
from .server import DashboardService, GuiError, serve_dashboard

__all__ = [
    "ActiveTaskRef",
    "ControlCenterError",
    "ControlCenterService",
    "DashboardService",
    "GuiError",
    "serve_dashboard",
]
