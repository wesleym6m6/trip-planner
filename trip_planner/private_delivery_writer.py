"""Phase 6.2B authoritative create-only private-delivery writer.

This module is deliberately separate from the Phase 6.2A candidate contract.
It accepts no 6.2A review or response and never promotes a manifest or digest
into authority.  A trusted host reloads bounded canonical bytes and a
read-only evidence snapshot, reproduces the exact reviewed artifacts, pins an
existing ignored private root plus an absent leaf, and presents a new sealed
write review.  Only the matching fresh profile-specific response can enter the
final pre-write recheck.

The filesystem contract is create-only and manifest-last, not a portable
multi-file atomic transaction.  A target directory may contain an incomplete
uncommitted subset after a fault.  A valid exact ``manifest.json`` installed
last is the logical commit marker.  Existing targets are never merged,
overwritten, replaced, repaired, or deleted by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import unicodedata
import weakref
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

from .canonical_tripctl import (
    CanonicalTripctlError,
    _CanonicalSourceSnapshot,
    _read_source_snapshot,
    _source_is_current,
)
from .codec import PlanCodecError, decode_plan
from .evidence_session import EvidenceSessionDeliverySource
from .facts import EvidenceSnapshot, FactKey
from .lodging import LodgingIntakeAssessment
from .lodging_confirmation import LodgingConfirmationReview
from .private_delivery import (
    PrivateDeliveryArtifact,
    PrivateDeliveryError,
    PrivateDeliveryProfile,
    PrivateDeliveryReview,
    prepare_private_delivery_review,
)
from .store import TripStore


PRIVATE_DELIVERY_WRITE_VERSION = "private-delivery-write/v1"
PRIVATE_DELIVERY_WRITE_RESPONSE_VERSION = "private-delivery-write-response/v1"
PRIVATE_DELIVERY_WRITE_OUTCOME_VERSION = "private-delivery-write-outcome/v1"
PRIVATE_DELIVERY_WRITE_REVIEW_TTL = timedelta(minutes=5)
MAX_PRIVATE_DELIVERY_WRITE_REVIEW_BYTES = 64 * 1024
MAX_PRIVATE_DELIVERY_WRITE_ACTIVE_REVIEWS = 256
MAX_PRIVATE_DELIVERY_TARGET_LEAF_BYTES = 128

_UTC = timezone.utc
_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_TARGET_LEAF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_REVIEW_TOKEN = object()
_RESPONSE_TOKEN = object()
_OUTCOME_TOKEN = object()
_PATH_TYPE = type(Path("."))
_RLOCK_TYPE = type(threading.RLock())
_EVIDENCE_SESSION_DELIVERY_READ_SNAPSHOT = (
    EvidenceSessionDeliverySource.read_snapshot
)


class PrivateDeliveryReadOnlyEvidenceSource(Protocol):
    """Fresh, non-mutating evidence boundary required by the writer."""

    trip_id: str
    slug: str
    trips_root: Path
    data_dir: Path

    def read_snapshot(self, *, evaluation_at: datetime) -> EvidenceSnapshot:
        """Re-read one immutable snapshot without durable cleanup or purge."""


class PrivateDeliveryWriteResponseKind(str, Enum):
    """Exact write-review responses; only profile matches may be accepted."""

    AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE = (
        "authorize_html_preview_create_only_write"
    )
    AUTHORIZE_HTML_ICS_READY_BUNDLE_CREATE_ONLY_WRITE = (
        "authorize_html_ics_ready_bundle_create_only_write"
    )
    REQUEST_CHANGES = "request_changes"
    CANCEL = "cancel"


class PrivateDeliveryWriteOutcomeKind(str, Enum):
    """Truthful observable states for one terminal write attempt."""

    CREATED = "created"
    REQUEST_CHANGES = "request_changes"
    CANCELLED = "cancelled"
    PARTIAL_UNCOMMITTED = "partial_uncommitted"
    OUTCOME_UNKNOWN = "outcome_unknown"
    RECONCILED_CREATED = "reconciled_created"
    RECOVERY_CONFLICT = "recovery_conflict"


_ACCEPT_FOR_PROFILE = {
    PrivateDeliveryProfile.HTML_PREVIEW: (
        PrivateDeliveryWriteResponseKind.
        AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE
    ),
    PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE: (
        PrivateDeliveryWriteResponseKind.
        AUTHORIZE_HTML_ICS_READY_BUNDLE_CREATE_ONLY_WRITE
    ),
}


class PrivateDeliveryWriteError(ValueError):
    """A bounded refusal whose value is always one fixed public code."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or _ERROR_CODE_RE.fullmatch(code) is None:
            raise ValueError("private delivery write error code is invalid")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class _WriteController:
    store: TripStore = field(repr=False, compare=False)
    evidence_source: object = field(repr=False, compare=False)
    clock: Callable[[], datetime] = field(repr=False, compare=False)
    fault_hook: Callable[[str], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    availability_keys: tuple[FactKey, ...] = field(
        default=(),
        repr=False,
        compare=False,
    )
    lodging_intake: LodgingIntakeAssessment | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    pending_lodging_review: LodgingConfirmationReview | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    trips_root: Path = field(default=Path("."), repr=False)
    trip_dir: Path = field(default=Path("."), repr=False)
    data_dir: Path = field(default=Path("."), repr=False)
    plan_path: Path = field(default=Path("."), repr=False)
    private_root: Path = field(default=Path("."), repr=False)
    target_leaf: str = field(default="", repr=False)
    private_root_identity: tuple[int, int, int, int] = field(
        default=(0, 0, 0, 0),
        repr=False,
    )
    source_directory_identity: tuple[int, int] = field(
        default=(0, 0),
        repr=False,
    )
    source_identity: tuple[int, int, int, int, int] = field(
        default=(0, 0, 0, 0, 0),
        repr=False,
    )
    source_digest: str = field(default="", repr=False)
    evidence_source_id: int = field(default=0, repr=False)
    clock_id: int = field(default=0, repr=False)
    fault_hook_id: int | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True)
class PrivateDeliveryWriteReview:
    """One sealed exact write review with no captured authorization yet."""

    profile: PrivateDeliveryProfile
    artifacts: tuple[PrivateDeliveryArtifact, ...] = field(repr=False)
    created_at: datetime = field(repr=False)
    expires_at: datetime = field(repr=False)
    source_path: str = field(repr=False)
    target_path: str = field(repr=False)
    candidate_review_id: str = field(repr=False)
    review_id: str = field(repr=False)
    private_review_json: bytes = field(repr=False)
    _controller: _WriteController = field(repr=False, compare=False)
    contract_version: str = PRIVATE_DELIVERY_WRITE_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError("PrivateDeliveryWriteReview must come from preparation")
        _verify_review_shape(self)

    @property
    def authorization_response_kind(self) -> PrivateDeliveryWriteResponseKind:
        return _ACCEPT_FOR_PROFILE[self.profile]

    def __repr__(self) -> str:
        return (
            "PrivateDeliveryWriteReview(contains_private_data=True, "
            "dynamic_status_redacted=True)"
        )

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("PrivateDeliveryWriteReview is process-local")

    def to_safe_dict(self) -> dict[str, object]:
        record = _verified_review_record(self)
        with record.lock:
            _require_review_seal(self, record)
            dynamic = _record_safe_status(record)
            return {
                "contract_version": PRIVATE_DELIVERY_WRITE_VERSION,
                "profile": self.profile.value,
                "artifact_filenames": [item.filename for item in self.artifacts],
                "authorization_response_kind": (
                    self.authorization_response_kind.value
                ),
                "contains_private_data": True,
                "authoritative_source_reloaded": True,
                "source_filesystem_verified_at_review": True,
                "target_parent_verified_at_review": True,
                "target_absent_at_review": True,
                "create_only": True,
                "overwrite_allowed": False,
                "authorization_response_captured": (
                    record.captured_response_id is not None
                ),
                **dynamic,
            }

    def to_ephemeral_private_review(self) -> dict[str, Any]:
        _verified_review_record(self)
        try:
            value = json.loads(self.private_review_json)
        except Exception:
            raise PrivateDeliveryWriteError(
                "PRIVATE_DELIVERY_WRITE_REVIEW_TAMPERED"
            ) from None
        if type(value) is not dict:
            raise PrivateDeliveryWriteError(
                "PRIVATE_DELIVERY_WRITE_REVIEW_TAMPERED"
            )
        return value

    def to_ephemeral_private_artifacts(
        self,
    ) -> tuple[PrivateDeliveryArtifact, ...]:
        """Return exact reviewed private bytes without adding authority."""

        record = _verified_review_record(self)
        with record.lock:
            _require_review_seal(self, record)
            return self.artifacts


