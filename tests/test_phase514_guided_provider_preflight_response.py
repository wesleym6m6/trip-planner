"""Phase 5.14 exact response handoff for fresh provider preflight reviews."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace
from datetime import timedelta

import trip_planner
import trip_planner.guided_provider_preflight_response as response_module
from tests.test_phase513_guided_provider_preflight import (
    CHECKED_AT,
    EVALUATION_AT,
    EXPIRES_AT,
    _complete_google_items,
    _prepared,
)
from tests.test_phase510_guided_evidence_plan import _plan, _requirement
from tests.test_phase511_guided_provider_scope import _proposal, _scope_item
from tests.test_phase56_guided_refinement import PRIVATE, _preference
from tests.test_phase58_guided_itinerary import _candidate
from trip_planner.guided_evidence_plan import (
    GuidedEvidenceDisposition,
    GuidedEvidenceTopic,
)
from trip_planner.guided_itinerary import (
    GuidedItineraryResponseKind,
    capture_guided_itinerary_response,
)
from trip_planner.guided_proposal import GuidedDirectionPreferenceKind
from trip_planner.guided_provider_preflight import (
    GuidedProviderCredentialStatus,
    GuidedProviderPreflightItem,
    prepare_guided_provider_preflight,
)
from trip_planner.guided_provider_preflight_response import (
    GuidedProviderPreflightResponse,
    GuidedProviderPreflightResponseKind,
    GuidedProviderPreflightResponseReview,
    GuidedProviderPreflightResponseStatus,
    assess_guided_provider_preflight_response,
    capture_guided_provider_preflight_response,
)
from trip_planner.guided_provider_scope import GuidedProviderCapability
from trip_planner.guided_provider_scope_response import (
    GuidedProviderScopeResponseKind,
    capture_guided_provider_scope_response,
)
from trip_planner.guided_refinement import (
    GuidedRefinementResponseKind,
    capture_guided_refinement_response,
)


ASSESSMENT_AT = EVALUATION_AT + timedelta(minutes=1)


def _captured(
    kind: GuidedProviderPreflightResponseKind,
    *,
    context: tuple[object, ...] | None = None,
    evaluation_at=EVALUATION_AT,
):
    exact_context = _prepared() if context is None else context
    response = capture_guided_provider_preflight_response(
        *exact_context,
        kind=kind,
        evaluation_at=evaluation_at,
    )
    return (*exact_context, response)


class GuidedProviderPreflightResponseTests(unittest.TestCase):
    def test_accept_handoff_is_non_authoritative_and_preserves_aggregates(
        self,
    ) -> None:
        context = _captured(
            GuidedProviderPreflightResponseKind.ACCEPT_PROVIDER_PREFLIGHT
        )

        review = assess_guided_provider_preflight_response(
            *context,
            evaluation_at=ASSESSMENT_AT,
        )
        safe = review.to_dict()

        self.assertEqual(
            GuidedProviderPreflightResponseStatus
            .READY_FOR_PRIVATE_PROVIDER_EXECUTION_AUTHORIZATION,
            review.status,
        )
        self.assertEqual(
            "prepare_private_provider_execution_authorization",
            safe["next_action"],
        )
        self.assertFalse(safe["requires_user_response"])
        self.assertFalse(safe["requires_user_review"])
        self.assertFalse(safe["requires_user_decision"])
        handoff = safe["provider_preflight_response"]
        self.assertEqual("accept_provider_preflight", handoff["kind"])
        self.assertTrue(handoff["exact_private_context_bound"])
        self.assertTrue(
            handoff["accepted_for_execution_authorization_preparation"]
        )
        self.assertTrue(
            handoff["may_prepare_private_provider_execution_authorization"]
        )
        self.assertFalse(handoff["requested_smaller_provider_preflight"])
        self.assertFalse(
            handoff["external_execution_cancelled_for_current_scope"]
        )
        self.assertTrue(handoff["evidence_requirements_preserved"])
        self.assertTrue(handoff["provider_scope_preserved"])
        self.assertTrue(handoff["preflight_unchanged"])
        self.assertTrue(handoff["host_attestation_fresh"])
        self.assertEqual(2, handoff["accepted_scope_item_count"])
        self.assertEqual(5, handoff["accepted_max_request_count"])
        self.assertEqual(2, handoff["preflight_item_count"])
        self.assertEqual(5, handoff["max_request_count"])
        self.assertEqual(
            {
                "google_places_current_hours": 1,
                "google_places_identity_lookup": 1,
                "google_routes": 0,
                "serpapi_google_hotels": 0,
            },
            handoff["capability_counts"],
        )
        self.assertEqual(136_000, handoff[
            "estimated_first_paid_tier_google_cost_usd_micros"
        ])
        self.assertEqual(0, handoff["serpapi_plan_credit_cap"])
        self.assertTrue(
            handoff["all_provider_costs_have_currency_list_rate_estimates"]
        )
        self.assertFalse(handoff["monthly_free_usage_remaining_checked"])
        self.assertFalse(handoff["cost_estimate_is_hard_currency_cap"])
        self.assertTrue(handoff["host_pricing_profile_attested"])
        self.assertFalse(handoff["pricing_verified_by_response_contract"])
        self.assertTrue(handoff["host_provider_policy_profile_attested"])
        self.assertFalse(
            handoff["provider_policy_verified_by_response_contract"]
        )
        self.assertTrue(handoff["billing_region_attested"])
        self.assertTrue(
            handoff["expected_provider_retention_profile_attested"]
        )
        self.assertFalse(
            handoff["provider_retention_verified_by_response_contract"]
        )
        self.assertTrue(handoff["credential_availability_attested"])
        self.assertTrue(handoff["credentials_available"])
        self.assertFalse(handoff["credentials_accessed"])
        self.assertTrue(handoff["credential_availability_recheck_required"])
        self.assertTrue(handoff["execution_time_recheck_required"])
        self.assertTrue(
            handoff[
                "explicit_execution_authorization_required_before_any_call"
            ]
        )
        self.assertFalse(
            handoff["preflight_acceptance_is_provider_authorization"]
        )
        self.assertFalse(handoff["provider_scope_authorized"])
        self.assertFalse(handoff["provider_requests_created"])
        self.assertFalse(handoff["provider_calls_permitted"])
        self.assertFalse(handoff["is_travel_ready"])
        self.assertEqual("candidate", handoff["decision_state"])
        self.assertEqual("unverified", handoff["evidence_state"])
        self.assertFalse(handoff["supports_authoritative_use"])
        self.assertIn("provider_preflight", safe["needs_verification"])
        self.assertEqual(
            {
                "process_local": True,
                "official_sources_fetched_by_contract": False,
                "environment_read": False,
                "vault_accessed": False,
                "credentials_accessed": False,
                "provider_requests_created": False,
                "provider_calls": False,
                "writes_to_trip": False,
                "scheduled": False,
                "trip_created": False,
                "rendered": False,
                "deployed": False,
            },
            safe["side_effects"],
        )

    def test_reduce_and_cancel_are_typed_without_mutating_context(self) -> None:
        cases = (
            (
                GuidedProviderPreflightResponseKind
                .REQUEST_SMALLER_PROVIDER_PREFLIGHT,
                "ready_for_private_provider_preflight_refinement",
                "refine_private_provider_preflight",
                True,
                False,
            ),
            (
                GuidedProviderPreflightResponseKind.CANCEL_EXTERNAL_EXECUTION,
                "external_execution_cancelled",
                "continue_private_evidence_review",
                False,
                True,
            ),
        )
        for kind, status, action, smaller, cancelled in cases:
            with self.subTest(kind=kind):
                context = _captured(kind)
                original_scope = context[-4]
                original_preflight = context[-2]
                safe = assess_guided_provider_preflight_response(
                    *context,
                    evaluation_at=ASSESSMENT_AT,
                ).to_dict()
                self.assertEqual(status, safe["status"])
                self.assertEqual(action, safe["next_action"])
                handoff = safe["provider_preflight_response"]
                self.assertEqual(smaller, handoff[
                    "requested_smaller_provider_preflight"
                ])
                self.assertEqual(cancelled, handoff[
                    "external_execution_cancelled_for_current_scope"
                ])
                self.assertTrue(handoff["evidence_requirements_preserved"])
                self.assertTrue(handoff["provider_scope_preserved"])
                self.assertTrue(handoff["preflight_unchanged"])
                self.assertIs(original_scope, context[-4])
                self.assertIs(original_preflight, context[-2])
                self.assertFalse(handoff["provider_calls_permitted"])

    def test_blocked_or_expired_preflight_cannot_capture_response(self) -> None:
        unavailable = _prepared(
            items=_complete_google_items(
                credential_status=GuidedProviderCredentialStatus.UNAVAILABLE
            )
        )
        with self.assertRaises(ValueError):
            capture_guided_provider_preflight_response(
                *unavailable,
                kind=(
                    GuidedProviderPreflightResponseKind.ACCEPT_PROVIDER_PREFLIGHT
                ),
                evaluation_at=EVALUATION_AT,
            )
        with self.assertRaises(ValueError):
            capture_guided_provider_preflight_response(
                *_prepared(),
                kind=(
                    GuidedProviderPreflightResponseKind.ACCEPT_PROVIDER_PREFLIGHT
                ),
                evaluation_at=EXPIRES_AT,
            )
        structural = _prepared(items=(_complete_google_items()[0],))
        with self.assertRaises(ValueError):
            capture_guided_provider_preflight_response(
                *structural,
                kind=(
                    GuidedProviderPreflightResponseKind.ACCEPT_PROVIDER_PREFLIGHT
                ),
                evaluation_at=EVALUATION_AT,
            )
        with self.assertRaises(TypeError):
            capture_guided_provider_preflight_response(
                *_prepared(),
                kind="accept_provider_preflight",
                evaluation_at=EVALUATION_AT,
            )

    def test_response_cannot_replay_after_expiry_or_before_capture(self) -> None:
        context = _captured(
            GuidedProviderPreflightResponseKind.ACCEPT_PROVIDER_PREFLIGHT
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_preflight_response(
                *context,
                evaluation_at=EXPIRES_AT,
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_preflight_response(
                *context,
                evaluation_at=EVALUATION_AT - timedelta(microseconds=1),
            )
        with self.assertRaises(ValueError):
            capture_guided_provider_preflight_response(
                *_prepared(),
                kind=(
                    GuidedProviderPreflightResponseKind.ACCEPT_PROVIDER_PREFLIGHT
                ),
                evaluation_at=EVALUATION_AT.replace(tzinfo=None),
            )

    def test_response_binds_every_context_layer_and_allows_card_reordering(
        self,
    ) -> None:
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
            scope_response,
            preflight,
            response,
        ) = _captured(
            GuidedProviderPreflightResponseKind.ACCEPT_PROVIDER_PREFLIGHT
        )
        original = assess_guided_provider_preflight_response(
            brief,
            cards,
            preference,
            refinement,
            refinement_response,
            itinerary_candidate,
            itinerary_response,
            evidence_plan,
            provider_scope,
            scope_response,
            preflight,
            response,
            evaluation_at=ASSESSMENT_AT,
        ).to_dict()
        reordered = assess_guided_provider_preflight_response(
            brief,
            tuple(reversed(cards)),
            preference,
            refinement,
            refinement_response,
            itinerary_candidate,
            itinerary_response,
            evidence_plan,
            provider_scope,
            scope_response,
            preflight,
            response,
            evaluation_at=ASSESSMENT_AT,
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
        changed_scope_response = capture_guided_provider_scope_response(
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
                scope_response,
                preflight,
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
                scope_response,
                preflight,
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
                scope_response,
                preflight,
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
                scope_response,
                preflight,
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
                scope_response,
                preflight,
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
                scope_response,
                preflight,
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
                scope_response,
                preflight,
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
                scope_response,
                preflight,
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
                scope_response,
                preflight,
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
                provider_scope,
                changed_scope_response,
                preflight,
            ),
        )
        for changed_context in changed_contexts:
            with self.subTest(changed_context=changed_context):
                with self.assertRaises(ValueError):
                    assess_guided_provider_preflight_response(
                        *changed_context,
                        response,
                        evaluation_at=ASSESSMENT_AT,
                    )

    def test_response_binds_exact_preflight_profiles_and_caps(self) -> None:
        context = _prepared()
        response = capture_guided_provider_preflight_response(
            *context,
            kind=GuidedProviderPreflightResponseKind.ACCEPT_PROVIDER_PREFLIGHT,
            evaluation_at=EVALUATION_AT,
        )
        items = _complete_google_items()
        changed_items = (
            items[0],
            replace(items[1], max_request_count=2),
        )
        changed_preflight = prepare_guided_provider_preflight(
            *context[:-1],
            items=changed_items,
            checked_at=CHECKED_AT,
            expires_at=EXPIRES_AT,
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_preflight_response(
                *context[:-1],
                changed_preflight,
                response,
                evaluation_at=ASSESSMENT_AT,
            )
        shifted_preflight = prepare_guided_provider_preflight(
            *context[:-1],
            items=items,
            checked_at=CHECKED_AT + timedelta(minutes=1),
            expires_at=EXPIRES_AT,
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_preflight_response(
                *context[:-1],
                shifted_preflight,
                response,
                evaluation_at=ASSESSMENT_AT,
            )

    def test_response_and_review_cannot_be_forged_or_replaced(self) -> None:
        context = _captured(
            GuidedProviderPreflightResponseKind.ACCEPT_PROVIDER_PREFLIGHT
        )
        response = context[-1]
        review = assess_guided_provider_preflight_response(
            *context,
            evaluation_at=ASSESSMENT_AT,
        )
        with self.assertRaises(ValueError):
            GuidedProviderPreflightResponse(
                kind=(
                    GuidedProviderPreflightResponseKind.ACCEPT_PROVIDER_PREFLIGHT
                ),
                _reviewed_at=EVALUATION_AT,
            )
        with self.assertRaises(ValueError):
            replace(
                response,
                kind=(
                    GuidedProviderPreflightResponseKind.CANCEL_EXTERNAL_EXECUTION
                ),
            )
        tampered = object.__new__(GuidedProviderPreflightResponse)
        object.__setattr__(
            tampered,
            "kind",
            GuidedProviderPreflightResponseKind.CANCEL_EXTERNAL_EXECUTION,
        )
        object.__setattr__(tampered, "_reviewed_at", response._reviewed_at)
        object.__setattr__(
            tampered,
            "_context_fingerprint",
            response._context_fingerprint,
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_preflight_response(
                *context[:-1],
                tampered,
                evaluation_at=ASSESSMENT_AT,
            )
        with self.assertRaises(ValueError):
            GuidedProviderPreflightResponseReview(
                status=(
                    GuidedProviderPreflightResponseStatus
                    .READY_FOR_PRIVATE_PROVIDER_EXECUTION_AUTHORIZATION
                ),
                response_kind=(
                    GuidedProviderPreflightResponseKind.ACCEPT_PROVIDER_PREFLIGHT
                ),
                next_action="prepare_private_provider_execution_authorization",
                accepted_scope_item_count=2,
                accepted_max_request_count=5,
                preflight_item_count=2,
                max_request_count=5,
                host_attestation_fresh=True,
            )
        with self.assertRaises(ValueError):
            replace(review, next_action="execute_guided_provider_request")

    def test_safe_output_and_schema_exclude_private_material(self) -> None:
        context = _captured(
            GuidedProviderPreflightResponseKind.ACCEPT_PROVIDER_PREFLIGHT
        )
        response = context[-1]
        review = assess_guided_provider_preflight_response(
            *context,
            evaluation_at=ASSESSMENT_AT,
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
            "source_line_index",
            "Private direction A",
            "2026-10-12",
            CHECKED_AT.isoformat(),
            EVALUATION_AT.isoformat(),
            EXPIRES_AT.isoformat(),
        ):
            self.assertNotIn(private_value, rendered)
        self.assertEqual(
            {"kind", "_reviewed_at", "_context_fingerprint"},
            {item.name for item in fields(GuidedProviderPreflightResponse)},
        )
        forbidden_fields = {
            "query",
            "payload",
            "url",
            "api_key",
            "secret",
            "provider_resource_id",
            "place_id",
            "raw_policy_text",
            "billing_address",
            "authorization",
            "confirmation",
        }
        self.assertTrue(
            forbidden_fields.isdisjoint(
                {item.name for item in fields(GuidedProviderPreflightResponse)}
            )
        )

    def test_public_contract_has_no_request_execution_or_mutation_path(self) -> None:
        for name in (
            "GUIDED_PROVIDER_PREFLIGHT_RESPONSE_VERSION",
            "GuidedProviderPreflightResponse",
            "GuidedProviderPreflightResponseKind",
            "GuidedProviderPreflightResponseReview",
            "GuidedProviderPreflightResponseStatus",
            "assess_guided_provider_preflight_response",
            "capture_guided_provider_preflight_response",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        for unsupported_name in (
            "parse_guided_provider_preflight_response",
            "build_guided_provider_request",
            "authorize_guided_provider_execution",
            "execute_guided_provider_request",
            "call_guided_provider",
            "schedule_guided_provider_preflight_response",
            "create_trip_from_guided_provider_preflight_response",
            "apply_guided_provider_preflight_response",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        source = inspect.getsource(response_module)
        tree = ast.parse(source)
        allowed_imports = {"hashlib", "json", "re"}
        allowed_from_imports = {
            (0, "__future__"),
            (0, "collections"),
            (0, "dataclasses"),
            (0, "datetime"),
            (0, "enum"),
            (0, "typing"),
            (1, "guided_draft"),
            (1, "guided_evidence_plan"),
            (1, "guided_itinerary"),
            (1, "guided_proposal"),
            (1, "guided_provider_preflight"),
            (1, "guided_provider_scope"),
            (1, "guided_provider_scope_response"),
            (1, "guided_refinement"),
            (1, "models"),
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name, allowed_imports)
            elif isinstance(node, ast.ImportFrom):
                self.assertIn((node.level, node.module), allowed_from_imports)
        for forbidden in (
            "os.environ",
            "getenv",
            "urlopen",
            "requests.",
            "subprocess",
            "Path(",
            "open(",
            "EvidenceStore",
            "ProviderRequest(",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
