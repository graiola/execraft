"""Shared validation for executable task plan graphs.

The onboarding and planning layers both need to validate ``PLAN.graph.yaml``.
Keeping the rules here avoids subtly different acceptance criteria for imported
and generated plans.
"""

from __future__ import annotations

from typing import Any, Mapping

from execraft.orchestrate.models import PlanGraph
from execraft.orchestrate.normalizer import plan_graph_from_mapping


class PlanGraphValidationError(ValueError):
    """Raised when an executable plan graph is structurally invalid."""


def validate_plan_graph_mapping(
    data: Mapping[str, Any],
    *,
    allowed_repositories: set[str],
) -> PlanGraph:
    """Validate one executable graph against task repository ownership."""

    graph, report = plan_graph_from_mapping(data)
    errors = list(report.errors)
    errors.extend(report.cycles_detected)
    errors.extend(graph.validate_completeness())
    if not graph.work_packages:
        errors.append("plan graph must contain at least one work package")
    for package in graph.work_packages:
        unknown = sorted(set(package.affected_repositories) - allowed_repositories)
        if unknown:
            errors.append(
                f"package {package.id} references repositories outside task scope: "
                + ", ".join(unknown)
            )
        if not package.affected_repositories:
            errors.append(f"package {package.id} has no affected repositories")
    source_document = str(data.get("source_document", "")).strip()
    if source_document and source_document != "PLAN.md":
        errors.append(
            f"plan graph source_document must be 'PLAN.md' when set, got {source_document!r}"
        )
    if errors:
        raise PlanGraphValidationError("; ".join(dict.fromkeys(errors)))
    return graph


__all__ = ["PlanGraphValidationError", "validate_plan_graph_mapping"]
