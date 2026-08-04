"""Private, process-local intake for a future Trip Planner conversation.

This module is intentionally a small bridge between a natural-language host
and the existing typed transport/lodging intake.  It does not parse text, call
providers, create a trip, render output, or promote a user statement into a
decision or evidence fact.  A host extracts only what the user actually said,
constructs these bounded values in memory, and uses the redacted review to
decide whether one genuinely blocking question remains.

``BriefKnownState`` records conversational certainty only.  It is separate
from ``DecisionState`` and ``EvidenceState``: wording such as "fixed" or
"booked" must remain a ``ReportedDecisionClaim`` on the already-bound
transport/lodging candidate, which is still candidate + unverified.
"""

from __future__ import annotations

import unicodedata
from dataclasses import InitVar, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any

from .lodging import (
    LocationHint,
    LocationPrecision,
    LodgingCandidate,
    LodgingIntakeAssessment,
    LodgingIntakeStatus,
    LodgingRequirement,
    TransportBoundary,
    assess_lodging_intake,
)


GUIDED_DRAFT_VERSION = "guided-draft/v1"
_MAX_DATE_SPAN_DAYS = 366
_MAX_TEXT_LENGTH = 512
_MAX_TEXT_FACTS = 64
_MAX_TRANSPORT_BOUNDARIES = 32
_MAX_LODGING_CANDIDATES = 256


class BriefKnownState(str, Enum):
    """How definite a conversation fact is, without decision authority."""

    UNKNOWN = "unknown"
    TENTATIVE = "tentative"
    USER_STATED = "user_stated"


class GuidedDraftStatus(str, Enum):
    """Whether the host can begin a candidate proposal."""

    NEEDS_INPUT = "needs_input"
    READY_FOR_PROPOSAL = "ready_for_proposal"


class GuidedQuestionCode(str, Enum):
    """The deliberately small set of initial blocking questions."""

    DESTINATION = "destination"
    DATES = "dates"


_QUESTION_PROMPTS = {
    GuidedQuestionCode.DESTINATION: "你想規劃哪個目的地？",
    GuidedQuestionCode.DATES: "你的抵達與離開日期是什麼？",
}
_NEXT_ACTIONS = {
    "capture_destination",
    "capture_date_span",
    "prepare_candidate_proposal",
}
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
}
# This only prevents ordinary accidental construction; it is not an authority
# or security boundary.  No downstream write may rely on a guided review.
_REVIEW_TOKEN = object()


