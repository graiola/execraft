"""Verification registry, progressive profiles, and known-failure tracking."""

from __future__ import annotations

import hashlib
import inspect
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from execraft.persistence.atomic import atomic_write_yaml


class VerificationProfile(str):
    pass


CHEAP = VerificationProfile("cheap")
FOCUSED = VerificationProfile("focused")
FULL = VerificationProfile("full")
INTEGRATION = VerificationProfile("integration")


@dataclass
class VerificationCommand:
    command: str
    profile: VerificationProfile
    repository_id: str = ""
    timeout_seconds: int = 300
    expected_returncode: int = 0
    enabled: bool = True
    id: str = ""
    reason: str = ""
    environment: dict[str, str] = field(default_factory=dict)
    unset_environment: list[str] = field(default_factory=list)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "command": self.command,
            "profile": str(self.profile),
            "repository_id": self.repository_id,
            "timeout_seconds": self.timeout_seconds,
            "expected_returncode": self.expected_returncode,
            "enabled": self.enabled,
            "reason": self.reason,
            "environment": dict(self.environment),
            "unset_environment": list(self.unset_environment),
        }

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "VerificationCommand":
        return cls(
            id=str(data.get("id", "")),
            command=str(data["command"]),
            profile=VerificationProfile(data.get("profile", "focused")),
            repository_id=str(data.get("repository_id", "")),
            timeout_seconds=int(data.get("timeout_seconds", 300)),
            expected_returncode=int(data.get("expected_returncode", 0)),
            enabled=bool(data.get("enabled", True)),
            reason=str(data.get("reason", "")),
            environment={
                str(key): str(value)
                for key, value in (data.get("environment") or {}).items()
            },
            unset_environment=[str(item) for item in (data.get("unset_environment") or [])],
        )


@dataclass
class VerificationResult:
    command: str
    returncode: int
    duration_seconds: float
    repository_id: str = ""
    stdout_fingerprint: str = ""
    relevant_excerpt: str = ""
    status: str = ""
    failures: list[dict[str, str]] = field(default_factory=list)
    failure_fingerprint_schema: int = 2

    @property
    def passed(self) -> bool:
        return self.status == "passed" or (not self.status and self.returncode == 0)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "repository_id": self.repository_id,
            "returncode": self.returncode,
            "duration_seconds": self.duration_seconds,
            "stdout_fingerprint": self.stdout_fingerprint,
            "relevant_excerpt": self.relevant_excerpt,
            "status": self.status or ("passed" if self.returncode == 0 else "failed"),
            "failures": [dict(item) for item in self.failures],
            "failure_fingerprint_schema": self.failure_fingerprint_schema,
        }


@dataclass
class KnownFailure:
    test_identifier: str
    reason: str
    expires_at: str = ""
    environment_blocked: bool = False

    @property
    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            expiry = datetime.fromisoformat(self.expires_at)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            return expiry < datetime.now(timezone.utc)
        except (ValueError, TypeError):
            return False

    def as_mapping(self) -> dict[str, Any]:
        return {
            "test_identifier": self.test_identifier,
            "reason": self.reason,
            "expires_at": self.expires_at,
            "environment_blocked": self.environment_blocked,
        }

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "KnownFailure":
        return cls(
            test_identifier=str(data["test_identifier"]),
            reason=str(data.get("reason", "")),
            expires_at=str(data.get("expires_at", "")),
            environment_blocked=bool(data.get("environment_blocked", False)),
        )


