"""Private candidate-direction review for a future Trip Planner conversation.

This is the small step after :mod:`trip_planner.guided_draft`: a host may put
one to three private direction cards in front of the user without creating a
canonical trip, querying a provider, or pretending that an outline is a
schedule.  Each outline line is only a relative idea slot.  It deliberately
does not model or validate dates, times, durations, routes, place identities,
prices, availability, or selections.  Private free text is not parsed, so it
may still contain an unverified claim about one of those topics; a host must
continue to present every card as an unverified relative idea.

The host owns the raw card text for the active conversation.  The assessor
returns only a redacted aggregate review and a single static subjective-review
prompt.  Raw cards may be shown to the user only in ``REVIEW_REQUIRED``; a
refinement result keeps them private while the host fixes coverage.  A user
response may then be captured as a private direction preference, but it is not
an approval, booking, selection, or write.  The next refinement/apply boundary
is deliberately outside this module.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import InitVar, dataclass, field, fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from .guided_draft import (
    BriefKnownState,
    GuidedDraftQuestion,
    GuidedDraftStatus,
    TripBriefDraft,
    assess_guided_draft,
)
from .models import DecisionState, EvidenceState


GUIDED_PROPOSAL_VERSION = "guided-proposal/v1"
GUIDED_DIRECTION_PREFERENCE_VERSION = "guided-direction-preference/v1"
_MAX_DIRECTION_CARDS = 3
_MAX_OUTLINE_LINES_PER_CARD = 32
_MAX_OUTLINE_SLOTS = 366
_MAX_MUST_DO_REFERENCES = 64
_MAX_TITLE_LENGTH = 256
_MAX_RATIONALE_LENGTH = 512
_CARD_REF_RE = re.compile(r"card-[a-z][a-z0-9-]{0,31}")
_CONTEXT_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
# This only prevents ordinary accidental construction; it is not an authority
# or security boundary.  No downstream write may rely on a guided review.
_REVIEW_TOKEN = object()
_PREFERENCE_TOKEN = object()
_NEXT_ACTIONS = {
    "capture_destination",
    "capture_date_span",
    "refine_required_must_do_coverage",
    "review_candidate_direction",
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
    "proposal_candidates",
}
_REVIEW_PROMPT = (
    "哪個方向較符合你的旅程？可選某一方向、混合，或交給我依你的偏好調整。"
)
_REVIEW_DISCLOSURE = "候選方向尚未確認營業、交通、空位或價格。"
_PREFERENCE_NEXT_ACTION = "refine_private_direction"


def _private_text(value: object, name: str, *, maximum: int) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} must be non-empty bounded text")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError(f"{name} cannot contain control characters")
    return normalized


class ProposalLineSource(str, Enum):
    """Whether an outline idea came from the user or the host's suggestion."""

    USER_STATED = "user_stated"
    AI_SUGGESTED = "ai_suggested"


class GuidedProposalStatus(str, Enum):
    """The current safe state of a direction-card assessment."""

    NEEDS_INPUT = "needs_input"
    NEEDS_REFINEMENT = "needs_refinement"
    REVIEW_REQUIRED = "review_required"


class GuidedDirectionPreferenceKind(str, Enum):
    """An explicit, non-authoritative response to visible direction cards."""

    PREFER_ONE = "prefer_one"
    MIX = "mix"
    REQUEST_REFINEMENT = "request_refinement"


class GuidedDirectionPreferenceStatus(str, Enum):
    """The only safe handoff state after a current direction review."""

    READY_FOR_PRIVATE_REFINEMENT = "ready_for_private_refinement"


