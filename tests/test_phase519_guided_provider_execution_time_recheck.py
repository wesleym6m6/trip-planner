"""Phase 5.19 exact provider execution-time host recheck gate."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace
from datetime import timedelta

import trip_planner
import trip_planner.guided_provider_execution_time_recheck as recheck_module
from tests.test_phase513_guided_provider_preflight import EXPIRES_AT
from tests.test_phase516_guided_provider_execution_target_bindings import (
    PRIVATE_HOTEL_QUERY,
    PRIVATE_QUERY,
    _default_preimages,
    _hotel_request,
    _serpapi_item,
    details_fingerprint,
)
from tests.test_phase517_guided_provider_execution_authorization_review import (
    REVIEW_AT,
    _single_topic_review,
)
from tests.test_phase518_guided_provider_execution_authorization_response import (
    ASSESS_RESPONSE_AT,
    CAPTURE_AT,
    _captured_response,
)
from trip_planner.guided_evidence_plan import GuidedEvidenceTopic
from trip_planner.guided_provider_execution_authorization_response import (
    GuidedProviderExecutionAuthorizationResponseKind,
    capture_guided_provider_execution_authorization_response,
)
from trip_planner.guided_provider_execution_time_recheck import (
    GUIDED_PROVIDER_EXECUTION_TIME_RECHECK_VERSION,
    GuidedProviderExecutionTimeRecheck,
    GuidedProviderExecutionTimeRecheckProblemCode,
    GuidedProviderExecutionTimeRecheckReview,
    GuidedProviderExecutionTimeRecheckStatus,
    assess_guided_provider_execution_time_recheck,
    prepare_guided_provider_execution_time_recheck,
)
from trip_planner.guided_provider_preflight import (
    GuidedProviderBillingRegion,
    GuidedProviderCredentialStatus,
    GuidedProviderPolicyProfile,
    GuidedProviderPreflightProblemCode,
    GuidedProviderPricingProfile,
    GuidedProviderRetentionProfile,
    GuidedProviderSerpApiZeroTraceStatus,
)
from trip_planner.guided_provider_scope import GuidedProviderCapability
from trip_planner.models import DecisionState, EvidenceState


RECHECK_AT = ASSESS_RESPONSE_AT + timedelta(minutes=1)
ASSESS_RECHECK_AT = RECHECK_AT + timedelta(minutes=1)


def _prepared_recheck(
    kind: GuidedProviderExecutionAuthorizationResponseKind = (
        GuidedProviderExecutionAuthorizationResponseKind.ACCEPT
    ),
    *,
    items: tuple[object, ...] | None = None,
):
    context, preimages = _captured_response(kind)
    current_items = context[10].items if items is None else items
    recheck = prepare_guided_provider_execution_time_recheck(
        *context,
        preimages=preimages,
        items=current_items,
        evaluation_at=RECHECK_AT,
    )
    return (*context, recheck), preimages


def _captured_serpapi_response():
    context, preimages = _single_topic_review(
        GuidedEvidenceTopic.LODGING,
        GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
        _serpapi_item(GuidedEvidenceTopic.LODGING),
        _hotel_request(),
    )
    response = capture_guided_provider_execution_authorization_response(
        *context,
        preimages=preimages,
        kind=GuidedProviderExecutionAuthorizationResponseKind.ACCEPT,
        evaluation_at=CAPTURE_AT,
    )
    return (*context, response), preimages


class GuidedProviderExecutionTimeRecheckTests(unittest.TestCase):
    def test_fresh_matching_recheck_only_reaches_materialization_review(self) -> None:
        context, preimages = _prepared_recheck()
        recheck = context[-1]
        assessed = assess_guided_provider_execution_time_recheck(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_RECHECK_AT,
        )
        safe = assessed.to_dict()
        handoff = safe["provider_execution_time_recheck"]

        self.assertEqual(
            GuidedProviderExecutionTimeRecheckStatus
            .READY_FOR_PRIVATE_PROVIDER_REQUEST_MATERIALIZATION_REVIEW,
            assessed.status,
        )
        self.assertEqual(
            "prepare_private_provider_request_materialization_review",
            assessed.next_action,
        )
        self.assertEqual(2, assessed.bound_request_count)
        self.assertEqual(5, assessed.accepted_max_request_count)
        self.assertEqual(2, assessed.bound_google_request_count)
        self.assertEqual(0, assessed.bound_serpapi_request_count)
        self.assertEqual(52_000, assessed.estimated_bound_first_paid_tier_google_cost_usd_micros)
        self.assertEqual(136_000, assessed.accepted_max_first_paid_tier_google_cost_usd_micros)
        self.assertTrue(assessed.host_attestation_fresh)
        self.assertTrue(assessed.all_credentials_available)
        self.assertTrue(assessed.serpapi_plan_state_sufficient)
        self.assertEqual((), assessed.profile_problem_codes)
        self.assertEqual((), assessed.preflight_problem_codes)
        self.assertTrue(handoff["exact_accept_response_bound"])
        self.assertTrue(handoff["same_exact_target_preimages_revalidated"])
        self.assertTrue(handoff["short_lived_host_attestation"])
        self.assertFalse(handoff["exact_recheck_times_exposed"])
        self.assertTrue(handoff["request_materialization_review_required"])
        self.assertEqual(2, handoff["eligible_for_request_materialization_review_count"])
        self.assertFalse(handoff["execution_authority_active"])
        self.assertEqual(0, handoff["provider_request_contract_count_created_by_recheck"])
        self.assertEqual(0, handoff["http_request_count_created_by_recheck"])
        self.assertEqual(0, handoff["provider_call_count_observed"])
        self.assertFalse(handoff["provider_calls_permitted"])
        self.assertEqual(DecisionState.CANDIDATE, recheck.decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, recheck.evidence_state)
        self.assertEqual("candidate", handoff["decision_state"])
        self.assertEqual("unverified", handoff["evidence_state"])
        self.assertIn("provider_execution_time_recheck", safe["needs_verification"])

    def test_only_an_exact_accept_response_can_prepare_a_recheck(self) -> None:
        for kind in (
            GuidedProviderExecutionAuthorizationResponseKind.REQUEST_SMALLER,
            GuidedProviderExecutionAuthorizationResponseKind.CANCEL,
        ):
            with self.subTest(kind=kind):
                context, preimages = _captured_response(kind)
                with self.assertRaises(ValueError):
                    prepare_guided_provider_execution_time_recheck(
                        *context,
                        preimages=preimages,
                        items=context[10].items,
                        evaluation_at=RECHECK_AT,
                    )

    def test_current_attestations_must_preserve_the_accepted_request_shape(self) -> None:
        context, preimages = _captured_response()
        accepted = context[10].items
        cases = (
            (accepted[0], accepted[0]),
            (replace(accepted[0], max_request_count=1), accepted[1]),
            (
                replace(
                    accepted[0],
                    capability=GuidedProviderCapability.GOOGLE_ROUTES,
                ),
                accepted[1],
            ),
        )
        for items in cases:
            with self.subTest(items=items):
                with self.assertRaises(ValueError):
                    prepare_guided_provider_execution_time_recheck(
                        *context,
                        preimages=preimages,
                        items=items,
                        evaluation_at=RECHECK_AT,
                    )

    def test_commercial_policy_or_region_changes_require_a_new_preflight(self) -> None:
        base_context, _ = _captured_response()
        accepted = base_context[10].items
        cases = (
            (
                replace(
                    accepted[0],
                    pricing_profile=(
                        GuidedProviderPricingProfile
                        .GOOGLE_TEXT_SEARCH_PRO_GLOBAL_2026_07_31
                    ),
                ),
                GuidedProviderExecutionTimeRecheckProblemCode
                .PRICING_PROFILE_CHANGED,
            ),
            (
                replace(
                    accepted[0],
                    policy_profile=GuidedProviderPolicyProfile.SERPAPI_TERMS_2026_04_08,
                ),
                GuidedProviderExecutionTimeRecheckProblemCode.POLICY_PROFILE_CHANGED,
            ),
            (
                replace(
                    accepted[0],
                    retention_profile=(
                        GuidedProviderRetentionProfile
                        .SERPAPI_STANDARD_PROVIDER_STORAGE
                    ),
                ),
                GuidedProviderExecutionTimeRecheckProblemCode.RETENTION_PROFILE_CHANGED,
            ),
            (
                replace(
                    accepted[0],
                    billing_region=GuidedProviderBillingRegion.EEA,
                ),
                GuidedProviderExecutionTimeRecheckProblemCode.BILLING_REGION_CHANGED,
            ),
        )
        for changed, expected_problem in cases:
            with self.subTest(problem=expected_problem):
                context, preimages = _prepared_recheck(
                    items=(changed, accepted[1]),
                )
                assessed = assess_guided_provider_execution_time_recheck(
                    *context,
                    preimages=preimages,
                    evaluation_at=ASSESS_RECHECK_AT,
                )
                self.assertEqual(
                    GuidedProviderExecutionTimeRecheckStatus
                    .NEEDS_NEW_PRIVATE_PROVIDER_PREFLIGHT_REVIEW,
                    assessed.status,
                )
                self.assertEqual(
                    "prepare_private_provider_preflight_review",
                    assessed.next_action,
                )
                self.assertIn(expected_problem, assessed.profile_problem_codes)
                self.assertFalse(
                    assessed.to_dict()["provider_execution_time_recheck"][
                        "execution_authority_active"
                    ]
                )

    def test_serpapi_zero_trace_change_requires_a_new_preflight(self) -> None:
        context, preimages = _captured_serpapi_response()
        changed = replace(
            context[10].items[0],
            serpapi_zero_trace_status=GuidedProviderSerpApiZeroTraceStatus.UNKNOWN,
        )
        recheck = prepare_guided_provider_execution_time_recheck(
            *context,
            preimages=preimages,
            items=(changed,),
            evaluation_at=RECHECK_AT,
        )
        assessed = assess_guided_provider_execution_time_recheck(
            *context,
            recheck,
            preimages=preimages,
            evaluation_at=ASSESS_RECHECK_AT,
        )

        self.assertEqual(
            GuidedProviderExecutionTimeRecheckStatus
            .NEEDS_NEW_PRIVATE_PROVIDER_PREFLIGHT_REVIEW,
            assessed.status,
        )
        self.assertIn(
            GuidedProviderExecutionTimeRecheckProblemCode
            .SERPAPI_ZERO_TRACE_STATUS_CHANGED,
            assessed.profile_problem_codes,
        )

    def test_current_credential_or_plan_failures_are_blocked_and_retryable(self) -> None:
        context, preimages = _captured_response()
        unavailable_items = (
            replace(
                context[10].items[0],
                credential_status=GuidedProviderCredentialStatus.UNAVAILABLE,
            ),
            context[10].items[1],
        )
        unavailable = prepare_guided_provider_execution_time_recheck(
            *context,
            preimages=preimages,
            items=unavailable_items,
            evaluation_at=RECHECK_AT,
        )
        unavailable_review = assess_guided_provider_execution_time_recheck(
            *context,
            unavailable,
            preimages=preimages,
            evaluation_at=ASSESS_RECHECK_AT,
        )
        self.assertEqual(GuidedProviderExecutionTimeRecheckStatus.BLOCKED, unavailable_review.status)
        self.assertEqual("refresh_private_provider_execution_time_recheck", unavailable_review.next_action)
        self.assertFalse(unavailable_review.all_credentials_available)
        self.assertIn(
            GuidedProviderPreflightProblemCode.CREDENTIAL_UNAVAILABLE,
            unavailable_review.preflight_problem_codes,
        )

        serp_context, serp_preimages = _captured_serpapi_response()
        insufficient_item = replace(
            serp_context[10].items[0],
            serpapi_remaining_plan_credits=0,
            serpapi_automatic_renewal_enabled=True,
        )
        serp_recheck = prepare_guided_provider_execution_time_recheck(
            *serp_context,
            preimages=serp_preimages,
            items=(insufficient_item,),
            evaluation_at=RECHECK_AT,
        )
        serp_review = assess_guided_provider_execution_time_recheck(
            *serp_context,
            serp_recheck,
            preimages=serp_preimages,
            evaluation_at=ASSESS_RECHECK_AT,
        )
        self.assertEqual(GuidedProviderExecutionTimeRecheckStatus.BLOCKED, serp_review.status)
        self.assertFalse(serp_review.serpapi_plan_state_sufficient)
        self.assertIn(
            GuidedProviderPreflightProblemCode.SERPAPI_PLAN_CREDITS_INSUFFICIENT,
            serp_review.preflight_problem_codes,
        )
        self.assertIn(
            GuidedProviderPreflightProblemCode.SERPAPI_AUTOMATIC_RENEWAL_ENABLED,
            serp_review.preflight_problem_codes,
        )

    def test_recheck_expires_after_five_minutes_without_reviving_authority(self) -> None:
        context, preimages = _prepared_recheck()
        assessed = assess_guided_provider_execution_time_recheck(
            *context,
            preimages=preimages,
            evaluation_at=RECHECK_AT + timedelta(minutes=5),
        )

        self.assertEqual(GuidedProviderExecutionTimeRecheckStatus.BLOCKED, assessed.status)
        self.assertFalse(assessed.host_attestation_fresh)
        self.assertIn(
            GuidedProviderPreflightProblemCode.PREFLIGHT_ATTESTATION_NOT_CURRENT,
            assessed.preflight_problem_codes,
        )
        self.assertEqual(
            0,
            assessed.to_dict()["provider_execution_time_recheck"][
                "eligible_for_request_materialization_review_count"
            ],
        )

    def test_recheck_fails_closed_on_preimage_context_and_time_drift(self) -> None:
        context, preimages = _prepared_recheck()
        changed_preimages = _default_preimages(
            identity=replace(
                preimages[0].target,
                text_query=PRIVATE_QUERY + " changed",
                intent_id="",
            )
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_time_recheck(
                *context,
                preimages=changed_preimages,
                evaluation_at=ASSESS_RECHECK_AT,
            )
        changed_context = (
            replace(context[0], constraints=context[0].must_do),
            *context[1:],
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_time_recheck(
                *changed_context,
                preimages=preimages,
                evaluation_at=ASSESS_RECHECK_AT,
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_time_recheck(
                *context,
                preimages=preimages,
                evaluation_at=RECHECK_AT - timedelta(microseconds=1),
            )
        with self.assertRaises(ValueError):
            prepare_guided_provider_execution_time_recheck(
                *context[:-1],
                preimages=preimages,
                items=context[10].items,
                evaluation_at=EXPIRES_AT,
            )

    def test_safe_views_and_repr_redact_private_targets_times_and_plan_state(self) -> None:
        context, preimages = _prepared_recheck()
        recheck = context[-1]
        assessed = assess_guided_provider_execution_time_recheck(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_RECHECK_AT,
        )
        details = preimages[1].target
        safe = assessed.to_dict()
        rendered = "\n".join(
            (
                repr(recheck),
                repr(assessed),
                json.dumps(safe, ensure_ascii=False),
            )
        )
        for private_value in (
            PRIVATE_QUERY,
            "guided-private-location",
            details.endpoint.provider_place_id,
            details_fingerprint(details),
            REVIEW_AT.isoformat(),
            CAPTURE_AT.isoformat(),
            RECHECK_AT.isoformat(),
            EXPIRES_AT.isoformat(),
        ):
            self.assertNotIn(private_value, rendered)
        self.assertEqual(
            {"items", "_checked_at", "_expires_at", "_context_fingerprint"},
            {item.name for item in fields(type(recheck))},
        )
        self.assertFalse(
            safe["provider_execution_time_recheck"]["private_target_values_exposed"]
        )
        self.assertFalse(
            safe["provider_execution_time_recheck"]["provider_identifier_values_exposed"]
        )
        self.assertFalse(
            safe["provider_execution_time_recheck"]["target_fingerprints_exposed"]
        )
        for item in safe["provider_execution_time_recheck"]["items"]:
            self.assertNotIn("serpapi_remaining_plan_credits", item)
            self.assertNotIn("serpapi_automatic_renewal_enabled", item)
            self.assertNotIn("serpapi_zero_trace_status", item)

        serp_context, serp_preimages = _captured_serpapi_response()
        current = replace(
            serp_context[10].items[0],
            serpapi_remaining_plan_credits=31,
        )
        serp_recheck = prepare_guided_provider_execution_time_recheck(
            *serp_context,
            preimages=serp_preimages,
            items=(current,),
            evaluation_at=RECHECK_AT,
        )
        serp_safe = assess_guided_provider_execution_time_recheck(
            *serp_context,
            serp_recheck,
            preimages=serp_preimages,
            evaluation_at=ASSESS_RECHECK_AT,
        ).to_dict()
        serp_item = serp_safe["provider_execution_time_recheck"]["items"][0]
        self.assertTrue(serp_item["serpapi_plan_state_attested"])
        self.assertFalse(serp_item["serpapi_plan_state_values_exposed"])
        self.assertNotIn("31", json.dumps(serp_item))
        self.assertNotIn(PRIVATE_HOTEL_QUERY, json.dumps(serp_safe, ensure_ascii=False))

    def test_contracts_are_token_gated_and_no_execution_surface_exists(self) -> None:
        context, preimages = _prepared_recheck()
        recheck = context[-1]
        assessed = assess_guided_provider_execution_time_recheck(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_RECHECK_AT,
        )
        with self.assertRaises(ValueError):
            GuidedProviderExecutionTimeRecheck(
                items=recheck.items,
                _checked_at=RECHECK_AT,
                _expires_at=RECHECK_AT + timedelta(minutes=5),
                _context_fingerprint="a" * 64,
            )
        with self.assertRaises(ValueError):
            replace(assessed, next_action="execute_provider")
        with self.assertRaises(ValueError):
            GuidedProviderExecutionTimeRecheckReview(
                status=(
                    GuidedProviderExecutionTimeRecheckStatus
                    .READY_FOR_PRIVATE_PROVIDER_REQUEST_MATERIALIZATION_REVIEW
                ),
                next_action="prepare_private_provider_request_materialization_review",
                accepted_scope_item_count=1,
                accepted_max_request_count=1,
                target_item_count=1,
                bound_request_count=1,
                max_request_count=1,
                bound_source_line_reference_count=1,
                bound_google_request_count=1,
                bound_serpapi_request_count=0,
                user_stated_source_line_count=1,
                tentative_source_line_count=0,
                ai_candidate_source_line_count=0,
                estimated_bound_first_paid_tier_google_cost_usd_micros=5_000,
                accepted_max_first_paid_tier_google_cost_usd_micros=5_000,
                serpapi_bound_plan_credit_count=0,
                serpapi_plan_credit_cap=0,
            )

        for name in (
            "GUIDED_PROVIDER_EXECUTION_TIME_RECHECK_VERSION",
            "GuidedProviderExecutionTimeRecheck",
            "GuidedProviderExecutionTimeRecheckProblemCode",
            "GuidedProviderExecutionTimeRecheckReview",
            "GuidedProviderExecutionTimeRecheckStatus",
            "assess_guided_provider_execution_time_recheck",
            "prepare_guided_provider_execution_time_recheck",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        self.assertEqual(
            "guided-provider-execution-time-recheck/v1",
            GUIDED_PROVIDER_EXECUTION_TIME_RECHECK_VERSION,
        )
        for unsupported_name in (
            "materialize_guided_provider_request",
            "authorize_guided_provider_execution",
            "build_guided_provider_http_request",
            "execute_guided_provider_request",
            "call_guided_provider",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        tree = ast.parse(inspect.getsource(recheck_module))
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_roots.update(
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
        )
        self.assertTrue(
            imported_roots.isdisjoint(
                {
                    "http",
                    "httpx",
                    "os",
                    "pathlib",
                    "requests",
                    "socket",
                    "subprocess",
                    "urllib",
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
