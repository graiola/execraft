"""Package-scoped verification baseline authorization.

The verification runner must distinguish a command's *raw* return code from the
policy decision about whether that result blocks a package.  Operators may
explicitly accept a proven pre-existing failure set for one package without
turning an entire command into a global known failure.

This module keeps that authorization narrow and deterministic:

* failures are identified individually and fingerprinted after normalizing only
  volatile diagnostics (timestamps, long generated integers, addresses, etc.);
* authorization is scoped to one package, profile, repository and exact command;
* new failures, changed signatures, changed commands and unparsable failures
  remain blocking;
* the record is durable audit evidence but ceases to authorize anything after
  package completion.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

if TYPE_CHECKING:
    from execraft.orchestrate.verification import VerificationCommand, VerificationResult


_GTEST_SUMMARY_FAILURE = re.compile(
    r"^-\s+(?P<suite>[A-Za-z0-9_./:+-]+)\s+(?P<test>[A-Za-z0-9_./:+-]+)\s*$"
)
_GTEST_FAILED_LINE = re.compile(
    r"^\[\s*FAILED\s*\]\s+(?P<identity>[A-Za-z0-9_./:+-]+)(?:\s+\([^)]*\))?\s*$"
)
_PYTEST_FAILED_LINE = re.compile(
    r"^FAILED\s+(?P<identity>[^\s]+?)(?:\s+-\s+(?P<message>.*))?\s*$"
)
_LONG_INTEGER = re.compile(r"(?<![A-Za-z0-9_])\d{8,}(?![A-Za-z0-9_])")
_HEX_ADDRESS = re.compile(r"0x[0-9a-fA-F]{6,}")
_DURATION = re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|sec|secs|seconds)\b", re.IGNORECASE)
_ABSOLUTE_SOURCE_PATH = re.compile(r"(?:/[A-Za-z0-9_.+@%=-]+)+/([^/\s:]+):(\d+)")
_WHITESPACE = re.compile(r"\s+")
_BASELINE_WORDS = ("baseline", "pre-existing", "preexisting")
_ACCEPT_WORDS = ("accept", "re-affirm", "reaffirm", "approve", "tolerat")


def _canonical_test_identity(identity: str) -> str:
    """Return a stable testcase identity across CTest and native gtest output.

    ``colcon test-result --verbose`` prefixes gtest suites with the package
    name (for example ``sample_plugins.MissionComposeController``), while the
    native gtest summary reports ``MissionComposeController.TestName``.  They
    are two renderings of the same testcase and must not become two baseline
    failures.  GoogleTest suite names do not use ``.`` as the suite/test
    separator internally, so retaining only the final suite component is the
    stable cross-rendering form here.
    """

    clean = identity.strip().strip(".")
    if not clean:
        return ""
    parts = clean.split(".")
    if len(parts) <= 2:
        return clean
    return ".".join(parts[-2:])


def _digest(value: str, *, length: int = 24) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:length]


def _command_digest(command: str) -> str:
    return _digest(command.strip(), length=20)


def _normalize_failure_text(text: str) -> str:
    """Remove diagnostics that are expected to vary without changing a defect."""

    normalized = _ABSOLUTE_SOURCE_PATH.sub(r"<source>/\1:\2", text)
    normalized = _HEX_ADDRESS.sub("<address>", normalized)
    normalized = _LONG_INTEGER.sub("<volatile-int>", normalized)
    normalized = _DURATION.sub("<duration>", normalized)
    return _WHITESPACE.sub(" ", normalized).strip()


@dataclass(frozen=True)
class FailureFingerprint:
    identifier: str
    signature: str
    excerpt: str = ""

    def as_mapping(self) -> dict[str, str]:
        return {
            "identifier": self.identifier,
            "signature": self.signature,
            "excerpt": self.excerpt,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "FailureFingerprint":
        return cls(
            identifier=str(data.get("identifier", "")).strip(),
            signature=str(data.get("signature", "")).strip(),
            excerpt=str(data.get("excerpt", "")).strip(),
        )


@dataclass(frozen=True)
class BaselineMatch:
    accepted: bool
    repository_id: str
    command_digest: str
    observed: tuple[FailureFingerprint, ...] = ()
    unexpected: tuple[FailureFingerprint, ...] = ()
    reason: str = ""

    def as_mapping(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "repository_id": self.repository_id,
            "command_digest": self.command_digest,
            "observed": [item.as_mapping() for item in self.observed],
            "unexpected": [item.as_mapping() for item in self.unexpected],
            "reason": self.reason,
        }


def _fingerprint(identity: str, block: str) -> FailureFingerprint:
    identity = _canonical_test_identity(identity)
    normalized = _normalize_failure_text(block)
    signature = _digest(f"{identity}\n{normalized}")
    return FailureFingerprint(
        identifier=identity,
        signature=signature,
        excerpt=normalized[:600],
    )


def extract_failure_fingerprints(output: str) -> list[FailureFingerprint]:
    """Extract individual GoogleTest/CTest/pytest failures from command output.

    Fail closed rather than inventing a command-wide fingerprint.  A raw
    non-zero command with no recognizable test failures is intentionally not
    eligible for a package baseline authorization.
    """

    lines = str(output or "").splitlines()
    # One testcase can be printed twice by colcon/CTest: once as a native
    # ``[ FAILED ] Suite.Test`` summary and again as the verbose CTest failure
    # block.  Baselines authorize *testcases*, not reporting-layer duplicates.
    # Keep one deterministic, information-rich fingerprint per identity.
    found: dict[str, FailureFingerprint] = {}

    def add(identity: str, block_lines: Iterable[str]) -> None:
        clean_identity = _canonical_test_identity(identity)
        if not clean_identity:
            return
        item = _fingerprint(clean_identity, "\n".join(block_lines))
        previous = found.get(item.identifier)
        if previous is None:
            found[item.identifier] = item
            return
        # Prefer the richer diagnostic block.  Tie-breaking lexicographically
        # makes the choice independent of output ordering.
        previous_rank = (len(previous.excerpt), previous.excerpt)
        candidate_rank = (len(item.excerpt), item.excerpt)
        if candidate_rank > previous_rank:
            found[item.identifier] = item

    index = 0
    while index < len(lines):
        line = lines[index].strip()
        summary_match = _GTEST_SUMMARY_FAILURE.match(line)
        if summary_match and index + 1 < len(lines):
            # CTest's verbose summary renders ``- suite Test`` followed by a
            # ``<<< failure message`` block.  Requiring that marker avoids
            # interpreting ordinary bullet lists as test failures.
            next_line = lines[index + 1].strip().lower()
            if next_line.startswith("<<< failure message"):
                identity = f"{summary_match.group('suite')}.{summary_match.group('test')}"
                block = [line]
                cursor = index + 1
                while cursor < len(lines):
                    block.append(lines[cursor])
                    if lines[cursor].strip().startswith(">>>"):
                        break
                    if cursor > index + 1 and _GTEST_SUMMARY_FAILURE.match(lines[cursor].strip()):
                        block.pop()
                        cursor -= 1
                        break
                    cursor += 1
                add(identity, block)
                index = max(index + 1, cursor + 1)
                continue

        failed_match = _GTEST_FAILED_LINE.match(line)
        if failed_match:
            add(failed_match.group("identity"), [line])
            index += 1
            continue

        pytest_match = _PYTEST_FAILED_LINE.match(line)
        if pytest_match:
            identity = pytest_match.group("identity")
            message = pytest_match.group("message") or ""
            add(identity, [identity, message])
        index += 1

    return [found[key] for key in sorted(found)]


def failure_fingerprints_from_mapping(data: Mapping[str, Any]) -> list[FailureFingerprint]:
    raw = data.get("failures")
    if isinstance(raw, list):
        schema = int(data.get("failure_fingerprint_schema", 0) or 0)
        parsed = [
            item
            for row in raw
            if isinstance(row, Mapping)
            for item in [_failure_from_persisted_mapping(row, schema=schema)]
            if item is not None
        ]
        if parsed:
            return _deduplicate_failure_fingerprints(parsed)
    return extract_failure_fingerprints(str(data.get("relevant_excerpt", "")))


def failure_fingerprints_for_result(result: VerificationResult) -> list[FailureFingerprint]:
    if result.failures:
        parsed = [
            item
            for row in result.failures
            for item in [
                _failure_from_persisted_mapping(
                    row, schema=result.failure_fingerprint_schema
                )
            ]
            if item is not None
        ]
        if parsed:
            return _deduplicate_failure_fingerprints(parsed)
    return extract_failure_fingerprints(result.relevant_excerpt)


def _failure_from_persisted_mapping(
    data: Mapping[str, Any],
    *,
    schema: int = 0,
) -> FailureFingerprint | None:
    """Upgrade persisted fingerprints to the current canonical representation.

    Schema-v1 records may contain package-prefixed gtest identities and a
    signature computed before duplicate-report canonicalization.  The stored
    normalized excerpt is sufficient to recompute the signature without
    rerunning verification.  If no excerpt exists, retaining the historical
    signature keeps matching fail-closed.
    """

    parsed = FailureFingerprint.from_mapping(data)
    identity = _canonical_test_identity(parsed.identifier)
    if not identity:
        return None
    if schema >= 2 and parsed.signature:
        return FailureFingerprint(identity, parsed.signature, parsed.excerpt)
    if parsed.excerpt:
        return _fingerprint(identity, parsed.excerpt)
    if not parsed.signature:
        return None
    return FailureFingerprint(identity, parsed.signature, "")


def _deduplicate_failure_fingerprints(
    failures: Sequence[FailureFingerprint],
) -> list[FailureFingerprint]:
    """Collapse duplicate reporting of one testcase deterministically."""

    selected: dict[str, FailureFingerprint] = {}
    for item in failures:
        identity = _canonical_test_identity(item.identifier)
        if not identity:
            continue
        candidate = FailureFingerprint(identity, item.signature, item.excerpt)
        previous = selected.get(identity)
        if previous is None:
            selected[identity] = candidate
            continue
        previous_rank = (len(previous.excerpt), previous.excerpt)
        candidate_rank = (len(candidate.excerpt), candidate.excerpt)
        if candidate_rank > previous_rank:
            selected[identity] = candidate
    return [selected[key] for key in sorted(selected)]


def command_baseline_entry(
    *,
    repository_id: str,
    command: str,
    failures: Sequence[FailureFingerprint],
) -> dict[str, Any]:
    return {
        "repository_id": repository_id,
        "command": command,
        "command_digest": _command_digest(command),
        "failures": [item.as_mapping() for item in failures],
    }


def build_baseline_acceptance(
    *,
    package_id: str,
    profile: str,
    failed_commands: Sequence[Mapping[str, Any]],
    accepted_by: str,
    accepted_at: str,
    incident_id: str = "",
    authorization_text: str = "",
) -> dict[str, Any] | None:
    """Build an exact package-scoped acceptance from persisted verification data."""

    commands: list[dict[str, Any]] = []
    for row in failed_commands:
        command = str(row.get("command", "")).strip()
        repository_id = str(row.get("repository_id", "")).strip()
        failures = failure_fingerprints_from_mapping(row)
        if not command or not failures:
            return None
        commands.append(
            command_baseline_entry(
                repository_id=repository_id,
                command=command,
                failures=failures,
            )
        )
    if not commands:
        return None
    return {
        "schema_version": 1,
        "fingerprint_schema": 2,
        "package_id": package_id,
        "profile": profile,
        "accepted_by": accepted_by,
        "accepted_at": accepted_at,
        "incident_id": incident_id,
        "authorization_text": authorization_text[:4000],
        "expires_on": "package_completion",
        "commands": commands,
    }


def _accepted_command(
    acceptance: Mapping[str, Any],
    *,
    repository_id: str,
    command: str,
) -> Mapping[str, Any] | None:
    digest = _command_digest(command)
    for item in acceptance.get("commands", []):
        if not isinstance(item, Mapping):
            continue
        if str(item.get("repository_id", "")).strip() != repository_id:
            continue
        if str(item.get("command_digest", "")).strip() != digest:
            continue
        if str(item.get("command", "")).strip() != command.strip():
            continue
        return item
    return None


def match_result_against_baseline(
    acceptance: Mapping[str, Any] | None,
    *,
    package_id: str,
    profile: str,
    repository_id: str,
    command: str,
    result: VerificationResult,
    package_completed: bool = False,
) -> BaselineMatch:
    """Return whether one raw failed command is fully covered by authorization."""

    digest = _command_digest(command)
    if not acceptance:
        return BaselineMatch(False, repository_id, digest, reason="no acceptance")
    if package_completed:
        return BaselineMatch(False, repository_id, digest, reason="package completed")
    if str(acceptance.get("package_id", "")) != package_id:
        return BaselineMatch(False, repository_id, digest, reason="package mismatch")
    if str(acceptance.get("profile", "")) != profile:
        return BaselineMatch(False, repository_id, digest, reason="profile mismatch")

    accepted_command = _accepted_command(
        acceptance,
        repository_id=repository_id,
        command=command,
    )
    if accepted_command is None:
        return BaselineMatch(False, repository_id, digest, reason="command not authorized")

    observed = tuple(failure_fingerprints_for_result(result))
    if not observed:
        return BaselineMatch(
            False,
            repository_id,
            digest,
            reason="failed command has no individually fingerprinted test failures",
        )
    accepted = {
        (item.identifier, item.signature)
        for row in accepted_command.get("failures", [])
        if isinstance(row, Mapping)
        for item in [
            _failure_from_persisted_mapping(
                row,
                schema=int(acceptance.get("fingerprint_schema", 0) or 0),
            )
        ]
        if item is not None and item.identifier and item.signature
    }
    unexpected = tuple(
        item for item in observed if (item.identifier, item.signature) not in accepted
    )
    return BaselineMatch(
        accepted=not unexpected,
        repository_id=repository_id,
        command_digest=digest,
        observed=observed,
        unexpected=unexpected,
        reason="all observed failures match accepted baseline" if not unexpected else "unexpected failure fingerprint",
    )


def is_explicit_baseline_acceptance(
    question: Mapping[str, Any],
    selected_option: Mapping[str, Any],
    guidance: str = "",
) -> bool:
    """Recognize an operator option that explicitly accepts a test baseline.

    This intentionally requires both baseline language and an acceptance verb;
    selecting a generic retry/fix/replan option can never create an exemption.
    """

    text = " ".join(
        str(value)
        for value in (
            question.get("question", ""),
            question.get("context", ""),
            selected_option.get("id", ""),
            selected_option.get("label", ""),
            selected_option.get("consequence", ""),
            guidance,
        )
    ).lower()
    return any(word in text for word in _BASELINE_WORDS) and any(
        word in text for word in _ACCEPT_WORDS
    )


def authorization_mentions_failures(
    authorization_text: str,
    failures: Sequence[FailureFingerprint],
) -> bool:
    """Require legacy human authorization text to name every current failure.

    New builds persist the exact fingerprint at answer time.  This stricter
    compatibility check is only for migrating already-recorded operator choices
    created by older builds, where the journal contains intent but not a stored
    fingerprint.
    """

    canonical = _deduplicate_failure_fingerprints(failures)
    if not canonical:
        return False

    haystack = re.sub(r"[^a-z0-9]+", " ", authorization_text.lower()).strip()
    by_suite: dict[str, list[FailureFingerprint]] = {}
    for failure in canonical:
        suite, _, testcase = failure.identifier.rpartition(".")
        by_suite.setdefault(suite.lower(), []).append(failure)
        normalized_testcase = re.sub(r"[^a-z0-9]+", " ", testcase.lower()).strip()
        if normalized_testcase and normalized_testcase in haystack:
            continue

    # Old Supervisor questions often summarized a verified fingerprint as
    # ``MissionComposeController x2`` plus ``MissionClientStatusStream`` rather
    # than repeating very long testcase names.  Accept that historical wording
    # only when the suite multiplicity exactly describes the *current* set.
    for suite, suite_failures in by_suite.items():
        suite_token = re.sub(r"[^a-z0-9]+", " ", suite).strip()
        if not suite_token or suite_token not in haystack:
            # Exact testcase names may still cover every member of this suite.
            if all(
                re.sub(r"[^a-z0-9]+", " ", item.identifier.rpartition(".")[2].lower()).strip()
                in haystack
                for item in suite_failures
            ):
                continue
            return False
        if len(suite_failures) == 1:
            continue
        count_pattern = re.compile(
            rf"\b{re.escape(suite_token)}\b\s*(?:x|times?)?\s*{len(suite_failures)}\b"
        )
        if not count_pattern.search(haystack):
            # Fall back to all exact testcase names if the old text did not
            # use multiplicity notation.
            if not all(
                re.sub(r"[^a-z0-9]+", " ", item.identifier.rpartition(".")[2].lower()).strip()
                in haystack
                for item in suite_failures
            ):
                return False
    return True


def acceptance_covers_failed_rows(
    acceptance: Mapping[str, Any],
    *,
    package_id: str,
    profile: str,
    failed_rows: Sequence[Mapping[str, Any]],
) -> bool:
    """Return whether a durable acceptance fully covers the current failures.

    This predicate is intentionally used *before* auto-resuming a
    ``HUMAN_REQUIRED`` verification state.  Without it, a stale or malformed
    acceptance can wake the package, fail matching, return to HUMAN_REQUIRED,
    and wake itself again forever.
    """

    if not failed_rows:
        return False
    if str(acceptance.get("package_id", "")) != package_id:
        return False
    if str(acceptance.get("profile", "")) != profile:
        return False
    for row in failed_rows:
        command = str(row.get("command", "")).strip()
        repository_id = str(row.get("repository_id", "")).strip()
        if not command:
            return False
        accepted_command = _accepted_command(
            acceptance,
            repository_id=repository_id,
            command=command,
        )
        if accepted_command is None:
            return False
        observed = failure_fingerprints_from_mapping(row)
        if not observed:
            return False
        accepted = {
            (item.identifier, item.signature)
            for item_row in accepted_command.get("failures", [])
            if isinstance(item_row, Mapping)
            for item in [
                _failure_from_persisted_mapping(
                    item_row,
                    schema=int(acceptance.get("fingerprint_schema", 0) or 0),
                )
            ]
            if item is not None
        }
        if any((item.identifier, item.signature) not in accepted for item in observed):
            return False
    return True


def enrich_repository_ids(
    failed_commands: Sequence[Mapping[str, Any]],
    configured_commands: Sequence[VerificationCommand],
) -> list[dict[str, Any]]:
    """Backfill repository IDs for verification records persisted by old builds."""

    by_command: dict[str, set[str]] = {}
    for configured in configured_commands:
        by_command.setdefault(configured.command, set()).add(configured.repository_id)
    enriched: list[dict[str, Any]] = []
    for row in failed_commands:
        item = dict(row)
        if not str(item.get("repository_id", "")).strip():
            repositories = by_command.get(str(item.get("command", "")), set())
            if len(repositories) == 1:
                item["repository_id"] = next(iter(repositories))
        enriched.append(item)
    return enriched


def legacy_operator_baseline_authorization(
    journal_entries: Sequence[Any],
    *,
    package_id: str,
    current_failures: Sequence[FailureFingerprint],
) -> dict[str, str] | None:
    """Recover explicit baseline intent recorded before fingerprint persistence.

    The preferred proof is an exact normalized fingerprint match between the
    verification failure immediately preceding the operator answer and the
    current failure set.  This is stronger than relying on prose in the old
    question and handles UIs that summarized test names.  A textual all-tests
    check remains as compatibility for journals that predate command evidence.
    """

    requests: dict[str, Mapping[str, Any]] = {}
    latest_verification: tuple[FailureFingerprint, ...] = ()
    accepted: dict[str, str] | None = None
    current_set = {(item.identifier, item.signature) for item in current_failures}

    for entry in journal_entries:
        event_type = str(getattr(entry, "event_type", ""))
        payload = getattr(entry, "payload", {})
        if not isinstance(payload, Mapping):
            continue
        if str(payload.get("package_id", "")) != package_id:
            continue

        if event_type == "verification_failed":
            latest_verification = tuple(
                fingerprint
                for row in payload.get("commands", [])
                if isinstance(row, Mapping)
                and str(row.get("status", "")) != "passed"
                for fingerprint in failure_fingerprints_from_mapping(row)
            )
            continue

        incident_id = str(payload.get("incident_id", "")).strip()
        if event_type == "supervisor_human_decision_requested" and incident_id:
            requests[incident_id] = payload
            continue
        if event_type != "supervisor_human_decision_received" or not incident_id:
            continue
        question = requests.get(incident_id)
        if question is None:
            continue
        selected_id = str(payload.get("option_id", "")).strip()
        selected_option = next(
            (
                item
                for item in question.get("options", [])
                if isinstance(item, Mapping)
                and str(item.get("id", "")).strip() == selected_id
            ),
            None,
        )
        if selected_option is None:
            continue
        guidance = str(payload.get("message", ""))
        if not is_explicit_baseline_acceptance(question, selected_option, guidance):
            continue
        authorization_text = " ".join(
            str(value)
            for value in (
                question.get("question", ""),
                question.get("context", ""),
                selected_option.get("label", ""),
                selected_option.get("consequence", ""),
                guidance,
            )
        )
        prior_set = {(item.identifier, item.signature) for item in latest_verification}
        evidence_matches = bool(prior_set) and prior_set == current_set
        if not evidence_matches:
            evidence_matches = authorization_mentions_failures(
                authorization_text, current_failures
            )
        if not evidence_matches:
            continue
        accepted = {
            "incident_id": incident_id,
            "accepted_at": str(getattr(entry, "timestamp", "")),
            "authorization_text": authorization_text,
        }
    return accepted



def failed_verification_rows(
    package: Any,
    configured_commands: Sequence[VerificationCommand],
) -> list[dict[str, Any]]:
    """Read one package's persisted failed commands across schema generations."""

    verification = getattr(package, "last_verification", {}) or {}
    raw = verification.get("failed_commands")
    if not isinstance(raw, list):
        raw = [
            item
            for item in verification.get("commands", [])
            if isinstance(item, Mapping)
            and str(item.get("status", "")) != "passed"
        ]
    rows = [dict(item) for item in raw if isinstance(item, Mapping)]
    return enrich_repository_ids(rows, configured_commands)


