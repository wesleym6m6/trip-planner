"""Phase 5.4 contracts for private guided direction-card review."""

from __future__ import annotations

import inspect
import json
import unittest
from dataclasses import fields, replace
from datetime import date

import trip_planner
import trip_planner.guided_proposal as guided_proposal_module
from trip_planner.guided_draft import (
    BriefKnownState,
    BriefTextFact,
    DateSpanDraft,
    DestinationDraft,
    GuidedQuestionCode,
    TripBriefDraft,
)
from trip_planner.guided_proposal import (
    GuidedDirectionCard,
    GuidedOutlineLine,
    GuidedProposalStatus,
    ProposalLineSource,
    assess_guided_proposal,
)
from trip_planner.lodging import LocationHint, LocationHintKind
from trip_planner.models import DecisionState, EvidenceState


START = date(2026, 10, 12)
END = date(2026, 10, 16)
PRIVATE = "private-title-rationale-url-price-token-place-id-never-serialize"


def _destination(
    *,
    state: BriefKnownState = BriefKnownState.USER_STATED,
) -> DestinationDraft:
    return DestinationDraft(
        location=LocationHint(
            kind=LocationHintKind.AREA,
            label="Synthetic private destination " + PRIVATE,
            input_text="Synthetic private destination " + PRIVATE,
            country_code="jp",
        ),
        state=state,
    )


def _ready_brief(
    *,
    destination_state: BriefKnownState = BriefKnownState.USER_STATED,
    date_state: BriefKnownState = BriefKnownState.USER_STATED,
) -> TripBriefDraft:
    return TripBriefDraft(
        destination=_destination(state=destination_state),
        dates=DateSpanDraft(state=date_state, start=START, end=END),
        pace=BriefTextFact(
            state=BriefKnownState.TENTATIVE,
            value="Private slow pace",
        ),
        must_do=(
            BriefTextFact(
                state=BriefKnownState.USER_STATED,
                value="Private user must-do",
            ),
            BriefTextFact(
                state=BriefKnownState.TENTATIVE,
                value="Private tentative must-do",
            ),
        ),
    )


def _line(
    *,
    slot: int,
    source: ProposalLineSource = ProposalLineSource.AI_SUGGESTED,
    known_state: BriefKnownState = BriefKnownState.UNKNOWN,
    must_do_indexes: tuple[int, ...] = (),
    title: str = "Private line title " + PRIVATE,
    rationale: str = "Private line rationale " + PRIVATE,
) -> GuidedOutlineLine:
    return GuidedOutlineLine(
        outline_slot=slot,
        source=source,
        known_state=known_state,
        title=title,
        rationale=rationale,
        must_do_indexes=must_do_indexes,
    )


def _cards(
    *,
    second_covers_required: bool = True,
) -> tuple[GuidedDirectionCard, GuidedDirectionCard]:
    first = GuidedDirectionCard(
        card_ref="card-a",
        title="Private relaxed islands " + PRIVATE,
        rationale="Private rationale A " + PRIVATE,
        lines=(
            _line(
                slot=1,
                source=ProposalLineSource.USER_STATED,
                known_state=BriefKnownState.USER_STATED,
                must_do_indexes=(0,),
            ),
            _line(slot=2),
        ),
    )
    second = GuidedDirectionCard(
        card_ref="card-b",
        title="Private mixed coast " + PRIVATE,
        rationale="Private rationale B " + PRIVATE,
        lines=(
            _line(
                slot=1,
                source=ProposalLineSource.USER_STATED,
                known_state=BriefKnownState.USER_STATED,
                must_do_indexes=(0,) if second_covers_required else (),
            ),
            _line(
                slot=2,
                source=ProposalLineSource.USER_STATED,
                known_state=BriefKnownState.TENTATIVE,
                must_do_indexes=(1,),
            ),
            _line(slot=3),
        ),
    )
    return first, second


