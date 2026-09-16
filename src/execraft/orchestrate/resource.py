"""Resource lifecycle manager with configurable watermarks.

Inventory and cleanup only ever touch resources that carry the Execraft
ownership marker (the Docker `execraft.managed=true` label, or a workspace's
`.execraft/workspace.yaml` marker file) — never a broad, unscoped sweep of the
host's Docker or filesystem state.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import yaml


class WorkspaceRetirement(Protocol):
    """Minimal structural contract for safe stale-workspace retirement."""

    def retire_stale_workspace(
        self, root: Path, *, dry_run: bool = False
    ) -> list[str]: ...


# Injectable so tests never invoke a real `docker` binary or daemon.
CommandRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]

MANAGED_LABEL = "execraft.managed=true"
WORKSPACE_MARKER = Path(".execraft") / "workspace.yaml"
PINNED_MARKER = Path(".execraft") / "pinned"


def _default_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=30, check=False
    )


def _nonblank_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


@dataclass
class ResourcePolicy:
    warning_percent: int = 85
    cleanup_percent: int = 90
    pause_percent: int = 95
    resume_percent: int = 75
    min_free_gb: float = 5.0
    docker_log_max_size_mb: int = 50
    docker_log_max_files: int = 5
    buildkit_max_cache_gb: float = 10.0
    buildkit_max_age_hours: int = 168
    managed_workspace_retention_days: int = 30
    preserve_pinned: bool = True

    def as_mapping(self) -> dict[str, Any]:
        return {
            "warning_percent": self.warning_percent,
            "cleanup_percent": self.cleanup_percent,
            "pause_percent": self.pause_percent,
            "resume_percent": self.resume_percent,
            "min_free_gb": self.min_free_gb,
            "docker_log_max_size_mb": self.docker_log_max_size_mb,
            "docker_log_max_files": self.docker_log_max_files,
            "buildkit_max_cache_gb": self.buildkit_max_cache_gb,
            "buildkit_max_age_hours": self.buildkit_max_age_hours,
            "managed_workspace_retention_days": self.managed_workspace_retention_days,
            "preserve_pinned": self.preserve_pinned,
        }

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "ResourcePolicy":
        return cls(
            warning_percent=int(data.get("warning_percent", 85)),
            cleanup_percent=int(data.get("cleanup_percent", 90)),
            pause_percent=int(data.get("pause_percent", 95)),
            resume_percent=int(data.get("resume_percent", 75)),
            min_free_gb=float(data.get("min_free_gb", 5.0)),
            docker_log_max_size_mb=int(data.get("docker_log_max_size_mb", 50)),
            docker_log_max_files=int(data.get("docker_log_max_files", 5)),
            buildkit_max_cache_gb=float(data.get("buildkit_max_cache_gb", 10.0)),
            buildkit_max_age_hours=int(data.get("buildkit_max_age_hours", 168)),
            managed_workspace_retention_days=int(
                data.get("managed_workspace_retention_days", 30)
            ),
            preserve_pinned=bool(data.get("preserve_pinned", True)),
        )


class DiskPressure(int):
    """Level of disk pressure: 0=ok, 1=warning, 2=cleanup, 3=pause."""


@dataclass
class ResourceInventory:
    filesystem_free_gb: float = 0.0
    filesystem_total_gb: float = 0.0
    filesystem_used_percent: int = 0
    docker_container_count: int = 0
    docker_image_count: int = 0
    docker_volume_count: int = 0
    managed_workspace_count: int = 0
    buildkit_cache_gb: float = 0.0

    @property
    def pressure(self) -> DiskPressure:
        percent = self.filesystem_used_percent
        if percent >= 95:
            return 3
        if percent >= 90:
            return 2
        if percent >= 85:
            return 1
        return 0


class ResourceManager:
    def __init__(
        self,
        policy: ResourcePolicy | None = None,
        *,
        runner: CommandRunner | None = None,
        min_cleanup_interval: float = 60.0,
        workspace_retirement: WorkspaceRetirement | None = None,
    ):
        self.policy = policy or ResourcePolicy()
        self._run = runner or _default_runner
        self._last_cleanup_ts: float = 0.0
        self._min_cleanup_interval = min_cleanup_interval
        self._workspace_retirement = workspace_retirement

    def inventory(self, workspace_roots: list[Path] | None = None) -> ResourceInventory:
        inv = ResourceInventory()
        stat = shutil.disk_usage(Path.home())
        inv.filesystem_total_gb = stat.total / (1024**3)
        inv.filesystem_free_gb = stat.free / (1024**3)
        inv.filesystem_used_percent = int(
            (stat.total - stat.free) / stat.total * 100
        ) if stat.total else 0

        if shutil.which("docker") is not None:
            inv.docker_container_count = self._docker_count(
                ["ps", "-a", "-q", "--filter", f"label={MANAGED_LABEL}"]
            )
            inv.docker_image_count = self._docker_count(
                ["images", "-q", "--filter", f"label={MANAGED_LABEL}"]
            )
            inv.docker_volume_count = self._docker_count(
                ["volume", "ls", "-q", "--filter", f"label={MANAGED_LABEL}"]
            )
            inv.buildkit_cache_gb = self._buildkit_cache_gb()

        for root in workspace_roots or []:
            if self._is_managed_workspace(root):
                inv.managed_workspace_count += 1

        return inv

    def needs_cleanup(self, inventory: ResourceInventory | None = None) -> bool:
        if inventory is None:
            now = datetime.now(timezone.utc).timestamp()
            if now - self._last_cleanup_ts < self._min_cleanup_interval:
                return False
            inv = self.inventory()
        else:
            inv = inventory
        low_free = (
            inv.filesystem_total_gb > 0
            and inv.filesystem_free_gb < self.policy.min_free_gb
        )
        if inv.filesystem_used_percent >= self.policy.cleanup_percent or low_free:
            return True
        if inv.buildkit_cache_gb > self.policy.buildkit_max_cache_gb:
            return True
        return False

    def needs_pause(self, inventory: ResourceInventory | None = None) -> bool:
        if inventory is None:
            now = datetime.now(timezone.utc).timestamp()
            if now - self._last_cleanup_ts < self._min_cleanup_interval:
                return False
            inv = self.inventory()
        else:
            inv = inventory
        low_free = (
            inv.filesystem_total_gb > 0
            and inv.filesystem_free_gb < self.policy.min_free_gb
        )
        return inv.filesystem_used_percent >= self.policy.pause_percent or low_free

    def can_resume(self, inventory: ResourceInventory | None = None) -> bool:
        inv = inventory or self.inventory()
        free_ok = (
            inv.filesystem_total_gb <= 0
            or inv.filesystem_free_gb >= self.policy.min_free_gb
        )
        return inv.filesystem_used_percent <= self.policy.resume_percent and free_ok

    def cleanup(
        self,
        workspace_roots: list[Path] | None = None,
        *,
        dry_run: bool = False,
    ) -> list[str]:
        """Remove managed workspaces past retention, plus idle managed
        Docker resources. Workspace directories are never deleted directly: stale
        candidates are delegated to :class:`WorkspaceLifecycleService`, which
        verifies archive, activity, Git, pin, and runtime ownership checks before
        using Git-aware worktree removal. Without that service, stale workspaces
        are preserved. Only Docker resources
        carrying the `execraft.managed=true` label are ever candidates, per
        PLAN's "Cleanup must default to managed resources only ... never
        use broad `docker system prune -a --volumes`"."""
        actions: list[str] = []
        now = datetime.now(timezone.utc)
        for root in workspace_roots or []:
            if not self._is_managed_workspace(root):
                continue
            created_at = self._workspace_created_at(root)
            is_stale = self._is_stale(created_at, now)
            if self.policy.preserve_pinned and self._is_pinned(root):
                if is_stale:
                    actions.append(
                        f"preserve stale pinned managed workspace: {root}"
                    )
                continue
            if is_stale:
                if self._workspace_retirement is None:
                    actions.append(
                        "preserve stale managed workspace because no safe lifecycle "
                        f"service is configured: {root}"
                    )
                else:
                    actions.extend(
                        self._workspace_retirement.retire_stale_workspace(
                            root, dry_run=dry_run
                        )
                    )
            else:
                caps = self._workspace_capabilities(root)
                if caps:
                    actions.extend(
                        self._cleanup_ros_colcon_trees(root, caps, dry_run=dry_run)
                    )

        if shutil.which("docker") is not None:
            actions.extend(self._cleanup_docker_containers(dry_run=dry_run))
            actions.extend(self._cleanup_docker_volumes(dry_run=dry_run))
            buildkit_actions = self._cleanup_buildkit_cache(dry_run=dry_run)
            if not actions or not all("no prune needed" in item for item in buildkit_actions):
                actions.extend(buildkit_actions)

        self._last_cleanup_ts = now.timestamp()
        return actions

    def _cleanup_docker_containers(self, *, dry_run: bool) -> list[str]:
        """Remove *exited* managed containers only. A running container may
        belong to an in-flight pipeline stage; PLAN's pressure-response
        policy is to "tear down idle runtime environments," not active
        ones, so a running container is never a candidate regardless of
        pressure."""
        actions: list[str] = []
        try:
            listing = self._run(
                [
                    "docker", "ps", "-a", "-q",
                    "--filter", f"label={MANAGED_LABEL}",
                    "--filter", "status=exited",
                ]
            )
        except (OSError, subprocess.SubprocessError):
            return actions
        if listing.returncode != 0:
            return actions

        for container_id in _nonblank_lines(listing.stdout):
            if dry_run:
                actions.append(f"would remove exited managed container: {container_id}")
                continue
            try:
                removed = self._run(["docker", "rm", container_id])
            except (OSError, subprocess.SubprocessError):
                continue
            if removed.returncode == 0:
                actions.append(f"removed exited managed container: {container_id}")
        return actions

    def _cleanup_docker_volumes(self, *, dry_run: bool) -> list[str]:
        """Remove managed volumes that are not currently attached to any
        container. Docker itself refuses to remove an in-use volume, so
        this only ever reports an action once `docker volume rm` actually
        succeeds — an in-use volume is silently left alone rather than
        being reported as removed when it was not."""
        actions: list[str] = []
        try:
            listing = self._run(
                ["docker", "volume", "ls", "-q", "--filter", f"label={MANAGED_LABEL}"]
            )
        except (OSError, subprocess.SubprocessError):
            return actions
        if listing.returncode != 0:
            return actions

        for volume_name in _nonblank_lines(listing.stdout):
            if dry_run:
                actions.append(f"would remove unused managed volume: {volume_name}")
                continue
            try:
                removed = self._run(["docker", "volume", "rm", volume_name])
            except (OSError, subprocess.SubprocessError):
                continue
            if removed.returncode == 0:
                actions.append(f"removed unused managed volume: {volume_name}")
        return actions

    def _buildkit_cache_gb(self) -> float:
        try:
            result = self._run(
                ["docker", "buildx", "du", "--json"]
            )
        except (OSError, subprocess.SubprocessError):
            return 0.0
        if result.returncode != 0 or not result.stdout.strip():
            return 0.0
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return 0.0
        total_bytes = 0
        for entry in data if isinstance(data, list) else data.get("layers", []):
            total_bytes += entry.get("disk_usage_bytes", 0)
            total_bytes += entry.get("cache_size_bytes", 0)
        return total_bytes / (1024**3)

    def _cleanup_buildkit_cache(self, *, dry_run: bool) -> list[str]:
        current = self._buildkit_cache_gb()
        if current <= self.policy.buildkit_max_cache_gb:
            if dry_run:
                return [f"buildkit cache {current:.1f}GiB below limit {self.policy.buildkit_max_cache_gb:.1f}GiB, no prune needed"]
            return []

        if dry_run:
            return [f"would prune buildkit cache ({current:.1f}GiB > {self.policy.buildkit_max_cache_gb:.1f}GiB limit)"]

        try:
            result = self._run(
                [
                    "docker",
                    "buildx",
                    "prune",
                    "--force",
                    "--keep-storage",
                    f"{self.policy.buildkit_max_cache_gb:g}GB",
                    "--filter",
                    f"until={self.policy.buildkit_max_age_hours}h",
                ]
            )
        except (OSError, subprocess.SubprocessError):
            return ["buildkit cache prune failed (command error)"]

        if result.returncode != 0:
            return [f"buildkit cache prune exited with code {result.returncode}"]
        after = self._buildkit_cache_gb()
        saved = current - after
        return [
            f"pruned buildkit cache, saved {saved:.1f}GiB (was {current:.1f}GiB, now {after:.1f}GiB)"
        ]

    def _docker_count(self, args: list[str]) -> int:
        try:
            result = self._run(["docker", *args])
        except (OSError, subprocess.SubprocessError):
            return 0
        if result.returncode != 0:
            return 0
        return len([line for line in result.stdout.splitlines() if line.strip()])

    def _is_managed_workspace(self, root: Path) -> bool:
        return (root / WORKSPACE_MARKER).is_file()

    def _is_pinned(self, root: Path) -> bool:
        return (root / PINNED_MARKER).exists()

    def _read_workspace_data(self, root: Path) -> dict[str, Any]:
        marker = root / WORKSPACE_MARKER
        try:
            return yaml.safe_load(marker.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {}

    def _workspace_created_at(self, root: Path) -> str:
        data = self._read_workspace_data(root)
        return str(data.get("created_at", ""))

    def _workspace_capabilities(self, root: Path) -> frozenset[str]:
        data = self._read_workspace_data(root)
        raw = data.get("capabilities") or []
        if not isinstance(raw, list):
            return frozenset()
        return frozenset(str(c) for c in raw)

    def _cleanup_ros_colcon_trees(
        self, root: Path, capabilities: frozenset[str], *, dry_run: bool
    ) -> list[str]:
        actions: list[str] = []
        has_ros = "ros" in capabilities
        has_colcon = "colcon" in capabilities
        if not has_ros and not has_colcon:
            return actions

        subdirs = []
        if has_colcon:
            subdirs.extend(["build", "install", "log"])
        if has_ros and not has_colcon:
            subdirs.append("log")

        for name in subdirs:
            target = root / name
            if not target.is_dir():
                continue
            actions.append(f"remove {name} tree: {target}")
            if not dry_run:
                shutil.rmtree(target, ignore_errors=True)
        return actions

    def _is_stale(self, created_at: str, now: datetime) -> bool:
        if not created_at:
            return False
        try:
            created = datetime.fromisoformat(created_at)
        except ValueError:
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_days = (now - created).total_seconds() / 86400
        return age_days >= self.policy.managed_workspace_retention_days
