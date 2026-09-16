"""Registration seam for agent-runtime candidate construction.

Orchestration is already runtime-neutral: the scheduler and orchestrator talk
to candidates through the ``AgentRuntime`` protocol and never branch on a
runtime kind.  The one remaining hard-coded dispatch was the ``if/elif`` in
``build_runtime_candidates``, which meant adding a runtime required editing
core candidate construction.

This module turns that dispatch into registration.  It deliberately holds
nothing but a mapping of runtime kind to builder callable, so it stays
importable from runtime-neutral configuration code without pulling in any
concrete runtime implementation (see ``tools/check_architecture.py``).

``RuntimeKind`` remains the enum for the two runtimes Execraft ships.  Because it
subclasses ``str``, a registered third-party kind can be carried as a plain
string and every existing ``runtime.kind == RuntimeKind.NATIVE`` comparison
keeps working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


class RuntimeRegistrationError(ValueError):
    """Raised when a runtime kind is registered or resolved incorrectly."""


@dataclass
class RuntimeBuildContext:
    """Everything a builder needs to construct one profile's candidate.

    ``shared`` is scratch space scoped to a single ``build_runtime_candidates``
    call.  Runtimes that must reuse an expensive per-runtime resource across
    profiles (an OpenClaw Gateway host, for example) cache it there instead of
    reaching back into candidate-construction internals.
    """

    execution: Any
    profile: Any
    runtime: Any
    workdir: Path
    state_root: Path
    read_only: bool
    opencode_config_path: Path | None = None
    model_registry: Any = None
    subagent_strategy: Any = None
    shared: dict[str, Any] = field(default_factory=dict)


RuntimeCandidateBuilder = Callable[[RuntimeBuildContext], Any]

_BUILDERS: dict[str, RuntimeCandidateBuilder] = {}


def register_runtime_builder(
    kind: str, builder: RuntimeCandidateBuilder, *, replace: bool = False
) -> None:
    """Register the candidate builder for one runtime kind.

    Re-registration is rejected unless ``replace`` is set, so a typo or a
    duplicate import cannot silently take over an already-registered runtime.
    """

    key = _normalized(kind)
    if not callable(builder):
        raise RuntimeRegistrationError(f"runtime builder for {key!r} must be callable")
    if key in _BUILDERS and not replace:
        raise RuntimeRegistrationError(f"runtime kind already registered: {key}")
    _BUILDERS[key] = builder


def unregister_runtime_builder(kind: str) -> None:
    """Remove a registration. Intended for tests that register a fake runtime."""

    _BUILDERS.pop(_normalized(kind), None)


def runtime_builder(kind: str) -> RuntimeCandidateBuilder:
    key = _normalized(kind)
    try:
        return _BUILDERS[key]
    except KeyError as exc:
        known = ", ".join(registered_runtime_kinds()) or "none"
        raise RuntimeRegistrationError(
            f"unsupported runtime kind: {key} (registered: {known})"
        ) from exc


def is_registered_runtime_kind(kind: str) -> bool:
    return _normalized(kind) in _BUILDERS


def registered_runtime_kinds() -> tuple[str, ...]:
    return tuple(sorted(_BUILDERS))


def _normalized(kind: Any) -> str:
    # RuntimeKind subclasses str, so enum members normalize to their value.
    value = str(getattr(kind, "value", kind)).strip().lower()
    if not value:
        raise RuntimeRegistrationError("runtime kind cannot be empty")
    return value
