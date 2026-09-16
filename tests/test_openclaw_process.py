from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from execraft.runtime.openclaw_auth import GatewayCredential
from execraft.runtime.openclaw_process import (
    OpenClawConfigError,
    OpenClawManagedProcess,
    OpenClawProcessError,
    resolve_managed_paths,
)
from execraft.runtime_config import OpenClawRuntimeOptions


def _credential(_ref: str, kind: str):
    return GatewayCredential(kind=kind, value="secret-value") if kind != "none" else None


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake-openclaw"
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_managed_paths_default_under_execraft_state(tmp_path: Path) -> None:
    paths = resolve_managed_paths(
        OpenClawRuntimeOptions(auth_ref="TOKEN"),
        runtime_id="oc",
        state_root=tmp_path,
    )
    assert paths.state_dir == tmp_path / "openclaw" / "oc" / "gateway"
    assert paths.config_path == paths.state_dir / "openclaw.json"
    assert paths.log_path == tmp_path / "openclaw" / "oc" / "gateway.log"


def test_relative_runtime_paths_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(OpenClawProcessError, match="absolute path"):
        resolve_managed_paths(
            OpenClawRuntimeOptions(auth_ref="TOKEN", state_dir="relative/state"),
            runtime_id="oc",
            state_root=tmp_path,
        )


def test_managed_process_uses_embedding_environment_and_stops_child(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    executable = _script(
        tmp_path,
        """import json, os, time\nkeys = [\n 'OPENCLAW_DISABLE_BONJOUR','OPENCLAW_EXEC_SHELL_SNAPSHOT','OPENCLAW_NO_RESPAWN',\n 'OPENCLAW_SKIP_CHANNELS','OPENCLAW_STATE_DIR','OPENCLAW_CONFIG_PATH','OPENCLAW_GATEWAY_TOKEN'\n]\nwith open(os.environ['FAKE_CAPTURE'], 'w') as f: json.dump({'env': {k: os.environ.get(k) for k in keys}, 'argv': __import__('sys').argv}, f)\ntime.sleep(60)\n""",
    )
    options = OpenClawRuntimeOptions(executable=str(executable), auth_ref="TOKEN")
    manager = OpenClawManagedProcess(
        options,
        runtime_id="oc",
        state_root=tmp_path,
        credential_resolver=_credential,
        environment={**os.environ, "FAKE_CAPTURE": str(capture)},
        credential_env_refs=("FAKE_CAPTURE",),
    )
    try:
        started = manager.start()
        assert started.running and started.pid
        deadline = time.time() + 2
        payload = None
        while payload is None and time.time() < deadline:
            if capture.is_file():
                try:
                    payload = json.loads(capture.read_text())
                except json.JSONDecodeError:
                    pass
            if payload is None:
                time.sleep(0.01)
        assert payload is not None
        env = payload["env"]
        assert env["OPENCLAW_DISABLE_BONJOUR"] == "1"
        assert env["OPENCLAW_EXEC_SHELL_SNAPSHOT"] == "0"
        assert env["OPENCLAW_NO_RESPAWN"] == "1"
        assert env["OPENCLAW_SKIP_CHANNELS"] == "1"
        assert env["OPENCLAW_GATEWAY_TOKEN"] == "secret-value"
        assert env["OPENCLAW_STATE_DIR"] == str(manager.paths.state_dir)
        assert env["OPENCLAW_CONFIG_PATH"] == str(manager.paths.config_path)
        assert payload["argv"][-2:] == ["--port", "18789"]
    finally:
        stopped = manager.stop(timeout_seconds=1)
        assert not stopped.running

def test_exit_code_78_is_reported_as_configuration_error(tmp_path: Path) -> None:
    executable = _script(tmp_path, "import sys\nsys.exit(78)\n")
    manager = OpenClawManagedProcess(
        OpenClawRuntimeOptions(executable=str(executable), auth_kind="none"),
        runtime_id="oc",
        state_root=tmp_path,
        credential_resolver=_credential,
    )
    try:
        manager.start()
        assert manager.process is not None
        manager.process.wait(timeout=2)
        with pytest.raises(OpenClawConfigError, match="EX_CONFIG"):
            manager.check_startup_exit()
    finally:
        manager.stop()


def test_managed_process_generates_minimal_private_config_without_secrets(tmp_path: Path) -> None:
    executable = _script(tmp_path, "import time\ntime.sleep(60)\n")
    manager = OpenClawManagedProcess(
        OpenClawRuntimeOptions(executable=str(executable), auth_ref="TOKEN"),
        runtime_id="oc",
        state_root=tmp_path,
        credential_resolver=_credential,
    )
    try:
        manager.start()
        payload = json.loads(manager.paths.config_path.read_text(encoding="utf-8"))
        assert payload == {"gateway": {"bind": "loopback", "mode": "local"}}
        assert "secret-value" not in manager.paths.config_path.read_text(encoding="utf-8")
        assert stat.S_IMODE(manager.paths.config_path.stat().st_mode) == 0o600
    finally:
        manager.stop(timeout_seconds=1)


def test_managed_process_never_overwrites_explicit_config(tmp_path: Path) -> None:
    executable = _script(tmp_path, "import time\ntime.sleep(60)\n")
    custom = tmp_path / "custom.json"
    custom.write_text('{"gateway":{"mode":"local"},"custom":true}\n', encoding="utf-8")
    custom.chmod(0o600)
    manager = OpenClawManagedProcess(
        OpenClawRuntimeOptions(
            executable=str(executable),
            auth_kind="none",
            config_path=str(custom),
        ),
        runtime_id="oc",
        state_root=tmp_path,
        credential_resolver=_credential,
    )
    original = custom.read_bytes()
    try:
        manager.start()
        assert custom.read_bytes() == original
    finally:
        manager.stop(timeout_seconds=1)
