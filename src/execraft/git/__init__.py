"""Typed boundary for bounded Git command execution."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class GitPolicy:
    timeout_seconds: float = 30.0
    check: bool = True


@dataclass(frozen=True)
class GitRequest:
    repository: Path
    arguments: tuple[str, ...]
    policy: GitPolicy = GitPolicy()
    environment: Mapping[str, str] | None = None


@dataclass(frozen=True)
class GitResult:
    command: tuple[str, ...]
    repository: Path
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class GitError(RuntimeError):
    """Base error for Git execution failures."""


class GitCommandError(GitError):
    def __init__(self, result: GitResult) -> None:
        self.result = result
        detail = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
        super().__init__(
            f"Git command failed ({result.returncode}) in {result.repository}: "
            f"{' '.join(result.command)}" + (f"\n{detail}" if detail else "")
        )


class GitTimeoutError(GitError):
    """Raised when a Git command exceeds its explicit timeout."""


class GitUnavailableError(GitError):
    """Raised when Git cannot be started."""


class GitClient:
    """Execute Git requests with explicit policy, results, and failure types."""

    def execute(self, request: GitRequest) -> GitResult:
        repository = request.repository.expanduser().resolve()
        command = ("git", *request.arguments)
        environment = os.environ.copy()
        if request.environment:
            environment.update(request.environment)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            completed = subprocess.run(
                command,
                cwd=repository,
                check=False,
                text=True,
                capture_output=True,
                timeout=request.policy.timeout_seconds,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitTimeoutError(
                f"Git command timed out after {request.policy.timeout_seconds:g}s in "
                f"{repository}: {' '.join(command)}"
            ) from exc
        except OSError as exc:
            raise GitUnavailableError(f"cannot execute Git: {exc}") from exc
        result = GitResult(
            command=command,
            repository=repository,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
        if request.policy.check and not result.ok:
            raise GitCommandError(result)
        return result

    def run(
        self,
        repository: Path,
        arguments: Sequence[str],
        *,
        check: bool = True,
        timeout_seconds: float = 30.0,
        environment: Mapping[str, str] | None = None,
    ) -> GitResult:
        return self.execute(
            GitRequest(
                repository=repository,
                arguments=tuple(arguments),
                policy=GitPolicy(timeout_seconds=timeout_seconds, check=check),
                environment=environment,
            )
        )


__all__ = [
    "GitClient",
    "GitCommandError",
    "GitError",
    "GitPolicy",
    "GitRequest",
    "GitResult",
    "GitTimeoutError",
    "GitUnavailableError",
]
