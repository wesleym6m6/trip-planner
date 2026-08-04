"""Phase 5.7 contracts for exact refined-direction response handoff."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

import trip_planner
from tests.test_phase56_guided_refinement import (
    PRIVATE,
    _brief,
    _candidate_from_sources,
    _cards,
    _preference,
)
from trip_planner.guided_draft import BriefKnownState, BriefTextFact
from trip_planner.guided_proposal import (
    GuidedDirectionPreferenceKind,
    GuidedOutlineLine,
    ProposalLineSource,
)
from trip_planner.guided_refinement import (
    GuidedRefinementResponse,
    GuidedRefinementResponseKind,
    GuidedRefinementResponseStatus,
    assess_guided_refinement_response,
    capture_guided_refinement_response,
)


def _ready_context():
    brief = _brief()
    cards = _cards()
    preference = _preference(
        brief,
        cards,
        GuidedDirectionPreferenceKind.PREFER_ONE,
        ("card-a",),
    )
    candidate = _candidate_from_sources(
        cards,
        (("card-a", 0), ("card-a", 1)),
    )
    return brief, cards, preference, candidate


def _capture(kind: GuidedRefinementResponseKind):
    brief, cards, preference, candidate = _ready_context()
    response = capture_guided_refinement_response(
        brief,
        cards,
        preference,
        candidate,
        kind=kind,
    )
    return brief, cards, preference, candidate, response


class GuidedRefinementResponseTests(unittest.TestCase):
    def test_accept_direction_hands_off_only_to_private_itinerary_candidate(self) -> None:
        brief, cards, preference, candidate, response = _capture(
            GuidedRefinementResponseKind.ACCEPT_DIRECTION
        )
        review = assess_guided_refinement_response(
            brief,
            cards,
            preference,
            candidate,
            response,
        )
        safe = review.to_dict()

        self.assertEqual(
            GuidedRefinementResponseStatus.READY_FOR_PRIVATE_ITINERARY_CANDIDATE,
            review.status,
        )
        self.assertEqual(
            "prepare_private_itinerary_candidate",
            safe["next_action"],
        )
        self.assertFalse(safe["requires_user_response"])
        self.assertFalse(safe["requires_user_review"])
        self.assertFalse(safe["requires_user_decision"])
        self.assertEqual(
            {
                "kind": "accept_direction",
                "preference_kind": "prefer_one",
                "accepted_for_private_itinerary_candidate": True,
                "refined_outline_line_count": 2,
                "decision_state": "candidate",
                "evidence_state": "unverified",
                "supports_authoritative_use": False,
            },
            safe["direction_response"],
        )
        self.assertEqual(
            {
                "process_local": True,
                "writes_to_trip": False,
                "provider_calls": False,
                "scheduled": False,
                "trip_created": False,
                "rendered": False,
                "deployed": False,
            },
            safe["side_effects"],
        )

    def test_request_adjustment_returns_to_private_refinement(self) -> None:
        brief, cards, preference, candidate, response = _capture(
            GuidedRefinementResponseKind.REQUEST_ADJUSTMENT
        )
        safe = assess_guided_refinement_response(
            brief,
            cards,
            preference,
            candidate,
            response,
        ).to_dict()

        self.assertEqual("ready_for_private_refinement", safe["status"])
        self.assertEqual("refine_private_direction", safe["next_action"])
        self.assertFalse(
            safe["direction_response"][
                "accepted_for_private_itinerary_candidate"
            ]
        )
        self.assertEqual("candidate", safe["direction_response"]["decision_state"])
        self.assertEqual("unverified", safe["direction_response"]["evidence_state"])
        self.assertEqual(
            {
                "process_local": True,
                "writes_to_trip": False,
                "provider_calls": False,
                "scheduled": False,
                "trip_created": False,
                "rendered": False,
                "deployed": False,
            },
            safe["side_effects"],
        )

    def test_capture_requires_a_current_visible_refinement_review(self) -> None:
        brief, cards, preference, _candidate = _ready_context()
        incomplete = _candidate_from_sources(cards, (("card-a", 0),))

        with self.assertRaises(ValueError):
            capture_guided_refinement_response(
                brief,
                cards,
                preference,
                incomplete,
                kind=GuidedRefinementResponseKind.ACCEPT_DIRECTION,
            )
        with self.assertRaises(TypeError):
            capture_guided_refinement_response(
                brief,
                cards,
                preference,
                _candidate,
                kind="accept_direction",  # type: ignore[arg-type]
            )

    def test_response_is_bound_to_exact_brief_cards_preference_and_candidate(self) -> None:
        brief, cards, preference, candidate, response = _capture(
            GuidedRefinementResponseKind.ACCEPT_DIRECTION
        )
        reordered = assess_guided_refinement_response(
            brief,
            tuple(reversed(cards)),
            preference,
            candidate,
            response,
        ).to_dict()
        original = assess_guided_refinement_response(
            brief,
            cards,
            preference,
            candidate,
            response,
        ).to_dict()
        self.assertEqual(original, reordered)

        changed_brief = replace(
            brief,
            pace=BriefTextFact(
                state=BriefKnownState.TENTATIVE,
                value="Changed private pace " + PRIVATE,
            ),
        )
        changed_cards = (
            replace(cards[0], title="Changed private card " + PRIVATE),
            cards[1],
        )
        changed_preference = _preference(
            brief,
            cards,
            GuidedDirectionPreferenceKind.REQUEST_REFINEMENT,
        )
        extra_line = GuidedOutlineLine(
            outline_slot=3,
            source=ProposalLineSource.AI_SUGGESTED,
            known_state=BriefKnownState.UNKNOWN,
            title="New private detail " + PRIVATE,
            rationale="New private rationale " + PRIVATE,
        )
        changed_candidate = replace(
            candidate,
            direction=replace(
                candidate.direction,
                lines=(*candidate.direction.lines, extra_line),
            ),
        )

        for changed_context in (
            (changed_brief, cards, preference, candidate),
            (brief, changed_cards, preference, candidate),
            (brief, cards, changed_preference, candidate),
            (brief, cards, preference, changed_candidate),
        ):
            with self.assertRaises(ValueError):
                assess_guided_refinement_response(
                    *changed_context,
                    response,
                )

    def test_response_and_review_cannot_be_forged_or_replaced(self) -> None:
        brief, cards, preference, candidate, response = _capture(
            GuidedRefinementResponseKind.ACCEPT_DIRECTION
        )
        review = assess_guided_refinement_response(
            brief,
            cards,
            preference,
            candidate,
            response,
        )

        with self.assertRaises(ValueError):
            GuidedRefinementResponse(
                kind=GuidedRefinementResponseKind.ACCEPT_DIRECTION,
            )
        with self.assertRaises(ValueError):
            replace(
                response,
                kind=GuidedRefinementResponseKind.REQUEST_ADJUSTMENT,
            )
        with self.assertRaises(ValueError):
            replace(review, tentative_fields=(PRIVATE,))

    def test_safe_transcript_and_repr_redact_exact_private_context(self) -> None:
        brief, cards, preference, candidate, response = _capture(
            GuidedRefinementResponseKind.ACCEPT_DIRECTION
        )
        review = assess_guided_refinement_response(
            brief,
            cards,
            preference,
            candidate,
            response,
        )
        rendered = "\n".join(
            (
                repr(response),
                repr(review),
                json.dumps(review.to_dict(), ensure_ascii=False),
            )
        )

        for private_value in (
            PRIVATE,
            "card-a",
            "card-refined",
            "Private direction A",
            "2026-10-12",
        ):
            self.assertNotIn(private_value, rendered)

    def test_public_contract_has_no_parser_confirmation_apply_or_trip_creation(self) -> None:
        for name in (
            "GUIDED_REFINEMENT_RESPONSE_VERSION",
            "GuidedRefinementResponse",
            "GuidedRefinementResponseKind",
            "GuidedRefinementResponseReview",
            "GuidedRefinementResponseStatus",
            "assess_guided_refinement_response",
            "capture_guided_refinement_response",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        for unsupported_name in (
            "parse_guided_refinement_response",
            "confirm_guided_refinement",
            "apply_guided_refinement_response",
            "create_trip_from_refinement_response",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
