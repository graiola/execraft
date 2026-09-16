"""Managed OpenClaw Gateway child-process lifecycle.

The host owns the child PID and runtime paths. OpenClaw state remains isolated
under Execraft's state hierarchy by default and child output is consumed by a log
file immediately, avoiding pipe backpressure and orphaned subprocesses.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from typing import IO, Iterable, Mapping

from execraft.runtime_config import OpenClawRuntimeOptions

from .openclaw_auth import CredentialResolver, resolve_environment_credential

EX_CONFIG = 78

# Managed OpenClaw is a security boundary, not a child shell.  Do not inherit
# the complete Execraft environment: CI/session shells commonly carry unrelated
# cloud, SSH, package-registry, and deployment credentials.  Provider secrets
# are added explicitly from canonical ModelRoute credential references below.
_SAFE_PARENT_ENVIRONMENT = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "PATH",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "USER",
    }
)


class OpenClawProcessError(RuntimeError):
    pass


class OpenClawExecutableMissing(OpenClawProcessError):
    pass


class OpenClawConfigError(OpenClawProcessError):
    pass


@dataclass(frozen=True)
class OpenClawRuntimePaths:
    state_dir: Path
    config_path: Path
    log_path: Path


@dataclass(frozen=True)
class ManagedGatewayStatus:
    running: bool
    pid: int | None
    exit_code: int | None
    paths: OpenClawRuntimePaths


class OpenClawManagedProcess:
    """Supervise a single managed Gateway process without hidden respawning."""

    def __init__(
        self,
        options: OpenClawRuntimeOptions,
        *,
        runtime_id: str,
        state_root: Path,
        credential_resolver: CredentialResolver = resolve_environment_credential,
        environment: Mapping[str, str] | None = None,
        config_payload: Mapping[str, object] | None = None,
        credential_env_refs: Iterable[str] = (),
    ) -> None:
        self.options = options
        self.runtime_id = runtime_id
        self._state_root = Path(state_root).expanduser().resolve()
        self._credentials = credential_resolver
        self._environment = dict(environment or os.environ)
        self._config_payload = dict(config_payload) if config_payload is not None else None
        self._credential_env_refs = tuple(
            sorted({_environment_ref_name(value) for value in credential_env_refs if str(value).strip()})
        )
        self._paths = resolve_managed_paths(options, runtime_id=runtime_id, state_root=state_root)
        self._process: subprocess.Popen[bytes] | None = None
        self._log_stream: IO[bytes] | None = None
        self._last_exit_code: int | None = None

    @property
    def paths(self) -> OpenClawRuntimePaths:
        return self._paths

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        return self._process

    @property
    def status(self) -> ManagedGatewayStatus:
        process = self._process
        if process is None:
            return ManagedGatewayStatus(
                running=False,
                pid=None,
                exit_code=self._last_exit_code,
                paths=self._paths,
            )
        code = process.poll()
        if code is not None:
            self._last_exit_code = code
        return ManagedGatewayStatus(
            running=code is None,
            pid=process.pid,
            exit_code=code,
            paths=self._paths,
        )

    def start(self) -> ManagedGatewayStatus:
        current = self.status
        if current.running:
            return current
        if self._process is not None:
            self._process = None
            self._close_log()
        executable = _resolve_executable(self.options.executable)
        _mkdir_private(self._paths.log_path.parent)
        # Managed runtime state is Execraft-owned even when the operator chooses an
        # explicit location.  Keeping it owner-only also lets us isolate HOME so
        # OpenClaw cannot discover unrelated host-home credentials/configuration.
        _mkdir_private(self._paths.state_dir)
        _mkdir_private(self._paths.state_dir / "home")
        self._paths.config_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_managed_config()
        log_fd = os.open(
            self._paths.log_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        os.chmod(self._paths.log_path, 0o600)
        self._log_stream = os.fdopen(log_fd, "ab", buffering=0)
        env = self._managed_environment()
        try:
            port = urlsplit(self.options.gateway).port
            if port is None:  # RuntimeConfig validation normally prevents this.
                raise OpenClawProcessError("managed OpenClaw gateway URL has no port")
            self._process = subprocess.Popen(
                [
                    str(executable),
                    "gateway",
                    "--allow-unconfigured",
                    "--port",
                    str(port),
                ],
                cwd=str(self._paths.state_dir),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=self._log_stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except BaseException:
            self._close_log()
            raise
        self._last_exit_code = None
        return self.status

    def check_startup_exit(self) -> None:
        status = self.status
        if status.running or status.exit_code is None:
            return
        if status.exit_code == EX_CONFIG:
            raise OpenClawConfigError(
                "OpenClaw Gateway exited with EX_CONFIG (78); inspect "
                f"{self._paths.log_path} and repair {self._paths.config_path}"
            )
        raise OpenClawProcessError(
            f"OpenClaw Gateway exited with code {status.exit_code}; "
            f"inspect {self._paths.log_path}"
        )

    def stop(self, *, timeout_seconds: float = 5.0) -> ManagedGatewayStatus:
        process = self._process
        if process is None:
            self._close_log()
            return self.status
        if process.poll() is None:
            _signal_process_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                _signal_process_group(process, signal.SIGKILL)
                process.wait(timeout=max(1.0, timeout_seconds))
        self._last_exit_code = process.returncode
        self._process = None
        self._close_log()
        return self.status

    def _ensure_managed_config(self) -> None:
        """Create only Execraft's default minimal Gateway config, never overwrite one.

        Execraft may supply a complete model/provider projection through
        ``_config_payload``. Without one, lifecycle setup still emits only the
        minimal local Gateway config. Explicit user configs are never overwritten.
        """

        if self.options.config_path:
            _require_private_explicit_config(self._paths.config_path)
            return
        payload = json.dumps(
            self._config_payload or {"gateway": {"mode": "local", "bind": "loopback"}},
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        temporary = self._paths.config_path.with_name(
            f".{self._paths.config_path.name}.tmp-{os.getpid()}"
        )
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._paths.config_path)
            os.chmod(self._paths.config_path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _managed_environment(self) -> dict[str, str]:
        env = {
            key: value
            for key, value in self._environment.items()
            if key in _SAFE_PARENT_ENVIRONMENT
        }
        for name in self._credential_env_refs:
            value = self._environment.get(name)
            if value:
                env[name] = value
        # Deliberately replace host HOME with a private runtime home.  The
        # Gateway receives canonical credentials explicitly; it must not discover
        # ~/.aws, ~/.ssh, ~/.config, or unrelated OpenClaw state from the Execraft
        # operator account.
        env["HOME"] = str(self._paths.state_dir / "home")
        env.update(
            {
                "OPENCLAW_DISABLE_BONJOUR": "1",
                "OPENCLAW_EXEC_SHELL_SNAPSHOT": "0",
                "OPENCLAW_NO_RESPAWN": "1",
                "OPENCLAW_SKIP_CHANNELS": "1",
                "OPENCLAW_STATE_DIR": str(self._paths.state_dir),
                "OPENCLAW_CONFIG_PATH": str(self._paths.config_path),
            }
        )
        credential = self._credentials(self.options.auth_ref, self.options.auth_kind)
        if credential is not None:
            if credential.kind == "token":
                env["OPENCLAW_GATEWAY_TOKEN"] = credential.value
            elif credential.kind == "password":
                env["OPENCLAW_GATEWAY_PASSWORD"] = credential.value
        return env

    def _close_log(self) -> None:
        stream = self._log_stream
        self._log_stream = None
        if stream is not None:
            stream.close()

    def __enter__(self) -> "OpenClawManagedProcess":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def resolve_managed_paths(
    options: OpenClawRuntimeOptions, *, runtime_id: str, state_root: Path
) -> OpenClawRuntimePaths:
    root = Path(state_root).expanduser().resolve() / "openclaw" / runtime_id
    state_dir = _configured_absolute(options.state_dir, "OpenClaw state_dir") or root / "gateway"
    config_path = _configured_absolute(options.config_path, "OpenClaw config_path") or state_dir / "openclaw.json"
    return OpenClawRuntimePaths(
        state_dir=state_dir.resolve(),
        config_path=config_path.resolve(),
        log_path=(root / "gateway.log").resolve(),
    )


def _configured_absolute(value: str, label: str) -> Path | None:
    if not value.strip():
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise OpenClawProcessError(f"{label} must be an absolute path")
    return path


def _resolve_executable(value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
        raise OpenClawExecutableMissing(f"OpenClaw executable is not executable: {candidate}")
    discovered = shutil.which(value)
    if not discovered:
        raise OpenClawExecutableMissing(
            f"OpenClaw executable {value!r} is not installed or not on PATH"
        )
    return Path(discovered).resolve()


def _require_private_explicit_config(path: Path) -> None:
    """Fail closed when an operator-owned managed config is broadly readable.

    Execraft never rewrites an explicit config path.  Because that file may carry
    OpenClaw/provider configuration, managed mode also refuses to launch with a
    group/world-accessible config rather than silently changing user-owned
    permissions.
    """

    if not path.is_file():
        raise OpenClawConfigError(f"explicit OpenClaw config does not exist: {path}")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise OpenClawConfigError(
            "explicit OpenClaw config must be owner-only (chmod 600): " f"{path}"
        )


def _environment_ref_name(value: object) -> str:
    text = str(value).strip()
    name = text[4:] if text.startswith("env:") else text
    name = name.strip()
    if not name or not (name[0].isalpha() or name[0] == "_") or not all(
        char.isalnum() or char == "_" for char in name
    ):
        raise OpenClawProcessError(f"invalid OpenClaw credential environment reference: {text!r}")
    return name


def _mkdir_private(path: Path) -> None:
    """Create/chmod an Execraft-owned runtime directory as owner-only."""

    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _signal_process_group(process: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except (AttributeError, OSError):  # pragma: no cover - non-POSIX fallback.
        if sig == signal.SIGKILL:
            process.kill()
        else:
            process.terminate()
