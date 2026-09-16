"""Regression tests for package-scoped verification baseline authorization."""

from dataclasses import dataclass

from execraft.orchestrate.models import PlanGraph, TaskExecutionState, WorkPackage, WorkPackageStage
from execraft.orchestrate.orchestrator import OrchestrationConfig, ProjectOrchestrator
from execraft.orchestrate.verification import CHEAP, VerificationCommand, VerificationRegistry
from execraft.orchestrate.supervisor import IncidentClass, IncidentStatus, SupervisorIncident
from execraft.orchestrate.verification_baseline import (
    build_baseline_acceptance,
    extract_failure_fingerprints,
    is_explicit_baseline_acceptance,
    legacy_operator_baseline_authorization,
)


BASE_FAILURES = """
build/test_results/test_mission_client_status_stream.gtest.xml: 1 test, 0 errors, 1 failure, 0 skipped
- sample_plugins.MissionClientStatusStream DeliversBothChannelsAndDropsOldGenerationAfterReconnect
  <<< failure message
    unknown file
    C++ exception with description "Invalid topic name: topic name token must not start with a number:
      '/stage5/status/18729847703761'"
  >>>
build/test_results/test_mission_compose_controller.gtest.xml: 32 tests, 0 errors, 2 failures, 0 skipped
- sample_plugins.MissionComposeController ArmThenTakeoffRemainDistinctAndDraftSelectionIsAtomic
  <<< failure message
    /workspace/sample_plugins/test/test_mission_compose_controller.cpp:1344
    Value of: controller.confirmPendingIntent()
      Actual: false
    Expected: true
  >>>
- sample_plugins.MissionComposeController SpatialRequestUsesPostInsertionDocumentGeneration
  <<< failure message
    /workspace/sample_plugins/test/test_mission_compose_controller.cpp:1430
    Expected speed_mps in parameter_keys
  >>>
"""


