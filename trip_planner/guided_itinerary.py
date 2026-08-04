"""Private relative-day itinerary candidate for an accepted trip direction.

Phase 5.8 consumes the exact accepted Phase 5.7 context and lets a host place
each refined direction line into one relative day bucket.  The buckets are
zero-based indexes from arrival day through departure day; they are not
calendar dates, times, durations, routes, or an executable schedule.  The
candidate contains only source indexes and opaque transport-boundary IDs, so
it cannot silently rewrite private direction text.

This module is process-local and has no parser, provider, scheduler,
persistence, trip creation, rendering, deployment, confirmation, or canonical
mutation path.  Every result remains candidate + unverified.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import InitVar, dataclass, field
from enum import Enum
from typing import Any

from .guided_draft import TripBriefDraft
from .guided_proposal import (
    GuidedDirectionCard,
    GuidedDirectionPreference,
)
from .guided_refinement import (
    GuidedRefinementCandidate,
    GuidedRefinementResponse,
    GuidedRefinementResponseKind,
    GuidedRefinementResponseStatus,
    assess_guided_refinement_response,
)
from .models import DecisionState, EvidenceState


GUIDED_ITINERARY_VERSION = "guided-itinerary/v1"
_MAX_RELATIVE_DAY_INDEX = 366
_MAX_REFINED_LINE_INDEX = 31
_MAX_LINES_PER_DAY = 32
_MAX_DAY_BUCKETS = 367
_MAX_TRANSPORT_BOUNDARIES = 32
_MAX_RETAINED_TRANSPORT_BOUNDARIES = 64
_MAX_BOUNDARY_ID_LENGTH = 160
_MAX_PLACEMENTS = _MAX_DAY_BUCKETS * _MAX_LINES_PER_DAY
_REVIEW_TOKEN = object()
_REFINE_ACTION = "refine_private_itinerary_candidate"
_REVIEW_ACTION = "review_private_itinerary_candidate"
_REVIEW_PROMPT = (
    "這個每日候選安排是否符合你的想法？可以接受安排，或指出要調整的地方。"
)
_REVIEW_DISCLOSURE = (
    "每日候選尚未確認景點身分、營業時間、交通、空位或價格，也不是可執行日程。"
)
_TENTATIVE_FIELDS = {
    "destination",
    "dates",
    "party",
    "budget",
    "pace",
    "must_do",
    "constraints",
}
_VERIFICATION_TOPICS = {
    "destination_location",
    "transport_boundaries",
    "lodging",
    "proposal_candidates",
    "itinerary_candidate",
}


class GuidedItineraryStatus(str, Enum):
    """Whether a private relative-day candidate needs repair or review."""

    NEEDS_REFINEMENT = "needs_refinement"
    REVIEW_REQUIRED = "review_required"


class GuidedItineraryProblemCode(str, Enum):
    """Redacted reasons a private itinerary candidate cannot be shown."""

    UNKNOWN_REFINED_LINE_INCLUDED = "unknown_refined_line_included"
    REFINED_LINE_NOT_PLACED = "refined_line_not_placed"
    REFINED_LINE_PLACED_MULTIPLE_TIMES = (
        "refined_line_placed_multiple_times"
    )
    RELATIVE_DAY_OUTSIDE_TRIP_SPAN = "relative_day_outside_trip_span"
    UNKNOWN_TRANSPORT_BOUNDARY_INCLUDED = (
        "unknown_transport_boundary_included"
    )
    TRANSPORT_BOUNDARY_NOT_RETAINED = "transport_boundary_not_retained"
    TRANSPORT_BOUNDARY_RETAINED_MULTIPLE_TIMES = (
        "transport_boundary_retained_multiple_times"
    )


def _private_boundary_id(value: object) -> str:
    if type(value) is not str:
        raise TypeError("Transport boundary references must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > _MAX_BOUNDARY_ID_LENGTH:
        raise ValueError("Transport boundary references must be bounded text")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError("Transport boundary references cannot contain controls")
    return normalized


@dataclass(frozen=True, slots=True, repr=False)
class GuidedItineraryDay:
    """One relative bucket containing exact refined-direction line indexes."""

    relative_day_index: int
    source_line_indexes: tuple[int, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.relative_day_index) is not int or not (
            0 <= self.relative_day_index <= _MAX_RELATIVE_DAY_INDEX
        ):
            raise ValueError("GuidedItineraryDay.relative_day_index is invalid")
        if (
            not isinstance(self.source_line_indexes, tuple)
            or not self.source_line_indexes
            or any(
                type(item) is not int
                or not 0 <= item <= _MAX_REFINED_LINE_INDEX
                for item in self.source_line_indexes
            )
        ):
            raise ValueError(
                "GuidedItineraryDay source indexes must be bounded and non-empty"
            )
        if len(self.source_line_indexes) > _MAX_LINES_PER_DAY:
            raise ValueError("GuidedItineraryDay has too many source lines")

    def __repr__(self) -> str:
        return (
            "GuidedItineraryDay("
            f"source_line_count={len(self.source_line_indexes)!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedItineraryCandidate:
    """Private source-only placement proposal; never an executable schedule."""

    days: tuple[GuidedItineraryDay, ...] = field(default=(), repr=False)
    retained_transport_boundary_ids: tuple[str, ...] = field(
        default=(),
        repr=False,
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.days, tuple)
            or any(type(item) is not GuidedItineraryDay for item in self.days)
        ):
            raise TypeError(
                "GuidedItineraryCandidate.days must contain exact values"
            )
        if len(self.days) > _MAX_DAY_BUCKETS:
            raise ValueError("GuidedItineraryCandidate has too many day buckets")
        day_indexes = [item.relative_day_index for item in self.days]
        if len(set(day_indexes)) != len(day_indexes):
            raise ValueError(
                "GuidedItineraryCandidate relative day indexes cannot repeat"
            )
        if (
            not isinstance(self.retained_transport_boundary_ids, tuple)
            or any(
                type(item) is not str
                for item in self.retained_transport_boundary_ids
            )
        ):
            raise TypeError(
                "GuidedItineraryCandidate boundary refs must be exact text"
            )
        if (
            len(self.retained_transport_boundary_ids)
            > _MAX_RETAINED_TRANSPORT_BOUNDARIES
        ):
            raise ValueError(
                "GuidedItineraryCandidate has too many transport boundary refs"
            )
        boundary_ids = tuple(
            sorted(
                _private_boundary_id(item)
                for item in self.retained_transport_boundary_ids
            )
        )
        object.__setattr__(
            self,
            "days",
            tuple(sorted(self.days, key=lambda item: item.relative_day_index)),
        )
        object.__setattr__(
            self,
            "retained_transport_boundary_ids",
            boundary_ids,
        )

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    def __repr__(self) -> str:
        return (
            "GuidedItineraryCandidate("
            f"relative_day_bucket_count={len(self.days)!r}, "
            f"placed_source_line_count="
            f"{sum(len(day.source_line_indexes) for day in self.days)!r}, "
            f"retained_transport_boundary_count="
            f"{len(self.retained_transport_boundary_ids)!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedItineraryReview:
    """Redacted assessment of one private relative-day candidate."""

    status: GuidedItineraryStatus
    next_action: str
    relative_day_bucket_count: int
    available_relative_day_count: int
    refined_source_line_count: int
    placed_source_line_count: int
    user_stated_line_count: int
    tentative_line_count: int
    ai_candidate_line_count: int
    expected_transport_boundary_count: int
    retained_transport_boundary_count: int
    problem_codes: tuple[GuidedItineraryProblemCode, ...] = ()
    tentative_fields: tuple[str, ...] = ()
    needs_verification: tuple[str, ...] = ()
    contract_version: str = GUIDED_ITINERARY_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError(
                "Guided itinerary reviews must be created by the assessor"
            )
        if type(self.status) is not GuidedItineraryStatus:
            raise TypeError("GuidedItineraryReview.status must be exact")
        for name, value, maximum in (
            ("relative_day_bucket_count", self.relative_day_bucket_count, 367),
            ("available_relative_day_count", self.available_relative_day_count, 367),
            ("refined_source_line_count", self.refined_source_line_count, 32),
            ("placed_source_line_count", self.placed_source_line_count, _MAX_PLACEMENTS),
            ("user_stated_line_count", self.user_stated_line_count, 32),
            ("tentative_line_count", self.tentative_line_count, 32),
            ("ai_candidate_line_count", self.ai_candidate_line_count, 32),
            (
                "expected_transport_boundary_count",
                self.expected_transport_boundary_count,
                _MAX_TRANSPORT_BOUNDARIES,
            ),
            (
                "retained_transport_boundary_count",
                self.retained_transport_boundary_count,
                _MAX_RETAINED_TRANSPORT_BOUNDARIES,
            ),
        ):
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError(f"GuidedItineraryReview.{name} is invalid")
        if self.available_relative_day_count < 1:
            raise ValueError(
                "GuidedItineraryReview needs at least one relative day"
            )
        if (
            self.user_stated_line_count
            + self.tentative_line_count
            + self.ai_candidate_line_count
            != self.refined_source_line_count
        ):
            raise ValueError(
                "GuidedItineraryReview source category counts are invalid"
            )
        if (
            not isinstance(self.problem_codes, tuple)
            or any(
                type(item) is not GuidedItineraryProblemCode
                for item in self.problem_codes
            )
            or tuple(sorted(set(self.problem_codes), key=lambda item: item.value))
            != self.problem_codes
        ):
            raise ValueError("GuidedItineraryReview problem codes are invalid")
        if self.status is GuidedItineraryStatus.NEEDS_REFINEMENT:
            if not self.problem_codes or self.next_action != _REFINE_ACTION:
                raise ValueError(
                    "Needs-refinement itinerary requires problems and repair action"
                )
        elif self.problem_codes or self.next_action != _REVIEW_ACTION:
            raise ValueError(
                "Review-required itinerary cannot carry repair problems"
            )
        if (
            not isinstance(self.tentative_fields, tuple)
            or any(item not in _TENTATIVE_FIELDS for item in self.tentative_fields)
            or len(set(self.tentative_fields)) != len(self.tentative_fields)
            or not isinstance(self.needs_verification, tuple)
            or any(
                item not in _VERIFICATION_TOPICS
                for item in self.needs_verification
            )
            or len(set(self.needs_verification)) != len(self.needs_verification)
        ):
            raise ValueError(
                "GuidedItineraryReview safe topic collections are invalid"
            )
        if self.contract_version != GUIDED_ITINERARY_VERSION:
            raise ValueError("Unsupported guided itinerary contract version")

    @property
    def may_present_itinerary_candidate(self) -> bool:
        return self.status is GuidedItineraryStatus.REVIEW_REQUIRED

    @property
    def review_prompt(self) -> str | None:
        return _REVIEW_PROMPT if self.may_present_itinerary_candidate else None

    @property
    def review_disclosure(self) -> str | None:
        return (
            _REVIEW_DISCLOSURE
            if self.may_present_itinerary_candidate
            else None
        )

    def __repr__(self) -> str:
        return (
            "GuidedItineraryReview("
            f"status={self.status.value!r}, "
            f"next_action={self.next_action!r}, "
            f"problem_count={len(self.problem_codes)!r}, "
            f"relative_day_bucket_count={self.relative_day_bucket_count!r}, "
            f"placed_source_line_count={self.placed_source_line_count!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return aggregate review state without private dates, text, or refs."""

        requires_review = self.may_present_itinerary_candidate
        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_response": requires_review,
            "requires_user_review": requires_review,
            "requires_user_decision": requires_review,
            "may_present_itinerary_candidate": requires_review,
            "review_prompt": self.review_prompt,
            "review_disclosure": self.review_disclosure,
            "itinerary_candidate": {
                "relative_day_bucket_count": self.relative_day_bucket_count,
                "available_relative_day_count": self.available_relative_day_count,
                "refined_source_line_count": self.refined_source_line_count,
                "placed_source_line_count": self.placed_source_line_count,
                "user_stated_line_count": self.user_stated_line_count,
                "tentative_line_count": self.tentative_line_count,
                "ai_candidate_line_count": self.ai_candidate_line_count,
                "expected_transport_boundary_count": (
                    self.expected_transport_boundary_count
                ),
                "retained_transport_boundary_count": (
                    self.retained_transport_boundary_count
                ),
                "uses_relative_day_indexes": True,
                "contains_calendar_dates": False,
                "contains_times": False,
                "contains_route_values": False,
                "is_executable_schedule": False,
                "decision_state": DecisionState.CANDIDATE.value,
                "evidence_state": EvidenceState.UNVERIFIED.value,
                "supports_authoritative_use": False,
            },
            "problems": [item.value for item in self.problem_codes],
            "tentative_fields": list(self.tentative_fields),
            "needs_verification": list(self.needs_verification),
            "side_effects": {
                "process_local": True,
                "writes_to_trip": False,
                "provider_calls": False,
                "scheduled": False,
                "trip_created": False,
                "rendered": False,
                "deployed": False,
            },
        }


