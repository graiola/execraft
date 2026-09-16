"""Compatibility facade for project onboarding and health checks.

New code should use :mod:`execraft.onboarding`; the public functions in this
module remain stable for existing callers and tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from execraft.agents.opencode_registry import (
    OpenCodeRegistryError,
    load_opencode_provider_registry,
)
from execraft.onboarding.discovery import (
    DiscoveredRepo,
    DiscoveryEngine,
    DiscoveryReport,
    discover,
    suggest_verification as _suggest_verification,
)
from execraft.onboarding.service import OnboardingService
from execraft.onboarding.profiles import (
    ProjectProfileCatalog,
    ProjectProfileRenderer,
    ProjectTemplateContext,
    default_profile_catalog,
)
from execraft.onboarding.templates import (
    TemplateCatalog,
    TemplateDescriptor,
    default_task_template,
)
from execraft.onboarding.transactions import CreationTransactionError
from execraft.project import ProjectError, load_project, resolve_project_source_root


def _is_git_worktree(path: Path) -> bool:
    marker = path / ".git"
    return marker.is_dir() or marker.is_file()


def _materialize_standard_project(
    report: DiscoveryReport, project_dir: Path
) -> None:
    """Render the built-in standard project template into an empty directory."""

    project_file = project_dir / "project.yaml"
    source_root = Path(report.source_root).resolve()
    repos: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, repo in enumerate(report.repositories, start=1):
        repo_path = Path(repo.path).resolve()
        try:
            relative = repo_path.relative_to(source_root).as_posix() or "."
        except ValueError:
            # Discovery findings own ambiguity reporting.  Rendering remains a
            # pure operation and only applies a deterministic path fallback.
            relative = repo_path.name
        repository_id = repo.id
        if repository_id in used_ids:
            repository_id = f"{repository_id}_{index}"
        used_ids.add(repository_id)
        repos.append(
            {
                "id": repository_id,
                "path": relative,
                "role": repo.role,
                "required": True,
                "base_branch": repo.base_branch,
                "workspace_name": repository_id,
            }
        )

    descriptor: dict[str, Any] = {
        "schema_version": 2,
        "project": report.project_id,
        "description": f"Project bootstrapped from {source_root.name}",
        "path_base": "project_directory",
        "repositories": repos,
        "task_templates": "task_templates",
        "instructions_dir": "instructions",
        "skills_dir": "skills",
        "opencode_dir": "provider_overrides/opencode",
        "claude_dir": "provider_overrides/claude",
        "codex_dir": "provider_overrides/codex",
        "vscode_dir": "vscode",
        "policies_dir": "policies",
        "verification_file": "verification.yaml",
        "agents_file": "agents.yaml",
        "resources_file": "resources.yaml",
        "default_policy": "workspace-write",
        "policy_profiles": {
            "read-only": {"description": "Planning and review only."},
            "workspace-write": {"description": "Edits limited to task-owned worktrees."},
            "host-integration": {"description": "Explicit host/runtime integration opt-in."},
        },
        "capabilities": ["git.worktrees"],
        "editor": {"extensions": [], "settings": {}, "exclude": []},
    }

    for relative in (
        "task_templates",
        "instructions",
        "skills",
        "provider_overrides/opencode/agents",
        "provider_overrides/opencode/commands",
        "provider_overrides/claude/agents",
        "provider_overrides/claude/commands",
        "provider_overrides/codex",
        "vscode",
        "policies",
        "tasks",
    ):
        (project_dir / relative).mkdir(parents=True, exist_ok=True)

    project_file.write_text(
        yaml.safe_dump(descriptor, sort_keys=False, width=1000), encoding="utf-8"
    )
    (project_dir / "verification.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "require_commands": True,
                "commands": _suggest_verification(report),
                "known_failures": [],
            },
            sort_keys=False,
            width=1000,
        ),
        encoding="utf-8",
    )
    (project_dir / "agents.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "scheduling": {
                    "allow_same_provider_review": True,
                    "rotate_agents": True,
                    "minimum_read_only_enforcement": "provider_policy",
                    "allow_cross_package_progress": True,
                    "prefer_capable_agents": True,
                    "complexity_medium_threshold": 45,
                    "complexity_high_threshold": 70,
                    "capability_weight_band": 15,
                    "agent_wait_poll_max_seconds": 300,
                    "agent_known_deadline_poll_max_seconds": 1800,
                    "blocking_agent_wait_max_seconds": 1800,
                    "decomposition": {
                        "enabled": True,
                        "complexity_threshold": 60,
                        "target_shard_complexity": 40,
                        "maximum_shard_complexity": 55,
                        "maximum_shards": 8,
                        "trigger_when_no_eligible_provider": True,
                    },
                    "parallel_shards": {
                        "enabled": True,
                        "max_workers": 2,
                        "stages": ["implement", "review", "fix_review"],
                        "require_disjoint_repositories_for_writes": True,
                    },
                    "recovery_playbooks": {
                        "enabled": True,
                        "review_exhausted": {
                            "enabled": True,
                            "max_rescue_cycles": 2,
                            "max_findings": 32,
                            "prefer_non_supervisor_fixer": True,
                            "allow_supervisor_fallback": False,
                        },
                    },
                },
                "supervisor": {
                    "enabled": True,
                    "agents": ["codex", "claude-code"],
                    "skill": "ai-supervise",
                    "max_attempts_per_incident": 3,
                    "max_agent_delegations": 6,
                    "max_delegation_rounds": 2,
                    "max_runtime_minutes": 60,
                    "ask_human_when_uncertain": True,
                    "require_human_for_destructive_actions": True,
                    "auto_decision": {
                        "enabled": False,
                        "minimum_weight": 70,
                        "minimum_margin": 20,
                        "max_per_incident": 2,
                    },
                },
                "commit": {
                    "mode": "automatic",
                },
                "providers": {
                    "codex": {
                        "adapter": "codex",
                        "enabled": False,
                        "capabilities": [
                            "decompose",
                            "implement",
                            "review",
                            "fix_review",
                            "supervise",
                        ],
                        "capability_weight": 100,
                        "capability_weights": {"decompose": 100, "supervise": 100},
                        "max_complexity_by_capability": {
                            "decompose": 100,
                            "supervise": 100,
                        },
                        "concurrency_group": "codex",
                        "priority": 400,
                        "inactivity_timeout_seconds": 900,
                        "max_internal_retry_delay_seconds": 120,
                    },
                    "claude": {
                        "adapter": "claude-code",
                        "enabled": False,
                        "capabilities": [
                            "decompose",
                            "implement",
                            "review",
                            "fix_review",
                            "supervise",
                        ],
                        "capability_weight": 95,
                        "capability_weights": {"decompose": 95, "supervise": 95},
                        "max_complexity_by_capability": {
                            "decompose": 100,
                            "supervise": 100,
                        },
                        "concurrency_group": "claude-code",
                        "priority": 300,
                        "inactivity_timeout_seconds": 900,
                        "max_internal_retry_delay_seconds": 120,
                    },
                    "antigravity": {
                        "adapter": "antigravity-cli",
                        "enabled": False,
                        "provider_id": "antigravity",
                        "binary": "agy",
                        "model": "",
                        "capabilities": ["decompose", "implement", "review", "fix_review"],
                        "capability_weight": 90,
                        "capability_weights": {"decompose": 90},
                        "max_complexity_by_capability": {"decompose": 95},
                        "concurrency_group": "antigravity",
                        "dangerously_skip_permissions": True,
                        "sandbox_enabled": False,
                        "policy_paths": [],
                        "priority": 250,
                        "timeout_seconds": 3600,
                        "inactivity_timeout_seconds": 900,
                        "max_internal_retry_delay_seconds": 120,
                    },
                    "opencode_zen_free": {
                        "adapter": "opencode",
                        "enabled": False,
                        "provider_id": "opencode-zen-free",
                        "aliases": ["opencode"],
                        "model": "opencode/deepseek-v4-flash-free",
                        "capabilities": ["decompose", "implement", "review", "fix_review"],
                        "capability_weight": 65,
                        "capability_weights": {
                            "decompose": 70,
                            "implement": 65,
                            "review": 70,
                            "fix_review": 60,
                        },
                        "max_complexity_by_capability": {
                            "decompose": 70,
                            "implement": 45,
                            "review": 80,
                            "fix_review": 50,
                        },
                        "agent_by_capability": {
                            "review": "ai-reviewer",
                            "fix_review": "ai-fixer",
                        },
                        "format_repair_agent": "ai-contract",
                        "concurrency_group": "opencode-zen-free",
                        "auto_approve": True,
                        "priority": 200,
                        "inactivity_timeout_seconds": 900,
                        "max_internal_retry_delay_seconds": 120,
                    },
                    "opencode_go": {
                        "adapter": "opencode",
                        "enabled": False,
                        "provider_id": "opencode-go",
                        "model": "opencode-go/deepseek-v4-flash",
                        "capabilities": ["decompose", "implement", "review", "fix_review"],
                        "capability_weight": 75,
                        "capability_weights": {"decompose": 75},
                        "max_complexity_by_capability": {
                            "decompose": 75, "implement": 70, "review": 85, "fix_review": 70
                        },
                        "concurrency_group": "opencode-go",
                        "agent_by_capability": {
                            "review": "ai-reviewer",
                            "fix_review": "ai-fixer",
                        },
                        "format_repair_agent": "ai-contract",
                        "priority": 100,
                        "inactivity_timeout_seconds": 900,
                        "max_internal_retry_delay_seconds": 120,
                    },
                    "ollama_local": {
                        "adapter": "opencode",
                        "enabled": False,
                        "provider_id": "ollama-local",
                        "binary": "opencode",
                        "model": "ollama-local/qwen2.5-coder:14b",
                        "capabilities": ["decompose", "implement", "review", "fix_review"],
                        "capability_weight": 55,
                        "capability_weights": {
                            "decompose": 60, "review": 45, "fix_review": 60
                        },
                        "max_complexity_by_capability": {
                            "decompose": 65,
                            "implement": 35,
                            "review": 70,
                            "fix_review": 45,
                        },
                        "agent_by_capability": {
                            "review": "ai-reviewer",
                            "fix_review": "ai-fixer",
                        },
                        "format_repair_agent": "ai-contract",
                        "concurrency_group": "local-ollama",
                        "priority": 50,
                        "auto_approve": False,
                        "timeout_seconds": 3600,
                        "inactivity_timeout_seconds": 900,
                        "max_internal_retry_delay_seconds": 120,
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (project_dir / "provider_overrides" / "opencode" / "providers.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "endpoints": {
                    "local-ollama": {
                        "enabled": True,
                        "provider_id": "ollama-local",
                        "kind": "openai-compatible",
                        "name": "Ollama (local)",
                        "base_url": "http://127.0.0.1:11434/v1",
                        "base_url_env": "EXECRAFT_OLLAMA_LOCAL_URL",
                        "connect_timeout_seconds": 5,
                        "models": {
                            "qwen2.5-coder:14b": {
                                "name": "Qwen 2.5 Coder 14B",
                                "limit": {"context": 32768, "output": 8192},
                            }
                        },
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (project_dir / "resources.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "warning_percent": 75,
                "pause_percent": 90,
                "resume_percent": 80,
                "minimum_free_gb": 15,
                "providers": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    policies = {
        "read-only": {"filesystem": "read-only", "network": "deny", "docker": "deny", "hardware": "deny"},
        "workspace-write": {"filesystem": "task-worktrees", "network": "ask", "docker": "deny", "hardware": "deny"},
        "host-integration": {"filesystem": "task-worktrees", "network": "allow", "docker": "ask", "hardware": "ask"},
    }
    for policy_id, body in policies.items():
        (project_dir / "policies" / f"{policy_id}.yaml").write_text(
            yaml.safe_dump(
                {"schema_version": 1, "id": policy_id, **body, "destructive_git": "deny"},
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    for name, title in (
        (
            "README.md",
            "# Task dossier: {{ title }}\n\n"
            "`BRIEF.md` and `PLAN.md` describe intent and design; `PLAN.graph.yaml` "
            "is the executable package graph; `TASK.yaml` is the repository/lifecycle "
            "manifest; `REVIEW.md` is the findings ledger; `HANDOFF.md` is append-only "
            "engineering history. `RUNTIME_STATUS.md` is generated locally from durable "
            "orchestrator state and ignored by Git.\n",
        ),
        (".gitignore", "/RUNTIME_STATUS.md\n"),
        ("BRIEF.md", "# Brief: {{ title }}\n\n## Goal\n\n## Constraints\n"),
        ("PLAN.md", "# Plan: {{ title }}\n\nSee `PLAN.graph.yaml` for the executable graph.\n"),
        (
            "HANDOFF.md",
            "# Engineering history: {{ title }}\n\n"
            "> Historical append-only record. Read `RUNTIME_STATUS.md` for live progress.\n",
        ),
        ("REVIEW.md", "# Review: {{ title }}\n\n## Findings\n"),
    ):
        (project_dir / "task_templates" / name).write_text(title, encoding="utf-8")
    (project_dir / "instructions" / "AGENTS.md").write_text(
        "# Generated project workspace\n\nUse `execraft` as the control plane. Edit only registered task worktrees.\n",
        encoding="utf-8",
    )
    for directory in ("skills", "provider_overrides/opencode/agents", "provider_overrides/opencode/commands", "provider_overrides/claude/agents", "provider_overrides/claude/commands", "provider_overrides/codex", "vscode"):
        (project_dir / directory / ".gitkeep").touch()
    return None


def default_template_catalog(
    profile_catalog: ProjectProfileCatalog | None = None,
) -> TemplateCatalog:
    """Return legacy and profile-backed project/task templates.

    ``standard@1`` is retained as a compatibility baseline for old projects and
    migration adoption. Unversioned ``standard`` resolves to the current profile-backed
    ``standard@3`` template.
    """

    catalog = TemplateCatalog()
    catalog.register(
        TemplateDescriptor(
            id="standard",
            version=1,
            kind="project",
            description="Legacy conservative multi-provider project scaffold.",
            renderer=lambda context, destination: _materialize_standard_project(
                context.report if isinstance(context, ProjectTemplateContext) else context,
                destination,
            ),
        )
    )
    profiles = profile_catalog or default_profile_catalog()
    renderer = ProjectProfileRenderer(profiles)
    for profile in profiles.profiles():
        if profile.reference == "standard@1":
            continue
        catalog.register(
            TemplateDescriptor(
                id=profile.id,
                version=profile.version,
                kind="project",
                description=profile.description,
                renderer=renderer.render,
            )
        )
    catalog.register(default_task_template())
    return catalog


def create_onboarding_service() -> OnboardingService:
    """Construct the default shared onboarding application service."""

    profile_catalog = default_profile_catalog()
    return OnboardingService(
        templates=default_template_catalog(profile_catalog),
        profile_catalog=profile_catalog,
    )


def scaffold_project(
    report: DiscoveryReport,
    output_dir: Path,
    *,
    dry_run: bool = False,
) -> Path:
    """Generate a project through the atomic onboarding transaction layer."""

    try:
        outcome = create_onboarding_service().create_project(
            report=report,
            output_dir=output_dir,
            template_id="standard@1",
            register=False,
            dry_run=dry_run,
            accept_decisions=True,
        )
    except CreationTransactionError as exc:
        raise ProjectError(str(exc)) from exc
    if dry_run:
        return output_dir.expanduser().resolve() / report.project_id / "project.yaml"
    if outcome.path is None:  # Defensive: committed outcomes always publish a path.
        raise ProjectError("project creation completed without a published descriptor")
    return outcome.path


def doctor_project(project_file: Path, source_root: Path | None = None) -> list[str]:
    """Check a descriptor against an explicit or locally bound checkout root."""

    issues: list[str] = []
    try:
        descriptor = load_project(project_file.parent)
        root = resolve_project_source_root(descriptor, source_root)
    except ProjectError as exc:
        return [str(exc)]

    opencode_relative = descriptor.paths.get("opencode_dir")
    if opencode_relative:
        registry_path = descriptor.configured_path("opencode_dir") / "providers.yaml"
        try:
            load_opencode_provider_registry(registry_path)
        except OpenCodeRegistryError as exc:
            issues.append(str(exc))

    for repo in descriptor.repositories:
        repo_path = (root / repo.path).resolve()
        try:
            repo_path.relative_to(root)
        except ValueError:
            issues.append(f"Repository path escapes source root: {repo.path}")
            continue
        if not repo_path.is_dir():
            if repo.required:
                issues.append(f"Required repository path not found: {repo.path}")
            continue
        if not _is_git_worktree(repo_path):
            issues.append(f"Repository missing .git marker: {repo.path}")
    return issues