class _Completed:
    def __init__(self, returncode: int, stdout: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class _Runner:
    def __init__(self, output: str):
        self.output = output
        self.calls = 0

    def __call__(self, command: str, *, cwd, timeout):
        self.calls += 1
        return _Completed(1, self.output)


@dataclass
class _JournalEntry:
    event_type: str
    payload: dict
    timestamp: str = "2026-08-24T10:00:00+00:00"


def _baseline_for(output: str, command: str = "run-ui-tests") -> dict:
    failures = [item.as_mapping() for item in extract_failure_fingerprints(output)]
    return build_baseline_acceptance(
        package_id="WP21-SYNC",
        profile="cheap",
        failed_commands=[
            {
                "repository_id": "frontend",
                "command": command,
                "status": "failed",
                "failures": failures,
                "relevant_excerpt": output,
            }
        ],
        accepted_by="operator",
        accepted_at="2026-08-24T10:00:00+00:00",
    )


def test_fingerprint_normalizes_generated_numeric_topic_token():
    first = extract_failure_fingerprints(BASE_FAILURES)
    second = extract_failure_fingerprints(
        BASE_FAILURES.replace("18729847703761", "98765432101234")
    )
    assert [(item.identifier, item.signature) for item in first] == [
        (item.identifier, item.signature) for item in second
    ]
    assert len(first) == 3


def test_explicit_baseline_option_requires_acceptance_and_baseline_language():
    question = {"question": "How should verification proceed?"}
    accepted = {
        "id": "accept_baseline",
        "label": "Re-affirm the time-bounded known-baseline acceptance",
        "consequence": "Continue this package only.",
    }
    retry = {"id": "retry", "label": "Retry verification", "consequence": "Run again"}
    assert is_explicit_baseline_acceptance(question, accepted)
    assert not is_explicit_baseline_acceptance(question, retry)


def test_legacy_operator_authorization_requires_every_current_failure_to_be_named():
    request = _JournalEntry(
        "supervisor_human_decision_requested",
        {
            "incident_id": "inc-1",
            "package_id": "WP21-SYNC",
            "question": (
                "Accept MissionClientStatusStream DeliversBothChannelsAndDropsOldGenerationAfterReconnect, "
                "MissionComposeController ArmThenTakeoffRemainDistinctAndDraftSelectionIsAtomic, and "
                "MissionComposeController SpatialRequestUsesPostInsertionDocumentGeneration as baseline?"
            ),
            "context": "These are pre-existing baseline defects.",
            "options": [
                {
                    "id": "accept_baseline",
                    "label": "Accept the known baseline",
                    "consequence": "Continue the package.",
                }
            ],
        },
    )
    answer = _JournalEntry(
        "supervisor_human_decision_received",
        {
            "incident_id": "inc-1",
            "package_id": "WP21-SYNC",
            "option_id": "accept_baseline",
            "message": "Proceed with the exact fingerprint.",
        },
    )
    failures = extract_failure_fingerprints(BASE_FAILURES)
    authorization = legacy_operator_baseline_authorization(
        [request, answer], package_id="WP21-SYNC", current_failures=failures
    )
    assert authorization is not None

    changed = failures + [
        type(failures[0])("suite.NewRegression", "deadbeef", "new failure")
    ]
    assert legacy_operator_baseline_authorization(
        [request, answer], package_id="WP21-SYNC", current_failures=changed
    ) is None


def test_exact_package_baseline_converts_raw_failure_to_effective_pass(tmp_path):
    command = VerificationCommand(
        command="run-ui-tests",
        profile=CHEAP,
        repository_id="frontend",
    )
    registry = VerificationRegistry(commands=[command])
    runner = _Runner(BASE_FAILURES)
    orch = ProjectOrchestrator(
        "test-project",
        config=OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
        ),
        registry=registry,
        command_runner=runner,
    )
    package = WorkPackage(
        id="WP21-SYNC",
        title="sync",
        affected_repositories=["frontend"],
        stage=WorkPackageStage.FAST_VERIFY,
        verification_profile="cheap",
        verification_baseline_acceptance=_baseline_for(BASE_FAILURES),
    )
    orch._state_record.plan_graph = PlanGraph([package])
    orch._workspace_root = tmp_path
    (tmp_path / "frontend" / ".git").mkdir(parents=True)
    orch._repository_paths["frontend"] = tmp_path / "frontend"

    assert orch._run_verification(package) is True
    assert package.last_verification["status"] == "passed_with_accepted_baseline"
    assert package.verification_attempts == 0
    command_result = package.last_verification["commands"][0]
    assert command_result["status"] == "failed"
    assert len(command_result["failures"]) == 3
    assert any(
        entry.event_type == "verification_passed_with_accepted_baseline"
        for entry in orch._journal.read()
    )


def test_new_failure_is_not_hidden_by_accepted_command(tmp_path):
    command = VerificationCommand(
        command="run-ui-tests",
        profile=CHEAP,
        repository_id="frontend",
    )
    registry = VerificationRegistry(commands=[command])
    output = BASE_FAILURES + """
- sample_plugins.NewSuite NewRegression
  <<< failure message
    /workspace/test.cpp:99
    Expected: true Actual: false
  >>>
"""
    orch = ProjectOrchestrator(
        "test-project",
        config=OrchestrationConfig(
            strict_checks=False,
            auto_commit=False,
            state_dir=tmp_path / "state",
            max_verification_attempts=2,
        ),
        registry=registry,
        command_runner=_Runner(output),
    )
    package = WorkPackage(
        id="WP21-SYNC",
        title="sync",
        affected_repositories=["frontend"],
        stage=WorkPackageStage.FAST_VERIFY,
        verification_profile="cheap",
        verification_baseline_acceptance=_baseline_for(BASE_FAILURES),
    )
    orch._state_record.plan_graph = PlanGraph([package])
    orch._workspace_root = tmp_path
    (tmp_path / "frontend" / ".git").mkdir(parents=True)
    orch._repository_paths["frontend"] = tmp_path / "frontend"

    assert orch._run_verification(package) is False
    assert package.last_verification["status"] == "failed"
    assert package.verification_attempts == 1


