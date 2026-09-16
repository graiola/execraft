"""Provider-neutral reasoning-effort levels.

Every supported CLI exposes a knob that trades reasoning depth against tokens
and latency, but each spells it differently and supports a different ladder:

===============  ==========================================  ====================
Provider         Flag                                        Levels
===============  ==========================================  ====================
Claude Code      ``--effort <level>``                        low..max (5)
Antigravity      ``--effort <level>``                        low, medium, high
Codex            ``-c model_reasoning_effort="<level>"``     low, medium, high
OpenCode         ``--variant <level>``                       minimal, high, max
===============  ==========================================  ====================

Configuration is written once in the orchestrator's own vocabulary and each
adapter maps it onto what its CLI accepts.  Mapping down (``xhigh`` on a CLI
that stops at ``high``) is deliberately silent: an effort level is a cost/quality
hint, and refusing to run a package because a provider has a shorter ladder
would trade a small quality difference for a hard scheduling failure.
"""

from __future__ import annotations

from typing import Mapping


#: The orchestrator's own ladder, weakest first.
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


class EffortPolicy:
    """Resolve the effort level for one invocation, clamped to a provider.

    Adapters hold one of these instead of a bare string: effort is configured
    per capability, but an adapter instance is shared across capabilities.
    """

    def __init__(
        self,
        *,
        default: object = "",
        by_capability: Mapping[str, object] | None = None,
        supported: tuple[str, ...] = EFFORT_LEVELS,
    ) -> None:
        self._supported = tuple(supported)
        self._default = clamp_effort(normalize_effort(default), self._supported)
        self._by_capability = {
            str(capability): clamp_effort(normalize_effort(level), self._supported)
            for capability, level in dict(by_capability or {}).items()
        }

    def for_capability(self, capability: object) -> str:
        """Return the clamped effort level, or ``""`` to leave the CLI default."""

        return self._by_capability.get(str(capability or ""), self._default)

    def for_handoff(self, execution_context: Mapping[str, object] | None) -> str:
        """Resolve effort from a handoff's authoritative capability marker.

        Handoffs assembled outside the context planner carry no capability, and
        fall back to the provider-wide default.
        """

        context = dict(execution_context or {})
        return self.for_capability(context.get("capability", ""))


def normalize_effort(value: object) -> str:
    """Return a canonical effort level, or ``""`` when unset.

    Raises ``ValueError`` for a non-empty value outside the ladder so a typo in
    project configuration fails at load time rather than at provider dispatch.
    """

    level = str(value or "").strip().lower()
    if not level:
        return ""
    if level not in EFFORT_LEVELS:
        raise ValueError(
            f"unsupported effort level {level!r}; expected one of "
            + ", ".join(EFFORT_LEVELS)
        )
    return level


def clamp_effort(level: str, supported: tuple[str, ...]) -> str:
    """Map *level* onto the nearest level a provider actually supports.

    The result is the strongest supported level that is no stronger than the
    request. When every supported level is stronger than the request (a CLI with
    no low-effort setting), the weakest supported level is used.
    """

    if not level:
        return ""
    if level in supported:
        return level
    requested = EFFORT_LEVELS.index(level)
    weaker = [
        item
        for item in supported
        if item in EFFORT_LEVELS and EFFORT_LEVELS.index(item) <= requested
    ]
    if weaker:
        return max(weaker, key=EFFORT_LEVELS.index)
    ranked = [item for item in supported if item in EFFORT_LEVELS]
    return min(ranked, key=EFFORT_LEVELS.index) if ranked else ""
