"""Canonical semantics for raw and effective verification outcomes.

Verification commands always retain their raw execution status.  Package-level
acceptance is a separate concept because policy may explicitly authorize an
exact, fingerprinted baseline failure.  Keeping those concepts separate avoids
silently rewriting failed test commands while allowing downstream checks to
reason about the effective verification decision consistently.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


PASSED = "passed"
PASSED_WITH_ACCEPTED_BASELINE = "passed_with_accepted_baseline"
FAILED = "failed"

ACCEPTED_EFFECTIVE_STATUSES = frozenset(
    {PASSED, PASSED_WITH_ACCEPTED_BASELINE}
)

# Exact message emitted by Execraft versions that understood only literal
# ``status == \"passed\"`` at the repository-sync commit check.  It is retained
# solely as a migration fingerprint so an already-safe READY_TO_COMMIT package
# can resume after upgrading without another Supervisor/operator round.
LEGACY_REPOSITORY_SYNC_COMMIT_CHECK_REASON = (
    "repository sync reached commit without passed verification and approved review"
)


def normalize_verification_status(value: object) -> str:
    """Return the canonical lowercase status token for *value*."""

    return str(value or "").strip().lower()


def effective_verification_status(verification: Mapping[str, Any] | None) -> str:
    """Return the package-level effective status from a verification mapping."""

    if not isinstance(verification, Mapping):
        return ""
    return normalize_verification_status(verification.get("status"))


def verification_is_accepted(verification: Mapping[str, Any] | None) -> bool:
    """Return whether downstream package checks may treat verification as accepted."""

    return effective_verification_status(verification) in ACCEPTED_EFFECTIVE_STATUSES


def verification_uses_accepted_baseline(
    verification: Mapping[str, Any] | None,
) -> bool:
    """Return whether acceptance depended on a scoped baseline authorization."""

    return (
        effective_verification_status(verification)
        == PASSED_WITH_ACCEPTED_BASELINE
    )


def accepted_baseline_failure_count(
    verification: Mapping[str, Any] | None,
) -> int:
    """Count individual raw failures covered by baseline matches."""

    if not isinstance(verification, Mapping):
        return 0
    matches = verification.get("accepted_baseline_matches")
    if not isinstance(matches, Sequence) or isinstance(matches, (str, bytes)):
        return 0
    total = 0
    for match in matches:
        if not isinstance(match, Mapping):
            continue
        observed = match.get("observed")
        if isinstance(observed, Sequence) and not isinstance(observed, (str, bytes)):
            total += len(observed)
    return total


def verification_acceptance_phrase(
    verification: Mapping[str, Any] | None,
) -> str:
    """Return an audit-safe prose fragment describing effective verification."""

    status = effective_verification_status(verification)
    if status == PASSED_WITH_ACCEPTED_BASELINE:
        count = accepted_baseline_failure_count(verification)
        suffix = f" ({count} accepted baseline failure(s))" if count else ""
        return "verification passed with package-scoped accepted baseline" + suffix
    if status == PASSED:
        return "verification passed"
    return f"verification status {status or 'unknown'}"
