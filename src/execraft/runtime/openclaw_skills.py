"""Execraft workflow-skill projection for the OpenClaw runtime.

The canonical skill catalog remains owned by :mod:`execraft.skills`.  This module
only projects the already-selected, validated handoff skills into a disposable
OpenClaw workspace so the runtime can advertise compact skill metadata and let
the model read ``SKILL.md`` on demand instead of embedding every instruction
body in the Execraft turn prompt.

Generated workspaces live under Execraft state, never inside product repositories.
They are optimization state: losing them is safe because every selected skill is
still present in the authoritative ``StructuredHandoff`` and can be rematerialized
before a managed OpenClaw turn.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Iterable, Mapping
import uuid

import yaml

from execraft.orchestrate.context_budget import estimate_tokens
from execraft.skills import MAX_SELECTED_SKILL_BYTES, MAX_SKILL_INSTRUCTION_BYTES


_MANIFEST_NAME = ".execraft-skill-manifest.json"
_SCHEMA_VERSION = 1


class OpenClawSkillProjectionError(ValueError):
    """Raised when selected workflow skills cannot be projected safely."""


@dataclass(frozen=True)
class OpenClawProjectedSkill:
    id: str
    description: str
    source: str
    version: str
    content_hash: str
    instruction_bytes: int
    path: Path

    def as_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "source": self.source,
            "version": self.version,
            "content_hash": self.content_hash,
            "instruction_bytes": self.instruction_bytes,
            "path": str(self.path),
        }


@dataclass(frozen=True)
class OpenClawSkillProjection:
    """One deterministic selected-skill snapshot for an OpenClaw profile."""

    workspace: Path
    skills_root: Path
    skills: tuple[OpenClawProjectedSkill, ...]
    manifest_sha256: str
    instruction_bytes: int
    instruction_estimated_tokens: int
    changed: bool

    @property
    def skill_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.skills)

    @property
    def manifest_path(self) -> Path:
        return self.workspace / _MANIFEST_NAME

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "manifest_sha256": self.manifest_sha256,
            "workspace": str(self.workspace),
            "skills_root": str(self.skills_root),
            "skills": [item.as_mapping() for item in self.skills],
            "instruction_bytes": self.instruction_bytes,
            "instruction_estimated_tokens": self.instruction_estimated_tokens,
            "changed": self.changed,
        }


def openclaw_profile_workspace(
    state_root: Path, *, runtime_id: str, candidate_id: str
) -> Path:
    """Return the deterministic Execraft-owned OpenClaw workspace for a profile."""

    runtime = _safe_identifier(runtime_id, label="runtime ID")
    candidate = _safe_identifier(candidate_id, label="candidate ID")
    return (
        Path(state_root).expanduser().resolve()
        / "openclaw"
        / runtime
        / "workspaces"
        / candidate
    ).resolve()


def materialize_openclaw_skills(
    workspace: Path,
    workflow_skills: Iterable[Mapping[str, Any]],
) -> OpenClawSkillProjection:
    """Materialize exactly the selected workflow skills into ``workspace/skills``.

    The handoff is already Execraft's authoritative selection.  Projection still
    validates hashes and byte counts so a corrupt persisted handoff cannot make
    OpenClaw execute instructions different from the context epoch recorded by
    the control plane.
    """

    requested_root = Path(workspace).expanduser()
    _assert_state_owned_directory(requested_root)
    root = requested_root.resolve()
    normalized = _normalize_skills(workflow_skills)
    manifest_payload = _manifest_payload(normalized)
    manifest_bytes = _stable_json_bytes(manifest_payload)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

    current_manifest = _read_current_manifest(root / _MANIFEST_NAME)
    current_hash = str(current_manifest.get("manifest_sha256", ""))
    skills_root = root / "skills"
    changed = current_hash != manifest_sha256 or not _projection_files_match(
        skills_root, normalized
    )

    if changed:
        _replace_skill_tree(root, normalized, manifest_payload, manifest_sha256)

    projected = tuple(
        OpenClawProjectedSkill(
            id=item["id"],
            description=item["description"],
            source=item["source"],
            version=item["version"],
            content_hash=item["content_hash"],
            instruction_bytes=item["instruction_bytes"],
            path=(skills_root / item["id"] / "SKILL.md").resolve(),
        )
        for item in normalized
    )
    instructions = tuple(item["instructions"] for item in normalized)
    return OpenClawSkillProjection(
        workspace=root,
        skills_root=skills_root,
        skills=projected,
        manifest_sha256=manifest_sha256,
        instruction_bytes=sum(item["instruction_bytes"] for item in normalized),
        instruction_estimated_tokens=sum(estimate_tokens(item) for item in instructions),
        changed=changed,
    )


def load_openclaw_skill_projection(workspace: Path) -> OpenClawSkillProjection | None:
    """Read the current Execraft-owned projection without trusting skill bodies.

    This is used only for a format-only continuation where the compact repair
    handoff intentionally omits its original skills.  The manifest lets Execraft
    preserve the previously validated lazy-skill snapshot for that same session.
    """

    requested_root = Path(workspace).expanduser()
    _assert_state_owned_directory(requested_root)
    root = requested_root.resolve()
    raw = _read_current_manifest(root / _MANIFEST_NAME)
    if not raw:
        return None
    skills_raw = raw.get("skills")
    if not isinstance(skills_raw, list):
        return None
    try:
        projected = tuple(
            OpenClawProjectedSkill(
                id=str(item["id"]),
                description=str(item.get("description", "")),
                source=str(item.get("source", "")),
                version=str(item.get("version", "")),
                content_hash=str(item["content_hash"]),
                instruction_bytes=int(item.get("instruction_bytes", 0)),
                path=(root / "skills" / str(item["id"]) / "SKILL.md").resolve(),
            )
            for item in skills_raw
            if isinstance(item, Mapping)
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not _loaded_projection_is_valid(root, raw, projected):
        return None
    return OpenClawSkillProjection(
        workspace=root,
        skills_root=root / "skills",
        skills=projected,
        manifest_sha256=str(raw.get("manifest_sha256", "")),
        instruction_bytes=sum(item.instruction_bytes for item in projected),
        instruction_estimated_tokens=int(raw.get("instruction_estimated_tokens", 0) or 0),
        changed=False,
    )



def _loaded_projection_is_valid(
    workspace: Path,
    manifest: Mapping[str, Any],
    projected: tuple[OpenClawProjectedSkill, ...],
) -> bool:
    declared_hash = str(manifest.get("manifest_sha256", "")).strip()
    semantic = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    semantic_hash = hashlib.sha256(_stable_json_bytes(semantic)).hexdigest()
    if not declared_hash or semantic_hash != declared_hash:
        return False
    skills_root = workspace / "skills"
    if skills_root.is_symlink() or not skills_root.is_dir():
        return False
    for item in projected:
        path = skills_root / item.id / "SKILL.md"
        if path.is_symlink() or not path.is_file():
            return False
        try:
            metadata, instructions = _parse_rendered_skill(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            return False
        if str(metadata.get("name", "")).strip() != item.id:
            return False
        if str(metadata.get("description", "")).strip() != item.description:
            return False
        encoded = instructions.encode("utf-8")
        if len(encoded) != item.instruction_bytes:
            return False
        if hashlib.sha256(encoded).hexdigest() != item.content_hash:
            return False
    return True


def _parse_rendered_skill(text: str) -> tuple[Mapping[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing skill front matter")
    try:
        closing = next(
            index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"
        )
    except StopIteration as exc:
        raise ValueError("unterminated skill front matter") from exc
    metadata = yaml.safe_load("\n".join(lines[1:closing])) or {}
    if not isinstance(metadata, Mapping):
        raise ValueError("skill front matter must be a mapping")
    return metadata, "\n".join(lines[closing + 1 :]).strip()

def _normalize_skills(
    workflow_skills: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_bytes = 0
    for raw in workflow_skills:
        if not isinstance(raw, Mapping):
            raise OpenClawSkillProjectionError("workflow skill must be a mapping")
        skill_id = _safe_identifier(raw.get("id", ""), label="workflow skill ID")
        if skill_id in seen:
            raise OpenClawSkillProjectionError(
                f"duplicate selected workflow skill {skill_id!r}"
            )
        seen.add(skill_id)
        description = str(raw.get("description", "")).strip()
        if not description:
            raise OpenClawSkillProjectionError(
                f"workflow skill {skill_id!r} has no description"
            )
        instructions = str(raw.get("instructions", "")).strip()
        if not instructions:
            raise OpenClawSkillProjectionError(
                f"workflow skill {skill_id!r} has no instructions"
            )
        instruction_bytes = len(instructions.encode("utf-8"))
        if instruction_bytes > MAX_SKILL_INSTRUCTION_BYTES:
            raise OpenClawSkillProjectionError(
                f"workflow skill {skill_id!r} contains {instruction_bytes} instruction "
                f"bytes; maximum is {MAX_SKILL_INSTRUCTION_BYTES}"
            )
        declared_bytes = raw.get("instruction_bytes")
        if declared_bytes not in {None, ""} and int(declared_bytes) != instruction_bytes:
            raise OpenClawSkillProjectionError(
                f"workflow skill {skill_id!r} instruction byte count does not match content"
            )
        content_hash = hashlib.sha256(instructions.encode("utf-8")).hexdigest()
        declared_hash = str(raw.get("content_hash", "")).strip()
        if declared_hash and declared_hash != content_hash:
            raise OpenClawSkillProjectionError(
                f"workflow skill {skill_id!r} content hash does not match instructions"
            )
        total_bytes += instruction_bytes
        normalized.append(
            {
                "id": skill_id,
                "description": description,
                "source": str(raw.get("source", "")).strip(),
                "version": str(raw.get("version", "1")).strip() or "1",
                "content_hash": content_hash,
                "instruction_bytes": instruction_bytes,
                "instructions": instructions,
                "selection_reason": str(raw.get("selection_reason", "role policy")).strip()
                or "role policy",
            }
        )
    if total_bytes > MAX_SELECTED_SKILL_BYTES:
        raise OpenClawSkillProjectionError(
            f"selected workflow skills contain {total_bytes} instruction bytes; "
            f"maximum is {MAX_SELECTED_SKILL_BYTES}"
        )
    return tuple(normalized)


def _manifest_payload(skills: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    entries = [
        {
            key: item[key]
            for key in (
                "id",
                "description",
                "source",
                "version",
                "content_hash",
                "instruction_bytes",
                "selection_reason",
            )
        }
        for item in skills
    ]
    instruction_tokens = sum(estimate_tokens(item["instructions"]) for item in skills)
    # The hash is intentionally over semantic projection metadata, not generated
    # absolute paths or timestamps, so identical selected skills are stable
    # across machines and restarts.
    semantic = {
        "schema_version": _SCHEMA_VERSION,
        "skills": entries,
        "instruction_estimated_tokens": instruction_tokens,
    }
    return semantic


def _replace_skill_tree(
    workspace: Path,
    skills: tuple[dict[str, Any], ...],
    manifest_payload: Mapping[str, Any],
    manifest_sha256: str,
) -> None:
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(workspace, 0o700)
    except OSError:
        pass
    skills_root = workspace / "skills"
    if skills_root.is_symlink():
        raise OpenClawSkillProjectionError(
            f"OpenClaw skill root must not be a symlink: {skills_root}"
        )
    staging = workspace / f".skills.tmp-{uuid.uuid4().hex}"
    old = workspace / f".skills.old-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    try:
        for item in skills:
            skill_dir = staging / item["id"]
            skill_dir.mkdir(mode=0o700)
            skill_file = skill_dir / "SKILL.md"
            _write_private(skill_file, _render_skill(item))
        if skills_root.exists():
            os.replace(skills_root, old)
        os.replace(staging, skills_root)
        manifest = {
            **dict(manifest_payload),
            "manifest_sha256": manifest_sha256,
        }
        _write_private(workspace / _MANIFEST_NAME, _stable_json_bytes(manifest))
    except BaseException:
        if not skills_root.exists() and old.exists():
            os.replace(old, skills_root)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if old.exists():
            shutil.rmtree(old)


def _render_skill(item: Mapping[str, Any]) -> bytes:
    # Keep front matter on the broad AgentSkills-compatible minimum. Execraft's
    # version/hash/source metadata lives in its adjacent manifest instead of
    # relying on OpenClaw-specific frontmatter extensions.
    frontmatter = yaml.safe_dump(
        {"name": item["id"], "description": item["description"]},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    text = f"---\n{frontmatter}\n---\n\n{item['instructions'].strip()}\n"
    return text.encode("utf-8")


def _projection_files_match(
    skills_root: Path, skills: tuple[dict[str, Any], ...]
) -> bool:
    if skills_root.is_symlink() or not skills_root.is_dir():
        return False
    expected = {item["id"] for item in skills}
    actual = {
        child.name
        for child in skills_root.iterdir()
        if child.is_dir() and not child.is_symlink()
    }
    if actual != expected:
        return False
    for item in skills:
        path = skills_root / item["id"] / "SKILL.md"
        if path.is_symlink() or not path.is_file():
            return False
        try:
            if path.read_bytes() != _render_skill(item):
                return False
        except OSError:
            return False
    return True


def _read_current_manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _write_private(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _assert_state_owned_directory(path: Path) -> None:
    # Existing path components may be attacker/user controlled state after a
    # crash or manual edit. Refuse a symlinked terminal workspace so projection
    # cannot be redirected into a product repository or arbitrary host path.
    if path.is_symlink():
        raise OpenClawSkillProjectionError(
            f"OpenClaw skill workspace must not be a symlink: {path}"
        )


def _safe_identifier(value: object, *, label: str) -> str:
    text = str(value).strip()
    if not text or not text.replace("-", "_").isidentifier():
        raise OpenClawSkillProjectionError(f"invalid {label}: {text!r}")
    return text


def _stable_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")
