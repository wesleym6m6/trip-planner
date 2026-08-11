"""Effect-bounded Phase 6.2A private-delivery review and manifest contracts.

This module binds one bounded canonical plan snapshot, its exact composed evidence
view, a closed artifact profile, and lexical source/target paths into an
ephemeral process-local review.  It produces deterministic private projection
bytes and a private provenance manifest, but performs no caller/project/private
source or target filesystem I/O, write, browser, calendar import, provider
call, canonical mutation, or deploy.  Canonical timezone-name validation in
both profiles may consult the host timezone database; the ready profile also
inherits the offset-projection dependency documented by the Phase 6.1A ICS
projector.

The review and its response are deliberately non-serializable.  A response
accepts only the synthetic/process-local candidate review; it does not grant a
filesystem write.  A later create-only writer must establish a fresh exact
write review from retained authoritative host objects, reload and reproject
every artifact, and verify the target filesystem.  Neither a response here,
a manifest, nor a digest is write authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import unicodedata
import weakref
from dataclasses import InitVar, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .canonical_tripctl import MAX_CANONICAL_PLAN_BYTES
from .codec import decode_plan, encode_plan
from .composition import COMPOSITION_VERSION, ComposedTripState, compose_trip_state
from .facts import (
    EVIDENCE_SNAPSHOT_VERSION,
    EvidencePersistence,
    EvidenceSnapshot,
    FactKey,
    FactKind,
    FactObservation,
    FactValue,
    ProviderPolicy,
    ProviderPolicyRegistry,
    ProviderProvenance,
)
from .lodging import (
    IntentAuthority,
    LocationHint,
    LocationHintKind,
    LodgingCandidate,
    LodgingIntakeAssessment,
    LodgingIntakeIssue,
    LodgingIntakeStatus,
    LodgingIntentDraft,
    LodgingKind,
    LodgingRequirement,
    PriceBasis,
    ReportedDecisionClaim,
    assess_lodging_intake,
    bind_lodging_candidate,
)
from .lodging_confirmation import (
    LodgingConfirmationProblem,
    LodgingConfirmationReview,
    LodgingConfirmationState,
)
from .models import DecisionState, EvidenceState, IssueSeverity, TripState
from .private_html import (
    MAX_PRIVATE_HTML_ACTIVITIES,
    MAX_PRIVATE_HTML_BYTES,
    PRIVATE_HTML_RENDERER_VERSION,
    PRIVATE_HTML_TEMPLATE_VERSION,
    PRIVATE_HTML_VERSION,
    PrivateHtmlProjection,
    PrivateHtmlProjectionError,
    project_private_html,
)
from .private_ics import (
    MAX_PRIVATE_ICS_BYTES,
    PRIVATE_ICS_UID_POLICY_VERSION,
    PRIVATE_ICS_VERSION,
    PrivateIcsProjection,
    PrivateIcsProjectionError,
    _event_interval,
    _uid_for,
    project_private_ics,
)
from .readiness import (
    READINESS_VERSION,
    ReadinessAction,
    ReadinessProblem,
    ReadinessSource,
    ReadinessStatus,
    TripReadiness,
    assess_trip_readiness,
)


PRIVATE_DELIVERY_VERSION = "trip-planner.private-delivery/v1"
PRIVATE_DELIVERY_MANIFEST_VERSION = "trip-planner.private-delivery-manifest/v1"
PRIVATE_DELIVERY_RESPONSE_VERSION = "trip-planner.private-delivery-response/v1"

PRIVATE_DELIVERY_REVIEW_TTL = timedelta(minutes=30)
MAX_PRIVATE_DELIVERY_MANIFEST_BYTES = 64 * 1024
MAX_PRIVATE_DELIVERY_REVIEW_BYTES = 4 * 1024 * 1024
MAX_PRIVATE_DELIVERY_PATH_CHARS = 4096
MAX_PRIVATE_DELIVERY_ACTIVE_REVIEWS = 4096
MAX_PRIVATE_DELIVERY_EVIDENCE_ITEMS = 4096
MAX_PRIVATE_DELIVERY_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_PRIVATE_DELIVERY_POLICIES = 256
MAX_PRIVATE_DELIVERY_POLICY_ITEMS = 256
MAX_PRIVATE_DELIVERY_LODGING_CANDIDATES = 256
MAX_PRIVATE_DELIVERY_LODGING_PROBLEMS = 256

_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_LEAF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_REVIEW_TOKEN = object()
_RESPONSE_TOKEN = object()
_ARTIFACT_TOKEN = object()
_UTC = timezone.utc


class PrivateDeliveryProfile(str, Enum):
    """The two closed Phase 6.2 private artifact profiles."""

    HTML_PREVIEW = "html_preview"
    HTML_ICS_READY_BUNDLE = "html_ics_ready_bundle"


class PrivateDeliveryResponseKind(str, Enum):
    """Exact candidate-review responses; none is filesystem authority."""

    ACCEPT_HTML_PREVIEW_CANDIDATE = "accept_html_preview_candidate"
    ACCEPT_HTML_ICS_READY_BUNDLE_CANDIDATE = (
        "accept_html_ics_ready_bundle_candidate"
    )
    REQUEST_CHANGES = "request_changes"
    CANCEL = "cancel"


_ACCEPT_FOR_PROFILE = {
    PrivateDeliveryProfile.HTML_PREVIEW:
        PrivateDeliveryResponseKind.ACCEPT_HTML_PREVIEW_CANDIDATE,
    PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE:
        PrivateDeliveryResponseKind.ACCEPT_HTML_ICS_READY_BUNDLE_CANDIDATE,
}


class PrivateDeliveryError(ValueError):
    """One bounded refusal that never includes a private input value."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or _ERROR_CODE_RE.fullmatch(code) is None:
            raise ValueError("private delivery error code is invalid")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class PrivateDeliveryArtifact:
    """One factory-only private in-memory artifact."""

    filename: str
    media_type: str
    payload: bytes = field(repr=False)
    sha256: str = field(repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _ARTIFACT_TOKEN:
            raise ValueError("PrivateDeliveryArtifact must come from preparation")
        if self.filename not in {"index.html", "calendar.ics", "manifest.json"}:
            raise ValueError("unsupported private delivery artifact name")
        expected_media = {
            "index.html": "text/html; charset=utf-8",
            "calendar.ics": "text/calendar; charset=utf-8",
            "manifest.json": "application/json; charset=utf-8",
        }[self.filename]
        if self.media_type != expected_media:
            raise ValueError("private delivery artifact media type mismatch")
        if type(self.payload) is not bytes or not self.payload:
            raise ValueError("private delivery artifact must contain bytes")
        limit = {
            "index.html": MAX_PRIVATE_HTML_BYTES,
            "calendar.ics": MAX_PRIVATE_ICS_BYTES,
            "manifest.json": MAX_PRIVATE_DELIVERY_MANIFEST_BYTES,
        }[self.filename]
        if len(self.payload) > limit:
            raise ValueError("private delivery artifact exceeds its bound")
        if (
            type(self.sha256) is not str
            or _SHA256_RE.fullmatch(self.sha256) is None
            or hashlib.sha256(self.payload).hexdigest() != self.sha256
        ):
            raise ValueError("private delivery artifact digest mismatch")

    def __repr__(self) -> str:
        return "PrivateDeliveryArtifact(contains_private_data=True)"

    def to_safe_dict(self) -> dict[str, object]:
        """Return only fixed artifact metadata, never bytes or a digest."""

        try:
            _verify_artifact(self)
        except Exception:
            raise PrivateDeliveryError(
                "PRIVATE_DELIVERY_ARTIFACT_TAMPERED"
            ) from None
        return {
            "filename": self.filename,
            "media_type": self.media_type,
            "contains_private_data": True,
            "bytes_exposed": False,
            "digest_exposed": False,
        }

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("PrivateDeliveryArtifact is process-local")


@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True)
class PrivateDeliveryReview:
    """Sealed no-write review for one exact process-local delivery candidate."""

    profile: PrivateDeliveryProfile
    artifacts: tuple[PrivateDeliveryArtifact, ...] = field(repr=False)
    created_at: datetime = field(repr=False)
    expires_at: datetime = field(repr=False)
    source_path: str = field(repr=False)
    target_path: str = field(repr=False)
    canonical_source_sha256: str = field(repr=False)
    plan_revision: str = field(repr=False)
    composed_state_digest: str = field(repr=False)
    evidence_binding_digest: str = field(repr=False)
    runtime_context_sha256: str = field(repr=False)
    review_id: str = field(repr=False)
    private_review_json: bytes = field(repr=False)
    readiness: TripReadiness | None = field(default=None, repr=False)
    _clock: Callable[[], datetime] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    contract_version: str = PRIVATE_DELIVERY_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError("PrivateDeliveryReview must come from preparation")
        if type(self.profile) is not PrivateDeliveryProfile:
            raise TypeError("profile must be exact PrivateDeliveryProfile")
        if (
            type(self.contract_version) is not str
            or self.contract_version != PRIVATE_DELIVERY_VERSION
            or not callable(self._clock)
        ):
            raise ValueError("unsupported private delivery review version")
        expected_names = (
            ("index.html", "manifest.json")
            if self.profile is PrivateDeliveryProfile.HTML_PREVIEW
            else ("index.html", "calendar.ics", "manifest.json")
        )
        if (
            not isinstance(self.artifacts, tuple)
            or any(
                type(item) is not PrivateDeliveryArtifact
                for item in self.artifacts
            )
            or tuple(item.filename for item in self.artifacts) != expected_names
        ):
            raise ValueError("private delivery artifact set mismatch")
        created = _utc_datetime(
            self.created_at,
            "PRIVATE_DELIVERY_REVIEW_TIME_INVALID",
            whole_second=False,
        )
        expires = _utc_datetime(
            self.expires_at,
            "PRIVATE_DELIVERY_REVIEW_TIME_INVALID",
            whole_second=False,
        )
        if expires <= created or expires > created + PRIVATE_DELIVERY_REVIEW_TTL:
            raise ValueError("private delivery review lifetime is invalid")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)
        _validate_digest(self.canonical_source_sha256)
        _validate_digest(self.plan_revision)
        _validate_state_digest(self.composed_state_digest)
        _validate_digest(self.evidence_binding_digest)
        _validate_digest(self.runtime_context_sha256)
        _validate_digest(self.review_id)
        if (
            type(self.private_review_json) is not bytes
            or not self.private_review_json
            or len(self.private_review_json) > MAX_PRIVATE_DELIVERY_REVIEW_BYTES
        ):
            raise ValueError("private review payload is invalid")
        try:
            decoded = json.loads(self.private_review_json)
        except Exception:
            raise ValueError("private review payload is invalid") from None
        if type(decoded) is not dict:
            raise ValueError("private review payload is invalid")
        if self.profile is PrivateDeliveryProfile.HTML_PREVIEW:
            if self.readiness is not None:
                raise ValueError("HTML preview must not carry readiness")
        elif type(self.readiness) is not TripReadiness:
            raise ValueError("ready bundle must carry exact readiness")

    @property
    def candidate_response_kind(self) -> PrivateDeliveryResponseKind:
        return _ACCEPT_FOR_PROFILE[self.profile]

    def __repr__(self) -> str:
        return (
            "PrivateDeliveryReview(contains_private_data=True, "
            "write_authorized=False, writes_performed=False)"
        )

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("PrivateDeliveryReview is process-local")

    def to_safe_dict(self) -> dict[str, object]:
        """Return a value-free, loggable description of this no-write review."""

        _verified_review_record(self)
        readiness_required = (
            self.profile is PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE
        )
        return {
            "contract_version": PRIVATE_DELIVERY_VERSION,
            "profile": self.profile.value,
            "artifact_filenames": [item.filename for item in self.artifacts],
            "candidate_response_kind": self.candidate_response_kind.value,
            "contains_private_data": True,
            "canonical_snapshot_bound": True,
            "source_path_verified": False,
            "target_lexically_bound": True,
            "target_filesystem_verified": False,
            "readiness_required": readiness_required,
            "readiness_satisfied_at_preparation": (
                True if readiness_required else None
            ),
            "readiness_fresh_at_preparation": (
                True if readiness_required else None
            ),
            "write_authorized": False,
            "writes_performed": False,
            "write_outcome": "not_performed",
        }

    def to_ephemeral_private_review(self) -> dict[str, Any]:
        """Return the exact private review; callers must not log or persist it."""

        _verified_review_record(self)
        return json.loads(self.private_review_json)

    def to_ephemeral_private_artifacts(
        self,
    ) -> tuple[PrivateDeliveryArtifact, ...]:
        """Return private bytes for review, not filesystem-write authority."""

        _verified_review_record(self)
        return self.artifacts