def test_operator_baseline_answer_is_persisted_without_another_supervisor_round(tmp_path):
    command = VerificationCommand(
        command="run-ui-tests", profile=CHEAP, repository_id="frontend"
    )
    orch = ProjectOrchestrator(
        "test-project",
        config=OrchestrationConfig(
            strict_checks=False, auto_commit=False, state_dir=tmp_path / "state"
        ),
        registry=VerificationRegistry(commands=[command]),
    )
    package = WorkPackage(
        id="WP21-SYNC",
        title="sync",
        affected_repositories=["frontend"],
        stage=WorkPackageStage.REGRESSION_VERIFY,
        verification_profile="cheap",
        last_verification={
            "status": "failed",
            "attempt": 15,
            "profile": "cheap",
            "failed_commands": [
                {
                    "repository_id": "frontend",
                    "command": "run-ui-tests",
                    "status": "failed",
                    "relevant_excerpt": BASE_FAILURES,
                    "failures": [
                        item.as_mapping()
                        for item in extract_failure_fingerprints(BASE_FAILURES)
                    ],
                }
            ],
        },
    )
    orch._state_record.plan_graph = PlanGraph([package])
    question = {
        "question": "How should WP21-SYNC verification proceed?",
        "context": "Only the exact pre-existing baseline remains.",
        "recommended_option": "accept_baseline",
        "options": [
            {
                "id": "accept_baseline",
                "label": "Re-affirm the time-bounded known-baseline acceptance",
                "consequence": "Conclude the package if the fingerprint stays exact.",
                "weight": 80,
                "risk": "routine",
            },
            {
                "id": "fix",
                "label": "Fix the tests",
                "consequence": "Expand scope.",
                "weight": 20,
                "risk": "product",
            },
        ],
    }
    incident = SupervisorIncident(
        incident_id="inc-1",
        fingerprint="fp",
        package_id=package.id,
        stage="regression_verify",
        classification=IncidentClass.TEST_FAILURE,
        status=IncidentStatus.WAITING_FOR_HUMAN,
        human_question=question,
    )
    orch._supervisor_incidents.save(incident)
    orch._state_record.state = TaskExecutionState.WAITING_FOR_HUMAN_DECISION

    result = orch.submit_supervisor_answer(
        "accept_baseline", message="Keep this authorization durable for WP21-SYNC."
    )

    assert result["status"] == IncidentStatus.RESOLVED.value
    assert orch.state == TaskExecutionState.RUNNING
    assert package.verification_baseline_acceptance["package_id"] == "WP21-SYNC"
    assert package.verification_baseline_acceptance["accepted_by"] == "operator"
    assert len(package.verification_baseline_acceptance["commands"][0]["failures"]) == 3
    assert orch._supervisor_incidents.active() is None


def test_legacy_journal_migration_uses_prior_failure_fingerprint_not_summary_wording():
    failures = extract_failure_fingerprints(BASE_FAILURES)
    failed = _JournalEntry(
        "verification_failed",
        {
            "package_id": "WP21-SYNC",
            "commands": [
                {
                    "command": "run-ui-tests",
                    "status": "failed",
                    "relevant_excerpt": BASE_FAILURES,
                }
            ],
        },
    )
    request = _JournalEntry(
        "supervisor_human_decision_requested",
        {
            "incident_id": "inc-2",
            "package_id": "WP21-SYNC",
            "question": "Attempt 15 has the exact previously reviewed 3-testcase fingerprint. Proceed?",
            "context": "The failures are a pre-existing baseline and no drift was detected.",
            "options": [
                {
                    "id": "reaffirm",
                    "label": "Re-affirm the known-baseline acceptance",
                    "consequence": "Continue WP21-SYNC.",
                }
            ],
        },
    )
    answer = _JournalEntry(
        "supervisor_human_decision_received",
        {
            "incident_id": "inc-2",
            "package_id": "WP21-SYNC",
            "option_id": "reaffirm",
            "message": "Proceed.",
        },
    )
    assert legacy_operator_baseline_authorization(
        [failed, request, answer],
        package_id="WP21-SYNC",
        current_failures=failures,
    ) is not None


def test_work_package_roundtrip_preserves_baseline_audit_record():
    package = WorkPackage(
        id="WP21-SYNC",
        title="sync",
        verification_baseline_acceptance=_baseline_for(BASE_FAILURES),
    )
    restored = WorkPackage.from_mapping(package.as_mapping())
    assert restored.verification_baseline_acceptance == package.verification_baseline_acceptance


