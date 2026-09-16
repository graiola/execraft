"""Compile operator runtime choices into existing profile preferences."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from execraft.runtime.product_support import profile_product_support


class RuntimeSelectionError(ValueError):
    pass


class RuntimePreferenceMode(str, Enum):
    AUTOMATIC = "automatic"
    PREFER = "prefer"
    FORCE = "force"


def _value(value: object) -> str:
    return str(getattr(value, "value", value))


def _supports(profile: Any, capability: str) -> bool:
    return capability in {_value(item) for item in getattr(profile, "capabilities", ())}


def _product_supported(config: Any, profile: Any) -> bool:
    """Apply product support policy when the full normalized topology is available.

    Lightweight compatibility/test projections used by older callers expose
    only ``agents``; they cannot select remote targets and therefore remain
    eligible rather than being forced to implement the complete v4 interface.
    """

    if not hasattr(config, "runtime"):
        return True
    return profile_product_support(config, profile).supported


def _effective_target_id(config: Any, profile: Any) -> str:
    explicit = str(getattr(profile, "target_id", ""))
    if explicit or not str(getattr(profile, "model_route_id", "")):
        return explicit
    route = config.model_route(profile.model_route_id)
    return str(getattr(route, "default_target", ""))


def _matches(
    config: Any,
    profile: Any,
    *,
    runtime_id: str,
    model_route_id: str,
    target_id: str,
) -> bool:
    return (
        (not runtime_id or profile.runtime_id == runtime_id)
        and (not model_route_id or profile.model_route_id == model_route_id)
        and (not target_id or _effective_target_id(config, profile) == target_id)
    )


@dataclass(frozen=True)
class RuntimeSelectionPlan:
    mode: RuntimePreferenceMode
    preferred_profiles: tuple[str, ...]
    matching_profiles: tuple[str, ...]
    effective_when: str
    requires_cancel_for_immediate: bool
    hot_migration_supported: bool = False

    def as_mapping(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "preferred_profiles": list(self.preferred_profiles),
            "matching_profiles": list(self.matching_profiles),
            "effective_when": self.effective_when,
            "requires_cancel_for_immediate": self.requires_cancel_for_immediate,
            "hot_migration_supported": self.hot_migration_supported,
        }


def plan_runtime_selection(
    config: Any,
    *,
    capability: str,
    mode: RuntimePreferenceMode | str = RuntimePreferenceMode.AUTOMATIC,
    runtime_id: str = "",
    model_route_id: str = "",
    target_id: str = "",
    invocation_active: bool = False,
) -> RuntimeSelectionPlan:
    """Plan a safe runtime switch using the existing profile-preference seam.

    ``automatic`` clears an operator override.  ``prefer`` ranks matching
    profiles first but preserves compatible fallbacks.  ``force`` restricts the
    preference list to matching profiles.  The plan intentionally applies only
    at invocation boundaries; callers must cancel a running invocation before an
    immediate switch.
    """

    mode = RuntimePreferenceMode(mode)
    eligible = [
        item
        for item in config.agents
        if bool(getattr(item, "enabled", True))
        and _supports(item, capability)
        and _product_supported(config, item)
    ]
    if mode == RuntimePreferenceMode.AUTOMATIC:
        preferred: tuple[str, ...] = ()
        matching: tuple[str, ...] = ()
    else:
        matches = [
            item
            for item in eligible
            if _matches(
                config,
                item,
                runtime_id=runtime_id,
                model_route_id=model_route_id,
                target_id=target_id,
            )
        ]
        if not matches:
            requested = " / ".join(
                value for value in (runtime_id, model_route_id, target_id) if value
            ) or "requested selection"
            raise RuntimeSelectionError(
                f"no {capability!r} execution profile matches {requested}"
            )
        matching = tuple(item.id for item in matches)
        if mode == RuntimePreferenceMode.FORCE:
            preferred = matching
        else:
            match_ids = set(matching)
            preferred = matching + tuple(item.id for item in eligible if item.id not in match_ids)

    return RuntimeSelectionPlan(
        mode=mode,
        preferred_profiles=preferred,
        matching_profiles=matching,
        effective_when="after_current_invocation" if invocation_active else "next_invocation",
        requires_cancel_for_immediate=invocation_active,
    )