@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True)
class PrivateDeliveryResponse:
    """One process-local response captured against exactly one review."""

    kind: PrivateDeliveryResponseKind
    profile: PrivateDeliveryProfile
    candidate_accepted: bool
    review_id: str = field(repr=False)
    response_id: str = field(repr=False)
    captured_at: datetime = field(repr=False)
    _review: PrivateDeliveryReview = field(repr=False, compare=False)
    contract_version: str = PRIVATE_DELIVERY_RESPONSE_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _RESPONSE_TOKEN:
            raise ValueError("PrivateDeliveryResponse must come from capture")
        if type(self.kind) is not PrivateDeliveryResponseKind:
            raise TypeError("response kind must be exact")
        if type(self.profile) is not PrivateDeliveryProfile:
            raise TypeError("response profile must be exact")
        if type(self.candidate_accepted) is not bool:
            raise TypeError("candidate_accepted must be bool")
        _validate_digest(self.review_id)
        _validate_digest(self.response_id)
        captured = _utc_datetime(
            self.captured_at,
            "PRIVATE_DELIVERY_RESPONSE_TIME_INVALID",
            whole_second=False,
        )
        object.__setattr__(self, "captured_at", captured)
        if (
            type(self.contract_version) is not str
            or self.contract_version != PRIVATE_DELIVERY_RESPONSE_VERSION
        ):
            raise ValueError("unsupported private delivery response version")
        if type(self._review) is not PrivateDeliveryReview:
            raise TypeError("response must retain its exact review")
        if self.review_id != self._review.review_id or self.profile is not self._review.profile:
            raise ValueError("response review binding mismatch")
        expected_accepted = self.kind is self._review.candidate_response_kind
        if self.candidate_accepted is not expected_accepted:
            raise ValueError("candidate response acceptance mismatch")

    def __repr__(self) -> str:
        return (
            "PrivateDeliveryResponse(write_authorized=False, "
            "writes_performed=False)"
        )

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("PrivateDeliveryResponse is process-local")

    def to_safe_dict(self) -> dict[str, object]:
        verify_private_delivery_response(self, check_freshness=False)
        return {
            "contract_version": PRIVATE_DELIVERY_RESPONSE_VERSION,
            "profile": self.profile.value,
            "kind": self.kind.value,
            "candidate_accepted": self.candidate_accepted,
            "review_bound": True,
            "write_authorized": False,
            "writes_performed": False,
            "write_outcome": "not_performed",
        }


@dataclass(slots=True)
class _ReviewRecord:
    review_ref: weakref.ReferenceType[PrivateDeliveryReview]
    fingerprint: str
    last_checked_at: datetime
    captured_response_id: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(slots=True)
class _ResponseRecord:
    response_ref: weakref.ReferenceType[PrivateDeliveryResponse]
    fingerprint: str


_REGISTRY_LOCK = threading.Lock()
_REVIEW_REGISTRY: dict[int, _ReviewRecord] = {}
_RESPONSE_REGISTRY: dict[int, _ResponseRecord] = {}


def _register_review(
    review: PrivateDeliveryReview,
) -> None:
    fingerprint = _review_fingerprint(review)
    with _REGISTRY_LOCK:
        if len(_REVIEW_REGISTRY) >= MAX_PRIVATE_DELIVERY_ACTIVE_REVIEWS:
            raise PrivateDeliveryError("PRIVATE_DELIVERY_REVIEW_LIMIT_EXCEEDED")
        key = id(review)
        if key in _REVIEW_REGISTRY:
            raise PrivateDeliveryError("PRIVATE_DELIVERY_REVIEW_REGISTRY_CONFLICT")

        def remove_review(
            reference: weakref.ReferenceType[PrivateDeliveryReview],
            *,
            registry_key: int = key,
        ) -> None:
            del reference
            with _REGISTRY_LOCK:
                _REVIEW_REGISTRY.pop(registry_key, None)

        _REVIEW_REGISTRY[key] = _ReviewRecord(
            review_ref=weakref.ref(review, remove_review),
            fingerprint=fingerprint,
            last_checked_at=review.created_at,
        )


def _register_response(response: PrivateDeliveryResponse) -> None:
    fingerprint = _response_fingerprint(response)
    with _REGISTRY_LOCK:
        if len(_RESPONSE_REGISTRY) >= MAX_PRIVATE_DELIVERY_ACTIVE_REVIEWS:
            raise PrivateDeliveryError("PRIVATE_DELIVERY_RESPONSE_LIMIT_EXCEEDED")
        key = id(response)
        if key in _RESPONSE_REGISTRY:
            raise PrivateDeliveryError("PRIVATE_DELIVERY_RESPONSE_REGISTRY_CONFLICT")
        def remove_response(
            reference: weakref.ReferenceType[PrivateDeliveryResponse],
            *,
            registry_key: int = key,
        ) -> None:
            del reference
            with _REGISTRY_LOCK:
                _RESPONSE_REGISTRY.pop(registry_key, None)

        _RESPONSE_REGISTRY[key] = _ResponseRecord(
            response_ref=weakref.ref(response, remove_response),
            fingerprint=fingerprint,
        )


def _verified_review_record(review: object) -> _ReviewRecord:
    if type(review) is not PrivateDeliveryReview:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_REVIEW_REQUIRED")
    with _REGISTRY_LOCK:
        record = _REVIEW_REGISTRY.get(id(review))
    if record is None or record.review_ref() is not review:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_REVIEW_UNREGISTERED")
    try:
        fingerprint = _review_fingerprint(review)
    except Exception:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_REVIEW_TAMPERED") from None
    if fingerprint != record.fingerprint:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_REVIEW_TAMPERED")
    return record


