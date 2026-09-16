"""CLI presentation for explicit operator risk acceptance.

The orchestration decision remains owned by :class:`ProjectOrchestrator`; this
module only keeps argument validation and human-facing output out of the already
large top-level CLI facade.
"""

from __future__ import annotations

from typing import Any


def run_accept_risk_cli(orchestrator: Any, args: Any, *, task_id: str) -> int:
    """Validate and render ``orchestrate accept-risk`` without owning policy."""

    if not args.package_id:
        raise ValueError("orchestrate accept-risk requires --package-id")
    if not args.acknowledge_unverified:
        raise ValueError(
            "orchestrate accept-risk requires --acknowledge-unverified; "
            "the decision does not mark review or acceptance criteria as passed"
        )
    if not str(args.accept_reason or "").strip():
        raise ValueError("orchestrate accept-risk requires --accept-reason")

    result = orchestrator.accept_operator_risk(
        str(args.package_id),
        reason=str(args.accept_reason),
        expected_sequence=args.expected_action_sequence,
    )
    print(
        f"Operator risk acceptance recorded for {result['package_id']} "
        f"({result['decision_id']})"
    )
    print(
        "Unverified acceptance remains explicit; the package will continue "
        f"from {result['next_stage']}."
    )
    print(
        "Resume with: execraft orchestrate run "
        f"--project {args.project_id} --task-id {task_id}"
    )
    return 0
