"""Phase 5.8 contracts for private source-only itinerary candidates."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace

import trip_planner
import trip_planner.guided_itinerary as guided_itinerary_module
from tests.test_phase53_guided_draft import _transport_boundary
from tests.test_phase56_guided_refinement import (
    PRIVATE,
    _brief,
    _candidate_from_sources,
    _cards,
    _preference,
)
from trip_planner.guided_proposal import GuidedDirectionPreferenceKind
from trip_planner.guided_refinement import (
    GuidedRefinementResponseKind,
    capture_guided_refinement_response,
)
from trip_planner.guided_itinerary import (
    GuidedItineraryCandidate,
    GuidedItineraryDay,
    GuidedItineraryProblemCode,
    GuidedItineraryReview,
    GuidedItineraryStatus,
    assess_guided_itinerary_candidate,
)
from trip_planner.models import DecisionState, EvidenceState


def _accepted_context(
    *,
    card_ref: str = "card-a",
    brief=None,
    response_kind: GuidedRefinementResponseKind = (
        GuidedRefinementResponseKind.ACCEPT_DIRECTION
    ),
):
    if brief is None:
        brief = _brief()
    cards = _cards()
    preference = _preference(
        brief,
        cards,
        GuidedDirectionPreferenceKind.PREFER_ONE,
        (card_ref,),
    )
    sources = tuple(
        (card_ref, line_index)
        for line_index in range(
            len(next(card for card in cards if card.card_ref == card_ref).lines)
        )
    )
    refinement = _candidate_from_sources(cards, sources)
    response = capture_guided_refinement_response(
        brief,
        cards,
        preference,
        refinement,
        kind=response_kind,
    )
    return brief, cards, preference, refinement, response


def _candidate(
    *day_lines: tuple[int, tuple[int, ...]],
    boundary_ids: tuple[str, ...] = (),
) -> GuidedItineraryCandidate:
    return GuidedItineraryCandidate(
        days=tuple(
            GuidedItineraryDay(
                relative_day_index=day_index,
                source_line_indexes=line_indexes,
            )
            for day_index, line_indexes in day_lines
        ),
        retained_transport_boundary_ids=boundary_ids,
    )


class GuidedItineraryTests(unittest.TestCase):
    def test_accepted_direction_reaches_one_private_candidate_review(self) -> None:
        context = _accepted_context()
        candidate = _candidate((0, (0,)), (2, (1,)))

        review = assess_guided_itinerary_candidate(*context, candidate)
        safe = review.to_dict()

        self.assertEqual(GuidedItineraryStatus.REVIEW_REQUIRED, review.status)
        self.assertEqual("review_private_itinerary_candidate", safe["next_action"])
        self.assertTrue(safe["requires_user_response"])
        self.assertTrue(safe["requires_user_review"])
        self.assertTrue(safe["requires_user_decision"])
        self.assertTrue(safe["may_present_itinerary_candidate"])
        self.assertEqual(
            "這個每日候選安排是否符合你的想法？可以接受安排，或指出要調整的地方。",
            safe["review_prompt"],
        )
        self.assertEqual(
            "每日候選尚未確認景點身分、營業時間、交通、空位或價格，也不是可執行日程。",
            safe["review_disclosure"],
        )
        self.assertEqual([], safe["problems"])
        self.assertEqual(
            {
                "relative_day_bucket_count": 2,
                "available_relative_day_count": 5,
                "refined_source_line_count": 2,
                "placed_source_line_count": 2,
                "user_stated_line_count": 1,
                "tentative_line_count": 0,
                "ai_candidate_line_count": 1,
                "expected_transport_boundary_count": 0,
                "retained_transport_boundary_count": 0,
                "uses_relative_day_indexes": True,
                "contains_calendar_dates": False,
                "contains_times": False,
                "contains_route_values": False,
                "is_executable_schedule": False,
                "decision_state": "candidate",
                "evidence_state": "unverified",
                "supports_authoritative_use": False,
            },
            safe["itinerary_candidate"],
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
                "scheduled": False,
                "trip_created": False,
                "rendered": False,
                "deployed": False,
            },
            safe["side_effects"],
        )
        self.assertEqual(DecisionState.CANDIDATE, candidate.decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, candidate.evidence_state)

    def test_source_categories_preserve_tentative_and_ai_distinctions(self) -> None:
        context = _accepted_context(card_ref="card-b")
        candidate = _candidate((0, (0,)), (1, (1, 2)))

        safe = assess_guided_itinerary_candidate(*context, candidate).to_dict()

        self.assertEqual(1, safe["itinerary_candidate"]["user_stated_line_count"])
        self.assertEqual(1, safe["itinerary_candidate"]["tentative_line_count"])
        self.assertEqual(1, safe["itinerary_candidate"]["ai_candidate_line_count"])
        self.assertIn("must_do", safe["tentative_fields"])

    def test_missing_duplicate_and_unknown_source_lines_stay_private(self) -> None:
        context = _accepted_context()
        cases = (
            (
                _candidate((0, (0,))),
                (GuidedItineraryProblemCode.REFINED_LINE_NOT_PLACED,),
            ),
            (
                _candidate((0, (0, 0, 1))),
                (
                    GuidedItineraryProblemCode.REFINED_LINE_PLACED_MULTIPLE_TIMES,
                ),
            ),
            (
                _candidate((0, (0, 1, 2))),
                (GuidedItineraryProblemCode.UNKNOWN_REFINED_LINE_INCLUDED,),
            ),
        )

        for candidate, expected_problems in cases:
            with self.subTest(expected_problems=expected_problems):
                review = assess_guided_itinerary_candidate(*context, candidate)
                safe = review.to_dict()
                self.assertEqual(
                    GuidedItineraryStatus.NEEDS_REFINEMENT,
                    review.status,
                )
                self.assertEqual(
                    "refine_private_itinerary_candidate",
                    safe["next_action"],
                )
                self.assertEqual(expected_problems, review.problem_codes)
                self.assertFalse(safe["may_present_itinerary_candidate"])
                self.assertIsNone(safe["review_prompt"])
                self.assertIsNone(safe["review_disclosure"])

    def test_relative_day_index_is_bounded_by_exact_trip_span(self) -> None:
        context = _accepted_context()
        candidate = _candidate((0, (0,)), (5, (1,)))

        review = assess_guided_itinerary_candidate(*context, candidate)

        self.assertEqual(GuidedItineraryStatus.NEEDS_REFINEMENT, review.status)
        self.assertEqual(
            (
                GuidedItineraryProblemCode.RELATIVE_DAY_OUTSIDE_TRIP_SPAN,
            ),
            review.problem_codes,
        )
        self.assertEqual(5, review.available_relative_day_count)

    def test_transport_boundaries_require_exact_multiset_carryover(self) -> None:
        boundary = _transport_boundary()
        brief = replace(_brief(), transport_boundaries=(boundary,))
        context = _accepted_context(brief=brief)
        valid = _candidate(
            (0, (0,)),
            (1, (1,)),
            boundary_ids=(boundary.boundary_id,),
        )

        review = assess_guided_itinerary_candidate(*context, valid)
        self.assertEqual(GuidedItineraryStatus.REVIEW_REQUIRED, review.status)
        self.assertEqual(1, review.expected_transport_boundary_count)
        self.assertEqual(1, review.retained_transport_boundary_count)

        cases = (
            (
                _candidate((0, (0, 1))),
                (GuidedItineraryProblemCode.TRANSPORT_BOUNDARY_NOT_RETAINED,),
            ),
            (
                _candidate(
                    (0, (0, 1)),
                    boundary_ids=(boundary.boundary_id, boundary.boundary_id),
                ),
                (
                    GuidedItineraryProblemCode.TRANSPORT_BOUNDARY_RETAINED_MULTIPLE_TIMES,
                ),
            ),
            (
                _candidate(
                    (0, (0, 1)),
                    boundary_ids=("unknown-private-boundary",),
                ),
                (
                    GuidedItineraryProblemCode.TRANSPORT_BOUNDARY_NOT_RETAINED,
                    GuidedItineraryProblemCode.UNKNOWN_TRANSPORT_BOUNDARY_INCLUDED,
                ),
            ),
        )
        for candidate, expected_problems in cases:
            with self.subTest(expected_problems=expected_problems):
                self.assertEqual(
                    expected_problems,
                    assess_guided_itinerary_candidate(
                        *context,
                        candidate,
                    ).problem_codes,
                )

    def test_adjustment_response_cannot_create_an_itinerary_candidate(self) -> None:
        context = _accepted_context(
            response_kind=GuidedRefinementResponseKind.REQUEST_ADJUSTMENT
        )

        with self.assertRaises(ValueError):
            assess_guided_itinerary_candidate(
                *context,
                _candidate((0, (0, 1))),
            )

    def test_exact_context_is_revalidated_and_card_order_is_irrelevant(self) -> None:
        brief, cards, preference, refinement, response = _accepted_context()
        candidate = _candidate((0, (0,)), (1, (1,)))
        original = assess_guided_itinerary_candidate(
            brief,
            cards,
            preference,
            refinement,
            response,
            candidate,
        ).to_dict()
        reordered = assess_guided_itinerary_candidate(
            brief,
            tuple(reversed(cards)),
            preference,
            refinement,
            response,
            candidate,
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
        changed_response = capture_guided_refinement_response(
            brief,
            cards,
            preference,
            changed_refinement,
            kind=GuidedRefinementResponseKind.ACCEPT_DIRECTION,
        )
        for changed_context in (
            (changed_brief, cards, preference, refinement, response),
            (brief, changed_cards, preference, refinement, response),
            (brief, cards, changed_preference, refinement, response),
            (brief, cards, preference, changed_refinement, response),
            (brief, cards, preference, refinement, changed_response),
        ):
            with self.subTest(changed_context=changed_context):
                with self.assertRaises(ValueError):
                    assess_guided_itinerary_candidate(
                        *changed_context,
                        candidate,
                    )

    def test_candidate_schema_has_no_free_text_dates_times_or_routes(self) -> None:
        self.assertEqual(
            {"relative_day_index", "source_line_indexes"},
            {item.name for item in fields(GuidedItineraryDay)},
        )
        self.assertEqual(
            {"days", "retained_transport_boundary_ids"},
            {item.name for item in fields(GuidedItineraryCandidate)},
        )
        with self.assertRaises(ValueError):
            GuidedItineraryDay(relative_day_index=0, source_line_indexes=())
        with self.assertRaises(ValueError):
            GuidedItineraryCandidate(
                days=(
                    GuidedItineraryDay(0, (0,)),
                    GuidedItineraryDay(0, (1,)),
                )
            )

    def test_review_cannot_be_forged_or_replaced(self) -> None:
        context = _accepted_context()
        review = assess_guided_itinerary_candidate(
            *context,
            _candidate((0, (0,)), (1, (1,))),
        )

        with self.assertRaises(ValueError):
            GuidedItineraryReview(
                status=GuidedItineraryStatus.REVIEW_REQUIRED,
                next_action="review_private_itinerary_candidate",
                relative_day_bucket_count=2,
                available_relative_day_count=5,
                refined_source_line_count=2,
                placed_source_line_count=2,
                user_stated_line_count=1,
                tentative_line_count=0,
                ai_candidate_line_count=1,
                expected_transport_boundary_count=0,
                retained_transport_boundary_count=0,
            )
        with self.assertRaises(ValueError):
            replace(review, tentative_fields=(PRIVATE,))

    def test_safe_transcript_and_repr_redact_private_context(self) -> None:
        boundary = _transport_boundary()
        brief = replace(_brief(), transport_boundaries=(boundary,))
        context = _accepted_context(brief=brief)
        candidate = _candidate(
            (0, (0,)),
            (1, (1,)),
            boundary_ids=(boundary.boundary_id,),
        )
        review = assess_guided_itinerary_candidate(*context, candidate)
        self.assertEqual(
            "GuidedItineraryDay(source_line_count=1)",
            repr(candidate.days[0]),
        )
        rendered = "\n".join(
            (
                repr(candidate),
                repr(candidate.days[0]),
                repr(review),
                json.dumps(review.to_dict(), ensure_ascii=False),
            )
        )

        for private_value in (
            PRIVATE,
            "card-a",
            "card-refined",
            boundary.boundary_id,
            "Private direction A",
            "2026-10-12",
        ):
            self.assertNotIn(private_value, rendered)

    def test_public_contract_has_no_parser_provider_scheduler_or_write_path(self) -> None:
        for name in (
            "GUIDED_ITINERARY_VERSION",
            "GuidedItineraryCandidate",
            "GuidedItineraryDay",
            "GuidedItineraryProblemCode",
            "GuidedItineraryReview",
            "GuidedItineraryStatus",
            "assess_guided_itinerary_candidate",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        for unsupported_name in (
            "parse_guided_itinerary",
            "schedule_guided_itinerary",
            "create_trip_from_guided_itinerary",
            "apply_guided_itinerary",
            "confirm_guided_itinerary",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        source = inspect.getsource(guided_itinerary_module)
        tree = ast.parse(source)
        allowed_imports = {"unicodedata"}
        allowed_from_imports = {
            (0, "__future__"),
            (0, "collections"),
            (0, "dataclasses"),
            (0, "enum"),
            (0, "typing"),
            (1, "guided_draft"),
            (1, "guided_proposal"),
            (1, "guided_refinement"),
            (1, "models"),
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for imported in node.names:
                    self.assertIn(imported.name, allowed_imports)
            elif isinstance(node, ast.ImportFrom):
                self.assertIn((node.level, node.module), allowed_from_imports)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(
                    node.func.id,
                    {"open", "exec", "eval", "compile", "__import__"},
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