def verify_private_delivery_response(
    response: PrivateDeliveryResponse,
    *,
    check_freshness: bool = True,
) -> None:
    """Verify one sealed candidate response; this never authorizes a write."""

    if type(response) is not PrivateDeliveryResponse:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_RESPONSE_REQUIRED")
    if type(check_freshness) is not bool:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_RESPONSE_CHECK_INVALID")
    with _REGISTRY_LOCK:
        response_record = _RESPONSE_REGISTRY.get(id(response))
    if (
        response_record is None
        or response_record.response_ref() is not response
    ):
        raise PrivateDeliveryError("PRIVATE_DELIVERY_RESPONSE_UNREGISTERED")
    try:
        fingerprint = _response_fingerprint(response)
    except Exception:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_RESPONSE_TAMPERED") from None
    if fingerprint != response_record.fingerprint:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_RESPONSE_TAMPERED")
    review_record = _verified_review_record(response._review)
    with review_record.lock:
        if review_record.captured_response_id != response.response_id:
            raise PrivateDeliveryError("PRIVATE_DELIVERY_RESPONSE_BINDING_MISMATCH")
        if check_freshness:
            checked = _sample_clock(response._review._clock)
            if checked < review_record.last_checked_at:
                raise PrivateDeliveryError("PRIVATE_DELIVERY_CLOCK_ROLLBACK")
            review_record.last_checked_at = checked
            try:
                post_clock_fingerprint = _response_fingerprint(response)
            except Exception:
                raise PrivateDeliveryError(
                    "PRIVATE_DELIVERY_RESPONSE_TAMPERED"
                ) from None
            if post_clock_fingerprint != response_record.fingerprint:
                raise PrivateDeliveryError(
                    "PRIVATE_DELIVERY_RESPONSE_TAMPERED"
                )
            _verified_review_record(response._review)
            if checked >= response._review.expires_at:
                raise PrivateDeliveryError("PRIVATE_DELIVERY_REVIEW_EXPIRED")


def _review_fingerprint(review: PrivateDeliveryReview) -> str:
    if (
        type(review.profile) is not PrivateDeliveryProfile
        or type(review.contract_version) is not str
        or review.contract_version != PRIVATE_DELIVERY_VERSION
        or not callable(review._clock)
    ):
        raise ValueError("invalid profile")
    expected_names = (
        ("index.html", "manifest.json")
        if review.profile is PrivateDeliveryProfile.HTML_PREVIEW
        else ("index.html", "calendar.ics", "manifest.json")
    )
    if (
        type(review.artifacts) is not tuple
        or len(review.artifacts) != len(expected_names)
        or any(
            type(item) is not PrivateDeliveryArtifact
            for item in review.artifacts
        )
    ):
        raise ValueError("invalid artifact set")
    artifact_payload: list[dict[str, object]] = []
    for item in review.artifacts:
        _verify_artifact(item)
        artifact_payload.append(
            {
                "filename": item.filename,
                "media_type": item.media_type,
                "sha256": item.sha256,
                "payload_sha256": hashlib.sha256(item.payload).hexdigest(),
            }
        )
    if tuple(item.filename for item in review.artifacts) != expected_names:
        raise ValueError("invalid artifact set")
    created = _sealed_utc_datetime(
        review.created_at,
        "PRIVATE_DELIVERY_REVIEW_TIME_INVALID",
    )
    expires = _sealed_utc_datetime(
        review.expires_at,
        "PRIVATE_DELIVERY_REVIEW_TIME_INVALID",
    )
    source_path = _absolute_path(
        review.source_path,
        "PRIVATE_DELIVERY_SOURCE_PATH_INVALID",
    )
    target_path = _absolute_path(
        review.target_path,
        "PRIVATE_DELIVERY_TARGET_PATH_INVALID",
        require_safe_leaf=True,
    )
    for value in (
        review.canonical_source_sha256,
        review.plan_revision,
        review.evidence_binding_digest,
        review.runtime_context_sha256,
        review.review_id,
    ):
        _validate_digest(value)
    _validate_state_digest(review.composed_state_digest)
    if (
        type(review.private_review_json) is not bytes
        or not review.private_review_json
        or len(review.private_review_json) > MAX_PRIVATE_DELIVERY_REVIEW_BYTES
    ):
        raise ValueError("invalid private review")
    readiness_payload: dict[str, Any] | None
    if review.readiness is None:
        readiness_payload = None
    elif type(review.readiness) is TripReadiness:
        _validate_readiness_shape(review.readiness)
        readiness_payload = review.readiness.to_dict()
        _validate_digest(review.readiness.readiness_id)
    else:
        raise ValueError("invalid readiness")
    return _digest_json(
        {
            "contract_version": PRIVATE_DELIVERY_VERSION,
            "profile": review.profile.value,
            "artifacts": artifact_payload,
            "created_at": _utc_iso(created),
            "expires_at": _utc_iso(expires),
            "source_path": source_path,
            "target_path": target_path,
            "canonical_source_sha256": review.canonical_source_sha256,
            "plan_revision": review.plan_revision,
            "composed_state_digest": review.composed_state_digest,
            "evidence_binding_digest": review.evidence_binding_digest,
            "runtime_context_sha256": review.runtime_context_sha256,
            "review_id": review.review_id,
            "private_review_sha256": hashlib.sha256(
                review.private_review_json
            ).hexdigest(),
            "readiness": readiness_payload,
            "clock_object_id": id(review._clock),
        },
        prefix="private-delivery-review-seal",
    )


def _response_fingerprint(response: PrivateDeliveryResponse) -> str:
    if (
        type(response.kind) is not PrivateDeliveryResponseKind
        or type(response.profile) is not PrivateDeliveryProfile
        or type(response.candidate_accepted) is not bool
        or type(response._review) is not PrivateDeliveryReview
        or type(response.contract_version) is not str
        or response.contract_version != PRIVATE_DELIVERY_RESPONSE_VERSION
    ):
        raise ValueError("invalid response")
    _validate_digest(response.review_id)
    _validate_digest(response.response_id)
    captured = _sealed_utc_datetime(
        response.captured_at,
        "PRIVATE_DELIVERY_RESPONSE_TIME_INVALID",
    )
    return _digest_json(
        {
            "contract_version": PRIVATE_DELIVERY_RESPONSE_VERSION,
            "kind": response.kind.value,
            "profile": response.profile.value,
            "candidate_accepted": response.candidate_accepted,
            "review_id": response.review_id,
            "response_id": response.response_id,
            "captured_at": _utc_iso(captured),
            "review_object_id": id(response._review),
        },
        prefix="private-delivery-response-seal",
    )


def _verify_artifact(artifact: PrivateDeliveryArtifact) -> None:
    expected_media = {
        "index.html": ("text/html; charset=utf-8", MAX_PRIVATE_HTML_BYTES),
        "calendar.ics": ("text/calendar; charset=utf-8", MAX_PRIVATE_ICS_BYTES),
        "manifest.json": (
            "application/json; charset=utf-8",
            MAX_PRIVATE_DELIVERY_MANIFEST_BYTES,
        ),
    }
    if type(artifact.filename) is not str or artifact.filename not in expected_media:
        raise ValueError("invalid artifact")
    media_type, limit = expected_media[artifact.filename]
    if (
        type(artifact.media_type) is not str
        or artifact.media_type != media_type
        or type(artifact.payload) is not bytes
        or not artifact.payload
        or len(artifact.payload) > limit
        or type(artifact.sha256) is not str
        or _SHA256_RE.fullmatch(artifact.sha256) is None
        or hashlib.sha256(artifact.payload).hexdigest() != artifact.sha256
    ):
        raise ValueError("invalid artifact")


