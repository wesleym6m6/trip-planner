"""Private, source-preserving refinement of one guided trip direction.

Phase 5.6 consumes the exact process-local brief, direction cards, and typed
preference produced by the preceding guided flow.  It lets a host synthesize
one new candidate direction while proving that selected source material and
user-stated lines were not silently dropped.  The result remains an
unverified candidate and must be shown to the user for one more subjective
review.  This module has no provider, persistence, scheduling, rendering, or
canonical mutation path.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import InitVar, dataclass, field
from enum import Enum
from typing import Any

from .guided_draft import TripBriefDraft
from .guided_proposal import (
    GuidedDirectionCard,
    GuidedDirectionPreference,
    GuidedDirectionPreferenceKind,
    GuidedOutlineLine,
    GuidedProposalStatus,
    ProposalLineSource,
    assess_guided_direction_preference,
    assess_guided_proposal,
)
from .models import DecisionState, EvidenceState


GUIDED_REFINEMENT_VERSION = "guided-refinement/v1"
_CARD_REF_RE = re.compile(r"card-[a-z][a-z0-9-]{0,31}")
_MAX_SOURCE_LINE_INDEX = 31
_MAX_RETAINED_SOURCE_LINES = 96
_REVIEW_TOKEN = object()
_REFINE_ACTION = "refine_private_direction"
_REVIEW_ACTION = "review_refined_direction"
_REVIEW_PROMPT = (
    "這個整合後方向是否符合你的想法？可以確認方向，或指出要調整的地方。"
)
_REVIEW_DISCLOSURE = "整合後方向尚未確認營業、交通、空位或價格。"
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


class GuidedRefinementStatus(str, Enum):
    """Whether the private synthesis needs repair or user review."""

    NEEDS_REFINEMENT = "needs_refinement"
    REVIEW_REQUIRED = "review_required"


class GuidedRefinementProblemCode(str, Enum):
    """Safe aggregate reasons why a synthesis cannot yet be shown."""

    PREFERRED_CARD_NOT_RETAINED = "preferred_card_not_retained"
    PREFERRED_LINE_NOT_RETAINED = "preferred_line_not_retained"
    USER_STATED_LINE_NOT_RETAINED = "user_stated_line_not_retained"
    UNPREFERRED_SOURCE_INCLUDED = "unpreferred_source_included"
    RETAINED_SOURCE_LINE_NOT_CARRIED = "retained_source_line_not_carried"
    REQUIRED_MUST_DO_COVERAGE_INCOMPLETE = (
        "required_must_do_coverage_incomplete"
    )


def _private_card_ref(value: object) -> str:
    if type(value) is not str:
        raise TypeError("GuidedSourceLineRef.card_ref must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if _CARD_REF_RE.fullmatch(normalized) is None:
        raise ValueError("GuidedSourceLineRef.card_ref is invalid")
    return normalized


@dataclass(frozen=True, slots=True, repr=False)
class GuidedSourceLineRef:
    """Private pointer to one line in the exact current direction-card set."""

    card_ref: str = field(repr=False)
    line_index: int = field(repr=False)

    def __post_init__(self) -> None:
        card_ref = _private_card_ref(self.card_ref)
        if type(self.line_index) is not int or not (
            0 <= self.line_index <= _MAX_SOURCE_LINE_INDEX
        ):
            raise ValueError("GuidedSourceLineRef.line_index is invalid")
        object.__setattr__(self, "card_ref", card_ref)

    def __repr__(self) -> str:
        return "GuidedSourceLineRef()"


@dataclass(frozen=True, slots=True, repr=False)
class GuidedRefinementCandidate:
    """One private candidate direction plus its exact source-line carryover."""

    direction: GuidedDirectionCard = field(repr=False)
    retained_source_lines: tuple[GuidedSourceLineRef, ...] = field(
        default=(),
        repr=False,
    )

    def __post_init__(self) -> None:
        if type(self.direction) is not GuidedDirectionCard:
            raise TypeError(
                "GuidedRefinementCandidate.direction must be exact"
            )
        if (
            not isinstance(self.retained_source_lines, tuple)
            or any(
                type(item) is not GuidedSourceLineRef
                for item in self.retained_source_lines
            )
        ):
            raise TypeError(
                "GuidedRefinementCandidate retained sources must be exact"
            )
        if len(self.retained_source_lines) > _MAX_RETAINED_SOURCE_LINES:
            raise ValueError(
                "GuidedRefinementCandidate has too many retained sources"
            )
        identities = tuple(
            (item.card_ref, item.line_index)
            for item in self.retained_source_lines
        )
        if len(set(identities)) != len(identities):
            raise ValueError(
                "GuidedRefinementCandidate retained sources cannot repeat"
            )
        object.__setattr__(
            self,
            "retained_source_lines",
            tuple(
                sorted(
                    self.retained_source_lines,
                    key=lambda item: (item.card_ref, item.line_index),
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
            "GuidedRefinementCandidate("
            f"retained_source_line_count={len(self.retained_source_lines)!r}, "
            f"refined_outline_line_count={len(self.direction.lines)!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GuidedRefinementReview:
    """Redacted assessment of a private, non-authoritative refinement."""

    status: GuidedRefinementStatus
    next_action: str
    preference_kind: GuidedDirectionPreferenceKind
    selected_source_card_count: int
    retained_source_card_count: int
    retained_source_line_count: int
    refined_outline_line_count: int
    required_must_do_count: int
    minimum_declared_must_do_coverage: int
    uncovered_required_must_do_count: int
    problem_codes: tuple[GuidedRefinementProblemCode, ...] = ()
    tentative_fields: tuple[str, ...] = ()
    needs_verification: tuple[str, ...] = ()
    contract_version: str = GUIDED_REFINEMENT_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError(
                "Guided refinement reviews must be created by the assessor"
            )
        if type(self.status) is not GuidedRefinementStatus:
            raise TypeError("GuidedRefinementReview.status must be exact")
        if type(self.preference_kind) is not GuidedDirectionPreferenceKind:
            raise TypeError(
                "GuidedRefinementReview.preference_kind must be exact"
            )
        for name, value, maximum in (
            ("selected_source_card_count", self.selected_source_card_count, 3),
            ("retained_source_card_count", self.retained_source_card_count, 3),
            (
                "retained_source_line_count",
                self.retained_source_line_count,
                _MAX_RETAINED_SOURCE_LINES,
            ),
            ("refined_outline_line_count", self.refined_outline_line_count, 32),
            ("required_must_do_count", self.required_must_do_count, 64),
            (
                "minimum_declared_must_do_coverage",
                self.minimum_declared_must_do_coverage,
                64,
            ),
            (
                "uncovered_required_must_do_count",
                self.uncovered_required_must_do_count,
                64,
            ),
        ):
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError(f"GuidedRefinementReview.{name} is invalid")
        expected_selected_count = {
            GuidedDirectionPreferenceKind.PREFER_ONE: 1,
            GuidedDirectionPreferenceKind.REQUEST_REFINEMENT: 0,
        }.get(self.preference_kind)
        if (
            expected_selected_count is not None
            and self.selected_source_card_count != expected_selected_count
        ):
            raise ValueError(
                "GuidedRefinementReview selected source count conflicts with kind"
            )
        if (
            self.preference_kind is GuidedDirectionPreferenceKind.MIX
            and not 2 <= self.selected_source_card_count <= 3
        ):
            raise ValueError(
                "GuidedRefinementReview mixed source count is invalid"
            )
        if (
            self.minimum_declared_must_do_coverage
            > self.required_must_do_count
            or self.uncovered_required_must_do_count
            != self.required_must_do_count
            - self.minimum_declared_must_do_coverage
        ):
            raise ValueError(
                "GuidedRefinementReview must-do coverage is invalid"
            )
        if (
            not isinstance(self.problem_codes, tuple)
            or any(
                type(item) is not GuidedRefinementProblemCode
                for item in self.problem_codes
            )
            or tuple(sorted(set(self.problem_codes), key=lambda item: item.value))
            != self.problem_codes
        ):
            raise ValueError(
                "GuidedRefinementReview problem codes are invalid"
            )
        if self.status is GuidedRefinementStatus.NEEDS_REFINEMENT:
            if not self.problem_codes or self.next_action != _REFINE_ACTION:
                raise ValueError(
                    "Needs-refinement review requires problems and repair action"
                )
        elif self.problem_codes or self.next_action != _REVIEW_ACTION:
            raise ValueError(
                "Review-required refinement cannot carry repair problems"
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
                "GuidedRefinementReview safe topic collections are invalid"
            )
        if self.contract_version != GUIDED_REFINEMENT_VERSION:
            raise ValueError("Unsupported guided refinement contract version")

    @property
    def may_present_refined_direction(self) -> bool:
        return self.status is GuidedRefinementStatus.REVIEW_REQUIRED

    @property
    def review_prompt(self) -> str | None:
        return _REVIEW_PROMPT if self.may_present_refined_direction else None

    @property
    def review_disclosure(self) -> str | None:
        return _REVIEW_DISCLOSURE if self.may_present_refined_direction else None

    def __repr__(self) -> str:
        return (
            "GuidedRefinementReview("
            f"status={self.status.value!r}, "
            f"next_action={self.next_action!r}, "
            f"problem_count={len(self.problem_codes)!r}, "
            f"refined_outline_line_count={self.refined_outline_line_count!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return only aggregate state; all card text and refs stay private."""

        requires_review = self.may_present_refined_direction
        return {
            "contract_version": self.contract_version,
            "status": self.status.value,
            "next_action": self.next_action,
            "requires_user_response": requires_review,
            "requires_user_review": requires_review,
            "requires_user_decision": requires_review,
            "may_present_refined_direction": requires_review,
            "review_prompt": self.review_prompt,
            "review_disclosure": self.review_disclosure,
            "refinement": {
                "preference_kind": self.preference_kind.value,
                "selected_source_card_count": self.selected_source_card_count,
                "retained_source_card_count": self.retained_source_card_count,
                "retained_source_line_count": self.retained_source_line_count,
                "refined_outline_line_count": self.refined_outline_line_count,
                "required_must_do_count": self.required_must_do_count,
                "minimum_declared_must_do_coverage": (
                    self.minimum_declared_must_do_coverage
                ),
                "uncovered_required_must_do_count": (
                    self.uncovered_required_must_do_count
                ),
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
                "rendered": False,
                "deployed": False,
            },
        }


