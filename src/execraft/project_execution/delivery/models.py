"""Provider-neutral immutable value objects for Project delivery.

Delivery is intentionally downstream of ProjectMilestone achievement.  A
Milestone freezes a reproducible baseline; these objects describe a candidate
built from that baseline and the result of asking an external adapter to
materialize/distribute it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
from typing import Any, Mapping, Sequence

from execraft.project import validate_project_id

from ..errors import ProjectExecutionError
from ..models import validate_asset_id

DELIVERY_SCHEMA_VERSION = 1
_MAX_TEXT_LENGTH = 20_000
_MAX_REFERENCE_LENGTH = 4_096


def _text(
    value: object,
    *,
    label: str,
    required: bool = False,
    limit: int = _MAX_TEXT_LENGTH,
) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ProjectExecutionError(f"{label} cannot be empty")
    if len(text) > limit:
        raise ProjectExecutionError(f"{label} exceeds {limit} characters")
    return text


def _timestamp(value: object, *, label: str, required: bool = False) -> str:
    text = _text(value, label=label, required=required)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ProjectExecutionError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ProjectExecutionError(f"{label} must include a timezone offset")
    return parsed.isoformat()


def _identifier(value: object, *, label: str) -> str:
    """Validate a stable provider/target identifier without provider semantics."""

    return validate_asset_id(value, label=label)


def _canonical_json(value: object, *, label: str) -> str:
    """Return a stable JSON encoding and reject non-JSON delivery state."""

    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProjectExecutionError(f"{label} must be JSON-serializable") from exc


def _baseline_digest(baseline_json: str) -> str:
    return "sha256:" + hashlib.sha256(baseline_json.encode("utf-8")).hexdigest()


def _candidate_id(milestone_id: str, baseline_digest: str) -> str:
    # Keep a readable prefix, but hash the *full* Milestone identity together
    # with the baseline digest so long IDs sharing a prefix cannot alias.
    identity = hashlib.sha256(
        f"{milestone_id}\0{baseline_digest}".encode("utf-8")
    ).hexdigest()[:24]
    prefix = milestone_id[:68].rstrip("-")
    return f"{prefix}-{identity}"


def _references(raw: object) -> tuple[str, ...]:
    if raw in (None, ""):
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ProjectExecutionError("delivery references must be a list")
    values = tuple(
        _text(value, label="delivery reference", required=True, limit=_MAX_REFERENCE_LENGTH)
        for value in raw
    )
    if len(values) != len(set(values)):
        raise ProjectExecutionError("delivery references cannot contain duplicates")
    return values


class DeliveryOutcome(str, Enum):
    """Terminal provider-reported outcome."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DeliveryOperationState(str, Enum):
    """Durable state of one external delivery attempt."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED}


@dataclass(frozen=True)
class DeliveryCandidate:
    """Immutable delivery candidate derived from one Milestone baseline.

    The baseline is stored as canonical JSON rather than as a mutable mapping.
    ``baseline`` returns a new object on every access, so callers/adapters cannot
    mutate the candidate snapshot in memory.
    """

    candidate_id: str
    project_id: str
    milestone_id: str
    baseline_digest: str
    baseline_json: str
    created_at: str
    schema_version: int = DELIVERY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DELIVERY_SCHEMA_VERSION:
            raise ProjectExecutionError(
                f"unsupported delivery candidate schema_version: {self.schema_version!r}"
            )
        try:
            project_id = validate_project_id(self.project_id)
        except Exception as exc:
            raise ProjectExecutionError(str(exc)) from exc
        milestone_id = validate_asset_id(self.milestone_id, label="milestone id")
        created_at = _timestamp(
            self.created_at,
            label="candidate created_at",
            required=True,
        )
        try:
            baseline = json.loads(self.baseline_json)
        except (TypeError, ValueError) as exc:
            raise ProjectExecutionError("candidate baseline_json is invalid") from exc
        if not isinstance(baseline, Mapping):
            raise ProjectExecutionError("candidate baseline must be a mapping")
        self._validate_baseline(baseline)
        canonical = _canonical_json(baseline, label="candidate baseline")
        digest = _baseline_digest(canonical)
        candidate_id = _candidate_id(milestone_id, digest)
        if self.baseline_digest and self.baseline_digest != digest:
            raise ProjectExecutionError(
                "candidate baseline digest does not match baseline"
            )
        if self.candidate_id and self.candidate_id != candidate_id:
            raise ProjectExecutionError(
                "candidate id does not match baseline digest"
            )
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "milestone_id", milestone_id)
        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(self, "baseline_digest", digest)
        object.__setattr__(self, "baseline_json", canonical)
        object.__setattr__(self, "created_at", created_at)

    @staticmethod
    def _validate_baseline(baseline: Mapping[str, Any]) -> None:
        _timestamp(
            baseline.get("achieved_at"),
            label="candidate baseline achieved_at",
            required=True,
        )
        try:
            definition_revision = int(baseline.get("definition_revision", 0))
        except (TypeError, ValueError) as exc:
            raise ProjectExecutionError(
                "candidate baseline definition_revision must be an integer"
            ) from exc
        if definition_revision < 1:
            raise ProjectExecutionError(
                "candidate baseline definition_revision must be positive"
            )
        repositories = baseline.get("repositories", {})
        if not isinstance(repositories, Mapping):
            raise ProjectExecutionError(
                "candidate baseline repositories must be a mapping"
            )
        for repository_id, revision in repositories.items():
            if not isinstance(repository_id, str) or not isinstance(revision, str):
                raise ProjectExecutionError(
                    "candidate repository IDs/revisions must be strings"
                )
            _text(
                repository_id,
                label="candidate repository id",
                required=True,
                limit=500,
            )
            _text(
                revision,
                label="candidate repository revision",
                required=True,
                limit=500,
            )
        artifacts = baseline.get("artifacts", [])
        if not isinstance(artifacts, Sequence) or isinstance(
            artifacts, (str, bytes)
        ):
            raise ProjectExecutionError("candidate baseline artifacts must be a list")
        for artifact in artifacts:
            if not isinstance(artifact, str):
                raise ProjectExecutionError(
                    "candidate baseline artifacts must contain strings"
                )
            _text(
                artifact,
                label="candidate artifact",
                required=True,
                limit=_MAX_REFERENCE_LENGTH,
            )
        delivery = baseline.get("delivery", {})
        if not isinstance(delivery, Mapping) or delivery.get("policy") != "candidate":
            raise ProjectExecutionError(
                "candidate baseline must carry delivery.policy=candidate"
            )
        if set(delivery) != {"policy"}:
            raise ProjectExecutionError(
                "candidate baseline delivery metadata must remain provider-neutral"
            )

    @classmethod
    def from_baseline(
        cls,
        *,
        project_id: str,
        milestone_id: str,
        baseline: Mapping[str, Any],
        created_at: str,
    ) -> "DeliveryCandidate":
        canonical = _canonical_json(dict(baseline), label="Milestone baseline")
        digest = _baseline_digest(canonical)
        return cls(
            candidate_id=_candidate_id(milestone_id, digest),
            project_id=project_id,
            milestone_id=milestone_id,
            baseline_digest=digest,
            baseline_json=canonical,
            created_at=created_at,
        )

    @property
    def baseline(self) -> dict[str, Any]:
        return json.loads(self.baseline_json)

    @property
    def repositories(self) -> dict[str, str]:
        raw = self.baseline.get("repositories", {})
        if not isinstance(raw, Mapping):
            return {}
        return {str(key): str(value) for key, value in raw.items()}

    @property
    def artifacts(self) -> tuple[str, ...]:
        raw = self.baseline.get("artifacts", [])
        if not isinstance(raw, list):
            return ()
        return tuple(str(value) for value in raw)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "project_id": self.project_id,
            "milestone_id": self.milestone_id,
            "baseline_digest": self.baseline_digest,
            "created_at": self.created_at,
            "baseline": self.baseline,
        }

    @classmethod
    def from_mapping(cls, raw: object) -> "DeliveryCandidate":
        if not isinstance(raw, Mapping):
            raise ProjectExecutionError("delivery candidate must be a mapping")
        baseline = raw.get("baseline")
        if not isinstance(baseline, Mapping):
            raise ProjectExecutionError("delivery candidate baseline must be a mapping")
        return cls(
            schema_version=int(raw.get("schema_version", 0)),
            candidate_id=str(raw.get("candidate_id", "")),
            project_id=str(raw.get("project_id", "")),
            milestone_id=str(raw.get("milestone_id", "")),
            baseline_digest=str(raw.get("baseline_digest", "")),
            baseline_json=_canonical_json(dict(baseline), label="candidate baseline"),
            created_at=str(raw.get("created_at", "")),
        )


@dataclass(frozen=True)
class DeliveryTarget:
    """Logical destination resolved by a delivery adapter outside the domain.

    Only a stable ID and optional human label are persisted. Credentials,
    registry URLs, deployment-provider configuration, and similar details stay
    in the provider adapter/configuration layer.
    """

    target_id: str
    label: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_id",
            _identifier(self.target_id, label="delivery target id"),
        )
        object.__setattr__(
            self,
            "label",
            _text(self.label, label="delivery target label", limit=500),
        )

    def as_mapping(self) -> dict[str, str]:
        row = {"target_id": self.target_id}
        if self.label:
            row["label"] = self.label
        return row

    @classmethod
    def from_mapping(cls, raw: object) -> "DeliveryTarget":
        if not isinstance(raw, Mapping):
            raise ProjectExecutionError("delivery target must be a mapping")
        return cls(
            target_id=str(raw.get("target_id", "")),
            label=str(raw.get("label", "")),
        )


@dataclass(frozen=True)
class DeliveryResult:
    """Provider-neutral terminal result returned by a delivery adapter."""

    outcome: DeliveryOutcome
    references: tuple[str, ...] = ()
    message: str = ""

    def __post_init__(self) -> None:
        try:
            outcome = DeliveryOutcome(self.outcome)
        except ValueError as exc:
            raise ProjectExecutionError("delivery outcome must be succeeded or failed") from exc
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "references", _references(self.references))
        object.__setattr__(self, "message", _text(self.message, label="delivery result message"))

    def as_mapping(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "references": list(self.references),
            "message": self.message,
        }

    @classmethod
    def from_mapping(cls, raw: object) -> "DeliveryResult":
        if not isinstance(raw, Mapping):
            raise ProjectExecutionError("delivery result must be a mapping")
        return cls(
            outcome=str(raw.get("outcome", "")),
            references=_references(raw.get("references")),
            message=str(raw.get("message", "")),
        )


@dataclass(frozen=True)
class DeliveryRequest:
    """One idempotency-keyed delivery request passed to an adapter."""

    operation_id: str
    candidate: DeliveryCandidate
    target: DeliveryTarget

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _identifier(self.operation_id, label="delivery operation id"),
        )
        if not isinstance(self.candidate, DeliveryCandidate):
            raise ProjectExecutionError("delivery request candidate is invalid")
        if not isinstance(self.target, DeliveryTarget):
            raise ProjectExecutionError("delivery request target is invalid")


@dataclass(frozen=True)
class DeliveryOperation:
    """Durable audit state for one adapter invocation."""

    operation_id: str
    sequence: int
    candidate_id: str
    target: DeliveryTarget
    provider_id: str
    state: DeliveryOperationState
    requested_at: str
    completed_at: str = ""
    result: DeliveryResult | None = None
    diagnostic: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _identifier(self.operation_id, label="delivery operation id"),
        )
        if int(self.sequence) < 1:
            raise ProjectExecutionError("delivery operation sequence must be positive")
        object.__setattr__(self, "sequence", int(self.sequence))
        object.__setattr__(
            self,
            "candidate_id",
            _identifier(self.candidate_id, label="delivery candidate id"),
        )
        if not isinstance(self.target, DeliveryTarget):
            raise ProjectExecutionError("delivery operation target is invalid")
        object.__setattr__(
            self,
            "provider_id",
            _identifier(self.provider_id, label="delivery provider id"),
        )
        try:
            state = DeliveryOperationState(self.state)
        except ValueError as exc:
            raise ProjectExecutionError("invalid delivery operation state") from exc
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self,
            "requested_at",
            _timestamp(self.requested_at, label="delivery requested_at", required=True),
        )
        object.__setattr__(
            self,
            "completed_at",
            _timestamp(self.completed_at, label="delivery completed_at"),
        )
        object.__setattr__(
            self,
            "diagnostic",
            _text(self.diagnostic, label="delivery diagnostic"),
        )
        if self.result is not None and not isinstance(self.result, DeliveryResult):
            raise ProjectExecutionError("delivery operation result is invalid")
        if state == DeliveryOperationState.SUCCEEDED:
            if self.result is None or self.result.outcome != DeliveryOutcome.SUCCEEDED:
                raise ProjectExecutionError("succeeded delivery operation requires succeeded result")
        if state == DeliveryOperationState.FAILED:
            if self.result is None or self.result.outcome != DeliveryOutcome.FAILED:
                raise ProjectExecutionError("failed delivery operation requires failed result")
        if (
            state in {
                DeliveryOperationState.PENDING,
                DeliveryOperationState.UNCERTAIN,
            }
            and self.result is not None
        ):
            raise ProjectExecutionError(
                "non-terminal delivery operation cannot contain terminal result"
            )
        if state.terminal and not self.completed_at:
            raise ProjectExecutionError(
                "terminal delivery operation requires completed_at"
            )
        if not state.terminal and self.completed_at:
            raise ProjectExecutionError(
                "non-terminal delivery operation cannot contain completed_at"
            )

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.candidate_id, self.target.target_id, self.provider_id)

    def as_mapping(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "operation_id": self.operation_id,
            "sequence": self.sequence,
            "candidate_id": self.candidate_id,
            "target": self.target.as_mapping(),
            "provider_id": self.provider_id,
            "state": self.state.value,
            "requested_at": self.requested_at,
        }
        if self.completed_at:
            row["completed_at"] = self.completed_at
        if self.result is not None:
            row["result"] = self.result.as_mapping()
        if self.diagnostic:
            row["diagnostic"] = self.diagnostic
        return row

    @classmethod
    def from_mapping(cls, raw: object) -> "DeliveryOperation":
        if not isinstance(raw, Mapping):
            raise ProjectExecutionError("delivery operation must be a mapping")
        result = raw.get("result")
        return cls(
            operation_id=str(raw.get("operation_id", "")),
            sequence=int(raw.get("sequence", 0)),
            candidate_id=str(raw.get("candidate_id", "")),
            target=DeliveryTarget.from_mapping(raw.get("target")),
            provider_id=str(raw.get("provider_id", "")),
            state=str(raw.get("state", "")),
            requested_at=str(raw.get("requested_at", "")),
            completed_at=str(raw.get("completed_at", "")),
            result=DeliveryResult.from_mapping(result) if result is not None else None,
            diagnostic=str(raw.get("diagnostic", "")),
        )