def prepare_private_delivery_review(
    canonical_plan_bytes: bytes,
    snapshot: EvidenceSnapshot,
    *,
    profile: PrivateDeliveryProfile,
    source_path: str,
    target_path: str,
    clock: Callable[[], datetime],
    availability_keys: tuple[FactKey, ...] = (),
    lodging_intake: LodgingIntakeAssessment | None = None,
    pending_lodging_review: LodgingConfirmationReview | None = None,
) -> PrivateDeliveryReview:
    """Prepare one deterministic, canonical-bound, no-write private review.

    ``source_path`` and ``target_path`` are lexical private bindings only.
    Phase 6.2B must reload and pin their filesystem identities before any write.
    """

    if type(profile) is not PrivateDeliveryProfile:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_PROFILE_INVALID")
    if type(snapshot) is not EvidenceSnapshot:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_TYPED_INPUT_REQUIRED")
    if (
        type(canonical_plan_bytes) is not bytes
        or not canonical_plan_bytes
        or len(canonical_plan_bytes) > MAX_CANONICAL_PLAN_BYTES
    ):
        raise PrivateDeliveryError("PRIVATE_DELIVERY_CANONICAL_INVALID")
    canonical_bytes = canonical_plan_bytes
    try:
        detached_plan = decode_plan(canonical_bytes)
        if encode_plan(detached_plan) != canonical_bytes:
            raise ValueError("canonical bytes differ")
    except Exception:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_CANONICAL_INVALID") from None
    availability_keys = _validated_availability_keys(availability_keys)
    source = _absolute_path(source_path, "PRIVATE_DELIVERY_SOURCE_PATH_INVALID")
    target = _absolute_path(
        target_path,
        "PRIVATE_DELIVERY_TARGET_PATH_INVALID",
        require_safe_leaf=True,
    )
    if source == target:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_TARGET_PATH_INVALID")
    prepared = _sample_clock(clock)
    try:
        review_deadline = prepared + PRIVATE_DELIVERY_REVIEW_TTL
    except (OverflowError, ValueError):
        raise PrivateDeliveryError(
            "PRIVATE_DELIVERY_REVIEW_TIME_INVALID"
        ) from None
    if profile is PrivateDeliveryProfile.HTML_PREVIEW and (
        lodging_intake is not None or pending_lodging_review is not None
    ):
        raise PrivateDeliveryError("PRIVATE_DELIVERY_PROFILE_INPUT_INVALID")
    lodging_intake, pending_lodging_review = _validated_lodging_inputs(
        lodging_intake,
        pending_lodging_review,
    )
    _validate_evidence_snapshot(snapshot)
    if prepared != snapshot.evaluation_at:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_REVIEW_TIME_MISMATCH")

    try:
        expected = compose_trip_state(
            detached_plan,
            snapshot,
            availability_keys=availability_keys,
        )
    except Exception:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_COMPOSITION_FAILED") from None
    runtime_context_sha256 = _runtime_context_sha256(
        expected,
        availability_keys=availability_keys,
        lodging_intake=lodging_intake,
        pending_lodging_review=pending_lodging_review,
    )

    try:
        html = project_private_html(expected.state)
    except PrivateHtmlProjectionError:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_HTML_PROJECTION_FAILED") from None
    except Exception:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_HTML_PROJECTION_FAILED") from None

    readiness: TripReadiness | None = None
    ics: PrivateIcsProjection | None = None
    expires = review_deadline
    if profile is PrivateDeliveryProfile.HTML_ICS_READY_BUNDLE:
        if prepared.microsecond:
            raise PrivateDeliveryError(
                "PRIVATE_DELIVERY_CALENDAR_TIME_SUBSECOND_UNSUPPORTED"
            )
        try:
            readiness = assess_trip_readiness(
                canonical_plan=detached_plan,
                composed=expected,
                snapshot=snapshot,
                availability_keys=availability_keys,
                lodging_intake=lodging_intake,
                pending_lodging_review=pending_lodging_review,
            )
        except Exception:
            raise PrivateDeliveryError(
                "PRIVATE_DELIVERY_READINESS_ASSESSMENT_FAILED"
            ) from None
        if (
            readiness.status is not ReadinessStatus.TRAVEL_READY
            or readiness.next_action is not ReadinessAction.NONE
            or readiness.problems
        ):
            raise PrivateDeliveryError("PRIVATE_DELIVERY_READINESS_NOT_READY")
        if readiness.evaluated_at != prepared:
            raise PrivateDeliveryError("PRIVATE_DELIVERY_READINESS_BINDING_MISMATCH")
        if readiness.recheck_required_at is not None:
            if readiness.recheck_required_at <= prepared:
                raise PrivateDeliveryError("PRIVATE_DELIVERY_READINESS_EXPIRED")
            expires = min(expires, readiness.recheck_required_at)
        try:
            ics = project_private_ics(
                expected.state,
                uid_namespace=expected.trip_id,
                generated_at=readiness.evaluated_at,
            )
        except PrivateIcsProjectionError:
            raise PrivateDeliveryError("PRIVATE_DELIVERY_ICS_PROJECTION_FAILED") from None
        except Exception:
            raise PrivateDeliveryError("PRIVATE_DELIVERY_ICS_PROJECTION_FAILED") from None

    payload_artifacts = [
        _artifact(
            "index.html",
            "text/html; charset=utf-8",
            html.html_bytes,
        )
    ]
    if ics is not None:
        payload_artifacts.append(
            _artifact(
                "calendar.ics",
                "text/calendar; charset=utf-8",
                ics.calendar_bytes,
            )
        )
    manifest_bytes = _manifest_bytes(
        profile=profile,
        canonical_bytes=canonical_bytes,
        composed=expected,
        snapshot=snapshot,
        html=html,
        ics=ics,
        readiness=readiness,
        runtime_context_sha256=runtime_context_sha256,
        payload_artifacts=tuple(payload_artifacts),
    )
    manifest_artifact = _artifact(
        "manifest.json",
        "application/json; charset=utf-8",
        manifest_bytes,
    )
    artifacts = tuple(payload_artifacts) + (manifest_artifact,)
    private_review_json = _private_review_bytes(
        profile=profile,
        source_path=source,
        target_path=target,
        state=expected.state,
        artifacts=artifacts,
        readiness=readiness,
        uid_namespace=expected.trip_id,
        created_at=prepared,
        expires_at=expires,
    )
    review_id = _digest_json(
        {
            "contract_version": PRIVATE_DELIVERY_VERSION,
            "profile": profile.value,
            "source_path": source,
            "target_path": target,
            "canonical_source_sha256": hashlib.sha256(canonical_bytes).hexdigest(),
            "plan_revision": expected.plan_revision,
            "composed_state_digest": expected.composed_state_digest,
            "evidence_binding_digest": expected.evidence.binding_digest,
            "runtime_context_sha256": runtime_context_sha256,
            "artifact_sha256": [item.sha256 for item in artifacts],
            "private_review_sha256": hashlib.sha256(
                private_review_json
            ).hexdigest(),
            "created_at": _utc_iso(prepared),
            "expires_at": _utc_iso(expires),
        },
        prefix="private-delivery-review",
    )
    review = PrivateDeliveryReview(
        profile=profile,
        artifacts=artifacts,
        created_at=prepared,
        expires_at=expires,
        source_path=source,
        target_path=target,
        canonical_source_sha256=hashlib.sha256(canonical_bytes).hexdigest(),
        plan_revision=expected.plan_revision,
        composed_state_digest=expected.composed_state_digest,
        evidence_binding_digest=expected.evidence.binding_digest,
        runtime_context_sha256=runtime_context_sha256,
        review_id=review_id,
        private_review_json=private_review_json,
        readiness=readiness,
        _clock=clock,
        _token=_REVIEW_TOKEN,
    )
    _register_review(review)
    return review


def capture_private_delivery_response(
    review: PrivateDeliveryReview,
    kind: PrivateDeliveryResponseKind,
) -> PrivateDeliveryResponse:
    """Capture one fresh candidate response; this never authorizes a write."""

    if type(kind) is not PrivateDeliveryResponseKind:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_RESPONSE_KIND_INVALID")
    record = _verified_review_record(review)
    with record.lock:
        _verified_review_record(review)
        if record.captured_response_id is not None:
            raise PrivateDeliveryError("PRIVATE_DELIVERY_RESPONSE_ALREADY_CAPTURED")
        captured = _sample_clock(review._clock)
        if captured < record.last_checked_at:
            raise PrivateDeliveryError("PRIVATE_DELIVERY_CLOCK_ROLLBACK")
        record.last_checked_at = captured
        _verified_review_record(review)
        if captured >= review.expires_at:
            raise PrivateDeliveryError("PRIVATE_DELIVERY_REVIEW_EXPIRED")
        if kind in {
            PrivateDeliveryResponseKind.ACCEPT_HTML_PREVIEW_CANDIDATE,
            PrivateDeliveryResponseKind.ACCEPT_HTML_ICS_READY_BUNDLE_CANDIDATE,
        } and kind is not review.candidate_response_kind:
            raise PrivateDeliveryError(
                "PRIVATE_DELIVERY_RESPONSE_PROFILE_MISMATCH"
            )
        candidate_accepted = kind is review.candidate_response_kind
        response_id = _digest_json(
            {
                "contract_version": PRIVATE_DELIVERY_RESPONSE_VERSION,
                "review_id": review.review_id,
                "profile": review.profile.value,
                "kind": kind.value,
                "candidate_accepted": candidate_accepted,
                "captured_at": _utc_iso(captured),
            },
            prefix="private-delivery-response",
        )
        response = PrivateDeliveryResponse(
            kind=kind,
            profile=review.profile,
            candidate_accepted=candidate_accepted,
            review_id=review.review_id,
            response_id=response_id,
            captured_at=captured,
            _review=review,
            _token=_RESPONSE_TOKEN,
        )
        _register_response(response)
        record.captured_response_id = response.response_id
        return response


