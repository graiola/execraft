from execraft.orchestrate.review_blockers import (
    EXTERNAL_ACTION_MARKER,
    all_findings_require_external_action,
    external_action_findings,
    is_external_action_finding,
)


def test_explicit_external_action_marker_is_authoritative() -> None:
    finding = (
        f"{EXTERNAL_ACTION_MARKER} WP24-FR-003 CRITICAL | "
        "Run the real PX4 simulation and capture durable evidence."
    )

    assert is_external_action_finding(finding) is True
    assert external_action_findings([finding]) == (finding,)
    assert all_findings_require_external_action([finding]) is True


def test_legacy_sandbox_denied_real_sim_finding_is_recognized() -> None:
    finding = (
        "WP24-FR-003 [critical] real-simulation acceptance is unmet; "
        "the implementation result conceded that the sandbox denied Docker/AF_INET. "
        "Execute the real Gazebo/PX4 acceptance run and capture durable evidence."
    )

    assert is_external_action_finding(finding) is True


def test_ordinary_network_or_docker_code_finding_remains_repairable() -> None:
    finding = (
        "WP12-FR-002 HIGH | Docker network retry code drops the original exception; "
        "fix the implementation and add a regression test."
    )

    assert is_external_action_finding(finding) is False


def test_mixed_external_and_repairable_findings_still_enter_fix_cycle() -> None:
    external = (
        f"{EXTERNAL_ACTION_MARKER} Run hardware acceptance and capture evidence."
    )
    repairable = "WP24-FR-004 HIGH | remove the assertion that requires evidence absence."

    assert all_findings_require_external_action([external, repairable]) is False


def test_legacy_mixed_environment_and_source_fix_finding_remains_repairable() -> None:
    finding = (
        "WP24-FR-003 [critical] real-simulation acceptance is unmet; sandbox denied "
        "Docker/AF_INET. Required fix: execute the real Gazebo/PX4 slices and remove "
        "the assertions that require evidence to be absent."
    )

    assert is_external_action_finding(finding) is False
    assert all_findings_require_external_action([finding]) is False