def _line_signature(line: GuidedOutlineLine) -> tuple[object, ...]:
    """Compare source carryover while allowing relative-slot reordering."""

    return (
        line.source,
        line.known_state,
        line.title,
        line.rationale,
        line.must_do_indexes,
    )


def _resolve_source_lines(
    cards_by_ref: dict[str, GuidedDirectionCard],
    refs: tuple[GuidedSourceLineRef, ...],
) -> tuple[tuple[GuidedSourceLineRef, GuidedOutlineLine], ...]:
    resolved: list[tuple[GuidedSourceLineRef, GuidedOutlineLine]] = []
    for source_ref in refs:
        card = cards_by_ref.get(source_ref.card_ref)
        if card is None:
            raise ValueError(
                "Guided refinement references a card outside the current proposal"
            )
        if source_ref.line_index >= len(card.lines):
            raise ValueError(
                "Guided refinement references a line outside the current proposal"
            )
        resolved.append((source_ref, card.lines[source_ref.line_index]))
    return tuple(resolved)


def assess_guided_refinement(
    brief: TripBriefDraft,
    cards: tuple[GuidedDirectionCard, ...],
    preference: GuidedDirectionPreference,
    candidate: GuidedRefinementCandidate,
) -> GuidedRefinementReview:
    """Assess one exact-context private synthesis before user presentation.

    Malformed or stale references fail closed.  A well-formed synthesis that
    drops selected/user-stated material returns a redacted repair result; only
    a source-preserving candidate reaches the fixed user-review question.
    """

    if type(candidate) is not GuidedRefinementCandidate:
        raise TypeError("candidate must be an exact GuidedRefinementCandidate")
    preference_review = assess_guided_direction_preference(
        brief,
        cards,
        preference,
    )
    cards_by_ref = {card.card_ref: card for card in cards}
    if candidate.direction.card_ref in cards_by_ref:
        raise ValueError(
            "A refined direction must use a fresh process-local card reference"
        )
    resolved = _resolve_source_lines(
        cards_by_ref,
        candidate.retained_source_lines,
    )
    retained_identities = {
        (source_ref.card_ref, source_ref.line_index)
        for source_ref, _ in resolved
    }
    retained_card_refs = {source_ref.card_ref for source_ref, _ in resolved}
    selected_card_refs = set(preference.card_refs)
    problems: set[GuidedRefinementProblemCode] = set()

    if preference.kind is GuidedDirectionPreferenceKind.PREFER_ONE:
        if retained_card_refs.difference(selected_card_refs):
            problems.add(
                GuidedRefinementProblemCode.UNPREFERRED_SOURCE_INCLUDED
            )
        selected_ref = preference.card_refs[0]
        expected = {
            (selected_ref, line_index)
            for line_index in range(len(cards_by_ref[selected_ref].lines))
        }
        if not expected.issubset(retained_identities):
            problems.add(
                GuidedRefinementProblemCode.PREFERRED_LINE_NOT_RETAINED
            )
    elif preference.kind is GuidedDirectionPreferenceKind.MIX:
        if retained_card_refs.difference(selected_card_refs):
            problems.add(
                GuidedRefinementProblemCode.UNPREFERRED_SOURCE_INCLUDED
            )
        missing_direction_source = False
        for selected_ref in selected_card_refs:
            selected_card = cards_by_ref[selected_ref]
            direction_line_indexes = {
                line_index
                for line_index, line in enumerate(selected_card.lines)
                if line.source is ProposalLineSource.AI_SUGGESTED
            }
            if not direction_line_indexes:
                direction_line_indexes = set(range(len(selected_card.lines)))
            if not any(
                (selected_ref, line_index) in retained_identities
                for line_index in direction_line_indexes
            ):
                missing_direction_source = True
        if missing_direction_source:
            problems.add(
                GuidedRefinementProblemCode.PREFERRED_CARD_NOT_RETAINED
            )

    if preference.kind is not GuidedDirectionPreferenceKind.REQUEST_REFINEMENT:
        selected_signatures = {
            _line_signature(line)
            for card_ref in selected_card_refs
            for line in cards_by_ref[card_ref].lines
        }
        unpreferred_only_signatures = {
            _line_signature(line)
            for card_ref, card in cards_by_ref.items()
            if card_ref not in selected_card_refs
            for line in card.lines
        }.difference(selected_signatures)
        if any(
            _line_signature(line) in unpreferred_only_signatures
            for line in candidate.direction.lines
        ):
            problems.add(
                GuidedRefinementProblemCode.UNPREFERRED_SOURCE_INCLUDED
            )

    user_source_card_refs = (
        selected_card_refs
        if preference.kind is not GuidedDirectionPreferenceKind.REQUEST_REFINEMENT
        else set(cards_by_ref)
    )
    expected_user_stated = {
        (card_ref, line_index)
        for card_ref in user_source_card_refs
        for line_index, line in enumerate(cards_by_ref[card_ref].lines)
        if line.source is ProposalLineSource.USER_STATED
    }
    if not expected_user_stated.issubset(retained_identities):
        problems.add(
            GuidedRefinementProblemCode.USER_STATED_LINE_NOT_RETAINED
        )

    retained_signature_counts = Counter(
        _line_signature(source_line) for _, source_line in resolved
    )
    refined_signature_counts = Counter(
        _line_signature(line) for line in candidate.direction.lines
    )
    if retained_signature_counts - refined_signature_counts:
        problems.add(
            GuidedRefinementProblemCode.RETAINED_SOURCE_LINE_NOT_CARRIED
        )

    proposal_review = assess_guided_proposal(brief, (candidate.direction,))
    if proposal_review.status is GuidedProposalStatus.NEEDS_REFINEMENT:
        problems.add(
            GuidedRefinementProblemCode.REQUIRED_MUST_DO_COVERAGE_INCOMPLETE
        )
    elif proposal_review.status is not GuidedProposalStatus.REVIEW_REQUIRED:
        raise ValueError(
            "A guided refinement requires a complete current trip brief"
        )

    problem_codes = tuple(sorted(problems, key=lambda item: item.value))
    status = (
        GuidedRefinementStatus.NEEDS_REFINEMENT
        if problem_codes
        else GuidedRefinementStatus.REVIEW_REQUIRED
    )
    return GuidedRefinementReview(
        status=status,
        next_action=(
            _REFINE_ACTION
            if status is GuidedRefinementStatus.NEEDS_REFINEMENT
            else _REVIEW_ACTION
        ),
        preference_kind=preference_review.preference_kind,
        selected_source_card_count=len(preference.card_refs),
        retained_source_card_count=len(retained_card_refs),
        retained_source_line_count=len(resolved),
        refined_outline_line_count=proposal_review.outline_line_count,
        required_must_do_count=proposal_review.required_must_do_count,
        minimum_declared_must_do_coverage=(
            proposal_review.minimum_declared_must_do_coverage
        ),
        uncovered_required_must_do_count=(
            proposal_review.uncovered_required_must_do_count
        ),
        problem_codes=problem_codes,
        tentative_fields=proposal_review.tentative_fields,
        needs_verification=proposal_review.needs_verification,
        _token=_REVIEW_TOKEN,
    )


__all__ = [
    "GUIDED_REFINEMENT_VERSION",
    "GuidedRefinementCandidate",
    "GuidedRefinementProblemCode",
    "GuidedRefinementReview",
    "GuidedRefinementStatus",
    "GuidedSourceLineRef",
    "assess_guided_refinement",
]