def _manifest_bytes(
    *,
    profile: PrivateDeliveryProfile,
    canonical_bytes: bytes,
    composed: ComposedTripState,
    snapshot: EvidenceSnapshot,
    html: PrivateHtmlProjection,
    ics: PrivateIcsProjection | None,
    readiness: TripReadiness | None,
    runtime_context_sha256: str,
    payload_artifacts: tuple[PrivateDeliveryArtifact, ...],
) -> bytes:
    readiness_payload: dict[str, object]
    if readiness is None:
        readiness_payload = {"required": False, "status": "not_assessed"}
    else:
        readiness_payload = {
            "required": True,
            "status": readiness.status.value,
            "readiness_contract_version": READINESS_VERSION,
            "readiness_id": readiness.readiness_id,
            "evaluated_at": _utc_iso(readiness.evaluated_at),
            "recheck_required_at": (
                _utc_iso(readiness.recheck_required_at)
                if readiness.recheck_required_at is not None
                else None
            ),
            "kernel_report_digest": readiness.kernel_report_digest,
            "canonical_lodging_digest": readiness.canonical_lodging_digest,
            "evidence_snapshot_id": readiness.evidence_snapshot_id,
        }
    projector_payload: dict[str, object] = {
        "html": {
            "contract_version": html.contract_version,
            "renderer_version": html.renderer_version,
            "template_version": html.template_version,
            "input_sha256": html.input_sha256,
        }
    }
    if ics is not None:
        projector_payload["ics"] = {
            "contract_version": ics.contract_version,
            "uid_policy_version": PRIVATE_ICS_UID_POLICY_VERSION,
            "input_sha256": ics.input_sha256,
            "generated_at": _utc_iso(readiness.evaluated_at),
        }
    payload = {
        "contract_version": PRIVATE_DELIVERY_MANIFEST_VERSION,
        "profile": profile.value,
        "visibility": "private",
        "safe_to_publish": False,
        "source": {
            "canonical_source_sha256": hashlib.sha256(canonical_bytes).hexdigest(),
            "composition_contract_version": COMPOSITION_VERSION,
            "evidence_snapshot_contract_version": EVIDENCE_SNAPSHOT_VERSION,
            "trip_id": composed.trip_id,
            "plan_revision": composed.plan_revision,
            "canonical_state_digest": composed.canonical_state_digest,
            "composed_state_digest": composed.composed_state_digest,
            "evidence_binding_digest": composed.evidence.binding_digest,
            "runtime_context_sha256": runtime_context_sha256,
            "evidence_snapshot_id": snapshot.snapshot_id,
            "policy_registry_revision": snapshot.policies.revision,
            "store_revision": snapshot.store_revision,
            "evidence_revision": snapshot.evidence_revision,
            "outcome_revision": snapshot.outcome_revision,
        },
        "projectors": projector_payload,
        "readiness": readiness_payload,
        "artifacts": [
            {
                "filename": item.filename,
                "media_type": item.media_type,
                "bytes": len(item.payload),
                "sha256": item.sha256,
            }
            for item in payload_artifacts
        ],
    }
    encoded = _canonical_json_bytes(payload)
    if len(encoded) > MAX_PRIVATE_DELIVERY_MANIFEST_BYTES:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_MANIFEST_LIMIT_EXCEEDED")
    return encoded


def _private_review_bytes(
    *,
    profile: PrivateDeliveryProfile,
    source_path: str,
    target_path: str,
    state: TripState,
    artifacts: tuple[PrivateDeliveryArtifact, ...],
    readiness: TripReadiness | None,
    uid_namespace: str,
    created_at: datetime,
    expires_at: datetime,
) -> bytes:
    readiness_payload: dict[str, object]
    if readiness is None:
        readiness_payload = {
            "required": False,
            "status": "not_assessed",
            "statement": "HTML preview does not assess or claim travel readiness.",
        }
    else:
        readiness_payload = {
            "required": True,
            "status": readiness.status.value,
            "evaluated_at": _utc_iso(readiness.evaluated_at),
            "recheck_required_at": (
                _utc_iso(readiness.recheck_required_at)
                if readiness.recheck_required_at is not None
                else None
            ),
        }
    payload: dict[str, object] = {
        "contract_version": PRIVATE_DELIVERY_VERSION,
        "profile": profile.value,
        "source_path": source_path,
        "target_path": target_path,
        "source_path_verified": False,
        "target_filesystem_verified": False,
        "write_authorized": False,
        "create_only": True,
        "overwrite_allowed": False,
        "artifact_filenames": [item.filename for item in artifacts],
        "candidate_response_kind": _ACCEPT_FOR_PROFILE[profile].value,
        "other_response_kinds": [
            PrivateDeliveryResponseKind.REQUEST_CHANGES.value,
            PrivateDeliveryResponseKind.CANCEL.value,
        ],
        "created_at": _utc_iso(created_at),
        "expires_at": _utc_iso(expires_at),
        "trip": {
            "title": state.title,
            "dates": [day.date.isoformat() for day in state.days],
        },
        "readiness": readiness_payload,
    }
    if readiness is not None:
        payload["calendar_events"] = _private_calendar_event_review(
            state,
            uid_namespace=uid_namespace,
        )
    encoded = _canonical_json_bytes(payload)
    if len(encoded) > MAX_PRIVATE_DELIVERY_REVIEW_BYTES:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_REVIEW_LIMIT_EXCEEDED")
    return encoded


def _private_calendar_event_review(
    state: TripState,
    *,
    uid_namespace: str,
) -> list[dict[str, object]]:
    activities = {item.activity_id: item for item in state.activities}
    projected: list[tuple[datetime, str, dict[str, object]]] = []
    active = {
        DecisionState.SELECTED,
        DecisionState.FIXED,
        DecisionState.BOOKED,
    }
    for day in state.days:
        for activity_id in day.activity_ids:
            activity = activities.get(activity_id)
            if activity is None or activity.decision_state not in active:
                continue
            try:
                start_utc, end_utc = _event_interval(day, activity)
                uid = _uid_for(uid_namespace, activity.activity_id)
                zone = ZoneInfo(day.timezone or "")
                start_local = start_utc.astimezone(zone)
                end_local = end_utc.astimezone(zone)
            except Exception:
                raise PrivateDeliveryError(
                    "PRIVATE_DELIVERY_CALENDAR_REVIEW_FAILED"
                ) from None
            projected.append(
                (
                    start_utc,
                    uid,
                    {
                        "summary": activity.title,
                        "local_start": start_local.isoformat(),
                        "local_end": end_local.isoformat(),
                        "timezone": day.timezone,
                    },
                )
            )
    return [item[2] for item in sorted(projected, key=lambda item: item[:2])]


def _artifact(
    filename: str,
    media_type: str,
    payload: bytes,
) -> PrivateDeliveryArtifact:
    return PrivateDeliveryArtifact(
        filename=filename,
        media_type=media_type,
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        _token=_ARTIFACT_TOKEN,
    )


def _runtime_context_sha256(
    composed: ComposedTripState,
    *,
    availability_keys: tuple[FactKey, ...],
    lodging_intake: LodgingIntakeAssessment | None,
    pending_lodging_review: LodgingConfirmationReview | None,
) -> str:
    if (
        lodging_intake is not None
        and type(lodging_intake) is not LodgingIntakeAssessment
    ):
        raise PrivateDeliveryError("PRIVATE_DELIVERY_TYPED_INPUT_REQUIRED")
    if (
        pending_lodging_review is not None
        and type(pending_lodging_review) is not LodgingConfirmationReview
    ):
        raise PrivateDeliveryError("PRIVATE_DELIVERY_TYPED_INPUT_REQUIRED")
    availability_payload = []
    for item in composed.activity_availability:
        availability_payload.append(
            {
                "activity_id": item.activity_id,
                "disposition": item.disposition.value,
                "intervals": [
                    {
                        "start_at": _utc_iso(interval.start_at),
                        "end_at": _utc_iso(interval.end_at),
                    }
                    for interval in item.intervals
                ],
                "evidence_refs": list(item.evidence_refs),
                "reason": item.reason,
                "fresh_until": (
                    _utc_iso(item.fresh_until)
                    if item.fresh_until is not None
                    else None
                ),
            }
        )
    live_attribution_payload = [
        {
            "observation_id": item.observation_id,
            "provider_id": item.provider_id,
            "label": item.label,
            "uri": item.uri,
        }
        for item in composed.live_attributions
    ]
    return _digest_json(
        {
            "composition_contract_version": composed.contract_version,
            "trip_id": composed.trip_id,
            "plan_revision": composed.plan_revision,
            "canonical_state_digest": composed.canonical_state_digest,
            "composed_state_digest": composed.composed_state_digest,
            "evidence": composed.evidence.to_dict(),
            "availability_key_ids": sorted(
                item.key_id for item in availability_keys
            ),
            "activity_availability": availability_payload,
            "live_attributions": live_attribution_payload,
            "lodging_intake_assessment": (
                lodging_intake.to_dict()
                if lodging_intake is not None
                else None
            ),
            "pending_lodging_review": (
                {
                    "review_id": pending_lodging_review.review_id,
                    "state": pending_lodging_review.state.value,
                }
                if pending_lodging_review is not None
                else None
            ),
        },
        prefix="private-delivery-runtime-context",
    )


def _validate_lodging_issue_shape(value: object) -> None:
    if (
        type(value) is not LodgingIntakeIssue
        or not _bounded_exact_text(value.code)
        or type(value.severity) is not IssueSeverity
        or not _bounded_exact_text(value.message)
        or type(value.candidate_ids) is not tuple
        or len(value.candidate_ids) > MAX_PRIVATE_DELIVERY_LODGING_CANDIDATES
        or any(not _bounded_exact_text(item) for item in value.candidate_ids)
        or type(value.nights) is not tuple
        or len(value.nights) > 366
        or any(type(item) is not date for item in value.nights)
        or type(value.suggested_actions) is not tuple
        or len(value.suggested_actions) > MAX_PRIVATE_DELIVERY_POLICY_ITEMS
        or any(not _bounded_exact_text(item) for item in value.suggested_actions)
    ):
        raise ValueError("lodging intake issue shape is invalid")


