"""Host-confirmed canonical lodging selection and apply staging.

Phase 4.5C recommendations remain advisory.  This module is the separate
Phase 4.5D authority seam: it converts an exact, redacted lodging selection
into one typed canonical mutation, previews that mutation, waits for a
host-issued human confirmation grant, then re-previews and delegates the
atomic write to :class:`TripStore`.

Raw lodging labels, addresses, coordinates, prices, booking links, provider
tokens, and process-local candidate IDs never enter the generated patch,
review serialization, canonical lodging record, or receipt.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol

from .codec import canonical_json_bytes
from .facts import EvidenceSnapshot, FactContractError
from .lodging import LodgingKind
from .lodging_evidence import (
    LodgingComparisonAssessment,
    LodgingComparisonCandidate,
)
from .lodging_itinerary import (
    LodgingItineraryAssessment,
    LodgingItineraryOption,
    LodgingOptionDisposition,
)
from .models import DecisionState
from .mutations import (
    ApprovalGrant,
    ConfirmedLodgingStay,
    LodgingAnchorAssignment,
    LodgingConfirmationGrant,
    PlanPatch,
    SetLodgingSelection,
    _mint_lodging_confirmation_grant,
    lodging_confirmation_grant_payload,
    patch_digest,
)
from .scheduling import (
    ScheduleStatus,
    replay_schedule_candidate,
)
from .store import StoreResult


LODGING_CONFIRMATION_VERSION = "lodging-confirmation/v1"
_MAX_STAYS = 128
_MAX_ANCHORS = 128
_MAX_TEXT = 256
_REVIEW_LIFETIME = timedelta(minutes=30)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_PATCH_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_MACHINE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_CANONICAL_LODGING_LOCATION_RE = re.compile(
    r"lodging-location-[0-9a-f]{64}"
)
_CONFIRMATION_ONLY_ISSUES = frozenset(
    {"REPORTED_LODGING_DECISION_AWAITS_CONFIRMATION"}
)


class LodgingConfirmationState(str, Enum):
    """Stable staging outcomes without exposing canonical private data."""

    WAITING_CONFIRMATION = "waiting_confirmation"
    WAITING_APPROVAL = "waiting_approval"
    APPLIED = "applied"
    REPLAY_CONFIRMED = "replay_confirmed"
    NO_OP = "no_op"
    OUTCOME_UNKNOWN = "outcome_unknown"
    REJECTED = "rejected"


def _canonical_digest(value: Any, *, prefix: str) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(
        prefix.encode("utf-8") + b"\n" + encoded
    ).hexdigest()


def _text(value: object, name: str, *, maximum: int = _MAX_TEXT) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
    ):
        raise ValueError(f"{name} must be bounded visible text")
    return normalized


def _machine_id(value: object, name: str) -> str:
    normalized = _text(value, name)
    if _MACHINE_ID_RE.fullmatch(normalized) is None:
        raise ValueError(f"{name} must be a bounded machine identifier")
    return normalized


def _digest(value: object, name: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _patch_digest(value: object, name: str) -> str:
    if type(value) is not str or _PATCH_DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a prefixed SHA-256 digest")
    return value


def _exact_date(value: object, name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{name} must be an exact date")
    return value


def _utc(value: object, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _state_digest(plan: Mapping[str, Any]) -> str:
    state = plan.get("state")
    if not isinstance(state, Mapping):
        raise ValueError("candidate plan has no canonical state")
    return hashlib.sha256(canonical_json_bytes(state)).hexdigest()


def canonical_lodging_location_id(value: object) -> str:
    """Allocate a durable opaque ID without deriving it from private input."""

    normalized = _text(
        value,
        "lodging location reference",
        maximum=1024,
    )
    if _CANONICAL_LODGING_LOCATION_RE.fullmatch(normalized) is not None:
        return normalized
    return "lodging-location-" + secrets.token_hex(32)


def _safe_problem_code(value: object) -> str:
    if type(value) is not str:
        return "LODGING_CONFIRMATION_FAILED"
    normalized = unicodedata.normalize("NFC", value).strip()
    if _MACHINE_ID_RE.fullmatch(normalized) is None:
        return "LODGING_CONFIRMATION_FAILED"
    return normalized


@dataclass(frozen=True, slots=True)
class LodgingConfirmationProblem:
    """Secret-free reason a confirmation cannot proceed."""

    code: str
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "code",
            _safe_problem_code(self.code),
        )
        object.__setattr__(
            self,
            "message",
            _text(self.message, "LodgingConfirmationProblem.message", maximum=512),
        )

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True, repr=False)
class LodgingSelectionSegment:
    """One redacted, durable lodging choice for a contiguous date span."""

    location_id: str = field(repr=False)
    check_in: date
    check_out: date
    kind: LodgingKind
    decision_state: DecisionState
    lodging_id: str = ""

    def __post_init__(self) -> None:
        location_id = canonical_lodging_location_id(self.location_id)
        check_in = _exact_date(
            self.check_in,
            "LodgingSelectionSegment.check_in",
        )
        check_out = _exact_date(
            self.check_out,
            "LodgingSelectionSegment.check_out",
        )
        if check_out <= check_in:
            raise ValueError("lodging check-out must be after check-in")
        if type(self.kind) is not LodgingKind:
            raise TypeError("LodgingSelectionSegment.kind must be exact")
        if type(self.decision_state) is not DecisionState:
            raise TypeError(
                "LodgingSelectionSegment.decision_state must be exact"
            )
        if self.decision_state not in {
            DecisionState.SELECTED,
            DecisionState.FIXED,
            DecisionState.BOOKED,
        }:
            raise ValueError(
                "confirmed lodging must be selected, fixed, or booked"
            )
        expected = "lodging-" + _canonical_digest(
            {
                "location_id": location_id,
                "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
                "kind": self.kind.value,
            },
            prefix="canonical-lodging-id",
        )
        if self.lodging_id and self.lodging_id != expected:
            raise ValueError(
                "LodgingSelectionSegment.lodging_id differs from content"
            )
        object.__setattr__(self, "location_id", location_id)
        object.__setattr__(self, "check_in", check_in)
        object.__setattr__(self, "check_out", check_out)
        object.__setattr__(self, "lodging_id", expected)

    def to_binding_dict(self) -> dict[str, Any]:
        return {
            "lodging_id": self.lodging_id,
            "location_id": self.location_id,
            "check_in": self.check_in.isoformat(),
            "check_out": self.check_out.isoformat(),
            "kind": self.kind.value,
            "decision_state": self.decision_state.value,
            "evidence_state": "unverified",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "lodging_id": self.lodging_id,
            "check_in": self.check_in.isoformat(),
            "check_out": self.check_out.isoformat(),
            "kind": self.kind.value,
            "decision_state": self.decision_state.value,
            "evidence_state": "unverified",
            "has_canonical_location": True,
        }

    def to_core(self) -> ConfirmedLodgingStay:
        return ConfirmedLodgingStay(**self.to_binding_dict())


@dataclass(frozen=True, slots=True)
class LodgingSelectionAnchor:
    """A day role bound to confirmed lodging IDs, never candidate IDs."""

    day_id: str
    start_lodging_id: str | None = None
    end_lodging_id: str | None = None

    def __post_init__(self) -> None:
        day_id = _machine_id(
            self.day_id,
            "LodgingSelectionAnchor.day_id",
        )
        start = (
            _machine_id(
                self.start_lodging_id,
                "LodgingSelectionAnchor.start_lodging_id",
            )
            if self.start_lodging_id is not None
            else None
        )
        end = (
            _machine_id(
                self.end_lodging_id,
                "LodgingSelectionAnchor.end_lodging_id",
            )
            if self.end_lodging_id is not None
            else None
        )
        if start is None and end is None:
            raise ValueError("a lodging anchor requires a start or end role")
        object.__setattr__(self, "day_id", day_id)
        object.__setattr__(self, "start_lodging_id", start)
        object.__setattr__(self, "end_lodging_id", end)

    def to_dict(self) -> dict[str, Any]:
        return {
            "day_id": self.day_id,
            "start_lodging_id": self.start_lodging_id,
            "end_lodging_id": self.end_lodging_id,
        }

    def to_core(self) -> LodgingAnchorAssignment:
        return LodgingAnchorAssignment(**self.to_dict())


@dataclass(frozen=True, slots=True, repr=False)
class LodgingConfirmationRequest:
    """Exact canonical lodging intent awaiting a separate human grant."""

    trip_id: str
    base_revision: str
    idempotency_key: str = field(repr=False)
    segments: tuple[LodgingSelectionSegment, ...]
    anchors: tuple[LodgingSelectionAnchor, ...]
    evaluation_at: datetime
    selection_binding_digest: str = ""
    reviewed_itinerary: bool = False
    request_id: str = ""
    contract_version: str = LODGING_CONFIRMATION_VERSION

    def __post_init__(self) -> None:
        trip_id = _machine_id(
            self.trip_id,
            "LodgingConfirmationRequest.trip_id",
        )
        base_revision = _digest(
            self.base_revision,
            "LodgingConfirmationRequest.base_revision",
        )
        idempotency_key = _machine_id(
            self.idempotency_key,
            "LodgingConfirmationRequest.idempotency_key",
        )
        if (
            not isinstance(self.segments, tuple)
            or any(
                type(item) is not LodgingSelectionSegment
                for item in self.segments
            )
        ):
            raise TypeError(
                "segments must contain exact LodgingSelectionSegment values"
            )
        if not self.segments or len(self.segments) > _MAX_STAYS:
            raise ValueError("segments are outside the bounded contract")
        segments = tuple(
            sorted(
                self.segments,
                key=lambda item: (
                    item.check_in,
                    item.check_out,
                    item.lodging_id,
                ),
            )
        )
        if len({item.lodging_id for item in segments}) != len(segments):
            raise ValueError("segments cannot repeat a lodging ID")
        for left, right in zip(segments, segments[1:]):
            if left.check_out != right.check_in:
                raise ValueError(
                    "confirmed lodging segments must be contiguous "
                    "without gaps or overlaps"
                )
        if (
            not isinstance(self.anchors, tuple)
            or any(
                type(item) is not LodgingSelectionAnchor
                for item in self.anchors
            )
        ):
            raise TypeError(
                "anchors must contain exact LodgingSelectionAnchor values"
            )
        if not self.anchors or len(self.anchors) > _MAX_ANCHORS:
            raise ValueError("anchors are outside the bounded contract")
        anchors = tuple(sorted(self.anchors, key=lambda item: item.day_id))
        if len({item.day_id for item in anchors}) != len(anchors):
            raise ValueError("anchors cannot repeat a day")
        lodging_ids = {item.lodging_id for item in segments}
        referenced = {
            lodging_id
            for anchor in anchors
            for lodging_id in (
                anchor.start_lodging_id,
                anchor.end_lodging_id,
            )
            if lodging_id is not None
        }
        if not referenced.issubset(lodging_ids):
            raise ValueError("anchors reference an unknown lodging segment")
        evaluation_at = _utc(
            self.evaluation_at,
            "LodgingConfirmationRequest.evaluation_at",
        )
        if type(self.reviewed_itinerary) is not bool:
            raise TypeError("reviewed_itinerary must be bool")
        direct_binding = _canonical_digest(
            {
                "segments": [
                    item.to_binding_dict() for item in segments
                ],
                "anchors": [item.to_dict() for item in anchors],
            },
            prefix="direct-lodging-selection-binding",
        )
        if self.reviewed_itinerary:
            selection_binding = _digest(
                self.selection_binding_digest,
                "LodgingConfirmationRequest.selection_binding_digest",
            )
        else:
            if (
                self.selection_binding_digest
                and self.selection_binding_digest != direct_binding
            ):
                raise ValueError(
                    "Direct lodging selection binding differs from content"
                )
            selection_binding = direct_binding
        if self.contract_version != LODGING_CONFIRMATION_VERSION:
            raise ValueError("unsupported lodging confirmation version")
        expected = _canonical_digest(
            {
                "contract_version": self.contract_version,
                "trip_id": trip_id,
                "base_revision": base_revision,
                "idempotency_key": idempotency_key,
                "segments": [
                    item.to_binding_dict() for item in segments
                ],
                "anchors": [item.to_dict() for item in anchors],
                "evaluation_at": evaluation_at.isoformat(),
                "selection_binding_digest": selection_binding,
                "reviewed_itinerary": self.reviewed_itinerary,
            },
            prefix="lodging-confirmation-request",
        )
        if self.request_id and self.request_id != expected:
            raise ValueError(
                "LodgingConfirmationRequest.request_id differs from content"
            )
        object.__setattr__(self, "trip_id", trip_id)
        object.__setattr__(self, "base_revision", base_revision)
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(self, "segments", segments)
        object.__setattr__(self, "anchors", anchors)
        object.__setattr__(self, "evaluation_at", evaluation_at)
        object.__setattr__(
            self,
            "selection_binding_digest",
            selection_binding,
        )
        object.__setattr__(self, "request_id", expected)

    @property
    def stay_start(self) -> date:
        return self.segments[0].check_in

    @property
    def stay_end(self) -> date:
        return self.segments[-1].check_out

    def to_patch(self) -> PlanPatch:
        operation = SetLodgingSelection(
            op_id="confirm-lodging-selection",
            stays=tuple(item.to_core() for item in self.segments),
            anchors=tuple(item.to_core() for item in self.anchors),
            selection_binding_digest=self.selection_binding_digest,
        )
        return PlanPatch(
            trip_id=self.trip_id,
            base_revision=self.base_revision,
            idempotency_key=self.idempotency_key,
            operations=(operation,),
            intent="Apply an exact human-reviewed lodging selection.",
        )

    def __repr__(self) -> str:
        return (
            "LodgingConfirmationRequest("
            f"request_id={self.request_id!r}, "
            f"stay_count={len(self.segments)!r}, "
            f"anchor_count={len(self.anchors)!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "trip_id": self.trip_id,
            "base_revision": self.base_revision,
            "segments": [item.to_dict() for item in self.segments],
            "anchors": [item.to_dict() for item in self.anchors],
            "stay_night_count": (self.stay_end - self.stay_start).days,
            "selection_binding_digest": self.selection_binding_digest,
            "reviewed_itinerary": self.reviewed_itinerary,
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True, slots=True, repr=False)
class LodgingConfirmationReview:
    """Safe, expiring review over one exact canonical effect."""

    state: LodgingConfirmationState
    request_id: str
    trip_id: str
    base_revision: str
    created_at: datetime
    expires_at: datetime
    stay_count: int
    stay_night_count: int
    affected_day_ids: tuple[str, ...] = ()
    decision_states: tuple[str, ...] = ()
    reviewed_itinerary: bool = False
    patch_digest: str | None = None
    required_lodging_confirmation_scope: str | None = None
    required_store_approval_scope: str | None = None
    expected_state_digest: str | None = None
    expected_applied_revision: str | None = None
    check_status: str | None = None
    review_id: str | None = None
    problems: tuple[LodgingConfirmationProblem, ...] = ()

    def __post_init__(self) -> None:
        if type(self.state) is not LodgingConfirmationState:
            raise TypeError("LodgingConfirmationReview.state must be exact")
        request_id = _digest(self.request_id, "request_id")
        trip_id = _machine_id(self.trip_id, "trip_id")
        base_revision = _digest(self.base_revision, "base_revision")
        created_at = _utc(self.created_at, "created_at")
        expires_at = _utc(self.expires_at, "expires_at")
        if expires_at != created_at + _REVIEW_LIFETIME:
            raise ValueError("lodging review lifetime must be exactly 30 minutes")
        for value, name in (
            (self.stay_count, "stay_count"),
            (self.stay_night_count, "stay_night_count"),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if (
            not isinstance(self.affected_day_ids, tuple)
            or any(type(item) is not str for item in self.affected_day_ids)
        ):
            raise TypeError("affected_day_ids must be a text tuple")
        affected = tuple(
            sorted(
                {
                    _machine_id(item, "affected_day_ids item")
                    for item in self.affected_day_ids
                }
            )
        )
        allowed_decisions = {
            item.value
            for item in (
                DecisionState.SELECTED,
                DecisionState.FIXED,
                DecisionState.BOOKED,
            )
        }
        if (
            not isinstance(self.decision_states, tuple)
            or any(item not in allowed_decisions for item in self.decision_states)
        ):
            raise ValueError("decision_states contain an invalid value")
        decisions = tuple(sorted(set(self.decision_states)))
        if type(self.reviewed_itinerary) is not bool:
            raise TypeError("reviewed_itinerary must be bool")
        patch_value = (
            _patch_digest(self.patch_digest, "patch_digest")
            if self.patch_digest is not None
            else None
        )
        lodging_scope = (
            _patch_digest(
                self.required_lodging_confirmation_scope,
                "required_lodging_confirmation_scope",
            )
            if self.required_lodging_confirmation_scope is not None
            else None
        )
        store_scope = (
            _patch_digest(
                self.required_store_approval_scope,
                "required_store_approval_scope",
            )
            if self.required_store_approval_scope is not None
            else None
        )
        expected_state = (
            _digest(self.expected_state_digest, "expected_state_digest")
            if self.expected_state_digest is not None
            else None
        )
        expected_revision = (
            _digest(
                self.expected_applied_revision,
                "expected_applied_revision",
            )
            if self.expected_applied_revision is not None
            else None
        )
        if self.check_status is not None:
            _machine_id(self.check_status, "check_status")
        if (
            not isinstance(self.problems, tuple)
            or any(
                type(item) is not LodgingConfirmationProblem
                for item in self.problems
            )
        ):
            raise TypeError("problems must contain exact problem values")
        complete = (
            patch_value,
            lodging_scope,
            expected_state,
            expected_revision,
            self.check_status,
        )
        if self.state is LodgingConfirmationState.WAITING_CONFIRMATION:
            if any(item is None for item in complete) or self.problems:
                raise ValueError(
                    "a confirmation-ready review requires an exact effect"
                )
        elif self.state is LodgingConfirmationState.REJECTED:
            if not self.problems:
                raise ValueError("a rejected review requires a problem")
        elif self.state not in {
            LodgingConfirmationState.NO_OP,
            LodgingConfirmationState.REPLAY_CONFIRMED,
        }:
            raise ValueError("review has a non-staging state")
        payload = {
            "state": self.state.value,
            "request_id": request_id,
            "trip_id": trip_id,
            "base_revision": base_revision,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "stay_count": self.stay_count,
            "stay_night_count": self.stay_night_count,
            "affected_day_ids": list(affected),
            "decision_states": list(decisions),
            "reviewed_itinerary": self.reviewed_itinerary,
            "patch_digest": patch_value,
            "required_lodging_confirmation_scope": lodging_scope,
            "required_store_approval_scope": store_scope,
            "expected_state_digest": expected_state,
            "expected_applied_revision": expected_revision,
            "check_status": self.check_status,
            "problem_codes": [item.code for item in self.problems],
        }
        expected_review_id = _canonical_digest(
            payload,
            prefix="lodging-confirmation-review",
        )
        if self.review_id and self.review_id != expected_review_id:
            raise ValueError(
                "LodgingConfirmationReview.review_id differs from content"
            )
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "trip_id", trip_id)
        object.__setattr__(self, "base_revision", base_revision)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "affected_day_ids", affected)
        object.__setattr__(self, "decision_states", decisions)
        object.__setattr__(self, "patch_digest", patch_value)
        object.__setattr__(
            self,
            "required_lodging_confirmation_scope",
            lodging_scope,
        )
        object.__setattr__(
            self,
            "required_store_approval_scope",
            store_scope,
        )
        object.__setattr__(self, "expected_state_digest", expected_state)
        object.__setattr__(
            self,
            "expected_applied_revision",
            expected_revision,
        )
        object.__setattr__(self, "review_id", expected_review_id)

    @property
    def ready_for_confirmation(self) -> bool:
        return self.state is LodgingConfirmationState.WAITING_CONFIRMATION

    def __repr__(self) -> str:
        return (
            "LodgingConfirmationReview("
            f"review_id={self.review_id!r}, state={self.state.value!r}, "
            f"stay_count={self.stay_count!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "review_id": self.review_id,
            "request_id": self.request_id,
            "trip_id": self.trip_id,
            "base_revision": self.base_revision,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "stay_count": self.stay_count,
            "stay_night_count": self.stay_night_count,
            "affected_day_ids": list(self.affected_day_ids),
            "decision_states": list(self.decision_states),
            "reviewed_itinerary": self.reviewed_itinerary,
            "patch_digest": self.patch_digest,
            "required_lodging_confirmation_scope": (
                self.required_lodging_confirmation_scope
            ),
            "required_store_approval_scope": (
                self.required_store_approval_scope
            ),
            "expected_state_digest": self.expected_state_digest,
            "expected_applied_revision": self.expected_applied_revision,
            "check_status": self.check_status,
            "problems": [item.to_dict() for item in self.problems],
        }


@dataclass(frozen=True, slots=True, repr=False)
class LodgingConfirmationAuthority:
    """Host-owned signer, human identity, and trusted confirmation clock.

    The signer must live outside the AI-executable trust boundary.  It may use
    a hardware/service key or an opaque host-side grant registry.  The
    corresponding verifier is injected separately into :class:`TripStore`.
    """

    confirmed_by: str
    issuer_id: str
    signer: Callable[[bytes], str] = field(repr=False)
    clock: Callable[[], datetime] = field(repr=False)

    def __post_init__(self) -> None:
        confirmed_by = _text(
            self.confirmed_by,
            "LodgingConfirmationAuthority.confirmed_by",
        )
        issuer_id = _machine_id(
            self.issuer_id,
            "LodgingConfirmationAuthority.issuer_id",
        )
        if not callable(self.signer):
            raise TypeError("lodging confirmation authority requires a signer")
        if not callable(self.clock):
            raise TypeError("lodging confirmation authority requires a clock")
        object.__setattr__(self, "confirmed_by", confirmed_by)
        object.__setattr__(self, "issuer_id", issuer_id)

    def _now(self) -> datetime:
        try:
            value = self.clock()
        except Exception:
            raise FactContractError(
                "PENDING_REVIEW",
                "Trusted lodging confirmation clock failed.",
            ) from None
        return _utc(value, "trusted lodging confirmation clock")

    def issue_grant(
        self,
        review: LodgingConfirmationReview,
    ) -> LodgingConfirmationGrant:
        if (
            type(review) is not LodgingConfirmationReview
            or not review.ready_for_confirmation
            or review.review_id is None
            or review.patch_digest is None
            or review.required_lodging_confirmation_scope is None
        ):
            raise FactContractError(
                "PENDING_REVIEW",
                "A lodging grant requires an exact confirmation-ready review.",
            )
        confirmed_at = self._now()
        if not review.created_at <= confirmed_at <= review.expires_at:
            raise FactContractError(
                "PENDING_REVIEW",
                "Lodging confirmation review has expired.",
            )
        payload = lodging_confirmation_grant_payload(
            review_id=review.review_id,
            trip_id=review.trip_id,
            base_revision=review.base_revision,
            request_digest=review.patch_digest,
            scope_digest=review.required_lodging_confirmation_scope,
            confirmed_by=self.confirmed_by,
            confirmed_at=confirmed_at,
            expires_at=review.expires_at,
            issuer_id=self.issuer_id,
        )
        try:
            signature = _text(
                self.signer(payload),
                "host lodging confirmation signature",
                maximum=1024,
            )
        except Exception:
            raise FactContractError(
                "PENDING_REVIEW",
                "Trusted lodging confirmation signer failed.",
            ) from None
        return _mint_lodging_confirmation_grant(
            review_id=review.review_id,
            trip_id=review.trip_id,
            base_revision=review.base_revision,
            request_digest=review.patch_digest,
            scope_digest=review.required_lodging_confirmation_scope,
            confirmed_by=self.confirmed_by,
            confirmed_at=confirmed_at,
            expires_at=review.expires_at,
            issuer_id=self.issuer_id,
            signature=signature,
        )


class LodgingConfirmationRepository(Protocol):
    """Minimal store seam used by the in-memory confirmation stager."""

    def load_plan(self) -> Mapping[str, Any]: ...

    def preview_patch(
        self,
        patch: PlanPatch,
        approvals: Sequence[ApprovalGrant] = (),
        *,
        lodging_confirmations: Sequence[LodgingConfirmationGrant] = (),
        evaluation_at: datetime | None = None,
    ) -> StoreResult: ...

    def apply_patch(
        self,
        patch: PlanPatch,
        approvals: Sequence[ApprovalGrant] = (),
        *,
        lodging_confirmations: Sequence[LodgingConfirmationGrant] = (),
        dry_run: bool = False,
        evaluation_at: datetime | None = None,
    ) -> StoreResult: ...


@dataclass(frozen=True, slots=True)
class LodgingConfirmationResult:
    """Safe apply outcome; canonical data remains behind the repository."""

    state: LodgingConfirmationState
    review_id: str | None = None
    applied: bool = False
    candidate_is_current: bool = False
    transaction_id: str | None = None
    current_revision: str | None = None
    check_status: str | None = None
    pending_review_retained: bool = False
    problems: tuple[LodgingConfirmationProblem, ...] = ()

    def __post_init__(self) -> None:
        if type(self.state) is not LodgingConfirmationState:
            raise TypeError("LodgingConfirmationResult.state must be exact")
        if self.review_id is not None:
            _digest(self.review_id, "review_id")
        for value, name in (
            (self.applied, "applied"),
            (self.candidate_is_current, "candidate_is_current"),
            (self.pending_review_retained, "pending_review_retained"),
        ):
            if type(value) is not bool:
                raise TypeError(f"{name} must be bool")
        if self.current_revision is not None:
            _digest(self.current_revision, "current_revision")
        if self.transaction_id is not None:
            _machine_id(self.transaction_id, "transaction_id")
        if self.check_status is not None:
            _machine_id(self.check_status, "check_status")
        if (
            not isinstance(self.problems, tuple)
            or any(
                type(item) is not LodgingConfirmationProblem
                for item in self.problems
            )
        ):
            raise TypeError("problems must contain exact problem values")

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "review_id": self.review_id,
            "applied": self.applied,
            "candidate_is_current": self.candidate_is_current,
            "transaction_id": self.transaction_id,
            "current_revision": self.current_revision,
            "check_status": self.check_status,
            "pending_review_retained": self.pending_review_retained,
            "problems": [item.to_dict() for item in self.problems],
        }


@dataclass(slots=True)
class _PendingConfirmation:
    request: LodgingConfirmationRequest
    patch: PlanPatch
    review: LodgingConfirmationReview


class LodgingConfirmationStager:
    """One-process, one-pending-review lodging confirmation seam."""

    def __init__(
        self,
        repository: LodgingConfirmationRepository,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(clock):
            raise TypeError("LodgingConfirmationStager.clock must be callable")
        self._repository = repository
        self._clock = clock
        self._pending: _PendingConfirmation | None = None

    def _now(self) -> datetime:
        try:
            return _utc(self._clock(), "lodging staging clock")
        except Exception:
            raise FactContractError(
                "PENDING_REVIEW",
                "Trusted lodging staging clock failed.",
            ) from None

    @property
    def pending_review(self) -> LodgingConfirmationReview | None:
        return self._pending.review if self._pending is not None else None

    def stage(
        self,
        request: LodgingConfirmationRequest,
    ) -> LodgingConfirmationReview:
        if type(request) is not LodgingConfirmationRequest:
            raise TypeError("request must be exact LodgingConfirmationRequest")
        patch = request.to_patch()
        patch_value = patch_digest(patch)
        created_at = self._now()
        initial = self._repository.preview_patch(
            patch,
            evaluation_at=request.evaluation_at,
        )
        if initial.status in {"replayed", "replayed_rolled_back"}:
            self._pending = None
            if initial.status == "replayed_rolled_back":
                return _rejected_review(
                    request,
                    created_at,
                    "LODGING_CONFIRMATION_ROLLED_BACK",
                    "The exact lodging confirmation was applied and rolled back.",
                    patch_value=patch_value,
                )
            return _terminal_review(
                request,
                created_at,
                LodgingConfirmationState.REPLAY_CONFIRMED,
                patch_value=patch_value,
            )
        if initial.success and initial.status == "no_op":
            self._pending = None
            return _terminal_review(
                request,
                created_at,
                LodgingConfirmationState.NO_OP,
                patch_value=patch_value,
            )
        draft = initial.draft
        scope = (
            draft.required_lodging_confirmation_scope
            if draft is not None
            else initial.required_lodging_confirmation_scope
        )
        problem_codes = {
            item.code for item in initial.problems
        }
        allowed_pending = {
            "LODGING_CONFIRMATION_REQUIRED",
            "APPROVAL_REQUIRED",
        }
        if (
            draft is None
            or scope is None
            or not problem_codes
            or not problem_codes.issubset(allowed_pending)
        ):
            self._pending = None
            return _review_from_store_failure(
                request,
                created_at,
                initial,
                patch_value=patch_value,
            )

        preliminary_review_id = _canonical_digest(
            {
                "request_id": request.request_id,
                "patch_digest": patch_value,
                "scope_digest": scope,
            },
            prefix="lodging-preview-capability",
        )
        preview_grant = _mint_lodging_confirmation_grant(
            review_id=preliminary_review_id,
            trip_id=request.trip_id,
            base_revision=request.base_revision,
            request_digest=patch_value,
            scope_digest=scope,
            confirmed_by="host-preview-only",
            confirmed_at=created_at,
            expires_at=created_at + _REVIEW_LIFETIME,
            issuer_id="preview-only",
            signature="preview-only",
        )
        preview_approvals: tuple[ApprovalGrant, ...] = ()
        if draft.required_approval_scope is not None:
            preview_approvals = (
                ApprovalGrant(
                    approval_id="lodging-preview-only",
                    scope_digest=draft.required_approval_scope,
                    approved_by="host-preview-only",
                    approved_at=created_at.isoformat(),
                ),
            )
        preview = self._repository.preview_patch(
            patch,
            preview_approvals,
            lodging_confirmations=(preview_grant,),
            evaluation_at=request.evaluation_at,
        )
        if not preview.success or preview.status not in {
            "preview_ready",
            "no_op",
        }:
            self._pending = None
            return _review_from_store_failure(
                request,
                created_at,
                preview,
                patch_value=patch_value,
            )
        if preview.status == "no_op":
            self._pending = None
            return _terminal_review(
                request,
                created_at,
                LodgingConfirmationState.NO_OP,
                patch_value=patch_value,
            )
        candidate = preview.mutable_candidate_plan()
        if candidate is None:
            self._pending = None
            return _rejected_review(
                request,
                created_at,
                "LODGING_PREVIEW_CONTRACT_FAILED",
                "The repository preview omitted its canonical candidate.",
                patch_value=patch_value,
            )
        review = LodgingConfirmationReview(
            state=LodgingConfirmationState.WAITING_CONFIRMATION,
            request_id=request.request_id,
            trip_id=request.trip_id,
            base_revision=request.base_revision,
            created_at=created_at,
            expires_at=created_at + _REVIEW_LIFETIME,
            stay_count=len(request.segments),
            stay_night_count=(
                request.stay_end - request.stay_start
            ).days,
            affected_day_ids=(
                preview.draft.affected_day_ids
                if preview.draft is not None
                else ()
            ),
            decision_states=tuple(
                item.decision_state.value for item in request.segments
            ),
            reviewed_itinerary=request.reviewed_itinerary,
            patch_digest=patch_value,
            required_lodging_confirmation_scope=scope,
            required_store_approval_scope=preview.required_approval_scope,
            expected_state_digest=_state_digest(candidate),
            expected_applied_revision=preview.applied_revision,
            check_status=preview.check_status,
        )
        self._pending = _PendingConfirmation(
            request=request,
            patch=patch,
            review=review,
        )
        return review

    def commit(
        self,
        review_id: str,
        *,
        confirmation: LodgingConfirmationGrant | None = None,
        approvals: Sequence[ApprovalGrant] = (),
    ) -> LodgingConfirmationResult:
        review_id = _digest(review_id, "review_id")
        pending = self._pending
        if pending is None or pending.review.review_id != review_id:
            return _problem_result(
                LodgingConfirmationState.REJECTED,
                "UNKNOWN_LODGING_CONFIRMATION_REVIEW",
                "No pending lodging confirmation matches the review ID.",
                review_id=review_id,
            )
        review = pending.review
        now = self._now()
        if not _grant_matches_review(confirmation, review, now):
            return _problem_result(
                LodgingConfirmationState.WAITING_CONFIRMATION,
                "LODGING_CONFIRMATION_REQUIRED",
                "An exact, live host lodging confirmation is required.",
                review_id=review_id,
                pending=True,
            )
        assert type(confirmation) is LodgingConfirmationGrant
        effective_approvals = _protected_approvals_for_confirmation(
            review,
            confirmation,
            approvals,
        )
        preview = self._repository.preview_patch(
            pending.patch,
            effective_approvals,
            lodging_confirmations=(confirmation,),
            evaluation_at=pending.request.evaluation_at,
        )
        if preview.status == "replayed":
            self._pending = None
            current = (
                preview.current_revision is not None
                and preview.current_revision == preview.applied_revision
            )
            return LodgingConfirmationResult(
                state=LodgingConfirmationState.REPLAY_CONFIRMED,
                review_id=review_id,
                applied=True,
                candidate_is_current=current,
                transaction_id=preview.transaction_id,
                current_revision=preview.current_revision,
                check_status=preview.check_status,
            )
        if preview.status == "replayed_rolled_back":
            self._pending = None
            return _problem_result(
                LodgingConfirmationState.REJECTED,
                "LODGING_CONFIRMATION_ROLLED_BACK",
                "The exact lodging confirmation was applied and rolled back.",
                review_id=review_id,
                current_revision=preview.current_revision,
            )
        if (
            not preview.success
            and preview.required_approval_scope is not None
            and any(
                item.code
                in {"APPROVAL_REQUIRED", "APPROVAL_SCOPE_MISMATCH"}
                for item in preview.problems
            )
        ):
            return _problem_result(
                LodgingConfirmationState.WAITING_APPROVAL,
                "STORE_APPROVAL_REQUIRED",
                "The exact protected-change approval is still required.",
                review_id=review_id,
                pending=True,
            )
        mismatch = _preview_mismatch(preview, review)
        if mismatch is not None:
            self._pending = None
            return _problem_result(
                LodgingConfirmationState.REJECTED,
                mismatch.code,
                mismatch.message,
                review_id=review_id,
                current_revision=preview.current_revision,
            )
        result = self._repository.apply_patch(
            pending.patch,
            effective_approvals,
            lodging_confirmations=(confirmation,),
            evaluation_at=pending.request.evaluation_at,
        )
        if result.status == "applied":
            self._pending = None
            return LodgingConfirmationResult(
                state=LodgingConfirmationState.APPLIED,
                review_id=review_id,
                applied=True,
                candidate_is_current=True,
                transaction_id=result.transaction_id,
                current_revision=result.current_revision,
                check_status=result.check_status,
            )
        if result.status == "replayed":
            self._pending = None
            current = (
                result.current_revision is not None
                and result.current_revision == result.applied_revision
            )
            return LodgingConfirmationResult(
                state=LodgingConfirmationState.REPLAY_CONFIRMED,
                review_id=review_id,
                applied=True,
                candidate_is_current=current,
                transaction_id=result.transaction_id,
                current_revision=result.current_revision,
                check_status=result.check_status,
            )
        if result.status == "replayed_rolled_back":
            self._pending = None
            return _problem_result(
                LodgingConfirmationState.REJECTED,
                "LODGING_CONFIRMATION_ROLLED_BACK",
                "The exact lodging confirmation was applied and rolled back.",
                review_id=review_id,
                current_revision=result.current_revision,
            )
        if any(
            problem.code
            in {
                "LODGING_CONFIRMATION_MISMATCH",
                "LODGING_CONFIRMATION_REQUIRED",
                "UNTRUSTED_LODGING_CONFIRMATION",
            }
            for problem in result.problems
        ):
            return _problem_result(
                LodgingConfirmationState.WAITING_CONFIRMATION,
                "LODGING_CONFIRMATION_REQUIRED",
                "An exact, live host lodging confirmation is required.",
                review_id=review_id,
                current_revision=result.current_revision,
                pending=True,
            )
        if result.status in {
            "commit_outcome_unknown",
            "write_failed",
        }:
            return _problem_result(
                LodgingConfirmationState.OUTCOME_UNKNOWN,
                "LODGING_CONFIRMATION_OUTCOME_UNKNOWN",
                "Retry the same review and idempotency request.",
                review_id=review_id,
                current_revision=result.current_revision,
                pending=True,
            )
        self._pending = None
        return _problem_result(
            LodgingConfirmationState.REJECTED,
            (
                result.problems[0].code
                if result.problems
                else "LODGING_CONFIRMATION_REJECTED"
            ),
            "The canonical lodging confirmation was rejected.",
            review_id=review_id,
            current_revision=result.current_revision,
        )


def lodging_confirmation_request_from_option(
    *,
    assessment: LodgingItineraryAssessment,
    option: LodgingItineraryOption,
    comparison: LodgingComparisonAssessment,
    snapshot: EvidenceSnapshot,
    decision_states: Mapping[str, DecisionState],
    idempotency_key: str,
) -> LodgingConfirmationRequest:
    """Project one reviewed runtime option into a redacted canonical request.

    The 4.5C rank does not authorize this request.  It only provides an exact
    option shape.  A later :class:`LodgingConfirmationGrant` is always required.
    """

    if type(assessment) is not LodgingItineraryAssessment:
        raise TypeError("assessment must be exact")
    if type(option) is not LodgingItineraryOption:
        raise TypeError("option must be exact")
    if type(comparison) is not LodgingComparisonAssessment:
        raise TypeError("comparison must be exact")
    if type(snapshot) is not EvidenceSnapshot:
        raise TypeError("snapshot must be exact")
    if not isinstance(decision_states, Mapping):
        raise TypeError("decision_states must be a mapping")
    assessment.require_snapshot(snapshot)
    comparison.require_snapshot(snapshot)
    if assessment.basis != comparison.basis:
        raise FactContractError(
            "EVIDENCE_BINDING_MISMATCH",
            "Lodging recommendation and comparison bases differ.",
        )
    option_assessment = next(
        (
            item
            for item in assessment.options
            if item.option_id == option.option_id
        ),
        None,
    )
    if option_assessment is None:
        raise ValueError("selected lodging option was not reviewed")
    if option_assessment.disposition is LodgingOptionDisposition.INFEASIBLE:
        raise ValueError("an infeasible lodging option cannot be confirmed")
    if (
        option_assessment.disposition
        is LodgingOptionDisposition.NEEDS_VERIFICATION
        and not set(option_assessment.issue_codes).issubset(
            _CONFIRMATION_ONLY_ISSUES
        )
    ):
        raise FactContractError(
            "PENDING_REVIEW",
            "The lodging option has unresolved non-confirmation evidence.",
        )
    if option.result.status is not ScheduleStatus.SOLVED:
        raise ValueError("selected lodging option has no solved schedule")
    schedule_candidate = option.result.candidate
    if schedule_candidate is None:
        raise ValueError("selected lodging option has no schedule candidate")
    replay_schedule_candidate(option.problem, schedule_candidate)
    if option.problem.evidence_binding is None or (
        option.problem.evidence_binding.snapshot_id != snapshot.snapshot_id
    ):
        raise FactContractError(
            "EVIDENCE_REVISION_CHANGED",
            "Lodging option no longer matches the evidence snapshot.",
        )
    if set(decision_states) != set(option.candidate_ids):
        raise ValueError(
            "decision_states must cover the exact selected candidate set"
        )
    comparisons: dict[str, LodgingComparisonCandidate] = {
        item.candidate_id: item for item in comparison.candidates
    }
    selected = []
    selected_bindings: list[dict[str, str]] = []
    lodging_id_by_candidate: dict[str, str] = {}
    for candidate_id in option.candidate_ids:
        item = comparisons.get(candidate_id)
        if item is None or item.identity.endpoint is None:
            raise FactContractError(
                "LODGING_LOCATION_UNRESOLVED",
                "Confirmed lodging requires a canonical location identity.",
            )
        decision = decision_states[candidate_id]
        if type(decision) is not DecisionState or decision not in {
            DecisionState.SELECTED,
            DecisionState.FIXED,
            DecisionState.BOOKED,
        }:
            raise ValueError("invalid confirmed lodging decision state")
        segment = LodgingSelectionSegment(
            location_id=item.identity.endpoint.location_id,
            check_in=item.candidate.check_in,
            check_out=item.candidate.check_out,
            kind=item.candidate.draft.kind,
            decision_state=decision,
        )
        selected.append(segment)
        lodging_id_by_candidate[candidate_id] = segment.lodging_id
        selected_bindings.append(
            {
                "comparison_id": item.comparison_id,
                "identity_id": item.identity.identity_id,
                "endpoint_id": item.identity.endpoint.endpoint_id,
                "lodging_id": segment.lodging_id,
            }
        )
    anchors = tuple(
        LodgingSelectionAnchor(
            day_id=item.day_id,
            start_lodging_id=(
                lodging_id_by_candidate[item.start_candidate_id]
                if item.start_candidate_id is not None
                else None
            ),
            end_lodging_id=(
                lodging_id_by_candidate[item.end_candidate_id]
                if item.end_candidate_id is not None
                else None
            ),
        )
        for item in option.anchors
    )
    selection_binding = _canonical_digest(
        {
            "itinerary_assessment_id": assessment.assessment_id,
            "itinerary_option_id": option.option_id,
            "comparison_assessment_id": comparison.assessment_id,
            "evidence_basis_id": comparison.basis.basis_id,
            "snapshot_id": snapshot.snapshot_id,
            "schedule_candidate_id": schedule_candidate.candidate_id,
            "selected_endpoint_bindings": sorted(
                selected_bindings,
                key=lambda item: item["lodging_id"],
            ),
            "canonical_anchors": [
                item.to_dict() for item in anchors
            ],
        },
        prefix="lodging-itinerary-selection-binding",
    )
    return LodgingConfirmationRequest(
        trip_id=option.problem.trip_id,
        base_revision=option.problem.base_revision,
        idempotency_key=idempotency_key,
        segments=tuple(selected),
        anchors=anchors,
        evaluation_at=option.problem.evaluation_at,
        selection_binding_digest=selection_binding,
        reviewed_itinerary=True,
    )


def _grant_matches_review(
    grant: LodgingConfirmationGrant | None,
    review: LodgingConfirmationReview,
    now: datetime,
) -> bool:
    return (
        type(grant) is LodgingConfirmationGrant
        and review.review_id is not None
        and review.patch_digest is not None
        and review.required_lodging_confirmation_scope is not None
        and grant.review_id == review.review_id
        and grant.trip_id == review.trip_id
        and grant.base_revision == review.base_revision
        and grant.request_digest == review.patch_digest
        and grant.scope_digest
        == review.required_lodging_confirmation_scope
        and review.created_at
        <= grant.confirmed_at
        <= now
        <= review.expires_at
        and grant.expires_at == review.expires_at
    )


def _protected_approvals_for_confirmation(
    review: LodgingConfirmationReview,
    confirmation: LodgingConfirmationGrant,
    approvals: Sequence[ApprovalGrant],
) -> tuple[ApprovalGrant, ...]:
    """Let one exact host confirmation satisfy the generic protected gate too.

    The lodging grant is bound to the complete review, whose digest includes
    the store approval scope.  Deriving this ordinary approval only after that
    grant has been verified keeps the two store policies independently
    enforced without asking the host to confirm the same effect twice.
    """

    effective = tuple(approvals)
    scope = review.required_store_approval_scope
    if scope is None or any(
        type(grant) is ApprovalGrant and grant.scope_digest == scope
        for grant in effective
    ):
        return effective
    return effective + (
        ApprovalGrant(
            approval_id=f"lodging-confirmation:{confirmation.grant_id}",
            scope_digest=scope,
            approved_by=confirmation.confirmed_by,
            approved_at=confirmation.confirmed_at.isoformat(),
        ),
    )


def _preview_mismatch(
    result: StoreResult,
    review: LodgingConfirmationReview,
) -> LodgingConfirmationProblem | None:
    if not result.success or result.status != "preview_ready":
        code = (
            result.problems[0].code
            if result.problems
            else "LODGING_REVIEW_STALE"
        )
        return LodgingConfirmationProblem(
            code=code,
            message="The lodging review no longer previews successfully.",
        )
    candidate = result.mutable_candidate_plan()
    if candidate is None:
        return LodgingConfirmationProblem(
            code="LODGING_PREVIEW_CONTRACT_FAILED",
            message="The repeated preview omitted its canonical candidate.",
        )
    if (
        _state_digest(candidate) != review.expected_state_digest
        or result.applied_revision != review.expected_applied_revision
        or result.check_status != review.check_status
        or result.required_lodging_confirmation_scope
        != review.required_lodging_confirmation_scope
        or result.required_approval_scope
        != review.required_store_approval_scope
    ):
        return LodgingConfirmationProblem(
            code="LODGING_REVIEW_STALE",
            message="The canonical lodging effect changed after review.",
        )
    return None


def _terminal_review(
    request: LodgingConfirmationRequest,
    created_at: datetime,
    state: LodgingConfirmationState,
    *,
    patch_value: str,
) -> LodgingConfirmationReview:
    return LodgingConfirmationReview(
        state=state,
        request_id=request.request_id,
        trip_id=request.trip_id,
        base_revision=request.base_revision,
        created_at=created_at,
        expires_at=created_at + _REVIEW_LIFETIME,
        stay_count=len(request.segments),
        stay_night_count=(request.stay_end - request.stay_start).days,
        decision_states=tuple(
            item.decision_state.value for item in request.segments
        ),
        reviewed_itinerary=request.reviewed_itinerary,
        patch_digest=patch_value,
    )


def _rejected_review(
    request: LodgingConfirmationRequest,
    created_at: datetime,
    code: str,
    message: str,
    *,
    patch_value: str | None = None,
) -> LodgingConfirmationReview:
    return LodgingConfirmationReview(
        state=LodgingConfirmationState.REJECTED,
        request_id=request.request_id,
        trip_id=request.trip_id,
        base_revision=request.base_revision,
        created_at=created_at,
        expires_at=created_at + _REVIEW_LIFETIME,
        stay_count=len(request.segments),
        stay_night_count=(request.stay_end - request.stay_start).days,
        decision_states=tuple(
            item.decision_state.value for item in request.segments
        ),
        reviewed_itinerary=request.reviewed_itinerary,
        patch_digest=patch_value,
        problems=(LodgingConfirmationProblem(code, message),),
    )


def _review_from_store_failure(
    request: LodgingConfirmationRequest,
    created_at: datetime,
    result: StoreResult,
    *,
    patch_value: str,
) -> LodgingConfirmationReview:
    problem = result.problems[0] if result.problems else None
    return _rejected_review(
        request,
        created_at,
        problem.code if problem is not None else "LODGING_PREVIEW_FAILED",
        "The canonical lodging preview was rejected.",
        patch_value=patch_value,
    )


def _problem_result(
    state: LodgingConfirmationState,
    code: str,
    message: str,
    *,
    review_id: str | None = None,
    current_revision: str | None = None,
    pending: bool = False,
) -> LodgingConfirmationResult:
    return LodgingConfirmationResult(
        state=state,
        review_id=review_id,
        current_revision=current_revision,
        pending_review_retained=pending,
        problems=(LodgingConfirmationProblem(code, message),),
    )


__all__ = [
    "LODGING_CONFIRMATION_VERSION",
    "LodgingConfirmationAuthority",
    "LodgingConfirmationProblem",
    "LodgingConfirmationRequest",
    "LodgingConfirmationResult",
    "LodgingConfirmationReview",
    "LodgingConfirmationStager",
    "LodgingConfirmationState",
    "LodgingSelectionAnchor",
    "LodgingSelectionSegment",
    "canonical_lodging_location_id",
    "lodging_confirmation_request_from_option",
]
