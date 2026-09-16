#!/usr/bin/env python3
"""Enforce architecture boundaries and prevent growth of recorded debt ceilings."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ArchitectureFailure(RuntimeError):
    """Raised when one enforced source boundary is violated."""


def _tree(relative: str) -> ast.Module:
    path = ROOT / relative
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise ArchitectureFailure(f"required function {name!r} is missing")


def _line_count(node: ast.FunctionDef) -> int:
    return int(node.end_lineno or node.lineno) - node.lineno + 1


def _imports_module(tree: ast.Module, module: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            return True
        if isinstance(node, ast.Import):
            if any(alias.name == module for alias in node.names):
                return True
    return False


def _imported_modules(tree: ast.Module) -> set[str]:
    """Return absolute module names imported by *tree* for boundary checks."""

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def _check_file_size_limits() -> list[str]:
    """Reject new oversized modules and growth in the recorded baseline."""

    baseline_violations = {
        "src/execraft/archive/manager.py": 922,
        "src/execraft/cli.py": 2411,
        "src/execraft/gui/server.py": 2945,
        "src/execraft/gui/task_lifecycle.py": 1094,
        "src/execraft/gui/workspace_changes.py": 952,
        "src/execraft/onboarding/start.py": 893,
        "src/execraft/onboarding/task_definition.py": 1016,
        "src/execraft/orchestrate/agent_console.py": 1354,
        "src/execraft/orchestrate/orchestrator.py": 8300,
        "src/execraft/orchestrate/scope_recovery.py": 1847,
        "src/execraft/process/supervision.py": 1213,
        "src/execraft/orchestrate/supervisor.py": 1535,
        "src/execraft/replan/service.py": 1603,
        "src/execraft/repository_sync/service.py": 998,
        "src/execraft/workspace/lifecycle_safety.py": 833,
        "src/execraft/workspace/task_git.py": 893,
    }
    failures: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        if path.name == "__init__.py":
            continue
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        relative = str(path.relative_to(ROOT))
        allowed = baseline_violations.get(relative, 800)
        if line_count > allowed:
            failures.append(
                f"{relative} exceeds {allowed} line budget ({line_count} lines)"
            )
    return failures



def _check_frontend_file_size_limits() -> list[str]:
    """Prevent the Run workbench from regrowing a single JavaScript coordinator."""

    limits = {
        "src/execraft/assets/gui/app.js": 1600,
        "src/execraft/assets/gui/execution-trace-view.js": 360,
        "src/execraft/assets/gui/profile-maintenance.js": 350,
        "src/execraft/assets/gui/workspace-changes-view.js": 400,
    }
    failures: list[str] = []
    for relative, allowed in limits.items():
        path = ROOT / relative
        if not path.exists():
            failures.append(f"required frontend module is missing: {relative}")
            continue
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        if line_count > allowed:
            failures.append(
                f"{relative} exceeds {allowed} line budget ({line_count} lines)"
            )
    return failures

def _check_gui_private_member_references() -> list[str]:
    """Keep private class members resolvable so browser module parsing stays valid.

    A ``this.#member`` reference without a matching declaration in the same
    module is an early SyntaxError in browsers, which silently disables the
    whole GUI module graph. ``node --check`` does not validate private names,
    so this guard closes that gap deterministically.
    """

    gui_root = ROOT / "src" / "execraft" / "assets" / "gui"
    declaration = re.compile(
        r"^\s*(?:static\s+)?(?:async\s+)?(?:get\s+|set\s+)?#(\w+)\s*[=(;]",
        re.MULTILINE,
    )
    reference = re.compile(r"this\.#(\w+)")
    failures: list[str] = []
    for path in sorted(gui_root.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        declared = {match.group(1) for match in declaration.finditer(text)}
        for name in sorted({match.group(1) for match in reference.finditer(text)}):
            if name not in declared:
                failures.append(
                    f"{path.relative_to(ROOT)} references undeclared private member this.#{name}"
                )
    return failures

def _check_function_size_limits() -> list[str]:
    """Check no function exceeds 150 lines (non-regression only for now)."""
    # Known oversized functions are recorded as non-regression debt; new ones fail.
    baseline_violations = {
        "src/execraft/bootstrap.py:_materialize_standard_project",
        "src/execraft/cli.py:cmd_project",
        "src/execraft/cli.py:cmd_task",
        "src/execraft/cli.py:cmd_workspace",
        "src/execraft/completion/service.py:complete",
        "src/execraft/onboarding/service.py:create_task",
        "src/execraft/onboarding/start.py:_ensure_plan",
        "src/execraft/onboarding/start.py:_execute",
        "src/execraft/project.py:load_project",
        "src/execraft/replan/agent.py:propose",
        "src/execraft/replan/impact.py:analyze_impact",
        "src/execraft/replan/service.py:create_candidate",
        "src/execraft/workspace/lifecycle_safety.py:_repository_checks",
        "src/execraft/archive/manager.py:preflight",
        "src/execraft/agents/live_session.py:managed_jsonl_session",
        "src/execraft/agents/opencode_adapter.py:execute",
        "src/execraft/cli_parsers/orchestration.py:add_orchestration_command",
        "src/execraft/cli_parsers/workspace.py:add_workspace_commands",
        "src/execraft/gui/routes/dashboard.py:dispatch_post",
        "src/execraft/gui/server.py:_run_control",
        "src/execraft/orchestrate/agent_attempt.py:execute_attempt",
        "src/execraft/orchestrate/context.py:_candidate_blocks",
        "src/execraft/orchestrate/context.py:enrich",
        "src/execraft/orchestrate/execution_trace.py:build_execution_trace",
        "src/execraft/orchestrate/orchestrator.py:_execute_agent",
        "src/execraft/orchestrate/orchestrator.py:_run_decomposition",
        "src/execraft/orchestrate/orchestrator.py:_run_pipeline_unlocked",
        "src/execraft/orchestrate/orchestrator.py:_run_supervision_unlocked",
        "src/execraft/orchestrate/orchestrator.py:_run_verification",
        "src/execraft/orchestrate/orchestrator.py:_wait_for_agent_availability",
        "src/execraft/orchestrate/progress.py:_format_event",
        "src/execraft/orchestrate/provider_failover.py:execute_with_failover",
        "src/execraft/orchestrate/scope_recovery.py:_attempt_agent_scope_recovery",
        "src/execraft/orchestrate/shard_wave.py:run",
        "src/execraft/orchestrate/sharding.py:validate_decomposition_payload",
        "src/execraft/process/supervision.py:_managed_run_file_capture",
        "src/execraft/process/supervision.py:managed_run",
        "src/execraft/orchestrate/supervisor.py:from_mapping",
        "src/execraft/orchestrate/supervisor_coordinator.py:execute_supervisor_delegations",
        "src/execraft/orchestrate/task_status.py:render_runtime_status",
    }
    failures: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                lines = _line_count(node)
                if lines > 150:
                    key = f"{path.relative_to(ROOT)}:{node.name}"
                    if key not in baseline_violations:
                        failures.append(
                            f"{key} exceeds 150 line limit ({lines} lines) [NEW VIOLATION]"
                        )
    return failures



def _check_export_renderer_boundary() -> list[str]:
    """Keep presentation renderers independent from canonical domain storage."""

    renderers = (
        "src/execraft/export/svg.py",
        "src/execraft/export/pdf.py",
        "src/execraft/export/layout.py",
    )
    failures: list[str] = []
    for relative in renderers:
        path = ROOT / relative
        if not path.is_file():
            failures.append(f"required export renderer module is missing: {relative}")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        forbidden = sorted(
            module
            for module in _imported_modules(tree)
            if module.startswith("execraft.") and not module.startswith("execraft.export")
        )
        if forbidden:
            failures.append(
                "Export renderer crosses the presentation boundary: "
                f"{relative}: " + ", ".join(forbidden)
            )
    return failures

def _check_project_execution_boundary() -> list[str]:
    """Keep Project Execution outside Task Work Package orchestration internals."""

    root = ROOT / "src" / "execraft" / "project_execution"
    if not root.is_dir():
        return []

    # The filesystem adapter may resolve the existing Task runtime storage
    # identity. No other Task orchestration module is part of the project-domain
    # contract; control actions enter through TaskExecutionPort callbacks.
    allowed_orchestration_imports = {
        "src/execraft/project_execution/task_projection.py": {
            "execraft.orchestrate.identity",
        },
    }
    failures: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative = str(path.relative_to(ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        orchestration_imports = {
            module
            for module in _imported_modules(tree)
            if module == "execraft.orchestrate"
            or module.startswith("execraft.orchestrate.")
        }
        unexpected = sorted(
            orchestration_imports
            - allowed_orchestration_imports.get(relative, set())
        )
        if unexpected:
            failures.append(
                "Project Execution crosses the Task orchestration boundary: "
                f"{relative}: " + ", ".join(unexpected)
            )
    return failures


def _check_project_delivery_boundary() -> list[str]:
    """Keep Project delivery provider-neutral and outside Task/runtime internals."""

    root = ROOT / "src" / "execraft" / "project_execution" / "delivery"
    if not root.is_dir():
        return []
    forbidden_prefixes = (
        "execraft.orchestrate",
        "execraft.gui",
        "execraft.remote",
        "execraft.repository_sync",
        "docker",
        "github",
    )
    failures: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative = str(path.relative_to(ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        unexpected = sorted(
            module
            for module in _imported_modules(tree)
            if any(
                module == prefix or module.startswith(prefix + ".")
                for prefix in forbidden_prefixes
            )
        )
        if unexpected:
            failures.append(
                "Project delivery depends on provider/task implementation details: "
                f"{relative}: " + ", ".join(unexpected)
            )
    return failures



def _check_task_terminology() -> list[str]:
    """Keep legacy Task terminology confined to bounded read/migration seams.

    Project Execution owns Phase/Gate/Milestone. Task Execution owns
    Task/Work Package/Stage/Check/Hold/Verification. Historical Task journals,
    PLAN headings and the retired directive/config keys remain readable only in
    the explicitly allowlisted compatibility modules below.
    """

    failures: list[str] = []
    source_root = ROOT / "src" / "execraft"

    globally_forbidden = (
        "ProjectState",
        "ProjectStateRecord",
        "MilestoneDirective",
        "set_milestone_directive",
    )
    for path in sorted(source_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        relative = str(path.relative_to(ROOT))
        for token in globally_forbidden:
            if token in text:
                failures.append(
                    f"retired Task terminology {token!r} remains in canonical source: {relative}"
                )

    # These spellings are historical input only. They may never spread beyond
    # the one module responsible for reading/migrating that historical format.
    bounded_legacy_tokens = {
        "milestone-directives.json": {
            "src/execraft/orchestrate/directives.py",
        },
        '"refresh_before_gate"': {
            "src/execraft/repository_sync/policy.py",
        },
        '"repository_scope_gate_reconciled"': {
            "src/execraft/orchestrate/event_compat.py",
        },
        '"repository_sync_commit_gate_superseded"': {
            "src/execraft/orchestrate/event_compat.py",
        },
        '"repository_sync_commit_gate_recovery"': {
            "src/execraft/orchestrate/event_compat.py",
        },
    }
    for token, allowed in bounded_legacy_tokens.items():
        for path in sorted(source_root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            relative = str(path.relative_to(ROOT))
            if token in text and relative not in allowed:
                failures.append(
                    f"legacy read token {token!r} escaped its compatibility boundary: {relative}"
                )

    # The historical Task payload key is also read-only compatibility. Project
    # Execution legitimately uses ``milestone_id`` for Project Milestones, so
    # constrain this check to the Task orchestrator package.
    for path in sorted((source_root / "orchestrate").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        relative = str(path.relative_to(ROOT))
        if '"milestone_id"' in text and relative != "src/execraft/orchestrate/event_compat.py":
            failures.append(
                "legacy Task payload key 'milestone_id' escaped its compatibility boundary: "
                f"{relative}"
            )

    repository_sync = ROOT / "src" / "execraft" / "repository_sync"
    for path in sorted(repository_sync.rglob("*.py")):
        text = path.read_text(encoding="utf-8").lower()
        if "milestone" in text:
            failures.append(
                "repository synchronization must use Work Package terminology: "
                f"{path.relative_to(ROOT)}"
            )

    # Enforce the ontology over Task orchestration prose as well as symbol names.
    # Only the historical readers are allowed to mention the retired nouns.
    milestone_word = re.compile(r"\bmilestones?\b", re.IGNORECASE)
    gate_word = re.compile(r"\bgates?\b", re.IGNORECASE)
    task_word_allowlist = {
        "src/execraft/orchestrate/directives.py",
        "src/execraft/orchestrate/event_compat.py",
        "src/execraft/orchestrate/normalizer.py",
        "src/execraft/repository_sync/policy.py",
    }
    for package in (source_root / "orchestrate", source_root / "repository_sync"):
        for path in sorted(package.rglob("*.py")):
            relative = str(path.relative_to(ROOT))
            if relative in task_word_allowlist:
                continue
            text = path.read_text(encoding="utf-8")
            if milestone_word.search(text):
                failures.append(
                    "Task implementation uses Project-only Milestone terminology: "
                    f"{relative}"
                )
            if gate_word.search(text):
                failures.append(
                    "Task implementation uses Project-only Gate terminology: "
                    f"{relative}"
                )

    gui_assets = ROOT / "src" / "execraft" / "assets" / "gui"
    retired_assets = sorted(gui_assets.glob("milestone-*.js"))
    for path in retired_assets:
        failures.append(f"retired Task milestone GUI asset remains: {path.relative_to(ROOT)}")

    return failures

def validate() -> list[str]:
    """Return human-readable violations of the enforced module boundaries."""

    failures: list[str] = []
    failures.extend(_check_file_size_limits())
    failures.extend(_check_frontend_file_size_limits())
    failures.extend(_check_gui_private_member_references())
    failures.extend(_check_function_size_limits())
    failures.extend(_check_project_execution_boundary())
    failures.extend(_check_export_renderer_boundary())
    failures.extend(_check_project_delivery_boundary())
    failures.extend(_check_task_terminology())
    cli_tree = _tree("src/execraft/cli.py")
    if any(
        isinstance(node, ast.FunctionDef) and node.name == "build_parser"
        for node in cli_tree.body
    ):
        failures.append("execraft.cli must not own build_parser")
    if not _imports_module(cli_tree, "execraft.cli_parsers"):
        failures.append("execraft.cli must compose its parser through execraft.cli_parsers")

    server_tree = _tree("src/execraft/gui/server.py")
    handler = _function(server_tree, "_handler_factory")
    if _line_count(handler) > 180:
        failures.append(
            f"GUI handler transport grew to {_line_count(handler)} lines; route logic belongs in execraft.gui.routes"
        )
    for method_name, maximum in (("do_GET", 40), ("do_POST", 30)):
        method = _function(server_tree, method_name)
        if _line_count(method) > maximum:
            failures.append(
                f"{method_name} grew to {_line_count(method)} lines; maximum is {maximum}"
            )

    orchestrator_tree = _tree("src/execraft/orchestrate/orchestrator.py")
    wait_method = _function(orchestrator_tree, "_wait_for_agent_availability")
    if _line_count(wait_method) > 180:
        failures.append(
            "provider wait calculation must remain delegated to execraft.orchestrate.agent_wait"
        )
    for method_name, maximum in (
        ("_finalize_standard_package", 95),
        ("_finalize_aggregate_package", 90),
        ("_escalate_workspace_finalization_failure", 95),
    ):
        method = _function(orchestrator_tree, method_name)
        if _line_count(method) > maximum:
            failures.append(
                f"{method_name} grew to {_line_count(method)} lines; finalization "
                "policy and assessment belong in execraft.orchestrate.package_finalization"
            )

    for path in (ROOT / "src/execraft/gui/routes").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _imports_module(tree, "execraft.gui.server"):
            failures.append(f"GUI route module imports transport server: {path.relative_to(ROOT)}")
    for path in (ROOT / "src/execraft/cli_parsers").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _imports_module(tree, "execraft.cli"):
            failures.append(f"CLI parser module imports command execution: {path.relative_to(ROOT)}")

    runtime_neutral = (
        "src/execraft/execution_identity.py",
        "src/execraft/runtime_config.py",
        "src/execraft/model_routes.py",
        "src/execraft/model_registry.py",
        "src/execraft/routing_compat.py",
        "src/execraft/targets/config.py",
        "src/execraft/targets/inventory.py",
        "src/execraft/targets/health.py",
        "src/execraft/agents/profile.py",
        "src/execraft/agents/execution_config.py",
        "src/execraft/agents/execution_compat.py",
        "src/execraft/runtime/contracts.py",
        "src/execraft/runtime/product_support.py",
    )
    forbidden_runtime_dependencies = (
        "execraft.agents.factory",
        "execraft.agents.codex_adapter",
        "execraft.agents.claude_code_adapter",
        "execraft.agents.opencode_adapter",
        "execraft.agents.antigravity_cli_adapter",
        "execraft.agents.opencode_registry",
        "execraft.orchestrate.orchestrator",
    )
    for relative in runtime_neutral:
        tree = _tree(relative)
        imported = _imported_modules(tree)
        forbidden = sorted(imported.intersection(forbidden_runtime_dependencies))
        if forbidden:
            failures.append(
                f"runtime-neutral config imports concrete execution code: {relative}: "
                + ", ".join(forbidden)
            )

    # Orchestration selects candidates through the AgentRuntime protocol and
    # runtime registry. Importing a concrete runtime here would put
    # runtime-specific branching back into the control plane and break the
    # "add a runtime by registration and configuration" extension contract.
    concrete_runtime_modules = (
        "execraft.runtime.native",
        "execraft.runtime.openclaw_agent",
        "execraft.runtime.openclaw_execution_turn",
        "execraft.runtime.openclaw_gateway",
        "execraft.runtime.openclaw_remote_target",
        "execraft.runtime.openclaw_service",
        "execraft.agents.factory",
    )
    for path in sorted((ROOT / "src" / "execraft" / "orchestrate").rglob("*.py")):
        imported = _imported_modules(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
        forbidden = sorted(imported.intersection(concrete_runtime_modules))
        if forbidden:
            failures.append(
                "orchestration imports a concrete runtime implementation: "
                f"{path.relative_to(ROOT)}: " + ", ".join(forbidden)
            )

    openclaw_projection_boundary = (
        "src/execraft/runtime/openclaw_projection.py",
    )
    forbidden_openclaw_projection_dependencies = (
        "execraft.agents.factory",
        "execraft.agents.codex_adapter",
        "execraft.agents.claude_code_adapter",
        "execraft.agents.opencode_adapter",
        "execraft.agents.antigravity_cli_adapter",
        "execraft.orchestrate.orchestrator",
        "execraft.targets.health",
    )
    for relative in openclaw_projection_boundary:
        tree = _tree(relative)
        imported = _imported_modules(tree)
        forbidden = sorted(imported.intersection(forbidden_openclaw_projection_dependencies))
        if forbidden:
            failures.append(
                f"OpenClaw configuration projection crosses execution boundary: "
                f"{relative}: " + ", ".join(forbidden)
            )

    openclaw_gateway_boundary = (
        "src/execraft/runtime/openclaw_auth.py",
        "src/execraft/runtime/openclaw_protocol.py",
        "src/execraft/runtime/openclaw_gateway.py",
        "src/execraft/runtime/openclaw_discovery.py",
        "src/execraft/runtime/openclaw_process.py",
        "src/execraft/runtime/openclaw_service.py",
    )
    forbidden_openclaw_dependencies = (
        "execraft.agents.factory",
        "execraft.agents.codex_adapter",
        "execraft.agents.claude_code_adapter",
        "execraft.agents.opencode_adapter",
        "execraft.agents.antigravity_cli_adapter",
        "execraft.model_registry",
        "execraft.orchestrate.orchestrator",
        "execraft.orchestrate.scheduler",
        "execraft.targets.health",
        "execraft.targets.inventory",
    )
    for relative in openclaw_gateway_boundary:
        tree = _tree(relative)
        imported = _imported_modules(tree)
        forbidden = sorted(imported.intersection(forbidden_openclaw_dependencies))
        if forbidden:
            failures.append(
                f"OpenClaw Gateway foundation crosses control-plane/model boundary: "
                f"{relative}: " + ", ".join(forbidden)
            )

    openclaw_runtime_boundary = (
        "src/execraft/runtime/openclaw_agent.py",
    )
    forbidden_openclaw_runtime_dependencies = (
        "execraft.agents.factory",
        "execraft.agents.codex_adapter",
        "execraft.agents.claude_code_adapter",
        "execraft.agents.opencode_adapter",
        "execraft.agents.antigravity_cli_adapter",
        "execraft.model_registry",
        "execraft.orchestrate.orchestrator",
        "execraft.targets.health",
        "execraft.targets.inventory",
    )
    for relative in openclaw_runtime_boundary:
        tree = _tree(relative)
        imported = _imported_modules(tree)
        forbidden = sorted(imported.intersection(forbidden_openclaw_runtime_dependencies))
        if forbidden:
            failures.append(
                f"OpenClaw agent runtime crosses orchestration/model boundary: "
                f"{relative}: " + ", ".join(forbidden)
            )

    candidate_composition_boundary = (
        "src/execraft/agents/runtime_candidates.py",
    )
    forbidden_candidate_composition_dependencies = (
        "execraft.cli",
        "execraft.orchestrate.orchestrator",
        # Experimental-disabled implementation modules stay out of the promoted
        # runtime candidate path. Reactivation requires an explicit product-support
        # decision rather than an incidental import.
        "execraft.runtime.openclaw_remote_target",
        "execraft.runtime.openclaw_subagents",
    )
    for relative in candidate_composition_boundary:
        tree = _tree(relative)
        imported = _imported_modules(tree)
        forbidden = sorted(imported.intersection(forbidden_candidate_composition_dependencies))
        if forbidden:
            failures.append(
                f"runtime candidate composition imports command/orchestration owner: "
                f"{relative}: " + ", ".join(forbidden)
            )
    return failures


def main() -> int:
    failures = validate()
    if failures:
        for failure in failures:
            print(f"architecture error: {failure}", file=sys.stderr)
        return 1
    print("Architecture boundaries: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
