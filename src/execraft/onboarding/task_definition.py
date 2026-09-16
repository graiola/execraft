"""Task-definition import, provenance, and deterministic dossier materialization.

This module deliberately does not invoke an AI provider. It owns the safe,
deterministic part of importing BRIEF/PLAN artifacts. Missing executable graphs
are produced later by :class:`DraftPlanService`, where provider policy belongs.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from execraft.persistence import atomic_write_text, atomic_write_yaml, sha256_file
from execraft.onboarding.plan_validation import (
    PlanGraphValidationError,
    validate_plan_graph_mapping,
)

_DEFINITION_SCHEMA_VERSION = 1
_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_IMPORT_REVISION_DIRECTORY = Path("imports") / "revision-0001"
_REVISION_DIRECTORY = Path("revisions")
_DOCUMENT_NAMES = ("BRIEF.md", "PLAN.md", "PLAN.graph.yaml")


class TaskDefinitionError(ValueError):
    """Raised when an imported task definition is unsafe or inconsistent."""


class TaskDefinitionDriftError(TaskDefinitionError):
    """Raised when live dossier documents differ from the accepted revision."""


@dataclass(frozen=True)
class DefinitionIntegrityReport:
    """Hash-based comparison between the live dossier and accepted metadata."""

    revision: int
    accepted_definition_sha256: str
    actual_definition_sha256: str
    changed_documents: tuple[str, ...] = ()
    missing_documents: tuple[str, ...] = ()
    unexpected_documents: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not (self.changed_documents or self.missing_documents or self.unexpected_documents)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "ok": self.ok,
            "accepted_definition_sha256": self.accepted_definition_sha256,
            "actual_definition_sha256": self.actual_definition_sha256,
            "changed_documents": list(self.changed_documents),
            "missing_documents": list(self.missing_documents),
            "unexpected_documents": list(self.unexpected_documents),
        }


@dataclass(frozen=True)
class TaskDefinitionInput:
    """Optional user-supplied task-contract documents.

    The contents are already text rather than paths so the same value object can
    serve the CLI, GUI uploads, and programmatic callers. Use :meth:`from_paths`
    for filesystem imports; it performs bounded, symlink-safe UTF-8 reads.
    """

    brief_markdown: str = ""
    plan_markdown: str = ""
    plan_graph_yaml: str = ""
    sources: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_paths(
        cls,
        *,
        brief_file: Path | None = None,
        plan_file: Path | None = None,
        plan_graph_file: Path | None = None,
    ) -> "TaskDefinitionInput":
        values: dict[str, str] = {}
        sources: dict[str, str] = {}
        for name, candidate in (
            ("BRIEF.md", brief_file),
            ("PLAN.md", plan_file),
            ("PLAN.graph.yaml", plan_graph_file),
        ):
            if candidate is None:
                continue
            content, source = _read_import_file(candidate, label=name)
            values[name] = content
            sources[name] = source
        return cls(
            brief_markdown=values.get("BRIEF.md", ""),
            plan_markdown=values.get("PLAN.md", ""),
            plan_graph_yaml=values.get("PLAN.graph.yaml", ""),
            sources=sources,
        )

    @classmethod
    def from_contents(
        cls,
        *,
        brief_markdown: str = "",
        plan_markdown: str = "",
        plan_graph_yaml: str = "",
        brief_source: str = "",
        plan_source: str = "",
        plan_graph_source: str = "",
    ) -> "TaskDefinitionInput":
        sources = {
            name: source.strip()
            for name, source in (
                ("BRIEF.md", brief_source),
                ("PLAN.md", plan_source),
                ("PLAN.graph.yaml", plan_graph_source),
            )
            if source.strip()
        }
        return cls(
            brief_markdown=_validate_inline_text(brief_markdown, label="BRIEF.md"),
            plan_markdown=_validate_inline_text(plan_markdown, label="PLAN.md"),
            plan_graph_yaml=_validate_inline_text(
                plan_graph_yaml, label="PLAN.graph.yaml"
            ),
            sources=sources,
        )

    @property
    def supplied(self) -> bool:
        return any((self.brief_markdown, self.plan_markdown, self.plan_graph_yaml))

    @property
    def has_brief(self) -> bool:
        return bool(self.brief_markdown)

    @property
    def has_plan(self) -> bool:
        return bool(self.plan_markdown)

    @property
    def has_plan_graph(self) -> bool:
        return bool(self.plan_graph_yaml)

    def source_fingerprint(self) -> str:
        """Hash the imported source set with stable document-name boundaries."""

        digest = hashlib.sha256()
        for name, content in self.documents().items():
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(content.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest() if self.supplied else ""

    def documents(self) -> dict[str, str]:
        result: dict[str, str] = {}
        if self.brief_markdown:
            result["BRIEF.md"] = self.brief_markdown
        if self.plan_markdown:
            result["PLAN.md"] = self.plan_markdown
        if self.plan_graph_yaml:
            result["PLAN.graph.yaml"] = self.plan_graph_yaml
        return result

    def suggested_title(self) -> str:
        """Derive a bounded task title without requiring a Markdown H1.

        Explicit H1 headings remain authoritative.  Graph/package titles and a
        compact semantic excerpt are deterministic fallbacks so CLI and GUI
        imports can omit ``--title`` even for existing plans that use a
        different heading convention.
        """

        for content in (self.brief_markdown, self.plan_markdown):
            title = _markdown_title(content)
            if title:
                return title
        if self.plan_graph_yaml:
            try:
                raw = yaml.safe_load(self.plan_graph_yaml) or {}
            except yaml.YAMLError:
                raw = {}
            packages = raw.get("work_packages") if isinstance(raw, Mapping) else None
            if isinstance(packages, list):
                for package in packages:
                    if not isinstance(package, Mapping):
                        continue
                    title = str(package.get("title", "")).strip()
                    if title:
                        return title[:120]
        excerpt = _semantic_excerpt(self.brief_markdown or self.plan_markdown, limit=120)
        return excerpt.rstrip(" .,:;-")

    def intent_text(self) -> str:
        """Return bounded semantic text for task identity/repository inference."""

        content = self.brief_markdown or self.plan_markdown
        if not content and self.plan_graph_yaml:
            try:
                raw = yaml.safe_load(self.plan_graph_yaml) or {}
            except yaml.YAMLError:
                raw = {}
            if isinstance(raw, Mapping):
                packages = raw.get("work_packages") or []
                snippets: list[str] = []
                if isinstance(packages, list):
                    for package in packages[:12]:
                        if not isinstance(package, Mapping):
                            continue
                        snippets.extend(
                            str(item)
                            for item in (package.get("requirements") or [])[:8]
                        )
                content = " ".join(snippets)
        return _semantic_excerpt(content)


@dataclass(frozen=True)
class PreparedTaskDefinition:
    """Resolved immutable definition ready to overlay on a task template."""

    title: str
    brief_markdown: str
    plan_markdown: str = ""
    plan_graph_yaml: str = ""
    imported: TaskDefinitionInput = field(default_factory=TaskDefinitionInput)
    request_sha256: str = ""
    created_at: str = ""
    brief_origin: str = "generated_from_intent"
    plan_origin: str = "template"
    graph_origin: str = ""

    @property
    def imported_plan(self) -> bool:
        return self.imported.has_plan

    @property
    def imported_graph(self) -> bool:
        return self.imported.has_plan_graph


class TaskDefinitionService:
    """Validate, stage, and maintain task-definition provenance."""

    def prepare(
        self,
        *,
        definition: TaskDefinitionInput | None,
        title: str,
        fallback_brief: str,
        request_sha256: str,
        created_at: str,
        allowed_repositories: set[str],
    ) -> PreparedTaskDefinition:
        imported = definition or TaskDefinitionInput()
        self._validate_import(imported, allowed_repositories=allowed_repositories)
        resolved_title = title.strip() or imported.suggested_title()
        if not resolved_title:
            raise TaskDefinitionError(
                "task title cannot be empty; supply --title or a Markdown H1 in BRIEF.md/PLAN.md"
            )

        if imported.has_brief:
            brief = imported.brief_markdown
            brief_origin = "imported"
        elif imported.has_plan or imported.has_plan_graph:
            brief = _brief_from_imported_plan(resolved_title)
            brief_origin = "generated_from_imported_plan"
        else:
            normalized = fallback_brief.strip()
            brief = f"# Brief: {resolved_title}\n\n## Intent\n{normalized}\n"
            brief_origin = "generated_from_intent"

        plan = imported.plan_markdown
        plan_origin = "imported" if imported.has_plan else "template"
        graph = imported.plan_graph_yaml
        graph_origin = "imported" if imported.has_plan_graph else ""
        if graph and not plan:
            plan = _plan_from_graph(resolved_title, graph)
            plan_origin = "generated_from_imported_graph"

        return PreparedTaskDefinition(
            title=resolved_title,
            brief_markdown=brief,
            plan_markdown=plan,
            plan_graph_yaml=graph,
            imported=imported,
            request_sha256=request_sha256,
            created_at=created_at,
            brief_origin=brief_origin,
            plan_origin=plan_origin,
            graph_origin=graph_origin,
        )

    def materialize(self, destination: Path, prepared: PreparedTaskDefinition) -> Path:
        """Overlay prepared documents and write revision-1 provenance atomically."""

        if prepared.imported.supplied or not (destination / "BRIEF.md").is_file():
            atomic_write_text(destination / "BRIEF.md", prepared.brief_markdown)
        if prepared.plan_markdown:
            atomic_write_text(destination / "PLAN.md", prepared.plan_markdown)
        if prepared.plan_graph_yaml:
            atomic_write_text(destination / "PLAN.graph.yaml", prepared.plan_graph_yaml)

        imported_paths: dict[str, str] = {}
        for name, content in prepared.imported.documents().items():
            relative = _IMPORT_REVISION_DIRECTORY / name
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(target, content)
            imported_paths[name] = relative.as_posix()

        metadata = self._metadata(
            destination,
            prepared=prepared,
            imported_paths=imported_paths,
        )
        path = destination / "DEFINITION.yaml"
        atomic_write_yaml(path, metadata)
        self.ensure_revision_baseline(destination, adopted_at=prepared.created_at)
        return path

    def refresh_generated_plan(
        self,
        dossier: Path,
        *,
        generated_by: str,
        provider_id: str = "",
        consistency_mode: str = "",
        consistency_summary: str = "",
    ) -> Path:
        """Refresh current hashes after planning without erasing source provenance."""

        path = dossier / "DEFINITION.yaml"
        if not path.is_file():
            return path
        raw = _read_yaml_mapping(path, label="DEFINITION.yaml")
        sources = raw.get("sources")
        if not isinstance(sources, dict):
            sources = {}
            raw["sources"] = sources
        imported_plan = (
            isinstance(sources.get("PLAN.md"), Mapping)
            and str(sources["PLAN.md"].get("origin", "")) == "imported"
        )
        if not imported_plan:
            sources["PLAN.md"] = self._generated_source_entry(
                dossier / "PLAN.md", generated_by=generated_by, provider_id=provider_id
            )
        sources["PLAN.graph.yaml"] = self._generated_source_entry(
            dossier / "PLAN.graph.yaml",
            generated_by=generated_by,
            provider_id=provider_id,
        )
        raw["current"] = _current_hashes(dossier)
        raw["definition_sha256"] = _definition_sha256(dossier)
        raw["package_ids_seen"] = sorted(
            set(str(item) for item in (raw.get("package_ids_seen") or []))
            | set(_plan_package_ids(dossier / "PLAN.graph.yaml"))
        )
        if consistency_mode:
            raw["consistency"] = {
                "mode": consistency_mode,
                "summary": consistency_summary,
            }
        atomic_write_yaml(path, raw)
        self.ensure_revision_baseline(dossier)
        return path

    def live_documents(
        self,
        dossier: Path,
        *,
        require_complete: bool = True,
    ) -> dict[str, str]:
        """Read bounded, regular task-definition documents without symlink traversal."""

        return self._safe_live_documents(
            Path(dossier).expanduser().resolve(),
            require_complete=require_complete,
        )

    def ensure_revision_baseline(
        self,
        dossier: Path,
        *,
        adopted_at: str = "",
        allow_incomplete: bool = False,
    ) -> Path | None:
        """Ensure revision 1 has durable provenance and an immutable snapshot.

        Older tasks may predate ``DEFINITION.yaml``, and
        generated plans do not have a complete three-document dossier until
        planning finishes.  Replanning needs an actual revision-1 snapshot so
        accepting revision 2 never destroys the only copy of the original
        contract.

        A legacy dossier without metadata is adopted only when all three live
        documents are present and safe.  Existing metadata is never rewritten
        merely to hide drift: when revision 1 has changed out of band this
        method returns ``None`` and the normal drift/replan flow remains in
        control.
        """

        dossier = Path(dossier).expanduser().resolve()
        documents = self._safe_live_documents(dossier, require_complete=False)
        if set(documents) != set(_DOCUMENT_NAMES) and not allow_incomplete:
            return None
        if "BRIEF.md" not in documents:
            return None

        metadata = dict(self.load_metadata(dossier))
        if not metadata:
            current = _current_hashes(dossier)
            metadata = {
                "schema_version": _DEFINITION_SCHEMA_VERSION,
                "revision": 1,
                "created_at": adopted_at,
                "request_sha256": "",
                "source_fingerprint": "",
                "sources": {
                    name: {
                        "origin": "legacy_baseline_adopted",
                        "sha256": current[name],
                    }
                    for name in _DOCUMENT_NAMES
                },
                "current": current,
                "definition_sha256": _definition_sha256(dossier),
                "revision_path": (_REVISION_DIRECTORY / "revision-0001").as_posix(),
                "package_ids_seen": _plan_package_ids(dossier / "PLAN.graph.yaml"),
                "migration": {
                    "kind": "legacy_baseline_adoption",
                    "adopted_at": adopted_at,
                },
            }
            atomic_write_yaml(dossier / "DEFINITION.yaml", metadata)

        revision = max(1, int(metadata.get("revision", 1) or 1))
        if revision != 1:
            return self.revision_snapshot_path(dossier, revision)
        expected_revision_path = (_REVISION_DIRECTORY / "revision-0001").as_posix()
        if str(metadata.get("revision_path", "")) != expected_revision_path:
            metadata["revision_path"] = expected_revision_path
            atomic_write_yaml(dossier / "DEFINITION.yaml", metadata)

        target = self.revision_snapshot_path(dossier, 1)
        if target.is_dir():
            self._verify_revision_snapshot(target, metadata)
            return target
        if not self.integrity_report(dossier).ok:
            return None
        if target.exists() or target.is_symlink():
            raise TaskDefinitionError(f"task revision snapshot is unsafe: {target}")

        root = self._revision_root(dossier)
        temporary = Path(
            tempfile.mkdtemp(prefix=".revision-0001.tmp-", dir=str(root))
        )
        try:
            for name in _DOCUMENT_NAMES:
                if name not in documents:
                    continue
                atomic_write_text(temporary / name, documents[name])
            atomic_write_yaml(temporary / "DEFINITION.yaml", metadata)
            inventory = {
                name: sha256_file(temporary / name)
                for name in (*documents, "DEFINITION.yaml")
            }
            atomic_write_yaml(
                temporary / "REVISION.yaml",
                {
                    "schema_version": 1,
                    "revision": 1,
                    "kind": "baseline",
                    "definition_sha256": str(metadata.get("definition_sha256", "")),
                    "documents": inventory,
                },
            )
            os.replace(temporary, target)
        except Exception:
            if temporary.is_dir() and not temporary.is_symlink():
                shutil.rmtree(temporary, ignore_errors=True)
            raise
        return target

    def require_revision_snapshot(
        self,
        dossier: Path,
        revision: int,
        *,
        allow_incomplete: bool = False,
    ) -> Path:
        """Validate that the accepted revision has a trustworthy history record."""

        dossier = Path(dossier).expanduser().resolve()
        if revision <= 0:
            raise TaskDefinitionError(f"invalid task-definition revision: {revision}")
        if revision == 1:
            path = self.ensure_revision_baseline(
                dossier, allow_incomplete=allow_incomplete
            )
            if path is None:
                raise TaskDefinitionError(
                    "accepted revision 1 has no recoverable snapshot; resolve definition drift "
                    "before creating another revision"
                )
        else:
            path = self.revision_snapshot_path(dossier, revision)
        if not path.is_dir() or path.is_symlink():
            raise TaskDefinitionError(
                f"accepted task-definition revision snapshot is missing or unsafe: {path}"
            )
        metadata = self.load_metadata(dossier)
        self._verify_revision_snapshot(path, metadata if revision == 1 else None)
        return path

    @staticmethod
    def revision_snapshot_path(dossier: Path, revision: int) -> Path:
        root = Path(dossier).expanduser().resolve() / _REVISION_DIRECTORY
        return root / f"revision-{int(revision):04d}"

    @staticmethod
    def _revision_root(dossier: Path) -> Path:
        dossier = Path(dossier).expanduser().resolve()
        root = dossier / _REVISION_DIRECTORY
        if root.is_symlink():
            raise TaskDefinitionError(f"task revision directory cannot be a symbolic link: {root}")
        root.mkdir(parents=True, exist_ok=True)
        resolved = root.resolve()
        if resolved.parent != dossier:
            raise TaskDefinitionError(f"task revision directory escapes the dossier: {root}")
        return resolved

    @staticmethod
    def _safe_live_documents(dossier: Path, *, require_complete: bool) -> dict[str, str]:
        documents: dict[str, str] = {}
        for name in _DOCUMENT_NAMES:
            path = dossier / name
            if path.is_symlink():
                raise TaskDefinitionError(f"task-definition document cannot be a symbolic link: {path}")
            if not path.is_file():
                if require_complete:
                    raise TaskDefinitionError(f"task-definition document is missing: {path}")
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise TaskDefinitionError(f"cannot read task-definition document {path}: {exc}") from exc
            _validate_document_text(text, label=name)
            documents[name] = text
        return documents

    @staticmethod
    def _verify_revision_snapshot(
        path: Path,
        expected_definition: Mapping[str, Any] | None = None,
    ) -> None:
        marker = path / "REVISION.yaml"
        # Early revision-2 dossiers predate the baseline marker format inside this
        # implementation; their candidate/APPLIED inventory is validated by
        # ReplanService.  Revision 1 always uses REVISION.yaml.
        if not marker.is_file():
            candidate_path = path / "CANDIDATE.yaml"
            applied_path = path / "APPLIED.yaml"
            if not candidate_path.is_file() or not applied_path.is_file():
                raise TaskDefinitionError(f"task revision snapshot metadata is missing: {marker}")
            candidate = _read_yaml_mapping(candidate_path, label="CANDIDATE.yaml")
            documents = candidate.get("documents") or {}
            required_documents = set(_DOCUMENT_NAMES)
            if not isinstance(documents, Mapping) or set(map(str, documents)) != required_documents:
                raise TaskDefinitionError(
                    f"task revision candidate inventory is invalid: {candidate_path}"
                )
            for name in sorted(required_documents):
                artifact = path / name
                if not artifact.is_file() or artifact.is_symlink():
                    raise TaskDefinitionError(
                        f"task revision snapshot artifact is unsafe: {artifact}"
                    )
                if sha256_file(artifact) != str(documents.get(name, "")):
                    raise TaskDefinitionError(
                        f"task revision snapshot artifact changed: {artifact}"
                    )
            candidate_revision = int(candidate.get("revision", 0) or 0)
            expected_revision = int(path.name.rsplit("-", 1)[-1])
            if candidate_revision != expected_revision:
                raise TaskDefinitionError(
                    f"task revision candidate number mismatch: {candidate_path}"
                )
            applied = _read_yaml_mapping(applied_path, label="APPLIED.yaml")
            accepted_metadata_sha = str(
                applied.get("accepted_definition_metadata_sha256", "")
            )
            if accepted_metadata_sha:
                accepted_metadata = path / "DEFINITION.accepted.yaml"
                if not accepted_metadata.is_file() or accepted_metadata.is_symlink():
                    raise TaskDefinitionError(
                        f"accepted revision metadata is missing or unsafe: {accepted_metadata}"
                    )
                if sha256_file(accepted_metadata) != accepted_metadata_sha:
                    raise TaskDefinitionError(
                        f"accepted revision metadata changed: {accepted_metadata}"
                    )
            transaction = applied.get("transaction") or {}
            accepted_sha = (
                str(transaction.get("definition_sha256_after", ""))
                if isinstance(transaction, Mapping)
                else ""
            )
            if accepted_sha and _definition_sha256(path) != accepted_sha:
                raise TaskDefinitionError(
                    f"task revision snapshot definition hash mismatch: {applied_path}"
                )
            return
        raw = _read_yaml_mapping(marker, label="REVISION.yaml")
        if int(raw.get("revision", 0)) != int(path.name.rsplit("-", 1)[-1]):
            raise TaskDefinitionError(f"task revision snapshot number mismatch: {marker}")
        inventory = raw.get("documents") or {}
        allowed = {*_DOCUMENT_NAMES, "DEFINITION.yaml"}
        required = {"BRIEF.md", "DEFINITION.yaml"}
        recorded_files = set(map(str, inventory)) if isinstance(inventory, Mapping) else set()
        if not isinstance(inventory, Mapping) or not required <= recorded_files <= allowed:
            raise TaskDefinitionError(f"task revision snapshot inventory is invalid: {marker}")
        for name in sorted(recorded_files):
            artifact = path / name
            if not artifact.is_file() or artifact.is_symlink():
                raise TaskDefinitionError(f"task revision snapshot artifact is unsafe: {artifact}")
            if sha256_file(artifact) != str(inventory.get(name, "")):
                raise TaskDefinitionError(f"task revision snapshot artifact changed: {artifact}")
        recorded = str(raw.get("definition_sha256", ""))
        if recorded and _definition_sha256(path) != recorded:
            raise TaskDefinitionError(
                f"task revision snapshot definition content mismatch: {marker}"
            )
        if expected_definition is not None:
            expected = str(expected_definition.get("definition_sha256", ""))
            if recorded != expected:
                raise TaskDefinitionError(
                    f"task revision snapshot definition hash mismatch: {marker}"
                )

    @staticmethod
    def load_metadata(dossier: Path) -> Mapping[str, Any]:
        path = dossier / "DEFINITION.yaml"
        return _read_yaml_mapping(path, label="DEFINITION.yaml") if path.is_file() else {}

    @staticmethod
    def imported_plan(dossier: Path) -> bool:
        raw = TaskDefinitionService.load_metadata(dossier)
        sources = raw.get("sources") if isinstance(raw, Mapping) else None
        item = sources.get("PLAN.md") if isinstance(sources, Mapping) else None
        return isinstance(item, Mapping) and str(item.get("origin", "")) == "imported"

    @staticmethod
    def request_sha256(dossier: Path) -> str:
        raw = TaskDefinitionService.load_metadata(dossier)
        return str(raw.get("request_sha256", "")) if isinstance(raw, Mapping) else ""

    @staticmethod
    def current_hashes(dossier: Path) -> dict[str, str]:
        return _current_hashes(Path(dossier).resolve())

    @staticmethod
    def definition_sha256(dossier: Path) -> str:
        return _definition_sha256(Path(dossier).resolve())

    def integrity_report(self, dossier: Path) -> DefinitionIntegrityReport:
        """Return drift against the accepted DEFINITION.yaml revision.

        Legacy dossiers without provenance are intentionally treated as valid;
        they cannot participate in revisioned replanning until definition metadata is
        created, but existing orchestration remains backward compatible.
        """

        dossier = Path(dossier).resolve()
        self._safe_live_documents(dossier, require_complete=False)
        metadata = self.load_metadata(dossier)
        if not metadata:
            digest = _definition_sha256(dossier)
            return DefinitionIntegrityReport(
                revision=0,
                accepted_definition_sha256=digest,
                actual_definition_sha256=digest,
            )
        accepted = metadata.get("current") or {}
        if not isinstance(accepted, Mapping):
            raise TaskDefinitionError("DEFINITION.yaml current hashes must be a mapping")
        expected = {str(name): str(value) for name, value in accepted.items()}
        accepted_definition_sha = str(metadata.get("definition_sha256", ""))
        calculated_definition_sha = _definition_sha256_from_hashes(expected)
        if accepted_definition_sha != calculated_definition_sha:
            raise TaskDefinitionError(
                "DEFINITION.yaml definition_sha256 does not match its accepted document hashes"
            )
        sources = metadata.get("sources") or {}
        if sources and not isinstance(sources, Mapping):
            raise TaskDefinitionError("DEFINITION.yaml sources must be a mapping")
        if isinstance(sources, Mapping):
            for name, expected_sha in expected.items():
                source = sources.get(name)
                if not isinstance(source, Mapping):
                    continue
                source_sha = str(source.get("sha256", ""))
                if source_sha and source_sha != expected_sha:
                    raise TaskDefinitionError(
                        f"DEFINITION.yaml source hash for {name} does not match current metadata"
                    )
        actual = _current_hashes(dossier)
        missing = tuple(sorted(set(expected) - set(actual)))
        unexpected = tuple(sorted(set(actual) - set(expected)))
        changed = tuple(
            sorted(
                name
                for name in set(expected) & set(actual)
                if expected[name] != actual[name]
            )
        )
        return DefinitionIntegrityReport(
            revision=max(1, int(metadata.get("revision", 1))),
            accepted_definition_sha256=accepted_definition_sha,
            actual_definition_sha256=_definition_sha256(dossier),
            changed_documents=changed,
            missing_documents=missing,
            unexpected_documents=unexpected,
        )

    def require_integrity(self, dossier: Path) -> DefinitionIntegrityReport:
        report = self.integrity_report(dossier)
        if report.ok:
            return report
        details: list[str] = []
        if report.changed_documents:
            details.append("changed=" + ",".join(report.changed_documents))
        if report.missing_documents:
            details.append("missing=" + ",".join(report.missing_documents))
        if report.unexpected_documents:
            details.append("unexpected=" + ",".join(report.unexpected_documents))
        raise TaskDefinitionDriftError(
            "TASK_DEFINITION_DRIFT: BRIEF.md, PLAN.md, or PLAN.graph.yaml changed "
            "outside an accepted replanning transaction (" + "; ".join(details) + "). "
            "Run 'execraft task replan <task-id> --from-current-files' to stage the edits."
        )

    def _validate_import(
        self,
        imported: TaskDefinitionInput,
        *,
        allowed_repositories: set[str],
    ) -> None:
        for name, content in imported.documents().items():
            _validate_document_text(content, label=name)
        if imported.plan_graph_yaml:
            try:
                raw = yaml.safe_load(imported.plan_graph_yaml) or {}
            except yaml.YAMLError as exc:
                raise TaskDefinitionError(f"PLAN.graph.yaml is invalid YAML: {exc}") from exc
            if not isinstance(raw, Mapping):
                raise TaskDefinitionError("PLAN.graph.yaml must contain a mapping")
            try:
                validate_plan_graph_mapping(
                    raw,
                    allowed_repositories=allowed_repositories,
                )
            except PlanGraphValidationError as exc:
                raise TaskDefinitionError(f"invalid imported PLAN.graph.yaml: {exc}") from exc

    @staticmethod
    def _generated_source_entry(
        path: Path,
        *,
        generated_by: str,
        provider_id: str,
    ) -> dict[str, str]:
        return {
            "origin": "generated",
            "generated_by": generated_by,
            "provider_id": provider_id,
            "sha256": sha256_file(path),
        }

    def _metadata(
        self,
        destination: Path,
        *,
        prepared: PreparedTaskDefinition,
        imported_paths: Mapping[str, str],
    ) -> dict[str, Any]:
        sources: dict[str, dict[str, str]] = {}
        origin_by_name = {
            "BRIEF.md": prepared.brief_origin,
            "PLAN.md": prepared.plan_origin,
            "PLAN.graph.yaml": prepared.graph_origin,
        }
        for name in _DOCUMENT_NAMES:
            path = destination / name
            if not path.is_file():
                continue
            origin = origin_by_name[name] or "template"
            entry: dict[str, str] = {
                "origin": origin,
                "sha256": sha256_file(path),
            }
            imported_source = prepared.imported.sources.get(name, "")
            if imported_source:
                entry["source"] = imported_source
            if name in imported_paths:
                entry["snapshot"] = imported_paths[name]
            sources[name] = entry
        metadata: dict[str, Any] = {
            "schema_version": _DEFINITION_SCHEMA_VERSION,
            "revision": 1,
            "created_at": prepared.created_at,
            "request_sha256": prepared.request_sha256,
            "source_fingerprint": prepared.imported.source_fingerprint(),
            "sources": sources,
            "current": _current_hashes(destination),
            "definition_sha256": _definition_sha256(destination),
            "revision_path": (_REVISION_DIRECTORY / "revision-0001").as_posix(),
            "package_ids_seen": _plan_package_ids(destination / "PLAN.graph.yaml"),
        }
        if prepared.imported_graph:
            metadata["consistency"] = {
                "mode": "structural",
                "summary": (
                    "Imported PLAN.graph.yaml passed executable graph and repository-scope "
                    "validation. Semantic BRIEF/PLAN coherence was not delegated because the "
                    "caller supplied the executable graph."
                ),
            }
        return metadata


def _read_import_file(path: Path, *, label: str) -> tuple[str, str]:
    raw_path = path.expanduser()
    if raw_path.is_symlink():
        raise TaskDefinitionError(f"{label} import cannot be a symbolic link: {raw_path}")
    try:
        resolved = raw_path.resolve(strict=True)
    except OSError as exc:
        raise TaskDefinitionError(f"cannot resolve {label} import {raw_path}: {exc}") from exc
    if not resolved.is_file():
        raise TaskDefinitionError(f"{label} import is not a regular file: {resolved}")
    stat = resolved.stat()
    if stat.st_size > _MAX_DOCUMENT_BYTES:
        raise TaskDefinitionError(
            f"{label} import exceeds {_MAX_DOCUMENT_BYTES} bytes: {resolved}"
        )
    try:
        data = resolved.read_bytes()
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskDefinitionError(f"{label} import must be UTF-8: {resolved}") from exc
    normalized = _normalize_newlines(text)
    _validate_document_text(normalized, label=label)
    return normalized, str(resolved)


def _validate_inline_text(text: str, *, label: str) -> str:
    if not text:
        return ""
    normalized = _normalize_newlines(text)
    _validate_document_text(normalized, label=label)
    return normalized


def _validate_document_text(text: str, *, label: str) -> None:
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_DOCUMENT_BYTES:
        raise TaskDefinitionError(f"{label} exceeds {_MAX_DOCUMENT_BYTES} bytes")
    if not text.strip():
        raise TaskDefinitionError(f"supplied {label} cannot be empty")
    if "\x00" in text:
        raise TaskDefinitionError(f"{label} cannot contain NUL bytes")


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _markdown_title(markdown: str) -> str:
    for line in markdown.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if not match:
            continue
        title = match.group(1).strip()
        title = re.sub(r"^(?:brief|plan)\s*:\s*", "", title, flags=re.IGNORECASE)
        return title[:120].strip()
    return ""


def _semantic_excerpt(markdown: str, *, limit: int = 4000) -> str:
    if not markdown:
        return ""
    lines = markdown.splitlines()
    if lines and re.match(r"^#\s+", lines[0]):
        without_title = "\n".join(lines[1:]).strip()
        if without_title:
            markdown = without_title
    text = re.sub(r"```.*?```", " ", markdown, flags=re.DOTALL)
    text = re.sub(r"[#>*_`\[\]()]+", " ", text)
    text = " ".join(text.split())
    return text[:limit].rstrip()


def _brief_from_imported_plan(title: str) -> str:
    return (
        f"# Brief: {title}\n\n"
        "## Intent\n\n"
        "This task was created from an imported implementation plan. "
        "The imported `PLAN.md` is the authoritative human-readable scope; "
        "the executable `PLAN.graph.yaml` is validated or generated from it.\n"
    )


def _plan_from_graph(title: str, graph_yaml: str) -> str:
    raw = yaml.safe_load(graph_yaml) or {}
    packages = raw.get("work_packages") if isinstance(raw, Mapping) else []
    lines = [
        f"# Plan: {title}",
        "",
        "This human-readable plan was generated from an imported `PLAN.graph.yaml`.",
        "",
        "## Work packages",
        "",
    ]
    if isinstance(packages, list):
        for package in packages:
            if not isinstance(package, Mapping):
                continue
            package_id = str(package.get("id", "")).strip()
            package_title = str(package.get("title", "")).strip()
            lines.append(f"### {package_id} — {package_title}".rstrip(" —"))
            lines.append("")
            for requirement in package.get("requirements") or []:
                lines.append(f"- {requirement}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _plan_package_ids(path: Path) -> list[str]:
    if not path.is_file() or path.is_symlink():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return []
    if not isinstance(raw, Mapping):
        return []
    packages = raw.get("work_packages") or raw.get("packages") or []
    if not isinstance(packages, list):
        return []
    return sorted(
        {
            str(item.get("id", "")).strip()
            for item in packages
            if isinstance(item, Mapping) and str(item.get("id", "")).strip()
        }
    )


def _current_hashes(dossier: Path) -> dict[str, str]:
    return {
        name: sha256_file(dossier / name)
        for name in _DOCUMENT_NAMES
        if (dossier / name).is_file()
    }


def _definition_sha256(dossier: Path) -> str:
    return _definition_sha256_from_hashes(_current_hashes(dossier))


def _definition_sha256_from_hashes(hashes: Mapping[str, str]) -> str:
    digest = hashlib.sha256()
    for name, value in sorted(hashes.items()):
        digest.update(str(name).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(value).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()



def _read_yaml_mapping(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink():
        raise TaskDefinitionError(f"{label} cannot be a symbolic link: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise TaskDefinitionError(f"cannot read {label}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise TaskDefinitionError(f"{label} must contain a mapping")
    return dict(raw)



__all__ = [
    "DefinitionIntegrityReport",
    "PreparedTaskDefinition",
    "TaskDefinitionDriftError",
    "TaskDefinitionError",
    "TaskDefinitionInput",
    "TaskDefinitionService",
]
