"""Phase 5.10 contracts for private line-level evidence requirements."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace

import trip_planner
import trip_planner.guided_evidence_plan as guided_evidence_plan_module
from tests.test_phase53_guided_draft import _transport_boundary
from tests.test_phase56_guided_refinement import PRIVATE, _preference
from tests.test_phase58_guided_itinerary import _accepted_context, _candidate
from tests.test_phase59_guided_itinerary_response import _capture
from trip_planner.guided_evidence_plan import (
    GuidedEvidenceDisposition,
    GuidedEvidencePlanProblemCode,
    GuidedEvidencePlanReview,
    GuidedEvidencePlanStatus,
    GuidedEvidenceRequirementPlan,
    GuidedEvidenceTopic,
    GuidedLineEvidenceRequirement,
    assess_guided_evidence_requirement_plan,
)
from trip_planner.guided_itinerary import (
    GuidedItineraryResponseKind,
    capture_guided_itinerary_response,
)
from trip_planner.guided_proposal import GuidedDirectionPreferenceKind
from trip_planner.guided_refinement import (
    GuidedRefinementResponseKind,
    capture_guided_refinement_response,
)
from trip_planner.models import DecisionState, EvidenceState


def _requirement(
    source_line_index: int,
    *topics: GuidedEvidenceTopic,
    disposition: GuidedEvidenceDisposition = (
        GuidedEvidenceDisposition.REQUIRES_VERIFICATION
    ),
) -> GuidedLineEvidenceRequirement:
    return GuidedLineEvidenceRequirement(
        source_line_index=source_line_index,
        disposition=disposition,
        topics=topics,
    )


def _plan(
    *declarations: GuidedLineEvidenceRequirement,
) -> GuidedEvidenceRequirementPlan:
    return GuidedEvidenceRequirementPlan(declarations=declarations)


def _accepted_response_context():
    return _capture(
        GuidedItineraryResponseKind.ACCEPT_ITINERARY_CANDIDATE
    )


def _complete_plan() -> GuidedEvidenceRequirementPlan:
    return _plan(
        _requirement(
            0,
            GuidedEvidenceTopic.CURRENT_OPENING_HOURS,
            GuidedEvidenceTopic.PLACE_IDENTITY,
        ),
        _requirement(
            1,
            disposition=(
                GuidedEvidenceDisposition.NO_EXTERNAL_EVIDENCE_IDENTIFIED
            ),
        ),
    )


class GuidedEvidencePlanTests(unittest.TestCase):
    def test_complete_plan_reaches_only_private_provider_scope_review(self) -> None:
        context = _accepted_response_context()
        plan = _complete_plan()

        review = assess_guided_evidence_requirement_plan(*context, plan)
        safe = review.to_dict()

        self.assertEqual(
            GuidedEvidencePlanStatus.READY_FOR_PRIVATE_PROVIDER_SCOPE,
            review.status,
        )
        self.assertEqual(
            "prepare_private_provider_scope_review",
            safe["next_action"],
        )
        self.assertFalse(safe["requires_user_response"])
        self.assertFalse(safe["requires_user_review"])
        self.assertFalse(safe["requires_user_decision"])
        self.assertEqual([], safe["problems"])
        self.assertEqual(
            {
                "source_line_count": 2,
                "declaration_count": 2,
                "classified_source_line_count": 2,
                "requires_verification_line_count": 1,
                "no_external_evidence_identified_line_count": 1,
                "topic_counts": {
                    "availability": 0,
                    "current_opening_hours": 1,
                    "lodging": 0,
                    "place_identity": 1,
                    "price": 0,
                    "route": 0,
                },
                "evidence_requirements": [
                    "current_opening_hours",
                    "place_identity",
                ],
                "all_source_lines_classified": True,
                "no_external_evidence_identified_means_verified": False,
                "contains_line_text": False,
                "contains_calendar_dates": False,
                "contains_provider_identifiers": False,
                "contains_provider_query": False,
                "contains_provider_payload": False,
                "relative_day_bucket_count": 2,
                "transport_boundary_count": 0,
                "decision_state": "candidate",
                "evidence_state": "unverified",
                "supports_authoritative_use": False,
            },
            safe["evidence_requirement_plan"],
        )
        self.assertEqual(
            {
                "may_prepare_private_provider_scope_review": True,
                "provider_scope_authorized": False,
                "provider_requests_created": False,
                "provider_calls_permitted": False,
            },
            safe["provider_scope"],
        )
        self.assertEqual(
            [
                "destination_location",
                "proposal_candidates",
                "itinerary_candidate",
                "evidence_requirements",
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
        self.assertEqual(DecisionState.CANDIDATE, plan.decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, plan.evidence_state)

    def test_no_external_evidence_disposition_never_implies_verification(self) -> None:
        context = _accepted_response_context()
        plan = _plan(
            *(
                _requirement(
                    index,
                    disposition=(
                        GuidedEvidenceDisposition.NO_EXTERNAL_EVIDENCE_IDENTIFIED
                    ),
                )
                for index in range(2)
            )
        )

        safe = assess_guided_evidence_requirement_plan(*context, plan).to_dict()

        self.assertEqual("unverified", safe["evidence_requirement_plan"]["evidence_state"])
        self.assertFalse(
            safe["evidence_requirement_plan"][
                "no_external_evidence_identified_means_verified"
            ]
        )
        self.assertFalse(
            safe["evidence_requirement_plan"]["supports_authoritative_use"]
        )
        self.assertFalse(safe["provider_scope"]["provider_scope_authorized"])
        self.assertEqual([], safe["evidence_requirement_plan"]["evidence_requirements"])

    def test_topics_are_provider_neutral_canonical_sets_with_strict_dispositions(self) -> None:
        requirement = _requirement(
            0,
            GuidedEvidenceTopic.ROUTE,
            GuidedEvidenceTopic.PLACE_IDENTITY,
            GuidedEvidenceTopic.ROUTE,
        )
        self.assertEqual(
            (
                GuidedEvidenceTopic.PLACE_IDENTITY,
                GuidedEvidenceTopic.ROUTE,
            ),
            requirement.topics,
        )

        with self.assertRaises(ValueError):
            _requirement(0)
        with self.assertRaises(ValueError):
            _requirement(
                0,
                GuidedEvidenceTopic.PRICE,
                disposition=(
                    GuidedEvidenceDisposition.NO_EXTERNAL_EVIDENCE_IDENTIFIED
                ),
            )
        with self.assertRaises(TypeError):
            GuidedLineEvidenceRequirement(
                source_line_index=0,
                disposition="requires_verification",  # type: ignore[arg-type]
                topics=(GuidedEvidenceTopic.ROUTE,),
            )
        with self.assertRaises(TypeError):
            GuidedLineEvidenceRequirement(
                source_line_index=0,
                disposition=GuidedEvidenceDisposition.REQUIRES_VERIFICATION,
                topics=("route",),  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            _requirement(32, GuidedEvidenceTopic.ROUTE)

    def test_missing_duplicate_and_unknown_lines_stay_private(self) -> None:
        context = _accepted_response_context()
        cases = (
            (
                _plan(_requirement(0, GuidedEvidenceTopic.ROUTE)),
                (GuidedEvidencePlanProblemCode.SOURCE_LINE_NOT_CLASSIFIED,),
            ),
            (
                _plan(
                    _requirement(0, GuidedEvidenceTopic.ROUTE),
                    _requirement(0, GuidedEvidenceTopic.PRICE),
                    _requirement(1, GuidedEvidenceTopic.PLACE_IDENTITY),
                ),
                (
                    GuidedEvidencePlanProblemCode.SOURCE_LINE_CLASSIFIED_MULTIPLE_TIMES,
                ),
            ),
            (
                _plan(
                    _requirement(0, GuidedEvidenceTopic.ROUTE),
                    _requirement(1, GuidedEvidenceTopic.PRICE),
                    _requirement(2, GuidedEvidenceTopic.PLACE_IDENTITY),
                ),
                (GuidedEvidencePlanProblemCode.UNKNOWN_SOURCE_LINE_INCLUDED,),
            ),
        )

        for plan, expected_problems in cases:
            with self.subTest(expected_problems=expected_problems):
                review = assess_guided_evidence_requirement_plan(*context, plan)
                safe = review.to_dict()
                self.assertEqual(
                    GuidedEvidencePlanStatus.NEEDS_REFINEMENT,
                    review.status,
                )
                self.assertEqual(
                    "refine_private_evidence_requirements",
                    safe["next_action"],
                )
                self.assertEqual(
                    [item.value for item in expected_problems],
                    safe["problems"],
                )
                self.assertFalse(
                    safe["provider_scope"][
                        "may_prepare_private_provider_scope_review"
                    ]
                )
                self.assertFalse(
                    safe["evidence_requirement_plan"][
                        "all_source_lines_classified"
                    ]
                )
                self.assertFalse(safe["side_effects"]["provider_calls"])

    def test_plan_requires_exact_accepted_phase59_response(self) -> None:
        adjustment_context = _capture(
            GuidedItineraryResponseKind.REQUEST_ITINERARY_ADJUSTMENT
        )
        with self.assertRaises(ValueError):
            assess_guided_evidence_requirement_plan(
                *adjustment_context,
                _complete_plan(),
            )
        with self.assertRaises(TypeError):
            assess_guided_evidence_requirement_plan(
                *_accepted_response_context(),
                object(),  # type: ignore[arg-type]
            )

    def test_phase59_binding_is_revalidated_and_card_order_is_canonical(self) -> None:
        (
            brief,
            cards,
            preference,
            refinement,
            refinement_response,
            itinerary_candidate,
            itinerary_response,
        ) = _accepted_response_context()
        plan = _complete_plan()
        original = assess_guided_evidence_requirement_plan(
            brief,
            cards,
            preference,
            refinement,
            refinement_response,
            itinerary_candidate,
            itinerary_response,
            plan,
        ).to_dict()
        reordered = assess_guided_evidence_requirement_plan(
            brief,
            tuple(reversed(cards)),
            preference,
            refinement,
            refinement_response,
            itinerary_candidate,
            itinerary_response,
            plan,
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
                    refinement.direction.lines[0],
                    replace(refinement.direction.lines[1], outline_slot=3),
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
        changed_response = capture_guided_itinerary_response(
            brief,
            cards,
            preference,
            refinement,
            refinement_response,
            changed_candidate,
            kind=GuidedItineraryResponseKind.ACCEPT_ITINERARY_CANDIDATE,
        )
        changed_contexts = (
            (
                changed_brief,
                cards,
                preference,
                refinement,
                refinement_response,
                itinerary_candidate,
                itinerary_response,
            ),
            (
                brief,
                changed_cards,
                preference,
                refinement,
                refinement_response,
                itinerary_candidate,
                itinerary_response,
            ),
            (
                brief,
                cards,
                changed_preference,
                refinement,
                refinement_response,
                itinerary_candidate,
                itinerary_response,
            ),
            (
                brief,
                cards,
                preference,
                changed_refinement,
                refinement_response,
                itinerary_candidate,
                itinerary_response,
            ),
            (
                brief,
                cards,
                preference,
                refinement,
                changed_refinement_response,
                itinerary_candidate,
                itinerary_response,
            ),
            (
                brief,
                cards,
                preference,
                refinement,
                refinement_response,
                changed_candidate,
                itinerary_response,
            ),
            (
                brief,
                cards,
                preference,
                refinement,
                refinement_response,
                itinerary_candidate,
                changed_response,
            ),
        )
        for changed_context in changed_contexts:
            with self.subTest(changed_context=changed_context):
                with self.assertRaises(ValueError):
                    assess_guided_evidence_requirement_plan(
                        *changed_context,
                        plan,
                    )

    def test_raw_plan_schema_cannot_hold_text_dates_or_provider_payloads(self) -> None:
        self.assertEqual(
            {"source_line_index", "disposition", "topics"},
            {item.name for item in fields(GuidedLineEvidenceRequirement)},
        )
        self.assertEqual(
            {"declarations"},
            {item.name for item in fields(GuidedEvidenceRequirementPlan)},
        )
        schema_names = {
            item.name for item in fields(GuidedLineEvidenceRequirement)
        } | {item.name for item in fields(GuidedEvidenceRequirementPlan)}
        for forbidden_field in (
            "text",
            "title",
            "date",
            "time",
            "boundary_id",
            "provider_id",
            "provider_request",
            "query",
            "payload",
            "url",
            "approval",
            "confirmation",
        ):
            self.assertNotIn(forbidden_field, schema_names)

    def test_review_cannot_be_forged_or_replaced(self) -> None:
        review = assess_guided_evidence_requirement_plan(
            *_accepted_response_context(),
            _complete_plan(),
        )
        with self.assertRaises(ValueError):
            GuidedEvidencePlanReview(
                status=GuidedEvidencePlanStatus.READY_FOR_PRIVATE_PROVIDER_SCOPE,
                next_action="prepare_private_provider_scope_review",
                source_line_count=2,
                declaration_count=2,
                classified_source_line_count=2,
                requires_verification_line_count=1,
                no_external_evidence_identified_line_count=1,
                relative_day_bucket_count=2,
                transport_boundary_count=0,
                topic_counts=tuple((topic, 0) for topic in GuidedEvidenceTopic),
            )
        with self.assertRaises(ValueError):
            replace(review, needs_verification=(PRIVATE,))

    def test_safe_transcript_and_repr_redact_private_context_and_indexes(self) -> None:
        boundary = _transport_boundary()
        base_brief = _accepted_context()[0]
        brief = replace(base_brief, transport_boundaries=(boundary,))
        context = _accepted_context(brief=brief)
        itinerary_candidate = _candidate(
            (0, (0,)),
            (1, (1,)),
            boundary_ids=(boundary.boundary_id,),
        )
        itinerary_response = capture_guided_itinerary_response(
            *context,
            itinerary_candidate,
            kind=GuidedItineraryResponseKind.ACCEPT_ITINERARY_CANDIDATE,
        )
        plan = _complete_plan()
        review = assess_guided_evidence_requirement_plan(
            *context,
            itinerary_candidate,
            itinerary_response,
            plan,
        )
        self.assertEqual(1, review.transport_boundary_count)
        self.assertEqual(
            1,
            review.to_dict()["evidence_requirement_plan"][
                "transport_boundary_count"
            ],
        )
        rendered = "\n".join(
            (
                repr(plan.declarations[0]),
                repr(plan),
                repr(review),
                json.dumps(review.to_dict(), ensure_ascii=False),
            )
        )

        for private_value in (
            PRIVATE,
            "card-a",
            "card-refined",
            boundary.boundary_id,
            "source_line_index",
            "Private direction A",
            "2026-10-12",
        ):
            self.assertNotIn(private_value, rendered)

    def test_public_contract_has_no_provider_request_cli_or_apply_path(self) -> None:
        for name in (
            "GUIDED_EVIDENCE_PLAN_VERSION",
            "GuidedEvidenceDisposition",
            "GuidedEvidencePlanProblemCode",
            "GuidedEvidencePlanReview",
            "GuidedEvidencePlanStatus",
            "GuidedEvidenceRequirementPlan",
            "GuidedEvidenceTopic",
            "GuidedLineEvidenceRequirement",
            "assess_guided_evidence_requirement_plan",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        for unsupported_name in (
            "parse_guided_evidence_plan",
            "prepare_private_provider_scope_review",
            "create_guided_provider_request",
            "call_guided_evidence_provider",
            "schedule_guided_evidence_plan",
            "create_trip_from_guided_evidence_plan",
            "confirm_guided_evidence_plan",
            "apply_guided_evidence_plan",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        source = inspect.getsource(guided_evidence_plan_module)
        tree = ast.parse(source)
        allowed_from_imports = {
            (0, "__future__"),
            (0, "collections"),
            (0, "dataclasses"),
            (0, "enum"),
            (0, "typing"),
            (1, "guided_draft"),
            (1, "guided_itinerary"),
            (1, "guided_proposal"),
            (1, "guided_refinement"),
            (1, "models"),
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.fail(f"Unexpected direct import: {node.names!r}")
            elif isinstance(node, ast.ImportFrom):
                self.assertIn((node.level, node.module), allowed_from_imports)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(
                    node.func.id,
                    {"open", "exec", "eval", "compile", "__import__"},
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