@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True)
class PrivateDeliveryWriteResponse:
    """One fresh process-local response to exactly one write review."""

    kind: PrivateDeliveryWriteResponseKind
    profile: PrivateDeliveryProfile
    accepted_for_prewrite_recheck: bool
    review_id: str = field(repr=False)
    response_id: str = field(repr=False)
    captured_at: datetime = field(repr=False)
    _review: PrivateDeliveryWriteReview = field(repr=False, compare=False)
    contract_version: str = PRIVATE_DELIVERY_WRITE_RESPONSE_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _RESPONSE_TOKEN:
            raise ValueError("PrivateDeliveryWriteResponse must come from capture")
        _verify_response_shape(self)

    def __repr__(self) -> str:
        return (
            "PrivateDeliveryWriteResponse(authorization_response_captured=True, "
            "dynamic_status_redacted=True)"
        )

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("PrivateDeliveryWriteResponse is process-local")

    def to_safe_dict(self) -> dict[str, object]:
        verify_private_delivery_write_response(self, check_freshness=False)
        review_record = _verified_review_record(self._review)
        with review_record.lock:
            _require_review_seal(self._review, review_record)
            return {
                "contract_version": PRIVATE_DELIVERY_WRITE_RESPONSE_VERSION,
                "profile": self.profile.value,
                "kind": self.kind.value,
                "authorization_response_captured": True,
                "accepted_for_prewrite_recheck_at_capture": (
                    self.accepted_for_prewrite_recheck
                ),
                "current_execution_eligibility_not_asserted": True,
                **_record_safe_status(review_record),
            }


@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True)
class PrivateDeliveryWriteOutcome:
    """One value-free terminal or recovery result for a write attempt.

    ``final_target_published`` refers only to this private manifest-based
    logical commit, never public publication. ``None`` means the observable
    state is unknown or conflicting. ``cleanup_complete`` means no cleanup is
    outstanding; it does not claim that deletion or repair was performed.
    """

    kind: PrivateDeliveryWriteOutcomeKind
    profile: PrivateDeliveryProfile
    filesystem_mutation_started: bool
    final_target_published: bool | None
    cleanup_complete: bool
    recovery_required: bool
    contract_version: str = PRIVATE_DELIVERY_WRITE_OUTCOME_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _OUTCOME_TOKEN:
            raise ValueError("PrivateDeliveryWriteOutcome must come from execution")
        if type(self.kind) is not PrivateDeliveryWriteOutcomeKind:
            raise TypeError("write outcome kind must be exact")
        if type(self.profile) is not PrivateDeliveryProfile:
            raise TypeError("write outcome profile must be exact")
        if (
            type(self.filesystem_mutation_started) is not bool
            or (
                self.final_target_published is not None
                and type(self.final_target_published) is not bool
            )
            or type(self.cleanup_complete) is not bool
            or type(self.recovery_required) is not bool
        ):
            raise TypeError("write outcome flags must be bool")
        if self.contract_version != PRIVATE_DELIVERY_WRITE_OUTCOME_VERSION:
            raise ValueError("unsupported write outcome version")

    def __repr__(self) -> str:
        return "PrivateDeliveryWriteOutcome(contains_private_data=False)"

    def to_safe_dict(self) -> dict[str, object]:
        _verified_outcome_record(self)
        return {
            "contract_version": PRIVATE_DELIVERY_WRITE_OUTCOME_VERSION,
            "profile": self.profile.value,
            "write_outcome": self.kind.value,
            "filesystem_mutation_started": self.filesystem_mutation_started,
            "final_target_published": self.final_target_published,
            "cleanup_complete": self.cleanup_complete,
            "cleanup_outstanding": not self.cleanup_complete,
            "recovery_required": self.recovery_required,
            "private_values_exposed": False,
        }


@dataclass(slots=True)
class _WriteReviewRecord:
    review_ref: weakref.ReferenceType[PrivateDeliveryWriteReview]
    fingerprint: str
    last_checked_at: datetime
    captured_response_id: str | None = None
    execution_state: str = "pending"
    target_identity: tuple[int, int] | None = None
    last_outcome: PrivateDeliveryWriteOutcome | None = None
    last_outcome_fingerprint: str | None = None
    transition_active: bool = False
    transition_poisoned: bool = False
    lock: object = field(default_factory=threading.RLock)


@dataclass(slots=True)
class _WriteResponseRecord:
    response_ref: weakref.ReferenceType[PrivateDeliveryWriteResponse]
    fingerprint: str


@dataclass(slots=True)
class _WriteOutcomeRecord:
    outcome_ref: weakref.ReferenceType[PrivateDeliveryWriteOutcome]
    fingerprint: str


_REGISTRY_LOCK = threading.RLock()
_REVIEW_REGISTRY: dict[int, _WriteReviewRecord] = {}
_RESPONSE_REGISTRY: dict[int, _WriteResponseRecord] = {}
_OUTCOME_REGISTRY: dict[int, _WriteOutcomeRecord] = {}


def prepare_private_delivery_write_review(
    store: TripStore,
    evidence_source: PrivateDeliveryReadOnlyEvidenceSource,
    *,
    profile: PrivateDeliveryProfile,
    private_root: str | Path,
    target_leaf: str,
    clock: Callable[[], datetime],
    availability_keys: tuple[FactKey, ...] = (),
    lodging_intake: LodgingIntakeAssessment | None = None,
    pending_lodging_review: LodgingConfirmationReview | None = None,
    fault_hook: Callable[[str], None] | None = None,
) -> PrivateDeliveryWriteReview:
    """Reload, reproject, pin an absent target, and create a fresh write review.

    ``fault_hook`` is a trusted synthetic fault-injection seam.  A real host
    must leave it as ``None``; it is not part of the delivery authority or a
    supported application callback boundary.
    """

    if type(store) is not TripStore:
        raise PrivateDeliveryWriteError("PRIVATE_DELIVERY_WRITE_STORE_INVALID")
    if type(profile) is not PrivateDeliveryProfile:
        raise PrivateDeliveryWriteError("PRIVATE_DELIVERY_WRITE_PROFILE_INVALID")
    if not callable(clock) or (fault_hook is not None and not callable(fault_hook)):
        raise PrivateDeliveryWriteError("PRIVATE_DELIVERY_WRITE_HOST_INVALID")
    paths = _validated_store_paths(store)
    root = _validated_private_root(paths[1], private_root)
    leaf = _validated_target_leaf(target_leaf)
    created_at = _sample_clock(clock)

    first_source, first_candidate = _reload_candidate(
        store=store,
        evidence_source=evidence_source,
        profile=profile,
        source_path=paths[3],
        target_path=root / leaf,
        evaluation_at=created_at,
        availability_keys=availability_keys,
        lodging_intake=lodging_intake,
        pending_lodging_review=pending_lodging_review,
    )
    second_source, candidate = _reload_candidate(
        store=store,
        evidence_source=evidence_source,
        profile=profile,
        source_path=paths[3],
        target_path=root / leaf,
        evaluation_at=created_at,
        availability_keys=availability_keys,
        lodging_intake=lodging_intake,
        pending_lodging_review=pending_lodging_review,
    )
    _require_same_source(first_source, second_source)
    _require_same_candidate(first_candidate, candidate)
    root_identity = _verified_private_root(root)
    with _opened_private_root(root, expected_identity=root_identity) as root_fd:
        _require_target_absent(root_fd, leaf)

    expires_at = min(
        created_at + PRIVATE_DELIVERY_WRITE_REVIEW_TTL,
        candidate.expires_at,
    )
    if expires_at <= created_at:
        raise PrivateDeliveryWriteError("PRIVATE_DELIVERY_WRITE_REVIEW_EXPIRED")
    artifacts = candidate.to_ephemeral_private_artifacts()
    controller = _WriteController(
        store=store,
        evidence_source=evidence_source,
        clock=clock,
        fault_hook=fault_hook,
        availability_keys=availability_keys,
        lodging_intake=lodging_intake,
        pending_lodging_review=pending_lodging_review,
        trips_root=paths[0],
        trip_dir=paths[1],
        data_dir=paths[2],
        plan_path=paths[3],
        private_root=root,
        target_leaf=leaf,
        private_root_identity=root_identity,
        source_directory_identity=second_source.directory_identity,
        source_identity=second_source.source_identity,
        source_digest=second_source.source_digest,
        evidence_source_id=id(evidence_source),
        clock_id=id(clock),
        fault_hook_id=(id(fault_hook) if fault_hook is not None else None),
    )
    private_review_json = _write_review_bytes(
        candidate=candidate,
        source_path=paths[3],
        target_path=root / leaf,
        created_at=created_at,
        expires_at=expires_at,
    )
    review_id = _digest_json(
        {
            "candidate_review_id": candidate.review_id,
            "profile": profile.value,
            "source_identity": list(second_source.source_identity),
            "source_digest": second_source.source_digest,
            "private_root_identity": list(root_identity),
            "target_leaf": leaf,
            "created_at": _utc_iso(created_at),
            "expires_at": _utc_iso(expires_at),
            "artifacts": [
                [item.filename, item.media_type, item.sha256]
                for item in artifacts
            ],
            "private_review_sha256": hashlib.sha256(
                private_review_json
            ).hexdigest(),
        },
        prefix=PRIVATE_DELIVERY_WRITE_VERSION,
    )
    review = PrivateDeliveryWriteReview(
        profile=profile,
        artifacts=artifacts,
        created_at=created_at,
        expires_at=expires_at,
        source_path=os.fspath(paths[3]),
        target_path=os.fspath(root / leaf),
        candidate_review_id=candidate.review_id,
        review_id=review_id,
        private_review_json=private_review_json,
        _controller=controller,
        _token=_REVIEW_TOKEN,
    )
    _register_review(review)
    return review


