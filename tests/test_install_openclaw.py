"""Cover the OpenClaw provisioning script's configuration and guard behaviour.

The install path itself shells out to npm and is not exercised here; what is
covered is everything that can silently produce a broken or unsafe runtime
configuration.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from execraft.agents.config import parse_execution_config
from execraft.runtime.openclaw_protocol import TESTED_OPENCLAW_VERSIONS
from execraft.runtime_config import OpenClawMode, RuntimeKind

ROOT = Path(__file__).resolve().parents[1]


def _module() -> Any:
    path = ROOT / "tools" / "install_openclaw.py"
    spec = importlib.util.spec_from_file_location("execraft_install_openclaw", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before executing: the module defines dataclasses, whose field
    # resolution looks the defining module up in sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def installer() -> Any:
    return _module()


def _v4_config() -> dict[str, Any]:
    return {
        "schema_version": 4,
        "runtimes": {"native": {"kind": "native", "adapter": "codex", "binary": "codex"}},
        "execution_targets": {
            "local-ollama": {
                "kind": "local",
                "endpoint": "http://127.0.0.1:11434/v1",
            }
        },
        "model_routes": {
            "route-local": {
                "provider": "ollama",
                "model": "qwen3-coder:30b-32k",
                "default_target": "local-ollama",
            }
        },
        "agents": {
            "codex": {"runtime": "native", "capabilities": ["implement"]},
        },
    }


def _write(tmp_path: Path, mapping: dict[str, Any]) -> Path:
    path = tmp_path / "agents.yaml"
    path.write_text(yaml.safe_dump(mapping, sort_keys=False), encoding="utf-8")
    return path


def _configure(installer: Any, path: Path, **overrides: Any):
    kwargs: dict[str, Any] = {
        "runtime_id": "openclaw-local",
        "profile_id": "openclaw-local-review",
        "gateway": "ws://127.0.0.1:18789",
        "auth_kind": "",
        "auth_ref": "",
        "mode": "managed",
        "executable": "openclaw",
        "capabilities": ["review"],
        "model_route": "route-local",
        "priority": 50,
        "max_complexity": 40,
        "apply": False,
    }
    kwargs.update(overrides)
    return installer.configure(path, **kwargs)


def test_pinned_version_comes_from_the_validated_protocol_set(installer: Any) -> None:
    """The installer must not carry its own copy of the supported version."""

    assert installer.pinned_version() in TESTED_OPENCLAW_VERSIONS


def test_generated_configuration_parses_and_binds_the_openclaw_runtime(
    installer: Any, tmp_path: Path
) -> None:
    path = _write(tmp_path, _v4_config())

    updated, backup = _configure(installer, path)

    assert backup is None
    assert path.read_text(encoding="utf-8") == yaml.safe_dump(
        _v4_config(), sort_keys=False
    )  # dry run leaves the file untouched

    execution = parse_execution_config(updated, include_disabled=True)
    runtime = execution.runtime("openclaw-local")
    assert runtime.kind == RuntimeKind.OPENCLAW
    assert runtime.openclaw is not None
    assert runtime.openclaw.mode == OpenClawMode.MANAGED
    # Fail closed on unvalidated Gateway releases by default.
    assert runtime.openclaw.version_policy.value == "pinned-compatible"
    # A managed Gateway is still authenticated: OpenClaw rejects an
    # unauthenticated connect even over loopback.
    assert runtime.openclaw.auth_kind == "token"
    assert runtime.openclaw.auth_ref == "env:OPENCLAW_GATEWAY_TOKEN"

    profile = execution.agent("openclaw-local-review")
    assert profile.runtime_id == "openclaw-local"
    assert profile.model_route_id == "route-local"


def test_apply_writes_a_backup_and_a_parseable_file(
    installer: Any, tmp_path: Path
) -> None:
    path = _write(tmp_path, _v4_config())
    original = path.read_text(encoding="utf-8")

    _updated, backup = _configure(installer, path, apply=True)

    assert backup is not None
    assert backup.read_text(encoding="utf-8") == original
    written = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "openclaw-local" in written["runtimes"]
    parse_execution_config(written, include_disabled=True)


def test_pre_v4_configuration_is_refused(installer: Any, tmp_path: Path) -> None:
    mapping = _v4_config()
    mapping["schema_version"] = 3
    path = _write(tmp_path, mapping)

    with pytest.raises(installer.InstallError, match="migrate it first"):
        _configure(installer, path)


def test_unknown_model_route_is_refused(installer: Any, tmp_path: Path) -> None:
    path = _write(tmp_path, _v4_config())

    with pytest.raises(installer.InstallError, match="unknown model route"):
        _configure(installer, path, model_route="does-not-exist")


@pytest.mark.parametrize(
    "field,value,message",
    (
        ("runtime_id", "native", "already exists"),
        ("profile_id", "codex", "already exists"),
    ),
)
def test_existing_ids_are_never_silently_overwritten(
    installer: Any, tmp_path: Path, field: str, value: str, message: str
) -> None:
    path = _write(tmp_path, _v4_config())

    with pytest.raises(installer.InstallError, match=message):
        _configure(installer, path, **{field: value})


def test_invalid_generated_configuration_is_not_written(
    installer: Any, tmp_path: Path
) -> None:
    """A rejected configuration must leave the project file untouched."""

    path = _write(tmp_path, _v4_config())
    original = path.read_text(encoding="utf-8")

    with pytest.raises(installer.InstallError):
        _configure(installer, path, capabilities=["not-a-capability"], apply=True)

    assert path.read_text(encoding="utf-8") == original


def test_managed_gateway_defaults_to_openclaws_own_token_variable(
    installer: Any,
) -> None:
    """OpenClaw refuses an unauthenticated connect even on loopback.

    Verified against 2026.7.1-2, which answers ``AUTH_TOKEN_MISSING``. Using
    OpenClaw's own variable means one export configures the Gateway and the
    Execraft client together.
    """

    assert installer.resolve_auth("managed", "", "") == (
        "token",
        "env:OPENCLAW_GATEWAY_TOKEN",
    )


def test_explicit_credential_reference_is_preserved(installer: Any) -> None:
    assert installer.resolve_auth("external", "token", "env:OTHER") == (
        "token",
        "env:OTHER",
    )


def test_unauthenticated_gateway_remains_selectable(installer: Any) -> None:
    """A Gateway configured to accept local connections needs no credential."""

    assert installer.resolve_auth("managed", "none", "") == ("none", "")


def test_credential_reference_conflicting_with_auth_kind_is_refused(
    installer: Any,
) -> None:
    with pytest.raises(installer.InstallError, match="meaningless"):
        installer.resolve_auth("managed", "none", "env:TOKEN")


def test_configured_credential_is_only_ever_a_reference(
    installer: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The project file records the variable name, never the resolved secret."""

    secret = "s3cr3t-gateway-value"
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", secret)
    path = _write(tmp_path, _v4_config())

    _updated, backup = _configure(
        installer,
        path,
        auth_kind="token",
        auth_ref="env:OPENCLAW_GATEWAY_TOKEN",
        apply=True,
    )

    assert backup is not None
    written = path.read_text(encoding="utf-8")
    assert "env:OPENCLAW_GATEWAY_TOKEN" in written
    assert secret not in written
