from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """Result of probing whether the browser adapter is available."""

    available: bool
    browser_version: str | None = None
    message: str = ""


@dataclass
class PrepareResult:
    """Result of preparing an archive for browser-based execution."""

    run_id: str
    archive_path: Path
    bundle_manifest: dict[str, Any]


@dataclass
class ExecuteResult:
    """Result of executing a browser-based interaction."""

    run_id: str
    success: bool
    changed_files: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""


@dataclass
class RunStatus:
    """Status of a browser agent run."""

    run_id: str
    state: str  # pending, running, completed, failed
    message: str = ""


@dataclass
class ApplyResult:
    """Result of applying file changes from a browser run."""

    run_id: str
    files_applied: list[Path] = field(default_factory=list)
    files_backed_up: list[Path] = field(default_factory=list)
    verification_passed: bool = False
    handoff_updated: bool = False
    message: str = ""


# ---------------------------------------------------------------------------
# Transport-neutral adapter interface
# ---------------------------------------------------------------------------


class BrowserAdapter(ABC):
    """Abstract interface for browser-based AI agent transports."""

    @abstractmethod
    async def login(
        self, profile_dir: Path | None = None
    ) -> dict[str, Any]:
        """Authenticate with the browser-based service.

        Returns authentication state/metadata.
        """

    @abstractmethod
    async def probe(self) -> ProbeResult:
        """Check if the browser adapter is available and working."""

    @abstractmethod
    async def prepare(
        self, archive_path: Path, bundle_manifest: dict[str, Any]
    ) -> PrepareResult:
        """Prepare an archive for browser-based execution."""

    @abstractmethod
    async def execute(self, run_id: str) -> ExecuteResult:
        """Execute a browser-based interaction for the given run."""

    @abstractmethod
    async def status(self, run_id: str) -> RunStatus:
        """Check the status of a browser agent run."""



def sha256_data(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
