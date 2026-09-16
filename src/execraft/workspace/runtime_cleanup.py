"""Exact, ownership-scoped shutdown for task runtime resources."""

from __future__ import annotations

import shutil
import subprocess
from typing import Callable

from execraft.workspace.lifecycle_types import RuntimeStopResult
from execraft.workspace.task_git import TaskGitError
from execraft.workspace.workspace_git import WorkspaceRecord

MANAGED_LABEL = "execraft.managed=true"
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"

CommandRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]
DockerLocator = Callable[[str], str | None]


def _default_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


class DockerRuntimeController:
    """Stop only Docker resources owned by one recorded Compose project.

    Containers must carry both the exact Compose project label and Execraft's
    ownership label. Networks are selected by the exact Compose project label
    after all owned containers have been removed. Volumes are intentionally
    retained, matching ``docker compose down`` without ``--volumes``.
    """

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        locator: DockerLocator | None = None,
    ) -> None:
        self._run = runner or _default_runner
        self._locate = locator or shutil.which

    def stop(self, record: WorkspaceRecord, *, dry_run: bool = False) -> RuntimeStopResult:
        capabilities = set(record.capabilities)
        compose_enabled = "runtime.compose" in capabilities or (
            not capabilities and bool(record.compose_project)
        )
        if not compose_enabled or not record.compose_project:
            return RuntimeStopResult(
                actions=(f"runtime stop not required for workspace {record.task_id}",),
                skipped=True,
            )
        if self._locate("docker") is None:
            raise TaskGitError(
                "Docker is unavailable; runtime ownership cannot be verified or stopped"
            )

        project = record.compose_project
        all_containers = self._list_ids(
            "ps",
            "-a",
            "-q",
            "--filter",
            f"label={COMPOSE_PROJECT_LABEL}={project}",
        )
        managed_containers = self._list_ids(
            "ps",
            "-a",
            "-q",
            "--filter",
            f"label={COMPOSE_PROJECT_LABEL}={project}",
            "--filter",
            f"label={MANAGED_LABEL}",
        )
        unowned = sorted(set(all_containers) - set(managed_containers))
        if unowned:
            raise TaskGitError(
                "refusing to stop Compose resources without Execraft ownership labels: "
                + ", ".join(unowned)
            )

        networks = self._list_ids(
            "network",
            "ls",
            "-q",
            "--filter",
            f"label={COMPOSE_PROJECT_LABEL}={project}",
        )
        actions: list[str] = []
        stopped: list[str] = []
        removed_networks: list[str] = []

        for container_id in managed_containers:
            actions.append(f"stop managed runtime container: {container_id}")
            if dry_run:
                continue
            self._require_success(
                ["docker", "rm", "-f", container_id],
                purpose=f"stop managed runtime container {container_id}",
            )
            stopped.append(container_id)

        for network_id in networks:
            actions.append(f"remove Compose network: {network_id}")
            if dry_run:
                continue
            self._require_success(
                ["docker", "network", "rm", network_id],
                purpose=f"remove Compose network {network_id}",
            )
            removed_networks.append(network_id)

        if not dry_run:
            remaining = self._list_ids(
                "ps",
                "-a",
                "-q",
                "--filter",
                f"label={COMPOSE_PROJECT_LABEL}={project}",
            )
            if remaining:
                raise TaskGitError(
                    "runtime shutdown left Compose containers running: " + ", ".join(remaining)
                )
        if not actions:
            actions.append(f"no runtime resources found for Compose project {project}")
        return RuntimeStopResult(
            actions=tuple(actions),
            stopped_containers=tuple(stopped),
            removed_networks=tuple(removed_networks),
        )

    def _list_ids(self, *args: str) -> list[str]:
        result = self._require_success(
            ["docker", *args],
            purpose="inspect task-owned Docker resources",
        )
        return _lines(result.stdout)

    def _require_success(
        self,
        args: list[str],
        *,
        purpose: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self._run(args)
        except (OSError, subprocess.SubprocessError) as exc:
            raise TaskGitError(f"cannot {purpose}: {exc}") from exc
        if result.returncode != 0:
            stderr = (getattr(result, "stderr", "") or "").strip()
            detail = f": {stderr}" if stderr else ""
            raise TaskGitError(f"cannot {purpose}{detail}")
        return result