def _normalize_preference_card_refs(
    kind: object,
    card_refs: object,
) -> tuple[GuidedDirectionPreferenceKind, tuple[str, ...]]:
    if type(kind) is not GuidedDirectionPreferenceKind:
        raise TypeError("GuidedDirectionPreference.kind must be exact")
    if (
        not isinstance(card_refs, tuple)
        or any(type(item) is not str for item in card_refs)
    ):
        raise TypeError("GuidedDirectionPreference.card_refs must be exact text")
    normalized_card_refs = tuple(
        _private_text(
            item,
            "GuidedDirectionPreference.card_refs",
            maximum=40,
        )
        for item in card_refs
    )
    if any(
        _CARD_REF_RE.fullmatch(item) is None
        for item in normalized_card_refs
    ):
        raise ValueError("GuidedDirectionPreference.card_refs are invalid")
    if len(set(normalized_card_refs)) != len(normalized_card_refs):
        raise ValueError("GuidedDirectionPreference.card_refs cannot repeat")
    if len(normalized_card_refs) > _MAX_DIRECTION_CARDS:
        raise ValueError("GuidedDirectionPreference has too many card refs")
    if kind is GuidedDirectionPreferenceKind.PREFER_ONE:
        if len(normalized_card_refs) != 1:
            raise ValueError("A one-direction preference needs one card ref")
    elif kind is GuidedDirectionPreferenceKind.MIX:
        if not 2 <= len(normalized_card_refs) <= _MAX_DIRECTION_CARDS:
            raise ValueError("A mixed preference needs two or more card refs")
    elif normalized_card_refs:
        raise ValueError("A refinement request cannot name direction cards")
    return kind, tuple(sorted(normalized_card_refs))


