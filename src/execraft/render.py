"""Render a disposable provider/editor workspace shell.

Built-in provider assets and generic skills ship with :mod:`execraft`. A project may
add or override files, but product repositories never receive generated AI state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from execraft.project import ProjectError, load_project as load_project_descriptor
from execraft.agents.opencode_registry import (
    OpenCodeProviderRegistry,
    load_opencode_provider_registry,
)


@dataclass
class RenderedFile:
    relative_path: str
    sha256: str
    source: str


@dataclass
class RenderResult:
    workspace_root: Path
    rendered_files: list[RenderedFile] = field(default_factory=list)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "workspace_root": ".",
            "files": {
                item.relative_path: {"sha256": item.sha256, "source": item.source}
                for item in self.rendered_files
            },
        }


def _asset_root() -> Path:
    return Path(__file__).resolve().parent / "assets"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _copy_file(
    src: Path, dst: Path, workspace_root: Path | None = None, *, source: str = "project"
) -> RenderedFile:
    dst.parent.mkdir(parents=True, exist_ok=True)
    content = src.read_bytes()
    dst.write_bytes(content)
    base = workspace_root if workspace_root else dst.parent
    return RenderedFile(
        relative_path=dst.relative_to(base).as_posix(),
        sha256=_sha256(content),
        source=source,
    )


def _write_file(
    dst: Path, content: str, workspace_root: Path | None = None, *, source: str = "generated"
) -> RenderedFile:
    dst.parent.mkdir(parents=True, exist_ok=True)
    raw = content.encode("utf-8")
    dst.write_bytes(raw)
    base = workspace_root if workspace_root else dst.parent
    return RenderedFile(
        relative_path=dst.relative_to(base).as_posix(),
        sha256=_sha256(raw),
        source=source,
    )


def _deduplicate(items: Iterable[RenderedFile]) -> list[RenderedFile]:
    # Project overlays are rendered after built-ins and intentionally win.
    merged: dict[str, RenderedFile] = {}
    for item in items:
        merged[item.relative_path] = item
    return [merged[key] for key in sorted(merged)]


def load_project(project_dir: Path) -> dict[str, Any]:
    try:
        load_project_descriptor(project_dir)
    except ProjectError as exc:
        if "project.yaml not found" in str(exc):
            raise FileNotFoundError(str(exc)) from exc
        raise ValueError(str(exc)) from exc
    with open(project_dir / "project.yaml", encoding="utf-8") as project_file:
        return yaml.safe_load(project_file)


def _configured_source(
    project_dir: Path,
    project: dict[str, Any],
    key: str,
    fallback: str,
) -> Path:
    relative = str(project.get(key, fallback))
    path_base = str(project.get("path_base", "registry_root")).strip()
    base = project_dir if path_base == "project_directory" else project_dir.parent.parent
    return (base / relative).resolve()


def _copy_named_directories(
    sources: list[tuple[Path, str]],
    workspace_root: Path,
    destination_root: Path,
    *,
    required_overlay: bool = False,
) -> list[RenderedFile]:
    rendered: list[RenderedFile] = []
    for index, (source_dir, source_label) in enumerate(sources):
        if not source_dir.exists():
            if required_overlay and index == len(sources) - 1:
                raise FileNotFoundError(f"Configured directory not found: {source_dir}")
            continue
        for child in sorted(source_dir.iterdir()):
            if not child.is_dir():
                continue
            source_file = child / "SKILL.md"
            if not source_file.is_file():
                continue
            rendered.append(
                _copy_file(
                    source_file,
                    destination_root / child.name / "SKILL.md",
                    workspace_root,
                    source=source_label,
                )
            )
    return _deduplicate(rendered)


def render_skills(
    project_dir: Path,
    workspace_root: Path,
    project: dict[str, Any],
) -> list[RenderedFile]:
    project_skills = _configured_source(
        project_dir,
        project,
        "skills_dir",
        f"projects/{project.get('project', project_dir.name)}/skills",
    )
    if not project_skills.exists():
        raise FileNotFoundError(f"Skills directory not found: {project_skills}")
    return _copy_named_directories(
        [
            (_asset_root() / "skills", "builtin"),
            (project_skills, "project"),
        ],
        workspace_root,
        workspace_root / ".agents" / "skills",
        required_overlay=True,
    )


def _copy_provider_files(
    builtin_root: Path,
    overlay_root: Path,
    workspace_root: Path,
    destination_root: Path,
    category: str,
) -> list[RenderedFile]:
    rendered: list[RenderedFile] = []
    for source_root, label in ((builtin_root, "builtin"), (overlay_root, "project")):
        source = source_root / category
        if not source.is_dir():
            continue
        for file in sorted(source.iterdir()):
            if file.is_file() and file.name != ".gitkeep":
                rendered.append(
                    _copy_file(file, destination_root / category / file.name, workspace_root, source=label)
                )
    return _deduplicate(rendered)


_BUILTIN_POLICIES: dict[str, dict[str, Any]] = {
    "read-only": {
        "id": "read-only",
        "filesystem": "read-only",
        "network": "deny",
        "docker": "deny",
        "hardware": "deny",
    },
    "workspace-write": {
        "id": "workspace-write",
        "filesystem": "workspace-write",
        "network": "ask",
        "docker": "ask",
        "hardware": "deny",
    },
    "host-integration": {
        "id": "host-integration",
        "filesystem": "workspace-write",
        "network": "ask",
        "docker": "ask",
        "hardware": "ask",
    },
}


def _load_policy(project_dir: Path, project: dict[str, Any], profile: str | None) -> dict[str, Any]:
    selected = profile or str(project.get("default_policy", "workspace-write"))
    policies = _configured_source(
        project_dir,
        project,
        "policies_dir",
        f"projects/{project.get('project', project_dir.name)}/policies",
    )
    path = policies / f"{selected}.yaml"
    if not path.is_file():
        builtin = _BUILTIN_POLICIES.get(selected)
        if builtin is None:
            raise FileNotFoundError(f"Policy profile not found: {path}")
        return dict(builtin)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or raw.get("id") != selected:
        raise ValueError(f"Invalid policy profile: {path}")
    return raw


def _opencode_permissions(
    policy: dict[str, Any], *, dossier_dir: Path | None = None
) -> dict[str, Any]:
    filesystem = policy.get("filesystem", "read-only")
    # OpenCode's non-interactive ``run --auto`` mode does not reliably answer
    # permission prompts raised by nested @general subagents.  A writable
    # execraft workspace is already the policy boundary, so make shell execution
    # explicit instead of leaving an unattended run blocked on ``ask``.
    shell = "deny" if filesystem == "read-only" else "allow"
    mutate = "deny" if filesystem == "read-only" else "allow"
    permissions: dict[str, Any] = {
        "*": "ask",
        "read": "allow",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "edit": mutate,
        "write": mutate,
        "bash": shell,
        "external_directory": "deny",
        "doom_loop": "ask",
    }
    if dossier_dir is not None:
        dossier = dossier_dir.resolve().as_posix()
        external_pattern = f"{dossier}/**"
        permissions["external_directory"] = {
            "*": "deny",
            dossier: "allow",
            external_pattern: "allow",
        }
        # The canonical task dossier is context, not an implementation target.
        # OpenCode documents `edit` as the capability for edit/write/patch; retain the
        # legacy write key too for compatibility with installed CLI versions.
        permissions["edit"] = {"*": mutate, external_pattern: "deny"}
        permissions["write"] = {"*": mutate, external_pattern: "deny"}
    return permissions


def render_opencode(
    project_dir: Path,
    workspace_root: Path,
    project: dict[str, Any],
    *,
    policy_profile: str | None = None,
    dossier_dir: Path | None = None,
) -> list[RenderedFile]:
    overlay = _configured_source(
        project_dir,
        project,
        "opencode_dir",
        f"projects/{project.get('project', project_dir.name)}/opencode",
    )
    if not overlay.exists():
        raise FileNotFoundError(f"OpenCode directory not found: {overlay}")
    builtin = _asset_root() / "providers" / "opencode"
    rendered: list[RenderedFile] = []
    rendered.extend(_copy_provider_files(builtin, overlay, workspace_root, workspace_root / ".opencode", "agents"))
    rendered.extend(_copy_provider_files(builtin, overlay, workspace_root, workspace_root / ".opencode", "commands"))
    policy = _load_policy(project_dir, project, policy_profile)
    config = {
        "$schema": "https://opencode.ai/config.json",
        "permission": _opencode_permissions(policy, dossier_dir=dossier_dir),
    }

    registry = load_opencode_provider_registry(overlay / "providers.yaml")
    generated_providers = registry.as_opencode_providers()
    if generated_providers:
        config["provider"] = generated_providers

    config_overlay_path = overlay / "config.json"
    if config_overlay_path.is_file():
        try:
            config_overlay = json.loads(config_overlay_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid OpenCode config overlay: {config_overlay_path}: {exc}"
            ) from exc
        if not isinstance(config_overlay, dict):
            raise ValueError(
                f"OpenCode config overlay must contain an object: {config_overlay_path}"
            )
        # Provider/model definitions may be project-specific, but the generated
        # policy remains authoritative. Prevent an overlay from weakening the
        # workspace filesystem and external-directory safety contract.
        config_overlay.pop("$schema", None)
        config_overlay.pop("permission", None)
        overlay_providers = config_overlay.get("provider", {})
        if not isinstance(overlay_providers, dict):
            raise ValueError(
                f"OpenCode config overlay provider must be an object: {config_overlay_path}"
            )
        duplicate_providers = sorted(set(generated_providers) & set(overlay_providers))
        if duplicate_providers:
            raise ValueError(
                "OpenCode providers must be declared in either providers.yaml or "
                "config.json, not both: " + ", ".join(duplicate_providers)
            )
        config = _merge_json(config, config_overlay)
    rendered.append(
        _write_file(workspace_root / "opencode.json", json.dumps(config, indent=2) + "\n", workspace_root)
    )
    rendered.append(
        _write_file(
            workspace_root / ".opencode" / ".gitignore",
            "node_modules\npackage.json\npackage-lock.json\nbun.lock\n",
            workspace_root,
        )
    )
    return _deduplicate(rendered)


def refresh_opencode_providers(
    workspace_root: Path, registry: OpenCodeProviderRegistry
) -> list[str]:
    """Re-sync generated OpenCode provider blocks with the project registry.

    ``opencode.json`` is generated once when the workspace shell is rendered,
    but ``providers.yaml`` keeps evolving: operators add satellites, retarget a
    base URL, or publish a new model set while a long task is still running.
    The OpenCode CLI is launched with the rendered file as ``OPENCODE_CONFIG``,
    so a provider added after the render is unknown to it and every invocation
    fails with ``Model not found``.  Endpoint probes and ``agents doctor`` read
    the live registry instead, so they keep reporting the endpoint as healthy
    and the mismatch stays invisible until a run burns an attempt.

    Only registry-managed providers are rewritten.  Permissions, overlay
    providers, and any other generated key are preserved, and the generated
    manifest is updated so the refresh does not register as workspace drift.
    Returns the provider ids that were added or updated.
    """

    config_path = workspace_root / "opencode.json"
    generated = registry.as_opencode_providers()
    if not generated or not config_path.is_file():
        return []
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A hand-corrupted workspace config is an operator problem; rewriting it
        # here would silently discard the permission contract it carries.
        return []
    if not isinstance(config, dict):
        return []
    providers = config.get("provider")
    if not isinstance(providers, dict):
        providers = {}
    stale = sorted(
        provider_id
        for provider_id, block in generated.items()
        if providers.get(provider_id) != block
    )
    if not stale:
        return []
    providers.update({provider_id: generated[provider_id] for provider_id in stale})
    config["provider"] = providers
    rendered = _write_file(
        config_path, json.dumps(config, indent=2) + "\n", workspace_root
    )
    _update_manifest_entry(workspace_root, rendered)
    return stale


def _update_manifest_entry(workspace_root: Path, rendered: RenderedFile) -> None:
    """Record a post-render file rewrite in the generated manifest."""

    manifest_path = workspace_root / ".execraft" / "generated-manifest.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    if not isinstance(manifest, dict):
        return
    files = manifest.get("files")
    if not isinstance(files, dict) or rendered.relative_path not in files:
        return
    files[rendered.relative_path] = {
        "sha256": rendered.sha256,
        "source": rendered.source,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _merge_json(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_json(result[key], value)
        else:
            result[key] = value
    return result


def render_claude(
    project_dir: Path,
    workspace_root: Path,
    project: dict[str, Any],
    *,
    policy_profile: str | None = None,
) -> list[RenderedFile]:
    overlay = _configured_source(
        project_dir,
        project,
        "claude_dir",
        f"projects/{project.get('project', project_dir.name)}/claude",
    )
    if not overlay.exists():
        raise FileNotFoundError(f"Claude directory not found: {overlay}")
    builtin = _asset_root() / "providers" / "claude"
    rendered: list[RenderedFile] = []
    rendered.extend(_copy_provider_files(builtin, overlay, workspace_root, workspace_root / ".claude", "agents"))
    rendered.extend(_copy_provider_files(builtin, overlay, workspace_root, workspace_root / ".claude", "commands"))

    settings: dict[str, Any] = {}
    settings_file = overlay / "settings.json"
    if settings_file.is_file():
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
    policy = _load_policy(project_dir, project, policy_profile)
    mode = "plan" if policy.get("filesystem") == "read-only" else "acceptEdits"
    settings = _merge_json(settings, {"permissions": {"defaultMode": mode}})
    rendered.append(
        _write_file(
            workspace_root / ".claude" / "settings.json",
            json.dumps(settings, indent=2) + "\n",
            workspace_root,
        )
    )
    claude_md = overlay / "CLAUDE.md"
    if claude_md.is_file():
        rendered.append(_copy_file(claude_md, workspace_root / "CLAUDE.md", workspace_root))
    else:
        rendered.append(_write_file(workspace_root / "CLAUDE.md", "@AGENTS.md\n", workspace_root))
    return _deduplicate(rendered)


def _replace_toml_scalar(text: str, key: str, value: str) -> str:
    line = f'{key} = "{value}"'
    pattern = re.compile(rf"(?m)^\s*{re.escape(key)}\s*=.*$")
    if pattern.search(text):
        return pattern.sub(line, text)
    return text.rstrip() + "\n" + line + "\n"


def render_codex(
    project_dir: Path,
    workspace_root: Path,
    project: dict[str, Any],
    *,
    policy_profile: str | None = None,
) -> list[RenderedFile]:
    overlay = _configured_source(
        project_dir,
        project,
        "codex_dir",
        f"projects/{project.get('project', project_dir.name)}/codex",
    )
    if not overlay.exists():
        raise FileNotFoundError(f"Codex directory not found: {overlay}")
    source = overlay / "config.toml"
    text = source.read_text(encoding="utf-8") if source.is_file() else ""
    policy = _load_policy(project_dir, project, policy_profile)
    sandbox = "read-only" if policy.get("filesystem") == "read-only" else "workspace-write"
    text = _replace_toml_scalar(text, "sandbox_mode", sandbox)
    text = _replace_toml_scalar(text, "approval_policy", "on-request")
    return [
        _write_file(workspace_root / ".codex" / "config.toml", text, workspace_root)
    ]


def render_manifest(workspace_root: Path, rendered_files: list[RenderedFile]) -> RenderedFile:
    manifest = {
        "schema_version": 1,
        "workspace_root": ".",
        "files": {
            item.relative_path: {"sha256": item.sha256, "source": item.source}
            for item in rendered_files
        },
    }
    return _write_file(
        workspace_root / ".execraft" / "generated-manifest.json",
        json.dumps(manifest, indent=2) + "\n",
        workspace_root,
    )


def render_instructions(
    project_dir: Path,
    workspace_root: Path,
    project: dict[str, Any],
) -> list[RenderedFile]:
    source_dir = _configured_source(
        project_dir,
        project,
        "instructions_dir",
        f"projects/{project.get('project', project_dir.name)}/instructions",
    )
    agents = source_dir / "AGENTS.md"
    if agents.is_file():
        return [_copy_file(agents, workspace_root / "AGENTS.md", workspace_root)]
    return [
        _write_file(
            workspace_root / "AGENTS.md",
            "# Generated development workspace\n\n"
            "This shell is generated by `execraft`; edit product source only in registered worktrees.\n",
            workspace_root,
        )
    ]



def render_devcontainer(
    project_dir: Path,
    workspace_root: Path,
    project: dict[str, Any],
) -> list[RenderedFile]:
    """Copy an optional profile-managed dev-container into the workspace shell."""

    relative = str(project.get("devcontainer_dir", "") or "").strip()
    if not relative:
        return []
    source_dir = _configured_source(project_dir, project, "devcontainer_dir", relative)
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Configured devcontainer directory not found: {source_dir}")
    rendered: list[RenderedFile] = []
    for source in sorted(path for path in source_dir.rglob("*") if path.is_file()):
        rendered.append(
            _copy_file(
                source,
                workspace_root / ".devcontainer" / source.relative_to(source_dir),
                workspace_root,
                source="project",
            )
        )
    return rendered

def _workspace_relative(workspace_root: Path, value: str | Path) -> str:
    return Path(os.path.relpath(Path(value).resolve(), workspace_root.resolve())).as_posix()


def render_vscode(
    workspace_root: Path,
    project: dict[str, Any],
    *,
    task_id: str,
    repositories: list[dict[str, str]],
    dossier_dir: Path | None = None,
) -> list[RenderedFile]:
    folders: list[dict[str, str]] = [{"name": "Workspace", "path": "."}]
    if dossier_dir is not None:
        folders.append({"name": "Task dossier", "path": _workspace_relative(workspace_root, dossier_dir)})
    for item in repositories:
        folders.append({"name": item["id"], "path": _workspace_relative(workspace_root, item["worktree_path"])})

    exclude = {
        "**/.git/**": True,
        "**/.pytest_cache/**": True,
        "**/__pycache__/**": True,
        "**/.execraft/runs/**": True,
    }
    editor = project.get("editor") or {}
    for pattern in editor.get("exclude") or []:
        exclude[str(pattern)] = True
    extensions = list(editor.get("extensions") or [])
    terminal_profiles: dict[str, Any] = {
        "Workspace": {
            "path": "bash",
            "args": [
                "-lc",
                f"cd {shlex.quote(str(workspace_root.resolve()))} && exec bash",
            ],
        }
    }
    for item in repositories:
        terminal_profiles[item["id"]] = {
            "path": "bash",
            "args": [
                "-lc",
                f"cd {shlex.quote(str(Path(item['worktree_path']).resolve()))} && exec bash",
            ],
        }

    settings = {
        "files.exclude": exclude,
        "search.exclude": exclude,
        "terminal.integrated.defaultProfile.linux": "Workspace",
        "terminal.integrated.profiles.linux": terminal_profiles,
    }
    settings.update(editor.get("settings") or {})
    task_cwd = {"cwd": str(workspace_root.resolve())}
    task_entries: list[dict[str, Any]] = [
        {"label": "execraft: status", "type": "shell", "command": f"execraft workspace status {shlex.quote(task_id)}", "problemMatcher": [], "options": task_cwd},
        {"label": "execraft: refresh workspace", "type": "shell", "command": f"execraft workspace refresh {shlex.quote(task_id)}", "problemMatcher": [], "options": task_cwd},
        {"label": "execraft: focused verification", "type": "shell", "command": f"execraft workspace verify {shlex.quote(task_id)} --profile focused", "problemMatcher": [], "group": "test", "options": task_cwd},
        {"label": "execraft: full task verification", "type": "shell", "command": f"execraft workspace verify {shlex.quote(task_id)} --profile full", "problemMatcher": [], "group": "test", "options": task_cwd},
        {"label": "execraft: browser prepare", "type": "shell", "command": f"execraft browser prepare --task-id {shlex.quote(task_id)} --workspace-root {shlex.quote(str(workspace_root.resolve()))}", "problemMatcher": [], "options": task_cwd},
    ]
    for configured_task in editor.get("tasks") or []:
        if isinstance(configured_task, dict):
            rendered_task = json.loads(json.dumps(configured_task).replace("{{ task_id }}", task_id))
            rendered_task.setdefault("options", task_cwd)
            task_entries.append(rendered_task)
    tasks = {"version": "2.0.0", "tasks": task_entries}
    workspace_file = {"folders": folders, "settings": settings, "extensions": {"recommendations": extensions}}
    return [
        _write_file(workspace_root / ".vscode" / "settings.json", json.dumps(settings, indent=2) + "\n", workspace_root),
        _write_file(workspace_root / ".vscode" / "tasks.json", json.dumps(tasks, indent=2) + "\n", workspace_root),
        _write_file(workspace_root / ".vscode" / "extensions.json", json.dumps({"recommendations": extensions}, indent=2) + "\n", workspace_root),
        _write_file(workspace_root / f"{task_id}.code-workspace", json.dumps(workspace_file, indent=2) + "\n", workspace_root),
    ]


def _assert_owned_clean_target(workspace_root: Path, force_clean: bool) -> None:
    if force_clean:
        return
    marker = workspace_root / ".execraft" / "workspace.yaml"
    if not marker.is_file():
        raise ValueError(
            f"refusing to clean unowned workspace {workspace_root}; "
            "create it through 'execraft workspace start' or pass force_clean=True"
        )


def render_workspace(
    project_dir: Path,
    workspace_root: Path,
    *,
    clean: bool = False,
    force_clean: bool = False,
    task_id: str | None = None,
    repositories: list[dict[str, str]] | None = None,
    dossier_dir: Path | None = None,
    policy_profile: str | None = None,
) -> RenderResult:
    project = load_project(project_dir)
    workspace_root.mkdir(parents=True, exist_ok=True)
    if clean:
        _assert_owned_clean_target(workspace_root, force_clean)
        for name in (".agents", ".opencode", ".claude", ".codex", ".vscode", ".devcontainer"):
            target = workspace_root / name
            if target.exists():
                shutil.rmtree(target)
        for name in ("AGENTS.md", "CLAUDE.md", "opencode.json"):
            (workspace_root / name).unlink(missing_ok=True)

    rendered: list[RenderedFile] = []
    rendered.extend(render_skills(project_dir, workspace_root, project))
    rendered.extend(
        render_opencode(
            project_dir,
            workspace_root,
            project,
            policy_profile=policy_profile,
            dossier_dir=dossier_dir,
        )
    )
    rendered.extend(render_claude(project_dir, workspace_root, project, policy_profile=policy_profile))
    rendered.extend(render_codex(project_dir, workspace_root, project, policy_profile=policy_profile))
    rendered.extend(render_instructions(project_dir, workspace_root, project))
    rendered.extend(render_devcontainer(project_dir, workspace_root, project))
    if task_id:
        rendered.extend(
            render_vscode(
                workspace_root,
                project,
                task_id=task_id,
                repositories=repositories or [],
                dossier_dir=dossier_dir,
            )
        )
    rendered = _deduplicate(rendered)
    rendered.append(render_manifest(workspace_root, rendered))
    return RenderResult(workspace_root=workspace_root, rendered_files=rendered)
