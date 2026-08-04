"""Phase 5.9 contracts for exact private itinerary-candidate responses."""

from __future__ import annotations

import json
import unittest
from dataclasses import fields, replace

import trip_planner
from tests.test_phase53_guided_draft import _transport_boundary
from tests.test_phase56_guided_refinement import PRIVATE, _preference
from tests.test_phase58_guided_itinerary import _accepted_context, _candidate
from trip_planner.guided_proposal import GuidedDirectionPreferenceKind
from trip_planner.guided_refinement import (
    GuidedRefinementResponseKind,
    capture_guided_refinement_response,
)
from trip_planner.guided_itinerary import (
    GuidedItineraryResponse,
    GuidedItineraryResponseKind,
    GuidedItineraryResponseReview,
    GuidedItineraryResponseStatus,
    assess_guided_itinerary_response,
    capture_guided_itinerary_response,
)


def _reviewable_context(*, brief=None):
    phase58_context = _accepted_context(brief=brief)
    itinerary_candidate = _candidate((0, (0,)), (1, (1,)))
    return (*phase58_context, itinerary_candidate)


def _capture(kind: GuidedItineraryResponseKind, *, brief=None):
    context = _reviewable_context(brief=brief)
    response = capture_guided_itinerary_response(
        *context,
        kind=kind,
    )
    return (*context, response)