def _exact_date(value: object, name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{name} must be an exact date")
    return value


def _private_text(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > _MAX_TEXT_LENGTH:
        raise ValueError(f"{name} must be non-empty bounded text")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError(f"{name} cannot contain control characters")
    return normalized


@dataclass(frozen=True, slots=True, repr=False)
class BriefTextFact:
    """One private preference or constraint extracted from the conversation."""

    state: BriefKnownState
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.state) is not BriefKnownState:
            raise TypeError("BriefTextFact.state must be exact")
        if self.state is BriefKnownState.UNKNOWN:
            raise ValueError("A supplied BriefTextFact cannot be unknown")
        object.__setattr__(
            self,
            "value",
            _private_text(self.value, "BriefTextFact.value"),
        )

    def __repr__(self) -> str:
        return (
            "BriefTextFact("
            f"state={self.state.value!r}, has_value=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class DestinationDraft:
    """A private destination hint with conversational certainty."""

    location: LocationHint = field(repr=False)
    state: BriefKnownState = BriefKnownState.USER_STATED

    def __post_init__(self) -> None:
        if type(self.location) is not LocationHint:
            raise TypeError("DestinationDraft.location must be exact")
        if type(self.state) is not BriefKnownState:
            raise TypeError("DestinationDraft.state must be exact")
        if self.state is BriefKnownState.UNKNOWN:
            raise ValueError("A supplied destination cannot be unknown")

    def __repr__(self) -> str:
        return (
            "DestinationDraft("
            f"state={self.state.value!r}, "
            f"precision={self.location.precision.value!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class DateSpanDraft:
    """A half-open private stay span or an intentionally vague date hint.

    An exact span uses ``[start, end)`` so it can be passed unchanged to the
    existing nightly lodging assessor.  A phrase such as "十月初" belongs in a
    tentative ``hint`` and must not be converted into made-up dates.
    """

    state: BriefKnownState = BriefKnownState.UNKNOWN
    start: date | None = field(default=None, repr=False)
    end: date | None = field(default=None, repr=False)
    hint: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.state) is not BriefKnownState:
            raise TypeError("DateSpanDraft.state must be exact")
        has_start = self.start is not None
        has_end = self.end is not None
        if has_start != has_end:
            raise ValueError("DateSpanDraft requires both start and end")

        start = (
            _exact_date(self.start, "DateSpanDraft.start")
            if has_start
            else None
        )
        end = (
            _exact_date(self.end, "DateSpanDraft.end")
            if has_end
            else None
        )
        hint = (
            _private_text(self.hint, "DateSpanDraft.hint")
            if self.hint is not None
            else None
        )

        if start is not None and end is not None:
            if end <= start:
                raise ValueError("DateSpanDraft.end must be after start")
            if (end - start).days > _MAX_DATE_SPAN_DAYS:
                raise ValueError("DateSpanDraft exceeds the bounded date span")
            if hint is not None:
                raise ValueError("Exact dates cannot also carry a vague hint")
            if self.state is BriefKnownState.UNKNOWN:
                raise ValueError("Exact dates cannot be unknown")
        elif self.state is BriefKnownState.USER_STATED:
            raise ValueError("User-stated dates require an exact date span")
        elif self.state is BriefKnownState.TENTATIVE:
            if hint is None:
                raise ValueError("Tentative dates require a private date hint")
        elif hint is not None:
            raise ValueError("An unknown date span cannot carry a hint")

        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "hint", hint)

    @property
    def has_exact_span(self) -> bool:
        return self.start is not None

    @property
    def overnight_count(self) -> int | None:
        if self.start is None or self.end is None:
            return None
        return (self.end - self.start).days

    def __repr__(self) -> str:
        return (
            "DateSpanDraft("
            f"state={self.state.value!r}, "
            f"has_exact_span={self.has_exact_span!r}, "
            f"has_hint={self.hint is not None!r})"
        )


def _optional_text_fact(
    value: BriefTextFact | None,
    name: str,
) -> BriefTextFact | None:
    if value is not None and type(value) is not BriefTextFact:
        raise TypeError(f"{name} must be an exact BriefTextFact")
    return value


def _text_fact_tuple(
    value: tuple[BriefTextFact, ...],
    name: str,
) -> tuple[BriefTextFact, ...]:
    if (
        not isinstance(value, tuple)
        or any(type(item) is not BriefTextFact for item in value)
    ):
        raise TypeError(f"{name} must contain exact BriefTextFact values")
    if len(value) > _MAX_TEXT_FACTS:
        raise ValueError(f"{name} has too many values")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class TripBriefDraft:
    """A bounded, private draft that never becomes a canonical trip by itself."""

    destination: DestinationDraft | None = field(default=None, repr=False)
    dates: DateSpanDraft = field(default_factory=DateSpanDraft, repr=False)
    party: BriefTextFact | None = field(default=None, repr=False)
    budget: BriefTextFact | None = field(default=None, repr=False)
    pace: BriefTextFact | None = field(default=None, repr=False)
    must_do: tuple[BriefTextFact, ...] = field(default=(), repr=False)
    constraints: tuple[BriefTextFact, ...] = field(
        default=(),
        repr=False,
    )
    transport_boundaries: tuple[TransportBoundary, ...] = field(
        default=(),
        repr=False,
    )
    lodging_requirement: LodgingRequirement = LodgingRequirement.UNKNOWN
    lodging_candidates: tuple[LodgingCandidate, ...] = field(
        default=(),
        repr=False,
    )
    contract_version: str = GUIDED_DRAFT_VERSION

    def __post_init__(self) -> None:
        if (
            self.destination is not None
            and type(self.destination) is not DestinationDraft
        ):
            raise TypeError("TripBriefDraft.destination must be exact")
        if type(self.dates) is not DateSpanDraft:
            raise TypeError("TripBriefDraft.dates must be exact")
        _optional_text_fact(self.party, "TripBriefDraft.party")
        _optional_text_fact(self.budget, "TripBriefDraft.budget")
        _optional_text_fact(self.pace, "TripBriefDraft.pace")
        _text_fact_tuple(self.must_do, "TripBriefDraft.must_do")
        _text_fact_tuple(self.constraints, "TripBriefDraft.constraints")
        if type(self.lodging_requirement) is not LodgingRequirement:
            raise TypeError("TripBriefDraft.lodging_requirement must be exact")
        if self.contract_version != GUIDED_DRAFT_VERSION:
            raise ValueError("Unsupported guided draft contract version")
        if (
            not isinstance(self.transport_boundaries, tuple)
            or any(
                type(item) is not TransportBoundary
                for item in self.transport_boundaries
            )
        ):
            raise TypeError(
                "TripBriefDraft.transport_boundaries must contain exact values"
            )
        if len(self.transport_boundaries) > _MAX_TRANSPORT_BOUNDARIES:
            raise ValueError("TripBriefDraft has too many transport boundaries")
        boundary_ids = [item.boundary_id for item in self.transport_boundaries]
        if len(set(boundary_ids)) != len(boundary_ids):
            raise ValueError("TripBriefDraft transport boundaries cannot repeat")
        if (
            not isinstance(self.lodging_candidates, tuple)
            or any(
                type(item) is not LodgingCandidate
                for item in self.lodging_candidates
            )
        ):
            raise TypeError(
                "TripBriefDraft.lodging_candidates must contain exact values"
            )
        candidate_ids = [item.candidate_id for item in self.lodging_candidates]
        if len(candidate_ids) > _MAX_LODGING_CANDIDATES:
            raise ValueError("TripBriefDraft has too many lodging candidates")
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("TripBriefDraft lodging candidates cannot repeat")

    def __repr__(self) -> str:
        return (
            "TripBriefDraft("
            f"destination_provided={self.destination is not None!r}, "
            f"date_state={self.dates.state.value!r}, "
            f"transport_boundary_count={len(self.transport_boundaries)!r}, "
            f"lodging_candidate_count={len(self.lodging_candidates)!r})"
        )


@dataclass(frozen=True, slots=True)
class GuidedDraftQuestion:
    """One static, safe question chosen by the pure assessor."""

    code: GuidedQuestionCode

    def __post_init__(self) -> None:
        if type(self.code) is not GuidedQuestionCode:
            raise TypeError("GuidedDraftQuestion.code must be exact")

    @property
    def prompt(self) -> str:
        return _QUESTION_PROMPTS[self.code]

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "prompt": self.prompt}