def assess_guided_itinerary_candidate(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...],
    preference: GuidedDirectionPreference,
    refinement: GuidedRefinementCandidate,
    response: GuidedRefinementResponse,
    candidate: GuidedItineraryCandidate,
) -> GuidedItineraryReview:
    """Revalidate an accepted direction and assess one relative-day mapping."""

    if type(candidate) is not GuidedItineraryCandidate:
        raise TypeError("candidate must be an exact GuidedItineraryCandidate")
    response_review = assess_guided_refinement_response(
        brief,
        cards,
        preference,
        refinement,
        response,
    )
    if (
        response_review.response_kind
        is not GuidedRefinementResponseKind.ACCEPT_DIRECTION
        or response_review.status
        is not GuidedRefinementResponseStatus.READY_FOR_PRIVATE_ITINERARY_CANDIDATE
    ):
        raise ValueError(
            "A guided itinerary candidate requires an exact accepted direction"
        )
    overnight_count = brief.dates.overnight_count
    if overnight_count is None:
        raise ValueError(
            "A guided itinerary candidate requires exact current trip dates"
        )

    placements = tuple(
        source_line_index
        for day in candidate.days
        for source_line_index in day.source_line_indexes
    )
    line_count = len(refinement.direction.lines)
    placement_counts = Counter(placements)
    problems: set[GuidedItineraryProblemCode] = set()
    if any(index >= line_count for index in placements):
        problems.add(
            GuidedItineraryProblemCode.UNKNOWN_REFINED_LINE_INCLUDED
        )
    if any(placement_counts[index] == 0 for index in range(line_count)):
        problems.add(GuidedItineraryProblemCode.REFINED_LINE_NOT_PLACED)
    if any(placement_counts[index] > 1 for index in range(line_count)):
        problems.add(
            GuidedItineraryProblemCode.REFINED_LINE_PLACED_MULTIPLE_TIMES
        )
    if any(day.relative_day_index > overnight_count for day in candidate.days):
        problems.add(
            GuidedItineraryProblemCode.RELATIVE_DAY_OUTSIDE_TRIP_SPAN
        )

    expected_boundaries = Counter(
        item.boundary_id for item in brief.transport_boundaries
    )
    retained_boundaries = Counter(candidate.retained_transport_boundary_ids)
    if any(item not in expected_boundaries for item in retained_boundaries):
        problems.add(
            GuidedItineraryProblemCode.UNKNOWN_TRANSPORT_BOUNDARY_INCLUDED
        )
    if expected_boundaries - retained_boundaries:
        problems.add(
            GuidedItineraryProblemCode.TRANSPORT_BOUNDARY_NOT_RETAINED
        )
    if any(
        retained_boundaries[item] > expected_boundaries[item]
        for item in retained_boundaries
        if item in expected_boundaries
    ):
        problems.add(
            GuidedItineraryProblemCode.TRANSPORT_BOUNDARY_RETAINED_MULTIPLE_TIMES
        )

    problem_codes = tuple(sorted(problems, key=lambda item: item.value))
    status = (
        GuidedItineraryStatus.NEEDS_REFINEMENT
        if problem_codes
        else GuidedItineraryStatus.REVIEW_REQUIRED
    )
    source_buckets = Counter(
        line.presentation_source for line in refinement.direction.lines
    )
    needs_verification = tuple(
        dict.fromkeys(
            (*response_review.needs_verification, "itinerary_candidate")
        )
    )
    return GuidedItineraryReview(
        status=status,
        next_action=(
            _REFINE_ACTION
            if status is GuidedItineraryStatus.NEEDS_REFINEMENT
            else _REVIEW_ACTION
        ),
        relative_day_bucket_count=len(candidate.days),
        available_relative_day_count=overnight_count + 1,
        refined_source_line_count=line_count,
        placed_source_line_count=len(placements),
        user_stated_line_count=source_buckets["user_stated"],
        tentative_line_count=source_buckets["tentative"],
        ai_candidate_line_count=source_buckets["ai_candidate"],
        expected_transport_boundary_count=sum(expected_boundaries.values()),
        retained_transport_boundary_count=sum(retained_boundaries.values()),
        problem_codes=problem_codes,
        tentative_fields=response_review.tentative_fields,
        needs_verification=needs_verification,
        _token=_REVIEW_TOKEN,
    )


__all__ = [
    "GUIDED_ITINERARY_VERSION",
    "GuidedItineraryCandidate",
    "GuidedItineraryDay",
    "GuidedItineraryProblemCode",
    "GuidedItineraryReview",
    "GuidedItineraryStatus",
    "assess_guided_itinerary_candidate",
]