def _validate_lodging_assessment_shape(
    value: object,
) -> LodgingIntakeAssessment:
    if (
        type(value) is not LodgingIntakeAssessment
        or type(value.status) is not LodgingIntakeStatus
        or type(value.requirement) is not LodgingRequirement
        or type(value.stay_start) is not date
        or type(value.stay_end) is not date
        or type(value.candidates) is not tuple
        or len(value.candidates) > MAX_PRIVATE_DELIVERY_LODGING_CANDIDATES
        or not _bounded_exact_text(value.assessment_id)
        or not _bounded_exact_text(value.contract_version)
    ):
        raise ValueError("lodging intake shape is invalid")
    for name in (
        "required_nights",
        "option_missing_nights",
        "undecided_nights",
        "conflicting_nights",
    ):
        dates = getattr(value, name)
        if (
            type(dates) is not tuple
            or len(dates) > 366
            or any(type(item) is not date for item in dates)
        ):
            raise ValueError("lodging intake date inventory is invalid")
    if (
        type(value.needs_verification_candidate_ids) is not tuple
        or len(value.needs_verification_candidate_ids)
        > MAX_PRIVATE_DELIVERY_LODGING_CANDIDATES
        or any(
            not _bounded_exact_text(item)
            for item in value.needs_verification_candidate_ids
        )
        or type(value.issues) is not tuple
        or len(value.issues) > MAX_PRIVATE_DELIVERY_POLICY_ITEMS
    ):
        raise ValueError("lodging intake derived inventory is invalid")
    for issue in value.issues:
        _validate_lodging_issue_shape(issue)
    return value


def _validated_lodging_inputs(
    lodging_intake: LodgingIntakeAssessment | None,
    pending_lodging_review: LodgingConfirmationReview | None,
) -> tuple[
    LodgingIntakeAssessment | None,
    LodgingConfirmationReview | None,
]:
    try:
        if lodging_intake is None:
            validated_intake = None
        else:
            _validate_lodging_assessment_shape(lodging_intake)
            candidates = tuple(
                _rebuilt_lodging_candidate(item)
                for item in lodging_intake.candidates
            )
            validated_intake = assess_lodging_intake(
                stay_start=lodging_intake.stay_start,
                stay_end=lodging_intake.stay_end,
                requirement=lodging_intake.requirement,
                candidates=candidates,
            )
            if validated_intake != lodging_intake:
                raise ValueError("lodging intake differs from reassessment")

        if pending_lodging_review is None:
            validated_review = None
        else:
            if (
                type(pending_lodging_review) is not LodgingConfirmationReview
                or type(pending_lodging_review.state)
                is not LodgingConfirmationState
                or any(
                    not _bounded_exact_text(item)
                    for item in (
                        pending_lodging_review.request_id,
                        pending_lodging_review.trip_id,
                        pending_lodging_review.base_revision,
                        pending_lodging_review.review_id,
                    )
                )
                or not _exact_factory_utc(pending_lodging_review.created_at)
                or not _exact_factory_utc(pending_lodging_review.expires_at)
                or type(pending_lodging_review.stay_count) is not int
                or not 0 <= pending_lodging_review.stay_count <= 128
                or type(pending_lodging_review.stay_night_count) is not int
                or not 0 <= pending_lodging_review.stay_night_count <= 366
                or type(pending_lodging_review.affected_day_ids) is not tuple
                or len(pending_lodging_review.affected_day_ids) > 128
                or any(
                    not _bounded_exact_text(item)
                    for item in pending_lodging_review.affected_day_ids
                )
                or type(pending_lodging_review.decision_states) is not tuple
                or len(pending_lodging_review.decision_states) > 3
                or any(
                    not _bounded_exact_text(item)
                    or item not in {
                        DecisionState.SELECTED.value,
                        DecisionState.FIXED.value,
                        DecisionState.BOOKED.value,
                    }
                    for item in pending_lodging_review.decision_states
                )
                or type(pending_lodging_review.reviewed_itinerary) is not bool
                or any(
                    item is not None and not _bounded_exact_text(item)
                    for item in (
                        pending_lodging_review.patch_digest,
                        pending_lodging_review.required_lodging_confirmation_scope,
                        pending_lodging_review.required_store_approval_scope,
                        pending_lodging_review.expected_state_digest,
                        pending_lodging_review.expected_applied_revision,
                        pending_lodging_review.check_status,
                    )
                )
                or type(pending_lodging_review.problems) is not tuple
                or len(pending_lodging_review.problems)
                > MAX_PRIVATE_DELIVERY_LODGING_PROBLEMS
            ):
                raise ValueError("lodging review shape is invalid")
            problems: list[LodgingConfirmationProblem] = []
            for item in pending_lodging_review.problems:
                if (
                    type(item) is not LodgingConfirmationProblem
                    or not _bounded_exact_text(item.code)
                    or not _bounded_exact_text(item.message)
                ):
                    raise ValueError("lodging review problem is invalid")
                problems.append(
                    LodgingConfirmationProblem(
                        code=item.code,
                        message=item.message,
                    )
                )
            validated_review = replace(
                pending_lodging_review,
                problems=tuple(problems),
            )
            if validated_review.review_id != pending_lodging_review.review_id:
                raise ValueError("lodging review differs from rebuilt content")
            if validated_review != pending_lodging_review:
                raise ValueError("lodging review is not factory-normalized")
    except Exception:
        raise PrivateDeliveryError(
            "PRIVATE_DELIVERY_LODGING_CONTEXT_INVALID"
        ) from None
    return validated_intake, validated_review


def _rebuilt_lodging_candidate(value: object) -> LodgingCandidate:
    if type(value) is not LodgingCandidate:
        raise ValueError("lodging candidate must be exact")
    draft = value.draft
    if (
        type(draft) is not LodgingIntentDraft
        or type(draft.kind) is not LodgingKind
        or not _bounded_exact_text(draft.label)
        or type(draft.location) is not LocationHint
        or type(draft.check_in) is not date
        or type(draft.check_out) is not date
        or (
            draft.price_amount_minor is not None
            and (
                type(draft.price_amount_minor) is not int
                or not 0 <= draft.price_amount_minor <= 10**15
            )
        )
        or (
            draft.currency is not None
            and not _bounded_exact_text(draft.currency)
        )
        or (draft.price_basis is not None and type(draft.price_basis) is not PriceBasis)
        or (
            draft.price_is_estimate is not None
            and type(draft.price_is_estimate) is not bool
        )
        or not _bounded_exact_text(draft.draft_id)
        or type(value.decision_state) is not DecisionState
        or type(value.evidence_state) is not EvidenceState
        or type(value.authority) is not IntentAuthority
        or type(value.evidence_refs) is not tuple
        or value.evidence_refs
        or not _bounded_exact_text(value.candidate_id)
        or not _bounded_exact_text(value.contract_version)
    ):
        raise ValueError("lodging candidate shape is invalid")
    if (
        draft.check_out <= draft.check_in
        or (draft.check_out - draft.check_in).days > 366
    ):
        raise ValueError("lodging draft dates are invalid")

    location = draft.location
    if (
        type(location.kind) is not LocationHintKind
        or not _bounded_exact_text(location.label)
        or any(
            item is not None and not _bounded_exact_text(item)
            for item in (
                location.location_id,
                location.provider_place_id,
                location.input_text,
                location.country_code,
            )
        )
        or any(
            item is not None
            and (
                type(item) is not float
                or not math.isfinite(item)
            )
            for item in (location.latitude, location.longitude)
        )
        or (
            location.radius_m is not None
            and (
                type(location.radius_m) is not int
                or not 1 <= location.radius_m <= 10**9
            )
        )
        or not _bounded_exact_text(location.location_digest)
    ):
        raise ValueError("lodging location shape is invalid")
    rebuilt_location = replace(location)
    if rebuilt_location.location_digest != location.location_digest:
        raise ValueError("lodging location identity drifted")

    claim = draft.reported_decision
    rebuilt_claim: ReportedDecisionClaim | None = None
    if claim is not None:
        if (
            type(claim) is not ReportedDecisionClaim
            or type(claim.decision_state) is not DecisionState
            or not _bounded_exact_text(claim.source_ref)
            or not _bounded_exact_text(claim.claim_id)
        ):
            raise ValueError("reported lodging claim shape is invalid")
        rebuilt_claim = replace(claim)
        if rebuilt_claim.claim_id != claim.claim_id:
            raise ValueError("reported lodging claim identity drifted")

    rebuilt_draft = replace(
        draft,
        location=rebuilt_location,
        reported_decision=rebuilt_claim,
    )
    if rebuilt_draft.draft_id != draft.draft_id:
        raise ValueError("lodging draft identity drifted")
    rebuilt_candidate = bind_lodging_candidate(
        rebuilt_draft,
        authority=value.authority,
    )
    if rebuilt_candidate.candidate_id != value.candidate_id:
        raise ValueError("lodging candidate identity drifted")
    if rebuilt_candidate != value:
        raise ValueError("lodging candidate content drifted")
    return rebuilt_candidate


