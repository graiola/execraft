"""Package-level execution policy shared by CLI, GUI, and orchestrator.

The orchestrator historically stored only ranked agent preferences directly on
``WorkPackage`` and duplicated role knowledge across Python and JavaScript.  This
module is the canonical definition for execution roles, their capabilities, and
role-default workflow skills. Preferences are soft by default; operator policy may mark one
role pool as binding, while health, capability, independence, and concurrency
policy remain authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from execraft.orchestrate.scheduler import AgentCapability


class ExecutionPolicyError(ValueError):
    """Raised when a package execution policy is malformed."""


@dataclass(frozen=True)
class ExecutionRoleDefinition:
    """Static metadata for one package execution role."""

    id: str
    label: str
    capability: AgentCapability
    default_skills: tuple[str, ...] = ()
    read_only: bool = False

    def as_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "capability": self.capability.value,
            "default_skills": list(self.default_skills),
            "read_only": self.read_only,
        }


EXECUTION_ROLES: tuple[ExecutionRoleDefinition, ...] = (
    ExecutionRoleDefinition(
        id="decompose",
        label="Decompose",
        capability=AgentCapability.DECOMPOSE,
        default_skills=("ai-plan",),
        read_only=True,
    ),
    ExecutionRoleDefinition(
        id="implement",
        label="Implement",
        capability=AgentCapability.IMPLEMENT,
        default_skills=("ai-implement",),
    ),
    ExecutionRoleDefinition(
        id="review",
        label="Review",
        capability=AgentCapability.REVIEW,
        default_skills=("ai-review",),
        read_only=True,
    ),
    ExecutionRoleDefinition(
        id="fix_review",
        label="Fix review",
        capability=AgentCapability.FIX_REVIEW,
        default_skills=("ai-fix-review",),
    ),
    ExecutionRoleDefinition(
        id="final_review",
        label="Final review",
        capability=AgentCapability.REVIEW,
        default_skills=("ai-review",),
        read_only=True,
    ),
)

ROLE_BY_ID: dict[str, ExecutionRoleDefinition] = {
    definition.id: definition for definition in EXECUTION_ROLES
}


def role_definition(role: str) -> ExecutionRoleDefinition:
    """Return the canonical definition for *role*."""

    normalized = str(role).strip()
    try:
        return ROLE_BY_ID[normalized]
    except KeyError as exc:
        raise ExecutionPolicyError(
            f"unsupported execution role {normalized!r}; expected one of "
            + ", ".join(ROLE_BY_ID)
        ) from exc


def normalize_binding_roles(value: object) -> list[str]:
    """Validate roles whose ranked agent pool is operator-binding."""

    if value is None:
        return []
    if not isinstance(value, (list, tuple, set)) or isinstance(value, (str, bytes)):
        raise ExecutionPolicyError("agent preference binding roles must be a list")
    result: list[str] = []
    for item in value:
        role = str(item).strip()
        if not role or role in result:
            continue
        role_definition(role)
        result.append(role)
    return result


def role_for_capability(capability: AgentCapability, *, final_review: bool = False) -> str:
    """Map a runtime capability to the package policy role used for selection."""

    if capability == AgentCapability.REVIEW and final_review:
        return "final_review"
    return capability.value


def normalize_ranked_ids(values: Iterable[object], *, label: str) -> list[str]:
    """Normalize a ranked identifier sequence while preserving declaration order."""

    if isinstance(values, (str, bytes, Mapping, set, frozenset)):
        raise ExecutionPolicyError(f"{label} must be a list")
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise ExecutionPolicyError(f"{label} must be a list") from exc
    result: list[str] = []
    for raw_value in iterator:
        value = str(raw_value).strip()
        if not value:
            continue
        if value not in result:
            result.append(value)
    return result


def normalize_role_mapping(
    mapping: Mapping[str, Iterable[object]] | None,
    *,
    label: str,
) -> dict[str, list[str]]:
    """Validate and normalize a role-to-ranked-identifiers mapping."""

    if mapping is None:
        return {}
    if not isinstance(mapping, Mapping):
        raise ExecutionPolicyError(f"{label} must be a mapping")
    normalized: dict[str, list[str]] = {}
    for raw_role, raw_values in mapping.items():
        role = str(raw_role).strip()
        role_definition(role)
        values = normalize_ranked_ids(raw_values, label=f"{label} for {role!r}")
        if values:
            normalized[role] = values
    return normalized


def effective_skill_ids(role: str, configured: Iterable[object] | None) -> list[str]:
    """Return explicit skills or the canonical role defaults.

    An empty configured list means "use defaults" rather than "disable all
    workflow guidance".  This keeps legacy state safe while allowing an operator
    to replace the defaults by selecting one or more skills explicitly.
    """

    definition = role_definition(role)
    values = normalize_ranked_ids(configured or (), label=f"skills for {role!r}")
    return values or list(definition.default_skills)


def execution_policy_mapping(
    *,
    agents: Mapping[str, Iterable[object]] | None,
    skills: Mapping[str, Iterable[object]] | None,
) -> dict[str, dict[str, list[str]]]:
    """Return one canonical JSON-compatible execution policy mapping."""

    return {
        "agents": normalize_role_mapping(agents, label="agent preferences"),
        "skills": normalize_role_mapping(skills, label="skill preferences"),
    }


def execution_role_metadata() -> list[dict[str, Any]]:
    """Return GUI/API metadata in stable role order."""

    return [definition.as_mapping() for definition in EXECUTION_ROLES]
