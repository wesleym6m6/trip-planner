"""Phase 5.11 contracts for bounded private provider-scope review."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace

import trip_planner
import trip_planner.guided_provider_scope as guided_provider_scope_module
from tests.test_phase53_guided_draft import _transport_boundary
from tests.test_phase56_guided_refinement import PRIVATE, _preference
from tests.test_phase58_guided_itinerary import _accepted_context, _candidate
from tests.test_phase59_guided_itinerary_response import _capture
from tests.test_phase510_guided_evidence_plan import (
    _accepted_response_context,
    _complete_plan,
    _plan,
    _requirement,
)
from trip_planner.guided_evidence_plan import (
    GuidedEvidenceDisposition,
    GuidedEvidenceTopic,
)
from trip_planner.guided_itinerary import (
    GuidedItineraryResponseKind,
    capture_guided_itinerary_response,
)
from trip_planner.guided_proposal import GuidedDirectionPreferenceKind
from trip_planner.guided_provider_scope import (
    GuidedProviderCapability,
    GuidedProviderDataCategory,
    GuidedProviderScopeItem,
    GuidedProviderScopeProblemCode,
    GuidedProviderScopeProposal,
    GuidedProviderScopeReview,
    GuidedProviderScopeStatus,
    assess_guided_provider_scope,
)
from trip_planner.guided_refinement import (
    GuidedRefinementResponseKind,
    capture_guided_refinement_response,
)
from trip_planner.models import DecisionState, EvidenceState


def _scope_item(
    topic: GuidedEvidenceTopic,
    capability: GuidedProviderCapability,
    max_request_count: int = 1,
) -> GuidedProviderScopeItem:
    return GuidedProviderScopeItem(
        topic=topic,
        capability=capability,
        max_request_count=max_request_count,
    )


def _proposal(
    *items: GuidedProviderScopeItem,
) -> GuidedProviderScopeProposal:
    return GuidedProviderScopeProposal(items=items)


def _complete_scope() -> GuidedProviderScopeProposal:
    return _proposal(
        _scope_item(
            GuidedEvidenceTopic.CURRENT_OPENING_HOURS,
            GuidedProviderCapability.GOOGLE_PLACES_CURRENT_HOURS,
            2,
        ),
        _scope_item(
            GuidedEvidenceTopic.PLACE_IDENTITY,
            GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP,
            3,
        ),
    )


class GuidedProviderScopeTests(unittest.TestCase):
    def test_complete_scope_reaches_one_private_subjective_review(self) -> None:
        context = _accepted_response_context()
        evidence_plan = _complete_plan()
        proposal = _complete_scope()

        review = assess_guided_provider_scope(
            *context,
            evidence_plan,
            proposal,
        )
        safe = review.to_dict()

        self.assertEqual(GuidedProviderScopeStatus.REVIEW_REQUIRED, review.status)
        self.assertEqual("review_private_provider_scope", safe["next_action"])
        self.assertTrue(safe["requires_user_response"])
        self.assertTrue(safe["requires_user_review"])
        self.assertTrue(safe["requires_user_decision"])
        self.assertTrue(safe["may_present_provider_scope"])
        self.assertEqual(
            "是否同意依照這個外部查證範圍繼續？你可以接受、縮小範圍或取消。",
            safe["review_prompt"],
        )
        self.assertIn("不等於授權執行", safe["review_disclosure"])
        self.assertIsNone(safe["no_provider_scope_disclosure"])
        self.assertEqual([], safe["problems"])
        self.assertEqual(
            {
                "required_topic_count": 2,
                "verification_unit_count": 2,
                "scope_item_count": 2,
                "distinct_scoped_topic_count": 2,
                "items": [
                    {
                        "topic": "current_opening_hours",
                        "capability": "google_places_current_hours",
                        "max_request_count": 2,
                    },
                    {
                        "topic": "place_identity",
                        "capability": "google_places_identity_lookup",
                        "max_request_count": 3,
                    },
                ],
                "data_categories": [
                    "destination_context",
                    "place_identity_context",
                    "place_search_context",
                    "travel_date_context",
                ],
                "max_request_count": 5,
                "hard_request_cap": 32,
                "within_hard_request_cap": True,
                "request_cap_is_currency_cost_limit": False,
                "potentially_billable": True,
                "pricing_verified": False,
                "must_check_current_pricing_before_call": True,
                "scope_plan_retention": "process_local_only",
                "provider_policy_review_required_before_call": True,
                "provider_terms_and_retention_may_apply": True,
                "host_managed_credentials_required_before_call": True,
                "no_provider_scope_required": False,
                "no_external_evidence_identified_means_verified": False,
                "contains_line_indexes": False,
                "contains_private_values": False,
                "contains_provider_resource_identifiers": False,
                "contains_provider_query": False,
                "contains_provider_payload": False,
                "provider_scope_authorized": False,
                "provider_requests_created": False,
                "provider_calls_permitted": False,
                "decision_state": "candidate",
                "evidence_state": "unverified",
                "supports_authoritative_use": False,
            },
            safe["provider_scope"],
        )
        self.assertEqual(
            [
                "destination_location",
                "proposal_candidates",
                "itinerary_candidate",
                "evidence_requirements",
                "provider_scope",
            ],
            safe["needs_verification"],
        )
        self.assertEqual(
            {
                "process_local": True,
                "credentials_accessed": False,
                "pricing_checked": False,
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
        self.assertEqual(DecisionState.CANDIDATE, proposal.decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, proposal.evidence_state)

    def test_capability_mapping_and_data_disclosures_are_fixed(self) -> None:
        context = _accepted_response_context()
        evidence_plan = _plan(
            _requirement(0, *tuple(GuidedEvidenceTopic)),
            _requirement(
                1,
                disposition=(
                    GuidedEvidenceDisposition.NO_EXTERNAL_EVIDENCE_IDENTIFIED
                ),
            ),
        )
        proposal = _proposal(
            _scope_item(
                GuidedEvidenceTopic.PLACE_IDENTITY,
                GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP,
            ),
            _scope_item(
                GuidedEvidenceTopic.CURRENT_OPENING_HOURS,
                GuidedProviderCapability.GOOGLE_PLACES_CURRENT_HOURS,
            ),
            _scope_item(
                GuidedEvidenceTopic.ROUTE,
                GuidedProviderCapability.GOOGLE_ROUTES,
            ),
            _scope_item(
                GuidedEvidenceTopic.LODGING,
                GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
            ),
            _scope_item(
                GuidedEvidenceTopic.AVAILABILITY,
                GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
            ),
            _scope_item(
                GuidedEvidenceTopic.PRICE,
                GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
            ),
        )

        safe = assess_guided_provider_scope(
            *context,
            evidence_plan,
            proposal,
        ).to_dict()

        self.assertEqual("review_required", safe["status"])
        self.assertEqual(6, safe["provider_scope"]["verification_unit_count"])
        self.assertEqual(
            [item.value for item in GuidedProviderDataCategory],
            safe["provider_scope"]["data_categories"],
        )

    def test_missing_extra_duplicate_mismatch_and_excess_limit_fail_closed(self) -> None:
        context = _accepted_response_context()
        evidence_plan = _complete_plan()
        cases = (
            (
                _proposal(
                    _scope_item(
                        GuidedEvidenceTopic.PLACE_IDENTITY,
                        GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP,
                    )
                ),
                (GuidedProviderScopeProblemCode.REQUIRED_TOPIC_NOT_SCOPED,),
            ),
            (
                _proposal(
                    *_complete_scope().items,
                    _scope_item(
                        GuidedEvidenceTopic.ROUTE,
                        GuidedProviderCapability.GOOGLE_ROUTES,
                    ),
                ),
                (GuidedProviderScopeProblemCode.UNREQUIRED_TOPIC_SCOPED,),
            ),
            (
                _proposal(
                    *_complete_scope().items,
                    _scope_item(
                        GuidedEvidenceTopic.PLACE_IDENTITY,
                        GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP,
                    ),
                ),
                (
                    GuidedProviderScopeProblemCode.TOPIC_SCOPED_MULTIPLE_TIMES,
                ),
            ),
            (
                _proposal(
                    _scope_item(
                        GuidedEvidenceTopic.CURRENT_OPENING_HOURS,
                        GuidedProviderCapability.GOOGLE_PLACES_CURRENT_HOURS,
                    ),
                    _scope_item(
                        GuidedEvidenceTopic.PLACE_IDENTITY,
                        GuidedProviderCapability.GOOGLE_ROUTES,
                    ),
                ),
                (GuidedProviderScopeProblemCode.CAPABILITY_TOPIC_MISMATCH,),
            ),
            (
                _proposal(
                    _scope_item(
                        GuidedEvidenceTopic.CURRENT_OPENING_HOURS,
                        GuidedProviderCapability.GOOGLE_PLACES_CURRENT_HOURS,
                        16,
                    ),
                    _scope_item(
                        GuidedEvidenceTopic.PLACE_IDENTITY,
                        GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP,
                        17,
                    ),
                ),
                (GuidedProviderScopeProblemCode.TOTAL_REQUEST_LIMIT_EXCEEDED,),
            ),
        )

        for proposal, expected_problems in cases:
            with self.subTest(expected_problems=expected_problems):
                review = assess_guided_provider_scope(
                    *context,
                    evidence_plan,
                    proposal,
                )
                safe = review.to_dict()
                self.assertEqual(
                    GuidedProviderScopeStatus.NEEDS_REFINEMENT,
                    review.status,
                )
                self.assertEqual(
                    "refine_private_provider_scope",
                    safe["next_action"],
                )
                self.assertEqual(
                    [item.value for item in expected_problems],
                    safe["problems"],
                )
                self.assertFalse(safe["requires_user_response"])
                self.assertEqual([], safe["provider_scope"]["items"])
                self.assertEqual(
                    [],
                    safe["provider_scope"]["data_categories"],
                )
                self.assertFalse(
                    safe["provider_scope"]["provider_scope_authorized"]
                )
                self.assertFalse(safe["side_effects"]["provider_calls"])

    def test_zero_topic_empty_scope_needs_no_user_response_but_stays_unverified(self) -> None:
        context = _accepted_response_context()
        evidence_plan = _plan(
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

        review = assess_guided_provider_scope(
            *context,
            evidence_plan,
            _proposal(),
        )
        safe = review.to_dict()

        self.assertEqual(
            GuidedProviderScopeStatus.NO_PROVIDER_SCOPE_REQUIRED,
            review.status,
        )
        self.assertEqual("continue_private_evidence_review", safe["next_action"])
        self.assertFalse(safe["requires_user_response"])
        self.assertFalse(safe["requires_user_review"])
        self.assertFalse(safe["requires_user_decision"])
        self.assertFalse(safe["may_present_provider_scope"])
        self.assertIsNone(safe["review_prompt"])
        self.assertIsNone(safe["review_disclosure"])
        self.assertIn(
            "不表示資料已驗證",
            safe["no_provider_scope_disclosure"],
        )
        self.assertTrue(
            safe["provider_scope"]["no_provider_scope_required"]
        )
        self.assertFalse(safe["provider_scope"]["potentially_billable"])
        self.assertFalse(safe["provider_scope"]["pricing_verified"])
        self.assertFalse(
            safe["provider_scope"][
                "no_external_evidence_identified_means_verified"
            ]
        )
        self.assertEqual("candidate", safe["provider_scope"]["decision_state"])
        self.assertEqual("unverified", safe["provider_scope"]["evidence_state"])
        self.assertFalse(
            safe["provider_scope"]["supports_authoritative_use"]
        )

    def test_zero_topic_plan_rejects_any_external_scope_item(self) -> None:
        context = _accepted_response_context()
        evidence_plan = _plan(
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
        proposal = _proposal(
            _scope_item(
                GuidedEvidenceTopic.ROUTE,
                GuidedProviderCapability.GOOGLE_ROUTES,
            )
        )

        safe = assess_guided_provider_scope(
            *context,
            evidence_plan,
            proposal,
        ).to_dict()

        self.assertEqual("needs_refinement", safe["status"])
        self.assertEqual(["unrequired_topic_scoped"], safe["problems"])
        self.assertFalse(safe["provider_scope"]["no_provider_scope_required"])
        self.assertFalse(safe["provider_scope"]["potentially_billable"])

    def test_scope_requires_exact_ready_phase510_context(self) -> None:
        with self.assertRaises(ValueError):
            assess_guided_provider_scope(
                *_accepted_response_context(),
                _plan(_requirement(0, GuidedEvidenceTopic.PLACE_IDENTITY)),
                _proposal(
                    _scope_item(
                        GuidedEvidenceTopic.PLACE_IDENTITY,
                        GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP,
                    )
                ),
            )

        adjustment_context = _capture(
            GuidedItineraryResponseKind.REQUEST_ITINERARY_ADJUSTMENT
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_scope(
                *adjustment_context,
                _complete_plan(),
                _complete_scope(),
            )
        with self.assertRaises(TypeError):
            assess_guided_provider_scope(
                *_accepted_response_context(),
                _complete_plan(),
                object(),  # type: ignore[arg-type]
            )

    def test_every_upstream_context_layer_is_revalidated_again(self) -> None:
        (
            brief,
            cards,
            preference,
            refinement,
            refinement_response,
            itinerary_candidate,
            itinerary_response,
        ) = _accepted_response_context()
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
                    assess_guided_provider_scope(
                        *changed_context,
                        _complete_plan(),
                        _complete_scope(),
                    )

    def test_scope_schema_has_no_request_values_or_authority(self) -> None:
        self.assertEqual(
            {"topic", "capability", "max_request_count"},
            {item.name for item in fields(GuidedProviderScopeItem)},
        )
        self.assertEqual(
            {"items"},
            {item.name for item in fields(GuidedProviderScopeProposal)},
        )
        schema_names = {
            item.name for item in fields(GuidedProviderScopeItem)
        } | {item.name for item in fields(GuidedProviderScopeProposal)}
        for forbidden_field in (
            "text",
            "line_index",
            "date",
            "place_id",
            "provider_id",
            "query",
            "payload",
            "url",
            "credential",
            "api_key",
            "approval",
            "authorization",
            "confirmation",
        ):
            self.assertNotIn(forbidden_field, schema_names)

        with self.assertRaises(TypeError):
            GuidedProviderScopeItem(
                topic="route",  # type: ignore[arg-type]
                capability=GuidedProviderCapability.GOOGLE_ROUTES,
                max_request_count=1,
            )
        with self.assertRaises(TypeError):
            GuidedProviderScopeItem(
                topic=GuidedEvidenceTopic.ROUTE,
                capability="google_routes",  # type: ignore[arg-type]
                max_request_count=1,
            )
        for invalid_limit in (True, 0, 33):
            with self.subTest(invalid_limit=invalid_limit):
                with self.assertRaises(ValueError):
                    _scope_item(
                        GuidedEvidenceTopic.ROUTE,
                        GuidedProviderCapability.GOOGLE_ROUTES,
                        invalid_limit,
                    )
        with self.assertRaises(TypeError):
            GuidedProviderScopeProposal(items=[])  # type: ignore[arg-type]

    def test_review_cannot_be_forged_or_replaced(self) -> None:
        review = assess_guided_provider_scope(
            *_accepted_response_context(),
            _complete_plan(),
            _complete_scope(),
        )
        with self.assertRaises(ValueError):
            GuidedProviderScopeReview(
                status=GuidedProviderScopeStatus.REVIEW_REQUIRED,
                next_action="review_private_provider_scope",
                required_topic_count=2,
                verification_unit_count=2,
                scope_item_count=2,
                distinct_scoped_topic_count=2,
                max_request_count=5,
                scope_items=_complete_scope().items,
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
        itinerary_response = capture_guided_itinerary_response(
            *context,
            itinerary_candidate,
            kind=GuidedItineraryResponseKind.ACCEPT_ITINERARY_CANDIDATE,
        )
        evidence_plan = _complete_plan()
        proposal = _complete_scope()
        review = assess_guided_provider_scope(
            *context,
            itinerary_candidate,
            itinerary_response,
            evidence_plan,
            proposal,
        )
        rendered = "\n".join(
            (
                repr(proposal.items[0]),
                repr(proposal),
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

    def test_public_contract_has_no_request_execution_or_apply_path(self) -> None:
        for name in (
            "GUIDED_PROVIDER_SCOPE_VERSION",
            "GuidedProviderCapability",
            "GuidedProviderDataCategory",
            "GuidedProviderScopeItem",
            "GuidedProviderScopeProblemCode",
            "GuidedProviderScopeProposal",
            "GuidedProviderScopeReview",
            "GuidedProviderScopeStatus",
            "assess_guided_provider_scope",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        for unsupported_name in (
            "parse_guided_provider_scope_response",
            "prepare_guided_provider_requests",
            "authorize_guided_provider_scope",
            "execute_guided_provider_scope",
            "call_guided_provider",
            "schedule_guided_provider_scope",
            "create_trip_from_guided_provider_scope",
            "apply_guided_provider_scope",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        source = inspect.getsource(guided_provider_scope_module)
        tree = ast.parse(source)
        allowed_from_imports = {
            (0, "__future__"),
            (0, "collections"),
            (0, "dataclasses"),
            (0, "enum"),
            (0, "typing"),
            (1, "guided_draft"),
            (1, "guided_evidence_plan"),
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
