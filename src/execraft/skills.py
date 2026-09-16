"""Validated workflow-skill discovery and prompt materialization.

Skills are Markdown files with YAML front matter. Built-in skills are loaded
first and project skills overlay them by ID, matching workspace rendering
semantics.  The catalog is intentionally provider-neutral: selected instructions
are embedded into the structured handoff, so every adapter receives the same
workflow contract without relying on provider-specific slash commands.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml


MAX_SKILL_INSTRUCTION_BYTES = 16 * 1024
MAX_SELECTED_SKILL_BYTES = 32 * 1024
KNOWN_SKILL_ROLES = frozenset(
    {
        "brief",
        "close",
        "commit",
        "decompose",
        "final_review",
        "fix_review",
        "implement",
        "merge",
        "plan",
        "review",
        "replan",
        "status",
        "supervise",
        "switch",
        "workspace",
    }
)


class SkillCatalogError(ValueError):
    """Raised when a skill definition or selection is invalid."""


@dataclass(frozen=True)
class SkillDefinition:
    id: str
    name: str
    description: str
    roles: tuple[str, ...]
    source: str
    path: Path
    instructions: str
    version: str = "1"
    content_hash: str = ""
    overrides: str = ""

    def supports_role(self, role: str) -> bool:
        # Project-authored legacy skills without ``roles`` remain globally
        # selectable. Built-ins declare roles explicitly to avoid presenting
        # unrelated operational skills in package execution controls.
        return not self.roles or "*" in self.roles or role in self.roles

    def as_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "roles": list(self.roles),
            "source": self.source,
            "version": self.version,
            "content_hash": self.content_hash,
            "instruction_bytes": len(self.instructions.encode("utf-8")),
            "overrides": self.overrides,
        }


@dataclass(frozen=True)
class MaterializedSkill:
    id: str
    description: str
    instructions: str
    source: str
    version: str
    content_hash: str
    instruction_bytes: int
    selection_reason: str = "role policy"

    def as_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "instructions": self.instructions,
            "source": self.source,
            "version": self.version,
            "content_hash": self.content_hash,
            "instruction_bytes": self.instruction_bytes,
            "selection_reason": self.selection_reason,
        }


class SkillCatalog:
    """Immutable catalog of built-in and project workflow skills."""

    def __init__(self, definitions: Iterable[SkillDefinition] = ()) -> None:
        by_id: dict[str, SkillDefinition] = {}
        for definition in definitions:
            by_id[definition.id] = definition
        self._by_id = by_id

    @property
    def definitions(self) -> tuple[SkillDefinition, ...]:
        return tuple(self._by_id[key] for key in sorted(self._by_id))

    @classmethod
    def load(
        cls,
        *,
        project_skills_dir: Path | None = None,
    ) -> "SkillCatalog":
        sources: list[tuple[Path, str]] = [
            (Path(__file__).resolve().parent / "assets" / "skills", "builtin")
        ]
        if project_skills_dir is not None:
            project_root = Path(project_skills_dir)
            if project_root.is_symlink():
                raise SkillCatalogError(
                    f"project skill catalog root must not be a symlink: {project_root}"
                )
            sources.append((project_root.resolve(), "project"))

        definitions: dict[str, SkillDefinition] = {}
        for directory, source in sources:
            if not directory.is_dir():
                # Project overlays are optional. Rendering may enforce a configured
                # directory separately, while orchestration must still be able to
                # use the complete built-in catalog for minimal/test projects.
                continue
            catalog_root = directory.resolve()
            for child in sorted(directory.iterdir()):
                skill_file = child / "SKILL.md"
                if child.is_symlink():
                    if skill_file.exists():
                        raise SkillCatalogError(
                            f"skill directories must not be symlinks: {child}"
                        )
                    continue
                if not child.is_dir() or not skill_file.is_file():
                    continue
                if skill_file.is_symlink():
                    raise SkillCatalogError(
                        f"skill files must not be symlinks: {skill_file}"
                    )
                resolved_skill_file = skill_file.resolve()
                if not resolved_skill_file.is_relative_to(catalog_root):
                    raise SkillCatalogError(
                        f"skill file escapes catalog root {catalog_root}: {skill_file}"
                    )
                definition = _load_skill(
                    resolved_skill_file,
                    skill_id=child.name,
                    source=source,
                )
                existing = definitions.get(definition.id)
                if existing is not None:
                    if source != "project" or existing.source != "builtin":
                        raise SkillCatalogError(
                            f"duplicate workflow skill ID {definition.id!r}: "
                            f"{existing.path} and {definition.path}"
                        )
                    if definition.overrides != "builtin":
                        raise SkillCatalogError(
                            f"project skill {definition.id!r} shadows built-in {existing.path}; "
                            "declare 'overrides: builtin' in YAML front matter to make the "
                            "replacement explicit"
                        )
                definitions[definition.id] = definition
        return cls(definitions.values())

    def definition(self, skill_id: str) -> SkillDefinition:
        normalized = str(skill_id).strip()
        try:
            return self._by_id[normalized]
        except KeyError as exc:
            raise SkillCatalogError(f"unknown workflow skill: {normalized}") from exc

    def for_role(self, role: str) -> list[SkillDefinition]:
        return [
            definition
            for definition in self.definitions
            if definition.supports_role(role)
        ]

    def validate_selection(self, role: str, skill_ids: Iterable[object]) -> list[str]:
        normalized: list[str] = []
        for raw_skill_id in skill_ids:
            skill_id = str(raw_skill_id).strip()
            if not skill_id or skill_id in normalized:
                continue
            definition = self.definition(skill_id)
            if not definition.supports_role(role):
                supported = ", ".join(definition.roles) or "all roles"
                raise SkillCatalogError(
                    f"workflow skill {skill_id!r} does not support role {role!r}; "
                    f"declared roles: {supported}"
                )
            normalized.append(skill_id)
        return normalized

    def materialize(self, role: str, skill_ids: Iterable[object]) -> list[MaterializedSkill]:
        definitions = [
            self.definition(skill_id)
            for skill_id in self.validate_selection(role, skill_ids)
        ]
        total_bytes = sum(
            len(definition.instructions.encode("utf-8"))
            for definition in definitions
        )
        if total_bytes > MAX_SELECTED_SKILL_BYTES:
            raise SkillCatalogError(
                f"selected workflow skills for role {role!r} contain {total_bytes} "
                f"instruction bytes; maximum is {MAX_SELECTED_SKILL_BYTES}"
            )
        return [
            MaterializedSkill(
                id=definition.id,
                description=definition.description,
                instructions=definition.instructions,
                source=definition.source,
                version=definition.version,
                content_hash=definition.content_hash,
                instruction_bytes=len(definition.instructions.encode("utf-8")),
            )
            for definition in definitions
        ]

    def as_mappings(self) -> list[dict[str, Any]]:
        return [definition.as_mapping() for definition in self.definitions]


def _load_skill(path: Path, *, skill_id: str, source: str) -> SkillDefinition:
    if not skill_id or not skill_id.replace("-", "_").isidentifier():
        raise SkillCatalogError(f"invalid workflow skill ID {skill_id!r}: {path}")
    text = path.read_text(encoding="utf-8")
    metadata, body = _split_front_matter(text, path=path)
    name = str(metadata.get("name", skill_id)).strip()
    if not name:
        raise SkillCatalogError(f"skill name cannot be empty: {path}")
    if name != skill_id:
        raise SkillCatalogError(
            f"skill directory/name mismatch for {path}: expected {skill_id!r}, got {name!r}"
        )
    description = str(metadata.get("description", "")).strip()
    if not description:
        raise SkillCatalogError(f"skill description is required: {path}")
    version = str(metadata.get("version", "1")).strip()
    if not version or len(version) > 64:
        raise SkillCatalogError(f"skill version must be 1..64 characters: {path}")
    overrides = str(metadata.get("overrides", "")).strip()
    if overrides not in {"", "builtin"}:
        raise SkillCatalogError(
            f"skill overrides must be empty or 'builtin': {path}"
        )
    if source != "project" and overrides:
        raise SkillCatalogError(
            f"only project skills may declare an override: {path}"
        )
    roles_raw = metadata.get("roles", [])
    if roles_raw is None:
        roles_raw = []
    if not isinstance(roles_raw, list):
        raise SkillCatalogError(f"skill roles must be a list: {path}")
    roles: list[str] = []
    for raw_role in roles_raw:
        role = str(raw_role).strip()
        if not role:
            raise SkillCatalogError(f"skill roles cannot contain empty values: {path}")
        if role != "*" and role not in KNOWN_SKILL_ROLES:
            raise SkillCatalogError(
                f"unsupported workflow role {role!r} in {path}"
            )
        if role not in roles:
            roles.append(role)
    instructions = body.strip()
    if not instructions:
        raise SkillCatalogError(f"skill instructions are empty: {path}")
    instruction_bytes = len(instructions.encode("utf-8"))
    if instruction_bytes > MAX_SKILL_INSTRUCTION_BYTES:
        raise SkillCatalogError(
            f"skill instructions in {path} contain {instruction_bytes} bytes; "
            f"maximum is {MAX_SKILL_INSTRUCTION_BYTES}"
        )
    content_hash = hashlib.sha256(instructions.encode("utf-8")).hexdigest()
    return SkillDefinition(
        id=skill_id,
        name=name,
        description=description,
        roles=tuple(roles),
        source=source,
        path=path.resolve(),
        instructions=instructions,
        version=version,
        content_hash=content_hash,
        overrides=overrides,
    )


def _split_front_matter(text: str, *, path: Path) -> tuple[Mapping[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillCatalogError(f"skill must start with YAML front matter: {path}")
    try:
        closing = next(
            index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"
        )
    except StopIteration as exc:
        raise SkillCatalogError(f"unterminated skill front matter: {path}") from exc
    try:
        metadata = yaml.safe_load("\n".join(lines[1:closing])) or {}
    except yaml.YAMLError as exc:
        raise SkillCatalogError(f"invalid skill front matter in {path}: {exc}") from exc
    if not isinstance(metadata, Mapping):
        raise SkillCatalogError(f"skill front matter must be a mapping: {path}")
    return metadata, "\n".join(lines[closing + 1 :])