def capture_private_delivery_write_response(
    review: PrivateDeliveryWriteReview,
    kind: PrivateDeliveryWriteResponseKind,
) -> PrivateDeliveryWriteResponse:
    """Capture exactly one fresh response to one exact write review."""

    if type(kind) is not PrivateDeliveryWriteResponseKind:
        raise PrivateDeliveryWriteError("PRIVATE_DELIVERY_WRITE_RESPONSE_INVALID")
    record = _verified_review_record(review)
    with record.lock:
        _begin_review_transition(record)
        try:
            checked_at = _sample_clock(review._controller.clock)
            _require_clean_review_transition(record)
            if checked_at < record.last_checked_at:
                raise PrivateDeliveryWriteError(
                    "PRIVATE_DELIVERY_WRITE_CLOCK_ROLLBACK"
                )
            record.last_checked_at = checked_at
            _require_review_seal(review, record)
            if checked_at >= review.expires_at:
                raise PrivateDeliveryWriteError(
                    "PRIVATE_DELIVERY_WRITE_REVIEW_EXPIRED"
                )
            if record.captured_response_id is not None:
                raise PrivateDeliveryWriteError(
                    "PRIVATE_DELIVERY_WRITE_RESPONSE_ALREADY_CAPTURED"
                )
            accepted = kind is review.authorization_response_kind
            if kind in {
                PrivateDeliveryWriteResponseKind.
                AUTHORIZE_HTML_PREVIEW_CREATE_ONLY_WRITE,
                PrivateDeliveryWriteResponseKind.
                AUTHORIZE_HTML_ICS_READY_BUNDLE_CREATE_ONLY_WRITE,
            } and not accepted:
                raise PrivateDeliveryWriteError(
                    "PRIVATE_DELIVERY_WRITE_RESPONSE_PROFILE_MISMATCH"
                )
            response_id = _digest_json(
                {
                    "review_id": review.review_id,
                    "kind": kind.value,
                    "captured_at": _utc_iso(checked_at),
                },
                prefix=PRIVATE_DELIVERY_WRITE_RESPONSE_VERSION,
            )
            response = PrivateDeliveryWriteResponse(
                kind=kind,
                profile=review.profile,
                accepted_for_prewrite_recheck=accepted,
                review_id=review.review_id,
                response_id=response_id,
                captured_at=checked_at,
                _review=review,
                _token=_RESPONSE_TOKEN,
            )
            _register_response(response)
            _require_clean_review_transition(record)
            record.captured_response_id = response_id
            return response
        finally:
            _end_review_transition(record)


def verify_private_delivery_write_response(
    response: PrivateDeliveryWriteResponse,
    *,
    check_freshness: bool = True,
) -> None:
    """Verify one live response without executing a filesystem effect."""

    response_record = _verified_response_record(response)
    review = response._review
    review_record = _verified_review_record(review)
    with review_record.lock:
        if not check_freshness:
            if review_record.captured_response_id != response.response_id:
                raise PrivateDeliveryWriteError(
                    "PRIVATE_DELIVERY_WRITE_RESPONSE_BINDING_MISMATCH"
                )
            _require_response_seal(response, response_record)
            _require_review_seal(review, review_record)
            return
        _begin_review_transition(review_record)
        try:
            if review_record.captured_response_id != response.response_id:
                raise PrivateDeliveryWriteError(
                    "PRIVATE_DELIVERY_WRITE_RESPONSE_BINDING_MISMATCH"
                )
            _require_response_seal(response, response_record)
            _require_review_seal(review, review_record)
            checked_at = _sample_clock(review._controller.clock)
            _require_clean_review_transition(review_record)
            if checked_at < review_record.last_checked_at:
                raise PrivateDeliveryWriteError(
                    "PRIVATE_DELIVERY_WRITE_CLOCK_ROLLBACK"
                )
            review_record.last_checked_at = checked_at
            if checked_at >= review.expires_at:
                raise PrivateDeliveryWriteError(
                    "PRIVATE_DELIVERY_WRITE_REVIEW_EXPIRED"
                )
        finally:
            _end_review_transition(review_record)


def execute_private_delivery_write_response(
    response: PrivateDeliveryWriteResponse,
) -> PrivateDeliveryWriteOutcome:
    """Consume one response, recheck exact inputs, and attempt one create."""

    response_record = _verified_response_record(response)
    review = response._review
    record = _verified_review_record(review)
    with record.lock:
        _begin_review_transition(record)
        try:
            if record.captured_response_id != response.response_id:
                raise PrivateDeliveryWriteError(
                    "PRIVATE_DELIVERY_WRITE_RESPONSE_BINDING_MISMATCH"
                )
            checked_at = _sample_clock(review._controller.clock)
            _require_clean_review_transition(record)
            if checked_at < record.last_checked_at:
                raise PrivateDeliveryWriteError(
                    "PRIVATE_DELIVERY_WRITE_CLOCK_ROLLBACK"
                )
            record.last_checked_at = checked_at
            _require_response_seal(response, response_record)
            _require_review_seal(review, record)
            if checked_at >= review.expires_at:
                record.execution_state = "terminal"
                raise PrivateDeliveryWriteError(
                    "PRIVATE_DELIVERY_WRITE_REVIEW_EXPIRED"
                )
            if record.execution_state != "pending":
                raise PrivateDeliveryWriteError(
                    "PRIVATE_DELIVERY_WRITE_RESPONSE_ALREADY_EXECUTED"
                )
            record.execution_state = "executing"

            if response.kind is PrivateDeliveryWriteResponseKind.REQUEST_CHANGES:
                record.execution_state = "terminal"
                outcome = _outcome(
                    review.profile,
                    PrivateDeliveryWriteOutcomeKind.REQUEST_CHANGES,
                    mutation=False,
                    published=False,
                    cleanup=True,
                    recovery=False,
                )
                _set_record_outcome(record, outcome)
                return outcome
            if response.kind is PrivateDeliveryWriteResponseKind.CANCEL:
                record.execution_state = "terminal"
                outcome = _outcome(
                    review.profile,
                    PrivateDeliveryWriteOutcomeKind.CANCELLED,
                    mutation=False,
                    published=False,
                    cleanup=True,
                    recovery=False,
                )
                _set_record_outcome(record, outcome)
                return outcome
            if not response.accepted_for_prewrite_recheck:
                record.execution_state = "terminal"
                raise PrivateDeliveryWriteError(
                    "PRIVATE_DELIVERY_WRITE_AUTHORIZATION_REQUIRED"
                )

            try:
                _reproduce_around_freshness(
                    review,
                    record,
                    require_target_absent=True,
                )
            except PrivateDeliveryWriteError:
                record.execution_state = "terminal"
                raise

            try:
                result = _write_reviewed_bundle(review, record)
            except Exception:
                record.execution_state = "terminal"
                raise
            record.target_identity = result.target_identity
            _set_record_outcome(record, result.outcome)
            if result.kind is PrivateDeliveryWriteOutcomeKind.CREATED:
                record.execution_state = "created"
            elif result.kind is PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN:
                record.execution_state = "outcome_unknown"
            else:
                record.execution_state = "terminal"
            return result.outcome
        finally:
            _end_review_transition(record)


def reconcile_private_delivery_write_response(
    response: PrivateDeliveryWriteResponse,
) -> PrivateDeliveryWriteOutcome:
    """Inspect one outcome-unknown target; never rewrite artifact content."""

    response_record = _verified_response_record(response)
    review = response._review
    record = _verified_review_record(review)
    with record.lock:
        _begin_review_transition(record)
        try:
            _require_response_seal(response, response_record)
            _require_review_seal(review, record)
            if record.captured_response_id != response.response_id:
                raise PrivateDeliveryWriteError(
                    "PRIVATE_DELIVERY_WRITE_RESPONSE_BINDING_MISMATCH"
                )
            if record.execution_state != "outcome_unknown":
                raise PrivateDeliveryWriteError(
                    "PRIVATE_DELIVERY_WRITE_RECOVERY_NOT_AVAILABLE"
                )
            if record.target_identity is None:
                outcome = _outcome(
                    review.profile,
                    PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN,
                    mutation=True,
                    published=None,
                    cleanup=False,
                    recovery=True,
                )
                _set_record_outcome(record, outcome)
                return outcome
            classification = _inspect_sync_and_reinspect_target(
                review,
                expected_identity=record.target_identity,
            )
            _require_clean_review_transition(record)
            if classification == "exact":
                record.execution_state = "reconciled"
                outcome = _outcome(
                    review.profile,
                    PrivateDeliveryWriteOutcomeKind.RECONCILED_CREATED,
                    mutation=True,
                    published=True,
                    cleanup=True,
                    recovery=False,
                )
                _set_record_outcome(record, outcome)
                return outcome
            if classification == "unavailable":
                outcome = _outcome(
                    review.profile,
                    PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN,
                    mutation=True,
                    published=None,
                    cleanup=False,
                    recovery=True,
                )
                _set_record_outcome(record, outcome)
                return outcome
            record.execution_state = "terminal"
            outcome = _outcome(
                review.profile,
                PrivateDeliveryWriteOutcomeKind.RECOVERY_CONFLICT,
                mutation=True,
                published=None,
                cleanup=False,
                recovery=True,
            )
            _set_record_outcome(record, outcome)
            return outcome
        finally:
            _end_review_transition(record)


