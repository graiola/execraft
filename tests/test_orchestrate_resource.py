"""Tests for resource lifecycle manager."""

import json
import shutil
from pathlib import Path

import pytest
import yaml

from execraft.orchestrate.resource import (
    ResourceInventory,
    ResourceManager,
    ResourcePolicy,
)


class TestResourcePolicy:
    def test_default_policy(self):
        policy = ResourcePolicy()
        assert policy.warning_percent == 85
        assert policy.cleanup_percent == 90
        assert policy.pause_percent == 95
        assert policy.resume_percent == 75
        assert policy.min_free_gb == 5.0

    def test_as_mapping_roundtrip(self):
        original = ResourcePolicy(
            warning_percent=80,
            cleanup_percent=88,
            pause_percent=96,
            resume_percent=70,
            min_free_gb=10.0,
        )
        mapping = original.as_mapping()
        restored = ResourcePolicy.from_mapping(mapping)
        assert restored.warning_percent == 80
        assert restored.cleanup_percent == 88
        assert restored.min_free_gb == 10.0


class TestResourceInventory:
    def test_pressure_ok(self):
        inv = ResourceInventory(filesystem_used_percent=50)
        assert inv.pressure == 0

    def test_pressure_warning(self):
        inv = ResourceInventory(filesystem_used_percent=87)
        assert inv.pressure == 1

    def test_pressure_cleanup(self):
        inv = ResourceInventory(filesystem_used_percent=92)
        assert inv.pressure == 2

    def test_pressure_pause(self):
        inv = ResourceInventory(filesystem_used_percent=97)
        assert inv.pressure == 3

    def test_pressure_zero_when_no_data(self):
        inv = ResourceInventory(filesystem_total_gb=0)
        assert inv.pressure == 0


class TestResourceManager:
    def test_default_policy(self):
        mgr = ResourceManager()
        assert mgr.policy.warning_percent == 85

    def test_custom_policy(self):
        policy = ResourcePolicy(warning_percent=90, cleanup_percent=95)
        mgr = ResourceManager(policy=policy)
        assert mgr.policy.warning_percent == 90
        assert mgr.policy.cleanup_percent == 95

    def test_inventory_returns_basic_metrics(self):
        mgr = ResourceManager()
        inv = mgr.inventory()
        assert inv.filesystem_total_gb > 0
        assert inv.filesystem_free_gb > 0

    def test_needs_cleanup_false_by_default(self):
        mgr = ResourceManager()
        inv = ResourceInventory(filesystem_used_percent=50)
        assert mgr.needs_cleanup(inv) is False

    def test_needs_cleanup_true(self):
        mgr = ResourceManager()
        inv = ResourceInventory(filesystem_used_percent=92)
        assert mgr.needs_cleanup(inv) is True

    def test_needs_pause_true(self):
        mgr = ResourceManager()
        inv = ResourceInventory(filesystem_used_percent=97)
        assert mgr.needs_pause(inv) is True

    def test_can_resume_true_when_below_threshold(self):
        mgr = ResourceManager()
        inv = ResourceInventory(filesystem_used_percent=70)
        assert mgr.can_resume(inv) is True

    def test_can_resume_false_when_above_threshold(self):
        mgr = ResourceManager(
            policy=ResourcePolicy(resume_percent=60)
        )
        inv = ResourceInventory(filesystem_used_percent=70)
        assert mgr.can_resume(inv) is False

    def test_cleanup_returns_actions(self):
        mgr = ResourceManager()
        actions = mgr.cleanup()
        assert isinstance(actions, list)

    def test_cleanup_dry_run(self):
        mgr = ResourceManager()
        actions = mgr.cleanup(dry_run=True)
        assert isinstance(actions, list)


class _FakeCompletedProcess:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


