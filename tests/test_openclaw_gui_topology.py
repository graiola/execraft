"""Regression coverage for OpenClaw GUI/runtime topology contracts.

These tests exercise browser-independent presentation helpers with Node and the
shipped sample schema-v4 configuration without requiring Playwright.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from execraft.model_registry import load_model_route_registry
from execraft.runtime.config_migration import normalize_execution_for_operator
from execraft.runtime.topology import build_runtime_topology


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is required for dashboard JavaScript tests")
def test_runtime_control_diagnostics_and_credentials_use_redacted_topology_contract(
    tmp_path: Path,
) -> None:
    """Target health and credential status follow the backend's public keys."""

    source = (ROOT / "src/execraft/assets/gui/runtime-control.js").read_text(
        encoding="utf-8"
    )
    utilities = (ROOT / "src/execraft/assets/gui/ui-utils.js").read_text(encoding="utf-8")
    (tmp_path / "runtime-control.mjs").write_text(
        source.replace('from "./ui-utils.js"', 'from "./ui-utils.mjs"'),
        encoding="utf-8",
    )
    (tmp_path / "ui-utils.mjs").write_text(utilities, encoding="utf-8")
    script = tmp_path / "runtime-control-regression.mjs"
    script.write_text(
        textwrap.dedent(
            """
            import {
              diagnosticsHealthy,
              modelRouteInventoryDetail,
            } from "./runtime-control.mjs";

            const fail = (message) => { throw new Error(message); };

            if (diagnosticsHealthy({execution_target: {healthy: false}}))
              fail("target-only failure was reported healthy");
            if (diagnosticsHealthy({execution_target: {healthy: null}}))
              fail("unknown target health was reported healthy");
            if (!diagnosticsHealthy({execution_target: {healthy: true}}))
              fail("healthy target was reported unhealthy");
            if (!diagnosticsHealthy({
              runtime: {healthy: true},
              model_route: {status: "healthy"},
              execution_target: {healthy: true},
            })) fail("healthy layered diagnostics were reported unhealthy");

            const rendered = modelRouteInventoryDetail({
              provider: "openai",
              model: "gpt-test",
              provider_alias: "cloud",
              api_family: "openai-compatible",
              credential_configured: true,
              // A defensive canary: the browser helper must ignore this even if
              // a future payload accidentally contains a secret-bearing field.
              credential_ref: "env:TOP_SECRET_REFERENCE",
            });
            if (!rendered.includes("credential configured"))
              fail(`credential status was not rendered: ${rendered}`);
            if (rendered.includes("TOP_SECRET_REFERENCE") || rendered.includes("credential_ref"))
              fail(`credential reference leaked into inventory: ${rendered}`);

            const anonymous = modelRouteInventoryDetail({
              provider: "ollama",
              model: "qwen",
              credential_configured: false,
            });
            if (anonymous.includes("credential configured"))
              fail(`credential status was invented: ${anonymous}`);
            """
        ),
        encoding="utf-8",
    )
    subprocess.run([NODE, script], check=True, cwd=tmp_path)


def test_runtime_control_source_does_not_depend_on_credential_reference() -> None:
    """The browser only consumes the redacted credential-presence boolean."""

    source = (ROOT / "src/execraft/assets/gui/runtime-control.js").read_text(
        encoding="utf-8"
    )
    assert "route.credential_ref" not in source
    assert "route.credential_configured" in source
    assert '"execution_target"' in source


def test_shipped_sample_v4_topology_exposes_native_and_openclaw_profiles() -> None:
    """Freeze the mixed-runtime sample config as a normalization fixture."""

    project = ROOT / "projects/sample"
    raw = yaml.safe_load((project / "agents.yaml").read_text(encoding="utf-8"))
    registry = load_model_route_registry(project / "opencode/providers.yaml")

    execution, warnings = normalize_execution_for_operator(raw, model_registry=registry)
    topology = build_runtime_topology(execution)

    assert warnings == ()
    assert topology["schema_version"] == 4
    assert topology["source_schema_version"] == 4

    runtime_kinds = {row["kind"] for row in topology["runtimes"]}
    assert {"native", "openclaw"} <= runtime_kinds

    profiles = {row["id"]: row for row in topology["profiles"]}
    assert "codex" in profiles
    assert profiles["codex"]["runtime_kind"] == "native"
    for profile_id in (
        "openclaw-local-review",
        "openclaw-gpu-a-review",
        "openclaw-gpu-b-review",
    ):
        assert profiles[profile_id]["runtime_kind"] == "openclaw"
        assert {"review", "fix_review"} <= set(profiles[profile_id]["capabilities"])

    assert len(topology["execution_targets"]) == 3
    assert len(topology["runtimes"]) == 5
