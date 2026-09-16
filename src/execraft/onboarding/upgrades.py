"""Safe reconciliation of profile-managed project descriptors.

Upgrades render the target profile into an isolated tree, compare it with the
last recorded managed-file hashes, and apply only conflict-free changes unless
an operator explicitly forces managed-file replacement. Unmanaged task dossiers
and custom files are never traversed or modified.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from execraft.persistence.atomic import atomic_write_yaml

from execraft.persistence import sha256_file
from execraft.onboarding.discovery import DiscoveryEngine
from execraft.onboarding.profiles import (
    ProjectProfileCatalog,
    ProjectProfileRenderer,
    ProjectTemplateContext,
    default_profile_catalog,
)
from execraft.onboarding.transactions import _creation_lock
from execraft.project import ProjectDescriptor, resolve_project_source_root


class ProjectUpgradeError(RuntimeError):
    """Raised when profile adoption or reconciliation cannot continue safely."""



def _safe_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise ProjectUpgradeError(f"unsafe managed-file path: {value!r}")
    return path


@dataclass(frozen=True)
class TemplateProvenance:
    profile: str
    features: tuple[str, ...]
    generated_with: str
    managed_files: Mapping[str, str]

    @classmethod
    def load(cls, path: Path) -> "TemplateProvenance":
        if not path.is_file():
            raise ProjectUpgradeError(
                f"template provenance is missing: {path}; run project upgrade --adopt first"
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, Mapping) or int(raw.get("schema_version", 0)) != 1:
            raise ProjectUpgradeError(f"invalid template provenance: {path}")
        managed = raw.get("managed_files") or {}
        if not isinstance(managed, Mapping):
            raise ProjectUpgradeError(f"managed_files must be a mapping: {path}")
        normalized: dict[str, str] = {}
        for name, digest in managed.items():
            relative = _safe_relative(str(name)).as_posix()
            digest_text = str(digest).strip().lower()
            if len(digest_text) != 64 or any(ch not in "0123456789abcdef" for ch in digest_text):
                raise ProjectUpgradeError(f"invalid managed-file digest for {relative}")
            normalized[relative] = digest_text
        features = raw.get("features") or []
        if not isinstance(features, list) or not all(isinstance(item, str) for item in features):
            raise ProjectUpgradeError(f"features must be a list: {path}")
        return cls(
            profile=str(raw.get("profile", "")).strip(),
            features=tuple(str(item).strip() for item in features if str(item).strip()),
            generated_with=str(raw.get("generated_with", "")).strip(),
            managed_files=normalized,
        )


@dataclass(frozen=True)
class UpgradeFile:
    path: str
    action: str
    current_sha256: str = ""
    baseline_sha256: str = ""
    target_sha256: str = ""
    reason: str = ""

    def as_mapping(self) -> dict[str, str]:
        return {
            "path": self.path,
            "action": self.action,
            "current_sha256": self.current_sha256,
            "baseline_sha256": self.baseline_sha256,
            "target_sha256": self.target_sha256,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class UpgradePlan:
    project_id: str
    project_dir: Path
    current_profile: str
    target_profile: str
    current_features: tuple[str, ...]
    target_features: tuple[str, ...]
    files: tuple[UpgradeFile, ...]
    provenance_missing: bool = False
    target_tree: Path | None = None

    @property
    def conflicts(self) -> tuple[UpgradeFile, ...]:
        return tuple(item for item in self.files if item.action == "conflict")

    @property
    def changes(self) -> tuple[UpgradeFile, ...]:
        return tuple(item for item in self.files if item.action in {"add", "update", "delete", "conflict"})

    @property
    def up_to_date(self) -> bool:
        return not self.provenance_missing and not self.changes

    @property
    def can_apply(self) -> bool:
        return not self.provenance_missing and not self.conflicts

    def as_mapping(self) -> dict[str, Any]:
        return {
            "project": self.project_id,
            "project_dir": str(self.project_dir),
            "current_profile": self.current_profile,
            "target_profile": self.target_profile,
            "current_features": list(self.current_features),
            "target_features": list(self.target_features),
            "provenance_missing": self.provenance_missing,
            "up_to_date": self.up_to_date,
            "can_apply": self.can_apply,
            "changes": len(self.changes),
            "conflicts": len(self.conflicts),
            "files": [item.as_mapping() for item in self.files],
        }

    def render_text(self) -> str:
        lines = [
            f"Project upgrade plan: {self.project_id}",
            f"Profile: {self.current_profile or '<untracked>'} -> {self.target_profile}",
            "Features: " + (", ".join(self.target_features) or "none"),
        ]
        if self.provenance_missing:
            lines.append("Provenance: missing; adopt the existing project before upgrading")
        for item in self.files:
            if item.action != "unchanged":
                lines.append(f"  {item.action:<8} {item.path} {item.reason}".rstrip())
        lines.append(f"Applicable: {'yes' if self.can_apply else 'no'}")
        return "\n".join(lines)


class ProjectUpgradeService:
    """Plan, adopt, and apply profile/template upgrades."""

    def __init__(
        self,
        *,
        catalog: ProjectProfileCatalog | None = None,
        discovery: DiscoveryEngine | None = None,
    ) -> None:
        self.catalog = catalog or default_profile_catalog()
        self.discovery = discovery or DiscoveryEngine()
        self.renderer = ProjectProfileRenderer(self.catalog)

    @staticmethod
    def provenance_path(project: ProjectDescriptor) -> Path:
        configured = project.configured_path("provenance_file")
        return configured or project.directory / ".execraft-template.yaml"

    def adopt(
        self,
        project: ProjectDescriptor,
        *,
        profile_reference: str = "",
        feature_references: Sequence[str] = (),
    ) -> TemplateProvenance:
        path = self.provenance_path(project)
        if path.exists():
            raise ProjectUpgradeError(f"project already has template provenance: {path}")
        profile = self.catalog.profile(profile_reference or project.profile or "standard@1")
        features = tuple(
            self.catalog.feature(reference).reference
            for reference in (feature_references or project.features)
        )
        managed: dict[str, str] = {}
        for candidate in sorted(project.directory.rglob("*")):
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(project.directory)
            if relative.parts and relative.parts[0] == "tasks":
                continue
            if relative.as_posix() == path.name:
                continue
            managed[relative.as_posix()] = sha256_file(candidate)
        payload = {
            "schema_version": 1,
            "profile": profile.reference,
            "features": list(features),
            "generated_with": "adopted by execraft",
            "managed_files": managed,
        }
        atomic_write_yaml(path, payload, sort_keys=False, width=1000)
        return TemplateProvenance.load(path)

    def plan(
        self,
        project: ProjectDescriptor,
        *,
        source_root: Path | None = None,
        target_profile: str = "",
        add_features: Sequence[str] = (),
        remove_features: Sequence[str] = (),
        include_devcontainer: bool | None = None,
    ) -> UpgradePlan:
        provenance_path = self.provenance_path(project)
        if not provenance_path.is_file():
            requested = target_profile or project.profile or "standard"
            target = self.catalog.profile(requested).reference
            return UpgradePlan(
                project_id=project.id,
                project_dir=project.directory,
                current_profile=project.profile,
                target_profile=target,
                current_features=tuple(project.features),
                target_features=tuple(project.features),
                files=(),
                provenance_missing=True,
            )
        provenance = TemplateProvenance.load(provenance_path)
        current_profile = provenance.profile or project.profile or "standard@1"
        target = self.catalog.profile(target_profile or current_profile.split("@", 1)[0])
        existing_features = [self.catalog.latest_feature_reference(item) for item in provenance.features]
        requested = tuple(existing_features) + tuple(add_features)
        excluded_list = list(remove_features)
        current_has_devcontainer = any(
            item.split("@", 1)[0] == "devcontainer" for item in requested
        )
        use_devcontainer = (
            current_has_devcontainer if include_devcontainer is None else include_devcontainer
        )
        if include_devcontainer is False and "devcontainer" not in {
            item.split("@", 1)[0] for item in excluded_list
        }:
            excluded_list.append("devcontainer")
        excluded = tuple(excluded_list)
        root = resolve_project_source_root(project, source_root)
        report = self.discovery.inspect(root)
        report.project_id = project.id

        temporary = Path(tempfile.mkdtemp(prefix=f"execraft-upgrade-{project.id}-"))
        try:
            target_tree = temporary / project.id
            target_tree.mkdir()
            context = ProjectTemplateContext(
                report=report,
                profile_reference=target.reference,
                requested_features=tuple(requested),
                include_devcontainer=use_devcontainer,
                excluded_features=excluded,
            )
            rendered = self.renderer.render(context, target_tree)
            self._preserve_project_identity(project, target_tree / "project.yaml")
            # Identity preservation changed the descriptor after renderer provenance
            # was calculated; refresh the target manifest before diffing.
            self.renderer._write_provenance(target_tree, rendered.profile, rendered.features)
            target_provenance = TemplateProvenance.load(target_tree / ".execraft-template.yaml")
            files = self._compare(project.directory, provenance, target_tree, target_provenance)
            return UpgradePlan(
                project_id=project.id,
                project_dir=project.directory,
                current_profile=current_profile,
                target_profile=target.reference,
                current_features=provenance.features,
                target_features=target_provenance.features,
                files=files,
                target_tree=target_tree,
            )
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    @staticmethod
    def _preserve_project_identity(project: ProjectDescriptor, target_path: Path) -> None:
        current_path = project.directory / "project.yaml"
        current = yaml.safe_load(current_path.read_text(encoding="utf-8")) or {}
        target = yaml.safe_load(target_path.read_text(encoding="utf-8")) or {}
        for key in ("project", "description", "repositories"):
            if key in current:
                target[key] = current[key]
        if "default_policy" in current:
            target["default_policy"] = current["default_policy"]
        current_editor = current.get("editor") or {}
        target_editor = target.get("editor") or {}
        if isinstance(current_editor, Mapping) and isinstance(target_editor, Mapping):
            merged = dict(target_editor)
            merged["extensions"] = sorted(
                set(target_editor.get("extensions") or []) | set(current_editor.get("extensions") or [])
            )
            merged["exclude"] = sorted(
                set(target_editor.get("exclude") or []) | set(current_editor.get("exclude") or [])
            )
            settings = dict(target_editor.get("settings") or {})
            settings.update(current_editor.get("settings") or {})
            merged["settings"] = settings
            target["editor"] = merged
        target_path.write_text(yaml.safe_dump(target, sort_keys=False, width=1000), encoding="utf-8")

    @staticmethod
    def _compare(
        current_root: Path,
        baseline: TemplateProvenance,
        target_root: Path,
        target: TemplateProvenance,
    ) -> tuple[UpgradeFile, ...]:
        rows: list[UpgradeFile] = []
        for name in sorted(set(baseline.managed_files) | set(target.managed_files)):
            relative = _safe_relative(name)
            current_path = current_root / relative
            target_path = target_root / relative
            baseline_hash = baseline.managed_files.get(name, "")
            target_hash = target.managed_files.get(name, "")
            current_hash = sha256_file(current_path) if current_path.is_file() else ""
            if target_hash and current_hash == target_hash:
                action, reason = "unchanged", ""
            elif target_hash and not current_hash:
                if baseline_hash:
                    action, reason = "conflict", "managed file was deleted locally"
                else:
                    action, reason = "add", "new profile-managed file"
            elif not target_hash and current_hash:
                if current_hash == baseline_hash:
                    action, reason = "delete", "removed by target profile"
                else:
                    action, reason = "conflict", "locally modified file removed by target profile"
            elif target_hash and current_hash == baseline_hash:
                action, reason = "update", "profile-managed file changed"
            elif target_hash and not baseline_hash:
                action, reason = "conflict", "target would replace an unmanaged existing file"
            else:
                action, reason = "conflict", "locally modified managed file"
            rows.append(
                UpgradeFile(name, action, current_hash, baseline_hash, target_hash, reason)
            )
        return tuple(rows)

    @staticmethod
    def cleanup(plan: UpgradePlan) -> None:
        """Release a staged dry-run tree without touching the project."""

        if plan.target_tree is not None:
            shutil.rmtree(plan.target_tree.parent, ignore_errors=True)

    def apply(self, plan: UpgradePlan, *, force_conflicts: bool = False) -> UpgradePlan:
        if plan.provenance_missing:
            raise ProjectUpgradeError("cannot upgrade without template provenance")
        if plan.target_tree is None or not plan.target_tree.is_dir():
            raise ProjectUpgradeError("upgrade plan no longer has a staged target tree")
        if plan.conflicts and not force_conflicts:
            raise ProjectUpgradeError(
                "upgrade has locally modified managed files; review the dry-run or pass --force-managed"
            )
        backup_root = Path(tempfile.mkdtemp(prefix=f"execraft-upgrade-backup-{plan.project_id}-"))
        touched = [item for item in plan.files if item.action != "unchanged"]
        with _creation_lock(plan.project_dir):
            try:
                for item in touched:
                    relative = _safe_relative(item.path)
                    current = plan.project_dir / relative
                    if current.is_file():
                        backup = backup_root / relative
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(current, backup)
                for item in touched:
                    relative = _safe_relative(item.path)
                    destination = plan.project_dir / relative
                    source = plan.target_tree / relative
                    if item.action == "delete" or (
                        item.action == "conflict" and not item.target_sha256
                    ):
                        destination.unlink(missing_ok=True)
                        continue
                    if item.action == "conflict" and not force_conflicts:
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    temporary = destination.with_suffix(destination.suffix + ".execraft-upgrade")
                    shutil.copy2(source, temporary)
                    os.replace(temporary, destination)
                # Provenance is not part of managed_files and is committed last.
                shutil.copy2(
                    plan.target_tree / ".execraft-template.yaml",
                    plan.project_dir / ".execraft-template.yaml.tmp",
                )
                os.replace(
                    plan.project_dir / ".execraft-template.yaml.tmp",
                    plan.project_dir / ".execraft-template.yaml",
                )
            except Exception:
                for item in reversed(touched):
                    relative = _safe_relative(item.path)
                    destination = plan.project_dir / relative
                    backup = backup_root / relative
                    if backup.is_file():
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(backup, destination)
                    elif destination.exists():
                        destination.unlink()
                raise
            finally:
                shutil.rmtree(backup_root, ignore_errors=True)
                shutil.rmtree(plan.target_tree.parent, ignore_errors=True)
        return plan


__all__ = [
    "ProjectUpgradeError",
    "ProjectUpgradeService",
    "TemplateProvenance",
    "UpgradeFile",
    "UpgradePlan",
]