class GuidedItineraryResponseTests(unittest.TestCase):
    def test_accept_candidate_hands_off_only_to_private_evidence_requirements(self) -> None:
        context = _capture(
            GuidedItineraryResponseKind.ACCEPT_ITINERARY_CANDIDATE
        )
        review = assess_guided_itinerary_response(*context)
        safe = review.to_dict()

        self.assertEqual(
            GuidedItineraryResponseStatus.READY_FOR_PRIVATE_EVIDENCE_REQUIREMENTS,
            review.status,
        )
        self.assertEqual(
            "prepare_private_evidence_requirements",
            safe["next_action"],
        )
        self.assertFalse(safe["requires_user_response"])
        self.assertFalse(safe["requires_user_review"])
        self.assertFalse(safe["requires_user_decision"])
        self.assertEqual(
            {
                "kind": "accept_itinerary_candidate",
                "accepted_as_private_candidate": True,
                "may_prepare_private_evidence_requirements": True,
                "relative_day_bucket_count": 2,
                "available_relative_day_count": 5,
                "refined_source_line_count": 2,
                "placed_source_line_count": 2,
                "transport_boundary_count": 0,
                "is_executable_schedule": False,
                "decision_state": "candidate",
                "evidence_state": "unverified",
                "supports_authoritative_use": False,
            },
            safe["itinerary_response"],
        )
        self.assertEqual(
            [
                "destination_location",
                "proposal_candidates",
                "itinerary_candidate",
            ],
            safe["needs_verification"],
        )
        self.assertEqual(
            {
                "process_local": True,
                "writes_to_trip": False,
                "provider_calls": False,
                "provider_requests_created": False,
                "scheduled": False,
                "trip_created": False,
                "rendered": False,
                "deployed": False,
            },
            safe["side_effects"],
        )

    def test_adjustment_response_returns_to_private_candidate_refinement(self) -> None:
        context = _capture(
            GuidedItineraryResponseKind.REQUEST_ITINERARY_ADJUSTMENT
        )
        safe = assess_guided_itinerary_response(*context).to_dict()

        self.assertEqual(
            "ready_for_private_itinerary_refinement",
            safe["status"],
        )
        self.assertEqual(
            "refine_private_itinerary_candidate",
            safe["next_action"],
        )
        self.assertFalse(
            safe["itinerary_response"]["accepted_as_private_candidate"]
        )
        self.assertFalse(
            safe["itinerary_response"][
                "may_prepare_private_evidence_requirements"
            ]
        )
        self.assertEqual(
            "candidate",
            safe["itinerary_response"]["decision_state"],
        )
        self.assertEqual(
            "unverified",
            safe["itinerary_response"]["evidence_state"],
        )
        self.assertFalse(safe["side_effects"]["provider_requests_created"])

    def test_capture_requires_current_visible_candidate_and_exact_enum(self) -> None:
        brief, cards, preference, refinement, refinement_response = (
            _accepted_context()
        )
        incomplete = _candidate((0, (0,)))

        with self.assertRaises(ValueError):
            capture_guided_itinerary_response(
                brief,
                cards,
                preference,
                refinement,
                refinement_response,
                incomplete,
                kind=GuidedItineraryResponseKind.ACCEPT_ITINERARY_CANDIDATE,
            )
        with self.assertRaises(TypeError):
            capture_guided_itinerary_response(
                brief,
                cards,
                preference,
                refinement,
                refinement_response,
                _candidate((0, (0, 1))),
                kind="accept_itinerary_candidate",  # type: ignore[arg-type]
            )

        adjustment_context = _accepted_context(
            response_kind=GuidedRefinementResponseKind.REQUEST_ADJUSTMENT
        )
        with self.assertRaises(ValueError):
            capture_guided_itinerary_response(
                *adjustment_context,
                _candidate((0, (0, 1))),
                kind=GuidedItineraryResponseKind.ACCEPT_ITINERARY_CANDIDATE,
            )

    def test_response_binds_every_context_layer_and_allows_card_reordering(self) -> None:
        (
            brief,
            cards,
            preference,
            refinement,
            refinement_response,
            itinerary_candidate,
            response,
        ) = _capture(GuidedItineraryResponseKind.ACCEPT_ITINERARY_CANDIDATE)
        original = assess_guided_itinerary_response(
            brief,
            cards,
            preference,
            refinement,
            refinement_response,
            itinerary_candidate,
            response,
        ).to_dict()
        reordered = assess_guided_itinerary_response(
            brief,
            tuple(reversed(cards)),
            preference,
            refinement,
            refinement_response,
            itinerary_candidate,
            response,
        ).to_dict()
        self.assertEqual(original, reordered)

        changed_brief = replace(brief, constraints=brief.must_do)
        changed_cards = (
            replace(cards[0], title="Changed private card " + PRIVATE),
            cards[1],
        )
        changed_preference = _preference(
            brief,
            cards,
            GuidedDirectionPreferenceKind.REQUEST_REFINEMENT,
        )
        changed_refinement = replace(
            refinement,
            direction=replace(
                refinement.direction,
                lines=(
                    *refinement.direction.lines,
                    replace(refinement.direction.lines[-1], outline_slot=3),
                ),
            ),
        )
        changed_refinement_response = capture_guided_refinement_response(
            brief,
            cards,
            preference,
            changed_refinement,
            kind=GuidedRefinementResponseKind.ACCEPT_DIRECTION,
        )
        changed_candidate = _candidate((1, (0,)), (2, (1,)))
        for changed_context in (
            (
                changed_brief,
                cards,
                preference,
                refinement,
                refinement_response,
                itinerary_candidate,
            ),
            (
                brief,
                changed_cards,
                preference,
                refinement,
                refinement_response,
                itinerary_candidate,
            ),
            (
                brief,
                cards,
                changed_preference,
                refinement,
                refinement_response,
                itinerary_candidate,
            ),
            (
                brief,
                cards,
                preference,
                changed_refinement,
                refinement_response,
                itinerary_candidate,
            ),
            (
                brief,
                cards,
                preference,
                refinement,
                changed_refinement_response,
                itinerary_candidate,
            ),
            (
                brief,
                cards,
                preference,
                refinement,
                refinement_response,
                changed_candidate,
            ),
        ):
            with self.subTest(changed_context=changed_context):
                with self.assertRaises(ValueError):
                    assess_guided_itinerary_response(
                        *changed_context,
                        response,
                    )

        changed_response = capture_guided_itinerary_response(
            brief,
            cards,
            preference,
            refinement,
            refinement_response,
            changed_candidate,
            kind=GuidedItineraryResponseKind.ACCEPT_ITINERARY_CANDIDATE,
        )
        with self.assertRaises(ValueError):
            assess_guided_itinerary_response(
                brief,
                cards,
                preference,
                refinement,
                refinement_response,
                itinerary_candidate,
                changed_response,
            )

    def test_response_schema_has_no_free_text_or_authority_fields(self) -> None:
        self.assertEqual(
            {"kind", "_context_fingerprint"},
            {item.name for item in fields(GuidedItineraryResponse)},
        )
        for forbidden_field in (
            "text",
            "reason",
            "date",
            "time",
            "route",
            "provider_request",
            "approval",
            "confirmation",
        ):
            self.assertNotIn(
                forbidden_field,
                {item.name for item in fields(GuidedItineraryResponse)},
            )

    def test_response_and_review_cannot_be_forged_or_replaced(self) -> None:
        context = _capture(
            GuidedItineraryResponseKind.ACCEPT_ITINERARY_CANDIDATE
        )
        response = context[-1]
        review = assess_guided_itinerary_response(*context)

        with self.assertRaises(ValueError):
            GuidedItineraryResponse(
                kind=GuidedItineraryResponseKind.ACCEPT_ITINERARY_CANDIDATE
            )
        with self.assertRaises(ValueError):
            replace(
                response,
                kind=GuidedItineraryResponseKind.REQUEST_ITINERARY_ADJUSTMENT,
            )
        with self.assertRaises(ValueError):
            GuidedItineraryResponseReview(
                status=(
                    GuidedItineraryResponseStatus.READY_FOR_PRIVATE_EVIDENCE_REQUIREMENTS
                ),
                response_kind=(
                    GuidedItineraryResponseKind.ACCEPT_ITINERARY_CANDIDATE
                ),
                relative_day_bucket_count=2,
                available_relative_day_count=5,
                refined_source_line_count=2,
                placed_source_line_count=2,
                transport_boundary_count=0,
                next_action="prepare_private_evidence_requirements",
            )
        with self.assertRaises(ValueError):
            replace(review, needs_verification=(PRIVATE,))

    def test_safe_transcript_and_repr_redact_exact_private_context(self) -> None:
        boundary = _transport_boundary()
        base_brief = _accepted_context()[0]
        brief = replace(base_brief, transport_boundaries=(boundary,))
        context = _accepted_context(brief=brief)
        itinerary_candidate = _candidate(
            (0, (0,)),
            (1, (1,)),
            boundary_ids=(boundary.boundary_id,),
        )
        response = capture_guided_itinerary_response(
            *context,
            itinerary_candidate,
            kind=GuidedItineraryResponseKind.ACCEPT_ITINERARY_CANDIDATE,
        )
        review = assess_guided_itinerary_response(
            *context,
            itinerary_candidate,
            response,
        )
        self.assertEqual(1, review.transport_boundary_count)
        self.assertEqual(
            1,
            review.to_dict()["itinerary_response"]["transport_boundary_count"],
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
            boundary.boundary_id,
            "source_line_indexes",
            "Private direction A",
            "2026-10-12",
        ):
            self.assertNotIn(private_value, rendered)

    def test_public_contract_has_no_parser_provider_request_or_apply_path(self) -> None:
        for name in (
            "GUIDED_ITINERARY_RESPONSE_VERSION",
            "GuidedItineraryResponse",
            "GuidedItineraryResponseKind",
            "GuidedItineraryResponseReview",
            "GuidedItineraryResponseStatus",
            "assess_guided_itinerary_response",
            "capture_guided_itinerary_response",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        for unsupported_name in (
            "parse_guided_itinerary_response",
            "prepare_private_evidence_requirements",
            "create_guided_provider_requests",
            "call_guided_itinerary_provider",
            "schedule_guided_itinerary_response",
            "create_trip_from_guided_itinerary_response",
            "confirm_guided_itinerary_response",
            "apply_guided_itinerary_response",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
