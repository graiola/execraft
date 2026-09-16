"""Agent subprocess heartbeat support.

The orchestrator owns presentation and persistence of progress. Provider
adapters only enrich low-level subprocess snapshots with package/provider
identity and forward them through a callback. Heartbeats are observational:
callback failures never affect the agent process or orchestration state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from execraft.orchestrate.scheduler import StructuredHandoff

AgentHeartbeatCallback = Callable[[Mapping[str, Any]], None]
AgentOutputCallback = Callable[[Mapping[str, Any]], None]
AgentControlCallback = Callable[[Mapping[str, Any]], list[Mapping[str, Any]]]
AgentTerminalCallback = AgentControlCallback
AgentInteractionCallback = Callable[[Mapping[str, Any]], None]


class AgentHeartbeatEmitter:
    """Bind managed-subprocess heartbeats to an agent handoff."""

    def __init__(self, *, provider_id: str, model: str = "") -> None:
        self._provider_id = provider_id
        self._model = model
        self._callback: AgentHeartbeatCallback | None = None
        self._output_callback: AgentOutputCallback | None = None
        self._control_callback: AgentControlCallback | None = None
        self._terminal_callback: AgentTerminalCallback | None = None
        self._interaction_callback: AgentInteractionCallback | None = None
        self._interval_seconds = 30.0

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    @property
    def enabled(self) -> bool:
        return self._callback is not None and self._interval_seconds > 0

    def configure(
        self,
        callback: AgentHeartbeatCallback | None,
        *,
        interval_seconds: float = 30.0,
    ) -> None:
        self._callback = callback
        self._interval_seconds = max(0.0, float(interval_seconds))

    def configure_output(self, callback: AgentOutputCallback | None) -> None:
        """Configure an observer for decoded provider stdout/stderr."""
        self._output_callback = callback

    def configure_operator_controls(
        self, callback: AgentControlCallback | None
    ) -> None:
        """Configure provider-native steering controls.

        This queue never implies that the child receives a TTY.  Protocol
        adapters translate records into their native steer/interrupt messages.
        """
        self._control_callback = callback

    def configure_terminal(self, callback: AgentTerminalCallback | None) -> None:
        """Configure controls for an explicitly PTY-capable child process."""
        self._terminal_callback = callback

    def configure_interaction(self, callback: AgentInteractionCallback | None) -> None:
        """Configure the provider-neutral live conversation event observer."""
        self._interaction_callback = callback

    def output_callback_for(self, handoff: StructuredHandoff):
        if self._output_callback is None:
            return None

        def emit(stream: str, text: str) -> None:
            callback = self._output_callback
            if callback is None or not text:
                return
            payload = {
                "package_id": handoff.work_package_id,
                "stage": handoff.stage,
                "agent_id": self._provider_id,
                "model": self._model,
                "stream": stream,
                "text": text,
            }
            try:
                callback(payload)
            except Exception:
                return

        return emit

    def interaction_callback_for(self, handoff: StructuredHandoff):
        """Return a callback that attributes semantic events to *handoff*."""

        if self._interaction_callback is None:
            return None

        def emit(event: Mapping[str, Any]) -> None:
            callback = self._interaction_callback
            if callback is None:
                return
            payload = {
                "package_id": handoff.work_package_id,
                "stage": handoff.stage,
                "agent_id": self._provider_id,
                "model": self._model,
                **dict(event),
            }
            try:
                callback(payload)
            except Exception:
                return

        return emit

    def control_callback_for(self, handoff: StructuredHandoff):
        """Return the provider-native operator-control consumer for *handoff*."""
        return self._scoped_control_callback(handoff, self._control_callback)

    def terminal_callback_for(self, handoff: StructuredHandoff):
        """Return the PTY control consumer for *handoff*."""
        return self._scoped_control_callback(handoff, self._terminal_callback)

    def _scoped_control_callback(
        self,
        handoff: StructuredHandoff,
        configured: AgentControlCallback | None,
    ):
        if configured is None:
            return None

        def consume() -> list[Mapping[str, Any]]:
            callback = configured
            payload = {
                "package_id": handoff.work_package_id,
                "stage": handoff.stage,
                "agent_id": self._provider_id,
                "model": self._model,
            }
            try:
                return list(callback(payload) or [])
            except Exception:
                return []

        return consume

    def callback_for(
        self, handoff: StructuredHandoff
    ) -> AgentHeartbeatCallback | None:
        if not self.enabled:
            return None

        def emit(snapshot: Mapping[str, Any]) -> None:
            callback = self._callback
            if callback is None:
                return
            payload = {
                "package_id": handoff.work_package_id,
                "stage": handoff.stage,
                "agent_id": self._provider_id,
                "model": self._model,
                **dict(snapshot),
            }
            try:
                callback(payload)
            except Exception:
                # Heartbeats are operator telemetry, never a product execution check.
                return

        return emit


def run_with_heartbeat(
    runner: Callable[..., Any],
    *,
    runner_is_injected: bool,
    args: list[str],
    cwd: Any,
    timeout: int,
    emitter: AgentHeartbeatEmitter,
    handoff: StructuredHandoff,
    output_classifier: Any = None,
    inactivity_timeout: float | None = None,
    output_silence_timeout: float | None = None,
    max_output_bytes: int | None = None,
    stdin_payload: str | bytes | None = None,
    interactive_terminal: bool = False,
    output_callback: Any = None,
    first_output_timeout: float | None = None,
    environment: Mapping[str, str] | None = None,
) -> Any:
    """Invoke a provider runner without breaking existing injected fakes.

    Real provider runners accept managed-subprocess heartbeat keywords. Existing
    test/plugin runners keep their smaller historical signature and are invoked
    unchanged.
    """
    kwargs: dict[str, Any] = {"cwd": cwd, "timeout": timeout}
    if not runner_is_injected:
        if environment is not None:
            kwargs["environment"] = environment
        if emitter.enabled:
            kwargs.update(
                {
                    "heartbeat_interval": emitter.interval_seconds,
                    "heartbeat_callback": emitter.callback_for(handoff),
                }
            )
        resolved_output_callback = (
            output_callback or emitter.output_callback_for(handoff)
        )
        if resolved_output_callback is not None:
            kwargs["output_callback"] = resolved_output_callback
        if stdin_payload is not None:
            kwargs["input_data"] = stdin_payload
        terminal_callback = emitter.terminal_callback_for(handoff)
        if (
            interactive_terminal
            and stdin_payload is None
            and terminal_callback is not None
        ):
            kwargs["terminal_callback"] = terminal_callback
        if output_classifier is not None:
            kwargs["output_classifier"] = output_classifier
        if inactivity_timeout is not None and inactivity_timeout > 0:
            kwargs["inactivity_timeout"] = inactivity_timeout
        if output_silence_timeout is not None and output_silence_timeout > 0:
            kwargs["output_silence_timeout"] = output_silence_timeout
        if first_output_timeout is not None and first_output_timeout > 0:
            kwargs["first_output_timeout"] = first_output_timeout
        if max_output_bytes is not None and max_output_bytes > 0:
            kwargs["max_output_bytes"] = max_output_bytes
    return runner(args, **kwargs)