@dataclass
class VerificationRegistry:
    commands: list[VerificationCommand] = field(default_factory=list)
    known_failures: list[KnownFailure] = field(default_factory=list)
    require_commands: bool = False

    def commands_for_profile(
        self, profile: VerificationProfile, repository_id: str = ""
    ) -> list[VerificationCommand]:
        matched = [
            cmd for cmd in self.commands
            if cmd.enabled and _profile_matches(str(cmd.profile), profile)
        ]
        if repository_id:
            matched = [
                cmd for cmd in matched
                if not cmd.repository_id or cmd.repository_id == repository_id
            ]
        return matched

    def is_known_failure(self, test_identifier: str) -> KnownFailure | None:
        for failure in self.known_failures:
            if failure.test_identifier == test_identifier and not failure.is_expired:
                return failure
        return None

    def active_known_failures(self) -> list[KnownFailure]:
        return [f for f in self.known_failures if not f.is_expired]

    def environment_blocked(self, test_identifier: str) -> bool:
        failure = self.is_known_failure(test_identifier)
        return bool(failure and failure.environment_blocked)

    def add_known_failure(self, failure: KnownFailure) -> None:
        self.known_failures = [
            existing for existing in self.known_failures
            if existing.test_identifier != failure.test_identifier
        ]
        self.known_failures.append(failure)

    def remove_expired_failures(self) -> int:
        before = len(self.known_failures)
        self.known_failures = [f for f in self.known_failures if not f.is_expired]
        return before - len(self.known_failures)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "require_commands": self.require_commands,
            "commands": [c.as_mapping() for c in self.commands],
            "known_failures": [f.as_mapping() for f in self.known_failures],
        }

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "VerificationRegistry":
        return cls(
            commands=[VerificationCommand.from_mapping(c) for c in data.get("commands", [])],
            known_failures=[KnownFailure.from_mapping(f) for f in data.get("known_failures", [])],
            require_commands=bool(data.get("require_commands", False)),
        )

    def save(self, path: Path) -> Path:
        atomic_write_yaml(path, self.as_mapping(), sort_keys=False, width=1000)
        return path

    @classmethod
    def load(cls, path: Path) -> "VerificationRegistry":
        if not path.is_file():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"verification registry must be a mapping: {path}")
        return cls.from_mapping(data)


def _profile_matches(cmd_profile: str, requested: VerificationProfile) -> bool:
    # Integration is broader than focused but narrower than full.
    ordering = {"cheap": 0, "focused": 1, "integration": 2, "full": 3}
    return ordering.get(cmd_profile, 3) <= ordering.get(str(requested), 3)


_PROFILE_ALIASES: dict[str, VerificationProfile] = {"targeted": INTEGRATION}
_KNOWN_PROFILES = {CHEAP, FOCUSED, INTEGRATION, FULL}


def resolve_profile(name: str) -> VerificationProfile:
    if name in _KNOWN_PROFILES:
        return VerificationProfile(name)
    return _PROFILE_ALIASES.get(name, FULL)


CommandRunner = Callable[..., "subprocess.CompletedProcess[str]"]


def _default_runner(
    command: str,
    *,
    cwd: Path,
    timeout: int,
    env: Mapping[str, str] | None = None,
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        command,
        shell=True,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=dict(env) if env is not None else None,
    )


def _runner_accepts_environment(runner: CommandRunner) -> bool:
    try:
        parameters = inspect.signature(runner).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD or parameter.name == "env"
        for parameter in parameters
    )


def verification_environment(
    cmd: VerificationCommand,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(base_environment) if base_environment is not None else os.environ.copy()
    for key in cmd.unset_environment:
        environment.pop(key, None)
    environment.update(cmd.environment)
    return environment


def run_verification_command(
    cmd: VerificationCommand,
    cwd: Path,
    *,
    runner: CommandRunner | None = None,
    base_environment: Mapping[str, str] | None = None,
) -> VerificationResult:
    """Execute a bounded command and normalize failures without reporting them as passes."""
    active_runner = runner or _default_runner
    start = time.monotonic()
    try:
        kwargs: dict[str, Any] = {"cwd": cwd, "timeout": cmd.timeout_seconds}
        if runner is None or _runner_accepts_environment(active_runner):
            kwargs["env"] = verification_environment(cmd, base_environment)
        completed = active_runner(cmd.command, **kwargs)
        returncode = completed.returncode
        output = (completed.stdout or "") + (completed.stderr or "")
        status = "passed" if returncode == cmd.expected_returncode else "failed"
    except Exception as exc:  # command-not-found, timeout, invalid cwd, runner fault
        returncode = cmd.expected_returncode + 1
        output = str(exc)
        status = "environment_failure"
    duration = time.monotonic() - start
    fingerprint = hashlib.sha256(output.encode("utf-8", "replace")).hexdigest()[:16]
    # Import lazily to keep the command runner independent from baseline policy
    # while still fingerprinting the *full* output before its human-facing
    # excerpt is truncated.  Baseline matching therefore cannot hide an early
    # additional failure that falls outside the final 2 KiB diagnostic tail.
    from execraft.orchestrate.verification_baseline import extract_failure_fingerprints

    failures = [item.as_mapping() for item in extract_failure_fingerprints(output)]
    return VerificationResult(
        command=cmd.command,
        returncode=returncode,
        duration_seconds=duration,
        repository_id=cmd.repository_id,
        stdout_fingerprint=fingerprint,
        relevant_excerpt=output[-2000:],
        status=status,
        failures=failures,
        failure_fingerprint_schema=2,
    )