@dataclass(frozen=True, slots=True)
class _WriteAttemptResult:
    kind: PrivateDeliveryWriteOutcomeKind
    outcome: PrivateDeliveryWriteOutcome
    target_identity: tuple[int, int] | None


def _write_reviewed_bundle(
    review: PrivateDeliveryWriteReview,
    record: _WriteReviewRecord,
) -> _WriteAttemptResult:
    controller = review._controller
    target_create_attempted = False
    target_created = False
    manifest_attempted = False
    target_identity: tuple[int, int] | None = None
    try:
        with _opened_private_root(
            controller.private_root,
            expected_identity=controller.private_root_identity,
        ) as root_fd:
            _require_target_absent(root_fd, controller.target_leaf)
            _fault(controller, "before_target_create")
            target_create_attempted = True
            try:
                os.mkdir(controller.target_leaf, 0o700, dir_fd=root_fd)
            except FileExistsError:
                target_create_attempted = False
                raise PrivateDeliveryWriteError(
                    "PRIVATE_DELIVERY_WRITE_TARGET_EXISTS"
                ) from None
            target_created = True
            created_stat = os.stat(
                controller.target_leaf,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(created_stat.st_mode)
                or created_stat.st_uid != os.geteuid()
                or stat.S_IMODE(created_stat.st_mode) != 0o700
            ):
                raise OSError("created target directory identity mismatch")
            created_identity = (created_stat.st_dev, created_stat.st_ino)
            _fault(controller, "after_target_create")
            target_fd = _open_target_directory(
                root_fd,
                controller.target_leaf,
                expected_identity=created_identity,
            )
            try:
                os.fchmod(target_fd, 0o700)
                target_stat = os.fstat(target_fd)
                if (
                    not stat.S_ISDIR(target_stat.st_mode)
                    or target_stat.st_uid != os.geteuid()
                    or stat.S_IMODE(target_stat.st_mode) != 0o700
                    or not _target_mapping_matches(
                        root_fd,
                        target_fd,
                        controller.target_leaf,
                    )
                ):
                    raise OSError("target directory hardening failed")
                target_identity = (target_stat.st_dev, target_stat.st_ino)
                payload_artifacts = tuple(
                    item for item in review.artifacts
                    if item.filename != "manifest.json"
                )
                manifest = next(
                    item for item in review.artifacts
                    if item.filename == "manifest.json"
                )
                for artifact in payload_artifacts:
                    _write_exact_file(target_fd, artifact)
                    _fault(
                        controller,
                        "after_" + artifact.filename.replace(".", "_") + "_fsync",
                    )
                os.fsync(target_fd)
                _fault(controller, "after_payload_directory_fsync")

                _reproduce_around_freshness(
                    review,
                    record,
                    require_target_absent=False,
                )
                manifest_attempted = True
                manifest_fd = _open_new_artifact(target_fd, manifest.filename)
                try:
                    _fault(controller, "after_manifest_create")
                    _write_all(manifest_fd, manifest.payload)
                    os.fchmod(manifest_fd, 0o600)
                    os.fsync(manifest_fd)
                    _verify_written_file(manifest_fd, manifest)
                finally:
                    os.close(manifest_fd)
                _fault(controller, "after_manifest_fsync")
                if _inspect_open_target(
                    root_fd,
                    target_fd,
                    controller.target_leaf,
                    review.artifacts,
                ) != "exact":
                    raise OSError("artifact tree mismatch")
                os.fsync(target_fd)
                _fault(controller, "after_manifest_directory_fsync")
                os.fsync(root_fd)
                _fault(controller, "after_parent_directory_fsync")
                if _inspect_open_target(
                    root_fd,
                    target_fd,
                    controller.target_leaf,
                    review.artifacts,
                ) != "exact":
                    raise OSError("artifact tree changed after durability check")
            finally:
                os.close(target_fd)
    except Exception as exc:
        if not target_create_attempted:
            if isinstance(exc, PrivateDeliveryWriteError):
                raise exc
            raise PrivateDeliveryWriteError(
                "PRIVATE_DELIVERY_WRITE_NOT_PERFORMED"
            ) from None
        if not target_created:
            outcome = _outcome(
                review.profile,
                PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN,
                mutation=True,
                published=None,
                cleanup=False,
                recovery=True,
            )
            return _WriteAttemptResult(
                kind=PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN,
                outcome=outcome,
                target_identity=None,
            )
        if manifest_attempted:
            classification = _inspect_exact_target(
                review,
                expected_identity=target_identity,
            )
            if classification == "incomplete":
                outcome = _outcome(
                    review.profile,
                    PrivateDeliveryWriteOutcomeKind.PARTIAL_UNCOMMITTED,
                    mutation=True,
                    published=False,
                    cleanup=False,
                    recovery=True,
                )
                return _WriteAttemptResult(
                    kind=PrivateDeliveryWriteOutcomeKind.PARTIAL_UNCOMMITTED,
                    outcome=outcome,
                    target_identity=target_identity,
                )
            outcome = _outcome(
                review.profile,
                PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN,
                mutation=True,
                published=None,
                cleanup=False,
                recovery=True,
            )
            return _WriteAttemptResult(
                kind=PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN,
                outcome=outcome,
                target_identity=target_identity,
            )
        outcome = _outcome(
            review.profile,
            PrivateDeliveryWriteOutcomeKind.PARTIAL_UNCOMMITTED,
            mutation=True,
            published=False,
            cleanup=False,
            recovery=True,
        )
        return _WriteAttemptResult(
            kind=PrivateDeliveryWriteOutcomeKind.PARTIAL_UNCOMMITTED,
            outcome=outcome,
            target_identity=target_identity,
        )
    if _inspect_exact_target(
        review,
        expected_identity=target_identity,
    ) != "exact":
        outcome = _outcome(
            review.profile,
            PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN,
            mutation=True,
            published=None,
            cleanup=False,
            recovery=True,
        )
        return _WriteAttemptResult(
            kind=PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN,
            outcome=outcome,
            target_identity=target_identity,
        )
    outcome = _outcome(
        review.profile,
        PrivateDeliveryWriteOutcomeKind.CREATED,
        mutation=True,
        published=True,
        cleanup=True,
        recovery=False,
    )
    return _WriteAttemptResult(
        kind=PrivateDeliveryWriteOutcomeKind.CREATED,
        outcome=outcome,
        target_identity=target_identity,
    )


def _reproduce_review(
    review: PrivateDeliveryWriteReview,
    *,
    require_target_absent: bool,
) -> None:
    controller = review._controller
    first_source, first_candidate = _reload_candidate(
        store=controller.store,
        evidence_source=controller.evidence_source,
        profile=review.profile,
        source_path=controller.plan_path,
        target_path=controller.private_root / controller.target_leaf,
        evaluation_at=review.created_at,
        availability_keys=controller.availability_keys,
        lodging_intake=controller.lodging_intake,
        pending_lodging_review=controller.pending_lodging_review,
    )
    second_source, second_candidate = _reload_candidate(
        store=controller.store,
        evidence_source=controller.evidence_source,
        profile=review.profile,
        source_path=controller.plan_path,
        target_path=controller.private_root / controller.target_leaf,
        evaluation_at=review.created_at,
        availability_keys=controller.availability_keys,
        lodging_intake=controller.lodging_intake,
        pending_lodging_review=controller.pending_lodging_review,
    )
    _require_same_source(first_source, second_source)
    _require_same_candidate(first_candidate, second_candidate)
    if (
        second_source.directory_identity != controller.source_directory_identity
        or second_source.source_identity != controller.source_identity
        or second_source.source_digest != controller.source_digest
    ):
        raise PrivateDeliveryWriteError("PRIVATE_DELIVERY_WRITE_SOURCE_STALE")
    _require_candidate_matches_review(second_candidate, review)
    with _opened_private_root(
        controller.private_root,
        expected_identity=controller.private_root_identity,
    ) as root_fd:
        if require_target_absent:
            _require_target_absent(root_fd, controller.target_leaf)


def _check_write_freshness(
    review: PrivateDeliveryWriteReview,
    record: _WriteReviewRecord,
) -> None:
    checked_at = _sample_clock(review._controller.clock)
    _require_clean_review_transition(record)
    if checked_at < record.last_checked_at:
        raise PrivateDeliveryWriteError("PRIVATE_DELIVERY_WRITE_CLOCK_ROLLBACK")
    record.last_checked_at = checked_at
    _require_review_seal(review, record)
    if checked_at >= review.expires_at:
        raise PrivateDeliveryWriteError("PRIVATE_DELIVERY_WRITE_REVIEW_EXPIRED")


def _reproduce_around_freshness(
    review: PrivateDeliveryWriteReview,
    record: _WriteReviewRecord,
    *,
    require_target_absent: bool,
) -> None:
    """Sandwich the last host-clock callback between exact reproductions."""

    _reproduce_review(
        review,
        require_target_absent=require_target_absent,
    )
    _require_clean_review_transition(record)
    _check_write_freshness(review, record)
    _reproduce_review(
        review,
        require_target_absent=require_target_absent,
    )
    _require_clean_review_transition(record)


