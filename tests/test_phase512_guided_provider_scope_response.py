"""Phase 5.12 contracts for exact private provider-scope responses."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace

import trip_planner
from tests.phase5_fixture_cache import reuse_immutable_default_fixture
import trip_planner.guided_provider_scope_response as scope_response_module
from tests.test_phase53_guided_draft import _transport_boundary
from tests.test_phase56_guided_refinement import PRIVATE, _preference
from tests.test_phase58_guided_itinerary import _accepted_context, _candidate
from tests.test_phase510_guided_evidence_plan import (
    _accepted_response_context,
    _complete_plan,
    _plan,
    _requirement,
)
from tests.test_phase511_guided_provider_scope import (
    _complete_scope,
    _proposal,
    _scope_item,
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
from trip_planner.guided_provider_scope import GuidedProviderCapability
from trip_planner.guided_provider_scope_response import (
    GuidedProviderScopeResponse,
    GuidedProviderScopeResponseKind,
    GuidedProviderScopeResponseReview,
    GuidedProviderScopeResponseStatus,
    assess_guided_provider_scope_response,
    capture_guided_provider_scope_response,
)
from trip_planner.guided_refinement import (
    GuidedRefinementResponseKind,
    capture_guided_refinement_response,
)


@reuse_immutable_default_fixture
def _reviewable_context():
    return (
        *_accepted_response_context(),
        _complete_plan(),
        _complete_scope(),
    )


def _capture(kind: GuidedProviderScopeResponseKind):
    context = _reviewable_context()
    response = capture_guided_provider_scope_response(
        *context,
        kind=kind,
    )
    return (*context, response)


class GuidedProviderScopeResponseTests(unittest.TestCase):
    def test_accept_scope_hands_off_only_to_private_preflight_review(self) -> None:
        context = _capture(
            GuidedProviderScopeResponseKind.ACCEPT_PROVIDER_SCOPE
        )

        review = assess_guided_provider_scope_response(*context)
        safe = review.to_dict()

        self.assertEqual(
            GuidedProviderScopeResponseStatus.READY_FOR_PRIVATE_PROVIDER_PREFLIGHT,
            review.status,
        )
        self.assertEqual(
            "prepare_private_provider_preflight_review",
            safe["next_action"],
        )
        self.assertFalse(safe["requires_user_response"])
        self.assertFalse(safe["requires_user_review"])
        self.assertFalse(safe["requires_user_decision"])
        self.assertEqual(
            {
                "kind": "accept_provider_scope",
                "accepted_for_private_preflight_review": True,
                "requested_smaller_provider_scope": False,
                "external_lookup_cancelled_for_current_scope": False,
                "may_prepare_private_provider_preflight_review": True,
                "may_refine_private_provider_scope": False,
                "evidence_requirements_preserved": True,
                "required_topic_count": 2,
                "verification_unit_count": 2,
                "scope_item_count": 2,
                "max_request_count": 5,
                "capability_counts": {
                    "google_places_current_hours": 1,
                    "google_places_identity_lookup": 1,
                    "google_routes": 0,
                    "serpapi_google_hotels": 0,
                },
                "scope_acceptance_is_provider_authorization": False,
                "pricing_checked": False,
                "provider_policy_reviewed": False,
                "credentials_accessed": False,
                "provider_scope_authorized": False,
                "provider_requests_created": False,
                "provider_calls_permitted": False,
                "is_travel_ready": False,
                "decision_state": "candidate",
                "evidence_state": "unverified",
                "supports_authoritative_use": False,
            },
            safe["provider_scope_response"],
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
                "provider_policy_reviewed": False,
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

    def test_reduce_and_cancel_have_distinct_non_authoritative_handoffs(self) -> None:
        cases = (
            (
                GuidedProviderScopeResponseKind.REQUEST_SMALLER_PROVIDER_SCOPE,
                GuidedProviderScopeResponseStatus.READY_FOR_PRIVATE_PROVIDER_SCOPE_REFINEMENT,
                "refine_private_provider_scope",
                True,
                False,
            ),
            (
                GuidedProviderScopeResponseKind.CANCEL_EXTERNAL_LOOKUP,
                GuidedProviderScopeResponseStatus.EXTERNAL_LOOKUP_CANCELLED,
                "continue_private_evidence_review",
                False,
                True,
            ),
        )
        for kind, expected_status, action, smaller, cancelled in cases:
            with self.subTest(kind=kind):
                safe = assess_guided_provider_scope_response(
                    *_capture(kind)
                ).to_dict()
                self.assertEqual(expected_status.value, safe["status"])
                self.assertEqual(action, safe["next_action"])
                response = safe["provider_scope_response"]
                self.assertEqual(smaller, response["requested_smaller_provider_scope"])
                self.assertEqual(
                    cancelled,
                    response["external_lookup_cancelled_for_current_scope"],
                )
                self.assertTrue(response["evidence_requirements_preserved"])
                self.assertFalse(
                    response["accepted_for_private_preflight_review"]
                )
                self.assertFalse(response["provider_scope_authorized"])
                self.assertFalse(response["provider_calls_permitted"])
                self.assertEqual("unverified", response["evidence_state"])
                self.assertFalse(response["is_travel_ready"])

    def test_capture_requires_exact_current_visible_nonempty_scope(self) -> None:
        context = _accepted_response_context()
        zero_plan = _plan(
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
        with self.assertRaises(ValueError):
            capture_guided_provider_scope_response(
                *context,
                zero_plan,
                _proposal(),
                kind=GuidedProviderScopeResponseKind.CANCEL_EXTERNAL_LOOKUP,
            )

        invalid_scope = _proposal(
            _scope_item(
                GuidedEvidenceTopic.PLACE_IDENTITY,
                GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP,
            )
        )
        with self.assertRaises(ValueError):
            capture_guided_provider_scope_response(
                *context,
                _complete_plan(),
                invalid_scope,
                kind=GuidedProviderScopeResponseKind.ACCEPT_PROVIDER_SCOPE,
            )
        with self.assertRaises(TypeError):
            capture_guided_provider_scope_response(
                *_reviewable_context(),
                kind="accept_provider_scope",  # type: ignore[arg-type]
            )

    def test_response_binds_every_context_layer_and_allows_card_reordering(self) -> None:
        (
            brief,
            cards,
            preference,
            refinement,
            refinement_response,
            itinerary_candidate,
            itinerary_response,
            evidence_plan,
            provider_scope,
            response,
        ) = _capture(GuidedProviderScopeResponseKind.ACCEPT_PROVIDER_SCOPE)
        original = assess_guided_provider_scope_response(
            brief,
            cards,
            preference,
            refinement,
            refinement_response,
            itinerary_candidate,
            itinerary_response,
            evidence_plan,
            provider_scope,
            response,
        ).to_dict()
        reordered = assess_guided_provider_scope_response(
            brief,
            tuple(reversed(cards)),
            preference,
            refinement,
            refinement_response,
            itinerary_candidate,
            itinerary_response,
            evidence_plan,
            provider_scope,
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
        changed_itinerary_response = capture_guided_itinerary_response(
            brief,
            cards,
            preference,
            refinement,
            refinement_response,
            changed_candidate,
            kind=GuidedItineraryResponseKind.ACCEPT_ITINERARY_CANDIDATE,
        )
        changed_evidence_plan = _plan(
            _requirement(
                0,
                disposition=(
                    GuidedEvidenceDisposition.NO_EXTERNAL_EVIDENCE_IDENTIFIED
                ),
            ),
            _requirement(
                1,
                GuidedEvidenceTopic.CURRENT_OPENING_HOURS,
                GuidedEvidenceTopic.PLACE_IDENTITY,
            ),
        )
        changed_provider_scope = _proposal(
            _scope_item(
                GuidedEvidenceTopic.CURRENT_OPENING_HOURS,
                GuidedProviderCapability.GOOGLE_PLACES_CURRENT_HOURS,
                1,
            ),
            _scope_item(
                GuidedEvidenceTopic.PLACE_IDENTITY,
                GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP,
                1,
            ),
        )
        changed_response = capture_guided_provider_scope_response(
            brief,
            cards,
            preference,
            refinement,
            refinement_response,
            itinerary_candidate,
            itinerary_response,
            evidence_plan,
            changed_provider_scope,
            kind=GuidedProviderScopeResponseKind.ACCEPT_PROVIDER_SCOPE,
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
                evidence_plan,
                provider_scope,
            ),
            (
                brief,
                changed_cards,
                preference,
                refinement,
                refinement_response,
                itinerary_candidate,
                itinerary_response,
                evidence_plan,
                provider_scope,
            ),
            (
                brief,
                cards,
                changed_preference,
                refinement,
                refinement_response,
                itinerary_candidate,
                itinerary_response,
                evidence_plan,
                provider_scope,
            ),
            (
                brief,
                cards,
                preference,
                changed_refinement,
                refinement_response,
                itinerary_candidate,
                itinerary_response,
                evidence_plan,
                provider_scope,
            ),
            (
                brief,
                cards,
                preference,
                refinement,
                changed_refinement_response,
                itinerary_candidate,
                itinerary_response,
                evidence_plan,
                provider_scope,
            ),
            (
                brief,
                cards,
                preference,
                refinement,
                refinement_response,
                changed_candidate,
                itinerary_response,
                evidence_plan,
                provider_scope,
            ),
            (
                brief,
                cards,
                preference,
                refinement,
                refinement_response,
                itinerary_candidate,
                changed_itinerary_response,
                evidence_plan,
                provider_scope,
            ),
            (
                brief,
                cards,
                preference,
                refinement,
                refinement_response,
                itinerary_candidate,
                itinerary_response,
                changed_evidence_plan,
                provider_scope,
            ),
            (
                brief,
                cards,
                preference,
                refinement,
                refinement_response,
                itinerary_candidate,
                itinerary_response,
                evidence_plan,
                changed_provider_scope,
            ),
        )
        for changed_context in changed_contexts:
            with self.subTest(changed_context=changed_context):
                with self.assertRaises(ValueError):
                    assess_guided_provider_scope_response(
                        *changed_context,
                        response,
                    )

        with self.assertRaises(ValueError):
            assess_guided_provider_scope_response(
                brief,
                cards,
                preference,
                refinement,
                refinement_response,
                itinerary_candidate,
                itinerary_response,
                evidence_plan,
                provider_scope,
                changed_response,
            )

    def test_response_schema_has_no_free_text_or_authority_fields(self) -> None:
        self.assertEqual(
            {"kind", "_context_fingerprint"},
            {item.name for item in fields(GuidedProviderScopeResponse)},
        )
        for forbidden_field in (
            "text",
            "reason",
            "line_index",
            "date",
            "provider_id",
            "query",
            "payload",
            "credential",
            "approval",
            "authorization",
            "confirmation",
        ):
            self.assertNotIn(
                forbidden_field,
                {item.name for item in fields(GuidedProviderScopeResponse)},
            )

    def test_response_and_review_cannot_be_forged_or_replaced(self) -> None:
        context = _capture(
            GuidedProviderScopeResponseKind.ACCEPT_PROVIDER_SCOPE
        )
        response = context[-1]
        review = assess_guided_provider_scope_response(*context)

        with self.assertRaises(ValueError):
            GuidedProviderScopeResponse(
                kind=GuidedProviderScopeResponseKind.ACCEPT_PROVIDER_SCOPE
            )
        with self.assertRaises(ValueError):
            replace(
                response,
                kind=(
                    GuidedProviderScopeResponseKind.CANCEL_EXTERNAL_LOOKUP
                ),
            )
        with self.assertRaises(ValueError):
            GuidedProviderScopeResponseReview(
                status=(
                    GuidedProviderScopeResponseStatus.READY_FOR_PRIVATE_PROVIDER_PREFLIGHT
                ),
                response_kind=(
                    GuidedProviderScopeResponseKind.ACCEPT_PROVIDER_SCOPE
                ),
                next_action="prepare_private_provider_preflight_review",
                required_topic_count=2,
                verification_unit_count=2,
                scope_item_count=2,
                max_request_count=5,
                capability_counts=tuple(
                    (capability, 0)
                    for capability in GuidedProviderCapability
                ),
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
        provider_scope = _complete_scope()
        response = capture_guided_provider_scope_response(
            *context,
            itinerary_candidate,
            itinerary_response,
            evidence_plan,
            provider_scope,
            kind=GuidedProviderScopeResponseKind.ACCEPT_PROVIDER_SCOPE,
        )
        review = assess_guided_provider_scope_response(
            *context,
            itinerary_candidate,
            itinerary_response,
            evidence_plan,
            provider_scope,
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
            boundary.boundary_id,
            "source_line_index",
            "Private direction A",
            "2026-10-12",
        ):
            self.assertNotIn(private_value, rendered)

    def test_public_contract_has_no_parser_preflight_request_or_apply_path(self) -> None:
        for name in (
            "GUIDED_PROVIDER_SCOPE_RESPONSE_VERSION",
            "GuidedProviderScopeResponse",
            "GuidedProviderScopeResponseKind",
            "GuidedProviderScopeResponseReview",
            "GuidedProviderScopeResponseStatus",
            "assess_guided_provider_scope_response",
            "capture_guided_provider_scope_response",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        for unsupported_name in (
            "parse_guided_provider_scope_response",
            "prepare_private_provider_preflight_review",
            "build_guided_provider_request",
            "authorize_guided_provider_scope",
            "execute_guided_provider_request",
            "call_guided_provider",
            "schedule_guided_provider_scope_response",
            "create_trip_from_guided_provider_scope_response",
            "apply_guided_provider_scope_response",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        source = inspect.getsource(scope_response_module)
        tree = ast.parse(source)
        allowed_imports = {"hashlib", "json", "re"}
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
            (1, "guided_provider_scope"),
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