def _validated_availability_keys(
    availability_keys: object,
) -> tuple[FactKey, ...]:
    if (
        type(availability_keys) is not tuple
        or len(availability_keys) > MAX_PRIVATE_HTML_ACTIVITIES
        or any(type(item) is not FactKey for item in availability_keys)
    ):
        raise PrivateDeliveryError("PRIVATE_DELIVERY_AVAILABILITY_INVALID")
    try:
        for item in availability_keys:
            _validate_fact_key_shape(item)
        rebuilt = tuple(replace(item) for item in availability_keys)
        if rebuilt != availability_keys:
            raise ValueError("availability key differs from validated content")
        if len({item.key_id for item in rebuilt}) != len(rebuilt):
            raise ValueError("availability keys repeat an identity")
    except Exception:
        raise PrivateDeliveryError(
            "PRIVATE_DELIVERY_AVAILABILITY_INVALID"
        ) from None
    return rebuilt


def _bounded_exact_text(
    value: object,
    *,
    maximum: int = MAX_PRIVATE_DELIVERY_PATH_CHARS,
) -> bool:
    return type(value) is str and len(value) <= maximum


def _exact_text_tuple(
    value: object,
    *,
    maximum: int = MAX_PRIVATE_DELIVERY_POLICY_ITEMS,
    allow_empty: bool = True,
) -> bool:
    return (
        type(value) is tuple
        and len(value) <= maximum
        and (allow_empty or bool(value))
        and all(_bounded_exact_text(item) for item in value)
    )


def _exact_factory_utc(value: object) -> bool:
    """Recognize timestamps already normalized by trusted factories.

    This deliberately does not call a caller-owned ``tzinfo`` method.
    Generic trusted-clock values are normalized separately by ``_sample_clock``.
    """

    return (
        type(value) is datetime
        and value.tzinfo is _UTC
        and value.fold == 0
    )


def _validate_provider_policy_shape(policy: object) -> None:
    if type(policy) is not ProviderPolicy:
        raise ValueError("snapshot policy is invalid")
    if any(
        not _bounded_exact_text(value)
        for value in (
            policy.policy_id,
            policy.provider_id,
            policy.adapter_id,
            policy.adapter_version,
            policy.contract_region,
            policy.policy_digest,
        )
    ):
        raise ValueError("snapshot policy text is invalid")
    if (
        type(policy.allowed_fact_kinds) is not tuple
        or not policy.allowed_fact_kinds
        or len(policy.allowed_fact_kinds) > len(FactKind)
        or any(type(item) is not FactKind for item in policy.allowed_fact_kinds)
        or not _exact_text_tuple(
            policy.allowed_value_fields,
            allow_empty=False,
        )
        or not _exact_text_tuple(
            policy.allowed_operations,
            allow_empty=False,
        )
        or not _exact_text_tuple(policy.allowed_query_fields)
        or not _exact_text_tuple(policy.required_attribution_labels)
        or type(policy.persistence) is not EvidencePersistence
        or type(policy.max_validity_seconds) is not int
        or not 0 < policy.max_validity_seconds <= 2**63 - 1
        or (
            policy.max_retention_seconds is not None
            and (
                type(policy.max_retention_seconds) is not int
                or not 0 < policy.max_retention_seconds <= 2**63 - 1
            )
        )
    ):
        raise ValueError("snapshot policy shape is invalid")


def _validate_fact_key_shape(key: object) -> None:
    if (
        type(key) is not FactKey
        or type(key.kind) is not FactKind
        or not _bounded_exact_text(key.contract_version)
        or not _bounded_exact_text(key.key_id)
        or type(key.subject_ids) is not tuple
        or not key.subject_ids
        or len(key.subject_ids) > 16
        or any(not _bounded_exact_text(item) for item in key.subject_ids)
        or type(key.qualifiers) is not tuple
        or len(key.qualifiers) > 32
    ):
        raise ValueError("fact key shape is invalid")
    for item in key.qualifiers:
        if (
            type(item) is not tuple
            or len(item) != 2
            or not _bounded_exact_text(item[0])
            or type(item[1]) not in {str, int, float, bool, type(None)}
            or (
                type(item[1]) is str
                and not _bounded_exact_text(item[1])
            )
            or (
                type(item[1]) is int
                and not -(2**63) <= item[1] <= 2**63 - 1
            )
            or (
                type(item[1]) is float
                and not math.isfinite(item[1])
            )
        ):
            raise ValueError("fact key qualifier is invalid")


def _validate_fact_value_shape(value: object) -> None:
    if (
        type(value) is not FactValue
        or type(value.kind) is not FactKind
        or not _bounded_exact_text(value.schema_version)
        or type(value.canonical_json) is not bytes
        or not _bounded_exact_text(value.value_digest)
    ):
        raise ValueError("fact value shape is invalid")


def _validate_provenance_shape(value: object) -> None:
    if type(value) is not ProviderProvenance:
        raise ValueError("provider provenance is invalid")
    if any(
        not _bounded_exact_text(item)
        for item in (
            value.provider_id,
            value.adapter_id,
            value.adapter_version,
            value.request_fingerprint,
            value.retention_policy_id,
        )
    ) or any(
        item is not None and not _bounded_exact_text(item)
        for item in (
            value.provider_record_id,
            value.response_id,
            value.source_uri,
        )
    ):
        raise ValueError("provider provenance text is invalid")
    if type(value.attributions) is not tuple or len(value.attributions) > 32:
        raise ValueError("provider attribution shape is invalid")
    for item in value.attributions:
        if (
            type(item) is not tuple
            or len(item) != 2
            or not _bounded_exact_text(item[0])
            or (
                item[1] is not None
                and not _bounded_exact_text(item[1])
            )
        ):
            raise ValueError("provider attribution item is invalid")


def _validate_observation_shape(value: object) -> None:
    if (
        type(value) is not FactObservation
        or not _bounded_exact_text(value.contract_version)
        or not _bounded_exact_text(value.observation_id)
        or type(value.confidence) is not float
        or not math.isfinite(value.confidence)
        or not 0.0 <= value.confidence <= 1.0
        or not _exact_factory_utc(value.retrieved_at)
        or not _exact_factory_utc(value.valid_until)
        or (
            value.purge_at is not None
            and not _exact_factory_utc(value.purge_at)
        )
    ):
        raise ValueError("fact observation shape is invalid")
    _validate_fact_key_shape(value.key)
    _validate_fact_value_shape(value.value)
    _validate_provenance_shape(value.provenance)


