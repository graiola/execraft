"""Focused tests for typed package-stage dispatch."""

from dataclasses import dataclass
from types import SimpleNamespace

from execraft.orchestrate.models import WorkPackage, WorkPackageKind, WorkPackageStage
from execraft.repository_sync.spec import RepositorySyncSpec, RepositorySyncTarget
from execraft.orchestrate.package_stage import (
    FinalReviewStageHandler,
    ImplementStageHandler,
    PackageStageEngine,
    StageDisposition,
)


@dataclass
class _ReviewSchedule:
    implementer_id: str
    reviewer_id: str
    final_reviewer_id: str


class _Host:
    def _should_auto_decompose(self, package: WorkPackage) -> bool:
        return False


@dataclass
class _RecordingHandler:
    stage: WorkPackageStage
    next_stage: WorkPackageStage
    calls: list[WorkPackageStage]
    disposition: StageDisposition = StageDisposition.CONTINUE

    def handle(self, host: _Host, package: WorkPackage) -> StageDisposition:
        self.calls.append(self.stage)
        package.stage = self.next_stage
        return self.disposition


def test_engine_dispatches_typed_handlers_from_persisted_stage() -> None:
    calls: list[WorkPackageStage] = []
    engine = PackageStageEngine(
        _Host(),
        handlers=(
            _RecordingHandler(WorkPackageStage.FAST_VERIFY, WorkPackageStage.REVIEW, calls),
            _RecordingHandler(WorkPackageStage.REVIEW, WorkPackageStage.READY_TO_COMMIT, calls),
        ),
    )
    package = WorkPackage(
        id="WP05", title="resume", stage=WorkPackageStage.FAST_VERIFY
    )

    engine.process(package)

    assert calls == [WorkPackageStage.FAST_VERIFY, WorkPackageStage.REVIEW]
    assert package.stage == WorkPackageStage.READY_TO_COMMIT


def test_engine_stops_when_handler_requests_lifecycle_pause() -> None:
    calls: list[WorkPackageStage] = []
    engine = PackageStageEngine(
        _Host(),
        handlers=(
            _RecordingHandler(
                WorkPackageStage.REVIEW,
                WorkPackageStage.FIX_REVIEW,
                calls,
                disposition=StageDisposition.STOP,
            ),
            _RecordingHandler(
                WorkPackageStage.FIX_REVIEW,
                WorkPackageStage.REGRESSION_VERIFY,
                calls,
            ),
        ),
    )
    package = WorkPackage(id="WP05", title="pause", stage=WorkPackageStage.REVIEW)

    engine.process(package)

    assert calls == [WorkPackageStage.REVIEW]
    assert package.stage == WorkPackageStage.FIX_REVIEW


def test_default_engine_exposes_supported_lifecycle_stages() -> None:
    handled = PackageStageEngine(_Host()).handled_stages

    assert handled == {
        WorkPackageStage.DECOMPOSE,
        WorkPackageStage.PREPARE,
        WorkPackageStage.IMPLEMENT,
        WorkPackageStage.FAST_VERIFY,
        WorkPackageStage.REVIEW,
        WorkPackageStage.FIX_REVIEW,
        WorkPackageStage.REGRESSION_VERIFY,
        WorkPackageStage.FINAL_REVIEW,
        WorkPackageStage.READY_TO_COMMIT,
    }


def test_duplicate_stage_handlers_are_rejected() -> None:
    calls: list[WorkPackageStage] = []
    duplicate = _RecordingHandler(
        WorkPackageStage.REVIEW, WorkPackageStage.FINAL_REVIEW, calls
    )

    try:
        PackageStageEngine(_Host(), handlers=(duplicate, duplicate))
    except ValueError as exc:
        assert str(exc) == "duplicate package-stage handler"
    else:  # pragma: no cover - assertion helper
        raise AssertionError("duplicate stage handler was accepted")


