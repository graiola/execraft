"""Package-scoped context capsules backed by the durable task dossier.

Capsules preserve requirements and relevant evidence while keeping append-only
engineering history out of ordinary prompts.  They are generated into runtime
state (not the versioned dossier), so orchestration never dirties a workspace by
refreshing context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

import yaml

from execraft.persistence import FileLock, LockLevel, atomic_write_json

from .invocations import AgentInvocationStore
from .models import WorkPackage


_CAPSULE_SCHEMA_VERSION = 1
_GENERIC_PLAN_REQUIREMENT = re.compile(
    r"\b(?:implement|complete|follow)\b.*\b(?:plan\.md|documented plan|plan section)\b",
    re.IGNORECASE,
)


class ContextCapsuleError(RuntimeError):
    """Raised when package context cannot be generated safely."""


def _stable_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(path: Path) -> str:
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _compact_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(value or {})
    result: dict[str, Any] = {}
    for key in (
        "status",
        "verdict",
        "summary",
        "findings",
        "observations",
        "resolved_findings",
        "acceptance_evidence",
        "agent_id",
        "invocation_id",
        "captured_at",
        "commands",
    ):
        item = raw.get(key)
        if item not in (None, "", [], {}):
            result[key] = item
    return result


def _safe_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value).strip()).strip(".-")
    if not normalized or normalized in {".", ".."}:
        raise ContextCapsuleError(f"invalid context capsule identifier: {value!r}")
    return normalized[:160]


@dataclass(frozen=True)
class PackageContextCapsule:
    package_id: str
    title: str
    objective: str
    requirements: tuple[str, ...]
    acceptance_criteria: tuple[dict[str, Any], ...]
    affected_repositories: tuple[str, ...]
    dependencies: tuple[str, ...]
    read_scope: tuple[str, ...]
    write_scope: tuple[str, ...]
    conflict_keys: tuple[str, ...]
    risk: str
    verification_profile: str
    plan_section: str = ""
    decisions: tuple[dict[str, Any], ...] = ()
    latest_evidence: Mapping[str, Any] = field(default_factory=dict)
    references: tuple[dict[str, str], ...] = ()
    source_fingerprints: Mapping[str, str] = field(default_factory=dict)
    legacy_fallback: bool = False
    schema_version: int = _CAPSULE_SCHEMA_VERSION
    capsule_sha256: str = ""

    def as_mapping(self, *, include_digest: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "package_id": self.package_id,
            "title": self.title,
            "objective": self.objective,
            "requirements": list(self.requirements),
            "acceptance_criteria": [dict(item) for item in self.acceptance_criteria],
            "affected_repositories": list(self.affected_repositories),
            "dependencies": list(self.dependencies),
            "read_scope": list(self.read_scope),
            "write_scope": list(self.write_scope),
            "conflict_keys": list(self.conflict_keys),
            "risk": self.risk,
            "verification_profile": self.verification_profile,
            "plan_section": self.plan_section,
            "decisions": [dict(item) for item in self.decisions],
            "latest_evidence": dict(self.latest_evidence),
            "references": [dict(item) for item in self.references],
            "source_fingerprints": dict(self.source_fingerprints),
            "legacy_fallback": self.legacy_fallback,
        }
        if include_digest:
            result["capsule_sha256"] = self.capsule_sha256 or hashlib.sha256(
                _stable_json(result).encode("utf-8")
            ).hexdigest()
        return result

    def with_digest(self) -> "PackageContextCapsule":
        digest = hashlib.sha256(
            _stable_json(self.as_mapping(include_digest=False)).encode("utf-8")
        ).hexdigest()
        return PackageContextCapsule(
            **{
                **self.__dict__,
                "capsule_sha256": digest,
            }
        )


class PackageContextCapsuleStore:
    """Generate and atomically persist package/shard context capsules."""

    def __init__(
        self,
        *,
        dossier_dir: Path | None,
        output_dir: Path,
        invocations: AgentInvocationStore,
        project_id: str,
    ) -> None:
        self.dossier_dir = Path(dossier_dir).resolve() if dossier_dir else None
        self.output_dir = Path(output_dir).resolve()
        self.invocations = invocations
        self.project_id = str(project_id)

    def path_for(self, package: WorkPackage) -> Path:
        suffix = f"--{_safe_component(package.shard_key)}" if package.shard_key else ""
        return self.output_dir / f"{_safe_component(package.id)}{suffix}.json"

    def generate(self, package: WorkPackage, *, force: bool = False) -> tuple[PackageContextCapsule, Path]:
        candidate = self._build(package).with_digest()
        path = self.path_for(package)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._file_lock(path):
            if not force and path.is_file():
                existing = self._load_path(path)
                if (
                    existing.capsule_sha256 == candidate.capsule_sha256
                    and existing.source_fingerprints == candidate.source_fingerprints
                ):
                    return existing, path
            self._atomic_write(path, candidate.as_mapping())
        return candidate, path

    def load(self, package: WorkPackage) -> PackageContextCapsule:
        path = self.path_for(package)
        if not path.is_file():
            return self.generate(package)[0]
        return self._load_path(path)

    def validate(self, path: Path) -> PackageContextCapsule:
        return self._load_path(Path(path).resolve())

    def _build(self, package: WorkPackage) -> PackageContextCapsule:
        plan_section = ""
        objective = package.title
        decisions: list[dict[str, Any]] = []
        fingerprints: dict[str, str] = {}
        references: list[dict[str, str]] = []

        if self.dossier_dir is not None:
            plan_path = self.dossier_dir / "PLAN.md"
            graph_path = self.dossier_dir / "PLAN.graph.yaml"
            brief_path = self.dossier_dir / "BRIEF.md"
            decisions_path = self.dossier_dir / "DECISIONS.yaml"
            for name, path in (
                ("PLAN.md", plan_path),
                ("PLAN.graph.yaml", graph_path),
                ("BRIEF.md", brief_path),
                ("DECISIONS.yaml", decisions_path),
            ):
                if path.is_file():
                    fingerprints[name] = _fingerprint(path)
                    references.append({"kind": name, "path": str(path)})
            plan_section = self._extract_plan_section(plan_path, package.id)
            objective = self._extract_objective(plan_section) or package.title
            decisions = self._load_decisions(decisions_path, package)

        requirements = tuple(str(item).strip() for item in package.requirements if str(item).strip())
        legacy_fallback = bool(plan_section) and (
            not requirements or any(_GENERIC_PLAN_REQUIREMENT.search(item) for item in requirements)
        )
        # Legacy/test plans may intentionally defer detailed requirements to a
        # later decomposition stage.  Keep the capsule usable and mark it as a
        # legacy fallback instead of failing ordinary context assembly.  Strict
        # graph validation can still reject underspecified packages before
        # execution when that policy is enabled.
        legacy_fallback = legacy_fallback or (not requirements and not plan_section)

        evidence = {
            "implementation": _compact_mapping(package.last_implementation),
            "verification": _compact_mapping(package.last_verification),
            "review": _compact_mapping(package.last_review),
            "unresolved_findings": list(package.review_findings),
        }
        return PackageContextCapsule(
            package_id=package.id,
            title=package.title,
            objective=objective,
            requirements=requirements,
            acceptance_criteria=tuple(
                {
                    "id": item.id,
                    "description": item.description,
                    "verified": item.verified,
                    "evidence": item.evidence,
                }
                for item in package.acceptance_criteria
            ),
            affected_repositories=tuple(package.affected_repositories),
            dependencies=tuple(package.dependencies),
            read_scope=tuple(package.read_scope),
            write_scope=tuple(package.write_scope),
            conflict_keys=tuple(package.conflict_keys),
            risk=package.risk,
            verification_profile=package.verification_profile,
            plan_section=plan_section,
            decisions=tuple(decisions),
            latest_evidence=evidence,
            references=tuple(references),
            source_fingerprints=fingerprints,
            legacy_fallback=legacy_fallback,
        )


    @staticmethod
    def _extract_plan_section(path: Path, package_id: str) -> str:
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        package_token = re.compile(
            rf"(?:^|[\s—–:()\[\]/-]){re.escape(package_id)}(?:$|[\s—–:()\[\]/-])",
            re.IGNORECASE,
        )
        start = -1
        level = 0
        for index, line in enumerate(lines):
            match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if match and package_token.search(match.group(2)):
                start = index
                level = len(match.group(1))
                break
        if start < 0:
            return ""
        end = len(lines)
        for index in range(start + 1, len(lines)):
            match = re.match(r"^(#{1,6})\s+", lines[index])
            if match and len(match.group(1)) <= level:
                end = index
                break
        return "\n".join(lines[start:end]).strip()

    @staticmethod
    def _extract_objective(section: str) -> str:
        if not section:
            return ""
        lines = section.splitlines()
        for index, line in enumerate(lines):
            if re.match(r"^#{2,6}\s+Objective\s*$", line, re.IGNORECASE):
                paragraphs: list[str] = []
                for candidate in lines[index + 1 :]:
                    if candidate.startswith("#"):
                        break
                    if candidate.strip():
                        paragraphs.append(candidate.strip())
                    elif paragraphs:
                        break
                return " ".join(paragraphs)
        for candidate in lines[1:]:
            if candidate.strip() and not candidate.lstrip().startswith(("#", "---")):
                return candidate.strip()
        return ""

    @staticmethod
    def _load_decisions(path: Path, package: WorkPackage) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ContextCapsuleError(f"invalid decision index {path}: {exc}") from exc
        entries = raw.get("decisions", raw if isinstance(raw, list) else [])
        if not isinstance(entries, list):
            raise ContextCapsuleError(f"decision index must contain a decisions list: {path}")
        result: list[dict[str, Any]] = []
        for item in entries:
            if not isinstance(item, Mapping):
                continue
            packages = [str(value) for value in (item.get("packages") or [])]
            scopes = [str(value) for value in (item.get("scope") or item.get("scopes") or [])]
            applies = (
                not packages and not scopes
                or package.id in packages
                or package.parent_id in packages
                or any(repository in scopes for repository in package.affected_repositories)
            )
            if not applies or str(item.get("status", "active")) in {"superseded", "rejected"}:
                continue
            result.append(
                {
                    key: item[key]
                    for key in (
                        "id",
                        "title",
                        "summary",
                        "decision",
                        "status",
                        "source",
                        "supersedes",
                    )
                    if key in item
                }
            )
        return sorted(result, key=lambda item: str(item.get("id", "")))

    @staticmethod
    def _load_path(path: Path) -> PackageContextCapsule:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContextCapsuleError(f"invalid context capsule {path}: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise ContextCapsuleError(f"context capsule must be a mapping: {path}")
        if int(raw.get("schema_version", 0)) != _CAPSULE_SCHEMA_VERSION:
            raise ContextCapsuleError(
                f"unsupported context capsule schema in {path}: "
                f"{raw.get('schema_version')!r}"
            )
        capsule = PackageContextCapsule(
            package_id=str(raw.get("package_id", "")),
            title=str(raw.get("title", "")),
            objective=str(raw.get("objective", "")),
            requirements=tuple(str(item) for item in (raw.get("requirements") or [])),
            acceptance_criteria=tuple(
                dict(item) for item in (raw.get("acceptance_criteria") or []) if isinstance(item, Mapping)
            ),
            affected_repositories=tuple(
                str(item) for item in (raw.get("affected_repositories") or [])
            ),
            dependencies=tuple(str(item) for item in (raw.get("dependencies") or [])),
            read_scope=tuple(str(item) for item in (raw.get("read_scope") or [])),
            write_scope=tuple(str(item) for item in (raw.get("write_scope") or [])),
            conflict_keys=tuple(str(item) for item in (raw.get("conflict_keys") or [])),
            risk=str(raw.get("risk", "medium")),
            verification_profile=str(raw.get("verification_profile", "targeted")),
            plan_section=str(raw.get("plan_section", "")),
            decisions=tuple(
                dict(item) for item in (raw.get("decisions") or []) if isinstance(item, Mapping)
            ),
            latest_evidence=dict(raw.get("latest_evidence") or {}),
            references=tuple(
                dict(item) for item in (raw.get("references") or []) if isinstance(item, Mapping)
            ),
            source_fingerprints=dict(raw.get("source_fingerprints") or {}),
            legacy_fallback=bool(raw.get("legacy_fallback", False)),
            schema_version=int(raw.get("schema_version", _CAPSULE_SCHEMA_VERSION)),
            capsule_sha256=str(raw.get("capsule_sha256", "")),
        )
        expected = capsule.with_digest().capsule_sha256
        if capsule.capsule_sha256 and capsule.capsule_sha256 != expected:
            raise ContextCapsuleError(f"context capsule digest mismatch: {path}")
        if not capsule.package_id or not capsule.title:
            raise ContextCapsuleError(f"context capsule lacks package identity: {path}")
        return capsule if capsule.capsule_sha256 else capsule.with_digest()

    @staticmethod
    def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
        atomic_write_json(path, dict(value), indent=2, ensure_ascii=False, trailing_newline=True)

    @staticmethod
    def _file_lock(path: Path) -> FileLock:
        return FileLock(
            path.with_suffix(path.suffix + ".lock"),
            level=LockLevel.RECORD,
        )
