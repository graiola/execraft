"""Google Antigravity CLI adapter for the generic Execraft agent protocol.

Antigravity exposes a non-interactive print mode through ``-p``/``--print``.
Unlike Claude Code, the current CLI does not expose a JSON transport flag: the
agent's final response is written directly to stdout. Structured task output is
therefore validated by the orchestrator from that text, exactly as it is for any
other provider whose transport and task schema are separate concerns.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from execraft.agents.effort import EffortPolicy
from execraft.agents.heartbeat import AgentHeartbeatEmitter, run_with_heartbeat
from execraft.agents.output_classification import (
    AgentOutputClassifier,
)
from execraft.runtime.contracts import RuntimeCapabilities
from execraft.orchestrate.scheduler import (
    AgentAdapter,
    AgentCapability,
    AgentExecutionError,
    Availability,
    StructuredHandoff,
    availability_for_failure,
    build_agent_prompt,
    diagnose_failure,
)
from execraft.orchestrate.failure_translation import (
    managed_process_failure,
    provider_failure,
)
from execraft.process import ManagedProcessTerminated, managed_run

AntigravityRunner = Callable[..., "subprocess.CompletedProcess[str]"]

# AGY exposes a three-level ladder; the two strongest orchestrator levels
# clamp down onto ``high``.
_SUPPORTED_EFFORT = ("low", "medium", "high")

_DEFAULT_CAPABILITIES = {
    AgentCapability.IMPLEMENT,
    AgentCapability.REVIEW,
    AgentCapability.FIX_REVIEW,
}



def _default_runner(
    args: list[str],
    *,
    cwd: Path,
    timeout: int,
    heartbeat_interval: float = 0.0,
    heartbeat_callback=None,
    output_classifier=None,
    output_callback=None,
    terminal_callback=None,
    inactivity_timeout: float | None = None,
    output_silence_timeout: float | None = None,
    first_output_timeout: float | None = None,
) -> "subprocess.CompletedProcess[str]":
    completed = managed_run(
        args,
        cwd=str(cwd),
        timeout=timeout,
        heartbeat_interval=heartbeat_interval,
        heartbeat_callback=heartbeat_callback,
        output_classifier=output_classifier,
        output_callback=output_callback,
        terminal_callback=terminal_callback,
        inactivity_timeout=inactivity_timeout,
        output_silence_timeout=output_silence_timeout,
        first_output_timeout=first_output_timeout,
        capture_mode="file",
    )
    if (
        completed.returncode == 0
        and not (completed.stdout or "").strip()
        and not (completed.stderr or "").strip()
    ):
        # AGY 1.1.x has had several headless regressions where a supervised
        # invocation exits successfully without publishing its final response.
        # Retry once through the simplest communicate()-based subprocess path.
        # This mirrors the successful `agy -p ... >out 2>err` diagnostic while
        # preserving the normal supervised path for every healthy invocation.
        return managed_run(
            args,
            cwd=str(cwd),
            timeout=timeout,
            output_callback=output_callback,
            first_output_timeout=first_output_timeout,
            capture_mode="pipe",
        )
    return completed


class AntigravityCliAgentAdapter(AgentAdapter):
    """Drive Antigravity through its supported headless text contract."""

    def __init__(
        self,
        *,
        provider_id: str = "antigravity",
        capabilities: set[AgentCapability] | None = None,
        model: str = "",
        effort: str = "",
        effort_by_capability: dict[str, str] | None = None,
        dangerously_skip_permissions: bool = False,
        sandbox_enabled: bool = False,
        policy_paths: tuple[str, ...] | list[str] | None = None,
        workdir: Path | None = None,
        timeout_seconds: int = 600,
        inactivity_timeout_seconds: int = 900,
        output_silence_timeout_seconds: int = 0,
        first_output_timeout_seconds: int = 0,
        max_internal_retry_delay_seconds: int = 120,
        capability_weight: int = 50,
        capability_weights: dict[AgentCapability, int] | None = None,
        runner: AntigravityRunner | None = None,
        binary: str | None = None,
    ) -> None:
        self._provider_id = provider_id
        self._capabilities = capabilities or set(_DEFAULT_CAPABILITIES)
        self._model = model
        self._effort = EffortPolicy(
            default=effort,
            by_capability=effort_by_capability,
            supported=_SUPPORTED_EFFORT,
        )
        self._dangerously_skip_permissions = dangerously_skip_permissions
        self._sandbox_enabled = sandbox_enabled
        self._policy_paths = tuple(str(item) for item in (policy_paths or ()))
        self._workdir = workdir or Path.cwd()
        self._timeout_seconds = timeout_seconds
        self._inactivity_timeout_seconds = inactivity_timeout_seconds
        self._output_silence_timeout_seconds = output_silence_timeout_seconds
        self._first_output_timeout_seconds = first_output_timeout_seconds
        self._max_internal_retry_delay_seconds = max_internal_retry_delay_seconds
        self._capability_weight = capability_weight
        self._capability_weights = dict(capability_weights or {})
        self._runner = runner or _default_runner
        self._runner_is_injected = runner is not None
        self._binary_explicit = binary is not None
        self._binary = binary or "agy"
        self._last_availability = Availability.AVAILABLE
        self._heartbeat = AgentHeartbeatEmitter(
            provider_id=self._provider_id, model=self._model
        )

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def capabilities(self) -> set[AgentCapability]:
        return self._capabilities

    @property
    def execution_capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            read_only_enforcement="advisory",
            workspace_write=True,
            network_isolation=False,
            command_allowlist=False,
            structured_output=True,
            structured_output_enforcement="prompt_only",
            streaming=False,
            raw_output_streaming=True,
            semantic_streaming=False,
            provider_native_steering=False,
            interactive_pty=False,
            session_resume=False,
        )

    @property
    def adapter_name(self) -> str:
        return "antigravity-cli"

    @property
    def model(self) -> str:
        return self._model

    @property
    def binary(self) -> str:
        return self._binary

    @property
    def capability_weight(self) -> int:
        return self._capability_weight

    def weight_for_capability(self, capability: AgentCapability) -> int:
        return self._capability_weights.get(capability, self._capability_weight)

    @property
    def availability(self) -> Availability:
        if (
            not self._runner_is_injected or self._binary_explicit
        ) and shutil.which(self._binary) is None:
            return Availability.DISABLED
        return self._last_availability

    def reset_transient_availability(self) -> None:
        if self._last_availability.is_transient:
            self._last_availability = Availability.AVAILABLE

    def configure_heartbeat(self, callback, *, interval_seconds: float = 30.0) -> None:
        self._heartbeat.configure(callback, interval_seconds=interval_seconds)

    def configure_output(self, callback) -> None:
        """Configure a read-only observer for supervised provider output."""
        self._heartbeat.configure_output(callback)

    def execute(self, handoff: StructuredHandoff) -> dict[str, Any]:
        self._validate_configuration()
        prompt = build_agent_prompt(handoff)
        workdir = Path(handoff.working_directory).resolve() if handoff.working_directory else self._workdir
        args = self._build_command(
            prompt, effort=self._effort.for_handoff(handoff.execution_context)
        )

        output_classifier = AgentOutputClassifier(
            adapter=self.adapter_name,
            provider_id=self._provider_id,
            max_internal_retry_delay_seconds=self._max_internal_retry_delay_seconds,
        )
        try:
            completed = run_with_heartbeat(
                self._runner,
                runner_is_injected=self._runner_is_injected,
                args=args,
                cwd=workdir,
                timeout=self._timeout_seconds,
                emitter=self._heartbeat,
                handoff=handoff,
                output_classifier=output_classifier,
                inactivity_timeout=self._inactivity_timeout_seconds,
                output_silence_timeout=self._output_silence_timeout_seconds,
                first_output_timeout=self._first_output_timeout_seconds,
                interactive_terminal=False,
            )
        except ManagedProcessTerminated as exc:
            self._last_availability, error = managed_process_failure(exc)
            raise error from exc
        except subprocess.TimeoutExpired as exc:
            self._last_availability = Availability.BUSY
            raise AgentExecutionError(
                f"Antigravity headless execution timed out after {self._timeout_seconds}s",
                classification="timeout",
            ) from exc

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if completed.returncode != 0:
            message = stderr.strip() or stdout.strip() or (
                f"Antigravity exited {completed.returncode}"
            )
            self._last_availability, error = provider_failure(
                self.adapter_name,
                message,
                artifact_payload={"stdout": stdout, "stderr": stderr},
            )
            raise error

        final_message = stdout.strip()
        if not final_message:
            diagnostic_text = stderr.strip()
            diagnosis = diagnose_failure(
                {"adapter": self.adapter_name, "message": diagnostic_text}
            )
            classification = (
                diagnosis.category
                if diagnostic_text and diagnosis.category != "unclassified"
                else "invalid_output"
            )
            self._last_availability = availability_for_failure(classification)
            prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            detail = (
                f"; stderr={diagnostic_text[:500]!r}" if diagnostic_text else ""
            )
            raise AgentExecutionError(
                "Antigravity CLI returned no stdout in headless print mode"
                f"{detail}; cwd={workdir}; prompt_bytes={len(prompt.encode('utf-8'))}; "
                f"prompt_sha256={prompt_digest}",
                classification=classification,
                retry_after_seconds=diagnosis.retry_after_seconds,
                persistent=diagnosis.persistent if classification != "invalid_output" else False,
                artifact_payload={
                    "stdout": stdout,
                    "stderr": stderr,
                    "cwd": str(workdir),
                    "prompt_bytes": len(prompt.encode("utf-8")),
                    "prompt_sha256": prompt_digest,
                    "command_flags": [
                        item for index, item in enumerate(args) if index not in {2}
                    ],
                },
            )

        self._last_availability = Availability.AVAILABLE
        return {
            "ok": True,
            "work_package_id": handoff.work_package_id,
            "final_message": final_message,
            "usage": {},
            "transport": {
                "format": "text",
                "stderr": stderr,
            },
        }

    def _build_command(self, prompt: str, *, effort: str = "") -> list[str]:
        """Build only flags supported by the current Antigravity CLI."""
        # AGY's print mode has its own five-minute default, independent from
        # the parent process timeout. Keep both layers aligned so legitimate
        # long-running fixes are not terminated early by the child CLI.
        args = [
            self._binary,
            "-p",
            prompt,
            "--print-timeout",
            f"{self._timeout_seconds}s",
        ]
        if self._model:
            args += ["--model", self._model]
        if effort:
            args += ["--effort", effort]
        if self._sandbox_enabled:
            args.append("--sandbox")
        if self._dangerously_skip_permissions:
            # ``StructuredHandoff.read_only`` describes the requested task, not
            # AGY's tool-approval policy.  Headless decomposition and review still
            # need read_file permission, and AGY cannot prompt for it.  Global
            # read-only project policy already disables this option at adapter
            # registration, so honour the configured flag for every normal stage.
            args.append("--dangerously-skip-permissions")
        return args

    def _validate_configuration(self) -> None:
        """Fail closed instead of forwarding unsupported policy-path flags."""
        if not self._policy_paths:
            return
        self._last_availability = Availability.DISABLED
        raise AgentExecutionError(
            "Antigravity CLI does not support per-invocation --policy paths; "
            "configure permissions/rules through Antigravity settings or the "
            "workspace and remove policy_paths from this provider",
            classification="configuration_error",
            persistent=True,
            artifact_payload={"policy_paths": list(self._policy_paths)},
        )