def _reload_candidate(
    *,
    store: TripStore,
    evidence_source: object,
    profile: PrivateDeliveryProfile,
    source_path: Path,
    target_path: Path,
    evaluation_at: datetime,
    availability_keys: tuple[FactKey, ...],
    lodging_intake: LodgingIntakeAssessment | None,
    pending_lodging_review: LodgingConfirmationReview | None,
) -> tuple[_CanonicalSourceSnapshot, PrivateDeliveryReview]:
    source = _secure_source_snapshot(store, source_path)
    try:
        plan = decode_plan(source.raw)
        trip_id = plan["trip_id"]
    except (KeyError, PlanCodecError, TypeError, ValueError):
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_CANONICAL_INVALID"
        ) from None
    if type(trip_id) is not str:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_CANONICAL_INVALID"
        )
    snapshot = _read_evidence_snapshot(
        evidence_source,
        evaluation_at=evaluation_at,
        expected_trip_id=trip_id,
        store=store,
    )
    try:
        candidate = prepare_private_delivery_review(
            source.raw,
            snapshot,
            profile=profile,
            source_path=os.fspath(source_path),
            target_path=os.fspath(target_path),
            clock=lambda: evaluation_at,
            availability_keys=availability_keys,
            lodging_intake=lodging_intake,
            pending_lodging_review=pending_lodging_review,
        )
    except PrivateDeliveryError as exc:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_REPROJECTION_REFUSED"
        ) from exc
    except Exception:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_REPROJECTION_REFUSED"
        ) from None
    if not _source_is_current(source):
        raise PrivateDeliveryWriteError("PRIVATE_DELIVERY_WRITE_SOURCE_STALE")
    return source, candidate


def _read_evidence_snapshot(
    source: object,
    *,
    evaluation_at: datetime,
    expected_trip_id: str,
    store: TripStore,
) -> EvidenceSnapshot:
    try:
        exact_session_adapter = (
            type(source) is EvidenceSessionDeliverySource
        )
        if exact_session_adapter:
            trip_id = object.__getattribute__(source, "trip_id")
            source_slug = object.__getattribute__(source, "slug")
            source_root = object.__getattribute__(source, "trips_root")
            source_data_dir = object.__getattribute__(source, "data_dir")
            reader = _EVIDENCE_SESSION_DELIVERY_READ_SNAPSHOT
        else:
            trip_id = getattr(source, "trip_id")
            source_slug = getattr(source, "slug")
            source_root = getattr(source, "trips_root")
            source_data_dir = getattr(source, "data_dir")
            reader = getattr(source, "read_snapshot")
        if (
            type(trip_id) is not str
            or trip_id != expected_trip_id
            or type(source_slug) is not str
            or source_slug != store.slug
            or type(source_root) is not _PATH_TYPE
            or source_root != store.trips_root
            or type(source_data_dir) is not _PATH_TYPE
            or source_data_dir != store.data_dir
        ):
            raise ValueError("evidence trip mismatch")
        if not callable(reader):
            raise TypeError("evidence reader unavailable")
        if exact_session_adapter:
            snapshot = reader(source, evaluation_at=evaluation_at)
        else:
            snapshot = reader(evaluation_at=evaluation_at)
    except Exception:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_EVIDENCE_UNAVAILABLE"
        ) from None
    if type(snapshot) is not EvidenceSnapshot:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_EVIDENCE_UNAVAILABLE"
        )
    return snapshot


def _secure_source_snapshot(
    store: TripStore,
    plan_path: Path,
) -> _CanonicalSourceSnapshot:
    try:
        _validated_store_paths(store)
        before = plan_path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) & 0o022
        ):
            raise ValueError("unsafe canonical source metadata")
        snapshot = _read_source_snapshot(store.data_dir)
        after = plan_path.lstat()
    except (CanonicalTripctlError, OSError, ValueError):
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_SOURCE_UNAVAILABLE"
        ) from None
    if _stat_identity(before) != _stat_identity(after):
        raise PrivateDeliveryWriteError("PRIVATE_DELIVERY_WRITE_SOURCE_STALE")
    return snapshot


def _validated_store_paths(
    store: TripStore,
) -> tuple[Path, Path, Path, Path]:
    try:
        if type(store) is not TripStore or type(store.slug) is not str:
            raise ValueError("invalid store")
        values = (
            store.trips_root,
            store.trip_dir,
            store.data_dir,
            store.plan_path,
        )
        if any(type(value) is not _PATH_TYPE for value in values):
            raise ValueError("store path type drift")
        trips_root, trip_dir, data_dir, plan_path = values
        if (
            trip_dir != trips_root / store.slug
            or data_dir != trip_dir / "data"
            or plan_path != data_dir / "plan.json"
        ):
            raise ValueError("store path binding drift")
        for path in (trips_root, trip_dir, data_dir):
            info = path.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise ValueError("unsafe store directory")
    except Exception:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_STORE_INVALID"
        ) from None
    return values


def _validated_private_root(trip_dir: Path, value: str | Path) -> Path:
    try:
        root = Path(value)
    except Exception:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_PRIVATE_ROOT_INVALID"
        ) from None
    if (
        type(root) is not _PATH_TYPE
        or not root.is_absolute()
        or root.parent != trip_dir
        or root.name == "data"
        or _TARGET_LEAF_RE.fullmatch(root.name) is None
    ):
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_PRIVATE_ROOT_INVALID"
        )
    _verified_private_root(root)
    return root


def _validated_target_leaf(value: object) -> str:
    if (
        type(value) is not str
        or _TARGET_LEAF_RE.fullmatch(value) is None
        or len(value.encode("utf-8")) > MAX_PRIVATE_DELIVERY_TARGET_LEAF_BYTES
        or value in {"data", "index", "manifest"}
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_TARGET_LEAF_INVALID"
        )
    return value


def _verified_private_root(path: Path) -> tuple[int, int, int, int]:
    try:
        with _opened_private_root(path, expected_identity=None) as descriptor:
            info = os.fstat(descriptor)
            return (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode))
    except PrivateDeliveryWriteError:
        raise
    except Exception:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_PRIVATE_ROOT_INVALID"
        ) from None


class _OpenedRoot:
    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor

    def __enter__(self) -> int:
        return self.descriptor

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        os.close(self.descriptor)


def _opened_private_root(
    path: Path,
    *,
    expected_identity: tuple[int, int, int, int] | None,
) -> _OpenedRoot:
    if not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW")):
        raise PrivateDeliveryWriteError("PRIVATE_DELIVERY_WRITE_PLATFORM_UNSAFE")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        lexical = path.lstat()
        identity = (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode))
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(lexical.st_mode)
            or (lexical.st_dev, lexical.st_ino) != (info.st_dev, info.st_ino)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or (expected_identity is not None and identity != expected_identity)
        ):
            raise OSError("unsafe private root")
    except Exception:
        try:
            os.close(descriptor)
        except Exception:
            pass
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_PRIVATE_ROOT_STALE"
        ) from None
    return _OpenedRoot(descriptor)


def _verify_private_root_identity(
    path: Path,
    expected: tuple[int, int, int, int],
) -> None:
    with _opened_private_root(path, expected_identity=expected):
        return


def _require_target_absent(root_fd: int, leaf: str) -> None:
    try:
        os.stat(leaf, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_TARGET_UNAVAILABLE"
        ) from None
    raise PrivateDeliveryWriteError("PRIVATE_DELIVERY_WRITE_TARGET_EXISTS")


def _open_target_directory(
    root_fd: int,
    leaf: str,
    *,
    expected_identity: tuple[int, int] | None,
) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(leaf, flags, dir_fd=root_fd)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or (
                expected_identity is not None
                and (info.st_dev, info.st_ino) != expected_identity
            )
        ):
            raise OSError("unsafe target directory")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _write_exact_file(target_fd: int, artifact: PrivateDeliveryArtifact) -> None:
    artifact.to_safe_dict()
    descriptor = _open_new_artifact(target_fd, artifact.filename)
    try:
        _write_all(descriptor, artifact.payload)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        _verify_written_file(descriptor, artifact)
    finally:
        os.close(descriptor)


def _open_new_artifact(target_fd: int, filename: str) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return os.open(filename, flags, 0o600, dir_fd=target_fd)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise OSError("short artifact write")
        written += count


def _verify_written_file(
    descriptor: int,
    artifact: PrivateDeliveryArtifact,
) -> None:
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size != len(artifact.payload)
    ):
        raise OSError("written artifact metadata mismatch")


def _bounded_directory_names(target_fd: int, *, limit: int) -> tuple[str, ...]:
    names: list[str] = []
    with os.scandir(target_fd) as entries:
        for entry in entries:
            if type(entry.name) is not str:
                raise OSError("artifact name type mismatch")
            names.append(entry.name)
            if len(names) > limit:
                break
    return tuple(names)


def _artifact_stat_signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _target_mapping_matches(
    root_fd: int,
    target_fd: int,
    leaf: str,
) -> bool:
    target = os.fstat(target_fd)
    mapped = os.stat(leaf, dir_fd=root_fd, follow_symlinks=False)
    return (
        stat.S_ISDIR(target.st_mode)
        and stat.S_ISDIR(mapped.st_mode)
        and target.st_uid == os.geteuid()
        and mapped.st_uid == os.geteuid()
        and stat.S_IMODE(target.st_mode) == 0o700
        and stat.S_IMODE(mapped.st_mode) == 0o700
        and (target.st_dev, target.st_ino) == (mapped.st_dev, mapped.st_ino)
    )


