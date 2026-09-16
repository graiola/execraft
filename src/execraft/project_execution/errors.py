"""Shared Project Execution exceptions without model/policy import cycles."""


class ProjectExecutionError(RuntimeError):
    """Base exception for invalid Project Execution operations."""


class ProjectExecutionNotFoundError(ProjectExecutionError):
    """Raised when a Project Execution definition or asset is missing."""


class ProjectExecutionConflictError(ProjectExecutionError):
    """Raised on optimistic-revision conflicts."""


__all__ = [
    "ProjectExecutionConflictError",
    "ProjectExecutionError",
    "ProjectExecutionNotFoundError",
]
