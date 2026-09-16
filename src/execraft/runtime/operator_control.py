"""Compile operator routing choices into durable package preferences.

The scheduler remains authoritative.  Operator controls only edit the existing
role -> profile preference mapping at safe orchestration boundaries; no live
Native/OpenClaw session is migrated between runtime implementations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from execraft.orchestrate.execution_policy import role_definition

from .selection import RuntimePreferenceMode, RuntimeSelectionPlan, plan_runtime_selection


@dataclass(frozen=True)
class RuntimePreferenceUpdate:
    role: str
    capability: str
    plan: RuntimeSelectionPlan
    agent_preferences: Mapping[str, list[str]]
    binding_roles: tuple[str, ...]

    def as_mapping(self) -> dict[str, object]:
        return {
            "role": self.role,
            "capability": self.capability,
            "plan": self.plan.as_mapping(),
            "agent_preferences": {
                str(role): list(values) for role, values in self.agent_preferences.items()
            },
            "agent_preference_binding_roles": list(self.binding_roles),
        }


def compile_runtime_preference_update(
    config: Any,
    *,
    existing_preferences: Mapping[str, list[str]] | None,
    existing_binding_roles: tuple[str, ...] | list[str] = (),
    role: str,
    mode: RuntimePreferenceMode | str,
    runtime_id: str = "",
    model_route_id: str = "",
    target_id: str = "",
    invocation_active: bool = False,
) -> RuntimePreferenceUpdate:
    """Return a complete package preference mapping after one routing choice.

    Automatic mode removes the role override and returns control to ordinary
    scheduler policy. Prefer/force compile to the profile ordering already used
    by orchestration, so this feature cannot become a second scheduler.
    """

    role = str(role).strip()
    definition = role_definition(role)
    capability = definition.capability.value
    plan = plan_runtime_selection(
        config,
        capability=capability,
        mode=mode,
        runtime_id=str(runtime_id).strip(),
        model_route_id=str(model_route_id).strip(),
        target_id=str(target_id).strip(),
        invocation_active=invocation_active,
    )
    updated = {
        str(role): [str(item) for item in values if str(item)]
        for role, values in (existing_preferences or {}).items()
    }
    bindings = [str(item) for item in existing_binding_roles if str(item)]
    if plan.mode == RuntimePreferenceMode.AUTOMATIC:
        updated.pop(role, None)
        bindings = [item for item in bindings if item != role]
    else:
        updated[role] = list(plan.preferred_profiles)
        if plan.mode == RuntimePreferenceMode.FORCE:
            if role not in bindings:
                bindings.append(role)
        else:
            bindings = [item for item in bindings if item != role]
    return RuntimePreferenceUpdate(role, capability, plan, updated, tuple(bindings))
