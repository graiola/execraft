"""OpenClaw onboarding, setup, and runtime-neutral integration coverage.

These tests exercise schema-v4 onboarding, transactional model/runtime setup,
and GUI contracts without requiring a live Gateway or browser binary.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from execraft import bootstrap
from execraft.agents.config import parse_execution_config
from execraft.onboarding.execution_inventory import ExecutionInventory
from execraft.onboarding.profiles import ProjectProfileRenderer, ProjectTemplateContext, default_profile_catalog
from execraft.onboarding.readiness import ReadinessService
from execraft.project import load_project
from execraft.gui.runtime_topology import runtime_setup_snapshot, runtime_topology_snapshot
from execraft.runtime.execution_setup import (
    ExecutionSetupError,
    apply_execution_configuration,
    preview_model_route_configuration,
)
from execraft.runtime.openclaw_setup import (
    OpenClawSetupError,
    apply_openclaw_configuration,
    preview_openclaw_configuration,
)
from execraft.runtime_config import RuntimeKind

ROOT = Path(__file__).resolve().parents[1]


def _init_git(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "setup@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "OpenClaw setup"], cwd=path, check=True)
    (path / "README.md").write_text("# OpenClaw setup\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


def _render_modern_project(tmp_path: Path) -> tuple[Path, object]:
    source = tmp_path / "source"
    _init_git(source)
    report = bootstrap.discover(source)
    destination = tmp_path / "project"
    destination.mkdir()
    ProjectProfileRenderer(default_profile_catalog()).render(
        ProjectTemplateContext(report=report, profile_reference="starter@1"),
        destination,
    )
    return source, load_project(destination)




def _render_registered_modern_project(tmp_path: Path) -> tuple[Path, object, object]:
    root = tmp_path / "control"
    source = tmp_path / "demo"
    _init_git(source)
    report = bootstrap.discover(source)
    destination = root / "projects" / report.project_id
    destination.mkdir(parents=True)
    ProjectProfileRenderer(default_profile_catalog()).render(
        ProjectTemplateContext(report=report, profile_reference="starter@1"),
        destination,
    )
    service = SimpleNamespace(
        root=root,
        project_id=report.project_id,
        state_root=tmp_path / "state",
        snapshot=lambda: {"mode": "home"},
    )
    return source, load_project(destination), service


def _base_v4(path: Path) -> Path:
    mapping = {
        "schema_version": 4,
        "runtimes": {
            "native-opencode": {"kind": "native", "adapter": "opencode", "binary": sys.executable},
        },
        "execution_targets": {},
        "model_routes": {
            "original": {"provider": "opencode", "model": "deepseek-v4-flash-free"},
        },
        "agents": {
            "local-coder": {
                "runtime": "native-opencode",
                "model_route": "original",
                "enabled": True,
                "capabilities": ["implement", "review"],
            }
        },
        "scheduling": {},
        "commit": {"mode": "automatic", "require_verification": True},
    }
    path.write_text(yaml.safe_dump(mapping, sort_keys=False), encoding="utf-8")
    return path


def test_modern_onboarding_renders_schema_v4_native_only(tmp_path: Path) -> None:
    _source, project = _render_modern_project(tmp_path)
    agents_path = project.configured_path("agents_file")
    assert agents_path is not None
    raw = yaml.safe_load(agents_path.read_text(encoding="utf-8"))

    assert raw["schema_version"] == 4
    assert {"runtimes", "model_routes", "execution_targets", "agents"} <= raw.keys()
    assert "providers" not in raw
    execution = parse_execution_config(raw, include_disabled=True)
    assert execution.runtimes
    assert all(runtime.kind == RuntimeKind.NATIVE for runtime in execution.runtimes)


def test_mixed_v4_execution_inventory_does_not_fall_back_to_provider_projection(
    tmp_path: Path,
) -> None:
    source, project = _render_modern_project(tmp_path)
    agents_path = project.configured_path("agents_file")
    assert agents_path is not None
    raw = yaml.safe_load(agents_path.read_text(encoding="utf-8"))
    raw["runtimes"]["native-codex"]["binary"] = sys.executable
    raw["agents"]["codex"]["enabled"] = True
    raw["runtimes"]["openclaw-external"] = {
        "kind": "openclaw",
        "mode": "external",
        "gateway": "ws://127.0.0.1:18789",
        "auth_kind": "token",
        "auth_ref": "env:OPENCLAW_GATEWAY_TOKEN",
    }
    raw["agents"]["openclaw-review"] = {
        "runtime": "openclaw-external",
        "model_route": "opencode-free",
        "enabled": True,
        "capabilities": ["review"],
        "policy": {"sandbox": "read-only", "sandbox_enabled": True},
    }
    agents_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    report = ExecutionInventory().inspect(project)
    assert {item.runtime_kind for item in report.profiles} == {"native", "openclaw"}
    assert not any(item.code == "execution.configuration_invalid" for item in report.findings)
    assert {item.id for item in report.ready_profiles} >= {"codex", "openclaw-review"}

    readiness = ReadinessService().evaluate(project, source_root=source)
    checks = {item.id: item for item in readiness.checks}
    assert "execution" in checks
    assert "providers" not in checks
    assert checks["execution"].status.value == "ready"


def test_runtime_neutral_model_setup_supports_native_local_ollama(tmp_path: Path) -> None:
    path = _base_v4(tmp_path / "agents.yaml")
    preview = preview_model_route_configuration(
        path,
        profile_id="local-coder",
        route_id="qwen-local",
        provider="ollama",
        model="qwen3-coder:30b",
        target_id="local-ollama",
        target_kind="local",
        endpoint="http://127.0.0.1:11434/v1",
        api_family="openai-compatible",
    )
    assert preview.changed
    updated = yaml.safe_load(preview.rendered_yaml)
    execution = parse_execution_config(updated, include_disabled=True)
    profile = execution.agent("local-coder")
    assert profile.model_route_id == "qwen-local"
    assert profile.target_id == "local-ollama"
    assert execution.model_route("qwen-local").provider == "ollama"

    original = path.read_bytes()
    backup = apply_execution_configuration(path, preview)
    assert backup is not None and backup.read_bytes() == original
    parse_execution_config(yaml.safe_load(path.read_text(encoding="utf-8")), include_disabled=True)


def test_execution_setup_requires_a_fresh_reviewed_hash(tmp_path: Path) -> None:
    path = _base_v4(tmp_path / "agents.yaml")
    preview = preview_model_route_configuration(
        path,
        profile_id="local-coder",
        route_id="qwen-local",
        provider="ollama",
        model="qwen3-coder:30b",
    )
    path.write_text(path.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    with pytest.raises(ExecutionSetupError, match="changed after preview"):
        apply_execution_configuration(path, preview)


def test_openclaw_setup_reuses_canonical_route_and_never_embeds_secret(tmp_path: Path) -> None:
    path = _base_v4(tmp_path / "agents.yaml")
    secret = "literal-secret-must-not-appear"
    preview = preview_openclaw_configuration(
        path,
        runtime_id="openclaw-local",
        profile_id="openclaw-local-review",
        mode="managed",
        gateway="ws://127.0.0.1:18789",
        executable="openclaw",
        auth_kind="token",
        auth_ref="env:OPENCLAW_GATEWAY_TOKEN",
        model_route_id="original",
        capabilities=["review", "fix_review"],
    )
    assert secret not in preview.rendered_yaml
    updated = yaml.safe_load(preview.rendered_yaml)
    execution = parse_execution_config(updated, include_disabled=True)
    runtime = execution.runtime("openclaw-local")
    assert runtime.kind == RuntimeKind.OPENCLAW
    profile = execution.agent("openclaw-local-review")
    assert profile.model_route_id == "original"
    assert profile.policy.sandbox_enabled is True

    backup = apply_openclaw_configuration(path, preview)
    assert backup is not None


def test_openclaw_gui_setup_rejects_plaintext_credentials(tmp_path: Path) -> None:
    path = _base_v4(tmp_path / "agents.yaml")
    with pytest.raises(OpenClawSetupError, match="env: reference"):
        preview_openclaw_configuration(
            path,
            runtime_id="openclaw-local",
            profile_id="openclaw-review",
            mode="external",
            gateway="wss://gateway.example.invalid",
            executable="openclaw",
            auth_kind="token",
            auth_ref="plaintext-token",
            model_route_id="original",
            capabilities=["review"],
        )


def test_gui_contract_contains_runtime_neutral_setup_and_explicit_migration() -> None:
    html = (ROOT / "src/execraft/assets/gui/index.html").read_text(encoding="utf-8")
    runtime_js = (ROOT / "src/execraft/assets/gui/runtime-control.js").read_text(encoding="utf-8")
    onboarding_js = (ROOT / "src/execraft/assets/gui/onboarding-view.js").read_text(encoding="utf-8")

    for control_id in (
        "projectExecutionView",
        "executionSetupProfile",
        "executionSetupPreview",
        "runtimeMigrationPreview",
        "runtimeMigrationApply",
        "openclawSetupPreview",
        "openclawSetupDiagnose",
    ):
        assert f'id="{control_id}"' in html
    assert "projectProvidersView" not in html
    assert "/api/runtime/execution/setup/preview" in runtime_js
    assert "/api/runtime/migration/preview" in runtime_js
    assert "/api/runtime/openclaw/setup/preview" in runtime_js
    assert "/api/onboarding/execution" in onboarding_js


def test_execution_inventory_reports_source_schema_not_normalized_schema(tmp_path: Path) -> None:
    _source, project = _render_modern_project(tmp_path)
    agents_path = project.configured_path("agents_file")
    assert agents_path is not None
    agents_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 3,
                "providers": {
                    "worker": {
                        "adapter": "codex",
                        "provider_id": "worker",
                        "binary": sys.executable,
                        "enabled": True,
                        "capabilities": ["implement", "review"],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = ExecutionInventory().inspect(project)

    assert report.source_schema_version == 3
    assert report.ready_profiles
    assert not any(item.code == "execution.configuration_invalid" for item in report.findings)


def test_model_route_edit_preserves_redacted_credential_reference(tmp_path: Path) -> None:
    path = _base_v4(tmp_path / "agents.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["model_routes"]["original"]["credential_ref"] = "env:LOCAL_MODEL_TOKEN"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    preview = preview_model_route_configuration(
        path,
        profile_id="local-coder",
        route_id="original",
        provider="opencode",
        model="deepseek-v4-flash-free",
        credential_ref="",
    )
    rendered = yaml.safe_load(preview.rendered_yaml)

    assert rendered["model_routes"]["original"]["credential_ref"] == "env:LOCAL_MODEL_TOKEN"
    assert "LOCAL_MODEL_TOKEN" in preview.rendered_yaml


def test_openclaw_setup_can_create_local_model_route_and_target(tmp_path: Path) -> None:
    path = _base_v4(tmp_path / "agents.yaml")
    preview = preview_openclaw_configuration(
        path,
        runtime_id="openclaw-local",
        profile_id="openclaw-local-implement",
        mode="managed",
        gateway="ws://127.0.0.1:18789",
        executable="openclaw",
        auth_kind="token",
        auth_ref="env:OPENCLAW_GATEWAY_TOKEN",
        model_route_id="qwen-local",
        target_id="local-ollama",
        capabilities=["implement"],
        route_id="qwen-local",
        provider="ollama",
        model="qwen3-coder:30b",
        endpoint="http://127.0.0.1:11434/v1",
        target_kind="local",
    )
    execution = parse_execution_config(
        yaml.safe_load(preview.rendered_yaml), include_disabled=True
    )

    profile = execution.agent("openclaw-local-implement")
    assert execution.runtime(profile.runtime_id).kind == RuntimeKind.OPENCLAW
    assert execution.model_route(profile.model_route_id).provider == "ollama"
    assert execution.target(profile.target_id).kind.value == "local"


def test_openclaw_setup_can_create_inference_satellite_route(tmp_path: Path) -> None:
    path = _base_v4(tmp_path / "agents.yaml")
    preview = preview_openclaw_configuration(
        path,
        runtime_id="openclaw-local",
        profile_id="openclaw-satellite-review",
        mode="managed",
        gateway="ws://127.0.0.1:18789",
        executable="openclaw",
        auth_kind="token",
        auth_ref="env:OPENCLAW_GATEWAY_TOKEN",
        model_route_id="qwen-satellite",
        target_id="gpu-satellite",
        capabilities=["review"],
        route_id="qwen-satellite",
        provider="ollama",
        model="qwen3-coder:30b",
        endpoint="http://192.0.2.10:11434/v1",
        target_kind="inference_endpoint",
    )
    execution = parse_execution_config(
        yaml.safe_load(preview.rendered_yaml), include_disabled=True
    )

    profile = execution.agent("openclaw-satellite-review")
    assert execution.target(profile.target_id).kind.value == "inference_endpoint"
    assert execution.target(profile.target_id).endpoint == "http://192.0.2.10:11434/v1"
    assert execution.model_route(profile.model_route_id).default_target == "gpu-satellite"


def test_optional_invalid_model_registry_does_not_invalidate_mixed_v4_execution(
    tmp_path: Path,
) -> None:
    source, project = _render_modern_project(tmp_path)
    agents_path = project.configured_path("agents_file")
    assert agents_path is not None
    raw = yaml.safe_load(agents_path.read_text(encoding="utf-8"))
    raw["runtimes"]["native-codex"]["binary"] = sys.executable
    raw["agents"]["codex"]["enabled"] = True
    raw["runtimes"]["openclaw-external"] = {
        "kind": "openclaw",
        "mode": "external",
        "gateway": "ws://127.0.0.1:18789",
        "auth_kind": "token",
        "auth_ref": "env:OPENCLAW_GATEWAY_TOKEN",
    }
    raw["agents"]["openclaw-review"] = {
        "runtime": "openclaw-external",
        "model_route": "opencode-free",
        "enabled": True,
        "capabilities": ["review"],
    }
    agents_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    opencode_dir = project.configured_path("opencode_dir")
    assert opencode_dir is not None
    (opencode_dir / "providers.yaml").write_text("schema_version: [broken\n", encoding="utf-8")

    inventory = ExecutionInventory().inspect(project)
    readiness = ReadinessService().evaluate(project, source_root=source)
    checks = {item.id: item for item in readiness.checks}

    assert inventory.ready_profiles
    assert not any(item.code == "execution.configuration_invalid" for item in inventory.findings)
    assert any("Model endpoint registry is invalid" in warning for warning in inventory.warnings)
    assert checks["execution"].status.value == "ready"
    assert checks["model_registry"].status.value == "warning"
    assert not checks["model_registry"].blocks


def test_runtime_topology_stays_visible_when_optional_registry_is_invalid(tmp_path: Path) -> None:
    _source, project, service = _render_registered_modern_project(tmp_path)
    agents_path = project.configured_path("agents_file")
    assert agents_path is not None
    raw = yaml.safe_load(agents_path.read_text(encoding="utf-8"))
    raw["runtimes"]["native-codex"]["binary"] = sys.executable
    raw["agents"]["codex"]["enabled"] = True
    raw["runtimes"]["openclaw-external"] = {
        "kind": "openclaw",
        "mode": "external",
        "gateway": "ws://127.0.0.1:18789",
        "auth_kind": "token",
        "auth_ref": "env:OPENCLAW_GATEWAY_TOKEN",
    }
    raw["agents"]["openclaw-review"] = {
        "runtime": "openclaw-external",
        "model_route": "opencode-free",
        "enabled": True,
        "capabilities": ["review"],
    }
    agents_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    opencode_dir = project.configured_path("opencode_dir")
    assert opencode_dir is not None
    (opencode_dir / "providers.yaml").write_text("schema_version: [broken\n", encoding="utf-8")

    topology = runtime_topology_snapshot(service, project.id)

    assert {item["kind"] for item in topology["runtimes"]} == {"native", "openclaw"}
    assert any(item["id"] == "openclaw-review" for item in topology["profiles"])
    assert topology["execution_lanes"]
    openclaw_lane = next(
        item for item in topology["execution_lanes"] if "openclaw-review" in item["profile_ids"]
    )
    assert openclaw_lane["runtime_kind"] == "openclaw"
    assert openclaw_lane["availability"] == "unknown"  # passive topology never probes
    assert any("Model endpoint registry is invalid" in warning for warning in topology["warnings"])


def test_setup_snapshot_exposes_external_openclaw_mode_auth_and_sandbox(tmp_path: Path) -> None:
    _source, project, service = _render_registered_modern_project(tmp_path)
    agents_path = project.configured_path("agents_file")
    assert agents_path is not None
    preview = preview_openclaw_configuration(
        agents_path,
        runtime_id="openclaw-external",
        profile_id="openclaw-external-review",
        mode="external",
        gateway="wss://gateway.example.invalid",
        executable="openclaw",
        auth_kind="token",
        auth_ref="env:OPENCLAW_GATEWAY_TOKEN",
        model_route_id="opencode-free",
        capabilities=["review"],
    )
    apply_openclaw_configuration(agents_path, preview)

    setup = runtime_setup_snapshot(service, project.id)

    runtime = next(item for item in setup["openclaw_runtimes"] if item["id"] == "openclaw-external")
    profile = next(item for item in setup["openclaw_profiles"] if item["id"] == "openclaw-external-review")
    assert runtime["mode"] == "external"
    assert runtime["gateway"] == "wss://gateway.example.invalid"
    assert runtime["authentication_configured"] is True
    assert profile["sandbox_enabled"] is True
    assert "auth_ref" not in runtime


def test_redacted_model_route_edit_preserves_existing_target_metadata(tmp_path: Path) -> None:
    path = _base_v4(tmp_path / "agents.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["execution_targets"]["local-ollama"] = {
        "kind": "local",
        "endpoint": "http://127.0.0.1:11434/v1",
        "concurrency_group": "local-gpu",
    }
    raw["model_routes"]["original"].update(
        {
            "credential_ref": "env:LOCAL_MODEL_TOKEN",
            "default_target": "local-ollama",
            "endpoint": "http://127.0.0.1:11434/v1",
        }
    )
    raw["agents"]["local-coder"]["target"] = "local-ollama"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    preview = preview_model_route_configuration(
        path,
        profile_id="local-coder",
        route_id="original",
        provider="opencode",
        model="deepseek-v4-flash-free-v2",
        target_id="local-ollama",
        target_kind="local",
        endpoint="http://127.0.0.1:11434/v1",
        credential_ref="",
    )
    rendered = yaml.safe_load(preview.rendered_yaml)

    assert rendered["model_routes"]["original"]["credential_ref"] == "env:LOCAL_MODEL_TOKEN"
    assert rendered["execution_targets"]["local-ollama"]["concurrency_group"] == "local-gpu"
    assert "LOCAL_MODEL_TOKEN" not in preview.as_mapping()["diff"]
    assert "credential_ref: <configured>" in preview.as_mapping()["diff"]


def test_openclaw_edit_preserves_redacted_auth_and_custom_executable(tmp_path: Path) -> None:
    path = _base_v4(tmp_path / "agents.yaml")
    first = preview_openclaw_configuration(
        path,
        runtime_id="openclaw-local",
        profile_id="openclaw-review",
        mode="managed",
        gateway="ws://127.0.0.1:18789",
        executable="/opt/openclaw/bin/openclaw",
        auth_kind="token",
        auth_ref="env:OPENCLAW_GATEWAY_TOKEN",
        model_route_id="original",
        capabilities=["review"],
    )
    apply_openclaw_configuration(path, first)

    second = preview_openclaw_configuration(
        path,
        runtime_id="openclaw-local",
        profile_id="openclaw-review",
        mode="managed",
        gateway="ws://127.0.0.1:18790",
        executable="openclaw",  # GUI default: preserve an existing custom path.
        auth_kind="token",
        auth_ref="",  # redacted GUI edit: preserve the configured reference.
        model_route_id="original",
        capabilities=["review"],
    )
    rendered = yaml.safe_load(second.rendered_yaml)
    runtime = rendered["runtimes"]["openclaw-local"]

    assert runtime["auth_ref"] == "env:OPENCLAW_GATEWAY_TOKEN"
    assert runtime["executable"] == "/opt/openclaw/bin/openclaw"
    assert "OPENCLAW_GATEWAY_TOKEN" not in second.as_mapping()["diff"]
    assert "auth_ref: <configured>" in second.as_mapping()["diff"]


def test_execution_setup_refuses_symlinked_agents_file(tmp_path: Path) -> None:
    real = _base_v4(tmp_path / "agents.real.yaml")
    link = tmp_path / "agents.yaml"
    link.symlink_to(real.name)

    with pytest.raises(ExecutionSetupError, match="must not be a symlink"):
        preview_model_route_configuration(
            link,
            profile_id="local-coder",
            route_id="qwen-local",
            provider="ollama",
            model="qwen3-coder:30b",
        )


def test_openclaw_setup_refuses_symlinked_agents_file(tmp_path: Path) -> None:
    real = _base_v4(tmp_path / "agents.real.yaml")
    link = tmp_path / "agents.yaml"
    link.symlink_to(real.name)

    with pytest.raises(OpenClawSetupError, match="must not be a symlink"):
        preview_openclaw_configuration(
            link,
            runtime_id="openclaw-local",
            profile_id="openclaw-review",
            mode="managed",
            gateway="ws://127.0.0.1:18789",
            executable="openclaw",
            auth_kind="token",
            auth_ref="env:OPENCLAW_GATEWAY_TOKEN",
            model_route_id="original",
            capabilities=["review"],
        )
