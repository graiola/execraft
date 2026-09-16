"""Repository synchronization primitives.

The package keeps declarative models eager and Git execution lazy.  Core
orchestration models import :mod:`execraft.repository_sync.spec`; eagerly importing
the service here would unnecessarily pull workspace/Git execution into that
low-level model dependency and make circular imports much easier to introduce.
"""

from __future__ import annotations

from typing import Any

from .policy import RepositorySyncPolicy
from .spec import RepositorySyncSpec, RepositorySyncSpecError, RepositorySyncTarget
from .transaction import (
    RepositorySyncRepositoryState,
    RepositorySyncTransaction,
    RepositorySyncTransactionError,
    RepositorySyncTransactionStore,
)

_LAZY_SERVICE_EXPORTS = {
    "RepositoryDivergence",
    "RepositorySyncError",
    "RepositorySyncPrepareResult",
    "RepositorySyncService",
}


def __getattr__(name: str) -> Any:
    if name not in _LAZY_SERVICE_EXPORTS:
        raise AttributeError(name)
    from . import service

    return getattr(service, name)


__all__ = [
    "RepositoryDivergence",
    "RepositorySyncPolicy",
    "RepositorySyncError",
    "RepositorySyncPrepareResult",
    "RepositorySyncRepositoryState",
    "RepositorySyncService",
    "RepositorySyncSpec",
    "RepositorySyncSpecError",
    "RepositorySyncTarget",
    "RepositorySyncTransaction",
    "RepositorySyncTransactionError",
    "RepositorySyncTransactionStore",
]