@dataclass(frozen=True, slots=True)
class BriefFactSummary:
    """A safe presence/state view with no private conversational text."""

    provided: bool
    state: BriefKnownState

    def __post_init__(self) -> None:
        if type(self.provided) is not bool:
            raise TypeError("BriefFactSummary.provided must be bool")
        if type(self.state) is not BriefKnownState:
            raise TypeError("BriefFactSummary.state must be exact")
        if self.provided != (self.state is not BriefKnownState.UNKNOWN):
            raise ValueError("BriefFactSummary presence and state conflict")

    def to_dict(self) -> dict[str, object]:
        return {"provided": self.provided, "state": self.state.value}


@dataclass(frozen=True, slots=True)
class BriefFactCollectionSummary:
    """Safe aggregate certainty counts for a private fact collection."""

    user_stated_count: int
    tentative_count: int

    def __post_init__(self) -> None:
        for name, value in (
            ("user_stated_count", self.user_stated_count),
            ("tentative_count", self.tentative_count),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"BriefFactCollectionSummary.{name} is invalid")

    def to_dict(self) -> dict[str, int]:
        return {
            "user_stated": self.user_stated_count,
            "tentative": self.tentative_count,
        }


def _fact_view(value: BriefTextFact | None) -> BriefFactSummary:
    return BriefFactSummary(
        provided=value is not None,
        state=(
            value.state if value is not None else BriefKnownState.UNKNOWN
        ),
    )