def test_human_required_restart_migrates_prior_operator_baseline_without_supervisor(tmp_path):
    command = VerificationCommand(
        command="run-ui-tests", profile=CHEAP, repository_id="frontend"
    )
    orch = ProjectOrchestrator(
        "test-project",
        config=OrchestrationConfig(
            strict_checks=False, auto_commit=False, state_dir=tmp_path / "state"
        ),
        registry=VerificationRegistry(commands=[command]),
    )
    failures = [item.as_mapping() for item in extract_failure_fingerprints(BASE_FAILURES)]
    package = WorkPackage(
        id="WP21-SYNC",
        title="sync",
        stage=WorkPackageStage.REGRESSION_VERIFY,
        verification_profile="cheap",
        last_verification={
            "status": "failed",
            "attempt": 17,
            "profile": "cheap",
            "failed_commands": [
                {
                    "command": "run-ui-tests",
                    "status": "failed",
                    "relevant_excerpt": BASE_FAILURES,
                    "failures": failures,
                }
            ],
        },
    )
    orch._state_record.plan_graph = PlanGraph([package])
    orch._journal.append(
        "verification_failed",
        {
            "package_id": package.id,
            "attempt": 15,
            "profile": "cheap",
            "commands": [
                {
                    "command": "run-ui-tests",
                    "status": "failed",
                    "relevant_excerpt": BASE_FAILURES,
                    "failures": failures,
                }
            ],
        },
    )
    orch._journal.append(
        "supervisor_human_decision_requested",
        {
            "incident_id": "legacy-inc",
            "package_id": package.id,
            "question": "Attempt 15 has the exact reviewed fingerprint. Proceed?",
            "context": "Only the pre-existing known baseline remains.",
            "options": [
                {
                    "id": "reaffirm",
                    "label": "Re-affirm the known-baseline acceptance",
                    "consequence": "Continue the sync.",
                }
            ],
        },
    )
    orch._journal.append(
        "supervisor_human_decision_received",
        {
            "incident_id": "legacy-inc",
            "package_id": package.id,
            "option_id": "reaffirm",
            "option_label": "Re-affirm the known-baseline acceptance",
            "message": "Proceed.",
        },
    )
    orch._journal.append(
        "human_intervention_required",
        {
            "package_id": package.id,
            "stage": "verification",
            "blocked_requirement": "failed verification 17 time(s)",
        },
    )
    orch._state_record.state = TaskExecutionState.HUMAN_REQUIRED

    assert orch.can_auto_resume_accepted_verification_baseline()
    orch._resume_accepted_verification_baseline_unlocked()

    assert orch.state == TaskExecutionState.RUNNING
    assert package.status == "pending"
    assert package.verification_baseline_acceptance["package_id"] == package.id
    assert package.verification_baseline_acceptance["accepted_by"] == "operator"
    assert any(
        entry.event_type == "verification_baseline_authorization_resumed"
        for entry in orch._journal.read()
    )


def test_colcon_native_and_ctest_duplicate_renderings_collapse_to_three_testcases():
    duplicated = (
        BASE_FAILURES
        + "\n[  FAILED  ] MissionClientStatusStream.DeliversBothChannelsAndDropsOldGenerationAfterReconnect (0 ms)\n"
        + "[  FAILED  ] MissionComposeController.ArmThenTakeoffRemainDistinctAndDraftSelectionIsAtomic (1 ms)\n"
        + "[  FAILED  ] MissionComposeController.SpatialRequestUsesPostInsertionDocumentGeneration (1 ms)\n"
    )
    failures = extract_failure_fingerprints(duplicated)
    assert [item.identifier for item in failures] == [
        "MissionClientStatusStream.DeliversBothChannelsAndDropsOldGenerationAfterReconnect",
        "MissionComposeController.ArmThenTakeoffRemainDistinctAndDraftSelectionIsAtomic",
        "MissionComposeController.SpatialRequestUsesPostInsertionDocumentGeneration",
    ]


