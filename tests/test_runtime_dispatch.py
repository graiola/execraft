from types import SimpleNamespace

from execraft.execution_identity import ExecutionIdentity
from execraft.orchestrate.runtime_dispatch import dispatch_parallel_shard_candidate, dispatch_runtime
from execraft.orchestrate.scheduler import StructuredHandoff
from execraft.runtime.contracts import RuntimeExecutionResult, RuntimeSessionRef


class _RuntimeCandidate:
    provider_id = "legacy-provider"
    candidate_id = "implementer-qwen"
    execution_identity = ExecutionIdentity(
        candidate_id="implementer-qwen",
        runtime_id="native",
        runtime_backend="opencode",
        model_route_id="qwen-satellite",
        model_provider="ollama",
        model="qwen3-coder",
        target_id="satellite-gpu",
        target_kind="inference_endpoint",
        concurrency_group="satellite-gpu",
        legacy_provider_id="legacy-provider",
    )

    def __init__(self):
        self.request = None

    def execute_runtime(self, request):
        self.request = request
        return RuntimeExecutionResult(
            output={"ok": True, "final_message": "done"},
            identity=request.identity,
            session_ref=RuntimeSessionRef(
                runtime_id="native",
                session_id="session-1",
                candidate_id=request.identity.candidate_id,
            ),
        )


def test_dispatch_runtime_uses_normalized_request_and_preserves_session():
    sample_handoff = StructuredHandoff(work_package_id="WP4", stage="implementation", summary="test")
    candidate = _RuntimeCandidate()
    result, session = dispatch_runtime(
        candidate,
        sample_handoff,
        capability="implementation",
        package_id="WP4",
        stage="implementation",
        attempt=2,
        metadata={"adapter": "opencode"},
    )
    assert result["final_message"] == "done"
    assert candidate.request.identity.target_id == "satellite-gpu"
    assert candidate.request.attempt == 2
    assert session is not None and session.session_id == "session-1"


def test_parallel_dispatch_uses_same_runtime_contract():
    sample_handoff = StructuredHandoff(work_package_id="WP4", stage="implementation", summary="test")
    candidate = _RuntimeCandidate()
    shard = SimpleNamespace(
        handoff=sample_handoff,
        capability=SimpleNamespace(value="implementation"),
        package=SimpleNamespace(id="WP4", stage=SimpleNamespace(value="implementation")),
    )
    result, error, duration, session = dispatch_parallel_shard_candidate(candidate, shard)
    assert error is None
    assert result and result["ok"] is True
    assert duration >= 0
    assert candidate.request.package_id == "WP4"
    assert session is not None and session.session_id == "session-1"