def _read_and_verify_artifact(
    target_fd: int,
    artifact: PrivateDeliveryArtifact,
) -> str:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(artifact.filename, flags, dir_fd=target_fd)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unavailable"
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size != len(artifact.payload)
        ):
            return "conflict"
        chunks: list[bytes] = []
        remaining = len(artifact.payload) + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if _artifact_stat_signature(before) != _artifact_stat_signature(after):
            return "unavailable"
        if b"".join(chunks) != artifact.payload:
            return "conflict"
    except OSError:
        return "unavailable"
    finally:
        os.close(descriptor)
    return "exact"


def _fsync_exact_artifact(
    target_fd: int,
    artifact: PrivateDeliveryArtifact,
) -> bool:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(artifact.filename, flags, dir_fd=target_fd)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size != len(artifact.payload)
            ):
                return False
            os.fsync(descriptor)
            after = os.fstat(descriptor)
            return (
                _artifact_stat_signature(before)
                == _artifact_stat_signature(after)
            )
        finally:
            os.close(descriptor)
    except OSError:
        return False


def _inspect_open_target(
    root_fd: int,
    target_fd: int,
    leaf: str,
    artifacts: tuple[PrivateDeliveryArtifact, ...],
) -> str:
    expected = {item.filename: item for item in artifacts}
    try:
        if not _target_mapping_matches(root_fd, target_fd, leaf):
            return "unavailable"
        before_names = _bounded_directory_names(
            target_fd,
            limit=len(expected),
        )
    except OSError:
        return "unavailable"
    actual = set(before_names)
    if len(actual) != len(before_names) or not actual.issubset(expected):
        return "conflict"
    manifest_present = "manifest.json" in actual
    if manifest_present and actual != set(expected):
        return "conflict"
    for name in before_names:
        classification = _read_and_verify_artifact(target_fd, expected[name])
        if classification != "exact":
            return classification
    try:
        after_names = _bounded_directory_names(
            target_fd,
            limit=len(expected),
        )
        if (
            len(before_names) != len(after_names)
            or set(before_names) != set(after_names)
        ):
            return "unavailable"
        if not _target_mapping_matches(root_fd, target_fd, leaf):
            return "unavailable"
    except OSError:
        return "unavailable"
    if actual == set(expected):
        return "exact"
    if not manifest_present:
        return "incomplete"
    return "conflict"


def _inspect_exact_target(
    review: PrivateDeliveryWriteReview,
    *,
    expected_identity: tuple[int, int] | None,
) -> str:
    controller = review._controller
    try:
        with _opened_private_root(
            controller.private_root,
            expected_identity=controller.private_root_identity,
        ) as root_fd:
            target_fd = _open_target_directory(
                root_fd,
                controller.target_leaf,
                expected_identity=expected_identity,
            )
            try:
                return _inspect_open_target(
                    root_fd,
                    target_fd,
                    controller.target_leaf,
                    review.artifacts,
                )
            finally:
                os.close(target_fd)
    except FileNotFoundError:
        return "absent"
    except PrivateDeliveryWriteError:
        return "unavailable"
    except OSError:
        try:
            with _opened_private_root(
                controller.private_root,
                expected_identity=controller.private_root_identity,
            ) as root_fd:
                os.stat(
                    controller.target_leaf,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
        except FileNotFoundError:
            return "absent"
        except Exception:
            return "unavailable"
        return "conflict"
    except Exception:
        return "unavailable"


def _inspect_sync_and_reinspect_target(
    review: PrivateDeliveryWriteReview,
    *,
    expected_identity: tuple[int, int],
) -> str:
    controller = review._controller
    try:
        with _opened_private_root(
            controller.private_root,
            expected_identity=controller.private_root_identity,
        ) as root_fd:
            target_fd = _open_target_directory(
                root_fd,
                controller.target_leaf,
                expected_identity=expected_identity,
            )
            try:
                before = _inspect_open_target(
                    root_fd,
                    target_fd,
                    controller.target_leaf,
                    review.artifacts,
                )
                if before != "exact":
                    return before
                for artifact in review.artifacts:
                    if not _fsync_exact_artifact(target_fd, artifact):
                        return "unavailable"
                middle = _inspect_open_target(
                    root_fd,
                    target_fd,
                    controller.target_leaf,
                    review.artifacts,
                )
                if middle != "exact":
                    return middle
                os.fsync(target_fd)
                os.fsync(root_fd)
                return _inspect_open_target(
                    root_fd,
                    target_fd,
                    controller.target_leaf,
                    review.artifacts,
                )
            finally:
                os.close(target_fd)
    except FileNotFoundError:
        return "absent"
    except Exception:
        return "unavailable"


def _write_review_bytes(
    *,
    candidate: PrivateDeliveryReview,
    source_path: Path,
    target_path: Path,
    created_at: datetime,
    expires_at: datetime,
) -> bytes:
    candidate_private = candidate.to_ephemeral_private_review()
    payload: dict[str, object] = {
        "contract_version": PRIVATE_DELIVERY_WRITE_VERSION,
        "profile": candidate.profile.value,
        "source_path": os.fspath(source_path),
        "target_path": os.fspath(target_path),
        "source_filesystem_verified": True,
        "target_parent_verified": True,
        "target_absent_at_review": True,
        "create_only": True,
        "overwrite_allowed": False,
        "external_phase62a_review_or_response_consumed": False,
        "matching_fresh_write_response_required": True,
        "same_process_live_response_required": True,
        "one_create_attempt_per_response": True,
        "partial_target_may_remain": True,
        "cross_process_recovery_authorized": False,
        "reconciliation_rewrites_content": False,
        "automatic_partial_cleanup": False,
        "vcs_ignore_config_verified_by_writer": False,
        "browser_authorized": False,
        "serve_share_or_calendar_import_authorized": False,
        "public_source_or_deploy_authorized": False,
        "manifest_is_logical_commit_marker": True,
        "portable_multi_file_atomicity_claimed": False,
        "artifact_filenames": [item.filename for item in candidate.artifacts],
        "authorization_response_kind": _ACCEPT_FOR_PROFILE[candidate.profile].value,
        "other_response_kinds": [
            PrivateDeliveryWriteResponseKind.REQUEST_CHANGES.value,
            PrivateDeliveryWriteResponseKind.CANCEL.value,
        ],
        "created_at": _utc_iso(created_at),
        "expires_at": _utc_iso(expires_at),
        "trip": candidate_private["trip"],
        "readiness": candidate_private["readiness"],
    }
    if "calendar_events" in candidate_private:
        payload["calendar_events"] = candidate_private["calendar_events"]
    encoded = _canonical_json_bytes(payload)
    if len(encoded) > MAX_PRIVATE_DELIVERY_WRITE_REVIEW_BYTES:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_REVIEW_LIMIT_EXCEEDED"
        )
    return encoded


def _require_same_source(
    first: _CanonicalSourceSnapshot,
    second: _CanonicalSourceSnapshot,
) -> None:
    if (
        first.directory_identity != second.directory_identity
        or first.source_identity != second.source_identity
        or first.source_digest != second.source_digest
        or first.raw != second.raw
    ):
        raise PrivateDeliveryWriteError("PRIVATE_DELIVERY_WRITE_SOURCE_STALE")


def _candidate_signature(candidate: PrivateDeliveryReview) -> tuple[object, ...]:
    candidate.to_safe_dict()
    return (
        candidate.profile,
        candidate.created_at,
        candidate.expires_at,
        candidate.canonical_source_sha256,
        candidate.plan_revision,
        candidate.composed_state_digest,
        candidate.evidence_binding_digest,
        candidate.runtime_context_sha256,
        candidate.review_id,
        tuple(
            (item.filename, item.media_type, item.sha256, item.payload)
            for item in candidate.artifacts
        ),
    )


def _require_same_candidate(
    first: PrivateDeliveryReview,
    second: PrivateDeliveryReview,
) -> None:
    if _candidate_signature(first) != _candidate_signature(second):
        raise PrivateDeliveryWriteError("PRIVATE_DELIVERY_WRITE_EVIDENCE_STALE")


def _require_candidate_matches_review(
    candidate: PrivateDeliveryReview,
    review: PrivateDeliveryWriteReview,
) -> None:
    if (
        candidate.review_id != review.candidate_review_id
        or candidate.profile is not review.profile
        or tuple(
            (item.filename, item.media_type, item.sha256, item.payload)
            for item in candidate.artifacts
        )
        != tuple(
            (item.filename, item.media_type, item.sha256, item.payload)
            for item in review.artifacts
        )
    ):
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_REVIEW_STALE"
        )


