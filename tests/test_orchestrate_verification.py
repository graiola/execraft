"""Tests for verification registry, known failures, and result tracking."""

import pytest

from execraft.orchestrate.verification import (
    CHEAP,
    FOCUSED,
    FULL,
    INTEGRATION,
    KnownFailure,
    VerificationCommand,
    VerificationRegistry,
    VerificationResult,
    _profile_matches,
    resolve_profile,
    run_verification_command,
)


class TestVerificationCommand:
    def test_create_command(self):
        cmd = VerificationCommand(
            command="pytest tests/",
            profile=CHEAP,
            repository_id="repo-a",
            timeout_seconds=120,
        )
        assert cmd.command == "pytest tests/"
        assert cmd.profile == "cheap"

    def test_as_mapping_roundtrip(self):
        original = VerificationCommand(
            command="python -m pytest tests/ -q",
            profile=FOCUSED,
            repository_id="core",
            timeout_seconds=300,
            expected_returncode=0,
            environment={"MODE": "test"},
            unset_environment=["COMPOSE_PREFIX"],
        )
        mapping = original.as_mapping()
        restored = VerificationCommand.from_mapping(mapping)
        assert restored.command == original.command
        assert restored.profile == original.profile
        assert restored.repository_id == "core"
        assert restored.timeout_seconds == 300
        assert restored.environment == {"MODE": "test"}
        assert restored.unset_environment == ["COMPOSE_PREFIX"]


class TestVerificationResult:
    def test_passed_property(self):
        assert VerificationResult(command="test", returncode=0, duration_seconds=1.0).passed is True
        assert VerificationResult(command="test", returncode=1, duration_seconds=1.0).passed is False

    def test_as_mapping(self):
        result = VerificationResult(
            command="pytest",
            returncode=0,
            duration_seconds=5.5,
            stdout_fingerprint="abc123",
            relevant_excerpt="All tests passed",
        )
        mapping = result.as_mapping()
        assert mapping["status"] == "passed"
        assert mapping["duration_seconds"] == 5.5


class TestKnownFailure:
    def test_not_expired_without_expiry(self):
        kf = KnownFailure(test_identifier="test_x", reason="flaky")
        assert kf.is_expired is False

    def test_not_expired_with_future_expiry(self):
        kf = KnownFailure(test_identifier="test_x", reason="flaky", expires_at="2099-01-01T00:00:00")
        assert kf.is_expired is False

    def test_as_mapping_roundtrip(self):
        original = KnownFailure(
            test_identifier="test_flaky",
            reason="Network-dependent",
            expires_at="2026-12-31T00:00:00",
            environment_blocked=True,
        )
        mapping = original.as_mapping()
        restored = KnownFailure.from_mapping(mapping)
        assert restored.test_identifier == "test_flaky"
        assert restored.environment_blocked is True

    def test_environment_blocked(self):
        kf = KnownFailure(test_identifier="t1", reason="needs GPU", environment_blocked=True)
        assert kf.environment_blocked is True


class TestVerificationRegistry:
    @pytest.fixture
    def registry(self):
        reg = VerificationRegistry()
        reg.commands = [
            VerificationCommand(command="flake8 src/", profile=CHEAP),
            VerificationCommand(command="pytest tests/unit/ -q", profile=FOCUSED, repository_id="core"),
            VerificationCommand(command="pytest tests/integration/ -q", profile=INTEGRATION, repository_id="core"),
            VerificationCommand(command="mypy src/", profile=CHEAP, repository_id="core"),
        ]
        return reg

    def test_commands_for_cheap_profile(self, registry):
        cmds = registry.commands_for_profile(CHEAP)
        assert len(cmds) == 2  # flake8 + mypy

    def test_commands_for_focused_includes_cheap(self, registry):
        cmds = registry.commands_for_profile(FOCUSED, repository_id="core")
        assert len(cmds) >= 2
        assert any(c.command == "flake8 src/" for c in cmds)

    def test_commands_for_full_includes_all(self, registry):
        cmds = registry.commands_for_profile(FULL)
        assert len(cmds) == 4

    def test_commands_filtered_by_repository(self, registry):
        cmds = registry.commands_for_profile(FOCUSED, repository_id="core")
        non_repo_cmds = [c for c in cmds if not c.repository_id]
        assert len(non_repo_cmds) > 0  # cheap commands without repo filter apply to all

    def test_known_failure_lifecycle(self):
        reg = VerificationRegistry()
        kf = KnownFailure(test_identifier="test_flaky", reason="flaky test", expires_at="2099-01-01T00:00:00")
        reg.add_known_failure(kf)
        assert reg.is_known_failure("test_flaky") is not None
        assert reg.is_known_failure("nonexistent") is None

    def test_known_failure_replaced_on_re_add(self, registry):
        kf1 = KnownFailure(test_identifier="test_x", reason="reason 1")
        kf2 = KnownFailure(test_identifier="test_x", reason="reason 2")
        registry.add_known_failure(kf1)
        registry.add_known_failure(kf2)
        assert registry.is_known_failure("test_x").reason == "reason 2"

    def test_remove_expired_failures(self):
        reg = VerificationRegistry()
        reg.known_failures = [
            KnownFailure(test_identifier="expired", reason="old", expires_at="2020-01-01T00:00:00"),
            KnownFailure(test_identifier="current", reason="still valid", expires_at="2099-01-01T00:00:00"),
        ]
        removed = reg.remove_expired_failures()
        assert removed == 1
        assert reg.is_known_failure("current") is not None
        assert reg.is_known_failure("expired") is None

    def test_active_known_failures(self):
        reg = VerificationRegistry()
        reg.known_failures = [
            KnownFailure(test_identifier="expired", reason="old", expires_at="2020-01-01T00:00:00"),
            KnownFailure(test_identifier="valid", reason="active", expires_at="2099-01-01T00:00:00"),
        ]
        active = reg.active_known_failures()
        assert len(active) == 1
        assert active[0].test_identifier == "valid"

    def test_environment_blocked(self):
        reg = VerificationRegistry()
        reg.add_known_failure(KnownFailure(
            test_identifier="needs_gpu", reason="GPU required", environment_blocked=True
        ))
        assert reg.environment_blocked("needs_gpu") is True
        assert reg.environment_blocked("other_test") is False

    def test_as_mapping_roundtrip(self, registry):
        mapping = registry.as_mapping()
        restored = VerificationRegistry.from_mapping(mapping)
        assert len(restored.commands) == len(registry.commands)
        assert restored.commands[0].command == registry.commands[0].command
        assert len(restored.known_failures) == len(registry.known_failures)

    def test_save_and_load(self, tmp_path):
        reg = VerificationRegistry()
        reg.commands.append(VerificationCommand(command="pytest", profile=CHEAP))
        path = tmp_path / "verification.yaml"
        reg.save(path)
        assert path.is_file()
        loaded = VerificationRegistry.load(path)
        assert len(loaded.commands) == 1

    def test_load_nonexistent(self, tmp_path):
        reg = VerificationRegistry.load(tmp_path / "nonexistent.yaml")
        assert len(reg.commands) == 0