class TestResourceManagerDockerInventory:
    def test_docker_inventory_counts_only_managed_label(self, monkeypatch):
        calls = []

        def runner(args):
            calls.append(args)
            if args[1] == "ps":
                return _FakeCompletedProcess("id1\nid2\n")
            if args[1] == "images":
                return _FakeCompletedProcess("img1\n")
            if args[1] == "volume":
                return _FakeCompletedProcess("")
            return _FakeCompletedProcess("")

        monkeypatch.setattr(
            "execraft.orchestrate.resource.shutil.which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        mgr = ResourceManager(runner=runner)
        inv = mgr.inventory()

        assert inv.docker_container_count == 2
        assert inv.docker_image_count == 1
        assert inv.docker_volume_count == 0
        docker_calls = [c for c in calls if c[:3] != ["docker", "buildx", "du"]]
        assert all("execraft.managed=true" in " ".join(call) for call in docker_calls)

    def test_docker_inventory_degrades_to_zero_on_command_failure(self, monkeypatch):
        def failing_runner(args):
            return _FakeCompletedProcess("", returncode=1)

        monkeypatch.setattr(
            "execraft.orchestrate.resource.shutil.which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        mgr = ResourceManager(runner=failing_runner)
        inv = mgr.inventory()
        assert inv.docker_container_count == 0

    def test_docker_inventory_skipped_when_docker_absent(self, monkeypatch):
        def unreachable_runner(args):
            raise AssertionError("docker should not be invoked when absent")

        monkeypatch.setattr(
            "execraft.orchestrate.resource.shutil.which", lambda name: None
        )
        mgr = ResourceManager(runner=unreachable_runner)
        inv = mgr.inventory()
        assert inv.docker_container_count == 0




class _FakeWorkspaceRetirement:
    def __init__(self):
        self.calls = []

    def retire_stale_workspace(self, root, *, dry_run=False):
        self.calls.append((Path(root), dry_run))
        if not dry_run:
            shutil.rmtree(root)
        return [f"{'would ' if dry_run else ''}retire stale managed workspace: {root}"]


class TestResourceManagerWorkspaceLifecycle:
    def _make_managed_workspace(self, tmp_path, name, *, created_at, pinned=False):
        root = tmp_path / name
        (root / ".execraft").mkdir(parents=True)
        (root / ".execraft" / "workspace.yaml").write_text(
            f"created_at: '{created_at}'\n", encoding="utf-8"
        )
        if pinned:
            (root / ".execraft" / "pinned").touch()
        return root

    def test_inventory_counts_only_marked_workspaces(self, tmp_path):
        managed = self._make_managed_workspace(
            tmp_path, "managed", created_at="2020-01-01T00:00:00+00:00"
        )
        unmanaged = tmp_path / "unmanaged"
        unmanaged.mkdir()

        mgr = ResourceManager()
        inv = mgr.inventory(workspace_roots=[managed, unmanaged])
        assert inv.managed_workspace_count == 1

    def test_cleanup_preserves_stale_workspace_without_lifecycle_service(self, tmp_path):
        stale = self._make_managed_workspace(
            tmp_path, "stale", created_at="2020-01-01T00:00:00+00:00"
        )
        mgr = ResourceManager()
        actions = mgr.cleanup(workspace_roots=[stale])
        assert actions == [
            "preserve stale managed workspace because no safe lifecycle "
            f"service is configured: {stale}"
        ]
        assert stale.exists()

    def test_cleanup_delegates_stale_workspace_to_lifecycle_service(self, tmp_path):
        stale = self._make_managed_workspace(
            tmp_path, "stale", created_at="2020-01-01T00:00:00+00:00"
        )
        retirement = _FakeWorkspaceRetirement()
        mgr = ResourceManager(workspace_retirement=retirement)
        actions = mgr.cleanup(workspace_roots=[stale])
        assert actions == [f"retire stale managed workspace: {stale}"]
        assert retirement.calls == [(stale, False)]
        assert not stale.exists()

    def test_cleanup_preserves_fresh_managed_workspace(self, tmp_path):
        from datetime import datetime, timezone
        fresh = self._make_managed_workspace(
            tmp_path, "fresh", created_at=datetime.now(timezone.utc).isoformat()
        )
        mgr = ResourceManager()
        actions = mgr.cleanup(workspace_roots=[fresh])
        assert actions == []
        assert fresh.exists()

    def test_cleanup_preserves_pinned_workspace_even_if_stale(self, tmp_path):
        pinned = self._make_managed_workspace(
            tmp_path, "pinned", created_at="2020-01-01T00:00:00+00:00", pinned=True
        )
        mgr = ResourceManager()
        actions = mgr.cleanup(workspace_roots=[pinned])
        assert actions == [f"preserve stale pinned managed workspace: {pinned}"]
        assert pinned.exists()

    def test_cleanup_never_touches_unmanaged_directory(self, tmp_path):
        unmanaged = tmp_path / "unmanaged"
        unmanaged.mkdir()
        mgr = ResourceManager()
        actions = mgr.cleanup(workspace_roots=[unmanaged])
        assert actions == []
        assert unmanaged.exists()

    def test_cleanup_dry_run_delegates_without_deleting(self, tmp_path):
        stale = self._make_managed_workspace(
            tmp_path, "stale", created_at="2020-01-01T00:00:00+00:00"
        )
        retirement = _FakeWorkspaceRetirement()
        mgr = ResourceManager(workspace_retirement=retirement)
        actions = mgr.cleanup(workspace_roots=[stale], dry_run=True)
        assert actions == [f"would retire stale managed workspace: {stale}"]
        assert retirement.calls == [(stale, True)]
        assert stale.exists()


def _buildx_du_ok_runner(args):
    """Helper: when buildx du is called, return zero cache."""
    if args[1:3] == ["buildx", "du"]:
        return _FakeCompletedProcess("[]")  # empty list = no cache
    return None


class TestResourceManagerDockerCleanup:
    def test_cleanup_removes_exited_managed_containers_only(self, monkeypatch):
        calls = []

        def runner(args):
            calls.append(args)
            handled = _buildx_du_ok_runner(args)
            if handled is not None:
                return handled
            if args[:2] == ["docker", "ps"]:
                assert "status=exited" in " ".join(args)
                assert "execraft.managed=true" in " ".join(args)
                return _FakeCompletedProcess("c1\nc2\n")
            if args[:2] == ["docker", "rm"]:
                return _FakeCompletedProcess("")
            if args[:3] == ["docker", "volume", "ls"]:
                return _FakeCompletedProcess("")
            return _FakeCompletedProcess("")

        monkeypatch.setattr(
            "execraft.orchestrate.resource.shutil.which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        mgr = ResourceManager(runner=runner)
        actions = mgr.cleanup()

        assert "removed exited managed container: c1" in actions
        assert "removed exited managed container: c2" in actions
        rm_calls = [c for c in calls if c[:2] == ["docker", "rm"]]
        assert sorted(c[2] for c in rm_calls) == ["c1", "c2"]

    def test_cleanup_never_removes_a_running_container(self, monkeypatch):
        # A running container is filtered out at the `docker ps` query
        # level (--filter status=exited), so this asserts the query shape
        # itself, not just the (already-filtered) result.
        seen_filters = []

        def runner(args):
            if args[:2] == ["docker", "ps"]:
                seen_filters.append(args)
                return _FakeCompletedProcess("")  # nothing exited
            return _FakeCompletedProcess("")

        monkeypatch.setattr(
            "execraft.orchestrate.resource.shutil.which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        mgr = ResourceManager(runner=runner)
        mgr.cleanup()

        assert any("status=exited" in " ".join(c) for c in seen_filters)

    def test_cleanup_dry_run_reports_without_removing_containers(self, monkeypatch):
        rm_calls = []

        def runner(args):
            if args[:2] == ["docker", "ps"]:
                return _FakeCompletedProcess("c1\n")
            if args[:2] == ["docker", "rm"]:
                rm_calls.append(args)
                return _FakeCompletedProcess("")
            return _FakeCompletedProcess("")

        monkeypatch.setattr(
            "execraft.orchestrate.resource.shutil.which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        mgr = ResourceManager(runner=runner)
        actions = mgr.cleanup(dry_run=True)

        assert "would remove exited managed container: c1" in actions
        assert rm_calls == []

    def test_cleanup_removes_unused_managed_volumes(self, monkeypatch):
        def runner(args):
            if args[:2] == ["docker", "ps"]:
                return _FakeCompletedProcess("")
            if args[:3] == ["docker", "volume", "ls"]:
                return _FakeCompletedProcess("vol1\nvol2\n")
            if args[:3] == ["docker", "volume", "rm"]:
                return _FakeCompletedProcess("")
            return _FakeCompletedProcess("")

        monkeypatch.setattr(
            "execraft.orchestrate.resource.shutil.which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        mgr = ResourceManager(runner=runner)
        actions = mgr.cleanup()

        assert "removed unused managed volume: vol1" in actions
        assert "removed unused managed volume: vol2" in actions

    def test_cleanup_does_not_report_an_in_use_volume_as_removed(self, monkeypatch):
        # docker refuses to remove an in-use volume (nonzero returncode);
        # that must not be reported as if it succeeded.
        def runner(args):
            if args[:2] == ["docker", "ps"]:
                return _FakeCompletedProcess("")
            if args[:3] == ["docker", "volume", "ls"]:
                return _FakeCompletedProcess("in-use-vol\n")
            if args[:3] == ["docker", "volume", "rm"]:
                return _FakeCompletedProcess("Error: volume is in use", returncode=1)
            return _FakeCompletedProcess("")

        monkeypatch.setattr(
            "execraft.orchestrate.resource.shutil.which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        mgr = ResourceManager(runner=runner)
        actions = mgr.cleanup()

        assert actions == []

    def test_cleanup_skips_docker_entirely_when_absent(self, monkeypatch):
        def unreachable_runner(args):
            raise AssertionError("docker should not be invoked when absent")

        monkeypatch.setattr(
            "execraft.orchestrate.resource.shutil.which", lambda name: None
        )
        mgr = ResourceManager(runner=unreachable_runner)
        actions = mgr.cleanup()
        assert actions == []


class TestResourceManagerBuildKit:
    def test_buildkit_cache_zero_when_command_fails(self, monkeypatch):
        def failing_runner(args):
            return _FakeCompletedProcess("", returncode=1)

        monkeypatch.setattr(
            "execraft.orchestrate.resource.shutil.which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        mgr = ResourceManager(runner=failing_runner)
        inv = mgr.inventory()
        assert inv.buildkit_cache_gb == 0.0

    def test_buildkit_cache_zero_when_no_docker(self, monkeypatch):
        monkeypatch.setattr(
            "execraft.orchestrate.resource.shutil.which", lambda name: None
        )
        mgr = ResourceManager()
        inv = mgr.inventory()
        assert inv.buildkit_cache_gb == 0.0

    def test_buildkit_cache_parses_json_empty(self, monkeypatch):
        def runner(args):
            if args[1:3] == ["buildx", "du"]:
                return _FakeCompletedProcess("[]")
            if args[1] == "ps":
                return _FakeCompletedProcess("")
            if args[1] == "images":
                return _FakeCompletedProcess("")
            if args[1] == "volume":
                return _FakeCompletedProcess("")
            return _FakeCompletedProcess("")

        monkeypatch.setattr(
            "execraft.orchestrate.resource.shutil.which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        mgr = ResourceManager(runner=runner)
        inv = mgr.inventory()
        assert inv.buildkit_cache_gb == 0.0

    def test_buildkit_cache_parses_json_with_layers(self, monkeypatch):
        def runner(args):
            if args[1:3] == ["buildx", "du"]:
                payload = json.dumps([
                    {"disk_usage_bytes": 2_000_000_000, "cache_size_bytes": 500_000_000},
                    {"disk_usage_bytes": 1_000_000_000, "cache_size_bytes": 0},
                ])
                return _FakeCompletedProcess(payload)
            if args[1] == "ps":
                return _FakeCompletedProcess("")
            if args[1] == "images":
                return _FakeCompletedProcess("")
            if args[1] == "volume":
                return _FakeCompletedProcess("")
            return _FakeCompletedProcess("")

        monkeypatch.setattr(
            "execraft.orchestrate.resource.shutil.which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        mgr = ResourceManager(runner=runner)
        inv = mgr.inventory()
        assert inv.buildkit_cache_gb == pytest.approx(
            3_500_000_000 / (1024**3), abs=0.01
        )

    def test_needs_cleanup_true_when_buildkit_cache_exceeds_limit(self, monkeypatch):
        def runner(args):
            if args[1:3] == ["buildx", "du"]:
                payload = json.dumps([
                    {"disk_usage_bytes": 15_000_000_000, "cache_size_bytes": 0},
                ])
                return _FakeCompletedProcess(payload)
            if args[1] == "ps":
                return _FakeCompletedProcess("")
            if args[1] == "images":
                return _FakeCompletedProcess("")
            if args[1] == "volume":
                return _FakeCompletedProcess("")
            return _FakeCompletedProcess("")

        monkeypatch.setattr(
            "execraft.orchestrate.resource.shutil.which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        mgr = ResourceManager(runner=runner, min_cleanup_interval=0)
        assert mgr.needs_cleanup() is True

    def test_cleanup_prunes_buildkit_cache_when_over_limit(self, monkeypatch):
        calls = []

        def runner(args):
            calls.append(args)
            if args[1:3] == ["buildx", "du"]:
                if not any("prune" in c for c in calls):
                    payload = json.dumps([
                        {"disk_usage_bytes": 15_000_000_000, "cache_size_bytes": 0},
                    ])
                    return _FakeCompletedProcess(payload)
                payload = json.dumps([
                    {"disk_usage_bytes": 2_000_000_000, "cache_size_bytes": 0},
                ])
                return _FakeCompletedProcess(payload)
            if args[1] == "ps":
                return _FakeCompletedProcess("")
            if args[1] == "images":
                return _FakeCompletedProcess("")
            if args[1] == "volume":
                return _FakeCompletedProcess("")
            return _FakeCompletedProcess("")

        monkeypatch.setattr(
            "execraft.orchestrate.resource.shutil.which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        mgr = ResourceManager(runner=runner)
        actions = mgr.cleanup()
        assert any("pruned buildkit cache" in a for a in actions)
        assert any("prune" in c for c in calls)

    def test_cleanup_skips_buildkit_prune_when_below_limit(self, monkeypatch):
        calls = []

        def runner(args):
            calls.append(args)
            if args[1:3] == ["buildx", "du"]:
                return _FakeCompletedProcess("[]")
            return _FakeCompletedProcess("")

        monkeypatch.setattr(
            "execraft.orchestrate.resource.shutil.which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        mgr = ResourceManager(runner=runner)
        actions = mgr.cleanup()
        assert not any("prune" in c for c in calls)
        assert ("pruned" not in " ".join(actions))

    def test_buildkit_dry_run_always_reports_status(self, monkeypatch):
        calls = []

        def runner(args):
            calls.append(args)
            if args[1:3] == ["buildx", "du"]:
                return _FakeCompletedProcess("[]")
            return _FakeCompletedProcess("")

        monkeypatch.setattr(
            "execraft.orchestrate.resource.shutil.which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        mgr = ResourceManager(runner=runner)
        actions = mgr.cleanup(dry_run=True)
        assert any("buildkit cache" in a for a in actions)
        assert not any("prune" in c for c in calls)

    def test_buildkit_cache_included_in_needs_cleanup_false_when_below_limit(
        self, monkeypatch
    ):
        def runner(args):
            if args[1:3] == ["buildx", "du"]:
                return _FakeCompletedProcess("[]")
            if args[1] == "ps":
                return _FakeCompletedProcess("")
            if args[1] == "images":
                return _FakeCompletedProcess("")
            if args[1] == "volume":
                return _FakeCompletedProcess("")
            return _FakeCompletedProcess("")

        monkeypatch.setattr(
            "execraft.orchestrate.resource.shutil.which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        mgr = ResourceManager(runner=runner, min_cleanup_interval=0)
        inv = ResourceInventory(
            filesystem_used_percent=50,
            buildkit_cache_gb=5.0,
        )
        assert mgr.needs_cleanup(inv) is False

    def test_throttle_prevents_repeated_cleanup_checks(self, monkeypatch):
        """needs_cleanup returns False if called within min_cleanup_interval."""
        calls = []

        def runner(args):
            calls.append(args)
            return _FakeCompletedProcess("")

        monkeypatch.setattr(
            "execraft.orchestrate.resource.shutil.which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        mgr = ResourceManager(runner=runner, min_cleanup_interval=3600)
        mgr.cleanup()
        assert mgr.needs_cleanup() is False

    def test_throttle_bypassed_when_inventory_provided_explicitly(self):
        """Even after recent cleanup, needs_cleanup returns True when
        a high-pressure inventory is passed directly."""
        mgr = ResourceManager(min_cleanup_interval=3600)
        mgr.cleanup()
        inv = ResourceInventory(filesystem_used_percent=92)
        assert mgr.needs_cleanup(inv) is True


class TestResourceManagerCapabilityGatedCleanup:
    def _make_managed_workspace(
        self, tmp_path, name, *, created_at, capabilities=None, pinned=False
    ):
        root = tmp_path / name
        (root / ".execraft").mkdir(parents=True)
        data = {"created_at": str(created_at)}
        if capabilities:
            data["capabilities"] = list(capabilities)
        (root / ".execraft" / "workspace.yaml").write_text(
            yaml.safe_dump(data, sort_keys=False),
            encoding="utf-8",
        )
        if pinned:
            (root / ".execraft" / "pinned").touch()
        return root

    def test_cleanup_removes_colcon_trees_when_capability_set(self, tmp_path):
        from datetime import datetime, timezone
        root = self._make_managed_workspace(
            tmp_path, "ros-ws", created_at=datetime.now(timezone.utc).isoformat(),
            capabilities=["colcon"],
        )
        (root / "build").mkdir()
        (root / "install").mkdir()
        (root / "log").mkdir()
        (root / "src").mkdir()

        mgr = ResourceManager()
        actions = mgr.cleanup(workspace_roots=[root])

        assert any("remove build tree" in a for a in actions)
        assert any("remove install tree" in a for a in actions)
        assert any("remove log tree" in a for a in actions)
        assert (root / "src").is_dir()
        assert (root / ".execraft").is_dir()

    def test_cleanup_skips_colcon_trees_when_capability_not_set(self, tmp_path):
        from datetime import datetime, timezone
        root = self._make_managed_workspace(
            tmp_path, "plain-ws", created_at=datetime.now(timezone.utc).isoformat(),
        )
        (root / "build").mkdir()
        (root / "log").mkdir()

        mgr = ResourceManager()
        actions = mgr.cleanup(workspace_roots=[root])

        assert not any("remove build tree" in a for a in actions)
        assert not any("remove log tree" in a for a in actions)
        assert (root / "build").is_dir()
        assert (root / "log").is_dir()

    def test_cleanup_removes_ros_log_when_ros_capability_set(self, tmp_path):
        from datetime import datetime, timezone
        root = self._make_managed_workspace(
            tmp_path, "ros-only", created_at=datetime.now(timezone.utc).isoformat(),
            capabilities=["ros"],
        )
        (root / "log").mkdir()
        (root / "build").mkdir()

        mgr = ResourceManager()
        actions = mgr.cleanup(workspace_roots=[root])

        assert any("remove log tree" in a for a in actions)
        assert not any("remove build tree" in a for a in actions)

    def test_cleanup_respects_stale_check_before_capability_cleanup(self, tmp_path):
        """When workspace is stale (past retention), it is removed entirely
        regardless of capabilities."""
        from datetime import datetime, timezone, timedelta
        old = datetime.now(timezone.utc) - timedelta(days=60)
        root = self._make_managed_workspace(
            tmp_path, "old-ros", created_at=old.isoformat(),
            capabilities=["ros", "colcon"],
        )
        (root / "build").mkdir()
        (root / "log").mkdir()

        retirement = _FakeWorkspaceRetirement()
        mgr = ResourceManager(
            policy=ResourcePolicy(managed_workspace_retention_days=30),
            workspace_retirement=retirement,
        )
        actions = mgr.cleanup(workspace_roots=[root])

        assert any("retire stale managed workspace" in a for a in actions)
        assert retirement.calls == [(root, False)]
        assert not root.exists()

    def test_cleanup_dry_run_reports_ros_colcon_without_deleting(self, tmp_path):
        from datetime import datetime, timezone
        root = self._make_managed_workspace(
            tmp_path, "ros-dry", created_at=datetime.now(timezone.utc).isoformat(),
            capabilities=["colcon"],
        )
        (root / "build").mkdir()
        (root / "log").mkdir()

        mgr = ResourceManager()
        actions = mgr.cleanup(workspace_roots=[root], dry_run=True)

        assert any("remove build tree" in a for a in actions)
        assert any("remove log tree" in a for a in actions)
        assert (root / "build").is_dir()
        assert (root / "log").is_dir()

    def test_cleanup_handles_missing_capability_field_gracefully(self, tmp_path):
        """A workspace.yaml without a capabilities field should not crash."""
        from datetime import datetime, timezone
        root = tmp_path / "no-caps"
        (root / ".execraft").mkdir(parents=True)
        (root / ".execraft" / "workspace.yaml").write_text(
            f"created_at: '{datetime.now(timezone.utc).isoformat()}'\n",
            encoding="utf-8",
        )
        (root / "build").mkdir()

        mgr = ResourceManager()
        actions = mgr.cleanup(workspace_roots=[root])
        assert not any("build tree" in a for a in actions)
        assert (root / "build").is_dir()

    def test_cleanup_handles_non_list_capabilities_field_gracefully(self, tmp_path):
        """A workspace.yaml with a malformed capabilities field should not crash."""
        from datetime import datetime, timezone
        root = tmp_path / "bad-caps"
        (root / ".execraft").mkdir(parents=True)
        (root / ".execraft" / "workspace.yaml").write_text(
            f"created_at: '{datetime.now(timezone.utc).isoformat()}'\ncapabilities: 'not-a-list'\n",
            encoding="utf-8",
        )
        (root / "log").mkdir()

        mgr = ResourceManager()
        actions = mgr.cleanup(workspace_roots=[root])
        assert not any("log tree" in a for a in actions)
