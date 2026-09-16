"""Prove runtime substitution with one shared runtime contract suite.

The PLAN's Definition of Done requires that "adding a test runtime does not
require modifying scheduler/orchestrator internals beyond registration and
configuration", and that every runtime passes the same contract suite.

These tests are the executable proof. The reference runtime below is a complete
third runtime kind: it is registered through ``execraft.runtime.registry``,
configured through ordinary schema-v4 ``agents.yaml`` data, and built by the
real ``build_runtime_candidates``. Nothing in the scheduler, the orchestrator or
the candidate factory is touched to make it work.

The shared suite covers the runtime-neutral protocol surface. Execution parity
against a live model or Gateway is deliberately not asserted here -- that needs
credentials and a running Gateway and belongs to the opt-in live suites -- so
this file proves substitutability, not end-to-end behavioural equivalence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from execraft.agents.config import parse_execution_config
from execraft.agents.config_errors import AgentConfigError
from execraft.agents.runtime_candidates import build_runtime_candidates
from execraft.execution_identity import ExecutionIdentity
from execraft.orchestrate.scheduler import AgentCapability, Availability
from execraft.runtime.contracts import (
    RuntimeCapabilities,
    RuntimeExecutionRequest,
    RuntimeExecutionResult,
    RuntimeSessionRef,
)
from execraft.runtime.registry import (
    RuntimeBuildContext,
    RuntimeRegistrationError,
    is_registered_runtime_kind,
    register_runtime_builder,
    registered_runtime_kinds,
    runtime_builder,
    unregister_runtime_builder,
)

REFERENCE_KIND = "reference"


class ReferenceAgentRuntime:
    """Minimal complete ``AgentRuntime`` used to prove the substitution seam."""

    def __init__(self, profile: Any, runtime: Any) -> None:
        self._profile = profile
        self._runtime = runtime
        self._identity = ExecutionIdentity(
            candidate_id=profile.candidate_id,
            runtime_id=runtime.id,
            runtime_backend=REFERENCE_KIND,
            concurrency_group=profile.concurrency_group or profile.candidate_id,
        )
        self.availability = Availability.AVAILABLE
        self.cancelled: list[str] = []

    @property
    def provider_id(self) -> str:
        return self._identity.candidate_id

    @property
    def runtime_id(self) -> str:
        return self._runtime.id

    @property
    def candidate_id(self) -> str:
        return self._identity.candidate_id

    @property
    def capabilities(self) -> set[AgentCapability]:
        return set(self._profile.capabilities)

    @property
    def execution_capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(read_only_enforcement="hard", workspace_write=False)

    @property
    def execution_identity(self) -> ExecutionIdentity:
        return self._identity

    def execute_runtime(
        self, request: RuntimeExecutionRequest
    ) -> RuntimeExecutionResult:
        return RuntimeExecutionResult(
            output={
                "ok": True,
                "work_package_id": request.package_id
                or request.handoff.work_package_id,
                "final_message": "reference runtime completed",
            },
            identity=self._identity,
            session_ref=RuntimeSessionRef(
                runtime_id=self.runtime_id,
                candidate_id=self.candidate_id,
                session_id=f"reference-{request.package_id or 'none'}",
                backend=REFERENCE_KIND,
                context_epoch=request.context_epoch,
            ),
            runtime_metadata={"backend": REFERENCE_KIND},
        )

    def execute(self, handoff: Any) -> dict[str, Any]:
        return {"ok": True, "work_package_id": handoff.work_package_id}

    def session_ref_from_result(
        self, result: Mapping[str, Any] | None
    ) -> RuntimeSessionRef | None:
        if not result:
            return None
        return RuntimeSessionRef(
            runtime_id=self.runtime_id,
            candidate_id=self.candidate_id,
            session_id=str(result.get("session_id", "")),
            backend=REFERENCE_KIND,
        )

    def cancel(self, execution_id: str) -> bool:
        self.cancelled.append(execution_id)
        return True


def _build_reference_candidate(ctx: RuntimeBuildContext) -> Any:
    return ReferenceAgentRuntime(ctx.profile, ctx.runtime)


@pytest.fixture
def reference_runtime_registered():
    register_runtime_builder(REFERENCE_KIND, _build_reference_candidate, replace=True)
    try:
        yield
    finally:
        unregister_runtime_builder(REFERENCE_KIND)


def _config(kind: str = REFERENCE_KIND) -> dict[str, Any]:
    return {
        "schema_version": 4,
        "runtimes": {"ext": {"kind": kind}},
        "execution_targets": {},
        "model_routes": {},
        "agents": {
            "ext-impl": {
                "runtime": "ext",
                "capabilities": ["implement"],
                "priority": 10,
            }
        },
    }


# --------------------------------------------------------------------------
# Substitution: registration + configuration only
# --------------------------------------------------------------------------


def test_third_runtime_is_configurable_and_built_without_core_edits(
    reference_runtime_registered, tmp_path: Path
) -> None:
    execution = parse_execution_config(_config())

    candidates = build_runtime_candidates(
        execution,
        workdir=tmp_path / "work",
        state_root=tmp_path / "state",
        read_only=False,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert isinstance(candidate, ReferenceAgentRuntime)
    assert candidate.runtime_id == "ext"
    assert candidate.execution_identity.runtime_backend == REFERENCE_KIND


def test_unregistered_runtime_kind_is_rejected_by_configuration() -> None:
    assert not is_registered_runtime_kind("nope")
    with pytest.raises(AgentConfigError, match="unsupported runtime kind"):
        parse_execution_config(_config("nope"))


def test_extension_runtime_may_not_borrow_native_or_openclaw_fields(
    reference_runtime_registered,
) -> None:
    native_fields = _config()
    native_fields["runtimes"]["ext"]["adapter"] = "codex"
    with pytest.raises(AgentConfigError, match="native adapter/binary"):
        parse_execution_config(native_fields)

    gateway_fields = _config()
    gateway_fields["runtimes"]["ext"]["gateway"] = "ws://127.0.0.1:18789"
    with pytest.raises(AgentConfigError, match="cannot declare OpenClaw fields"):
        parse_execution_config(gateway_fields)


def test_registry_rejects_duplicate_and_unknown_registration() -> None:
    register_runtime_builder(REFERENCE_KIND, _build_reference_candidate, replace=True)
    try:
        with pytest.raises(RuntimeRegistrationError, match="already registered"):
            register_runtime_builder(REFERENCE_KIND, _build_reference_candidate)
        with pytest.raises(RuntimeRegistrationError, match="must be callable"):
            register_runtime_builder("bad", object(), replace=True)  # type: ignore[arg-type]
    finally:
        unregister_runtime_builder(REFERENCE_KIND)
        unregister_runtime_builder("bad")

    with pytest.raises(RuntimeRegistrationError, match="unsupported runtime kind"):
        runtime_builder("definitely-not-registered")


def test_shipped_runtimes_are_registered() -> None:
    kinds = registered_runtime_kinds()
    assert "native" in kinds
    assert "openclaw" in kinds


# --------------------------------------------------------------------------
# Shared runtime contract suite
# --------------------------------------------------------------------------


def _native_candidate(tmp_path: Path) -> Any:
    execution = parse_execution_config(
        {
            "schema_version": 4,
            "runtimes": {"native": {"kind": "native", "adapter": "codex"}},
            "execution_targets": {},
            "model_routes": {},
            "agents": {
                "impl": {
                    "runtime": "native",
                    "capabilities": ["implement"],
                }
            },
        }
    )
    return build_runtime_candidates(
        execution,
        workdir=tmp_path / "work",
        state_root=tmp_path / "state",
        read_only=False,
    )[0]


def _reference_candidate(tmp_path: Path) -> Any:
    return build_runtime_candidates(
        parse_execution_config(_config()),
        workdir=tmp_path / "work",
        state_root=tmp_path / "state",
        read_only=False,
    )[0]


@pytest.mark.parametrize("factory_name", ["native", "reference"])
def test_runtime_contract_surface(
    factory_name: str, reference_runtime_registered, tmp_path: Path
) -> None:
    """Every runtime exposes the same neutral protocol surface.

    Deliberately scoped to the substitution contract. Behavioural equivalence
    under a real model/Gateway is covered by the per-runtime and opt-in live
    suites, not asserted here.
    """

    candidate = (
        _native_candidate(tmp_path)
        if factory_name == "native"
        else _reference_candidate(tmp_path)
    )

    assert isinstance(candidate.runtime_id, str) and candidate.runtime_id
    assert isinstance(candidate.candidate_id, str) and candidate.candidate_id

    capabilities = candidate.execution_capabilities
    assert isinstance(capabilities, RuntimeCapabilities)
    assert capabilities.read_only_enforcement in {"advisory", "provider_policy", "hard"}

    identity = candidate.execution_identity
    assert isinstance(identity, ExecutionIdentity)
    assert identity.runtime_id == candidate.runtime_id

    # A runtime must tolerate being asked about an execution it never ran.
    assert isinstance(candidate.cancel("execution-that-does-not-exist"), bool)
    assert candidate.session_ref_from_result(None) is None