def _fact_state_counts(
    values: tuple[BriefTextFact, ...],
) -> BriefFactCollectionSummary:
    return BriefFactCollectionSummary(
        user_stated_count=sum(
            1
            for item in values
            if item.state is BriefKnownState.USER_STATED
        ),
        tentative_count=sum(
            1
            for item in values
            if item.state is BriefKnownState.TENTATIVE
        ),
    )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedDraftReview:
    """Redacted result for the host's initial conversational next step."""

    status: GuidedDraftStatus
    next_question: GuidedDraftQuestion | None
    next_action: str
    destination_provided: bool
    destination_state: BriefKnownState
    destination_precision: LocationPrecision | None
    date_state: BriefKnownState
    date_has_exact_span: bool
    overnight_count: int | None
    party: BriefFactSummary
    budget: BriefFactSummary
    pace: BriefFactSummary
    must_do: BriefFactCollectionSummary
    constraints: BriefFactCollectionSummary
    transport_boundary_count: int = 0
    transport_exact_time_count: int = 0
    transport_window_count: int = 0
    transport_reported_decision_claim_count: int = 0
    lodging_requirement: LodgingRequirement = LodgingRequirement.UNKNOWN
    lodging_candidate_count: int = 0
    lodging_assessment_status: LodgingIntakeStatus | None = None
    lodging_issue_codes: tuple[str, ...] = ()
    lodging_advice: tuple[str, ...] = ()
    lodging_needs_verification: bool = False
    lodging_reported_decision_claim_count: int = 0
    tentative_fields: tuple[str, ...] = ()
    needs_verification: tuple[str, ...] = ()
    contract_version: str = GUIDED_DRAFT_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError("Guided draft reviews must be created by the assessor")
        if type(self.status) is not GuidedDraftStatus:
            raise TypeError("GuidedDraftReview.status must be exact")
        if (
            self.next_question is not None
            and type(self.next_question) is not GuidedDraftQuestion
        ):
            raise TypeError("GuidedDraftReview.next_question must be exact")
        if type(self.next_action) is not str:
            raise TypeError("GuidedDraftReview.next_action must be text")
        if self.status is GuidedDraftStatus.NEEDS_INPUT:
            if self.next_question is None:
                raise ValueError("Needs-input review requires one question")
        elif self.next_question is not None:
            raise ValueError("Ready review cannot carry an input question")
        if type(self.destination_provided) is not bool:
            raise TypeError("GuidedDraftReview.destination_provided must be bool")
        if type(self.destination_state) is not BriefKnownState:
            raise TypeError("GuidedDraftReview.destination_state must be exact")
        if (
            self.destination_precision is not None
            and type(self.destination_precision) is not LocationPrecision
        ):
            raise TypeError("GuidedDraftReview.destination_precision must be exact")
        if type(self.date_state) is not BriefKnownState:
            raise TypeError("GuidedDraftReview.date_state must be exact")
        if type(self.date_has_exact_span) is not bool:
            raise TypeError("GuidedDraftReview.date_has_exact_span must be bool")
        if self.overnight_count is not None and (
            type(self.overnight_count) is not int
            or not 1 <= self.overnight_count <= _MAX_DATE_SPAN_DAYS
        ):
            raise ValueError("GuidedDraftReview.overnight_count is invalid")
        for name, value in (
            ("party", self.party),
            ("budget", self.budget),
            ("pace", self.pace),
        ):
            if type(value) is not BriefFactSummary:
                raise TypeError(f"GuidedDraftReview.{name} must be exact")
        for name, value in (
            ("must_do", self.must_do),
            ("constraints", self.constraints),
        ):
            if type(value) is not BriefFactCollectionSummary:
                raise TypeError(f"GuidedDraftReview.{name} must be exact")
        if type(self.lodging_requirement) is not LodgingRequirement:
            raise TypeError("GuidedDraftReview.lodging_requirement must be exact")
        if self.lodging_candidate_count < 0:
            raise ValueError("GuidedDraftReview lodging candidate count is invalid")
        if (
            self.lodging_assessment_status is not None
            and type(self.lodging_assessment_status) is not LodgingIntakeStatus
        ):
            raise TypeError("GuidedDraftReview lodging status must be exact")
        if type(self.lodging_needs_verification) is not bool:
            raise TypeError("GuidedDraftReview lodging verification must be bool")
        if self.next_action not in _NEXT_ACTIONS:
            raise ValueError("GuidedDraftReview.next_action is unsupported")
        if (
            not isinstance(self.tentative_fields, tuple)
            or any(item not in _TENTATIVE_FIELDS for item in self.tentative_fields)
        ):
            raise ValueError("GuidedDraftReview tentative fields are unsupported")
        if (
            not isinstance(self.needs_verification, tuple)
            or any(item not in _VERIFICATION_TOPICS for item in self.needs_verification)
        ):
            raise ValueError("GuidedDraftReview verification topics are unsupported")
        for name, values in (
            ("lodging_issue_codes", self.lodging_issue_codes),
            ("lodging_advice", self.lodging_advice),
        ):
            if (
                not isinstance(values, tuple)
                or any(type(item) is not str for item in values)
            ):
                raise TypeError(f"GuidedDraftReview.{name} must be text tokens")
        if self.contract_version != GUIDED_DRAFT_VERSION:
            raise ValueError("Unsupported guided draft contract version")

    def __repr__(self) -> str:
        return (
            "GuidedDraftReview("
            f"status={self.status.value!r}, "
            f"next_action={self.next_action!r}, "
            f"transport_boundary_count={self.transport_boundary_count!r}, "
            f"lodging_candidate_count={self.lodging_candidate_count!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a safe transcript view without private trip input values."""

        lodging_status = (
            self.lodging_assessment_status.value
            if self.lodging_assessment_status is not None
            else "deferred_until_exact_dates"
        )
        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_question": (
                self.next_question.to_dict()
                if self.next_question is not None
                else None
            ),
            "next_action": self.next_action,
            "requires_user_response": self.next_question is not None,
            "proposal_requires_user_review": (
                self.status is GuidedDraftStatus.READY_FOR_PROPOSAL
            ),
            "brief": {
                "destination": {
                    "provided": self.destination_provided,
                    "state": self.destination_state.value,
                    "precision": (
                        self.destination_precision.value
                        if self.destination_precision is not None
                        else None
                    ),
                },
                "dates": {
                    "state": self.date_state.value,
                    "has_exact_span": self.date_has_exact_span,
                    "overnight_count": self.overnight_count,
                },
                "party": self.party.to_dict(),
                "budget": self.budget.to_dict(),
                "pace": self.pace.to_dict(),
                "must_do": self.must_do.to_dict(),
                "constraints": self.constraints.to_dict(),
            },
            "transport": {
                "candidate_count": self.transport_boundary_count,
                "exact_time_count": self.transport_exact_time_count,
                "window_count": self.transport_window_count,
                "reported_decision_claim_count": (
                    self.transport_reported_decision_claim_count
                ),
                "decision_state": (
                    "candidate"
                    if self.transport_boundary_count
                    else None
                ),
                "evidence_state": (
                    "unverified"
                    if self.transport_boundary_count
                    else None
                ),
            },
            "lodging": {
                "requirement": self.lodging_requirement.value,
                "candidate_count": self.lodging_candidate_count,
                "assessment_status": lodging_status,
                "issue_codes": list(self.lodging_issue_codes),
                "advice": list(self.lodging_advice),
                "needs_verification": self.lodging_needs_verification,
                "reported_decision_claim_count": (
                    self.lodging_reported_decision_claim_count
                ),
            },
            "tentative_fields": list(self.tentative_fields),
            "needs_verification": list(self.needs_verification),
            "side_effects": {
                "process_local": True,
                "writes_to_trip": False,
                "provider_calls": False,
                "rendered": False,
                "deployed": False,
            },
        }


