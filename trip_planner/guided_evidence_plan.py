"""Private evidence-requirement declarations for an accepted itinerary.

Phase 5.10 consumes the exact accepted Phase 5.9 context and requires one
typed declaration for every refined direction line.  A declaration either
names one or more provider-neutral evidence topics that still require
verification, or records that no external evidence requirement has yet been
identified for that line.  The latter is not proof and never upgrades the
line from unverified.

This module only validates a process-local technical plan.  It stores no line
text, dates, provider identifiers, queries, payloads, or URLs; creates no
provider request; and grants no provider, scheduling, persistence, trip,
rendering, deployment, confirmation, or canonical-mutation authority.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import InitVar, dataclass, field
from enum import Enum
from typing import Any

from .guided_draft import TripBriefDraft
from .guided_itinerary import (
    GuidedItineraryCandidate,
    GuidedItineraryResponse,
    GuidedItineraryResponseKind,
    GuidedItineraryResponseStatus,
    assess_guided_itinerary_response,
)
from .guided_proposal import GuidedDirectionCard, GuidedDirectionPreference
from .guided_refinement import (
    GuidedRefinementCandidate,
    GuidedRefinementResponse,
)
from .models import DecisionState, EvidenceState


GUIDED_EVIDENCE_PLAN_VERSION = "guided-evidence-plan/v1"
_MAX_SOURCE_LINE_INDEX = 31
_MAX_DECLARATIONS = 64
_REFINE_ACTION = "refine_private_evidence_requirements"
_PROVIDER_SCOPE_REVIEW_ACTION = "prepare_private_provider_scope_review"
_REVIEW_TOKEN = object()
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
    "evidence_requirements",
}


class GuidedEvidenceDisposition(str, Enum):
    """Whether one source line currently names external evidence needs."""

    REQUIRES_VERIFICATION = "requires_verification"
    NO_EXTERNAL_EVIDENCE_IDENTIFIED = (
        "no_external_evidence_identified"
    )


class GuidedEvidenceTopic(str, Enum):
    """Small provider-neutral vocabulary for evidence still to be gathered."""

    PLACE_IDENTITY = "place_identity"
    CURRENT_OPENING_HOURS = "current_opening_hours"
    ROUTE = "route"
    LODGING = "lodging"
    AVAILABILITY = "availability"
    PRICE = "price"


class GuidedEvidencePlanStatus(str, Enum):
    """Whether the declaration plan is complete enough for scope review."""

    NEEDS_REFINEMENT = "needs_refinement"
    READY_FOR_PRIVATE_PROVIDER_SCOPE = "ready_for_private_provider_scope"


class GuidedEvidencePlanProblemCode(str, Enum):
    """Redacted reasons an evidence-requirement plan is incomplete."""

    UNKNOWN_SOURCE_LINE_INCLUDED = "unknown_source_line_included"
    SOURCE_LINE_NOT_CLASSIFIED = "source_line_not_classified"
    SOURCE_LINE_CLASSIFIED_MULTIPLE_TIMES = (
        "source_line_classified_multiple_times"
    )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedLineEvidenceRequirement:
    """One source-index-only evidence declaration; never a provider request."""

    source_line_index: int = field(repr=False)
    disposition: GuidedEvidenceDisposition
    topics: tuple[GuidedEvidenceTopic, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if type(self.source_line_index) is not int or not (
            0 <= self.source_line_index <= _MAX_SOURCE_LINE_INDEX
        ):
            raise ValueError(
                "GuidedLineEvidenceRequirement.source_line_index is invalid"
            )
        if type(self.disposition) is not GuidedEvidenceDisposition:
            raise TypeError(
                "GuidedLineEvidenceRequirement.disposition must be exact"
            )
        if (
            not isinstance(self.topics, tuple)
            or any(type(item) is not GuidedEvidenceTopic for item in self.topics)
        ):
            raise TypeError(
                "GuidedLineEvidenceRequirement.topics must contain exact values"
            )
        topics = tuple(sorted(set(self.topics), key=lambda item: item.value))
        if (
            self.disposition is GuidedEvidenceDisposition.REQUIRES_VERIFICATION
            and not topics
        ):
            raise ValueError(
                "Requires-verification declarations need at least one topic"
            )
        if (
            self.disposition
            is GuidedEvidenceDisposition.NO_EXTERNAL_EVIDENCE_IDENTIFIED
            and topics
        ):
            raise ValueError(
                "No-external-evidence declarations cannot name topics"
            )
        object.__setattr__(self, "topics", topics)

    def __repr__(self) -> str:
        return (
            "GuidedLineEvidenceRequirement("
            f"disposition={self.disposition.value!r}, "
            f"topic_count={len(self.topics)!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedEvidenceRequirementPlan:
    """Complete intended line coverage, kept only in private process memory."""

    declarations: tuple[GuidedLineEvidenceRequirement, ...] = field(
        default=(),
        repr=False,
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.declarations, tuple)
            or any(
                type(item) is not GuidedLineEvidenceRequirement
                for item in self.declarations
            )
        ):
            raise TypeError(
                "GuidedEvidenceRequirementPlan.declarations must be exact"
            )
        if len(self.declarations) > _MAX_DECLARATIONS:
            raise ValueError(
                "GuidedEvidenceRequirementPlan has too many declarations"
            )
        object.__setattr__(
            self,
            "declarations",
            tuple(
                sorted(
                    self.declarations,
                    key=lambda item: (
                        item.source_line_index,
                        item.disposition.value,
                        tuple(topic.value for topic in item.topics),
                    ),
                )
            ),
        )

    @property
    def decision_state(self) -> DecisionState:
        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        return EvidenceState.UNVERIFIED

    def __repr__(self) -> str:
        return (
            "GuidedEvidenceRequirementPlan("
            f"declaration_count={len(self.declarations)!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedEvidencePlanReview:
    """Redacted aggregate assessment of one private declaration plan."""

    status: GuidedEvidencePlanStatus
    next_action: str
    source_line_count: int
    declaration_count: int
    classified_source_line_count: int
    requires_verification_line_count: int
    no_external_evidence_identified_line_count: int
    relative_day_bucket_count: int
    transport_boundary_count: int
    topic_counts: tuple[tuple[GuidedEvidenceTopic, int], ...] = ()
    problem_codes: tuple[GuidedEvidencePlanProblemCode, ...] = ()
    tentative_fields: tuple[str, ...] = ()
    needs_verification: tuple[str, ...] = ()
    contract_version: str = GUIDED_EVIDENCE_PLAN_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError(
                "Guided evidence plan reviews must be created by the assessor"
            )
        if type(self.status) is not GuidedEvidencePlanStatus:
            raise TypeError("GuidedEvidencePlanReview.status must be exact")
        for name, value, maximum in (
            ("source_line_count", self.source_line_count, 32),
            ("declaration_count", self.declaration_count, _MAX_DECLARATIONS),
            (
                "classified_source_line_count",
                self.classified_source_line_count,
                32,
            ),
            (
                "requires_verification_line_count",
                self.requires_verification_line_count,
                _MAX_DECLARATIONS,
            ),
            (
                "no_external_evidence_identified_line_count",
                self.no_external_evidence_identified_line_count,
                _MAX_DECLARATIONS,
            ),
            ("relative_day_bucket_count", self.relative_day_bucket_count, 367),
            ("transport_boundary_count", self.transport_boundary_count, 32),
        ):
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError(f"GuidedEvidencePlanReview.{name} is invalid")
        if self.source_line_count < 1 or self.relative_day_bucket_count < 1:
            raise ValueError(
                "GuidedEvidencePlanReview requires current itinerary content"
            )
        if self.classified_source_line_count > self.source_line_count:
            raise ValueError(
                "GuidedEvidencePlanReview classified count is inconsistent"
            )
        if (
            self.requires_verification_line_count
            + self.no_external_evidence_identified_line_count
            != self.declaration_count
        ):
            raise ValueError(
                "GuidedEvidencePlanReview disposition counts are inconsistent"
            )
        expected_topic_counts = tuple(
            (topic, count)
            for topic, count in sorted(
                self.topic_counts,
                key=lambda item: item[0].value,
            )
        )
        if (
            not isinstance(self.topic_counts, tuple)
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or type(item[0]) is not GuidedEvidenceTopic
                or type(item[1]) is not int
                or not 0 <= item[1] <= _MAX_DECLARATIONS
                for item in self.topic_counts
            )
            or len(self.topic_counts) != len(GuidedEvidenceTopic)
            or len({item[0] for item in self.topic_counts})
            != len(GuidedEvidenceTopic)
            or {item[0] for item in self.topic_counts}
            != set(GuidedEvidenceTopic)
            or self.topic_counts != expected_topic_counts
        ):
            raise ValueError(
                "GuidedEvidencePlanReview topic counts are invalid"
            )
        if (
            not isinstance(self.problem_codes, tuple)
            or any(
                type(item) is not GuidedEvidencePlanProblemCode
                for item in self.problem_codes
            )
            or tuple(
                sorted(set(self.problem_codes), key=lambda item: item.value)
            )
            != self.problem_codes
        ):
            raise ValueError(
                "GuidedEvidencePlanReview problem codes are invalid"
            )
        if self.status is GuidedEvidencePlanStatus.NEEDS_REFINEMENT:
            if not self.problem_codes or self.next_action != _REFINE_ACTION:
                raise ValueError(
                    "Needs-refinement evidence plan requires repair problems"
                )
        elif (
            self.problem_codes
            or self.next_action != _PROVIDER_SCOPE_REVIEW_ACTION
            or self.declaration_count != self.source_line_count
            or self.classified_source_line_count != self.source_line_count
        ):
            raise ValueError(
                "Provider-scope-ready evidence plan is inconsistent"
            )
        if (
            not isinstance(self.tentative_fields, tuple)
            or any(item not in _TENTATIVE_FIELDS for item in self.tentative_fields)
            or len(set(self.tentative_fields)) != len(self.tentative_fields)
            or not isinstance(self.needs_verification, tuple)
            or any(item not in _VERIFICATION_TOPICS for item in self.needs_verification)
            or len(set(self.needs_verification)) != len(self.needs_verification)
        ):
            raise ValueError(
                "GuidedEvidencePlanReview safe topic collections are invalid"
            )
        if self.contract_version != GUIDED_EVIDENCE_PLAN_VERSION:
            raise ValueError("Unsupported guided evidence plan contract version")

    @property
    def may_prepare_private_provider_scope(self) -> bool:
        return (
            self.status
            is GuidedEvidencePlanStatus.READY_FOR_PRIVATE_PROVIDER_SCOPE
        )

    def __repr__(self) -> str:
        return (
            "GuidedEvidencePlanReview("
            f"status={self.status.value!r}, "
            f"next_action={self.next_action!r}, "
            f"problem_count={len(self.problem_codes)!r}, "
            f"source_line_count={self.source_line_count!r}, "
            f"declaration_count={self.declaration_count!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return only aggregate state, never raw declarations or indexes."""

        ready = self.may_prepare_private_provider_scope
        topic_counts = {
            topic.value: count for topic, count in self.topic_counts
        }
        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_response": False,
            "requires_user_review": False,
            "requires_user_decision": False,
            "evidence_requirement_plan": {
                "source_line_count": self.source_line_count,
                "declaration_count": self.declaration_count,
                "classified_source_line_count": (
                    self.classified_source_line_count
                ),
                "requires_verification_line_count": (
                    self.requires_verification_line_count
                ),
                "no_external_evidence_identified_line_count": (
                    self.no_external_evidence_identified_line_count
                ),
                "topic_counts": topic_counts,
                "evidence_requirements": [
                    topic for topic, count in topic_counts.items() if count
                ],
                "all_source_lines_classified": ready,
                "no_external_evidence_identified_means_verified": False,
                "contains_line_text": False,
                "contains_calendar_dates": False,
                "contains_provider_identifiers": False,
                "contains_provider_query": False,
                "contains_provider_payload": False,
                "relative_day_bucket_count": self.relative_day_bucket_count,
                "transport_boundary_count": self.transport_boundary_count,
                "decision_state": DecisionState.CANDIDATE.value,
                "evidence_state": EvidenceState.UNVERIFIED.value,
                "supports_authoritative_use": False,
            },
            "provider_scope": {
                "may_prepare_private_provider_scope_review": ready,
                "provider_scope_authorized": False,
                "provider_requests_created": False,
                "provider_calls_permitted": False,
            },
            "problems": [item.value for item in self.problem_codes],
            "tentative_fields": list(self.tentative_fields),
            "needs_verification": list(self.needs_verification),
            "side_effects": {
                "process_local": True,
                "writes_to_trip": False,
                "provider_calls": False,
                "provider_requests_created": False,
                "scheduled": False,
                "trip_created": False,
                "rendered": False,
                "deployed": False,
            },
        }