class GuidedProposalTests(unittest.TestCase):
    def test_public_api_is_pure_and_has_no_selection_or_apply_path(self) -> None:
        for name in (
            "GuidedDirectionCard",
            "GuidedOutlineLine",
            "GuidedProposalReview",
            "assess_guided_proposal",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        for unsupported_name in (
            "choose_guided_direction",
            "select_guided_direction",
            "apply_guided_proposal",
            "guided_proposal_cli",
            "rank_guided_proposals",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        source = inspect.getsource(guided_proposal_module)
        for forbidden_import in (
            "from .tripctl",
            "from .store",
            "from .repair",
            "from .scheduling",
            "import subprocess",
            "import pathlib",
        ):
            self.assertNotIn(forbidden_import, source)

    def test_unready_brief_mirrors_existing_single_blocker_and_hides_cards(self) -> None:
        review = assess_guided_proposal(TripBriefDraft(), _cards())
        safe = review.to_dict()

        self.assertEqual(GuidedProposalStatus.NEEDS_INPUT, review.status)
        self.assertEqual(GuidedQuestionCode.DESTINATION, review.next_question.code)
        self.assertEqual(0, safe["proposal"]["direction_card_count"])
        self.assertFalse(safe["requires_user_review"])
        self.assertNotIn(PRIVATE, json.dumps(safe, ensure_ascii=False))

    def test_ready_cards_require_one_subjective_review_without_side_effects(self) -> None:
        cards = _cards()
        review = assess_guided_proposal(_ready_brief(), cards)
        safe = review.to_dict()

        self.assertEqual(GuidedProposalStatus.REVIEW_REQUIRED, review.status)
        self.assertIsNone(review.next_question)
        self.assertTrue(safe["requires_user_review"])
        self.assertTrue(safe["requires_user_response"])
        self.assertTrue(safe["requires_user_decision"])
        self.assertTrue(safe["may_present_direction_cards"])
        self.assertEqual("review_candidate_direction", safe["next_action"])
        self.assertEqual(review.review_prompt, safe["review_prompt"])
        self.assertEqual(review.review_disclosure, safe["review_disclosure"])
        self.assertEqual("候選方向尚未確認營業、交通、空位或價格。", review.review_disclosure)
        self.assertEqual(2, safe["proposal"]["direction_card_count"])
        self.assertEqual(5, safe["proposal"]["outline_line_count"])
        self.assertEqual(3, safe["proposal"]["outline_slot_count"])
        self.assertEqual(
            {"user_stated": 2, "tentative": 1, "ai_candidate": 2},
            safe["proposal"]["line_sources"],
        )
        self.assertEqual(1, safe["proposal"]["required_must_do_count"])
        self.assertEqual(1, safe["proposal"]["minimum_declared_must_do_coverage"])
        self.assertEqual(0, safe["proposal"]["uncovered_required_must_do_count"])
        self.assertEqual("candidate", safe["proposal"]["decision_state"])
        self.assertEqual("unverified", safe["proposal"]["evidence_state"])
        self.assertEqual(
            {
                "process_local": True,
                "writes_to_trip": False,
                "provider_calls": False,
                "rendered": False,
                "deployed": False,
            },
            safe["side_effects"],
        )

        for card in cards:
            for line in card.lines:
                self.assertEqual(DecisionState.CANDIDATE, line.decision_state)
                self.assertEqual(EvidenceState.UNVERIFIED, line.evidence_state)
        self.assertEqual("user_stated", cards[0].lines[0].presentation_source)
        self.assertEqual("tentative", cards[1].lines[1].presentation_source)
        self.assertEqual("ai_candidate", cards[0].lines[1].presentation_source)

    def test_missing_user_stated_must_do_requires_agent_refinement_not_user_choice(self) -> None:
        review = assess_guided_proposal(
            _ready_brief(),
            _cards(second_covers_required=False),
        )
        safe = review.to_dict()

        self.assertEqual(GuidedProposalStatus.NEEDS_REFINEMENT, review.status)
        self.assertFalse(safe["requires_user_review"])
        self.assertFalse(safe["requires_user_response"])
        self.assertFalse(safe["requires_user_decision"])
        self.assertFalse(safe["may_present_direction_cards"])
        self.assertIsNone(safe["review_prompt"])
        self.assertIsNone(safe["review_disclosure"])
        self.assertEqual(
            "refine_required_must_do_coverage",
            safe["next_action"],
        )
        self.assertEqual(1, safe["proposal"]["uncovered_required_must_do_count"])

    def test_tentative_line_cannot_cover_a_user_stated_must_do(self) -> None:
        tentative_claim = GuidedDirectionCard(
            card_ref="card-tentative",
            title="Private tentative coverage",
            rationale="Private tentative coverage",
            lines=(
                _line(
                    slot=1,
                    source=ProposalLineSource.USER_STATED,
                    known_state=BriefKnownState.TENTATIVE,
                    must_do_indexes=(0,),
                ),
            ),
        )

        review = assess_guided_proposal(_ready_brief(), (tentative_claim,))

        self.assertEqual(GuidedProposalStatus.NEEDS_REFINEMENT, review.status)
        self.assertEqual(0, review.minimum_declared_must_do_coverage)
        self.assertEqual(1, review.uncovered_required_must_do_count)
        self.assertFalse(review.may_present_direction_cards)

    def test_tentative_and_needs_verification_are_projected_not_promoted(self) -> None:
        review = assess_guided_proposal(
            _ready_brief(
                destination_state=BriefKnownState.TENTATIVE,
                date_state=BriefKnownState.TENTATIVE,
            ),
            _cards(),
        )
        safe = review.to_dict()

        self.assertEqual(
            ["destination", "dates", "pace", "must_do"],
            safe["tentative_fields"],
        )
        self.assertEqual(
            ["destination_location", "proposal_candidates"],
            safe["needs_verification"],
        )
        self.assertEqual("candidate", safe["proposal"]["decision_state"])
        self.assertEqual("unverified", safe["proposal"]["evidence_state"])

    def test_outline_is_relative_only_and_private_content_is_redacted(self) -> None:
        field_names = {item.name for item in fields(GuidedOutlineLine)}
        for forbidden_name in (
            "date",
            "time",
            "duration",
            "location_id",
            "place_id",
            "price",
            "route",
            "availability",
        ):
            self.assertNotIn(forbidden_name, field_names)

        card = GuidedDirectionCard(
            card_ref="card-private-secret",
            title=PRIVATE,
            rationale=PRIVATE,
            lines=(
                _line(
                    slot=1,
                    source=ProposalLineSource.USER_STATED,
                    known_state=BriefKnownState.USER_STATED,
                    title=PRIVATE,
                    rationale=PRIVATE,
                    must_do_indexes=(0,),
                ),
            ),
        )
        review = assess_guided_proposal(_ready_brief(), (card,))
        rendered = "\n".join(
            (
                repr(card),
                repr(card.lines[0]),
                repr(review),
                json.dumps(review.to_dict(), ensure_ascii=False),
            )
        )

        for private_value in (
            PRIVATE,
            "card-private-secret",
            "2026-10-12",
            "Private user must-do",
        ):
            self.assertNotIn(private_value, rendered)

    def test_card_bounds_indexes_and_source_state_fail_closed(self) -> None:
        brief = _ready_brief()
        with self.assertRaises(TypeError):
            assess_guided_proposal(brief)
        with self.assertRaises(ValueError):
            assess_guided_proposal(brief, _cards() + _cards())
        duplicate = GuidedDirectionCard(
            card_ref="card-a",
            title="Private duplicate",
            rationale="Private duplicate",
            lines=(
                _line(
                    slot=1,
                    source=ProposalLineSource.USER_STATED,
                    known_state=BriefKnownState.USER_STATED,
                    must_do_indexes=(0,),
                ),
            ),
        )
        with self.assertRaises(ValueError):
            assess_guided_proposal(brief, (_cards()[0], duplicate))
        with self.assertRaises(ValueError):
            _line(slot=367)
        extended_outline = GuidedDirectionCard(
            card_ref="card-c",
            title="Private relative fifth slot",
            rationale="Private relative fifth slot",
            lines=(
                _line(
                    slot=5,
                    source=ProposalLineSource.USER_STATED,
                    known_state=BriefKnownState.USER_STATED,
                    must_do_indexes=(0,),
                ),
            ),
        )
        self.assertEqual(
            GuidedProposalStatus.REVIEW_REQUIRED,
            assess_guided_proposal(brief, (extended_outline,)).status,
        )
        with self.assertRaises(ValueError):
            assess_guided_proposal(
                brief,
                (
                    GuidedDirectionCard(
                        card_ref="card-d",
                        title="Private bad must-do",
                        rationale="Private bad must-do",
                        lines=(
                            _line(
                                slot=1,
                                source=ProposalLineSource.USER_STATED,
                                known_state=BriefKnownState.USER_STATED,
                                must_do_indexes=(2,),
                            ),
                        ),
                    ),
                ),
            )
        with self.assertRaises(ValueError):
            _line(slot=1, must_do_indexes=(0,))
        with self.assertRaises(ValueError):
            GuidedOutlineLine(
                outline_slot=1,
                source=ProposalLineSource.AI_SUGGESTED,
                known_state=BriefKnownState.USER_STATED,
                title="Private bad source",
                rationale="Private bad source",
            )

    def test_safe_projection_is_order_independent(self) -> None:
        cards = _cards()
        forward = assess_guided_proposal(_ready_brief(), cards).to_dict()
        reverse = assess_guided_proposal(
            _ready_brief(),
            tuple(reversed(cards)),
        ).to_dict()

        self.assertEqual(forward, reverse)

    def test_ordinary_review_replace_is_rejected_without_claiming_authority(self) -> None:
        review = assess_guided_proposal(_ready_brief(), _cards())

        with self.assertRaises(ValueError):
            replace(
                review,
                status=GuidedProposalStatus.NEEDS_REFINEMENT,
                next_action="refine_required_must_do_coverage",
                uncovered_required_must_do_count=1,
                minimum_declared_must_do_coverage=0,
            )
        with self.assertRaises(ValueError):
            replace(review, tentative_fields=(PRIVATE,))
