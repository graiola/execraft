"""Domain-owned GUI HTTP route dispatch."""

from .router import GuiApiRouter, RouteNotFound

__all__ = ["GuiApiRouter", "RouteNotFound"]