def assess_guided_evidence_requirement_plan(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...],
    preference: GuidedDirectionPreference,
    refinement: GuidedRefinementCandidate,
    refinement_response: GuidedRefinementResponse,
    itinerary_candidate: GuidedItineraryCandidate,
    itinerary_response: GuidedItineraryResponse,
    plan: GuidedEvidenceRequirementPlan,
) -> GuidedEvidencePlanReview:
    """Revalidate exact Phase 5.9 acceptance and assess line declarations."""

    if type(plan) is not GuidedEvidenceRequirementPlan:
        raise TypeError("plan must be an exact GuidedEvidenceRequirementPlan")
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
        raise ValueError(
            "An evidence requirement plan requires an exact accepted itinerary"
        )

    declarations = plan.declarations
    line_count = len(refinement.direction.lines)
    declaration_counts = Counter(
        item.source_line_index for item in declarations
    )
    problems: set[GuidedEvidencePlanProblemCode] = set()
    if any(index >= line_count for index in declaration_counts):
        problems.add(
            GuidedEvidencePlanProblemCode.UNKNOWN_SOURCE_LINE_INCLUDED
        )
    if any(declaration_counts[index] == 0 for index in range(line_count)):
        problems.add(GuidedEvidencePlanProblemCode.SOURCE_LINE_NOT_CLASSIFIED)
    if any(count > 1 for count in declaration_counts.values()):
        problems.add(
            GuidedEvidencePlanProblemCode.SOURCE_LINE_CLASSIFIED_MULTIPLE_TIMES
        )

    problem_codes = tuple(sorted(problems, key=lambda item: item.value))
    status = (
        GuidedEvidencePlanStatus.NEEDS_REFINEMENT
        if problem_codes
        else GuidedEvidencePlanStatus.READY_FOR_PRIVATE_PROVIDER_SCOPE
    )
    disposition_counts = Counter(item.disposition for item in declarations)
    topic_counter: Counter[GuidedEvidenceTopic] = Counter(
        topic for item in declarations for topic in item.topics
    )
    topic_counts = tuple(
        (topic, topic_counter[topic])
        for topic in sorted(GuidedEvidenceTopic, key=lambda item: item.value)
    )
    needs_verification = tuple(
        dict.fromkeys(
            (*response_review.needs_verification, "evidence_requirements")
        )
    )
    return GuidedEvidencePlanReview(
        status=status,
        next_action=(
            _REFINE_ACTION
            if status is GuidedEvidencePlanStatus.NEEDS_REFINEMENT
            else _PROVIDER_SCOPE_REVIEW_ACTION
        ),
        source_line_count=line_count,
        declaration_count=len(declarations),
        classified_source_line_count=sum(
            1 for index in range(line_count) if declaration_counts[index] > 0
        ),
        requires_verification_line_count=disposition_counts[
            GuidedEvidenceDisposition.REQUIRES_VERIFICATION
        ],
        no_external_evidence_identified_line_count=disposition_counts[
            GuidedEvidenceDisposition.NO_EXTERNAL_EVIDENCE_IDENTIFIED
        ],
        relative_day_bucket_count=(
            response_review.relative_day_bucket_count
        ),
        transport_boundary_count=response_review.transport_boundary_count,
        topic_counts=topic_counts,
        problem_codes=problem_codes,
        tentative_fields=response_review.tentative_fields,
        needs_verification=needs_verification,
        _token=_REVIEW_TOKEN,
    )


__all__ = [
    "GUIDED_EVIDENCE_PLAN_VERSION",
    "GuidedEvidenceDisposition",
    "GuidedEvidencePlanProblemCode",
    "GuidedEvidencePlanReview",
    "GuidedEvidencePlanStatus",
    "GuidedEvidenceRequirementPlan",
    "GuidedEvidenceTopic",
    "GuidedLineEvidenceRequirement",
    "assess_guided_evidence_requirement_plan",
]
