"""Build compact verification and review summaries from the event journal."""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def _verification_summary(journal: Iterable[Any]) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    human_interventions: list[dict[str, Any]] = []
    for entry in journal:
        if not isinstance(entry, Mapping):
            continue
        event_type = str(entry.get("event_type", ""))
        payload = entry.get("payload") or {}
        if not isinstance(payload, Mapping):
            continue
        if event_type == "verification_command_run":
            commands.append(
                {
                    "sequence": entry.get("sequence"),
                    "timestamp": entry.get("timestamp"),
                    "package_id": payload.get("package_id", ""),
                    "repository_id": payload.get("repository_id", ""),
                    "command": payload.get("command", ""),
                    "status": payload.get("status", ""),
                    "returncode": payload.get("returncode"),
                    "duration_seconds": payload.get("duration_seconds"),
                    "stdout_fingerprint": payload.get("stdout_fingerprint", ""),
                }
            )
        elif event_type == "review_result":
            reviews.append(
                {
                    "sequence": entry.get("sequence"),
                    "timestamp": entry.get("timestamp"),
                    "package_id": payload.get("package_id", ""),
                    "verdict": payload.get("verdict", ""),
                    "findings": list(payload.get("findings") or []),
                }
            )
        elif event_type == "human_intervention_required":
            human_interventions.append(
                {
                    "sequence": entry.get("sequence"),
                    "timestamp": entry.get("timestamp"),
                    "package_id": payload.get("package_id", ""),
                    "stage": payload.get("stage", ""),
                    "blocked_requirement": payload.get("blocked_requirement", ""),
                }
            )
    passed = sum(1 for command in commands if command["status"] == "passed")
    failed = sum(1 for command in commands if command["status"] == "failed")
    return {
        "schema_version": 1,
        "totals": {
            "commands": len(commands),
            "passed": passed,
            "failed": failed,
            "reviews": len(reviews),
            "human_interventions": len(human_interventions),
        },
        "commands": commands,
        "reviews": reviews,
        "human_interventions": human_interventions,
    }
