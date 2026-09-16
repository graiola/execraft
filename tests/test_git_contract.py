from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from execraft.git import (
    GitClient,
    GitCommandError,
    GitPolicy,
    GitRequest,
    GitTimeoutError,
    GitUnavailableError,
)


def _repository(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_git_client_returns_typed_success_and_failure(tmp_path: Path) -> None:
    _repository(tmp_path)
    client = GitClient()

    success = client.run(tmp_path, ("status", "--short"))
    failure = client.run(tmp_path, ("show", "missing-ref"), check=False)

    assert success.ok and success.returncode == 0
    assert not failure.ok and failure.returncode != 0 and failure.stderr
    assert failure.command == ("git", "show", "missing-ref")


def test_git_client_check_policy_raises_typed_error(tmp_path: Path) -> None:
    _repository(tmp_path)
    with pytest.raises(GitCommandError) as raised:
        GitClient().execute(GitRequest(tmp_path, ("show", "missing-ref")))
    assert raised.value.result.returncode != 0


def test_git_client_translates_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["git", "status"], 0.01)

    monkeypatch.setattr(subprocess, "run", timeout)
    request = GitRequest(tmp_path, ("status",), GitPolicy(timeout_seconds=0.01))
    with pytest.raises(GitTimeoutError, match="timed out"):
        GitClient().execute(request)


def test_git_client_translates_start_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", unavailable)
    with pytest.raises(GitUnavailableError, match="cannot execute Git"):
        GitClient().run(tmp_path, ("status",))


def test_git_client_disables_terminal_prompts(tmp_path: Path) -> None:
    _repository(tmp_path)
    result = GitClient().run(tmp_path, ("var", "GIT_EDITOR"), check=False)
    assert isinstance(result.stdout, str)


def test_git_client_environment_override_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that GIT_TERMINAL_PROMPT cannot be overridden by caller environment."""
    _repository(tmp_path)

    # Capture subprocess.run to inspect the environment
    captured_env = {}

    def capture_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "env" in kwargs:
            captured_env.update(kwargs["env"])
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", capture_run)

    # Test with an environment that attempts to override GIT_TERMINAL_PROMPT
    GitClient().run(
        tmp_path,
        ("var", "GIT_EDITOR"),
        check=False,
        environment={"GIT_TERMINAL_PROMPT": "1"}
    )

    # Verify that GIT_TERMINAL_PROMPT is forced to 0 despite caller's attempt
    assert captured_env["GIT_TERMINAL_PROMPT"] == "0"
