"""Exact must-not-exist request and preview values for an initial plan.

Creation is intentionally narrower than migration or patching.  The request
can contain only a generation-one, receipt-free canonical plan with
candidate/unverified guided activities and no canonical lodging selection.
The runtime composition sidecar is never accepted here.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .codec import (
    FrozenJsonValue,
    canonical_json_bytes,
    build_plan,
    decode_plan,
    deep_copy_json,
    encode_plan,
    freeze_json,
)
from .guided_draft import TripBriefDraft
from .guided_itinerary import (
    GuidedItineraryCandidate,
    GuidedItineraryResponse,
    GuidedItineraryResponseKind,
    GuidedItineraryResponseStatus,
    assess_guided_itinerary_response,
)
from .guided_proposal import (
    GuidedDirectionCard,
    GuidedDirectionPreference,
)
from .guided_refinement import (
    GuidedRefinementCandidate,
    GuidedRefinementResponse,
)
from .models import CheckReport
from .mutations import is_lodging_activity_type


PLAN_CREATION_VERSION = "plan-creation/v1"
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_REQUEST_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_IDEMPOTENCY_KEY = 256
_TRIP_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_REQUEST_TOKEN = object()
_PREVIEW_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False, repr=False)
class PlanCreateRequest:
    """One private exact request to create an absent canonical baseline."""

    trip_id: str
    idempotency_key: str
    source_binding_digest: str
    evaluation_at: datetime
    candidate_plan: FrozenJsonValue = field(repr=False)
    candidate_bytes: bytes = field(repr=False)
    candidate_sha256: str
    request_digest: str
    contract_version: str = PLAN_CREATION_VERSION

    def __init__(
        self,
        *,
        trip_id: str,
        idempotency_key: str,
        source_binding_digest: str,
        evaluation_at: datetime,
        candidate_plan: Mapping[str, Any],
        _token: object | None = None,
    ) -> None:
        if _token is not _REQUEST_TOKEN:
            raise ValueError("Plan create requests require the trusted projector")
        candidate = _validate_initial_candidate(candidate_plan)
        if trip_id != candidate["trip_id"]:
            raise ValueError("Plan create trip ID differs from its candidate")
        key = _idempotency_key(idempotency_key)
        source_digest = _digest(source_binding_digest, "source_binding_digest")
        evaluated = _aware_utc(evaluation_at, "evaluation_at")
        candidate_bytes = encode_plan(candidate)
        candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
        request_digest = "sha256:" + _sha256(
            {
                "contract_version": PLAN_CREATION_VERSION,
                "trip_id": trip_id,
                "idempotency_key": key,
                "source_binding_digest": source_digest,
                "evaluation_at": evaluated.isoformat(),
                "candidate_sha256": candidate_sha256,
                "candidate_revision": candidate["revision"],
            }
        )
        object.__setattr__(self, "trip_id", trip_id)
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(self, "source_binding_digest", source_digest)
        object.__setattr__(self, "evaluation_at", evaluated)
        object.__setattr__(self, "candidate_plan", freeze_json(candidate))
        object.__setattr__(self, "candidate_bytes", candidate_bytes)
        object.__setattr__(self, "candidate_sha256", candidate_sha256)
        object.__setattr__(self, "request_digest", request_digest)
        object.__setattr__(self, "contract_version", PLAN_CREATION_VERSION)

    def __repr__(self) -> str:
        return (
            "PlanCreateRequest("
            f"trip_id={self.trip_id!r}, idempotency_key={self.idempotency_key!r})"
        )

    def mutable_candidate_plan(self) -> dict[str, Any]:
        value = deep_copy_json(self.candidate_plan)
        assert isinstance(value, dict)
        return value

    def to_safe_dict(self) -> dict[str, Any]:
        self.verify()
        return {
            "contract_version": self.contract_version,
            "trip_id": self.trip_id,
            "idempotency_key": self.idempotency_key,
            "evaluation_at": self.evaluation_at.isoformat(),
            "candidate_revision": self.mutable_candidate_plan()["revision"],
            "candidate_plan_exposed": False,
            "source_binding_digest_exposed": False,
            "request_digest_exposed": False,
            "expected_absent": True,
        }

    def to_review_payload(self) -> dict[str, Any]:
        """Return the exact bounded projection for an informed private review."""

        self.verify()
        return {
            "contract_version": self.contract_version,
            "trip_id": self.trip_id,
            "candidate_revision": self.mutable_candidate_plan()["revision"],
            "candidate_plan": self.mutable_candidate_plan(),
            "source_backed_guided_projection": True,
            "provider_runtime_state_included": False,
            "lodging_selection_included": False,
        }

    def verify(self) -> None:
        rebuilt = PlanCreateRequest(
            trip_id=self.trip_id,
            idempotency_key=self.idempotency_key,
            source_binding_digest=self.source_binding_digest,
            evaluation_at=self.evaluation_at,
            candidate_plan=self.mutable_candidate_plan(),
            _token=_REQUEST_TOKEN,
        )
        if (
            rebuilt.candidate_bytes != self.candidate_bytes
            or rebuilt.candidate_sha256 != self.candidate_sha256
            or rebuilt.request_digest != self.request_digest
            or rebuilt.contract_version != self.contract_version
        ):
            raise ValueError("Plan create request no longer matches its candidate")


@dataclass(frozen=True, slots=True, init=False, repr=False)
class PlanCreatePreview:
    """Read-only kernel preview bound to one exact store target."""

    request: PlanCreateRequest = field(repr=False)
    store_slug: str
    store_target_digest: str = field(repr=False)
    candidate_revision: str
    candidate_state_digest: str
    preview_digest: str
    check_report: CheckReport = field(repr=False)
    contract_version: str = PLAN_CREATION_VERSION

    def __init__(
        self,
        *,
        request: PlanCreateRequest,
        store_slug: str,
        store_target_digest: str,
        check_report: CheckReport,
        _token: object | None = None,
    ) -> None:
        if _token is not _PREVIEW_TOKEN:
            raise ValueError("Plan create previews require TripStore validation")
        if type(request) is not PlanCreateRequest:
            raise TypeError("Plan create preview requires an exact request")
        request.verify()
        if type(check_report) is not CheckReport:
            raise TypeError("Plan create preview requires an exact check report")
        if store_slug != request.trip_id:
            raise ValueError("Plan create preview target differs from request")
        target_digest = _digest(
            store_target_digest,
            "store_target_digest",
        )
        candidate = request.mutable_candidate_plan()
        candidate_revision = str(candidate["revision"])
        state_digest = hashlib.sha256(
            canonical_json_bytes(candidate["state"])
        ).hexdigest()
        preview_digest = _sha256(
            {
                "contract_version": PLAN_CREATION_VERSION,
                "domain": "plan-create-preview",
                "request_digest": request.request_digest,
                "store_slug": store_slug,
                "store_target_digest": target_digest,
                "candidate_revision": candidate_revision,
                "candidate_state_digest": state_digest,
                "check_report": _private_value(check_report),
            }
        )
        object.__setattr__(self, "request", request)
        object.__setattr__(self, "store_slug", store_slug)
        object.__setattr__(self, "store_target_digest", target_digest)
        object.__setattr__(self, "candidate_revision", candidate_revision)
        object.__setattr__(self, "candidate_state_digest", state_digest)
        object.__setattr__(self, "preview_digest", preview_digest)
        object.__setattr__(self, "check_report", check_report)
        object.__setattr__(self, "contract_version", PLAN_CREATION_VERSION)

    def __repr__(self) -> str:
        return (
            "PlanCreatePreview("
            f"store_slug={self.store_slug!r}, "
            f"check_status={self.check_report.status.value!r})"
        )

    def to_safe_dict(self) -> dict[str, Any]:
        self.verify()
        return {
            "contract_version": self.contract_version,
            "store_slug": self.store_slug,
            "candidate_revision": self.candidate_revision,
            "check_status": self.check_report.status.value,
            "issue_codes": [item.code for item in self.check_report.issues],
            "expected_absent": True,
            "candidate_plan_exposed": False,
            "source_binding_digest_exposed": False,
            "preview_digest_exposed": False,
            "writes_to_trip": False,
        }

    def verify(self) -> None:
        rebuilt = PlanCreatePreview(
            request=self.request,
            store_slug=self.store_slug,
            store_target_digest=self.store_target_digest,
            check_report=self.check_report,
            _token=_PREVIEW_TOKEN,
        )
        if (
            rebuilt.candidate_revision != self.candidate_revision
            or rebuilt.candidate_state_digest != self.candidate_state_digest
            or rebuilt.preview_digest != self.preview_digest
            or rebuilt.contract_version != self.contract_version
        ):
            raise ValueError("Plan create preview no longer matches its request")


def prepare_guided_plan_create_request(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...],
    preference: GuidedDirectionPreference,
    refinement: GuidedRefinementCandidate,
    refinement_response: GuidedRefinementResponse,
    itinerary_candidate: GuidedItineraryCandidate,
    itinerary_response: GuidedItineraryResponse,
    *,
    trip_id: str,
    timezone_name: str,
    idempotency_key: str,
    evaluation_at: datetime,
) -> PlanCreateRequest:
    """Project one exact accepted guided itinerary into an initial plan."""

    response_review = assess_guided_itinerary_response(
        brief,
        cards,
        preference,
        refinement,
        refinement_response,
        itinerary_candidate,
        itinerary_response,
    )
    if (
        response_review.response_kind
        is not GuidedItineraryResponseKind.ACCEPT_ITINERARY_CANDIDATE
        or response_review.status
        is not GuidedItineraryResponseStatus.READY_FOR_PRIVATE_EVIDENCE_REQUIREMENTS
    ):
        raise ValueError("Plan creation requires an exact accepted itinerary")
    if (
        brief.destination is None
        or brief.dates.start is None
        or brief.dates.end is None
    ):
        raise ValueError("Plan creation requires exact destination and dates")
    if brief.transport_boundaries:
        raise ValueError(
            "Initial plan creation cannot yet preserve transport boundaries"
        )
    normalized_trip_id = _trip_id(trip_id)
    normalized_timezone = _timezone_name(timezone_name)
    source_binding_digest = _sha256(
        {
            "contract_version": PLAN_CREATION_VERSION,
            "domain": "guided-plan-create-source",
            "brief": brief,
            "cards": tuple(sorted(cards, key=lambda item: item.card_ref)),
            "preference": preference,
            "refinement": refinement,
            "refinement_response": refinement_response,
            "itinerary_candidate": itinerary_candidate,
            "itinerary_response": itinerary_response,
            "itinerary_response_review": response_review,
            "trip_id": normalized_trip_id,
            "timezone": normalized_timezone,
        }
    )
    candidate = _guided_candidate_plan(
        brief,
        refinement,
        itinerary_candidate,
        trip_id=normalized_trip_id,
        timezone_name=normalized_timezone,
        source_binding_digest=source_binding_digest,
    )
    return PlanCreateRequest(
        trip_id=str(candidate["trip_id"]),
        idempotency_key=idempotency_key,
        source_binding_digest=source_binding_digest,
        evaluation_at=evaluation_at,
        candidate_plan=candidate,
        _token=_REQUEST_TOKEN,
    )


def _guided_candidate_plan(
    brief: TripBriefDraft,
    refinement: GuidedRefinementCandidate,
    itinerary: GuidedItineraryCandidate,
    *,
    trip_id: str,
    timezone_name: str,
    source_binding_digest: str,
) -> dict[str, Any]:
    assert brief.destination is not None
    assert brief.dates.start is not None and brief.dates.end is not None
    placed_by_day = {
        day.relative_day_index: day.source_line_indexes
        for day in itinerary.days
    }
    days: list[dict[str, Any]] = []
    day_count = (brief.dates.end - brief.dates.start).days + 1
    for day_index in range(day_count):
        day_date = brief.dates.start + timedelta(days=day_index)
        activities: list[dict[str, Any]] = []
        for source_index in placed_by_day.get(day_index, ()):
            line = refinement.direction.lines[source_index]
            identity = _sha256(
                {
                    "source_binding_digest": source_binding_digest,
                    "source_index": source_index,
                    "relative_day_index": day_index,
                }
            )
            activities.append(
                {
                    "activity_id": f"guided-activity-{identity}",
                    "title": line.title,
                    "location_id": f"guided-location-{identity}",
                    "decision_state": "candidate",
                    "flexibility": "movable",
                    "evidence_state": "unverified",
                    "type": "activity",
                }
            )
        days.append(
            {
                "day_id": f"guided-day-{day_index + 1}",
                "day": day_index + 1,
                "date": day_date.isoformat(),
                "timezone": timezone_name,
                "places": activities,
                "travel": [],
            }
        )
    destination = brief.destination.location.label
    return build_plan(
        trip_id=trip_id,
        generation=1,
        state={
            "trip": {
                "slug": trip_id,
                "title": destination,
                "timezone": timezone_name,
                "date_range": (
                    f"{brief.dates.start.isoformat()} ~ "
                    f"{brief.dates.end.isoformat()}"
                ),
                "cities": [destination],
            },
            "itinerary": {
                "available_modes": [],
                "days": days,
            },
        },
    )


def _validate_initial_candidate(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    candidate = decode_plan(encode_plan(value))
    if candidate["generation"] != 1 or candidate["receipts"] != {}:
        raise ValueError("Initial plan must be generation one and receipt-free")
    state = candidate["state"]
    assert isinstance(state, dict)
    trip = state["trip"]
    itinerary = state["itinerary"]
    assert isinstance(trip, dict) and isinstance(itinerary, dict)
    if trip.get("lodgings", []) != [] or "_trip_planner" in trip:
        raise ValueError(
            "Initial guided plan cannot bypass lodging or migration review"
        )
    days = itinerary.get("days")
    if not isinstance(days, list):
        raise ValueError("Initial plan itinerary days must be an array")
    for day in days:
        if not isinstance(day, dict):
            raise ValueError("Initial plan days must be objects")
        if day.get("start_lodging_id") is not None or day.get(
            "end_lodging_id"
        ) is not None:
            raise ValueError("Initial guided plan cannot contain lodging refs")
        places = day.get("places")
        if not isinstance(places, list):
            raise ValueError("Initial plan activities must be an array")
        for activity in places:
            if (
                not isinstance(activity, dict)
                or activity.get("decision_state") != "candidate"
                or activity.get("evidence_state") != "unverified"
            ):
                raise ValueError(
                    "Initial guided activities must remain candidate and unverified"
                )
            if is_lodging_activity_type(activity.get("type")):
                raise ValueError(
                    "Initial guided plan cannot contain lodging activities"
                )
    return candidate


def _idempotency_key(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_IDEMPOTENCY_KEY
        or value != value.strip()
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise ValueError("idempotency_key must be bounded visible text")
    return value


def _trip_id(value: str) -> str:
    if (
        type(value) is not str
        or len(value) > 128
        or _TRIP_ID_RE.fullmatch(value) is None
    ):
        raise ValueError("trip_id must be a strict canonical slug")
    return value


def _timezone_name(value: str) -> str:
    if type(value) is not str or not value or len(value) > 128:
        raise ValueError("timezone_name must be a bounded IANA timezone")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("timezone_name must be a valid IANA timezone") from None
    return value


def _digest(value: str, name: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _aware_utc(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _sha256(value: object) -> str:
    encoded = json.dumps(
        _private_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _private_value(value: object) -> object:
    if value is None or type(value) in {str, int, bool, float}:
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _aware_utc(value, "private datetime").isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _private_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_private_value(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _private_value(getattr(value, item.name))
            for item in fields(value)
        }
    raise TypeError("Plan creation binding contains an unsupported value")


__all__ = [
    "PLAN_CREATION_VERSION",
    "PlanCreatePreview",
    "PlanCreateRequest",
    "prepare_guided_plan_create_request",
]