class _FinalReviewHost:
    """Minimal host capturing the independence policy of a final review."""

    def __init__(self, schedule, *, allow_same_provider_review: bool = True):
        self._schedule = schedule
        self.config = SimpleNamespace(
            allow_same_provider_review=allow_same_provider_review
        )
        self.call_kwargs: dict = {}

    def _ensure_review_assignments(self, package: WorkPackage):
        return self._schedule

    def _package_working_directory(self, package: WorkPackage, *, write_capable: bool):
        assert write_capable is False
        return None

    def _call_agent(self, capability, agent_id, package, **kwargs):
        self.call_kwargs = kwargs
        return {"ok": True}

    def _review_result(self, package: WorkPackage, result):
        return "approved", []

    def _advance_package_stage(self, package: WorkPackage, stage) -> None:
        package.stage = stage


def test_final_review_relaxes_through_same_provider_as_last_resort() -> None:
    schedule = _ReviewSchedule(
        implementer_id="codex", reviewer_id="antigravity", final_reviewer_id=""
    )
    host = _FinalReviewHost(schedule)
    package = WorkPackage(
        id="WP20", title="ordinary", stage=WorkPackageStage.FINAL_REVIEW
    )
    package.last_fixer_id = "claude-code"

    FinalReviewStageHandler().handle(host, package)

    assert host.call_kwargs["excluded_agent_ids"] == {
        "codex",
        "antigravity",
        "claude-code",
    }
    tiers = host.call_kwargs["fallback_exclusion_tiers"]
    assert [policy for policy, _ in tiers] == [
        "reuse_primary_reviewer_for_final_review",
        "reuse_fixer_for_final_review",
        "reuse_provider_for_final_review",
    ]
    assert tiers[-1][1] == set()


def test_repository_sync_keeps_hard_independent_final_review() -> None:
    schedule = _ReviewSchedule(
        implementer_id="codex", reviewer_id="antigravity", final_reviewer_id="reviewer-2"
    )
    host = _FinalReviewHost(schedule)
    package = WorkPackage(
        id="WP20-SYNC",
        title="sync",
        stage=WorkPackageStage.FINAL_REVIEW,
        kind=WorkPackageKind.REPOSITORY_SYNC,
        affected_repositories=["backend"],
        repository_sync=RepositorySyncSpec(
            targets=(RepositorySyncTarget("backend"),),
            require_independent_review=True,
        ),
    )

    FinalReviewStageHandler().handle(host, package)

    assert host.call_kwargs["fallback_exclusion_tiers"] == []


class _PartialImplementationHost:
    """Host that simulates provider recovery after a partial persisted schedule."""

    def __init__(self) -> None:
        self.config = SimpleNamespace()
        self._journal = SimpleNamespace(append=lambda *args, **kwargs: None)
        self.schedule_calls = 0
        self.called_agent_id = ""

    def _schedule_agents(self, package: WorkPackage):
        self.schedule_calls += 1
        return _ReviewSchedule(
            implementer_id="codex",
            reviewer_id="claude-code",
            final_reviewer_id="antigravity",
        )

    def save_state(self, *, reason: str = "state_update") -> None:
        return None

    def _emit_progress(self, event_type: str, **payload) -> None:
        return None

    def _package_working_directory(self, package: WorkPackage, *, write_capable: bool):
        assert write_capable is True
        return None

    def _call_agent(self, capability, agent_id, package, **kwargs):
        self.called_agent_id = agent_id
        return {
            "ok": True,
            "status": "implemented",
            "summary": "done",
            "acceptance_evidence": [],
        }

    def _apply_implementation_result(self, package: WorkPackage, result) -> None:
        return None

    def _advance_package_stage(self, package: WorkPackage, stage) -> None:
        package.stage = stage


def test_implementation_reschedules_when_only_review_assignments_are_persisted() -> None:
    """Recovered implement providers must not be hidden by stale reviewer IDs."""

    host = _PartialImplementationHost()
    package = WorkPackage(
        id="WP24",
        title="recovered provider",
        stage=WorkPackageStage.IMPLEMENT,
    )
    # This is the broken persisted state that caused waiting_for_agent to loop:
    # implementation selection previously failed, while review selection succeeded.
    package.agent_id = ""
    package.reviewer_id = "claude-code"
    package.final_reviewer_id = "antigravity"

    ImplementStageHandler().handle(host, package)

    assert host.schedule_calls == 1
    assert package.agent_id == "codex"
    assert host.called_agent_id == "codex"
    assert package.stage == WorkPackageStage.FAST_VERIFY