def _verify_review_shape(review: PrivateDeliveryWriteReview) -> None:
    if (
        type(review.profile) is not PrivateDeliveryProfile
        or type(review.contract_version) is not str
        or review.contract_version != PRIVATE_DELIVERY_WRITE_VERSION
        or type(review.artifacts) is not tuple
        or type(review.created_at) is not datetime
        or type(review.expires_at) is not datetime
        or type(review.source_path) is not str
        or type(review.target_path) is not str
        or type(review.private_review_json) is not bytes
        or not review.private_review_json
        or len(review.private_review_json) > MAX_PRIVATE_DELIVERY_WRITE_REVIEW_BYTES
        or type(review._controller) is not _WriteController
    ):
        raise ValueError("private delivery write review shape is invalid")
    _validate_digest(review.candidate_review_id)
    _validate_digest(review.review_id)
    created = _exact_factory_utc(review.created_at)
    expires = _exact_factory_utc(review.expires_at)
    if expires <= created or expires > created + PRIVATE_DELIVERY_WRITE_REVIEW_TTL:
        raise ValueError("private delivery write review lifetime is invalid")
    expected_names = (
        ("index.html", "manifest.json")
        if review.profile is PrivateDeliveryProfile.HTML_PREVIEW
        else ("index.html", "calendar.ics", "manifest.json")
    )
    if (
        len(review.artifacts) != len(expected_names)
        or any(type(item) is not PrivateDeliveryArtifact for item in review.artifacts)
        or tuple(item.filename for item in review.artifacts) != expected_names
    ):
        raise ValueError("private delivery write artifact set is invalid")


def _verify_response_shape(response: PrivateDeliveryWriteResponse) -> None:
    if (
        type(response.kind) is not PrivateDeliveryWriteResponseKind
        or type(response.profile) is not PrivateDeliveryProfile
        or type(response.accepted_for_prewrite_recheck) is not bool
        or type(response.contract_version) is not str
        or response.contract_version != PRIVATE_DELIVERY_WRITE_RESPONSE_VERSION
        or type(response._review) is not PrivateDeliveryWriteReview
    ):
        raise ValueError("private delivery write response shape is invalid")
    _validate_digest(response.review_id)
    _validate_digest(response.response_id)
    _exact_factory_utc(response.captured_at)
    if (
        response.review_id != response._review.review_id
        or response.profile is not response._review.profile
        or response.accepted_for_prewrite_recheck
        is not (response.kind is response._review.authorization_response_kind)
    ):
        raise ValueError("private delivery write response binding is invalid")


def _controller_fingerprint(controller: _WriteController) -> str:
    if (
        type(controller) is not _WriteController
        or type(controller.store) is not TripStore
        or type(controller.private_root) is not _PATH_TYPE
        or type(controller.target_leaf) is not str
        or _TARGET_LEAF_RE.fullmatch(controller.target_leaf) is None
        or type(controller.private_root_identity) is not tuple
        or len(controller.private_root_identity) != 4
        or any(type(item) is not int for item in controller.private_root_identity)
        or type(controller.source_directory_identity) is not tuple
        or len(controller.source_directory_identity) != 2
        or any(
            type(item) is not int
            for item in controller.source_directory_identity
        )
        or type(controller.source_identity) is not tuple
        or len(controller.source_identity) != 5
        or any(type(item) is not int for item in controller.source_identity)
        or type(controller.source_digest) is not str
        or not controller.source_digest.startswith("sha256:")
        or _SHA256_RE.fullmatch(controller.source_digest[7:]) is None
        or type(controller.availability_keys) is not tuple
        or id(controller.evidence_source) != controller.evidence_source_id
        or id(controller.clock) != controller.clock_id
        or not callable(controller.clock)
        or (
            controller.fault_hook is None
            and controller.fault_hook_id is not None
        )
        or (
            controller.fault_hook is not None
            and (
                not callable(controller.fault_hook)
                or id(controller.fault_hook) != controller.fault_hook_id
            )
        )
    ):
        raise ValueError("private delivery write controller drifted")
    paths = _validated_store_paths(controller.store)
    if paths != (
        controller.trips_root,
        controller.trip_dir,
        controller.data_dir,
        controller.plan_path,
    ):
        raise ValueError("private delivery write store drifted")
    return _digest_json(
        {
            "store_id": id(controller.store),
            "evidence_source_id": controller.evidence_source_id,
            "clock_id": controller.clock_id,
            "fault_hook_id": controller.fault_hook_id,
            "context_ids": [
                id(controller.availability_keys),
                id(controller.lodging_intake),
                id(controller.pending_lodging_review),
            ],
            "paths": [os.fspath(item) for item in paths],
            "private_root": os.fspath(controller.private_root),
            "target_leaf": controller.target_leaf,
            "private_root_identity": list(controller.private_root_identity),
            "source_directory_identity": list(
                controller.source_directory_identity
            ),
            "source_identity": list(controller.source_identity),
            "source_digest": controller.source_digest,
        },
        prefix="private-delivery-write-controller/v1",
    )


def _review_fingerprint(review: PrivateDeliveryWriteReview) -> str:
    _verify_review_shape(review)
    for artifact in review.artifacts:
        artifact.to_safe_dict()
    return _digest_json(
        {
            "contract_version": review.contract_version,
            "profile": review.profile.value,
            "created_at": _utc_iso(review.created_at),
            "expires_at": _utc_iso(review.expires_at),
            "source_path": review.source_path,
            "target_path": review.target_path,
            "candidate_review_id": review.candidate_review_id,
            "review_id": review.review_id,
            "private_review_sha256": hashlib.sha256(
                review.private_review_json
            ).hexdigest(),
            "artifacts": [
                [item.filename, item.media_type, item.sha256]
                for item in review.artifacts
            ],
            "controller": _controller_fingerprint(review._controller),
        },
        prefix="private-delivery-write-review-seal/v1",
    )


def _response_fingerprint(response: PrivateDeliveryWriteResponse) -> str:
    _verify_response_shape(response)
    return _digest_json(
        {
            "contract_version": response.contract_version,
            "kind": response.kind.value,
            "profile": response.profile.value,
            "accepted": response.accepted_for_prewrite_recheck,
            "review_id": response.review_id,
            "response_id": response.response_id,
            "captured_at": _utc_iso(response.captured_at),
            "review_object_id": id(response._review),
        },
        prefix="private-delivery-write-response-seal/v1",
    )


def _verify_outcome_shape(outcome: PrivateDeliveryWriteOutcome) -> None:
    if (
        type(outcome) is not PrivateDeliveryWriteOutcome
        or type(outcome.kind) is not PrivateDeliveryWriteOutcomeKind
        or type(outcome.profile) is not PrivateDeliveryProfile
        or type(outcome.filesystem_mutation_started) is not bool
        or (
            outcome.final_target_published is not None
            and type(outcome.final_target_published) is not bool
        )
        or type(outcome.cleanup_complete) is not bool
        or type(outcome.recovery_required) is not bool
        or type(outcome.contract_version) is not str
        or outcome.contract_version != PRIVATE_DELIVERY_WRITE_OUTCOME_VERSION
    ):
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_OUTCOME_TAMPERED"
        )
    expected_flags = {
        PrivateDeliveryWriteOutcomeKind.CREATED: (True, True, True, False),
        PrivateDeliveryWriteOutcomeKind.REQUEST_CHANGES: (
            False,
            False,
            True,
            False,
        ),
        PrivateDeliveryWriteOutcomeKind.CANCELLED: (
            False,
            False,
            True,
            False,
        ),
        PrivateDeliveryWriteOutcomeKind.PARTIAL_UNCOMMITTED: (
            True,
            False,
            False,
            True,
        ),
        PrivateDeliveryWriteOutcomeKind.OUTCOME_UNKNOWN: (
            True,
            None,
            False,
            True,
        ),
        PrivateDeliveryWriteOutcomeKind.RECONCILED_CREATED: (
            True,
            True,
            True,
            False,
        ),
        PrivateDeliveryWriteOutcomeKind.RECOVERY_CONFLICT: (
            True,
            None,
            False,
            True,
        ),
    }
    if (
        outcome.filesystem_mutation_started,
        outcome.final_target_published,
        outcome.cleanup_complete,
        outcome.recovery_required,
    ) != expected_flags[outcome.kind]:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_OUTCOME_TAMPERED"
        )


def _outcome_fingerprint(outcome: PrivateDeliveryWriteOutcome) -> str:
    _verify_outcome_shape(outcome)
    return _digest_json(
        {
            "contract_version": outcome.contract_version,
            "kind": outcome.kind.value,
            "profile": outcome.profile.value,
            "filesystem_mutation_started": (
                outcome.filesystem_mutation_started
            ),
            "final_target_published": outcome.final_target_published,
            "cleanup_complete": outcome.cleanup_complete,
            "recovery_required": outcome.recovery_required,
        },
        prefix="private-delivery-write-outcome-seal/v1",
    )


def _register_outcome(outcome: PrivateDeliveryWriteOutcome) -> None:
    fingerprint = _outcome_fingerprint(outcome)
    key = id(outcome)
    with _REGISTRY_LOCK:
        if key in _OUTCOME_REGISTRY:
            raise PrivateDeliveryWriteError(
                "PRIVATE_DELIVERY_WRITE_OUTCOME_REGISTRY_CONFLICT"
            )

        def remove(
            reference: weakref.ReferenceType[PrivateDeliveryWriteOutcome],
        ) -> None:
            del reference
            with _REGISTRY_LOCK:
                _OUTCOME_REGISTRY.pop(key, None)

        _OUTCOME_REGISTRY[key] = _WriteOutcomeRecord(
            outcome_ref=weakref.ref(outcome, remove),
            fingerprint=fingerprint,
        )