def test_stale_existing_acceptance_does_not_auto_resume_human_required_loop(tmp_path):
    command = VerificationCommand(
        command="run-ui-tests", profile=CHEAP, repository_id="frontend"
    )
    orch = ProjectOrchestrator(
        "test-project",
        config=OrchestrationConfig(
            strict_checks=False, auto_commit=False, state_dir=tmp_path / "state"
        ),
        registry=VerificationRegistry(commands=[command]),
    )
    current = [item.as_mapping() for item in extract_failure_fingerprints(BASE_FAILURES)]
    stale = _baseline_for(BASE_FAILURES.replace("Expected speed_mps", "Expected altitude_m"))
    package = WorkPackage(
        id="WP21-SYNC",
        title="sync",
        stage=WorkPackageStage.REGRESSION_VERIFY,
        verification_profile="cheap",
        verification_baseline_acceptance=stale,
        last_verification={
            "status": "failed",
            "attempt": 41,
            "profile": "cheap",
            "failed_commands": [
                {
                    "repository_id": "frontend",
                    "command": "run-ui-tests",
                    "status": "failed",
                    "failure_fingerprint_schema": 2,
                    "failures": current,
                    "relevant_excerpt": BASE_FAILURES,
                }
            ],
        },
    )
    orch._state_record.plan_graph = PlanGraph([package])
    orch._journal.append(
        "human_intervention_required",
        {
            "package_id": package.id,
            "stage": "verification",
            "blocked_requirement": "failed verification 41 time(s)",
        },
    )
    orch._state_record.state = TaskExecutionState.HUMAN_REQUIRED

    assert orch.can_auto_resume_accepted_verification_baseline() is False


def test_legacy_suite_count_authorization_rebuilds_stale_acceptance_from_current_failure_set(tmp_path):
    command = VerificationCommand(
        command="run-ui-tests", profile=CHEAP, repository_id="frontend"
    )
    orch = ProjectOrchestrator(
        "test-project",
        config=OrchestrationConfig(
            strict_checks=False, auto_commit=False, state_dir=tmp_path / "state"
        ),
        registry=VerificationRegistry(commands=[command]),
    )
    current = [item.as_mapping() for item in extract_failure_fingerprints(BASE_FAILURES)]
    package = WorkPackage(
        id="WP21-SYNC",
        title="sync",
        stage=WorkPackageStage.REGRESSION_VERIFY,
        verification_profile="cheap",
        # Simulate the first patch having persisted an unusable v1 acceptance.
        verification_baseline_acceptance={
            "schema_version": 1,
            "package_id": "WP21-SYNC",
            "profile": "cheap",
            "commands": [
                {
                    "repository_id": "frontend",
                    "command": "run-ui-tests",
                    "command_digest": "stale",
                    "failures": [],
                }
            ],
        },
        last_verification={
            "status": "failed",
            "attempt": 41,
            "profile": "cheap",
            "failed_commands": [
                {
                    "repository_id": "frontend",
                    "command": "run-ui-tests",
                    "status": "failed",
                    "failure_fingerprint_schema": 2,
                    "failures": current,
                    "relevant_excerpt": BASE_FAILURES,
                }
            ],
        },
    )
    orch._state_record.plan_graph = PlanGraph([package])
    orch._journal.append(
        "supervisor_human_decision_requested",
        {
            "incident_id": "legacy-inc",
            "package_id": package.id,
            "question": "WP21-SYNC has the exact accepted 3-testcase fingerprint. Proceed?",
            "context": (
                "Known baseline: MissionComposeController x2; "
                "MissionClientStatusStream numeric-topic-name crash."
            ),
            "options": [
                {
                    "id": "reaffirm",
                    "label": "Re-affirm the time-bounded known-baseline acceptance",
                    "consequence": "Conclude WP21-SYNC.",
                }
            ],
        },
    )
    orch._journal.append(
        "supervisor_human_decision_received",
        {
            "incident_id": "legacy-inc",
            "package_id": package.id,
            "option_id": "reaffirm",
            "message": "Keep the exact package-local baseline durable.",
        },
    )
    orch._journal.append(
        "human_intervention_required",
        {
            "package_id": package.id,
            "stage": "verification",
            "blocked_requirement": "failed verification 41 time(s)",
        },
    )
    orch._state_record.state = TaskExecutionState.HUMAN_REQUIRED

    assert orch.can_auto_resume_accepted_verification_baseline() is True
    orch._resume_accepted_verification_baseline_unlocked()
    rebuilt = package.verification_baseline_acceptance
    assert rebuilt["fingerprint_schema"] == 2
    assert rebuilt["commands"][0]["command_digest"] != "stale"
    assert len(rebuilt["commands"][0]["failures"]) == 3