def build_package_baseline_acceptance(
    package: Any,
    configured_commands: Sequence[VerificationCommand],
    *,
    accepted_at: str,
    incident_id: str,
    authorization_text: str,
) -> dict[str, Any] | None:
    """Build an acceptance directly from a package's last failed verification."""

    verification = getattr(package, "last_verification", {}) or {}
    if str(verification.get("status", "")) != "failed":
        return None
    return build_baseline_acceptance(
        package_id=str(getattr(package, "id", "")),
        profile=str(verification.get("profile", "")),
        failed_commands=failed_verification_rows(package, configured_commands),
        accepted_by="operator",
        accepted_at=accepted_at,
        incident_id=incident_id,
        authorization_text=authorization_text,
    )


def legacy_package_baseline_candidate(
    *,
    state_value: str,
    human_report: Mapping[str, Any] | None,
    plan_graph: Any,
    configured_commands: Sequence[VerificationCommand],
    journal_entries: Sequence[Any],
    utc_now_value: str,
) -> tuple[Any, dict[str, Any]] | None:
    """Resolve a safe pre-patch baseline authorization for restart migration."""

    if state_value != "human_required" or not human_report:
        return None
    if str(human_report.get("stage", "")) != "verification":
        return None
    package_id = str(human_report.get("package_id", "")).strip()
    if not package_id:
        return None
    try:
        package = plan_graph.package_by_id(package_id)
    except Exception:
        return None
    existing = getattr(package, "verification_baseline_acceptance", {}) or {}
    rows = failed_verification_rows(package, configured_commands)
    verification = getattr(package, "last_verification", {}) or {}
    profile = str(verification.get("profile", ""))
    if existing and acceptance_covers_failed_rows(
        existing,
        package_id=package_id,
        profile=profile,
        failed_rows=rows,
    ):
        return package, dict(existing)
    failures = [
        fingerprint
        for row in rows
        for fingerprint in failure_fingerprints_from_mapping(row)
    ]
    if not failures:
        return None
    authorization = legacy_operator_baseline_authorization(
        journal_entries,
        package_id=package_id,
        current_failures=failures,
    )
    if authorization is None:
        # A stale acceptance is deliberately *not* auto-resumable.  Returning
        # it here would create a deterministic HUMAN_REQUIRED -> verify ->
        # HUMAN_REQUIRED loop with no possible state change.
        return None
    acceptance = build_package_baseline_acceptance(
        package,
        configured_commands,
        accepted_at=authorization.get("accepted_at", "") or utc_now_value,
        incident_id=authorization.get("incident_id", ""),
        authorization_text=authorization.get("authorization_text", ""),
    )
    if acceptance is None:
        return None
    return package, acceptance
