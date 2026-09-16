"""Parse BRIEF/PLAN documents into a normalized work-package dependency graph."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from .execution_policy import ExecutionPolicyError, normalize_role_mapping
from .models import (
    AcceptanceCriterion,
    OrchestrateError,
    PlanGraph,
    WorkPackage,
    WorkPackageKind,
    WorkPackageStage,
)
from execraft.repository_sync.spec import RepositorySyncSpec


def _parse_role_preferences(
    raw: object, *, label: str
) -> dict[str, list[str]]:
    try:
        return normalize_role_mapping(raw, label=label)  # type: ignore[arg-type]
    except ExecutionPolicyError as exc:
        raise OrchestrateError(str(exc)) from exc


@dataclass
class NormalizationReport:
    packages_found: int = 0
    cycles_detected: list[str] = field(default_factory=list)
    missing_dependencies: list[str] = field(default_factory=list)
    missing_acceptance_criteria: list[str] = field(default_factory=list)
    duplicate_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def has_errors(self) -> bool:
        # The plan parser must detect cycles, missing
        # dependencies, duplicate IDs, missing acceptance criteria" — these
        # were previously collected into the report but never actually
        # made has_errors() true (missing_dependencies happened to be
        # caught anyway, via validate_acyclic()'s package_by_id() raising
        # for an unknown dependency ID before the cycle check itself even
        # runs; missing_acceptance_criteria and duplicate IDs had no such
        # coincidental path and were silently accepted as a valid plan).
        return bool(
            self.errors
            or self.cycles_detected
            or self.missing_dependencies
            or self.missing_acceptance_criteria
            or self.duplicate_ids
        )

    def summary(self) -> str:
        parts = [f"packages: {self.packages_found}"]
        if self.cycles_detected:
            parts.append(f"cycles: {len(self.cycles_detected)}")
        if self.missing_dependencies:
            parts.append(f"missing_dependencies: {len(self.missing_dependencies)}")
        if self.missing_acceptance_criteria:
            parts.append(
                f"missing_acceptance_criteria: {len(self.missing_acceptance_criteria)}"
            )
        if self.duplicate_ids:
            parts.append(f"duplicate_ids: {len(self.duplicate_ids)}")
        if self.errors:
            parts.append(f"errors: {len(self.errors)}")
        if self.warnings:
            parts.append(f"warnings: {len(self.warnings)}")
        return "; ".join(parts)


_WORK_PACKAGE_HEADER = re.compile(
    r"^#{2,4}\s+(Work\s+package|Package|Step|Milestone)\s*[:\-]?\s*(.*?)$",
    re.IGNORECASE,
)
_DEPENDENCY_LINE = re.compile(r"[Dd]epend(?:s|encies|ent)?\s*[:\-]\s*(.+)")
_AC_LINE = re.compile(r"^\s*[-\*]\s+\[ ?[ xX]?\]\s+(.+?)(?:\s*\|.*)?$")
_REQUIREMENT_LINE = re.compile(r"^\s*[-\*]\s+(.+?)$")
_RISK_LINE = re.compile(r"[Rr]isk\s*[:\-]\s*(critical|high|medium|low)", re.IGNORECASE)
_COMPLEXITY_LINE = re.compile(r"[Cc]omplexity\s*[:\-]\s*(\d{1,3})")
_PRIORITY_LINE = re.compile(r"[Pp]riority\s*[:\-]\s*(\d+)")
_REPO_LINE = re.compile(
    r"[Aa]ffected\s*(?:repositories?|components?|files?)\s*[:\-]\s*(.+)",
    re.IGNORECASE,
)


def parse_plan_document(text: str) -> tuple[PlanGraph, NormalizationReport]:
    report = NormalizationReport()
    packages: list[WorkPackage] = []
    current: dict[str, Any] | None = None
    in_acceptance = False
    in_requirements = False
    in_dependencies = False

    for line in text.splitlines():
        stripped = line.strip()
        stripped_lower = stripped.lower()
        if not stripped:
            continue

        header_match = _WORK_PACKAGE_HEADER.match(stripped)
        if header_match:
            if current and current.get("id"):
                packages.append(_finalize_package(current))
            header_kind = header_match.group(1).strip().lower()
            if header_kind == "milestone":
                report.warnings.append(
                    "legacy Task PLAN 'Milestone' heading interpreted as a Work Package; "
                    "new PLAN writers must emit 'Work Package'"
                )
            current = {
                "id": "",
                "title": header_match.group(2).strip(),
                "dependencies": [],
                "requirements": [],
                "acceptance_criteria": [],
                "affected_repositories": [],
                "risk": "medium",
                "priority": 0,
                "complexity": 0,
                "verification_profile": "targeted",
            }
            package_id = _derive_id(current["title"])
            current["id"] = package_id
            in_acceptance = False
            in_requirements = False
            in_dependencies = False
            continue

        if current is None:
            continue

        if stripped.startswith("##"):
            in_acceptance = False
            in_requirements = False
            in_dependencies = False

            known_section = (
                re.match(
                    r"^#{1,4}\s+(Acceptance|Requirement|Prerequisite|Precondition|Dependenc)",
                    stripped_lower,
                    re.IGNORECASE,
                )
            )
            if known_section:
                section = known_section.group(1).lower()
                if section.startswith("accept"):
                    in_acceptance = True
                elif section.startswith("requir") or section.startswith("prereq") or section.startswith("precond"):
                    in_requirements = True
                elif section.startswith("depend"):
                    in_dependencies = True
                continue
            continue

        trimmed = stripped.lstrip("-* ")

        risk_match = _RISK_LINE.match(trimmed)
        if risk_match:
            current["risk"] = risk_match.group(1).lower()
            continue

        complexity_match = _COMPLEXITY_LINE.match(trimmed)
        if complexity_match:
            complexity = int(complexity_match.group(1))
            if not 1 <= complexity <= 100:
                raise OrchestrateError("complexity must be between 1 and 100")
            current["complexity"] = complexity
            continue

        priority_match = _PRIORITY_LINE.match(trimmed)
        if priority_match:
            current["priority"] = int(priority_match.group(1))
            continue

        repo_match = _REPO_LINE.match(trimmed)
        if repo_match:
            repos = [r.strip() for r in repo_match.group(1).split(",") if r.strip()]
            current["affected_repositories"].extend(repos)
            continue

        dep_match = _DEPENDENCY_LINE.match(trimmed)
        if dep_match:
            raw_deps = [d.strip() for d in dep_match.group(1).split(",") if d.strip()]
            deps = [
                _derive_id(d) for d in raw_deps
                if d.lower() not in {"none", "n/a", "na", "-", "tbd", "todo"}
            ]
            current["dependencies"].extend(deps)
            in_dependencies = False
            continue

        if in_acceptance:
            ac_match = _AC_LINE.match(stripped)
            if ac_match:
                desc = ac_match.group(1).strip()
                ac_id = _derive_id(desc)
                current["acceptance_criteria"].append(
                    AcceptanceCriterion(id=ac_id, description=desc)
                )
            elif stripped.startswith("- ") or stripped.startswith("* "):
                desc = stripped.lstrip("-* ").strip()
                if desc:
                    ac_id = _derive_id(desc)
                    current["acceptance_criteria"].append(
                        AcceptanceCriterion(id=ac_id, description=desc)
                    )

        if in_requirements:
            req_match = _REQUIREMENT_LINE.match(stripped)
            if req_match:
                current["requirements"].append(req_match.group(1).strip())

    if current and current.get("id"):
        packages.append(_finalize_package(current))

    graph = PlanGraph(work_packages=packages)
    report.packages_found = len(packages)

    try:
        graph.validate_acyclic()
    except OrchestrateError as exc:
        report.cycles_detected.append(str(exc))

    findings = graph.validate_completeness()
    for finding in findings:
        if "missing package" in finding:
            report.missing_dependencies.append(finding)
        elif "acceptance criteria" in finding:
            report.missing_acceptance_criteria.append(finding)
        elif "duplicate work package id" in finding:
            report.duplicate_ids.append(finding)

    return graph, report


def _derive_id(title: str) -> str:
    # Underscore is preserved (not stripped) so an already-derived ID
    # (e.g. a dependency reference spelled as an ID) round-trips unchanged;
    # each whitespace/hyphen character maps to its own "_" without
    # collapsing runs, matching the locked ID-derivation scheme.
    derived = re.sub(r"[^a-zA-Z0-9\s_-]", "", title)
    derived = re.sub(r"[\s-]", "_", derived.strip().lower())
    return derived[:64] or "package"


def _finalize_package(raw: dict[str, Any]) -> WorkPackage:
    if not raw["id"]:
        raise OrchestrateError("work package requires an ID")
    if not raw["title"]:
        raise OrchestrateError("work package requires a title")
    return WorkPackage(
        id=raw["id"],
        title=raw["title"],
        dependencies=raw.get("dependencies", []),
        requirements=raw.get("requirements", []),
        acceptance_criteria=raw.get("acceptance_criteria", []),
        affected_repositories=raw.get("affected_repositories", []),
        risk=raw.get("risk", "medium"),
        priority=raw.get("priority", 0),
        complexity=int(raw.get("complexity", 0)),
        verification_profile=raw.get("verification_profile", "targeted"),
        agent_preferences=_parse_role_preferences(
            raw.get("agent_preferences"), label="agent preferences"
        ),
        skill_preferences=_parse_role_preferences(
            raw.get("skill_preferences"), label="skill preferences"
        ),
    )


def normalize_work_packages(
    packages: list[WorkPackage],
) -> tuple[PlanGraph, NormalizationReport]:
    report = NormalizationReport()
    graph = PlanGraph(work_packages=list(packages))
    report.packages_found = len(packages)

    try:
        graph.validate_acyclic()
    except OrchestrateError as exc:
        report.cycles_detected.append(str(exc))

    findings = graph.validate_completeness()
    for finding in findings:
        if "missing package" in finding:
            report.missing_dependencies.append(finding)
        elif "acceptance criteria" in finding:
            report.missing_acceptance_criteria.append(finding)
        elif "duplicate work package id" in finding:
            report.duplicate_ids.append(finding)

    return graph, report


def plan_graph_from_mapping(data: Mapping[str, Any]) -> tuple[PlanGraph, NormalizationReport]:
    """Load the deterministic machine-readable plan representation.

    Markdown remains the human specification, while ``PLAN.graph.yaml`` is the
    execution contract. Unknown fields are ignored for forward compatibility, but
    malformed packages and incomplete dependency/evidence contracts fail closed.
    """
    packages_raw = data.get("work_packages") or data.get("packages") or []
    if not isinstance(packages_raw, list):
        report = NormalizationReport(errors=["work_packages must be a list"])
        return PlanGraph(), report
    packages: list[WorkPackage] = []
    report = NormalizationReport()
    for index, raw in enumerate(packages_raw, start=1):
        if not isinstance(raw, Mapping):
            report.errors.append(f"work package {index} must be a mapping")
            continue
        try:
            criteria_raw = raw.get("acceptance_criteria") or []
            criteria: list[AcceptanceCriterion] = []
            for criterion_index, criterion in enumerate(criteria_raw, start=1):
                if isinstance(criterion, str):
                    description = criterion.strip()
                    criterion_id = _derive_id(description)
                elif isinstance(criterion, Mapping):
                    description = str(criterion.get("description", "")).strip()
                    criterion_id = str(criterion.get("id") or _derive_id(description)).strip()
                else:
                    raise OrchestrateError("acceptance criterion must be a string or mapping")
                if not description or not criterion_id:
                    raise OrchestrateError("acceptance criterion requires id and description")
                verified = bool(criterion.get("verified", False)) if isinstance(criterion, Mapping) else False
                evidence = str(criterion.get("evidence", "")) if isinstance(criterion, Mapping) else ""
                criteria.append(
                    AcceptanceCriterion(
                        id=criterion_id,
                        description=description,
                        verified=verified,
                        evidence=evidence,
                    )
                )
            stage = WorkPackageStage(str(raw.get("stage", "prepare")))
            status = str(raw.get("status", "completed" if stage == WorkPackageStage.COMPLETED else "pending"))
            package = WorkPackage(
                id=str(raw.get("id", "")).strip(),
                title=str(raw.get("title", "")).strip(),
                dependencies=[str(item) for item in (raw.get("dependencies") or [])],
                requirements=[str(item) for item in (raw.get("requirements") or [])],
                acceptance_criteria=criteria,
                affected_repositories=[str(item) for item in (raw.get("affected_repositories") or [])],
                kind=WorkPackageKind(str(raw.get("kind", "development"))),
                repository_sync=(
                    RepositorySyncSpec.from_mapping(raw.get("repository_sync"))
                    if str(raw.get("kind", "development")) == WorkPackageKind.REPOSITORY_SYNC.value
                    else None
                ),
                stage=stage,
                status=status,
                risk=str(raw.get("risk", "medium")),
                priority=int(raw.get("priority", 0)),
                complexity=int(raw.get("complexity", 0)),
                verification_profile=str(raw.get("verification_profile", "targeted")),
                agent_preferences=_parse_role_preferences(
                    raw.get("agent_preferences"), label="agent preferences"
                ),
                skill_preferences=_parse_role_preferences(
                    raw.get("skill_preferences"), label="skill preferences"
                ),
                execution_mode=str(raw.get("execution_mode", "standard")),
                parent_id=str(raw.get("parent_id", "")),
                shard_key=str(raw.get("shard_key", "")),
                generated_by=str(raw.get("generated_by", "")),
                parallel_safe=bool(raw.get("parallel_safe", False)),
                read_scope=[str(item) for item in (raw.get("read_scope") or [])],
                write_scope=[str(item) for item in (raw.get("write_scope") or [])],
                conflict_keys=[str(item) for item in (raw.get("conflict_keys") or [])],
                decomposition_status=str(raw.get("decomposition_status", "")),
                decomposition_origin_stage=str(raw.get("decomposition_origin_stage", "")),
                decomposition_reason=str(raw.get("decomposition_reason", "")),
                decomposition_agent_id=str(raw.get("decomposition_agent_id", "")),
                decomposition_plan_hash=str(raw.get("decomposition_plan_hash", "")),
                shard_ids=[str(item) for item in (raw.get("shard_ids") or [])],
            )
            if not package.id or not package.title:
                raise OrchestrateError("work package requires id and title")
            packages.append(package)
        except (TypeError, ValueError, OrchestrateError) as exc:
            report.errors.append(f"work package {index}: {exc}")
    graph, normalized = normalize_work_packages(packages)
    normalized.errors[:0] = report.errors
    return graph, normalized


def load_plan_graph_file(path: Path) -> tuple[PlanGraph, NormalizationReport]:
    """Load Markdown or YAML plans, preferring explicit graph artifacts."""
    if not path.is_file():
        return PlanGraph(), NormalizationReport(errors=[f"plan file not found: {path}"])
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            return PlanGraph(), NormalizationReport(errors=[f"invalid plan YAML: {exc}"])
        if not isinstance(raw, Mapping):
            return PlanGraph(), NormalizationReport(errors=["plan YAML must contain a mapping"])
        return plan_graph_from_mapping(raw)
    return parse_plan_document(path.read_text(encoding="utf-8"))
