"""Offline Google Places identity review and promotion boundary.

This module deliberately does not perform HTTP I/O.  A future transport may
feed one bounded Places Text Search response into the pure review functions
below.  Only a reviewed place ID can become a provider observation; names,
addresses, types, and coordinates remain run-scoped review material.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from .facts import (
    AuthorizedProviderResult,
    EvidencePersistence,
    EvidenceSnapshot,
    FactContractError,
    FactKey,
    FactKind,
    FactObservation,
    FactValue,
    ProviderProvenance,
    ProviderRequest,
    ProviderResult,
    ProviderResultStatus,
    _GOOGLE_PLACE_IDENTITY_AUTHORIZATION_TOKEN,
    _authorize_google_place_identity_result,
)
from .models import EvidenceState


PLACE_IDENTITY_REVIEW_VERSION = "place-identity-review/v1"

# Text Search review fields are intentionally separate from the durable value
# policy.  Only ``provider_place_id`` may enter FactValue/EvidenceStore.
GOOGLE_PLACE_IDENTITY_FIELD_MASK = (
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.location",
    "places.primaryType",
    "places.types",
    "places.addressComponents",
)
GOOGLE_PLACE_ID_REFRESH_FIELD_MASK = "id"

_GOOGLE_PLACE_ID_POLICY = "google-place-id-v1"
_GOOGLE_PROVIDER = "google-places"
_GOOGLE_ATTRIBUTION = "Google Maps"
_MAX_CANDIDATES = 5
_MAX_TYPES = 32
_MAX_ADDRESS_COMPONENTS = 32
_MAX_COMPONENT_TYPES = 16
_MAX_TEXT = 1024
_REVIEW_LIFETIME = timedelta(minutes=30)
_TYPE_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_LANGUAGE_RE = re.compile(
    r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$"
)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class PlaceIdentityReviewStatus(str, Enum):
    """Whether a bounded candidate set can be promoted."""

    READY = "ready"
    REVIEW_REQUIRED = "review_required"
    FAILED = "failed"


class PlaceCandidateRejection(str, Enum):
    """Hard match failures that a review grant cannot override."""

    COUNTRY_MISMATCH = "country_mismatch"
    LOCALITY_MISMATCH = "locality_mismatch"
    TYPE_MISMATCH = "type_mismatch"
    OUTSIDE_GEOGRAPHIC_BOUNDARY = "outside_geographic_boundary"


@dataclass(frozen=True, slots=True, repr=False)
class PlaceIdentityIntent:
    """Trusted host match scope for one stable local location."""

    location_id: str
    text_query: str = field(repr=False)
    expected_name: str = field(repr=False)
    region_code: str
    language_code: str
    expected_locality: str | None = field(default=None, repr=False)
    expected_primary_types: tuple[str, ...] = ()
    latitude: float | None = field(default=None, repr=False)
    longitude: float | None = field(default=None, repr=False)
    radius_m: float | None = field(default=None, repr=False)
    intent_id: str = ""

    def __post_init__(self) -> None:
        location_id = _text(
            self.location_id,
            "PlaceIdentityIntent.location_id",
            maximum=256,
        )
        text_query = _text(
            self.text_query,
            "PlaceIdentityIntent.text_query",
            maximum=512,
        )
        expected_name = _text(
            self.expected_name,
            "PlaceIdentityIntent.expected_name",
            maximum=512,
        )
        region_code = _region_code(
            self.region_code, "PlaceIdentityIntent.region_code"
        )
        language_code = _language_code(self.language_code)
        locality = (
            None
            if self.expected_locality is None
            else _text(
                self.expected_locality,
                "PlaceIdentityIntent.expected_locality",
                maximum=256,
            )
        )
        for match_value, match_name in (
            (text_query, "PlaceIdentityIntent.text_query"),
            (expected_name, "PlaceIdentityIntent.expected_name"),
            *(
                ((locality, "PlaceIdentityIntent.expected_locality"),)
                if locality is not None
                else ()
            ),
        ):
            if not _match_text(match_value):
                raise FactContractError(
                    "INVALID_PROVIDER_REQUEST",
                    f"{match_name} must contain a matchable letter or number.",
                )
        if not isinstance(self.expected_primary_types, tuple):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "expected_primary_types must be a tuple.",
            )
        types = tuple(
            sorted(
                {
                    _place_type(item, "expected_primary_types item")
                    for item in self.expected_primary_types
                }
            )
        )
        coordinates = (self.latitude, self.longitude, self.radius_m)
        present = tuple(value is not None for value in coordinates)
        if any(present) and not all(present):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "latitude, longitude, and radius_m must be supplied together.",
            )
        latitude: float | None = None
        longitude: float | None = None
        radius_m: float | None = None
        if all(present):
            latitude = _finite_number(self.latitude, "latitude")
            longitude = _finite_number(self.longitude, "longitude")
            radius_m = _finite_number(self.radius_m, "radius_m")
            if not -90 <= latitude <= 90:
                raise FactContractError(
                    "INVALID_PROVIDER_REQUEST",
                    "latitude is outside [-90, 90].",
                )
            if not -180 <= longitude <= 180:
                raise FactContractError(
                    "INVALID_PROVIDER_REQUEST",
                    "longitude is outside [-180, 180].",
                )
            if not 0 < radius_m <= 50_000:
                raise FactContractError(
                    "INVALID_PROVIDER_REQUEST",
                    "radius_m must be within (0, 50000].",
                )
            latitude = 0.0 if latitude == 0 else latitude
            longitude = 0.0 if longitude == 0 else longitude
        if locality is None and radius_m is None:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                (
                    "Place identity requires an expected locality or a hard "
                    "geographic circle."
                ),
            )
        payload = {
            "location_id": location_id,
            "text_query": text_query,
            "expected_name": expected_name,
            "region_code": region_code,
            "language_code": language_code,
            "expected_locality": locality,
            "expected_primary_types": list(types),
            "latitude": latitude,
            "longitude": longitude,
            "radius_m": radius_m,
        }
        expected_id = _digest(payload, prefix="place-identity-intent")
        _check_supplied_digest(
            self.intent_id,
            expected_id,
            "PlaceIdentityIntent.intent_id",
        )
        object.__setattr__(self, "location_id", location_id)
        object.__setattr__(self, "text_query", text_query)
        object.__setattr__(self, "expected_name", expected_name)
        object.__setattr__(self, "region_code", region_code)
        object.__setattr__(self, "language_code", language_code)
        object.__setattr__(self, "expected_locality", locality)
        object.__setattr__(self, "expected_primary_types", types)
        object.__setattr__(self, "latitude", latitude)
        object.__setattr__(self, "longitude", longitude)
        object.__setattr__(self, "radius_m", radius_m)
        object.__setattr__(self, "intent_id", expected_id)

    def __repr__(self) -> str:
        return (
            "PlaceIdentityIntent("
            f"location_id={self.location_id!r}, intent_id={self.intent_id!r})"
        )

    def to_binding_dict(self) -> dict[str, Any]:
        """Return a durable-safe binding without query or match values."""

        return {
            "location_id": self.location_id,
            "match_field_names": [
                "expected_name",
                "region_code",
                "language_code",
                *(
                    ["expected_locality"]
                    if self.expected_locality is not None
                    else []
                ),
                *(
                    ["expected_primary_types"]
                    if self.expected_primary_types
                    else []
                ),
                *(
                    ["latitude", "longitude", "radius_m"]
                    if self.radius_m is not None
                    else []
                ),
            ],
            "intent_id": self.intent_id,
        }


@dataclass(frozen=True, slots=True, repr=False)
class PlaceIdentityRequest:
    """Exact provider request plus its run-scoped match intent."""

    intent: PlaceIdentityIntent = field(repr=False)
    snapshot: EvidenceSnapshot = field(repr=False)
    provider_request: ProviderRequest
    policy_registry_revision: str
    field_mask: tuple[str, ...]
    request_id: str = ""

    def __post_init__(self) -> None:
        if type(self.intent) is not PlaceIdentityIntent:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "PlaceIdentityRequest requires an exact intent.",
            )
        if type(self.snapshot) is not EvidenceSnapshot:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "PlaceIdentityRequest requires an exact evidence snapshot.",
            )
        if type(self.provider_request) is not ProviderRequest:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "PlaceIdentityRequest requires an exact ProviderRequest.",
            )
        _require_digest(
            self.policy_registry_revision,
            "policy_registry_revision",
        )
        if (
            self.policy_registry_revision
            != self.snapshot.policies.revision
        ):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Place identity request policy revision differs from its snapshot.",
            )
        if self.field_mask != GOOGLE_PLACE_IDENTITY_FIELD_MASK:
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Place identity search must use the exact minimal field mask.",
            )
        request = self.provider_request
        if (
            request.provider_id != _GOOGLE_PROVIDER
            or request.adapter_id != _GOOGLE_PROVIDER
            or request.adapter_version != "v1"
            or request.operation != "resolve-place"
            or request.policy_id != _GOOGLE_PLACE_ID_POLICY
            or len(request.fact_keys) != 1
        ):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Place identity request source or operation is unsupported.",
            )
        key = request.fact_keys[0]
        if (
            key.kind is not FactKind.PLACE_IDENTITY
            or key.subject_ids != (self.intent.location_id,)
            or key.qualifier_map.get("identity_provider")
            != _GOOGLE_PROVIDER
        ):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Place identity request differs from its stable location.",
            )
        policy = self.snapshot.policies.policy(request.policy_id)
        if (
            request.policy_digest != policy.policy_digest
            or request.provider_id != policy.provider_id
            or request.adapter_id != policy.adapter_id
            or request.adapter_version != policy.adapter_version
        ):
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                "Place identity request differs from its snapshot policy.",
            )
        if request.query_scope != _place_identity_query_scope(
            self.intent,
            self.snapshot,
        ):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Place identity request differs from its exact match intent.",
            )
        expected_id = _digest(
            {
                "intent_id": self.intent.intent_id,
                "request_fingerprint": request.request_fingerprint,
                "policy_registry_revision": self.policy_registry_revision,
                "field_mask": list(self.field_mask),
            },
            prefix="place-identity-request",
        )
        _check_supplied_digest(
            self.request_id,
            expected_id,
            "PlaceIdentityRequest.request_id",
        )
        object.__setattr__(self, "request_id", expected_id)

    def __repr__(self) -> str:
        return (
            "PlaceIdentityRequest("
            f"request_id={self.request_id!r}, "
            f"provider_request={self.provider_request!r})"
        )

    def to_binding_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.to_binding_dict(),
            "basis_snapshot_id": self.snapshot.snapshot_id,
            "basis_store_revision": self.snapshot.store_revision,
            "basis_evidence_revision": self.snapshot.evidence_revision,
            "provider_request": self.provider_request.to_binding_dict(),
            "policy_registry_revision": self.policy_registry_revision,
            "field_mask": list(self.field_mask),
            "request_id": self.request_id,
        }


@dataclass(frozen=True, slots=True, repr=False)
class PlaceIdentityCandidate:
    """One normalized, run-scoped Places candidate."""

    provider_place_id: str = field(repr=False)
    display_name: str = field(repr=False)
    formatted_address: str = field(repr=False)
    latitude: float = field(repr=False)
    longitude: float = field(repr=False)
    primary_type: str | None = field(default=None, repr=False)
    types: tuple[str, ...] = field(default=(), repr=False)
    country_code: str = field(default="", repr=False)
    locality_names: tuple[str, ...] = field(default=(), repr=False)
    candidate_id: str = ""

    def __post_init__(self) -> None:
        provider_place_id = _text(
            self.provider_place_id,
            "provider_place_id",
            maximum=512,
        )
        # Reuse the durable value normalizer so an ID accepted here cannot fail
        # later merely because its identifier text is malformed.
        FactValue.from_payload(
            FactKind.PLACE_IDENTITY,
            {"provider_place_id": provider_place_id},
        )
        display_name = _text(
            self.display_name, "display_name", maximum=512
        )
        if not _match_text(display_name):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "display_name must contain a matchable letter or number.",
            )
        address = _text(
            self.formatted_address,
            "formatted_address",
            maximum=_MAX_TEXT,
        )
        latitude = _finite_number(self.latitude, "candidate latitude")
        longitude = _finite_number(self.longitude, "candidate longitude")
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "Candidate coordinates are outside their valid ranges.",
            )
        primary_type = (
            None
            if self.primary_type is None
            else _place_type(self.primary_type, "primary_type")
        )
        if not isinstance(self.types, tuple):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "Candidate types must be a tuple.",
            )
        types = tuple(
            sorted({_place_type(item, "candidate type") for item in self.types})
        )
        if len(types) > _MAX_TYPES:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "Candidate types exceed their bounded contract.",
            )
        if primary_type is not None and primary_type not in types:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "Candidate primary_type must appear in its types.",
            )
        country_code = _region_code(
            self.country_code, "candidate country_code"
        )
        if not isinstance(self.locality_names, tuple):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "Candidate locality names must be a tuple.",
            )
        localities = tuple(
            sorted(
                {
                    _text(item, "candidate locality", maximum=256)
                    for item in self.locality_names
                },
                key=lambda item: (_match_text(item), item),
            )
        )
        if any(not _match_text(item) for item in localities):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "Candidate locality names must be matchable text.",
            )
        expected_id = _digest(
            {
                "provider_place_id": provider_place_id,
                "display_name": display_name,
                "formatted_address": address,
                "latitude": 0.0 if latitude == 0 else latitude,
                "longitude": 0.0 if longitude == 0 else longitude,
                "primary_type": primary_type,
                "types": list(types),
                "country_code": country_code,
                "locality_names": list(localities),
            },
            prefix="place-identity-candidate",
        )
        _check_supplied_digest(
            self.candidate_id,
            expected_id,
            "PlaceIdentityCandidate.candidate_id",
        )
        object.__setattr__(self, "provider_place_id", provider_place_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "formatted_address", address)
        object.__setattr__(self, "latitude", 0.0 if latitude == 0 else latitude)
        object.__setattr__(
            self, "longitude", 0.0 if longitude == 0 else longitude
        )
        object.__setattr__(self, "primary_type", primary_type)
        object.__setattr__(self, "types", types)
        object.__setattr__(self, "country_code", country_code)
        object.__setattr__(self, "locality_names", localities)
        object.__setattr__(self, "candidate_id", expected_id)

    def __repr__(self) -> str:
        return f"PlaceIdentityCandidate(candidate_id={self.candidate_id!r})"

    def to_binding_dict(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id}

    def to_review_payload(self) -> dict[str, Any]:
        """Return explicit ephemeral provider content for candidate review."""

        return {
            "candidate_id": self.candidate_id,
            "display_name": self.display_name,
            "formatted_address": self.formatted_address,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "primary_type": self.primary_type,
            "types": list(self.types),
            "country_code": self.country_code,
            "locality_names": list(self.locality_names),
        }


_PLACE_IDENTITY_EVALUATOR_TOKEN = object()


@dataclass(frozen=True, slots=True, repr=False)
class PlaceIdentityCandidateAssessment:
    """Hard gates and name-match status for one candidate."""

    candidate: PlaceIdentityCandidate = field(repr=False)
    exact_name_match: bool
    rejection_codes: tuple[PlaceCandidateRejection, ...] = ()
    distance_m: float | None = field(default=None, repr=False)
    assessment_id: str = ""
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _PLACE_IDENTITY_EVALUATOR_TOKEN:
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                "Candidate assessments must be minted by the trusted evaluator.",
            )
        if type(self.candidate) is not PlaceIdentityCandidate:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "Candidate assessment requires an exact candidate.",
            )
        if not isinstance(self.exact_name_match, bool):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "exact_name_match must be bool.",
            )
        if not isinstance(self.rejection_codes, tuple) or any(
            not isinstance(item, PlaceCandidateRejection)
            for item in self.rejection_codes
        ):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "rejection_codes must contain exact rejection values.",
            )
        rejections = tuple(
            sorted(set(self.rejection_codes), key=lambda item: item.value)
        )
        distance = self.distance_m
        if distance is not None:
            distance = _finite_number(distance, "distance_m")
            if distance < 0:
                raise FactContractError(
                    "INVALID_PROVIDER_RESPONSE",
                    "distance_m cannot be negative.",
                )
        expected_id = _digest(
            {
                "candidate_id": self.candidate.candidate_id,
                "exact_name_match": self.exact_name_match,
                "rejection_codes": [item.value for item in rejections],
                "distance_m": distance,
            },
            prefix="place-candidate-assessment",
        )
        _check_supplied_digest(
            self.assessment_id,
            expected_id,
            "PlaceIdentityCandidateAssessment.assessment_id",
        )
        object.__setattr__(self, "rejection_codes", rejections)
        object.__setattr__(self, "distance_m", distance)
        object.__setattr__(self, "assessment_id", expected_id)

    @property
    def eligible(self) -> bool:
        return not self.rejection_codes

    def __repr__(self) -> str:
        return (
            "PlaceIdentityCandidateAssessment("
            f"assessment_id={self.assessment_id!r}, "
            f"eligible={self.eligible!r})"
        )

    def to_binding_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate.candidate_id,
            "assessment_id": self.assessment_id,
            "eligible": self.eligible,
            "exact_name_match": self.exact_name_match,
            "rejection_codes": [
                item.value for item in self.rejection_codes
            ],
        }

    def to_review_payload(self) -> dict[str, Any]:
        return {
            **self.candidate.to_review_payload(),
            "eligible": self.eligible,
            "exact_name_match": self.exact_name_match,
            "rejection_codes": [
                item.value for item in self.rejection_codes
            ],
            "distance_m": self.distance_m,
        }


@dataclass(frozen=True, slots=True, repr=False)
class PlaceIdentityReview:
    """One immutable, expiring review over an exact candidate set."""

    request: PlaceIdentityRequest = field(repr=False)
    assessments: tuple[PlaceIdentityCandidateAssessment, ...] = field(
        repr=False
    )
    status: PlaceIdentityReviewStatus
    recommended_candidate_id: str | None
    completed_at: datetime
    expires_at: datetime
    attempts_used: int
    results_truncated: bool = False
    candidate_set_digest: str = ""
    review_id: str = ""
    contract_version: str = PLACE_IDENTITY_REVIEW_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _PLACE_IDENTITY_EVALUATOR_TOKEN:
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                "Place identity reviews must be minted by the trusted evaluator.",
            )
        if self.contract_version != PLACE_IDENTITY_REVIEW_VERSION:
            raise FactContractError(
                "UNSUPPORTED_VERSION",
                "Unsupported place identity review version.",
            )
        if type(self.request) is not PlaceIdentityRequest:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "PlaceIdentityReview requires an exact request.",
            )
        if not isinstance(self.assessments, tuple) or any(
            type(item) is not PlaceIdentityCandidateAssessment
            for item in self.assessments
        ):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "Review assessments must contain exact values.",
            )
        if len(self.assessments) > _MAX_CANDIDATES:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "Review candidate count exceeds its bound.",
            )
        if not isinstance(self.results_truncated, bool):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "results_truncated must be bool.",
            )
        assessments = tuple(
            sorted(
                self.assessments,
                key=lambda item: item.candidate.candidate_id,
            )
        )
        candidate_ids = [
            item.candidate.candidate_id for item in assessments
        ]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "Review cannot contain duplicate candidates.",
            )
        expected_status, expected_recommended = _review_outcome(
            self.request,
            assessments,
            results_truncated=self.results_truncated,
        )
        if (
            self.status is not expected_status
            or self.recommended_candidate_id != expected_recommended
        ):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "Review status does not match its candidate assessments.",
            )
        completed_at = _utc_datetime(
            self.completed_at, "PlaceIdentityReview.completed_at"
        )
        expires_at = _utc_datetime(
            self.expires_at, "PlaceIdentityReview.expires_at"
        )
        if expires_at != completed_at + _REVIEW_LIFETIME:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "Place identity review must expire after exactly 30 minutes.",
            )
        attempts_used = _attempts(self.attempts_used)
        candidate_set_digest = _digest(
            {
                "candidate_bindings": [
                    item.to_binding_dict() for item in assessments
                ],
                "results_truncated": self.results_truncated,
            },
            prefix="place-candidate-set",
        )
        _check_supplied_digest(
            self.candidate_set_digest,
            candidate_set_digest,
            "PlaceIdentityReview.candidate_set_digest",
        )
        review_id = _digest(
            {
                "contract_version": self.contract_version,
                "request_id": self.request.request_id,
                "policy_registry_revision": (
                    self.request.policy_registry_revision
                ),
                "candidate_set_digest": candidate_set_digest,
                "status": self.status.value,
                "recommended_candidate_id": self.recommended_candidate_id,
                "completed_at": _utc_iso(completed_at),
                "expires_at": _utc_iso(expires_at),
                "attempts_used": attempts_used,
                "results_truncated": self.results_truncated,
            },
            prefix="place-identity-review",
        )
        _check_supplied_digest(
            self.review_id,
            review_id,
            "PlaceIdentityReview.review_id",
        )
        object.__setattr__(self, "assessments", assessments)
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "attempts_used", attempts_used)
        object.__setattr__(
            self, "candidate_set_digest", candidate_set_digest
        )
        object.__setattr__(self, "review_id", review_id)

    def __repr__(self) -> str:
        return (
            "PlaceIdentityReview("
            f"review_id={self.review_id!r}, status={self.status.value!r}, "
            f"candidate_set_digest={self.candidate_set_digest!r})"
        )

    @property
    def eligible_assessments(
        self,
    ) -> tuple[PlaceIdentityCandidateAssessment, ...]:
        return tuple(item for item in self.assessments if item.eligible)

    def to_binding_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "request": self.request.to_binding_dict(),
            "status": self.status.value,
            "recommended_candidate_id": self.recommended_candidate_id,
            "candidate_set_digest": self.candidate_set_digest,
            "candidate_bindings": [
                item.to_binding_dict() for item in self.assessments
            ],
            "completed_at": _utc_iso(self.completed_at),
            "expires_at": _utc_iso(self.expires_at),
            "attempts_used": self.attempts_used,
            "results_truncated": self.results_truncated,
            "review_id": self.review_id,
        }

    def to_review_payload(self) -> dict[str, Any]:
        """Return explicit ephemeral content for a human candidate picker."""

        return {
            "review_id": self.review_id,
            "status": self.status.value,
            "candidate_set_digest": self.candidate_set_digest,
            "results_truncated": self.results_truncated,
            "attributions": [
                {"label": _GOOGLE_ATTRIBUTION, "uri": None}
            ],
            "expected": {
                "name": self.request.intent.expected_name,
                "region_code": self.request.intent.region_code,
                "locality": self.request.intent.expected_locality,
                "primary_types": list(
                    self.request.intent.expected_primary_types
                ),
            },
            "candidates": [
                item.to_review_payload() for item in self.assessments
            ],
            "expires_at": _utc_iso(self.expires_at),
        }


_REVIEW_GRANT_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False, repr=False)
class PlaceIdentityReviewGrant:
    """Out-of-band host approval over one exact live candidate set."""

    review_id: str
    candidate_set_digest: str
    candidate_id: str
    reviewer_id: str
    approved_at: datetime
    grant_id: str = ""

    def __init__(
        self,
        *,
        review_id: str,
        candidate_set_digest: str,
        candidate_id: str,
        reviewer_id: str,
        approved_at: datetime,
        grant_id: str = "",
        _token: object | None = None,
    ) -> None:
        if _token is not _REVIEW_GRANT_TOKEN:
            raise FactContractError(
                "PENDING_REVIEW",
                "Place identity grants must be issued by the trusted host.",
            )
        object.__setattr__(self, "review_id", review_id)
        object.__setattr__(
            self,
            "candidate_set_digest",
            candidate_set_digest,
        )
        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(self, "reviewer_id", reviewer_id)
        object.__setattr__(self, "approved_at", approved_at)
        object.__setattr__(self, "grant_id", grant_id)
        self.__post_init__()

    def __post_init__(self) -> None:
        for value, name in (
            (self.review_id, "review_id"),
            (self.candidate_set_digest, "candidate_set_digest"),
            (self.candidate_id, "candidate_id"),
        ):
            _require_digest(value, name)
        reviewer = _text(
            self.reviewer_id, "reviewer_id", maximum=256
        )
        approved_at = _utc_datetime(self.approved_at, "approved_at")
        grant_id = _digest(
            {
                "review_id": self.review_id,
                "candidate_set_digest": self.candidate_set_digest,
                "candidate_id": self.candidate_id,
                "reviewer_id": reviewer,
                "approved_at": _utc_iso(approved_at),
            },
            prefix="place-identity-review-grant",
        )
        _check_supplied_digest(
            self.grant_id,
            grant_id,
            "PlaceIdentityReviewGrant.grant_id",
        )
        object.__setattr__(self, "reviewer_id", reviewer)
        object.__setattr__(self, "approved_at", approved_at)
        object.__setattr__(self, "grant_id", grant_id)

    def __repr__(self) -> str:
        return (
            "PlaceIdentityReviewGrant("
            f"review_id={self.review_id!r}, grant_id={self.grant_id!r})"
        )

    def to_binding_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "candidate_set_digest": self.candidate_set_digest,
            "candidate_id": self.candidate_id,
            "reviewer_id": self.reviewer_id,
            "approved_at": _utc_iso(self.approved_at),
            "grant_id": self.grant_id,
        }


@dataclass(frozen=True, slots=True, repr=False)
class PlaceIdentityReviewAuthority:
    """Host-owned reviewer identity and trusted promotion clock."""

    reviewer_id: str
    clock: Callable[[], datetime] = field(repr=False)

    def __post_init__(self) -> None:
        reviewer = _text(
            self.reviewer_id,
            "reviewer_id",
            maximum=256,
        )
        if not callable(self.clock):
            raise FactContractError(
                "INVALID_PROVIDER_REQUEST",
                "Place identity review authority requires a trusted clock.",
            )
        object.__setattr__(self, "reviewer_id", reviewer)

    def _now(self) -> datetime:
        try:
            value = self.clock()
        except Exception:
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "Trusted place identity review clock failed.",
            ) from None
        return _utc_datetime(value, "trusted review clock")

    def issue_grant(
        self,
        review: PlaceIdentityReview,
        candidate_id: str,
    ) -> PlaceIdentityReviewGrant:
        if type(review) is not PlaceIdentityReview:
            raise FactContractError(
                "PENDING_REVIEW",
                "A trusted grant requires an exact place identity review.",
            )
        approved_at = self._now()
        if not review.completed_at <= approved_at <= review.expires_at:
            raise FactContractError(
                "PENDING_REVIEW",
                "Place identity review is outside its approval window.",
            )
        selected = next(
            (
                item
                for item in review.assessments
                if item.candidate.candidate_id == candidate_id
            ),
            None,
        )
        if selected is None or not selected.eligible:
            raise FactContractError(
                "OUT_OF_SCOPE_RESULT",
                "Trusted review cannot approve a hard-rejected candidate.",
            )
        return PlaceIdentityReviewGrant(
            review_id=review.review_id,
            candidate_set_digest=review.candidate_set_digest,
            candidate_id=candidate_id,
            reviewer_id=self.reviewer_id,
            approved_at=approved_at,
            _token=_REVIEW_GRANT_TOKEN,
        )


_PLACE_ENDPOINT_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False, repr=False)
class PlaceEndpointIdentity:
    """Fresh provider endpoint binding for the later Routes slice."""

    location_id: str
    provider_id: str
    provider_place_id: str
    observation_id: str
    value_digest: str
    valid_until: datetime
    snapshot_id: str
    endpoint_id: str = ""

    def __init__(
        self,
        *,
        location_id: str,
        provider_id: str,
        provider_place_id: str,
        observation_id: str,
        value_digest: str,
        valid_until: datetime,
        snapshot_id: str,
        endpoint_id: str = "",
        _token: object | None = None,
    ) -> None:
        if _token is not _PLACE_ENDPOINT_TOKEN:
            raise FactContractError(
                "UNTRUSTED_PROVENANCE",
                (
                    "Place endpoints can only be extracted from a fresh "
                    "trusted evidence snapshot."
                ),
            )
        object.__setattr__(self, "location_id", location_id)
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(
            self,
            "provider_place_id",
            provider_place_id,
        )
        object.__setattr__(self, "observation_id", observation_id)
        object.__setattr__(self, "value_digest", value_digest)
        object.__setattr__(self, "valid_until", valid_until)
        object.__setattr__(self, "snapshot_id", snapshot_id)
        object.__setattr__(self, "endpoint_id", endpoint_id)
        self.__post_init__()

    def __post_init__(self) -> None:
        location_id = _text(self.location_id, "location_id", maximum=256)
        if self.provider_id != _GOOGLE_PROVIDER:
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Endpoint identity provider must be google-places.",
            )
        place_id = _text(
            self.provider_place_id,
            "provider_place_id",
            maximum=512,
        )
        value = FactValue.from_payload(
            FactKind.PLACE_IDENTITY,
            {"provider_place_id": place_id},
        )
        _require_digest(self.observation_id, "observation_id")
        _require_digest(self.value_digest, "value_digest")
        if self.value_digest != value.value_digest:
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Endpoint value digest differs from its provider place ID.",
            )
        _require_digest(self.snapshot_id, "snapshot_id")
        valid_until = _utc_datetime(self.valid_until, "valid_until")
        endpoint_id = _digest(
            {
                "location_id": location_id,
                "provider_id": self.provider_id,
                "provider_place_id": place_id,
                "observation_id": self.observation_id,
                "value_digest": self.value_digest,
                "valid_until": _utc_iso(valid_until),
                "snapshot_id": self.snapshot_id,
            },
            prefix="place-endpoint-identity",
        )
        _check_supplied_digest(
            self.endpoint_id,
            endpoint_id,
            "PlaceEndpointIdentity.endpoint_id",
        )
        object.__setattr__(self, "location_id", location_id)
        object.__setattr__(self, "provider_place_id", place_id)
        object.__setattr__(self, "valid_until", valid_until)
        object.__setattr__(self, "endpoint_id", endpoint_id)

    def __repr__(self) -> str:
        return (
            "PlaceEndpointIdentity("
            f"location_id={self.location_id!r}, "
            f"endpoint_id={self.endpoint_id!r})"
        )

    def to_binding_dict(self) -> dict[str, Any]:
        return {
            "location_id": self.location_id,
            "provider_id": self.provider_id,
            "observation_id": self.observation_id,
            "value_digest": self.value_digest,
            "valid_until": _utc_iso(self.valid_until),
            "snapshot_id": self.snapshot_id,
            "endpoint_id": self.endpoint_id,
        }


def build_google_place_identity_request(
    intent: PlaceIdentityIntent,
    snapshot: EvidenceSnapshot,
) -> PlaceIdentityRequest:
    """Build one exact Text Search identity request without performing I/O."""

    if type(intent) is not PlaceIdentityIntent:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "intent must be an exact PlaceIdentityIntent.",
        )
    if type(snapshot) is not EvidenceSnapshot:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "snapshot must be an exact EvidenceSnapshot.",
        )
    policies = snapshot.policies
    policy = policies.policy(_GOOGLE_PLACE_ID_POLICY)
    if (
        policy.provider_id != _GOOGLE_PROVIDER
        or policy.adapter_id != _GOOGLE_PROVIDER
        or policy.adapter_version != "v1"
        or FactKind.PLACE_IDENTITY not in policy.allowed_fact_kinds
    ):
        raise FactContractError(
            "UNTRUSTED_PROVENANCE",
            "Host policy does not authorize Google place identity.",
        )
    key = _identity_key(intent.location_id)
    request = ProviderRequest(
        provider_id=_GOOGLE_PROVIDER,
        adapter_id=_GOOGLE_PROVIDER,
        adapter_version="v1",
        operation="resolve-place",
        fact_keys=(key,),
        policy_id=policy.policy_id,
        policy_digest=policy.policy_digest,
        query_scope=_place_identity_query_scope(intent, snapshot),
    )
    # Fail before transport if this package and the host policy drift.
    policies.policy(request.policy_id)
    if not {name for name, _value in request.query_scope}.issubset(
        policy.allowed_query_fields
    ):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "Place identity request exceeds its static query policy.",
        )
    return PlaceIdentityRequest(
        intent=intent,
        snapshot=snapshot,
        provider_request=request,
        policy_registry_revision=policies.revision,
        field_mask=GOOGLE_PLACE_IDENTITY_FIELD_MASK,
    )


def evaluate_google_place_identity_candidates(
    request: PlaceIdentityRequest,
    raw_response: Mapping[str, Any],
    *,
    completed_at: datetime,
    attempts_used: int = 1,
) -> PlaceIdentityReview:
    """Normalize and evaluate one bounded Text Search response."""

    if type(request) is not PlaceIdentityRequest:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "request must be an exact PlaceIdentityRequest.",
        )
    completed = _utc_datetime(completed_at, "completed_at")
    attempts = _attempts(attempts_used)
    candidates, results_truncated = _decode_candidates(raw_response)
    assessments = tuple(
        _assess_candidate(request.intent, candidate)
        for candidate in candidates
    )
    status, recommended = _review_outcome(
        request,
        assessments,
        results_truncated=results_truncated,
    )
    return PlaceIdentityReview(
        request=request,
        assessments=assessments,
        status=status,
        recommended_candidate_id=recommended,
        completed_at=completed,
        expires_at=completed + _REVIEW_LIFETIME,
        attempts_used=attempts,
        results_truncated=results_truncated,
        _token=_PLACE_IDENTITY_EVALUATOR_TOKEN,
    )


def finalize_google_place_identity_review(
    review: PlaceIdentityReview,
    current_snapshot: EvidenceSnapshot,
    authority: PlaceIdentityReviewAuthority,
    grant: PlaceIdentityReviewGrant | None = None,
) -> AuthorizedProviderResult:
    """Authorize an ID-only result after auto-safe or reviewed selection."""

    if (
        type(review) is not PlaceIdentityReview
        or type(current_snapshot) is not EvidenceSnapshot
        or type(authority) is not PlaceIdentityReviewAuthority
    ):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "promotion requires an exact review, snapshot, and host authority.",
        )
    promotion = authority._now()
    if not review.completed_at <= promotion <= review.expires_at:
        raise FactContractError(
            "PENDING_REVIEW",
            "Place identity review is outside its live promotion window.",
        )
    if promotion < current_snapshot.purge_checked_at:
        raise FactContractError(
            "PENDING_REVIEW",
            "Trusted promotion clock precedes the current evidence check.",
        )
    reviewed_snapshot = review.request.snapshot
    if (
        current_snapshot.policies.revision
        != reviewed_snapshot.policies.revision
        or current_snapshot.store_revision
        != reviewed_snapshot.store_revision
        or current_snapshot.evidence_revision
        != reviewed_snapshot.evidence_revision
        or current_snapshot.purge_checked_at
        < reviewed_snapshot.purge_checked_at
    ):
        raise FactContractError(
            "EVIDENCE_REVISION_CHANGED",
            "Place identity evidence changed after candidate review.",
        )
    current_status, current_recommended = _review_outcome(
        review.request,
        review.assessments,
        results_truncated=review.results_truncated,
        snapshot=current_snapshot,
    )
    if (
        current_status is not review.status
        or current_recommended != review.recommended_candidate_id
    ):
        raise FactContractError(
            "EVIDENCE_REVISION_CHANGED",
            "Current identity basis changes the candidate review outcome.",
        )
    if review.status is PlaceIdentityReviewStatus.FAILED:
        raise FactContractError(
            "OUT_OF_SCOPE_RESULT",
            "No candidate passed the hard place identity gates.",
        )
    selected_id: str | None
    if grant is None:
        if review.status is not PlaceIdentityReviewStatus.READY:
            raise FactContractError(
                "PENDING_REVIEW",
                "Ambiguous place candidates require an exact review grant.",
            )
        selected_id = review.recommended_candidate_id
    else:
        if type(grant) is not PlaceIdentityReviewGrant:
            raise FactContractError(
                "PENDING_REVIEW",
                "grant must be an exact PlaceIdentityReviewGrant.",
            )
        if (
            grant.review_id != review.review_id
            or grant.candidate_set_digest
            != review.candidate_set_digest
        ):
            raise FactContractError(
                "EVIDENCE_BINDING_MISMATCH",
                "Place identity grant does not bind this live review.",
            )
        if not (
            review.completed_at
            <= grant.approved_at
            <= promotion
            <= review.expires_at
        ):
            raise FactContractError(
                "PENDING_REVIEW",
                "Place identity grant is outside the trusted review window.",
            )
        selected_id = grant.candidate_id
    selected = next(
        (
            item
            for item in review.assessments
            if item.candidate.candidate_id == selected_id
        ),
        None,
    )
    if selected is None or not selected.eligible:
        raise FactContractError(
            "OUT_OF_SCOPE_RESULT",
            "Review grant selected a missing or hard-rejected candidate.",
        )
    result = _identity_provider_result(
        request=review.request.provider_request,
        provider_place_id=selected.candidate.provider_place_id,
        completed_at=review.completed_at,
        attempts_used=review.attempts_used,
    )
    return _authorize_google_place_identity_result(
        review.request.provider_request,
        result,
        current_snapshot.policies,
        _token=_GOOGLE_PLACE_IDENTITY_AUTHORIZATION_TOKEN,
    )


def build_google_place_identity_refresh_request(
    snapshot: EvidenceSnapshot,
    location_id: str,
) -> ProviderRequest:
    """Build an ID-only refresh from an existing trusted identity LKG."""

    if type(snapshot) is not EvidenceSnapshot:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "snapshot must be an exact EvidenceSnapshot.",
        )
    location = _text(location_id, "location_id", maximum=256)
    basis = _google_identity_observation(
        snapshot,
        location,
        allow_stale=True,
    )
    place_id = basis.value.payload["provider_place_id"]
    policy = snapshot.policies.policy(_GOOGLE_PLACE_ID_POLICY)
    request = ProviderRequest(
        provider_id=_GOOGLE_PROVIDER,
        adapter_id=_GOOGLE_PROVIDER,
        adapter_version="v1",
        operation="refresh-place-id",
        fact_keys=(_identity_key(location),),
        policy_id=policy.policy_id,
        policy_digest=policy.policy_digest,
        query_scope=(
            ("basis_observation_id", basis.observation_id),
            ("basis_provider_place_id", place_id),
            ("basis_snapshot_id", snapshot.snapshot_id),
            ("basis_value_digest", basis.value.value_digest),
            ("field_mask", GOOGLE_PLACE_ID_REFRESH_FIELD_MASK),
            ("provider_place_id", place_id),
        ),
    )
    if not {name for name, _value in request.query_scope}.issubset(
        policy.allowed_query_fields
    ):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "Place ID refresh exceeds its static query policy.",
        )
    return request


def finalize_google_place_identity_refresh(
    snapshot: EvidenceSnapshot,
    request: ProviderRequest,
    raw_response: Mapping[str, Any],
    *,
    completed_at: datetime,
    attempts_used: int = 1,
) -> AuthorizedProviderResult:
    """Normalize and authorize one successful ID-only refresh response."""

    if (
        type(snapshot) is not EvidenceSnapshot
        or type(request) is not ProviderRequest
    ):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "refresh requires an exact snapshot and ProviderRequest.",
        )
    if (
        request.provider_id != _GOOGLE_PROVIDER
        or request.adapter_id != _GOOGLE_PROVIDER
        or request.adapter_version != "v1"
        or request.operation != "refresh-place-id"
        or request.policy_id != _GOOGLE_PLACE_ID_POLICY
        or len(request.fact_keys) != 1
    ):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "Place ID refresh request is not exact.",
        )
    key = request.fact_keys[0]
    if (
        key.kind is not FactKind.PLACE_IDENTITY
        or len(key.subject_ids) != 1
        or key.qualifier_map.get("identity_provider") != _GOOGLE_PROVIDER
    ):
        raise FactContractError(
            "EVIDENCE_BINDING_MISMATCH",
            "Place ID refresh request scope is not exact.",
        )
    expected_request = build_google_place_identity_refresh_request(
        snapshot,
        key.subject_ids[0],
    )
    if request != expected_request:
        raise FactContractError(
            "EVIDENCE_BINDING_MISMATCH",
            "Place ID refresh request does not bind its trusted identity LKG.",
        )
    basis = _google_identity_observation(
        snapshot,
        key.subject_ids[0],
        allow_stale=True,
    )
    expected = basis.value.payload["provider_place_id"]
    completed = _utc_datetime(completed_at, "completed_at")
    if completed < basis.retrieved_at:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Place ID refresh cannot predate its identity basis.",
        )
    response = _exact_mapping(
        raw_response,
        required={"id"},
        optional=set(),
        context="Place ID refresh response",
    )
    returned = _text(
        response["id"], "Place ID refresh response.id", maximum=512
    )
    FactValue.from_payload(
        FactKind.PLACE_IDENTITY,
        {"provider_place_id": returned},
    )
    if returned != expected:
        raise FactContractError(
            "PENDING_REVIEW",
            (
                "A changed place ID requires a new search candidate review; "
                "ID-only refresh cannot silently rebind a location."
            ),
        )
    result = _identity_provider_result(
        request=request,
        provider_place_id=returned,
        completed_at=completed,
        attempts_used=_attempts(attempts_used),
    )
    return _authorize_google_place_identity_result(
        request,
        result,
        snapshot.policies,
        _token=_GOOGLE_PLACE_IDENTITY_AUTHORIZATION_TOKEN,
    )


def extract_fresh_google_place_endpoint(
    snapshot: EvidenceSnapshot,
    location_id: str,
) -> PlaceEndpointIdentity:
    """Require one fresh, unconflicted place ID before Routes work begins."""

    if type(snapshot) is not EvidenceSnapshot:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "snapshot must be an exact EvidenceSnapshot.",
        )
    location = _text(location_id, "location_id", maximum=256)
    key = _identity_key(location)
    resolution = snapshot.resolve(key)
    if resolution.evidence_state is EvidenceState.UNVERIFIED:
        raise FactContractError(
            "PENDING_REVIEW",
            "A fresh Google place identity is required for this endpoint.",
        )
    if resolution.evidence_state is EvidenceState.STALE:
        raise FactContractError(
            "STALE_EVIDENCE",
            "The Google place identity is older than its refresh window.",
        )
    if resolution.evidence_state is EvidenceState.CONFLICTED:
        raise FactContractError(
            "CONFLICT_DETECTED",
            "Conflicting Google place identities cannot define an endpoint.",
        )
    selected = resolution.selected
    if (
        resolution.evidence_state is not EvidenceState.VERIFIED
        or selected is None
        or not resolution.supports_travel_ready_use
    ):
        raise FactContractError(
            "PENDING_REVIEW",
            "The place identity is not ready for provider endpoint use.",
        )
    policy = snapshot.policies.policy(
        selected.provenance.retention_policy_id
    )
    if (
        policy.persistence is not EvidencePersistence.INDEFINITE_ID
        or selected.provenance.provider_id != _GOOGLE_PROVIDER
    ):
        raise FactContractError(
            "UNTRUSTED_PROVENANCE",
            "Endpoint identity is outside the Google place ID policy.",
        )
    return PlaceEndpointIdentity(
        location_id=location,
        provider_id=_GOOGLE_PROVIDER,
        provider_place_id=selected.value.payload["provider_place_id"],
        observation_id=selected.observation_id,
        value_digest=selected.value.value_digest,
        valid_until=selected.valid_until,
        snapshot_id=snapshot.snapshot_id,
        _token=_PLACE_ENDPOINT_TOKEN,
    )


def _identity_provider_result(
    *,
    request: ProviderRequest,
    provider_place_id: str,
    completed_at: datetime,
    attempts_used: int,
) -> ProviderResult:
    key = request.fact_keys[0]
    value = FactValue.from_payload(
        FactKind.PLACE_IDENTITY,
        {"provider_place_id": provider_place_id},
    )
    observation = FactObservation(
        key=key,
        value=value,
        provenance=ProviderProvenance(
            provider_id=request.provider_id,
            adapter_id=request.adapter_id,
            adapter_version=request.adapter_version,
            request_fingerprint=request.request_fingerprint,
            retention_policy_id=request.policy_id,
            provider_record_id=provider_place_id,
            response_id=None,
            source_uri=None,
            attributions=((_GOOGLE_ATTRIBUTION, None),),
        ),
        retrieved_at=completed_at,
        valid_until=_add_calendar_year(completed_at),
        purge_at=None,
        confidence=1.0,
    )
    return ProviderResult(
        request_fingerprint=request.request_fingerprint,
        status=ProviderResultStatus.SUCCESS,
        observations=(observation,),
        problems=(),
        attempts_used=attempts_used,
        completed_at=completed_at,
    )


def _identity_key(location_id: str) -> FactKey:
    return FactKey(
        kind=FactKind.PLACE_IDENTITY,
        subject_ids=(location_id,),
        qualifiers=(("identity_provider", _GOOGLE_PROVIDER),),
    )


def _google_identity_observation(
    snapshot: EvidenceSnapshot,
    location_id: str,
    *,
    allow_stale: bool,
    missing_ok: bool = False,
) -> FactObservation | None:
    resolution = snapshot.resolve(_identity_key(location_id))
    if resolution.evidence_state is EvidenceState.UNVERIFIED:
        if missing_ok:
            return None
        raise FactContractError(
            "PENDING_REVIEW",
            "A reviewed Google place identity is required.",
        )
    if resolution.evidence_state is EvidenceState.CONFLICTED:
        raise FactContractError(
            "CONFLICT_DETECTED",
            "Conflicting Google place identities require review.",
        )
    if (
        resolution.evidence_state is EvidenceState.STALE
        and not allow_stale
    ):
        raise FactContractError(
            "STALE_EVIDENCE",
            "The Google place identity is older than its refresh window.",
        )
    selected = resolution.selected
    if selected is None:
        raise FactContractError(
            "PENDING_REVIEW",
            "The Google place identity has no selected observation.",
        )
    policy = snapshot.policies.policy(
        selected.provenance.retention_policy_id
    )
    if (
        policy.policy_id != _GOOGLE_PLACE_ID_POLICY
        or policy.persistence is not EvidencePersistence.INDEFINITE_ID
        or selected.key.kind is not FactKind.PLACE_IDENTITY
        or selected.provenance.provider_id != _GOOGLE_PROVIDER
        or selected.key.qualifier_map.get("identity_provider")
        != _GOOGLE_PROVIDER
    ):
        raise FactContractError(
            "UNTRUSTED_PROVENANCE",
            "Place identity basis is outside the Google ID policy.",
        )
    return selected


def _place_identity_query_scope(
    intent: PlaceIdentityIntent,
    snapshot: EvidenceSnapshot,
) -> tuple[tuple[str, object], ...]:
    scope: list[tuple[str, object]] = [
        ("basis_snapshot_id", snapshot.snapshot_id),
        ("expected_name", intent.expected_name),
        (
            "field_mask",
            ",".join(GOOGLE_PLACE_IDENTITY_FIELD_MASK),
        ),
        ("language_code", intent.language_code),
        ("page_size", _MAX_CANDIDATES),
        ("region_code", intent.region_code),
        ("text_query", intent.text_query),
    ]
    basis = _google_identity_observation(
        snapshot,
        intent.location_id,
        allow_stale=True,
        missing_ok=True,
    )
    if basis is not None:
        scope.extend(
            (
                ("basis_observation_id", basis.observation_id),
                (
                    "basis_provider_place_id",
                    basis.value.payload["provider_place_id"],
                ),
                ("basis_value_digest", basis.value.value_digest),
            )
        )
    if intent.expected_locality is not None:
        scope.append(("expected_locality", intent.expected_locality))
    if intent.expected_primary_types:
        scope.append(
            (
                "expected_primary_types",
                ",".join(intent.expected_primary_types),
            )
        )
    if intent.radius_m is not None:
        scope.extend(
            (
                ("latitude", intent.latitude),
                ("longitude", intent.longitude),
                ("radius_m", intent.radius_m),
            )
        )
    return tuple(sorted(scope, key=lambda item: item[0]))


def _review_outcome(
    request: PlaceIdentityRequest,
    assessments: tuple[PlaceIdentityCandidateAssessment, ...],
    *,
    results_truncated: bool,
    snapshot: EvidenceSnapshot | None = None,
) -> tuple[PlaceIdentityReviewStatus, str | None]:
    eligible = tuple(item for item in assessments if item.eligible)
    basis = _google_identity_observation(
        request.snapshot if snapshot is None else snapshot,
        request.intent.location_id,
        allow_stale=True,
        missing_ok=True,
    )
    changes_existing_identity = bool(
        basis is not None
        and len(eligible) == 1
        and eligible[0].candidate.provider_place_id
        != basis.value.payload["provider_place_id"]
    )
    if (
        not results_truncated
        and not changes_existing_identity
        and len(eligible) == 1
        and eligible[0].exact_name_match
    ):
        return (
            PlaceIdentityReviewStatus.READY,
            eligible[0].candidate.candidate_id,
        )
    if eligible:
        return (PlaceIdentityReviewStatus.REVIEW_REQUIRED, None)
    return (PlaceIdentityReviewStatus.FAILED, None)


def _decode_candidates(
    raw_response: Mapping[str, Any],
) -> tuple[tuple[PlaceIdentityCandidate, ...], bool]:
    response = _exact_mapping(
        raw_response,
        required=set(),
        optional={"nextPageToken", "places"},
        context="Places Text Search response",
    )
    results_truncated = "nextPageToken" in response
    if results_truncated:
        _text(
            response["nextPageToken"],
            "Places Text Search response.nextPageToken",
            maximum=2048,
        )
    raw_places = response.get("places", [])
    if not isinstance(raw_places, list):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Places Text Search response.places must be an array.",
        )
    if len(raw_places) > _MAX_CANDIDATES:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"Places identity review accepts at most {_MAX_CANDIDATES} candidates.",
        )
    candidates = tuple(
        _decode_candidate(item, index)
        for index, item in enumerate(raw_places)
    )
    place_ids = [item.provider_place_id for item in candidates]
    if len(set(place_ids)) != len(place_ids):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Places response contains duplicate place IDs.",
        )
    return (
        tuple(sorted(candidates, key=lambda item: item.candidate_id)),
        results_truncated,
    )


def _decode_candidate(raw: Any, index: int) -> PlaceIdentityCandidate:
    value = _exact_mapping(
        raw,
        required={
            "id",
            "displayName",
            "formattedAddress",
            "location",
            "types",
            "addressComponents",
        },
        optional={"primaryType"},
        context=f"places[{index}]",
    )
    display = _exact_mapping(
        value["displayName"],
        required={"text"},
        optional={"languageCode"},
        context=f"places[{index}].displayName",
    )
    if "languageCode" in display:
        _response_language_code(
            display["languageCode"],
            f"places[{index}].displayName.languageCode",
        )
    location = _exact_mapping(
        value["location"],
        required={"latitude", "longitude"},
        optional=set(),
        context=f"places[{index}].location",
    )
    raw_types = value["types"]
    if not isinstance(raw_types, list) or any(
        not isinstance(item, str) for item in raw_types
    ):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"places[{index}].types must be an array of strings.",
        )
    if len(raw_types) > _MAX_TYPES:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"places[{index}].types exceeds its bound.",
        )
    raw_components = value["addressComponents"]
    if not isinstance(raw_components, list) or len(
        raw_components
    ) > _MAX_ADDRESS_COMPONENTS:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"places[{index}].addressComponents is invalid or oversized.",
        )
    country_codes: set[str] = set()
    locality_names: set[str] = set()
    locality_types = {
        "locality",
        "postal_town",
        "administrative_area_level_1",
        "administrative_area_level_2",
        "administrative_area_level_3",
    }
    for component_index, raw_component in enumerate(raw_components):
        component = _exact_mapping(
            raw_component,
            required={"longText", "shortText", "types"},
            optional={"languageCode"},
            context=(
                f"places[{index}].addressComponents[{component_index}]"
            ),
        )
        component_types = component["types"]
        if (
            not isinstance(component_types, list)
            or len(component_types) > _MAX_COMPONENT_TYPES
            or any(not isinstance(item, str) for item in component_types)
        ):
            raise FactContractError(
                "INVALID_PROVIDER_RESPONSE",
                "Address component types must be an array of strings.",
            )
        if "languageCode" in component:
            _response_language_code(
                component["languageCode"],
                (
                    f"places[{index}].addressComponents"
                    f"[{component_index}].languageCode"
                ),
            )
        normalized_types = {
            _place_type(item, "address component type")
            for item in component_types
        }
        long_text = _text(
            component["longText"],
            "address component longText",
            maximum=256,
        )
        short_text = _text(
            component["shortText"],
            "address component shortText",
            maximum=256,
        )
        if "country" in normalized_types:
            country_codes.add(
                _region_code(short_text, "address country code")
            )
        if normalized_types.intersection(locality_types):
            locality_names.update((long_text, short_text))
    if len(country_codes) != 1:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Candidate must contain exactly one country address component.",
        )
    return PlaceIdentityCandidate(
        provider_place_id=value["id"],
        display_name=display["text"],
        formatted_address=value["formattedAddress"],
        latitude=location["latitude"],
        longitude=location["longitude"],
        primary_type=value.get("primaryType"),
        types=tuple(raw_types),
        country_code=next(iter(country_codes)),
        locality_names=tuple(locality_names),
    )


def _assess_candidate(
    intent: PlaceIdentityIntent,
    candidate: PlaceIdentityCandidate,
) -> PlaceIdentityCandidateAssessment:
    rejections: list[PlaceCandidateRejection] = []
    if candidate.country_code != intent.region_code:
        rejections.append(PlaceCandidateRejection.COUNTRY_MISMATCH)
    if intent.expected_locality is not None:
        expected_locality = _match_text(intent.expected_locality)
        if not any(
            _match_text(item) == expected_locality
            for item in candidate.locality_names
        ):
            rejections.append(PlaceCandidateRejection.LOCALITY_MISMATCH)
    if (
        intent.expected_primary_types
        and candidate.primary_type not in intent.expected_primary_types
    ):
        rejections.append(PlaceCandidateRejection.TYPE_MISMATCH)
    distance_m: float | None = None
    if intent.radius_m is not None:
        distance_m = _haversine_m(
            intent.latitude,
            intent.longitude,
            candidate.latitude,
            candidate.longitude,
        )
        if distance_m > intent.radius_m:
            rejections.append(
                PlaceCandidateRejection.OUTSIDE_GEOGRAPHIC_BOUNDARY
            )
    return PlaceIdentityCandidateAssessment(
        candidate=candidate,
        exact_name_match=(
            _match_text(candidate.display_name)
            == _match_text(intent.expected_name)
        ),
        rejection_codes=tuple(rejections),
        distance_m=distance_m,
        _token=_PLACE_IDENTITY_EVALUATOR_TOKEN,
    )


def _exact_mapping(
    value: Any,
    *,
    required: set[str],
    optional: set[str],
    context: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{context} must be an object.",
        )
    try:
        copied = dict(value)
    except (TypeError, ValueError) as exc:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{context} is not a string-keyed object.",
        ) from exc
    if any(not isinstance(key, str) for key in copied):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{context} contains a non-text field name.",
        )
    names = set(copied)
    missing = required - names
    unknown = names - required - optional
    if missing or unknown:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            (
                f"{context} fields are invalid "
                f"(missing={sorted(missing)!r}, unknown={sorted(unknown)!r})."
            ),
        )
    return copied


def _text(value: Any, name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST"
            if name.startswith("PlaceIdentityIntent")
            or name in {"location_id", "current_place_id", "reviewer_id"}
            else "INVALID_PROVIDER_RESPONSE",
            f"{name} must be text.",
        )
    normalized = unicodedata.normalize("NFC", value).strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(not character.isprintable() for character in normalized)
    ):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST"
            if name.startswith("PlaceIdentityIntent")
            or name in {"location_id", "current_place_id", "reviewer_id"}
            else "INVALID_PROVIDER_RESPONSE",
            f"{name} is empty, oversized, or contains control characters.",
        )
    return normalized


def _match_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    tokens: list[str] = []
    current: list[str] = []
    for character in normalized:
        if character.isalnum():
            current.append(character)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return " ".join(tokens)


def _region_code(value: Any, name: str) -> str:
    normalized = _text(value, name, maximum=2).upper()
    if len(normalized) != 2 or not normalized.isascii() or not normalized.isalpha():
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST"
            if name.startswith("PlaceIdentityIntent")
            else "INVALID_PROVIDER_RESPONSE",
            f"{name} must be a two-letter region code.",
        )
    return normalized


def _language_code(value: Any) -> str:
    normalized = _text(
        value, "PlaceIdentityIntent.language_code", maximum=35
    )
    if _LANGUAGE_RE.fullmatch(normalized) is None:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST",
            "language_code must be a bounded BCP-47-style tag.",
        )
    pieces = normalized.split("-")
    return "-".join(
        [
            pieces[0].lower(),
            *(
                piece.upper() if len(piece) == 2 else piece
                for piece in pieces[1:]
            ),
        ]
    )


def _response_language_code(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 35
        or _LANGUAGE_RE.fullmatch(value) is None
    ):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{name} must be a bounded BCP-47-style tag.",
        )
    return value


def _place_type(value: Any, name: str) -> str:
    if not isinstance(value, str) or _TYPE_RE.fullmatch(value) is None:
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST"
            if name == "expected_primary_types item"
            else "INVALID_PROVIDER_RESPONSE",
            f"{name} must be a Google place type machine name.",
        )
    return value


def _finite_number(value: Any, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST"
            if name in {"latitude", "longitude", "radius_m"}
            else "INVALID_PROVIDER_RESPONSE",
            f"{name} must be a finite number.",
        )
    normalized = float(value)
    if not math.isfinite(normalized):
        raise FactContractError(
            "INVALID_PROVIDER_REQUEST"
            if name in {"latitude", "longitude", "radius_m"}
            else "INVALID_PROVIDER_RESPONSE",
            f"{name} must be a finite number.",
        )
    return normalized


def _attempts(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            "Places candidate results must consume at least one attempt.",
        )
    return value


def _utc_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{name} must be a timezone-aware datetime.",
        )
    normalized = value.astimezone(timezone.utc)
    if normalized.utcoffset() != timedelta(0):
        raise FactContractError(
            "INVALID_PROVIDER_RESPONSE",
            f"{name} could not be normalized to UTC.",
        )
    return normalized


def _utc_iso(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    text = normalized.isoformat(timespec="microseconds")
    if normalized.microsecond == 0:
        text = normalized.isoformat(timespec="seconds")
    return text.replace("+00:00", "Z")


def _add_calendar_year(value: datetime) -> datetime:
    try:
        return value.replace(year=value.year + 1)
    except ValueError:
        # February 29 refreshes on the last valid day of the next February.
        return value.replace(year=value.year + 1, month=2, day=28)


def _haversine_m(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    radius_m = 6_371_000.0
    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = lat_b - lat_a
    delta_lng = math.radians(longitude_b - longitude_a)
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a)
        * math.cos(lat_b)
        * math.sin(delta_lng / 2) ** 2
    )
    haversine = min(1.0, max(0.0, haversine))
    return radius_m * 2 * math.atan2(
        math.sqrt(haversine),
        math.sqrt(1 - haversine),
    )


def _digest(value: Any, *, prefix: str) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(
        f"trip-planner.{prefix}/v1\0".encode("ascii") + encoded
    ).hexdigest()


def _require_digest(value: Any, name: str) -> None:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise FactContractError(
            "DIGEST_MISMATCH",
            f"{name} must be a lowercase SHA-256 digest.",
        )


def _check_supplied_digest(
    supplied: str,
    expected: str,
    name: str,
) -> None:
    if supplied and supplied != expected:
        raise FactContractError(
            "DIGEST_MISMATCH",
            f"{name} does not match normalized content.",
        )


__all__ = [
    "GOOGLE_PLACE_IDENTITY_FIELD_MASK",
    "GOOGLE_PLACE_ID_REFRESH_FIELD_MASK",
    "PLACE_IDENTITY_REVIEW_VERSION",
    "PlaceCandidateRejection",
    "PlaceEndpointIdentity",
    "PlaceIdentityCandidate",
    "PlaceIdentityCandidateAssessment",
    "PlaceIdentityIntent",
    "PlaceIdentityRequest",
    "PlaceIdentityReview",
    "PlaceIdentityReviewAuthority",
    "PlaceIdentityReviewGrant",
    "PlaceIdentityReviewStatus",
    "build_google_place_identity_refresh_request",
    "build_google_place_identity_request",
    "evaluate_google_place_identity_candidates",
    "extract_fresh_google_place_endpoint",
    "finalize_google_place_identity_refresh",
    "finalize_google_place_identity_review",
]