class TestProfileMatching:
    def test_cheap_matches_cheap(self):
        assert _profile_matches("cheap", CHEAP) is True

    def test_cheap_matches_focused(self):
        assert _profile_matches("cheap", FOCUSED) is True

    def test_focused_does_not_match_cheap(self):
        assert _profile_matches("focused", CHEAP) is False

    def test_full_matches_full(self):
        assert _profile_matches("full", FULL) is True

    def test_full_matches_all(self):
        assert _profile_matches("full", CHEAP) is False


class TestResolveProfile:
    def test_known_profiles_pass_through(self):
        assert resolve_profile("cheap") == CHEAP
        assert resolve_profile("focused") == FOCUSED
        assert resolve_profile("integration") == INTEGRATION
        assert resolve_profile("full") == FULL

    def test_targeted_maps_to_integration(self):
        # WorkPackage.verification_profile defaults to "targeted", which is
        # not one of the registry's own profile names.
        assert resolve_profile("targeted") == INTEGRATION

    def test_unrecognized_profile_falls_back_to_full(self):
        # PLAN §M08B: "Unknown impact must fall back to a safe broader
        # profile."
        assert resolve_profile("whatever-this-is") == FULL


class TestRunVerificationCommand:
    def test_passing_command(self, tmp_path):
        calls = []

        def runner(command, *, cwd, timeout):
            calls.append((command, cwd, timeout))
            return _FakeCompletedProcess(0, stdout="ok")

        cmd = VerificationCommand(command="pytest -q", profile=CHEAP, timeout_seconds=42)
        result = run_verification_command(cmd, tmp_path, runner=runner)

        assert result.status == "passed"
        assert result.passed is True
        assert result.returncode == 0
        assert calls == [("pytest -q", tmp_path, 42)]
        assert result.stdout_fingerprint

    def test_runner_receives_sanitized_environment_when_supported(self, tmp_path):
        captured = {}

        def runner(command, *, cwd, timeout, env):
            captured.update(env)
            return _FakeCompletedProcess(0, stdout="ok")

        cmd = VerificationCommand(
            command="verify",
            profile=CHEAP,
            environment={"MODE": "focused"},
            unset_environment=["COMPOSE_PREFIX"],
        )
        result = run_verification_command(
            cmd,
            tmp_path,
            runner=runner,
            base_environment={"COMPOSE_PREFIX": "ai_demo_", "BASE": "1"},
        )

        assert result.passed
        assert captured == {"BASE": "1", "MODE": "focused"}

    def test_failing_command_is_not_reported_as_passed(self, tmp_path):
        def runner(command, *, cwd, timeout):
            return _FakeCompletedProcess(1, stdout="", stderr="assertion failed")

        cmd = VerificationCommand(command="pytest -q", profile=CHEAP)
        result = run_verification_command(cmd, tmp_path, runner=runner)

        assert result.status == "failed"
        assert result.passed is False
        assert "assertion failed" in result.relevant_excerpt

    def test_runner_exception_becomes_environment_failure_not_a_pass(self, tmp_path):
        def runner(command, *, cwd, timeout):
            raise TimeoutError("command timed out")

        cmd = VerificationCommand(command="pytest -q", profile=CHEAP)
        result = run_verification_command(cmd, tmp_path, runner=runner)

        assert result.status == "environment_failure"
        assert result.status != "passed"

    def test_default_runner_executes_a_real_subprocess(self, tmp_path):
        cmd = VerificationCommand(command="exit 0", profile=CHEAP)
        result = run_verification_command(cmd, tmp_path)
        assert result.status == "passed"

        failing = VerificationCommand(command="exit 7", profile=CHEAP)
        failing_result = run_verification_command(failing, tmp_path)
        assert failing_result.status == "failed"
        assert failing_result.returncode == 7


class _FakeCompletedProcess:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_sample_verification_covers_component_repositories():
    from pathlib import Path

    registry = VerificationRegistry.load(Path("projects/sample/verification.yaml"))
    expected_ids = {
        "core": "core-focused",
        "frontend": "frontend-focused",
        "worker_a": "worker-a-integration",
        "worker_b": "worker-b-integration",
    }

    for repository_id, expected_id in expected_ids.items():
        commands = registry.commands_for_profile(
            INTEGRATION if repository_id.startswith("worker_") else FOCUSED,
            repository_id=repository_id,
        )
        assert expected_id in {command.id for command in commands}
