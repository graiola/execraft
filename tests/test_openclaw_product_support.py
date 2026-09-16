from __future__ import annotations

import builtins
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from execraft.agents.config import parse_execution_config
from execraft.agents.runtime_candidates import build_runtime_candidates
from execraft.cli_parsers import build_parser
from execraft.runtime.product_support import (
    ProductSupportError,
    profile_product_support,
    target_product_support,
)
from execraft.runtime.subagent_policy import SubagentProfilePolicy, SubagentStrategy
from execraft.runtime.topology import build_runtime_topology


def _openclaw_config(*, target_kind: str = "local", profile_enabled: bool = True):
    target = {
        "kind": target_kind,
        "endpoint": (
            "ws://build01.example:18789"
            if target_kind == "remote_runtime"
            else "http://127.0.0.1:11434"
        ),
    }
    if target_kind == "remote_runtime":
        target["workspace_transport"] = "shared"
    return {
        "schema_version": 4,
        "runtimes": {
            "openclaw": {
                "kind": "openclaw",
                "mode": "external",
                "gateway": "ws://127.0.0.1:18789",
                "auth_kind": "none",
            }
        },
        "execution_targets": {"target": target},
        "model_routes": {
            "qwen": {
                "provider": "ollama",
                "model": "qwen3-coder:30b-32k",
                "endpoint": "http://127.0.0.1:11434",
                "default_target": "target",
            }
        },
        "agents": {
            "openclaw-worker": {
                "runtime": "openclaw",
                "model_route": "qwen",
                "target": "target",
                "enabled": profile_enabled,
                "capabilities": ["implement"],
            }
        },
    }


def test_supported_openclaw_scope_is_local_or_inference_only(tmp_path: Path) -> None:
    for kind in ("local", "inference_endpoint"):
        execution = parse_execution_config(_openclaw_config(target_kind=kind))
        support = profile_product_support(execution, execution.agent("openclaw-worker"))
        assert support.supported is True
        assert support.status == "supported"

    remote = parse_execution_config(_openclaw_config(target_kind="remote_runtime"))
    target_support = target_product_support(remote.target("target"))
    assert target_support.supported is False
    assert target_support.status == "experimental_disabled"
    with pytest.raises(ProductSupportError, match="Remote full-runtime"):
        build_runtime_candidates(
            remote,
            workdir=tmp_path / "work",
            state_root=tmp_path / "state",
            read_only=False,
        )


def test_active_subagents_fail_closed_before_candidate_construction(tmp_path: Path) -> None:
    execution = parse_execution_config(_openclaw_config())
    strategy = SubagentStrategy(
        enabled=True,
        profiles={"openclaw-worker": SubagentProfilePolicy(enabled=True)},
    )
    with pytest.raises(ProductSupportError, match="experimental-disabled"):
        build_runtime_candidates(
            execution,
            workdir=tmp_path / "work",
            state_root=tmp_path / "state",
            read_only=False,
            subagent_strategy=strategy,
        )


def test_disabled_historical_remote_definition_remains_readable() -> None:
    execution = parse_execution_config(
        _openclaw_config(target_kind="remote_runtime", profile_enabled=False)
    )
    assert execution.agents == ()
    assert execution.target("target").kind.value == "remote_runtime"


def test_topology_marks_experimental_target_and_profile() -> None:
    execution = parse_execution_config(_openclaw_config(target_kind="remote_runtime"))
    topology = build_runtime_topology(execution)
    target = topology["execution_targets"][0]
    profile = topology["profiles"][0]
    assert target["support"]["status"] == "experimental_disabled"
    assert target["support"]["supported"] is False
    assert profile["support"]["status"] == "experimental_disabled"
    assert profile["support"]["supported"] is False


def test_normal_candidate_builder_does_not_import_wp13_or_wp14_modules() -> None:
    from execraft.agents import runtime_candidates

    source = inspect.getsource(runtime_candidates)
    assert "openclaw_remote_target" not in source
    assert "openclaw_subagents" not in source


def test_cli_prefers_agent_terms_and_keeps_provider_aliases() -> None:
    parser = build_parser()
    preferred = parser.parse_args(
        ["start", "do work", "--agent", "local", "--require-agent"]
    )
    assert preferred.provider == "local"
    assert preferred.require_provider is True

    compatibility = parser.parse_args(
        ["start", "do work", "--provider", "legacy", "--require-provider"]
    )
    assert compatibility.provider == "legacy"
    assert compatibility.require_provider is True


def test_openclaw_remains_optional_for_native_cli_import(tmp_path: Path) -> None:
    script = r'''
import builtins
real_import = builtins.__import__
def blocked(name, *args, **kwargs):
    if name.split('.')[0] in {'websockets', 'cryptography'}:
        raise ModuleNotFoundError(name)
    return real_import(name, *args, **kwargs)
builtins.__import__ = blocked
import execraft.cli
print('native-control-plane-import-ok')
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path.cwd(),
        env={"PYTHONPATH": str(Path.cwd() / "src")},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "native-control-plane-import-ok" in result.stdout


def test_supported_architecture_docs_pin_current_openclaw_compatibility() -> None:
    text = Path("docs/supported-runtime-architecture.md").read_text(encoding="utf-8")
    assert "2026.7.1-2" in text
    assert "Gateway protocol: **4**" in text
    assert "Experimental-disabled" in text or "experimental-disabled" in text
    assert "Experimental-disabled definitions" in text
    assert "`remote_runtime`" in text


def test_optional_packaging_keeps_openclaw_out_of_base_dependencies() -> None:
    text = Path("pyproject.toml").read_text(encoding="utf-8")
    base = text.split("[project.optional-dependencies]", 1)[0]
    assert "websockets" not in base
    assert "cryptography" not in base
    assert 'openclaw = ["cryptography>=43", "websockets>=14,<17"]' in text