@dataclass(frozen=True, slots=True, repr=False)
class GuidedOutlineLine:
    """One private, relative idea slot; explicitly not a scheduled activity."""

    outline_slot: int
    source: ProposalLineSource
    known_state: BriefKnownState
    title: str = field(repr=False)
    rationale: str = field(repr=False)
    must_do_indexes: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if type(self.outline_slot) is not int or not (
            1 <= self.outline_slot <= _MAX_OUTLINE_SLOTS
        ):
            raise ValueError("GuidedOutlineLine.outline_slot is invalid")
        if type(self.source) is not ProposalLineSource:
            raise TypeError("GuidedOutlineLine.source must be exact")
        if type(self.known_state) is not BriefKnownState:
            raise TypeError("GuidedOutlineLine.known_state must be exact")
        if self.source is ProposalLineSource.AI_SUGGESTED:
            if self.known_state is not BriefKnownState.UNKNOWN:
                raise ValueError(
                    "An AI suggestion cannot claim a user-known state"
                )
        elif self.known_state is BriefKnownState.UNKNOWN:
            raise ValueError("A user-stated line cannot be unknown")
        title = _private_text(
            self.title,
            "GuidedOutlineLine.title",
            maximum=_MAX_TITLE_LENGTH,
        )
        rationale = _private_text(
            self.rationale,
            "GuidedOutlineLine.rationale",
            maximum=_MAX_RATIONALE_LENGTH,
        )
        if (
            not isinstance(self.must_do_indexes, tuple)
            or any(
                type(item) is not int
                or not 0 <= item < _MAX_MUST_DO_REFERENCES
                for item in self.must_do_indexes
            )
        ):
            raise ValueError(
                "GuidedOutlineLine.must_do_indexes must be bounded indexes"
            )
        if len(set(self.must_do_indexes)) != len(self.must_do_indexes):
            raise ValueError("GuidedOutlineLine.must_do_indexes cannot repeat")
        if (
            self.source is ProposalLineSource.AI_SUGGESTED
            and self.must_do_indexes
        ):
            raise ValueError(
                "An AI suggestion cannot declare user must-do coverage"
            )
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "rationale", rationale)
        object.__setattr__(
            self,
            "must_do_indexes",
            tuple(sorted(self.must_do_indexes)),
        )

    @property
    def decision_state(self) -> DecisionState:
        """Every direction-line remains a non-authoritative candidate."""

        return DecisionState.CANDIDATE

    @property
    def evidence_state(self) -> EvidenceState:
        """Direction cards contain no identity, route, or hours evidence."""

        return EvidenceState.UNVERIFIED

    @property
    def presentation_source(self) -> str:
        """Safe source badge a host must show beside this private line."""

        return _source_bucket(self)

    def __repr__(self) -> str:
        return (
            "GuidedOutlineLine("
            f"outline_slot={self.outline_slot!r}, "
            f"source={self.source.value!r}, "
            f"known_state={self.known_state.value!r}, "
            f"must_do_count={len(self.must_do_indexes)!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedDirectionCard:
    """One private direction card with only relative outline ideas."""

    card_ref: str
    title: str = field(repr=False)
    rationale: str = field(repr=False)
    lines: tuple[GuidedOutlineLine, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        card_ref = _private_text(
            self.card_ref,
            "GuidedDirectionCard.card_ref",
            maximum=40,
        )
        if _CARD_REF_RE.fullmatch(card_ref) is None:
            raise ValueError("GuidedDirectionCard.card_ref is invalid")
        title = _private_text(
            self.title,
            "GuidedDirectionCard.title",
            maximum=_MAX_TITLE_LENGTH,
        )
        rationale = _private_text(
            self.rationale,
            "GuidedDirectionCard.rationale",
            maximum=_MAX_RATIONALE_LENGTH,
        )
        if (
            not isinstance(self.lines, tuple)
            or not self.lines
            or any(type(item) is not GuidedOutlineLine for item in self.lines)
        ):
            raise TypeError(
                "GuidedDirectionCard.lines must contain exact non-empty values"
            )
        if len(self.lines) > _MAX_OUTLINE_LINES_PER_CARD:
            raise ValueError("GuidedDirectionCard has too many outline lines")
        object.__setattr__(self, "card_ref", card_ref)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "rationale", rationale)

    def __repr__(self) -> str:
        return (
            "GuidedDirectionCard("
            f"outline_line_count={len(self.lines)!r})"
        )


def _source_bucket(line: GuidedOutlineLine) -> str:
    if line.source is ProposalLineSource.AI_SUGGESTED:
        return "ai_candidate"
    if line.known_state is BriefKnownState.TENTATIVE:
        return "tentative"
    return "user_stated"


def _source_counts(
    cards: tuple[GuidedDirectionCard, ...],
) -> tuple[int, int, int]:
    counts = {"user_stated": 0, "tentative": 0, "ai_candidate": 0}
    for line in (line for card in cards for line in card.lines):
        counts[_source_bucket(line)] += 1
    return (
        counts["user_stated"],
        counts["tentative"],
        counts["ai_candidate"],
    )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedProposalReview:
    """Redacted result that is safe to retain as a conversational transcript."""

    status: GuidedProposalStatus
    next_question: GuidedDraftQuestion | None
    next_action: str
    direction_card_count: int
    outline_line_count: int
    outline_slot_count: int
    user_stated_line_count: int
    tentative_line_count: int
    ai_candidate_line_count: int
    required_must_do_count: int
    minimum_declared_must_do_coverage: int
    uncovered_required_must_do_count: int
    tentative_fields: tuple[str, ...] = ()
    needs_verification: tuple[str, ...] = ()
    contract_version: str = GUIDED_PROPOSAL_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError("Guided proposal reviews must be created by the assessor")
        if type(self.status) is not GuidedProposalStatus:
            raise TypeError("GuidedProposalReview.status must be exact")
        if (
            self.next_question is not None
            and type(self.next_question) is not GuidedDraftQuestion
        ):
            raise TypeError("GuidedProposalReview.next_question must be exact")
        if self.next_action not in _NEXT_ACTIONS:
            raise ValueError("GuidedProposalReview.next_action is unsupported")
        if self.status is GuidedProposalStatus.NEEDS_INPUT:
            if self.next_question is None:
                raise ValueError("Needs-input review requires one question")
            if self.direction_card_count != 0 or self.outline_line_count != 0:
                raise ValueError("Needs-input review cannot expose direction cards")
        elif self.next_question is not None:
            raise ValueError("Ready/refinement review cannot carry an input question")
        for name, value in (
            ("direction_card_count", self.direction_card_count),
            ("outline_line_count", self.outline_line_count),
            ("outline_slot_count", self.outline_slot_count),
            ("user_stated_line_count", self.user_stated_line_count),
            ("tentative_line_count", self.tentative_line_count),
            ("ai_candidate_line_count", self.ai_candidate_line_count),
            ("required_must_do_count", self.required_must_do_count),
            (
                "minimum_declared_must_do_coverage",
                self.minimum_declared_must_do_coverage,
            ),
            (
                "uncovered_required_must_do_count",
                self.uncovered_required_must_do_count,
            ),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"GuidedProposalReview.{name} is invalid")
        if self.direction_card_count > _MAX_DIRECTION_CARDS:
            raise ValueError("GuidedProposalReview has too many direction cards")
        if (
            self.user_stated_line_count
            + self.tentative_line_count
            + self.ai_candidate_line_count
            != self.outline_line_count
        ):
            raise ValueError("GuidedProposalReview source counts do not add up")
        if self.minimum_declared_must_do_coverage > self.required_must_do_count:
            raise ValueError("GuidedProposalReview must-do coverage is invalid")
        if (
            self.uncovered_required_must_do_count
            != self.required_must_do_count
            - self.minimum_declared_must_do_coverage
        ):
            raise ValueError("GuidedProposalReview uncovered count is invalid")
        if self.status is GuidedProposalStatus.NEEDS_REFINEMENT:
            if self.uncovered_required_must_do_count == 0:
                raise ValueError("Refinement requires a missing required must-do")
            if self.next_action != "refine_required_must_do_coverage":
                raise ValueError("Refinement review has the wrong next action")
        if self.status is GuidedProposalStatus.REVIEW_REQUIRED:
            if self.direction_card_count == 0:
                raise ValueError("Review-required state needs direction cards")
            if self.uncovered_required_must_do_count:
                raise ValueError("Review-required state cannot hide missing must-dos")
            if self.next_action != "review_candidate_direction":
                raise ValueError("Review-required state has the wrong next action")
        if (
            not isinstance(self.tentative_fields, tuple)
            or any(item not in _TENTATIVE_FIELDS for item in self.tentative_fields)
        ):
            raise ValueError("GuidedProposalReview tentative fields are unsupported")
        if (
            not isinstance(self.needs_verification, tuple)
            or any(item not in _VERIFICATION_TOPICS for item in self.needs_verification)
        ):
            raise ValueError(
                "GuidedProposalReview verification topics are unsupported"
            )
        if self.contract_version != GUIDED_PROPOSAL_VERSION:
            raise ValueError("Unsupported guided proposal contract version")

    def __repr__(self) -> str:
        return (
            "GuidedProposalReview("
            f"status={self.status.value!r}, "
            f"next_action={self.next_action!r}, "
            f"direction_card_count={self.direction_card_count!r}, "
            f"outline_line_count={self.outline_line_count!r})"
        )

    @property
    def review_prompt(self) -> str | None:
        if self.status is GuidedProposalStatus.REVIEW_REQUIRED:
            return _REVIEW_PROMPT
        return None

    @property
    def may_present_direction_cards(self) -> bool:
        """Whether a host may show the private cards to the user now."""

        return self.status is GuidedProposalStatus.REVIEW_REQUIRED

    @property
    def review_disclosure(self) -> str | None:
        """Fixed warning required whenever raw direction cards are shown."""

        if self.may_present_direction_cards:
            return _REVIEW_DISCLOSURE
        return None

    def to_dict(self) -> dict[str, Any]:
        """Return aggregate state only; direction-card text stays private."""

        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_question": (
                self.next_question.to_dict()
                if self.next_question is not None
                else None
            ),
            "next_action": self.next_action,
            "requires_user_response": (
                self.next_question is not None
                or self.status is GuidedProposalStatus.REVIEW_REQUIRED
            ),
            "requires_user_review": (
                self.status is GuidedProposalStatus.REVIEW_REQUIRED
            ),
            "requires_user_decision": (
                self.status is GuidedProposalStatus.REVIEW_REQUIRED
            ),
            "may_present_direction_cards": self.may_present_direction_cards,
            "review_prompt": self.review_prompt,
            "review_disclosure": self.review_disclosure,
            "proposal": {
                "direction_card_count": self.direction_card_count,
                "outline_line_count": self.outline_line_count,
                "outline_slot_count": self.outline_slot_count,
                "line_sources": {
                    "user_stated": self.user_stated_line_count,
                    "tentative": self.tentative_line_count,
                    "ai_candidate": self.ai_candidate_line_count,
                },
                "required_must_do_count": self.required_must_do_count,
                "minimum_declared_must_do_coverage": (
                    self.minimum_declared_must_do_coverage
                ),
                "uncovered_required_must_do_count": (
                    self.uncovered_required_must_do_count
                ),
                "decision_state": (
                    DecisionState.CANDIDATE.value
                    if self.direction_card_count
                    else None
                ),
                "evidence_state": (
                    EvidenceState.UNVERIFIED.value
                    if self.direction_card_count
                    else None
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


def _validate_cards(
    cards: tuple[GuidedDirectionCard, ...],
    *,
    must_do_count: int,
) -> tuple[GuidedDirectionCard, ...]:
    if (
        not isinstance(cards, tuple)
        or not cards
        or any(type(card) is not GuidedDirectionCard for card in cards)
    ):
        raise TypeError(
            "cards must contain one to three exact GuidedDirectionCard values"
        )
    if len(cards) > _MAX_DIRECTION_CARDS:
        raise ValueError("A guided proposal has at most three direction cards")
    card_refs = [card.card_ref for card in cards]
    if len(set(card_refs)) != len(card_refs):
        raise ValueError("Guided direction card references cannot repeat")
    for line in (line for card in cards for line in card.lines):
        if any(index >= must_do_count for index in line.must_do_indexes):
            raise ValueError("Guided outline line references an unknown must-do")
    return cards


def _required_must_do_indexes(brief: TripBriefDraft) -> frozenset[int]:
    return frozenset(
        index
        for index, item in enumerate(brief.must_do)
        if item.state is BriefKnownState.USER_STATED
    )


def _card_declared_coverage(
    card: GuidedDirectionCard,
    required: frozenset[int],
) -> int:
    declared = {
        index
        for line in card.lines
        if (
            line.source is ProposalLineSource.USER_STATED
            and line.known_state is BriefKnownState.USER_STATED
        )
        for index in line.must_do_indexes
    }
    return len(declared & required)


def assess_guided_proposal(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...] = (),
) -> GuidedProposalReview:
    """Assess private direction cards without creating a schedule or write.

    If the brief has an initial blocker, it deliberately returns that one
    existing question and neither validates nor exposes cards.  Once ready,
    every card must declare coverage for all user-stated must-dos before the
    host may ask its one subjective direction question.
    """

    if type(brief) is not TripBriefDraft:
        raise TypeError("brief must be an exact TripBriefDraft")
    draft_review = assess_guided_draft(brief)
    if draft_review.status is GuidedDraftStatus.NEEDS_INPUT:
        assert draft_review.next_question is not None
        return GuidedProposalReview(
            status=GuidedProposalStatus.NEEDS_INPUT,
            next_question=draft_review.next_question,
            next_action=draft_review.next_action,
            direction_card_count=0,
            outline_line_count=0,
            outline_slot_count=0,
            user_stated_line_count=0,
            tentative_line_count=0,
            ai_candidate_line_count=0,
            required_must_do_count=0,
            minimum_declared_must_do_coverage=0,
            uncovered_required_must_do_count=0,
            tentative_fields=draft_review.tentative_fields,
            needs_verification=draft_review.needs_verification,
            _token=_REVIEW_TOKEN,
        )

    validated_cards = _validate_cards(
        cards,
        must_do_count=len(brief.must_do),
    )
    required_indexes = _required_must_do_indexes(brief)
    coverage = tuple(
        _card_declared_coverage(card, required_indexes)
        for card in validated_cards
    )
    minimum_coverage = min(coverage)
    uncovered_count = len(required_indexes) - minimum_coverage
    if uncovered_count:
        status = GuidedProposalStatus.NEEDS_REFINEMENT
        next_action = "refine_required_must_do_coverage"
    else:
        status = GuidedProposalStatus.REVIEW_REQUIRED
        next_action = "review_candidate_direction"
    user_stated_count, tentative_count, ai_candidate_count = _source_counts(
        validated_cards
    )
    outline_line_count = sum(len(card.lines) for card in validated_cards)
    outline_slot_count = len(
        {
            line.outline_slot
            for card in validated_cards
            for line in card.lines
        }
    )
    return GuidedProposalReview(
        status=status,
        next_question=None,
        next_action=next_action,
        direction_card_count=len(validated_cards),
        outline_line_count=outline_line_count,
        outline_slot_count=outline_slot_count,
        user_stated_line_count=user_stated_count,
        tentative_line_count=tentative_count,
        ai_candidate_line_count=ai_candidate_count,
        required_must_do_count=len(required_indexes),
        minimum_declared_must_do_coverage=minimum_coverage,
        uncovered_required_must_do_count=uncovered_count,
        tentative_fields=draft_review.tentative_fields,
        needs_verification=(*draft_review.needs_verification, "proposal_candidates"),
        _token=_REVIEW_TOKEN,
    )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedDirectionPreference:
    """Private host extraction of one clear user direction preference.

    The host must create this only after it has understood the user's answer
    to the current direction-card question.  This model deliberately contains
    no natural-language parser or free-text user response.  Card references
    remain private and only identify the current process-local card set; they
    never confer selection, booking, evidence, or write authority.
    """

    kind: GuidedDirectionPreferenceKind
    card_refs: tuple[str, ...] = field(default=(), repr=False)
    _context_fingerprint: str = field(default="", repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _PREFERENCE_TOKEN:
            raise ValueError(
                "Guided direction preferences must be captured by the host helper"
            )
        kind, card_refs = _normalize_preference_card_refs(
            self.kind,
            self.card_refs,
        )
        if (
            type(self._context_fingerprint) is not str
            or _CONTEXT_FINGERPRINT_RE.fullmatch(self._context_fingerprint) is None
        ):
            raise ValueError(
                "GuidedDirectionPreference context fingerprint is invalid"
            )
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "card_refs", card_refs)

    def __repr__(self) -> str:
        return (
            "GuidedDirectionPreference("
            f"kind={self.kind.value!r}, "
            f"preferred_card_count={len(self.card_refs)!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedDirectionPreferenceReview:
    """Redacted, non-authoritative handoff to private direction refinement."""

    status: GuidedDirectionPreferenceStatus
    preference_kind: GuidedDirectionPreferenceKind
    preferred_card_count: int
    next_action: str
    tentative_fields: tuple[str, ...] = ()
    needs_verification: tuple[str, ...] = ()
    contract_version: str = GUIDED_DIRECTION_PREFERENCE_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError(
                "Guided direction preference reviews must be created by the assessor"
            )
        if type(self.status) is not GuidedDirectionPreferenceStatus:
            raise TypeError("GuidedDirectionPreferenceReview.status must be exact")
        if type(self.preference_kind) is not GuidedDirectionPreferenceKind:
            raise TypeError(
                "GuidedDirectionPreferenceReview.preference_kind must be exact"
            )
        if (
            type(self.preferred_card_count) is not int
            or not 0 <= self.preferred_card_count <= _MAX_DIRECTION_CARDS
        ):
            raise ValueError(
                "GuidedDirectionPreferenceReview preferred card count is invalid"
            )
        expected_count = {
            GuidedDirectionPreferenceKind.PREFER_ONE: 1,
            GuidedDirectionPreferenceKind.REQUEST_REFINEMENT: 0,
        }.get(self.preference_kind)
        if expected_count is not None and self.preferred_card_count != expected_count:
            raise ValueError(
                "GuidedDirectionPreferenceReview preference count conflicts with kind"
            )
        if (
            self.preference_kind is GuidedDirectionPreferenceKind.MIX
            and not 2 <= self.preferred_card_count <= _MAX_DIRECTION_CARDS
        ):
            raise ValueError(
                "GuidedDirectionPreferenceReview mixed count is invalid"
            )
        if self.next_action != _PREFERENCE_NEXT_ACTION:
            raise ValueError(
                "GuidedDirectionPreferenceReview next action is unsupported"
            )
        if (
            not isinstance(self.tentative_fields, tuple)
            or any(item not in _TENTATIVE_FIELDS for item in self.tentative_fields)
        ):
            raise ValueError(
                "GuidedDirectionPreferenceReview tentative fields are unsupported"
            )
        if (
            not isinstance(self.needs_verification, tuple)
            or any(item not in _VERIFICATION_TOPICS for item in self.needs_verification)
        ):
            raise ValueError(
                "GuidedDirectionPreferenceReview verification topics are unsupported"
            )
        if self.contract_version != GUIDED_DIRECTION_PREFERENCE_VERSION:
            raise ValueError("Unsupported guided direction preference contract")

    def __repr__(self) -> str:
        return (
            "GuidedDirectionPreferenceReview("
            f"status={self.status.value!r}, "
            f"preference_kind={self.preference_kind.value!r}, "
            f"preferred_card_count={self.preferred_card_count!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return aggregate preference state without card or user text."""

        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_response": False,
            "requires_user_review": False,
            "requires_user_decision": False,
            "direction_preference": {
                "kind": self.preference_kind.value,
                "preferred_card_count": self.preferred_card_count,
                "has_preferred_direction": self.preferred_card_count > 0,
                "decision_state": (
                    DecisionState.CANDIDATE.value
                    if self.preferred_card_count
                    else None
                ),
                "evidence_state": (
                    EvidenceState.UNVERIFIED.value
                    if self.preferred_card_count
                    else None
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


def _private_context_value(value: object) -> Any:
    """Return canonical private data solely for an in-memory stale guard."""

    if isinstance(value, Enum):
        return {
            "enum": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": value.value,
        }
    if isinstance(value, datetime):
        return {"datetime": value.isoformat()}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if value is None or type(value) in {bool, int, float, str}:
        return value
    if isinstance(value, tuple):
        return [_private_context_value(item) for item in value]
    if isinstance(value, list):
        return [_private_context_value(item) for item in value]
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise TypeError("Private context mapping keys must be text")
        return {
            key: _private_context_value(value[key])
            for key in sorted(value)
        }
    if is_dataclass(value):
        return {
            "dataclass": f"{type(value).__module__}.{type(value).__qualname__}",
            "fields": [
                [item.name, _private_context_value(getattr(value, item.name))]
                for item in fields(value)
            ],
        }
    raise TypeError("Unsupported private guided-preference context value")


def _direction_context_fingerprint(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...],
) -> str:
    """Bind a preference to exact private brief/card contents, never output."""

    canonical = {
        "brief": _private_context_value(brief),
        "cards": [
            _private_context_value(card)
            for card in sorted(cards, key=lambda item: item.card_ref)
        ],
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def capture_guided_direction_preference(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...],
    *,
    kind: GuidedDirectionPreferenceKind,
    card_refs: tuple[str, ...] = (),
) -> GuidedDirectionPreference:
    """Capture a clear response against the exact currently reviewed context.

    The content binding is only an integrity/staleness guard for this private
    conversation.  It grants neither a selection nor any write authority.
    """

    normalized_kind, normalized_card_refs = _normalize_preference_card_refs(
        kind,
        card_refs,
    )
    proposal_review = assess_guided_proposal(brief, cards)
    if proposal_review.status is not GuidedProposalStatus.REVIEW_REQUIRED:
        raise ValueError(
            "A direction preference requires a current review-required proposal"
        )
    current_card_refs = {card.card_ref for card in cards}
    if any(item not in current_card_refs for item in normalized_card_refs):
        raise ValueError(
            "Guided direction preference references a card outside the current proposal"
        )
    return GuidedDirectionPreference(
        kind=normalized_kind,
        card_refs=normalized_card_refs,
        _context_fingerprint=_direction_context_fingerprint(brief, cards),
        _token=_PREFERENCE_TOKEN,
    )


def assess_guided_direction_preference(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...],
    preference: GuidedDirectionPreference,
) -> GuidedDirectionPreferenceReview:
    """Accept one clear current-card preference for private refinement only.

    This deliberately re-evaluates the supplied brief and cards instead of
    trusting a previous review.  A caller must retain cards only for the active
    process-local conversation; if it regenerates them, it must show the new
    cards and capture a new preference rather than replaying an old response.
    """

    if type(preference) is not GuidedDirectionPreference:
        raise TypeError("preference must be an exact GuidedDirectionPreference")
    proposal_review = assess_guided_proposal(brief, cards)
    if proposal_review.status is not GuidedProposalStatus.REVIEW_REQUIRED:
        raise ValueError(
            "A direction preference requires a current review-required proposal"
        )
    if preference._context_fingerprint != _direction_context_fingerprint(
        brief,
        cards,
    ):
        raise ValueError(
            "Guided direction preference does not match the current private context"
        )
    current_card_refs = {card.card_ref for card in cards}
    if any(item not in current_card_refs for item in preference.card_refs):
        raise ValueError(
            "Guided direction preference references a card outside the current proposal"
        )
    return GuidedDirectionPreferenceReview(
        status=GuidedDirectionPreferenceStatus.READY_FOR_PRIVATE_REFINEMENT,
        preference_kind=preference.kind,
        preferred_card_count=len(preference.card_refs),
        next_action=_PREFERENCE_NEXT_ACTION,
        tentative_fields=proposal_review.tentative_fields,
        needs_verification=proposal_review.needs_verification,
        _token=_REVIEW_TOKEN,
    )