def _tentative_fields(brief: TripBriefDraft) -> tuple[str, ...]:
    fields: list[str] = []
    if (
        brief.destination is not None
        and brief.destination.state is BriefKnownState.TENTATIVE
    ):
        fields.append("destination")
    if brief.dates.state is BriefKnownState.TENTATIVE:
        fields.append("dates")
    for name, value in (
        ("party", brief.party),
        ("budget", brief.budget),
        ("pace", brief.pace),
    ):
        if value is not None and value.state is BriefKnownState.TENTATIVE:
            fields.append(name)
    if any(item.state is BriefKnownState.TENTATIVE for item in brief.must_do):
        fields.append("must_do")
    if any(item.state is BriefKnownState.TENTATIVE for item in brief.constraints):
        fields.append("constraints")
    return tuple(fields)


def _needs_verification(
    brief: TripBriefDraft,
    assessment: LodgingIntakeAssessment | None,
) -> tuple[str, ...]:
    topics: list[str] = []
    if (
        brief.destination is not None
        and brief.destination.location.precision is not LocationPrecision.EXACT
    ):
        topics.append("destination_location")
    if brief.transport_boundaries:
        topics.append("transport_boundaries")
    if assessment is not None and (
        assessment.needs_verification_candidate_ids
        or assessment.status
        in {
            LodgingIntakeStatus.CONFLICTED,
            LodgingIntakeStatus.AWAITING_CONFIRMATION,
            LodgingIntakeStatus.PARTIAL,
        }
    ):
        topics.append("lodging")
    return tuple(topics)