def _validate_evidence_snapshot(snapshot: EvidenceSnapshot) -> None:
    """Recompute every nested evidence identity before delivery projection.

    Evidence snapshots originate at the trusted host boundary, but frozen
    dataclasses are not a security boundary inside one Python process.  This
    verifier prevents process-local field mutation from preserving stale
    value, observation, policy, or snapshot identifiers.
    """

    try:
        if type(snapshot) is not EvidenceSnapshot:
            raise ValueError("snapshot must be exact")
        if (
            type(snapshot.contract_version) is not str
            or snapshot.contract_version != EVIDENCE_SNAPSHOT_VERSION
            or type(snapshot.policies) is not ProviderPolicyRegistry
            or type(snapshot.policies.policies) is not tuple
            or not snapshot.policies.policies
            or len(snapshot.policies.policies) > MAX_PRIVATE_DELIVERY_POLICIES
            or type(snapshot.observations) is not tuple
            or len(snapshot.observations) > MAX_PRIVATE_DELIVERY_EVIDENCE_ITEMS
            or not _bounded_exact_text(snapshot.policies.revision)
            or not _exact_factory_utc(snapshot.evaluation_at)
            or not _exact_factory_utc(snapshot.purge_checked_at)
            or not _bounded_exact_text(snapshot.store_revision)
            or not _bounded_exact_text(snapshot.evidence_revision)
            or not _bounded_exact_text(snapshot.snapshot_id)
            or (
                snapshot.outcome_revision is not None
                and not _bounded_exact_text(snapshot.outcome_revision)
            )
        ):
            raise ValueError("snapshot aggregate is invalid")

        policies: list[ProviderPolicy] = []
        for policy in snapshot.policies.policies:
            _validate_provider_policy_shape(policy)
            validated_policy = replace(policy)
            if validated_policy != policy:
                raise ValueError("snapshot policy identity drifted")
            policies.append(validated_policy)
        validated_registry = ProviderPolicyRegistry(
            policies=tuple(policies),
            revision=snapshot.policies.revision,
        )
        if validated_registry != snapshot.policies:
            raise ValueError("snapshot policy registry drifted")

        observations: list[FactObservation] = []
        slots: set[tuple[str, str]] = set()
        evidence_bytes = 0
        evaluation_at = _utc_datetime(
            snapshot.evaluation_at,
            "PRIVATE_DELIVERY_EVIDENCE_SNAPSHOT_INVALID",
            whole_second=False,
        )
        purge_checked_at = _utc_datetime(
            snapshot.purge_checked_at,
            "PRIVATE_DELIVERY_EVIDENCE_SNAPSHOT_INVALID",
            whole_second=False,
        )
        _validate_digest(snapshot.store_revision)
        _validate_digest(snapshot.evidence_revision)
        _validate_digest(snapshot.snapshot_id)
        if snapshot.outcome_revision is not None:
            _validate_digest(snapshot.outcome_revision)
        for observation in snapshot.observations:
            _validate_observation_shape(observation)
            evidence_bytes += len(observation.value.canonical_json)
            if evidence_bytes > MAX_PRIVATE_DELIVERY_EVIDENCE_BYTES:
                raise ValueError("snapshot evidence bytes exceed the bound")
            validated_observation = replace(
                observation,
                key=replace(observation.key),
                value=replace(observation.value),
                provenance=replace(observation.provenance),
            )
            if validated_observation != observation:
                raise ValueError("snapshot observation identity drifted")
            validated_registry.validate_observation(validated_observation)
            if (
                not validated_observation.retained_at(purge_checked_at)
                or validated_observation.retrieved_at > purge_checked_at
                or validated_observation.source_slot in slots
            ):
                raise ValueError("snapshot observation is not retained")
            slots.add(validated_observation.source_slot)
            observations.append(validated_observation)

        normalized = tuple(
            sorted(
                observations,
                key=lambda item: (
                    item.key.key_id,
                    item.provenance.provider_id,
                    item.observation_id,
                ),
            )
        )
        if normalized != snapshot.observations:
            raise ValueError("snapshot observation order drifted")
        expected_evidence_revision = _fact_contract_digest(
            {"observation_ids": [item.observation_id for item in normalized]},
            prefix="active-evidence",
        )
        if snapshot.evidence_revision != expected_evidence_revision:
            raise ValueError("snapshot evidence revision drifted")
        snapshot_payload: dict[str, object] = {
            "contract_version": snapshot.contract_version,
            "policy_registry_revision": validated_registry.revision,
            "store_revision": snapshot.store_revision,
            "evidence_revision": expected_evidence_revision,
            "evaluation_at": _utc_iso(evaluation_at),
            "purge_checked_at": _utc_iso(purge_checked_at),
        }
        if snapshot.outcome_revision is not None:
            snapshot_payload["outcome_revision"] = snapshot.outcome_revision
        expected_snapshot_id = _fact_contract_digest(
            snapshot_payload,
            prefix="evidence-snapshot",
        )
        if snapshot.snapshot_id != expected_snapshot_id:
            raise ValueError("snapshot identity drifted")
    except PrivateDeliveryError:
        raise
    except Exception:
        raise PrivateDeliveryError(
            "PRIVATE_DELIVERY_EVIDENCE_SNAPSHOT_INVALID"
        ) from None


def _sample_clock(clock: object) -> datetime:
    if not callable(clock):
        raise PrivateDeliveryError("PRIVATE_DELIVERY_CLOCK_INVALID")
    try:
        value = clock()
    except Exception:
        raise PrivateDeliveryError("PRIVATE_DELIVERY_CLOCK_INVALID") from None
    return _utc_datetime(
        value,
        "PRIVATE_DELIVERY_CLOCK_INVALID",
        whole_second=False,
    )


def _absolute_path(value: object, code: str, *, require_safe_leaf: bool = False) -> str:
    if type(value) is not str or not value or len(value) > MAX_PRIVATE_DELIVERY_PATH_CHARS:
        raise PrivateDeliveryError(code)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise PrivateDeliveryError(code) from None
    if len(encoded) > MAX_PRIVATE_DELIVERY_PATH_CHARS or value.startswith("//"):
        raise PrivateDeliveryError(code)
    if any(
        ord(char) < 32
        or ord(char) == 127
        or 0xD800 <= ord(char) <= 0xDFFF
        or unicodedata.category(char).startswith("C")
        for char in value
    ):
        raise PrivateDeliveryError(code)
    try:
        path = PurePosixPath(value)
    except Exception:
        raise PrivateDeliveryError(code) from None
    if (
        not path.is_absolute()
        or str(path) != value
        or value == "/"
        or any(part in {"", ".", ".."} for part in path.parts[1:])
        or any(len(part.encode("utf-8")) > 255 for part in path.parts[1:])
    ):
        raise PrivateDeliveryError(code)
    if require_safe_leaf and _SAFE_LEAF_RE.fullmatch(path.name) is None:
        raise PrivateDeliveryError(code)
    return value


def _utc_datetime(value: object, code: str, *, whole_second: bool) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.fold != 0:
        raise PrivateDeliveryError(code)
    try:
        if value.utcoffset() is None:
            raise ValueError("missing UTC offset")
        normalized = value.astimezone(_UTC)
    except Exception:
        raise PrivateDeliveryError(code) from None
    if whole_second and normalized.microsecond:
        raise PrivateDeliveryError(code)
    return normalized


def _sealed_utc_datetime(value: object, code: str) -> datetime:
    """Validate a factory-normalized UTC value without calling custom tzinfo."""

    if not _exact_factory_utc(value):
        raise PrivateDeliveryError(code)
    return value


def _validate_readiness_shape(value: object) -> TripReadiness:
    if type(value) is not TripReadiness:
        raise ValueError("readiness must be exact")
    if any(
        type(item) is not str
        or len(item) > MAX_PRIVATE_DELIVERY_PATH_CHARS
        for item in (
            value.trip_ref,
            value.plan_revision,
            value.canonical_state_digest,
            value.composed_state_digest,
            value.kernel_report_digest,
            value.canonical_lodging_digest,
            value.evidence_binding_digest,
            value.evidence_snapshot_id,
            value.readiness_id,
        )
    ):
        raise ValueError("readiness identity is invalid")
    if (
        not _exact_factory_utc(value.evaluated_at)
        or (
            value.recheck_required_at is not None
            and not _exact_factory_utc(value.recheck_required_at)
        )
        or type(value.status) is not ReadinessStatus
        or type(value.next_action) is not ReadinessAction
        or type(value.used_evidence_count) is not int
        or not 0 <= value.used_evidence_count <= MAX_PRIVATE_DELIVERY_EVIDENCE_ITEMS
        or type(value.lodging_stay_count) is not int
        or not 0 <= value.lodging_stay_count <= MAX_PRIVATE_HTML_ACTIVITIES
        or type(value.lodging_night_count) is not int
        or not 0 <= value.lodging_night_count <= MAX_PRIVATE_HTML_ACTIVITIES
        or type(value.problems) is not tuple
        or len(value.problems) > MAX_PRIVATE_DELIVERY_EVIDENCE_ITEMS
    ):
        raise ValueError("readiness shape is invalid")
    for problem in value.problems:
        if (
            type(problem) is not ReadinessProblem
            or type(problem.code) is not str
            or len(problem.code) > 128
            or type(problem.severity) is not IssueSeverity
            or type(problem.source) is not ReadinessSource
            or type(problem.next_action) is not ReadinessAction
            or type(problem.affected_count) is not int
            or not 1 <= problem.affected_count <= MAX_PRIVATE_HTML_ACTIVITIES
        ):
            raise ValueError("readiness problem shape is invalid")
    return value


def _utc_iso(value: datetime) -> str:
    return value.astimezone(_UTC).isoformat().replace("+00:00", "Z")


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
        raise PrivateDeliveryError("PRIVATE_DELIVERY_MANIFEST_INVALID") from None


def _digest_json(value: object, *, prefix: str) -> str:
    payload = prefix.encode("ascii") + b"\n" + _canonical_json_bytes(value)
    return hashlib.sha256(payload).hexdigest()


def _fact_contract_digest(value: object, *, prefix: str) -> str:
    """Match the evidence kernel's stable identity encoding."""

    try:
        encoded = json.dumps(
            {"prefix": prefix, "payload": value},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except Exception:
        raise ValueError("fact contract identity is invalid") from None
    return hashlib.sha256(encoded).hexdigest()


def _validate_digest(value: object) -> None:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError("expected lowercase SHA-256 digest")


def _validate_state_digest(value: object) -> None:
    if (
        type(value) is not str
        or not value.startswith("sha256:")
        or _SHA256_RE.fullmatch(value[7:]) is None
    ):
        raise ValueError("expected state digest")


__all__ = [
    "MAX_PRIVATE_DELIVERY_MANIFEST_BYTES",
    "MAX_PRIVATE_DELIVERY_EVIDENCE_BYTES",
    "MAX_PRIVATE_DELIVERY_EVIDENCE_ITEMS",
    "MAX_PRIVATE_DELIVERY_PATH_CHARS",
    "MAX_PRIVATE_DELIVERY_REVIEW_BYTES",
    "PRIVATE_DELIVERY_MANIFEST_VERSION",
    "PRIVATE_DELIVERY_RESPONSE_VERSION",
    "PRIVATE_DELIVERY_REVIEW_TTL",
    "PRIVATE_DELIVERY_VERSION",
    "PrivateDeliveryArtifact",
    "PrivateDeliveryError",
    "PrivateDeliveryProfile",
    "PrivateDeliveryResponse",
    "PrivateDeliveryResponseKind",
    "PrivateDeliveryReview",
    "capture_private_delivery_response",
    "prepare_private_delivery_review",
    "verify_private_delivery_response",
]