def _verified_outcome_record(
    outcome: object,
) -> _WriteOutcomeRecord:
    if type(outcome) is not PrivateDeliveryWriteOutcome:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_OUTCOME_REQUIRED"
        )
    with _REGISTRY_LOCK:
        record = _OUTCOME_REGISTRY.get(id(outcome))
    if record is None or record.outcome_ref() is not outcome:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_OUTCOME_UNREGISTERED"
        )
    try:
        fingerprint = _outcome_fingerprint(outcome)
    except Exception:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_OUTCOME_TAMPERED"
        ) from None
    if fingerprint != record.fingerprint:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_OUTCOME_TAMPERED"
        )
    return record


def _set_record_outcome(
    record: _WriteReviewRecord,
    outcome: PrivateDeliveryWriteOutcome,
) -> None:
    outcome_record = _verified_outcome_record(outcome)
    record.last_outcome = outcome
    record.last_outcome_fingerprint = outcome_record.fingerprint


def _record_safe_status(record: _WriteReviewRecord) -> dict[str, object]:
    outcome = record.last_outcome
    if outcome is None:
        return {
            "response_consumed": record.execution_state != "pending",
            "filesystem_mutation_started": False,
            "final_target_published": False,
            "cleanup_complete": True,
            "cleanup_outstanding": False,
            "recovery_required": False,
            "writes_performed": False,
            "write_outcome": "not_performed",
        }
    outcome_record = _verified_outcome_record(outcome)
    if record.last_outcome_fingerprint != outcome_record.fingerprint:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_OUTCOME_TAMPERED"
        )
    return {
        "response_consumed": record.execution_state != "pending",
        "filesystem_mutation_started": outcome.filesystem_mutation_started,
        "final_target_published": outcome.final_target_published,
        "cleanup_complete": outcome.cleanup_complete,
        "cleanup_outstanding": not outcome.cleanup_complete,
        "recovery_required": outcome.recovery_required,
        "writes_performed": outcome.filesystem_mutation_started,
        "write_outcome": outcome.kind.value,
    }


def _register_review(review: PrivateDeliveryWriteReview) -> None:
    fingerprint = _review_fingerprint(review)
    with _REGISTRY_LOCK:
        if len(_REVIEW_REGISTRY) >= MAX_PRIVATE_DELIVERY_WRITE_ACTIVE_REVIEWS:
            raise PrivateDeliveryWriteError(
                "PRIVATE_DELIVERY_WRITE_REVIEW_LIMIT_EXCEEDED"
            )
        key = id(review)
        if key in _REVIEW_REGISTRY:
            raise PrivateDeliveryWriteError(
                "PRIVATE_DELIVERY_WRITE_REVIEW_REGISTRY_CONFLICT"
            )

        def remove(reference: weakref.ReferenceType[PrivateDeliveryWriteReview]) -> None:
            del reference
            with _REGISTRY_LOCK:
                _REVIEW_REGISTRY.pop(key, None)

        _REVIEW_REGISTRY[key] = _WriteReviewRecord(
            review_ref=weakref.ref(review, remove),
            fingerprint=fingerprint,
            last_checked_at=review.created_at,
        )


def _register_response(response: PrivateDeliveryWriteResponse) -> None:
    fingerprint = _response_fingerprint(response)
    with _REGISTRY_LOCK:
        key = id(response)
        if key in _RESPONSE_REGISTRY:
            raise PrivateDeliveryWriteError(
                "PRIVATE_DELIVERY_WRITE_RESPONSE_REGISTRY_CONFLICT"
            )

        def remove(reference: weakref.ReferenceType[PrivateDeliveryWriteResponse]) -> None:
            del reference
            with _REGISTRY_LOCK:
                _RESPONSE_REGISTRY.pop(key, None)

        _RESPONSE_REGISTRY[key] = _WriteResponseRecord(
            response_ref=weakref.ref(response, remove),
            fingerprint=fingerprint,
        )


def _verified_review_record(review: object) -> _WriteReviewRecord:
    if type(review) is not PrivateDeliveryWriteReview:
        raise PrivateDeliveryWriteError("PRIVATE_DELIVERY_WRITE_REVIEW_REQUIRED")
    with _REGISTRY_LOCK:
        record = _REVIEW_REGISTRY.get(id(review))
    if record is None:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_REVIEW_UNREGISTERED"
        )
    if (
        type(record) is not _WriteReviewRecord
        or type(record.review_ref) is not weakref.ReferenceType
        or type(record.transition_active) is not bool
        or type(record.transition_poisoned) is not bool
        or type(record.lock) is not _RLOCK_TYPE
    ):
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_REVIEW_TAMPERED"
        )
    if record.review_ref() is not review:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_REVIEW_UNREGISTERED"
        )
    _require_review_seal(review, record)
    return record


def _begin_review_transition(record: _WriteReviewRecord) -> None:
    if record.transition_active:
        record.transition_poisoned = True
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_REENTRANT_OPERATION"
        )
    if record.transition_poisoned:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_REVIEW_TAMPERED"
        )
    record.transition_active = True


def _require_clean_review_transition(record: _WriteReviewRecord) -> None:
    if not record.transition_active or record.transition_poisoned:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_REENTRANT_OPERATION"
        )


def _end_review_transition(record: _WriteReviewRecord) -> None:
    record.transition_active = False
    record.transition_poisoned = False


def _require_review_seal(
    review: PrivateDeliveryWriteReview,
    record: _WriteReviewRecord,
) -> None:
    try:
        fingerprint = _review_fingerprint(review)
    except Exception:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_REVIEW_TAMPERED"
        ) from None
    if fingerprint != record.fingerprint:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_REVIEW_TAMPERED"
        )


def _verified_response_record(response: object) -> _WriteResponseRecord:
    if type(response) is not PrivateDeliveryWriteResponse:
        raise PrivateDeliveryWriteError("PRIVATE_DELIVERY_WRITE_RESPONSE_REQUIRED")
    with _REGISTRY_LOCK:
        record = _RESPONSE_REGISTRY.get(id(response))
    if record is None or record.response_ref() is not response:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_RESPONSE_UNREGISTERED"
        )
    _require_response_seal(response, record)
    return record


def _require_response_seal(
    response: PrivateDeliveryWriteResponse,
    record: _WriteResponseRecord,
) -> None:
    try:
        fingerprint = _response_fingerprint(response)
    except Exception:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_RESPONSE_TAMPERED"
        ) from None
    if fingerprint != record.fingerprint:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_RESPONSE_TAMPERED"
        )


def _sample_clock(clock: object) -> datetime:
    if not callable(clock):
        raise PrivateDeliveryWriteError("PRIVATE_DELIVERY_WRITE_CLOCK_INVALID")
    try:
        value = clock()
        if type(value) is not datetime or value.tzinfo is None:
            raise ValueError("clock must return aware datetime")
        offset = value.utcoffset()
        if offset is None:
            raise ValueError("clock offset missing")
        normalized = value.astimezone(_UTC)
    except Exception:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_CLOCK_INVALID"
        ) from None
    return normalized


def _exact_factory_utc(value: object) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is not _UTC
        or value.fold != 0
    ):
        raise ValueError("timestamp is not exact factory UTC")
    return value


def _fault(controller: _WriteController, stage: str) -> None:
    if controller.fault_hook is not None:
        controller.fault_hook(stage)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _validate_digest(value: object) -> None:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError("expected lowercase SHA-256")


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except Exception:
        raise PrivateDeliveryWriteError(
            "PRIVATE_DELIVERY_WRITE_REVIEW_INVALID"
        ) from None


def _digest_json(value: object, *, prefix: str) -> str:
    return hashlib.sha256(
        prefix.encode("ascii") + b"\n" + _canonical_json_bytes(value)
    ).hexdigest()


def _utc_iso(value: datetime) -> str:
    return value.astimezone(_UTC).isoformat().replace("+00:00", "Z")


def _outcome(
    profile: PrivateDeliveryProfile,
    kind: PrivateDeliveryWriteOutcomeKind,
    *,
    mutation: bool,
    published: bool | None,
    cleanup: bool,
    recovery: bool,
) -> PrivateDeliveryWriteOutcome:
    outcome = PrivateDeliveryWriteOutcome(
        kind=kind,
        profile=profile,
        filesystem_mutation_started=mutation,
        final_target_published=published,
        cleanup_complete=cleanup,
        recovery_required=recovery,
        _token=_OUTCOME_TOKEN,
    )
    _register_outcome(outcome)
    return outcome


__all__ = [
    "MAX_PRIVATE_DELIVERY_TARGET_LEAF_BYTES",
    "MAX_PRIVATE_DELIVERY_WRITE_REVIEW_BYTES",
    "PRIVATE_DELIVERY_WRITE_OUTCOME_VERSION",
    "PRIVATE_DELIVERY_WRITE_RESPONSE_VERSION",
    "PRIVATE_DELIVERY_WRITE_REVIEW_TTL",
    "PRIVATE_DELIVERY_WRITE_VERSION",
    "PrivateDeliveryReadOnlyEvidenceSource",
    "PrivateDeliveryWriteError",
    "PrivateDeliveryWriteOutcome",
    "PrivateDeliveryWriteOutcomeKind",
    "PrivateDeliveryWriteResponse",
    "PrivateDeliveryWriteResponseKind",
    "PrivateDeliveryWriteReview",
    "capture_private_delivery_write_response",
    "execute_private_delivery_write_response",
    "prepare_private_delivery_write_review",
    "reconcile_private_delivery_write_response",
    "verify_private_delivery_write_response",
]
