"""Opt-in adversarial sandbox checks for the supported security boundary."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
from urllib.parse import urlsplit

import pytest

from execraft.agents.config import parse_execution_config
from execraft.agents.runtime_candidates import build_runtime_candidates
from execraft.orchestrate.scheduler import StructuredHandoff
from execraft.runtime.contracts import RuntimeExecutionRequest


def _live_settings() -> tuple[str, str, str]:
    model = os.environ.get("EXECRAFT_OPENCLAW_LIVE_SECURITY_OLLAMA_MODEL", "").strip()
    endpoint = os.environ.get("EXECRAFT_OPENCLAW_LIVE_SECURITY_OLLAMA_URL", "").strip()
    if not model or not endpoint:
        pytest.skip(
            "set EXECRAFT_OPENCLAW_LIVE_SECURITY_OLLAMA_MODEL and "
            "EXECRAFT_OPENCLAW_LIVE_SECURITY_OLLAMA_URL for the live security suite"
        )
    executable = os.environ.get("EXECRAFT_OPENCLAW_LIVE_SECURITY_EXECUTABLE", "openclaw").strip()
    return model, endpoint, executable


def _runtime(tmp_path: Path):
    model, endpoint, executable = _live_settings()
    raw = {
        "schema_version": 4,
        "runtimes": {
            "openclaw": {
                "kind": "openclaw",
                "mode": "managed",
                "auth_kind": "none",
                "executable": executable,
                "startup_timeout_seconds": 60,
                "request_timeout_seconds": 90,
            }
        },
        "execution_targets": {"ollama": {"kind": "local", "endpoint": endpoint}},
        "model_routes": {
            "implementation-model": {
                "provider": "ollama",
                "model": model,
                "endpoint": endpoint,
                "default_target": "ollama",
            }
        },
        "agents": {
            "implementer": {
                "runtime": "openclaw",
                "model_route": "implementation-model",
                "capabilities": ["implement"],
                "policy": {"sandbox": "workspace-write", "timeout_seconds": 120},
            }
        },
    }
    execution = parse_execution_config(raw)
    worktree = tmp_path / "product"
    worktree.mkdir()
    state_root = tmp_path / "state"
    runtime = build_runtime_candidates(
        execution,
        workdir=worktree,
        state_root=state_root,
        read_only=False,
    )[0]
    return runtime, worktree, state_root


def _assert_secret_absent_from_tree(root: Path, secret: str) -> None:
    needle = secret.encode("utf-8")
    if not root.exists():
        return
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        assert needle not in payload, f"synthetic host secret leaked into runtime artifact {path}"


def _shell_command(script: str) -> str:
    return "sh -c " + shlex.quote(script)


def _execute_shell_probe(runtime, worktree: Path, command: str, package_id: str):
    handoff = StructuredHandoff(
        work_package_id=package_id,
        stage="implementation",
        summary=(
            "Execute the exact shell command below once using the runtime shell tool. "
            "Do not replace it with an explanation and do not omit any sub-command.\n\n"
            f"{command}"
        ),
        working_directory=str(worktree),
        read_only=False,
        required_isolation="hard",
        requirements=[
            "This is an authorized sandbox-enforcement test; execute the command exactly once."
        ],
    )
    result = runtime.execute_runtime(
        RuntimeExecutionRequest(
            identity=runtime.execution_identity,
            handoff=handoff,
            capability="implement",
            package_id=package_id,
            stage="implementation",
        )
    )
    assert result.output["ok"] is True
    assert result.runtime_metadata["security_enforcement"] == "managed_hard"
    assert result.runtime_metadata["security_policy"]["workspace_access"] == "rw"
    return result


def test_real_managed_workspace_blocks_escape_secret_and_root_write(tmp_path: Path) -> None:
    runtime, worktree, state_root = _runtime(tmp_path)
    secret_value = "synthetic-secret-not-mounted-into-sandbox"
    host_secret = tmp_path / "HOST_SECRET.txt"
    host_secret.write_text(secret_value, encoding="utf-8")
    host_escape = tmp_path / "FORBIDDEN_ESCAPE.txt"

    command = _shell_command(
        """set +e
printf "allowed\n" > ALLOWED_WRITE.txt
if printf "escape\n" > ../FORBIDDEN_ESCAPE.txt 2>/dev/null; then
  printf "unexpected-success\n" > ESCAPE_RESULT.txt
else
  printf "denied\n" > ESCAPE_RESULT.txt
fi
if cat ../HOST_SECRET.txt > SECRET_LEAK.txt 2>/dev/null; then
  printf "unexpected-success\n" > SECRET_RESULT.txt
else
  printf "denied\n" > SECRET_RESULT.txt
fi
if printf "root\n" > /PRIVILEGED_ESCAPE.txt 2>/dev/null; then
  printf "unexpected-success\n" > PRIVILEGED_RESULT.txt
else
  printf "denied\n" > PRIVILEGED_RESULT.txt
fi
"""
    )
    _execute_shell_probe(runtime, worktree, command, "LIVE-SECURITY-BOUNDARIES")

    assert (worktree / "ALLOWED_WRITE.txt").read_text(encoding="utf-8").strip() == "allowed"
    assert host_escape.exists() is False
    assert (worktree / "ESCAPE_RESULT.txt").read_text(encoding="utf-8").strip() == "denied"
    assert (worktree / "SECRET_RESULT.txt").read_text(encoding="utf-8").strip() == "denied"
    leak = worktree / "SECRET_LEAK.txt"
    if leak.exists():
        assert secret_value not in leak.read_text(encoding="utf-8", errors="replace")
    assert (worktree / "PRIVILEGED_RESULT.txt").read_text(encoding="utf-8").strip() == "denied"
    _assert_secret_absent_from_tree(state_root, secret_value)


def test_real_managed_workspace_blocks_network_egress(tmp_path: Path) -> None:
    probe_url = os.environ.get("EXECRAFT_OPENCLAW_LIVE_SECURITY_NETWORK_PROBE_URL", "").strip()
    if not probe_url:
        pytest.skip(
            "set EXECRAFT_OPENCLAW_LIVE_SECURITY_NETWORK_PROBE_URL to a known reachable "
            "HTTP(S) endpoint for the live network-denial check"
        )
    parsed = urlsplit(probe_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        pytest.fail("EXECRAFT_OPENCLAW_LIVE_SECURITY_NETWORK_PROBE_URL must be an HTTP(S) URL")

    runtime, worktree, _state_root = _runtime(tmp_path)
    quoted_url = shlex.quote(probe_url)
    command = _shell_command(
        f"""set +e
printf "allowed\n" > NETWORK_PROBE_STARTED.txt
if ! command -v curl >/dev/null 2>&1; then
  printf "curl-unavailable\n" > NETWORK_RESULT.txt
elif curl --fail --silent --show-error --max-time 5 {quoted_url} >/dev/null 2>&1; then
  printf "unexpected-success\n" > NETWORK_RESULT.txt
else
  printf "denied\n" > NETWORK_RESULT.txt
fi
"""
    )
    _execute_shell_probe(runtime, worktree, command, "LIVE-SECURITY-NETWORK")

    assert (worktree / "NETWORK_PROBE_STARTED.txt").exists()
    network_result = (worktree / "NETWORK_RESULT.txt").read_text(encoding="utf-8").strip()
    if network_result == "curl-unavailable":
        pytest.skip("managed OpenClaw sandbox image has no curl; network denial is not proven")
    assert network_result == "denied"