def assess_guided_draft(brief: TripBriefDraft) -> GuidedDraftReview:
    """Find the one initial blocker, without performing any side effect.

    Destination is requested before dates.  Once both are sufficiently exact,
    the brief is ready for a candidate proposal even if lodging/transport facts
    still need later confirmation or evidence.  Those later states are exposed
    as redacted review topics, not silently promoted or treated as blockers.
    """

    if type(brief) is not TripBriefDraft:
        raise TypeError("brief must be an exact TripBriefDraft")

    assessment = None
    if brief.dates.has_exact_span:
        assert brief.dates.start is not None
        assert brief.dates.end is not None
        assessment = assess_lodging_intake(
            stay_start=brief.dates.start,
            stay_end=brief.dates.end,
            requirement=brief.lodging_requirement,
            candidates=brief.lodging_candidates,
        )

    if brief.destination is None:
        status = GuidedDraftStatus.NEEDS_INPUT
        question: GuidedDraftQuestion | None = GuidedDraftQuestion(
            GuidedQuestionCode.DESTINATION
        )
        next_action = "capture_destination"
    elif not brief.dates.has_exact_span:
        status = GuidedDraftStatus.NEEDS_INPUT
        question = GuidedDraftQuestion(GuidedQuestionCode.DATES)
        next_action = "capture_date_span"
    else:
        status = GuidedDraftStatus.READY_FOR_PROPOSAL
        question = None
        next_action = "prepare_candidate_proposal"

    boundaries = brief.transport_boundaries
    return GuidedDraftReview(
        status=status,
        next_question=question,
        next_action=next_action,
        destination_provided=brief.destination is not None,
        destination_state=(
            brief.destination.state
            if brief.destination is not None
            else BriefKnownState.UNKNOWN
        ),
        destination_precision=(
            brief.destination.location.precision
            if brief.destination is not None
            else None
        ),
        date_state=brief.dates.state,
        date_has_exact_span=brief.dates.has_exact_span,
        overnight_count=brief.dates.overnight_count,
        party=_fact_view(brief.party),
        budget=_fact_view(brief.budget),
        pace=_fact_view(brief.pace),
        must_do=_fact_state_counts(brief.must_do),
        constraints=_fact_state_counts(brief.constraints),
        transport_boundary_count=len(boundaries),
        transport_exact_time_count=sum(
            1 for item in boundaries if item.draft.exact_at is not None
        ),
        transport_window_count=sum(
            1 for item in boundaries if item.draft.exact_at is None
        ),
        transport_reported_decision_claim_count=sum(
            1
            for item in boundaries
            if item.draft.reported_decision is not None
        ),
        lodging_requirement=brief.lodging_requirement,
        lodging_candidate_count=len(brief.lodging_candidates),
        lodging_assessment_status=(
            assessment.status if assessment is not None else None
        ),
        lodging_issue_codes=(
            tuple(sorted({item.code for item in assessment.issues}))
            if assessment is not None
            else ()
        ),
        lodging_advice=assessment.advice if assessment is not None else (),
        lodging_needs_verification=(
            bool(assessment.needs_verification_candidate_ids)
            if assessment is not None
            else False
        ),
        lodging_reported_decision_claim_count=sum(
            1
            for item in brief.lodging_candidates
            if item.draft.reported_decision is not None
        ),
        tentative_fields=_tentative_fields(brief),
        needs_verification=_needs_verification(brief, assessment),
        _token=_REVIEW_TOKEN,
    )
