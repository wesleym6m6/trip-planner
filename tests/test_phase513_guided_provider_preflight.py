"""Phase 5.13 contracts for exact offline provider preflight attestations."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import trip_planner
import trip_planner.guided_provider_preflight as preflight_module
from tests.test_phase510_guided_evidence_plan import (
    _accepted_response_context,
    _plan,
    _requirement,
)
from tests.test_phase511_guided_provider_scope import _proposal, _scope_item
from tests.test_phase512_guided_provider_scope_response import _capture
from tests.test_phase56_guided_refinement import PRIVATE
from trip_planner.guided_evidence_plan import (
    GuidedEvidenceDisposition,
    GuidedEvidenceTopic,
)
from trip_planner.guided_provider_preflight import (
    GuidedProviderBillingRegion,
    GuidedProviderCredentialStatus,
    GuidedProviderPolicyProfile,
    GuidedProviderPreflight,
    GuidedProviderPreflightItem,
    GuidedProviderPreflightProblemCode,
    GuidedProviderPreflightReview,
    GuidedProviderPreflightStatus,
    GuidedProviderPricingProfile,
    GuidedProviderRequestProfile,
    GuidedProviderRetentionProfile,
    GuidedProviderSerpApiZeroTraceStatus,
    assess_guided_provider_preflight,
    prepare_guided_provider_preflight,
)
from trip_planner.guided_provider_scope import GuidedProviderCapability
from trip_planner.guided_provider_scope_response import (
    GuidedProviderScopeResponseKind,
    capture_guided_provider_scope_response,
)
from trip_planner.models import DecisionState, EvidenceState


CHECKED_AT = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)
EXPIRES_AT = CHECKED_AT + timedelta(hours=24)
EVALUATION_AT = CHECKED_AT + timedelta(minutes=5)


def _google_item(
    topic: GuidedEvidenceTopic,
    capability: GuidedProviderCapability,
    max_request_count: int,
    *,
    credential_status: GuidedProviderCredentialStatus = (
        GuidedProviderCredentialStatus.AVAILABLE
    ),
    billing_region: GuidedProviderBillingRegion = (
        GuidedProviderBillingRegion.NON_EEA
    ),
    request_profile: GuidedProviderRequestProfile | None = None,
    pricing_profile: GuidedProviderPricingProfile | None = None,
    policy_profile: GuidedProviderPolicyProfile = (
        GuidedProviderPolicyProfile.GOOGLE_MAPS_NON_EEA_2026_06_10
    ),
    retention_profile: GuidedProviderRetentionProfile = (
        GuidedProviderRetentionProfile.GOOGLE_MAPS_PROCESS_LOCAL_ONLY
    ),
) -> GuidedProviderPreflightItem:
    expected_request = {
        GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP: (
            GuidedProviderRequestProfile.GOOGLE_PLACES_TEXT_SEARCH_PRO
        ),
        GuidedProviderCapability.GOOGLE_PLACES_CURRENT_HOURS: (
            GuidedProviderRequestProfile.GOOGLE_PLACES_PLACE_DETAILS_ENTERPRISE
        ),
        GuidedProviderCapability.GOOGLE_ROUTES: (
            GuidedProviderRequestProfile.GOOGLE_ROUTES_COMPUTE_ROUTES_ESSENTIALS
        ),
    }[capability]
    expected_pricing = {
        GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP: (
            GuidedProviderPricingProfile.GOOGLE_TEXT_SEARCH_PRO_GLOBAL_2026_07_31
        ),
        GuidedProviderCapability.GOOGLE_PLACES_CURRENT_HOURS: (
            GuidedProviderPricingProfile.GOOGLE_PLACE_DETAILS_ENTERPRISE_GLOBAL_2026_07_31
        ),
        GuidedProviderCapability.GOOGLE_ROUTES: (
            GuidedProviderPricingProfile.GOOGLE_COMPUTE_ROUTES_ESSENTIALS_GLOBAL_2026_07_31
        ),
    }[capability]
    return GuidedProviderPreflightItem(
        topic=topic,
        capability=capability,
        request_profile=request_profile or expected_request,
        pricing_profile=pricing_profile or expected_pricing,
        policy_profile=policy_profile,
        retention_profile=retention_profile,
        billing_region=billing_region,
        credential_status=credential_status,
        max_request_count=max_request_count,
    )


def _complete_google_items(
    *,
    credential_status: GuidedProviderCredentialStatus = (
        GuidedProviderCredentialStatus.AVAILABLE
    ),
    billing_region: GuidedProviderBillingRegion = (
        GuidedProviderBillingRegion.NON_EEA
    ),
) -> tuple[GuidedProviderPreflightItem, ...]:
    return (
        _google_item(
            GuidedEvidenceTopic.CURRENT_OPENING_HOURS,
            GuidedProviderCapability.GOOGLE_PLACES_CURRENT_HOURS,
            2,
            credential_status=credential_status,
            billing_region=billing_region,
        ),
        _google_item(
            GuidedEvidenceTopic.PLACE_IDENTITY,
            GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP,
            3,
            credential_status=credential_status,
            billing_region=billing_region,
        ),
    )


def _prepared(
    *,
    items: tuple[GuidedProviderPreflightItem, ...] | None = None,
    checked_at: datetime = CHECKED_AT,
    expires_at: datetime = EXPIRES_AT,
):
    context = _capture(GuidedProviderScopeResponseKind.ACCEPT_PROVIDER_SCOPE)
    preflight = prepare_guided_provider_preflight(
        *context,
        items=_complete_google_items() if items is None else items,
        checked_at=checked_at,
        expires_at=expires_at,
    )
    return (*context, preflight)


class GuidedProviderPreflightTests(unittest.TestCase):
    def test_current_google_attestations_reach_execution_review_only(self) -> None:
        context = _prepared()

        review = assess_guided_provider_preflight(
            *context,
            evaluation_at=EVALUATION_AT,
        )
        safe = review.to_dict()

        self.assertEqual(
            GuidedProviderPreflightStatus.REVIEW_REQUIRED,
            review.status,
        )
        self.assertEqual(
            136_000,
            review.estimated_first_paid_tier_google_cost_usd_micros,
        )
        self.assertEqual(0, review.serpapi_plan_credit_cap)
        self.assertTrue(
            review.all_provider_costs_have_currency_list_rate_estimates
        )
        self.assertEqual(
            "review_private_provider_execution_authorization",
            safe["next_action"],
        )
        self.assertTrue(safe["requires_user_response"])
        self.assertTrue(safe["requires_user_review"])
        self.assertTrue(safe["requires_user_decision"])
        self.assertIn("不是 provider 驗證", safe["review_disclosure"])
        self.assertEqual([], safe["problems"])
        preflight = safe["provider_preflight"]
        self.assertTrue(preflight["exact_private_context_bound"])
        self.assertTrue(preflight["host_attestation_fresh"])
        self.assertEqual(2, preflight["accepted_scope_item_count"])
        self.assertEqual(5, preflight["accepted_max_request_count"])
        self.assertEqual(2, preflight["preflight_item_count"])
        self.assertEqual(5, preflight["max_request_count"])
        self.assertTrue(preflight["within_accepted_request_cap"])
        self.assertEqual(136_000, preflight[
            "estimated_first_paid_tier_google_cost_usd_micros"
        ])
        self.assertEqual(0, preflight["serpapi_plan_credit_cap"])
        self.assertTrue(
            preflight[
                "all_provider_costs_have_currency_list_rate_estimates"
            ]
        )
        self.assertFalse(preflight["monthly_free_usage_remaining_checked"])
        self.assertFalse(preflight["cost_estimate_is_hard_currency_cap"])
        self.assertTrue(preflight["host_pricing_profile_attested"])
        self.assertFalse(preflight["pricing_verified_by_offline_contract"])
        self.assertTrue(preflight["host_provider_policy_profile_attested"])
        self.assertFalse(
            preflight["provider_policy_verified_by_offline_contract"]
        )
        self.assertTrue(preflight["billing_region_attested"])
        self.assertTrue(preflight["credential_availability_attested"])
        self.assertTrue(preflight["credentials_available"])
        self.assertFalse(preflight["credentials_accessed"])
        self.assertTrue(preflight["execution_time_recheck_required"])
        self.assertTrue(preflight["explicit_execution_authorization_required"])
        self.assertFalse(preflight["preflight_is_provider_authorization"])
        self.assertFalse(preflight["provider_scope_authorized"])
        self.assertFalse(preflight["provider_requests_created"])
        self.assertFalse(preflight["provider_calls_permitted"])
        self.assertEqual("candidate", preflight["decision_state"])
        self.assertEqual("unverified", preflight["evidence_state"])
        self.assertFalse(preflight["supports_authoritative_use"])
        self.assertEqual(
            {
                "pricing": "official_provider_pricing_documentation",
                "provider_policy": "official_provider_terms_documentation",
                "billing_region": "host_billing_account_configuration",
                "credential_availability": "host_secret_store_status",
            },
            preflight["attestation_sources"],
        )
        self.assertEqual(
            [
                "destination_context",
                "place_identity_context",
                "place_search_context",
                "travel_date_context",
            ],
            preflight["data_categories"],
        )
        self.assertEqual(
            [
                "google_places_place_details_enterprise",
                "google_places_text_search_pro",
            ],
            [item["request_profile"] for item in preflight["items"]],
        )
        self.assertEqual(
            [20_000, 32_000],
            [
                item["first_paid_tier_usd_micros_per_request"]
                for item in preflight["items"]
            ],
        )
        self.assertEqual(
            [1_000, 5_000],
            [
                item["published_monthly_free_request_cap"]
                for item in preflight["items"]
            ],
        )
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
        self.assertEqual(DecisionState.CANDIDATE, context[-1].decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, context[-1].evidence_state)

    def test_unavailable_credentials_unknown_region_and_expiry_block(self) -> None:
        cases = (
            (
                _complete_google_items(
                    credential_status=GuidedProviderCredentialStatus.UNAVAILABLE
                ),
                EVALUATION_AT,
                (GuidedProviderPreflightProblemCode.CREDENTIAL_UNAVAILABLE,),
            ),
            (
                _complete_google_items(
                    billing_region=GuidedProviderBillingRegion.UNKNOWN
                ),
                EVALUATION_AT,
                (GuidedProviderPreflightProblemCode.BILLING_REGION_UNCONFIRMED,),
            ),
            (
                _complete_google_items(
                    billing_region=GuidedProviderBillingRegion.EEA
                ),
                EVALUATION_AT,
                (GuidedProviderPreflightProblemCode.BILLING_REGION_UNCONFIRMED,),
            ),
            (
                _complete_google_items(),
                EXPIRES_AT,
                (
                    GuidedProviderPreflightProblemCode.PREFLIGHT_ATTESTATION_NOT_CURRENT,
                ),
            ),
        )
        for items, evaluated_at, expected in cases:
            with self.subTest(expected=expected):
                safe = assess_guided_provider_preflight(
                    *_prepared(items=items),
                    evaluation_at=evaluated_at,
                ).to_dict()
                self.assertEqual("blocked", safe["status"])
                self.assertEqual(
                    "resolve_private_provider_preflight_blockers",
                    safe["next_action"],
                )
                self.assertFalse(safe["requires_user_response"])
                self.assertEqual(
                    [item.value for item in expected],
                    safe["problems"],
                )
                self.assertEqual(2, len(safe["provider_preflight"]["items"]))
                self.assertFalse(
                    safe["provider_preflight"]["provider_calls_permitted"]
                )

    def test_structural_scope_and_profile_drift_needs_refinement(self) -> None:
        complete = _complete_google_items()
        cases = (
            (
                (complete[0],),
                (GuidedProviderPreflightProblemCode.ACCEPTED_SCOPE_ITEM_MISSING,),
            ),
            (
                (*complete, complete[1]),
                (
                    GuidedProviderPreflightProblemCode.TOPIC_ATTESTED_MULTIPLE_TIMES,
                ),
            ),
            (
                (
                    complete[0],
                    replace(complete[1], max_request_count=4),
                ),
                (
                    GuidedProviderPreflightProblemCode.REQUEST_LIMIT_EXCEEDS_ACCEPTED_SCOPE,
                ),
            ),
            (
                (
                    complete[0],
                    replace(
                        complete[1],
                        request_profile=(
                            GuidedProviderRequestProfile.GOOGLE_ROUTES_COMPUTE_ROUTES_ESSENTIALS
                        ),
                        pricing_profile=(
                            GuidedProviderPricingProfile
                            .GOOGLE_COMPUTE_ROUTES_ESSENTIALS_GLOBAL_2026_07_31
                        ),
                        retention_profile=(
                            GuidedProviderRetentionProfile.SERPAPI_ZERO_TRACE
                        ),
                    ),
                ),
                (
                    GuidedProviderPreflightProblemCode.PRICING_PROFILE_MISMATCH,
                    GuidedProviderPreflightProblemCode.REQUEST_PROFILE_MISMATCH,
                    GuidedProviderPreflightProblemCode.RETENTION_PROFILE_MISMATCH,
                ),
            ),
            (
                (
                    complete[0],
                    _google_item(
                        GuidedEvidenceTopic.PLACE_IDENTITY,
                        GuidedProviderCapability.GOOGLE_ROUTES,
                        3,
                    ),
                ),
                (GuidedProviderPreflightProblemCode.CAPABILITY_MISMATCH,),
            ),
            (
                (
                    complete[0],
                    replace(
                        complete[1],
                        policy_profile=(
                            GuidedProviderPolicyProfile.SERPAPI_TERMS_2026_04_08
                        ),
                    ),
                ),
                (GuidedProviderPreflightProblemCode.POLICY_PROFILE_MISMATCH,),
            ),
            (
                (
                    replace(
                        complete[0],
                        credential_status=(
                            GuidedProviderCredentialStatus.UNAVAILABLE
                        ),
                    ),
                    complete[1],
                ),
                (
                    GuidedProviderPreflightProblemCode
                    .CREDENTIAL_STATUS_INCONSISTENT,
                    GuidedProviderPreflightProblemCode.CREDENTIAL_UNAVAILABLE,
                ),
            ),
        )
        for items, expected in cases:
            with self.subTest(expected=expected):
                safe = assess_guided_provider_preflight(
                    *_prepared(items=items),
                    evaluation_at=EVALUATION_AT,
                ).to_dict()
                self.assertEqual("needs_refinement", safe["status"])
                self.assertEqual(
                    "refine_private_provider_preflight",
                    safe["next_action"],
                )
                self.assertEqual(
                    [item.value for item in expected],
                    safe["problems"],
                )
                self.assertEqual([], safe["provider_preflight"]["items"])
                self.assertFalse(
                    safe["provider_preflight"]["provider_calls_permitted"]
                )

    def test_serpapi_plan_state_is_bounded_without_currency_list_rate(self) -> None:
        upstream = _accepted_response_context()
        evidence_plan = _plan(
            _requirement(0, GuidedEvidenceTopic.LODGING),
            _requirement(
                1,
                disposition=(
                    GuidedEvidenceDisposition.NO_EXTERNAL_EVIDENCE_IDENTIFIED
                ),
            ),
        )
        provider_scope = _proposal(
            _scope_item(
                GuidedEvidenceTopic.LODGING,
                GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
                2,
            )
        )
        response = capture_guided_provider_scope_response(
            *upstream,
            evidence_plan,
            provider_scope,
            kind=GuidedProviderScopeResponseKind.ACCEPT_PROVIDER_SCOPE,
        )
        context = (*upstream, evidence_plan, provider_scope, response)
        item = GuidedProviderPreflightItem(
            topic=GuidedEvidenceTopic.LODGING,
            capability=GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
            request_profile=(
                GuidedProviderRequestProfile.SERPAPI_GOOGLE_HOTELS_PLAN_CREDIT
            ),
            pricing_profile=(
                GuidedProviderPricingProfile.SERPAPI_PLAN_CREDIT_2026_08_07
            ),
            policy_profile=(
                GuidedProviderPolicyProfile.SERPAPI_TERMS_2026_04_08
            ),
            retention_profile=(
                GuidedProviderRetentionProfile.SERPAPI_STANDARD_PROVIDER_STORAGE
            ),
            billing_region=GuidedProviderBillingRegion.NOT_APPLICABLE,
            credential_status=GuidedProviderCredentialStatus.AVAILABLE,
            max_request_count=2,
            serpapi_remaining_plan_credits=2,
            serpapi_automatic_renewal_enabled=False,
        )
        preflight = prepare_guided_provider_preflight(
            *context,
            items=(item,),
            checked_at=CHECKED_AT,
            expires_at=EXPIRES_AT,
        )
        safe = assess_guided_provider_preflight(
            *context,
            preflight,
            evaluation_at=EVALUATION_AT,
        ).to_dict()

        self.assertEqual("review_required", safe["status"])
        review = safe["provider_preflight"]
        self.assertEqual(2, review["serpapi_plan_credit_cap"])
        self.assertFalse(
            review["all_provider_costs_have_currency_list_rate_estimates"]
        )
        self.assertEqual(
            "serpapi-standard-provider-storage",
            review["items"][0]["expected_retention_profile"],
        )
        self.assertTrue(
            review["items"][0]["serpapi_plan_credits_sufficient"]
        )
        self.assertTrue(
            review["items"][0]["serpapi_automatic_renewal_disabled"]
        )
        self.assertIsNone(
            review["items"][0]["serpapi_zero_trace_entitlement_attested"]
        )
        with self.assertRaises(ValueError):
            replace(item, serpapi_remaining_plan_credits=33)

        zero_trace = replace(
            item,
            retention_profile=GuidedProviderRetentionProfile.SERPAPI_ZERO_TRACE,
            serpapi_zero_trace_status=(
                GuidedProviderSerpApiZeroTraceStatus.ENTITLED
            ),
        )
        zero_trace_preflight = prepare_guided_provider_preflight(
            *context,
            items=(zero_trace,),
            checked_at=CHECKED_AT,
            expires_at=EXPIRES_AT,
        )
        zero_trace_safe = assess_guided_provider_preflight(
            *context,
            zero_trace_preflight,
            evaluation_at=EVALUATION_AT,
        ).to_dict()
        self.assertEqual("review_required", zero_trace_safe["status"])
        self.assertTrue(
            zero_trace_safe["provider_preflight"]["items"][0][
                "serpapi_zero_trace_entitlement_attested"
            ]
        )

        zero_trace_unknown = replace(
            zero_trace,
            serpapi_zero_trace_status=(
                GuidedProviderSerpApiZeroTraceStatus.UNKNOWN
            ),
        )
        zero_trace_unknown_preflight = prepare_guided_provider_preflight(
            *context,
            items=(zero_trace_unknown,),
            checked_at=CHECKED_AT,
            expires_at=EXPIRES_AT,
        )
        zero_trace_unknown_safe = assess_guided_provider_preflight(
            *context,
            zero_trace_unknown_preflight,
            evaluation_at=EVALUATION_AT,
        ).to_dict()
        self.assertEqual("blocked", zero_trace_unknown_safe["status"])
        self.assertEqual(
            ["serpapi_zero_trace_entitlement_unconfirmed"],
            zero_trace_unknown_safe["problems"],
        )

        zero_trace_mismatch = replace(
            item,
            serpapi_zero_trace_status=(
                GuidedProviderSerpApiZeroTraceStatus.ENTITLED
            ),
        )
        zero_trace_mismatch_preflight = prepare_guided_provider_preflight(
            *context,
            items=(zero_trace_mismatch,),
            checked_at=CHECKED_AT,
            expires_at=EXPIRES_AT,
        )
        zero_trace_mismatch_safe = assess_guided_provider_preflight(
            *context,
            zero_trace_mismatch_preflight,
            evaluation_at=EVALUATION_AT,
        ).to_dict()
        self.assertEqual("needs_refinement", zero_trace_mismatch_safe["status"])
        self.assertEqual(
            ["serpapi_zero_trace_status_mismatch"],
            zero_trace_mismatch_safe["problems"],
        )

        blocked_item = replace(
            item,
            serpapi_remaining_plan_credits=1,
            serpapi_automatic_renewal_enabled=True,
        )
        blocked = prepare_guided_provider_preflight(
            *context,
            items=(blocked_item,),
            checked_at=CHECKED_AT,
            expires_at=EXPIRES_AT,
        )
        blocked_safe = assess_guided_provider_preflight(
            *context,
            blocked,
            evaluation_at=EVALUATION_AT,
        ).to_dict()
        self.assertEqual("blocked", blocked_safe["status"])
        self.assertEqual(
            [
                "serpapi_automatic_renewal_enabled",
                "serpapi_plan_credits_insufficient",
            ],
            blocked_safe["problems"],
        )

        unknown_plan = replace(
            item,
            serpapi_remaining_plan_credits=None,
            serpapi_automatic_renewal_enabled=None,
        )
        unknown = prepare_guided_provider_preflight(
            *context,
            items=(unknown_plan,),
            checked_at=CHECKED_AT,
            expires_at=EXPIRES_AT,
        )
        unknown_safe = assess_guided_provider_preflight(
            *context,
            unknown,
            evaluation_at=EVALUATION_AT,
        ).to_dict()
        self.assertEqual("blocked", unknown_safe["status"])
        self.assertEqual(
            ["serpapi_plan_state_unconfirmed"],
            unknown_safe["problems"],
        )

    def test_clock_contract_rejects_naive_or_overlong_attestations(self) -> None:
        with self.assertRaises(ValueError):
            _prepared(
                checked_at=CHECKED_AT.replace(tzinfo=None),
                expires_at=EXPIRES_AT,
            )
        with self.assertRaises(ValueError):
            _prepared(
                expires_at=CHECKED_AT + timedelta(hours=24, seconds=1),
            )
        safe = assess_guided_provider_preflight(
            *_prepared(),
            evaluation_at=CHECKED_AT - timedelta(microseconds=1),
        ).to_dict()
        self.assertEqual("blocked", safe["status"])
        self.assertEqual(
            ["preflight_attestation_not_current"],
            safe["problems"],
        )

    def test_preflight_binds_every_context_layer_and_allows_reordering(self) -> None:
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
            preflight,
        ) = _prepared()
        original = assess_guided_provider_preflight(
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
            preflight,
            evaluation_at=EVALUATION_AT,
        ).to_dict()
        reordered = assess_guided_provider_preflight(
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
            preflight,
            evaluation_at=EVALUATION_AT,
        ).to_dict()
        self.assertEqual(original, reordered)

        with self.assertRaises(ValueError):
            assess_guided_provider_preflight(
                replace(brief, constraints=brief.must_do),
                cards,
                preference,
                refinement,
                refinement_response,
                itinerary_candidate,
                itinerary_response,
                evidence_plan,
                provider_scope,
                response,
                preflight,
                evaluation_at=EVALUATION_AT,
            )

    def test_only_accept_response_can_prepare_preflight(self) -> None:
        for kind in (
            GuidedProviderScopeResponseKind.REQUEST_SMALLER_PROVIDER_SCOPE,
            GuidedProviderScopeResponseKind.CANCEL_EXTERNAL_LOOKUP,
        ):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    prepare_guided_provider_preflight(
                        *_capture(kind),
                        items=_complete_google_items(),
                        checked_at=CHECKED_AT,
                        expires_at=EXPIRES_AT,
                    )

    def test_attestation_and_review_cannot_be_forged_or_replaced(self) -> None:
        context = _prepared()
        preflight = context[-1]
        review = assess_guided_provider_preflight(
            *context,
            evaluation_at=EVALUATION_AT,
        )

        with self.assertRaises(ValueError):
            GuidedProviderPreflight(
                items=_complete_google_items(),
                checked_at=CHECKED_AT,
                expires_at=EXPIRES_AT,
            )
        with self.assertRaises(ValueError):
            replace(preflight, expires_at=EXPIRES_AT - timedelta(hours=1))
        with self.assertRaises(ValueError):
            GuidedProviderPreflightReview(
                status=GuidedProviderPreflightStatus.REVIEW_REQUIRED,
                next_action="review_private_provider_execution_authorization",
                accepted_scope_item_count=2,
                accepted_max_request_count=5,
                preflight_item_count=2,
                max_request_count=5,
                items=_complete_google_items(),
                host_attestation_fresh=True,
            )
        with self.assertRaises(ValueError):
            replace(review, next_action="execute_guided_provider_request")

    def test_safe_output_and_schema_exclude_private_request_material(self) -> None:
        context = _prepared()
        preflight = context[-1]
        review = assess_guided_provider_preflight(
            *context,
            evaluation_at=EVALUATION_AT,
        )
        rendered = "\n".join(
            (
                repr(preflight),
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
            EXPIRES_AT.isoformat(),
        ):
            self.assertNotIn(private_value, rendered)

        self.assertEqual(
            {"items", "checked_at", "expires_at", "_context_fingerprint"},
            {item.name for item in fields(GuidedProviderPreflight)},
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
            "authorization",
            "confirmation",
        }
        self.assertTrue(
            forbidden_fields.isdisjoint(
                {item.name for item in fields(GuidedProviderPreflightItem)}
            )
        )

    def test_public_contract_has_no_environment_request_execution_or_apply_path(self) -> None:
        for name in (
            "GUIDED_PROVIDER_PREFLIGHT_VERSION",
            "GuidedProviderBillingRegion",
            "GuidedProviderCredentialStatus",
            "GuidedProviderPolicyProfile",
            "GuidedProviderPreflight",
            "GuidedProviderPreflightItem",
            "GuidedProviderPreflightProblemCode",
            "GuidedProviderPreflightReview",
            "GuidedProviderPreflightStatus",
            "GuidedProviderPricingProfile",
            "GuidedProviderRequestProfile",
            "GuidedProviderRetentionProfile",
            "GuidedProviderSerpApiZeroTraceStatus",
            "assess_guided_provider_preflight",
            "prepare_guided_provider_preflight",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        for unsupported_name in (
            "parse_guided_provider_preflight",
            "read_guided_provider_credentials",
            "build_guided_provider_request",
            "authorize_guided_provider_execution",
            "execute_guided_provider_request",
            "call_guided_provider",
            "schedule_guided_provider_preflight",
            "create_trip_from_guided_provider_preflight",
            "apply_guided_provider_preflight",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        source = inspect.getsource(preflight_module)
        tree = ast.parse(source)
        allowed_imports = {"hashlib", "json", "re"}
        allowed_from_imports = {
            (0, "__future__"),
            (0, "collections"),
            (0, "dataclasses"),
            (0, "datetime"),
            (0, "enum"),
            (0, "typing"),
            (1, "facts"),
            (1, "guided_draft"),
            (1, "guided_evidence_plan"),
            (1, "guided_itinerary"),
            (1, "guided_proposal"),
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
