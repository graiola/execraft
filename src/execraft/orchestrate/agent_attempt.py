"""Single-attempt agent execution extracted from provider progression policy."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from execraft.execution_identity import execution_identity_of
from execraft.runtime.contracts import RuntimeSessionRef

from .artifacts import AgentArtifactReference, AgentArtifactStore
from .contract_health import ContractHealth, ContractHealthStore, schema_sha256
from .invocations import AgentInvocationStore, AgentInvocationRecord
from .execution_health import (
    ExecutionHealth,
    ExecutionHealthStore,
    failure_health_dimension,
)
from .provider_health import ProviderHealth, ProviderHealthStore
from .runtime_continuation import (
    RuntimeSessionBindingStore,
    build_delta_handoff,
    context_epoch,
    continuation_role,
    previous_handoff,
)
from .runtime_dispatch import dispatch_runtime
from .scheduler import (
    AgentAdapter,
    AgentCapability,
    AgentExecutionError,
    StructuredHandoff,
    enforce_semantic_output_limit,
)
from .structured_output import (
    StructuredOutputError,
    normalize_structured_result,
)
from .supervisor import SupervisorDecision
from .usage import normalize_agent_usage


class AgentAttemptFinalizationError(RuntimeError):
    """Raised when an attempt finished durably but a projection hook failed.

    The invocation ledger is terminal once ``complete()``/``fail()`` returns.
    Failures while projecting that result into package state, the journal, or
    the progress stream are orchestration persistence failures, not provider
    failures. The runner surfaces them with this marker so the orchestrator
    façade can wrap them without poisoning provider health.
    """


@dataclass(frozen=True)
class AgentAttemptResult:
    """Outcome of a single agent execution attempt."""

    success: bool
    result: dict[str, Any] | None
    invocation: AgentInvocationRecord
    artifact: AgentArtifactReference | None
    duration_seconds: float
    classification: str = ""
    retry_after_seconds: float | None = None
    persistent: bool = False
    contract_health_result: str = ""  # "", "available", or "failure"
    raw_result: dict[str, Any] | None = None
    provider_health: ProviderHealth | None = None
    execution_health: ExecutionHealth | None = None
    contract_health: ContractHealth | None = None


class AgentAttemptRunner:
    """Execute one agent attempt with artifact persistence, validation, and health tracking.

    Optional lifecycle hooks let the orchestrator façade project durable state
    (package attempts, journal events, progress, rotation cursors) at the exact
    points where the inline implementation used to do so, without duplicating
    the attempt lifecycle itself.
    """

    def __init__(
        self,
        *,
        agent_invocations: AgentInvocationStore,
        agent_artifacts: AgentArtifactStore,
        contract_health: ContractHealthStore,
        provider_health: ProviderHealthStore,
        execution_health: ExecutionHealthStore | None = None,
        runtime_sessions: RuntimeSessionBindingStore | None = None,
        project_id: str,
        task_id: str,
        invocation_project_id: str,
        on_attempt_started: Callable[..., None] | None = None,
        on_result_persisted: Callable[..., None] | None = None,
        on_completed: Callable[..., None] | None = None,
        on_failed: Callable[..., None] | None = None,
    ):
        self._agent_invocations = agent_invocations
        self._agent_artifacts = agent_artifacts
        self._contract_health = contract_health
        self._provider_health = provider_health
        self._execution_health = execution_health
        self._runtime_sessions = runtime_sessions
        self._project_id = project_id
        self._task_id = task_id
        self._invocation_project_id = invocation_project_id
        self._on_attempt_started = on_attempt_started
        self._on_result_persisted = on_result_persisted
        self._on_completed = on_completed
        self._on_failed = on_failed

    @staticmethod
    def _invoke_hook(hook: Callable[..., None] | None, *args: Any) -> None:
        if hook is not None:
            hook(*args)

    def execute_attempt(
        self,
        *,
        adapter: AgentAdapter,
        handoff: StructuredHandoff,
        capability: AgentCapability,
        package_id: str,
        stage: str,
        shard_key: str,
        attempt_number: int,
        parent_invocation_id: str,
        triggering_event_id: str,
        workspace_before_digest: str,
        workspace_after_digest_fn,
        rendered_prompt: str,
        metadata: dict[str, Any],
        strict_checks: bool,
        require_structured_output: bool,
    ) -> AgentAttemptResult:
        """Run one agent attempt, persist artifacts, validate output, update health."""
        identity = execution_identity_of(adapter)
        candidate_id = identity.candidate_id
        legacy_provider_id = identity.provider_id
        started = time.monotonic()

        session_role = continuation_role(capability)
        epoch = ""
        continuation_session: RuntimeSessionRef | None = None
        continuation_handoff: StructuredHandoff | None = None
        binding = None
        runtime_capabilities = getattr(adapter, "execution_capabilities", None)
        supports_controlled_continuation = bool(
            self._runtime_sessions is not None
            and str(getattr(adapter, "adapter_name", "")) == "openclaw"
            and getattr(runtime_capabilities, "session_resume", False)
        )
        if supports_controlled_continuation:
            binding = self._runtime_sessions.get(package_id, session_role)
            format_repair = bool(handoff.execution_context.get("format_repair"))
            same_identity = bool(
                binding is not None and binding.identity_compatible(identity)
            )
            # A format-only repair deliberately strips normal requirements and
            # skills from its compact handoff. Keep the already validated epoch
            # for the same runtime candidate so repair can continue the turn.
            epoch = (
                binding.context_epoch
                if format_repair and same_identity and binding is not None
                else context_epoch(
                    handoff,
                    identity,
                    runtime_policy=metadata.get("execution_capabilities")
                    if isinstance(metadata.get("execution_capabilities"), Mapping)
                    else None,
                )
            )
            if binding is not None and not binding.compatible(identity, epoch):
                self._runtime_sessions.invalidate(package_id, session_role)
                binding = None
            if binding is not None:
                prior_handoff = previous_handoff(self._agent_invocations, binding)
                if prior_handoff is None:
                    self._runtime_sessions.invalidate(package_id, session_role)
                    binding = None
                else:
                    continuation_session = binding.session_ref
                    continuation_handoff = build_delta_handoff(
                        handoff,
                        prior_handoff,
                        epoch=epoch,
                        prior_invocation_id=binding.last_invocation_id,
                        previous_workspace_digest=binding.last_workspace_digest,
                        current_workspace_digest=workspace_before_digest,
                    )

        dispatch_metadata = dict(metadata)
        if binding is not None:
            dispatch_metadata["runtime_continuation_binding"] = {
                "reuse_count": binding.reuse_count,
                "updated_at": binding.updated_at,
                "guarded_compaction_count": binding.guarded_compaction_count,
            }

        invocation = self._agent_invocations.begin(
            project_id=self._invocation_project_id,
            task_id=self._task_id,
            package_id=package_id,
            stage=stage,
            capability=capability.value,
            attempt=attempt_number,
            agent_id=legacy_provider_id,
            adapter=metadata["adapter"],
            model=metadata["model"],
            execution_identity=identity,
            parent_invocation_id=parent_invocation_id,
            triggering_event_id=triggering_event_id,
            handoff=handoff.as_mapping(),
            skills=handoff.skill_manifest,
            isolation=metadata["execution_capabilities"],
            workspace_before_digest=workspace_before_digest,
        )
        self._invoke_hook(
            self._on_attempt_started, invocation, handoff, metadata
        )

        artifact = None
        invocation_finalized = False
        result: dict[str, Any] | None = None
        runtime_session: RuntimeSessionRef | None = None
        runtime_metadata: dict[str, Any] = {}
        actual_prompt = rendered_prompt

        try:
            dispatch = dispatch_runtime(
                adapter, handoff, capability=capability.value, package_id=package_id,
                stage=stage, attempt=attempt_number, metadata=dispatch_metadata,
                session_ref=continuation_session, context_epoch=epoch,
                continuation_handoff=continuation_handoff,
            )
            result = dispatch.output
            runtime_session = dispatch.session_ref
            runtime_metadata = dict(dispatch.runtime_metadata)
            actual_prompt = dispatch.rendered_prompt or rendered_prompt

            artifact = self._agent_artifacts.persist(
                project_id=self._project_id,
                package_id=package_id,
                stage=stage,
                capability=capability.value,
                agent_id=candidate_id,
                result=result,
            )
            result = dict(result)
            result["_execraft_agent_artifact"] = artifact.as_mapping()
            self._invoke_hook(
                self._on_result_persisted, artifact, metadata, candidate_id
            )

            contract_schema_hash = (
                schema_sha256(handoff.expected_output_schema)
                if handoff.expected_output_schema
                else ""
            )

            if (
                strict_checks
                and require_structured_output
                and handoff.expected_output_schema
            ):
                try:
                    structured_payload = normalize_structured_result(
                        result,
                        handoff.expected_output_schema,
                    )
                except StructuredOutputError as exc:
                    raise RuntimeError(
                        f"invalid structured output: {exc}; artifact={artifact.path}"
                    ) from exc
                result["_execraft_structured_payload"] = structured_payload
                if capability == AgentCapability.SUPERVISE:
                    try:
                        SupervisorDecision.from_mapping(structured_payload)
                    except ValueError as exc:
                        raise RuntimeError(
                            f"invalid structured output: {exc}; "
                            f"artifact={artifact.path}"
                        ) from exc

            normalized_result = result.get("_execraft_structured_payload")
            if not isinstance(normalized_result, dict):
                normalized_result = {
                    key: value
                    for key, value in result.items()
                    if not str(key).startswith("_execraft_")
                }
            output_payload: object = normalized_result
            if not handoff.expected_output_schema:
                output_payload = result.get("final_message", normalized_result)
            result["_execraft_output_estimated_tokens"] = enforce_semantic_output_limit(
                output_payload,
                handoff.output_token_hard_limit,
            )

            duration = time.monotonic() - started
            usage = normalize_agent_usage(
                result,
                prompt=actual_prompt,
                provider=identity.model_provider or metadata["adapter"] or candidate_id,
                model=identity.model or metadata["model"],
                capability=capability.value,
                package_id=package_id,
                shard=shard_key,
                stage=stage,
                attempt=attempt_number,
                invocation_id=invocation.invocation_id,
                workflow_skills=handoff.workflow_skills,
            ).as_mapping()

            workspace_after_digest = workspace_after_digest_fn()
            completed_invocation = self._agent_invocations.complete(
                invocation.invocation_id,
                duration_seconds=duration,
                workspace_after_digest=workspace_after_digest,
                result_artifact=artifact.as_mapping(),
                normalized_result=normalized_result,
                usage=usage,
                runtime_session=runtime_session,
                runtime_metadata=runtime_metadata,
            )
            if supports_controlled_continuation and runtime_session is not None:
                self._runtime_sessions.upsert(
                    package_id=package_id, role=session_role, identity=identity, epoch=epoch,
                    session_ref=runtime_session, last_invocation_id=invocation.invocation_id,
                    last_workspace_digest=workspace_after_digest,
                    reused=bool(runtime_metadata.get("session_reused", False)),
                    guarded_compaction_count=_runtime_guarded_compaction_count(
                        runtime_metadata
                    ),
                )
            # From this point forward the durable invocation ledger is
            # terminal. Failures while projecting that result into package
            # state, journal, or progress stream are orchestration persistence
            # failures, not provider failures.
            invocation_finalized = True

            contract_health: ContractHealth | None = None
            contract_health_result = ""
            if contract_schema_hash:
                contract_health = self._contract_health.mark_available(
                    candidate_id,
                    metadata["model"],
                    capability.value,
                    contract_schema_hash,
                )
                contract_health_result = "available"
            self._provider_health.mark_available(legacy_provider_id)
            provider_health = self._provider_health.get(legacy_provider_id)
            if self._execution_health is not None:
                self._execution_health.mark_identity_available(identity)

            result["_execraft_invocation_id"] = invocation.invocation_id
            result["_execraft_executed_by"] = candidate_id
            result["_execraft_execution_identity"] = identity.as_mapping()
            if runtime_session is not None:
                result["_execraft_runtime_session"] = runtime_session.as_mapping()

            self._invoke_hook(
                self._on_completed,
                completed_invocation,
                artifact,
                result,
                metadata,
            )

            return AgentAttemptResult(
                success=True,
                result=result,
                invocation=completed_invocation,
                artifact=artifact,
                duration_seconds=duration,
                contract_health_result=contract_health_result,
                raw_result=result,
                provider_health=provider_health,
                contract_health=contract_health,
            )

        except Exception as exc:
            if invocation_finalized:
                raise AgentAttemptFinalizationError(
                    "agent invocation completed but orchestration result "
                    f"projection failed for {invocation.invocation_id}: {exc}"
                ) from exc

            if isinstance(exc, AgentExecutionError):
                classification = exc.classification
                retry_after_seconds = exc.retry_after_seconds
                persistent = exc.persistent
                artifact_payload = exc.artifact_payload
                health_dimension_hint = exc.health_dimension
            else:
                message = str(exc)
                from .scheduler import classify_failure

                classification = (
                    "invalid_output"
                    if message.startswith("invalid structured output:")
                    else classify_failure({"message": message})
                )
                retry_after_seconds = None
                persistent = False
                artifact_payload = {}
                health_dimension_hint = ""

            failure_artifact = None
            if artifact_payload:
                failure_artifact = self._agent_artifacts.persist(
                    project_id=self._project_id,
                    package_id=package_id,
                    stage=stage,
                    capability=capability.value,
                    agent_id=candidate_id,
                    result={
                        "ok": False,
                        "error": str(exc),
                        "classification": classification,
                        "retry_after_seconds": retry_after_seconds,
                        "transport": artifact_payload,
                    },
                )
                self._invoke_hook(
                    self._on_result_persisted, failure_artifact, metadata, candidate_id
                )

            stored_failure_artifact = failure_artifact or artifact
            duration = time.monotonic() - started
            failure_mapping = {
                "error": str(exc),
                "classification": classification,
                "retry_after_seconds": retry_after_seconds,
                "persistent": persistent,
            }
            if health_dimension_hint:
                failure_mapping["health_dimension"] = health_dimension_hint
            usage_source: Mapping[str, Any] | None = result
            if not usage_source and isinstance(artifact_payload, Mapping):
                usage_source = artifact_payload
            usage = normalize_agent_usage(
                usage_source,
                prompt=actual_prompt,
                provider=identity.model_provider or metadata["adapter"] or candidate_id,
                model=identity.model or metadata["model"],
                capability=capability.value,
                package_id=package_id,
                shard=shard_key,
                stage=stage,
                attempt=attempt_number,
                invocation_id=invocation.invocation_id,
                workflow_skills=handoff.workflow_skills,
            ).as_mapping()

            workspace_after_digest = workspace_after_digest_fn()
            failed_invocation = self._agent_invocations.fail(
                invocation.invocation_id,
                duration_seconds=duration,
                failure=failure_mapping,
                workspace_after_digest=workspace_after_digest,
                result_artifact=(
                    stored_failure_artifact.as_mapping()
                    if stored_failure_artifact is not None
                    else {}
                ),
                usage=usage,
                runtime_session=runtime_session,
                runtime_metadata=runtime_metadata,
            )
            if supports_controlled_continuation:
                if runtime_session is not None:
                    # The runtime turn reached a terminal success and returned a
                    # concrete session even if Execraft later rejected its output
                    # contract/budget. Persist the exact handoff as the new
                    # continuation anchor so runtime history and durable state
                    # remain aligned for repair/retry.
                    self._runtime_sessions.upsert(
                        package_id=package_id, role=session_role, identity=identity, epoch=epoch,
                        session_ref=runtime_session, last_invocation_id=invocation.invocation_id,
                        last_workspace_digest=workspace_after_digest,
                        reused=bool(runtime_metadata.get("session_reused", False)),
                        guarded_compaction_count=_runtime_guarded_compaction_count(
                            runtime_metadata
                        ),
                    )
                else:
                    # Unknown/failed turns are not safe continuation anchors. A
                    # later attempt reconstructs from the complete Execraft handoff.
                    self._runtime_sessions.invalidate(package_id, session_role)

            contract_schema_hash = (
                schema_sha256(handoff.expected_output_schema)
                if handoff.expected_output_schema
                else ""
            )
            contract_health: ContractHealth | None = None
            contract_health_result = ""
            if classification == "invalid_output" and contract_schema_hash:
                contract_health = self._contract_health.mark_failure(
                    candidate_id,
                    metadata["model"],
                    capability.value,
                    contract_schema_hash,
                    detail=str(exc),
                )
                contract_health_result = "failure"

            execution_health: ExecutionHealth | None = None
            health_dimension = failure_health_dimension(
                classification, identity, dimension_hint=health_dimension_hint
            )
            if self._execution_health is not None and health_dimension is not None:
                dimension, dimension_id = health_dimension
                execution_health = self._execution_health.mark_failure(
                    dimension,
                    dimension_id,
                    reason=classification,
                    detail=str(exc),
                    retry_after_seconds=retry_after_seconds,
                    persistent=persistent,
                )

            # Legacy provider health remains a compatibility projection. A
            # physical target failure is deliberately *not* copied into it:
            # other routes/candidates using the same account may still work.
            if health_dimension is not None and health_dimension[0] == "target":
                provider_health = self._provider_health.get(legacy_provider_id)
            else:
                provider_health = self._provider_health.mark_failure(
                    legacy_provider_id,
                    reason=classification,
                    detail=str(exc),
                    retry_after_seconds=retry_after_seconds,
                    persistent=persistent,
                )

            self._invoke_hook(
                self._on_failed,
                failed_invocation,
                stored_failure_artifact,
                failure_mapping,
                metadata,
                provider_health,
                result,
                contract_health,
            )

            return AgentAttemptResult(
                success=False,
                result=None,
                invocation=failed_invocation,
                artifact=stored_failure_artifact,
                duration_seconds=duration,
                classification=classification,
                retry_after_seconds=retry_after_seconds,
                persistent=persistent,
                contract_health_result=contract_health_result,
                raw_result=result,
                provider_health=provider_health,
                execution_health=execution_health,
                contract_health=contract_health,
            )


def _runtime_guarded_compaction_count(metadata: Mapping[str, Any]) -> int | None:
    """Return the highest compaction boundary explicitly guarded this turn."""

    value = metadata.get("guarded_compaction_count")
    if isinstance(value, bool) or value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None
