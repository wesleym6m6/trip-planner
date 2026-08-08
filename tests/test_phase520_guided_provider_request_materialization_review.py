"""Phase 5.20 exact private provider request-materialization review."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace
from datetime import timedelta

import trip_planner
import trip_planner.guided_provider_request_materialization_review as materialization_module
from tests.test_phase510_guided_evidence_plan import (
    _accepted_response_context,
    _plan,
    _requirement,
)
from tests.test_phase511_guided_provider_scope import _proposal, _scope_item
from tests.test_phase513_guided_provider_preflight import (
    CHECKED_AT,
    EXPIRES_AT,
    _google_item,
)
from tests.test_phase515_guided_provider_execution_targets import (
    TARGETS_ASSESSED_AT,
    _accepted_preflight_context,
)
from tests.test_phase516_guided_provider_execution_target_bindings import (
    PRIVATE_HOTEL_QUERY,
    PRIVATE_QUERY,
    _default_preimages,
    _hotel_request,
    _identity_intent,
    _route_request,
    _serpapi_item,
    details_fingerprint,
)
from tests.test_phase517_guided_provider_execution_authorization_review import (
    REVIEW_AT,
    _prepared_review,
    _single_topic_review,
)
from tests.test_phase518_guided_provider_execution_authorization_response import (
    CAPTURE_AT,
)
from tests.test_phase519_guided_provider_execution_time_recheck import (
    ASSESS_RECHECK_AT,
    RECHECK_AT,
    _prepared_recheck,
)
from trip_planner.guided_evidence_plan import GuidedEvidenceTopic
from trip_planner.guided_provider_execution_authorization_response import (
    GuidedProviderExecutionAuthorizationResponseKind,
    capture_guided_provider_execution_authorization_response,
)
from trip_planner.guided_provider_execution_time_recheck import (
    prepare_guided_provider_execution_time_recheck,
)
from trip_planner.guided_provider_execution_target_bindings import (
    GuidedProviderExecutionTargetPreimage,
)
from trip_planner.guided_provider_execution_targets import (
    prepare_guided_provider_execution_targets,
)
from trip_planner.guided_provider_preflight import (
    GuidedProviderCredentialStatus,
    prepare_guided_provider_preflight,
)
from trip_planner.guided_provider_request_materialization_review import (
    GUIDED_PROVIDER_REQUEST_MATERIALIZATION_REVIEW_VERSION,
    GuidedProviderRequestContractCandidate,
    GuidedProviderRequestMaterializationKind,
    GuidedProviderRequestMaterializationReview,
    GuidedProviderRequestMaterializationReviewStatus,
    assess_guided_provider_request_materialization_review,
    prepare_guided_provider_request_materialization_review,
)
from trip_planner.guided_provider_scope import GuidedProviderCapability
from trip_planner.guided_provider_scope_response import (
    GuidedProviderScopeResponseKind,
    capture_guided_provider_scope_response,
)
from trip_planner.models import DecisionState, EvidenceState


PREPARE_REVIEW_AT = ASSESS_RECHECK_AT + timedelta(minutes=1)
ASSESS_REVIEW_AT = PREPARE_REVIEW_AT + timedelta(minutes=1)


def _prepared_materialization_review(*, items: tuple[object, ...] | None = None):
    context, preimages = _prepared_recheck(items=items)
    review = prepare_guided_provider_request_materialization_review(
        *context,
        preimages=preimages,
        evaluation_at=PREPARE_REVIEW_AT,
    )
    return (*context, review), preimages


def _prepared_single_capability_review(
    topic: GuidedEvidenceTopic,
    capability: GuidedProviderCapability,
    preflight_item: object,
    target: object,
):
    context, preimages = _single_topic_review(
        topic,
        capability,
        preflight_item,
        target,
    )
    response = capture_guided_provider_execution_authorization_response(
        *context,
        preimages=preimages,
        kind=GuidedProviderExecutionAuthorizationResponseKind.ACCEPT,
        evaluation_at=CAPTURE_AT,
    )
    accepted = (*context, response)
    recheck = prepare_guided_provider_execution_time_recheck(
        *accepted,
        preimages=preimages,
        items=accepted[10].items,
        evaluation_at=RECHECK_AT,
    )
    review = prepare_guided_provider_request_materialization_review(
        *accepted,
        recheck,
        preimages=preimages,
        evaluation_at=PREPARE_REVIEW_AT,
    )
    return (*accepted, recheck, review), preimages


def _prepared_two_requests_for_one_scope_item():
    topic = GuidedEvidenceTopic.PLACE_IDENTITY
    capability = GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP
    upstream = _accepted_response_context()
    evidence_plan = _plan(
        _requirement(0, topic),
        _requirement(1, topic),
    )
    provider_scope = _proposal(_scope_item(topic, capability, 2))
    scope_response = capture_guided_provider_scope_response(
        *upstream,
        evidence_plan,
        provider_scope,
        kind=GuidedProviderScopeResponseKind.ACCEPT_PROVIDER_SCOPE,
    )
    base = (*upstream, evidence_plan, provider_scope, scope_response)
    preflight = prepare_guided_provider_preflight(
        *base,
        items=(_google_item(topic, capability, 2),),
        checked_at=CHECKED_AT,
        expires_at=EXPIRES_AT,
    )
    accepted = _accepted_preflight_context(context=(*base, preflight))
    targets = prepare_guided_provider_execution_targets(
        *accepted,
        evaluation_at=TARGETS_ASSESSED_AT,
    )
    first = _identity_intent()
    second = replace(
        first,
        location_id="guided-private-location-2",
        text_query=first.text_query + " second",
        intent_id="",
    )
    preimages = (
        GuidedProviderExecutionTargetPreimage(
            topic=topic,
            source_line_indexes=(0,),
            target=first,
        ),
        GuidedProviderExecutionTargetPreimage(
            topic=topic,
            source_line_indexes=(1,),
            target=second,
        ),
    )
    authorization_context, preimages = _prepared_review(
        context=(*accepted, targets),
        preimages=preimages,
    )
    response = capture_guided_provider_execution_authorization_response(
        *authorization_context,
        preimages=preimages,
        kind=GuidedProviderExecutionAuthorizationResponseKind.ACCEPT,
        evaluation_at=CAPTURE_AT,
    )
    response_context = (*authorization_context, response)
    recheck = prepare_guided_provider_execution_time_recheck(
        *response_context,
        preimages=preimages,
        items=response_context[10].items,
        evaluation_at=RECHECK_AT,
    )
    review = prepare_guided_provider_request_materialization_review(
        *response_context,
        recheck,
        preimages=preimages,
        evaluation_at=PREPARE_REVIEW_AT,
    )
    return (*response_context, recheck, review), preimages


class GuidedProviderRequestMaterializationReviewTests(unittest.TestCase):
    def test_ready_recheck_creates_only_non_executable_review_candidates(self) -> None:
        context, preimages = _prepared_materialization_review()
        review = context[-1]
        assessed = assess_guided_provider_request_materialization_review(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_REVIEW_AT,
        )
        safe = assessed.to_dict()
        handoff = safe["provider_request_materialization_review"]

        self.assertEqual(
            GuidedProviderRequestMaterializationReviewStatus.REVIEW_REQUIRED,
            assessed.status,
        )
        self.assertEqual(
            "capture_private_provider_request_materialization_response",
            assessed.next_action,
        )
        self.assertTrue(safe["requires_user_response"])
        self.assertTrue(safe["requires_user_review"])
        self.assertTrue(safe["requires_user_decision"])
        self.assertEqual(
            ["prepare_materialization", "request_smaller", "cancel"],
            safe["response_options"],
        )
        self.assertEqual(2, assessed.candidate_count)
        self.assertEqual(2, assessed.bound_request_count)
        self.assertEqual(5, assessed.accepted_max_request_count)
        self.assertEqual(52_000, assessed.estimated_bound_first_paid_tier_google_cost_usd_micros)
        self.assertEqual(136_000, assessed.accepted_max_first_paid_tier_google_cost_usd_micros)
        self.assertTrue(handoff["fresh_execution_time_recheck_bound"])
        self.assertTrue(handoff["same_exact_target_preimages_revalidated"])
        self.assertTrue(handoff["must_reassess_before_response_capture"])
        self.assertTrue(handoff["pricing_profile_recheck_bound"])
        self.assertTrue(handoff["policy_profile_recheck_bound"])
        self.assertTrue(handoff["retention_profile_recheck_bound"])
        self.assertTrue(handoff["billing_region_classification_recheck_bound"])
        self.assertTrue(handoff["credential_availability_recheck_bound"])
        self.assertTrue(handoff["all_credentials_available"])
        self.assertTrue(handoff["serpapi_plan_state_recheck_bound"])
        self.assertTrue(handoff["serpapi_plan_state_sufficient"])
        self.assertFalse(handoff["monthly_free_usage_remaining_checked"])
        self.assertFalse(handoff["cost_estimate_is_hard_currency_cap"])
        self.assertEqual(2, handoff["request_contract_candidate_count"])
        self.assertEqual(2, handoff["eligible_for_materialization_response_count"])
        self.assertFalse(handoff["request_contract_candidates_are_executable"])
        self.assertEqual(
            0,
            handoff[
                "executable_provider_request_contract_count_created_by_review"
            ],
        )
        self.assertEqual(0, handoff["http_request_count_created_by_review"])
        self.assertFalse(handoff["execution_authority_active"])
        self.assertEqual(0, handoff["provider_call_count_observed"])
        self.assertFalse(handoff["provider_calls_permitted"])
        self.assertEqual(DecisionState.CANDIDATE, review.decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, review.evidence_state)
        self.assertEqual("candidate", handoff["decision_state"])
        self.assertEqual("unverified", handoff["evidence_state"])
        self.assertIn(
            "provider_request_materialization_review",
            safe["needs_verification"],
        )

    def test_candidate_count_can_exceed_scope_topic_count_within_exact_cap(self) -> None:
        context, preimages = _prepared_two_requests_for_one_scope_item()
        assessed = assess_guided_provider_request_materialization_review(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_REVIEW_AT,
        )

        self.assertEqual(1, assessed.accepted_scope_item_count)
        self.assertEqual(2, assessed.candidate_count)
        self.assertEqual(2, assessed.bound_request_count)
        self.assertEqual(2, assessed.accepted_max_request_count)
        self.assertEqual(2, len(assessed._items))

    def test_private_payload_shows_exact_values_but_keeps_provider_ids_redacted(self) -> None:
        context, preimages = _prepared_materialization_review()
        review = assess_guided_provider_request_materialization_review(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_REVIEW_AT,
        )
        safe_rendered = json.dumps(review.to_dict(), ensure_ascii=False)
        private = review.to_ephemeral_private_review_payload()
        private_rendered = json.dumps(private, ensure_ascii=False)
        by_topic = {
            item["topic"]: item
            for item in private["request_contract_candidates"]
        }
        identity = by_topic[GuidedEvidenceTopic.PLACE_IDENTITY.value]
        details = by_topic[GuidedEvidenceTopic.CURRENT_OPENING_HOURS.value]

        self.assertNotIn(PRIVATE_QUERY, safe_rendered)
        self.assertNotIn("guided-private-location", safe_rendered)
        self.assertEqual(
            PRIVATE_QUERY,
            identity["provider_transmitted_values"]["text_query"],
        )
        self.assertEqual(
            "guided-private-location",
            identity["local_review_context"]["stable_local_location_id"],
        )
        self.assertEqual(
            ["provider_place_id"],
            details["redacted_bound_provider_identifier_fields"],
        )
        provider_place_id = preimages[1].target.endpoint.provider_place_id
        self.assertNotIn(provider_place_id, safe_rendered)
        self.assertNotIn(provider_place_id, private_rendered)
        self.assertFalse(private["provider_identifier_values_exposed"])
        self.assertFalse(private["credentials_exposed"])
        self.assertFalse(private["request_contract_candidates_are_executable"])
        self.assertEqual(0, private["http_request_count_created_by_review"])

    def test_all_four_typed_materialization_surfaces_are_derived(self) -> None:
        default_context, default_preimages = _prepared_materialization_review()
        default = assess_guided_provider_request_materialization_review(
            *default_context,
            preimages=default_preimages,
            evaluation_at=ASSESS_REVIEW_AT,
        ).to_dict()["provider_request_materialization_review"]["items"]
        default_kinds = {item["materialization_kind"] for item in default}
        self.assertEqual(
            {"google_places_text_search", "google_place_details"},
            default_kinds,
        )

        route_context, route_preimages = _prepared_single_capability_review(
            GuidedEvidenceTopic.ROUTE,
            GuidedProviderCapability.GOOGLE_ROUTES,
            _google_item(
                GuidedEvidenceTopic.ROUTE,
                GuidedProviderCapability.GOOGLE_ROUTES,
                1,
            ),
            _route_request(),
        )
        route = assess_guided_provider_request_materialization_review(
            *route_context,
            preimages=route_preimages,
            evaluation_at=ASSESS_REVIEW_AT,
        ).to_ephemeral_private_review_payload()["request_contract_candidates"][0]
        self.assertEqual("google_routes_compute_routes", route["materialization_kind"])
        self.assertEqual(
            ["destination_provider_place_id", "origin_provider_place_id"],
            route["redacted_bound_provider_identifier_fields"],
        )
        self.assertEqual("driving", route["provider_transmitted_values"]["mode"])

        serp_context, serp_preimages = _prepared_single_capability_review(
            GuidedEvidenceTopic.LODGING,
            GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
            _serpapi_item(GuidedEvidenceTopic.LODGING),
            _hotel_request(),
        )
        serp = assess_guided_provider_request_materialization_review(
            *serp_context,
            preimages=serp_preimages,
            evaluation_at=ASSESS_REVIEW_AT,
        )
        serp_safe = serp.to_dict()["provider_request_materialization_review"]
        serp_private = serp.to_ephemeral_private_review_payload()[
            "request_contract_candidates"
        ][0]
        self.assertEqual("serpapi_google_hotels", serp_private["materialization_kind"])
        self.assertEqual(PRIVATE_HOTEL_QUERY, serp_private["provider_transmitted_values"]["query"])
        self.assertEqual(1, serp_safe["bound_serpapi_request_count"])
        self.assertEqual(1, serp_safe["serpapi_bound_plan_credit_count"])
        self.assertFalse(
            serp_safe["all_bound_provider_costs_have_currency_list_rate_estimates"]
        )

    def test_blocked_recheck_cannot_prepare_a_materialization_review(self) -> None:
        base_context, _ = _prepared_recheck()
        unavailable_items = tuple(
            replace(
                item,
                credential_status=GuidedProviderCredentialStatus.UNAVAILABLE,
            )
            for item in base_context[10].items
        )
        blocked_context, preimages = _prepared_recheck(items=unavailable_items)
        with self.assertRaises(ValueError):
            prepare_guided_provider_request_materialization_review(
                *blocked_context,
                preimages=preimages,
                evaluation_at=PREPARE_REVIEW_AT,
            )

    def test_review_cannot_outlive_the_five_minute_execution_recheck(self) -> None:
        context, preimages = _prepared_materialization_review()
        with self.assertRaises(ValueError):
            assess_guided_provider_request_materialization_review(
                *context,
                preimages=preimages,
                evaluation_at=RECHECK_AT + timedelta(minutes=5),
            )
        recheck_context, recheck_preimages = _prepared_recheck()
        with self.assertRaises(ValueError):
            prepare_guided_provider_request_materialization_review(
                *recheck_context,
                preimages=recheck_preimages,
                evaluation_at=RECHECK_AT + timedelta(minutes=5),
            )

    def test_review_fails_closed_on_preimage_context_recheck_and_time_drift(self) -> None:
        context, preimages = _prepared_materialization_review()
        changed_preimages = _default_preimages(
            identity=replace(
                preimages[0].target,
                text_query=PRIVATE_QUERY + " changed",
                intent_id="",
            )
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_materialization_review(
                *context,
                preimages=changed_preimages,
                evaluation_at=ASSESS_REVIEW_AT,
            )
        changed_context = (
            replace(context[0], constraints=context[0].must_do),
            *context[1:],
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_materialization_review(
                *changed_context,
                preimages=preimages,
                evaluation_at=ASSESS_REVIEW_AT,
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_materialization_review(
                *context,
                preimages=preimages,
                evaluation_at=PREPARE_REVIEW_AT - timedelta(microseconds=1),
            )

        accepted_context = context[:-2]
        other_recheck = prepare_guided_provider_execution_time_recheck(
            *accepted_context,
            preimages=preimages,
            items=accepted_context[10].items,
            evaluation_at=RECHECK_AT + timedelta(seconds=1),
        )
        mismatched = (*context[:-2], other_recheck, context[-1])
        with self.assertRaises(ValueError):
            assess_guided_provider_request_materialization_review(
                *mismatched,
                preimages=preimages,
                evaluation_at=ASSESS_REVIEW_AT,
            )

    def test_safe_views_and_repr_hide_private_material_and_exact_times(self) -> None:
        context, preimages = _prepared_materialization_review()
        review = context[-1]
        assessed = assess_guided_provider_request_materialization_review(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_REVIEW_AT,
        )
        details = preimages[1].target
        rendered = "\n".join(
            (
                repr(review),
                repr(assessed),
                json.dumps(assessed.to_dict(), ensure_ascii=False),
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
            PREPARE_REVIEW_AT.isoformat(),
            EXPIRES_AT.isoformat(),
        ):
            self.assertNotIn(private_value, rendered)
        self.assertEqual(
            {
                "status",
                "next_action",
                "accepted_scope_item_count",
                "accepted_max_request_count",
                "candidate_count",
                "bound_request_count",
                "max_request_count",
                "bound_source_line_reference_count",
                "bound_google_request_count",
                "bound_serpapi_request_count",
                "user_stated_source_line_count",
                "tentative_source_line_count",
                "ai_candidate_source_line_count",
                "estimated_bound_first_paid_tier_google_cost_usd_micros",
                "accepted_max_first_paid_tier_google_cost_usd_micros",
                "serpapi_bound_plan_credit_count",
                "serpapi_plan_credit_cap",
                "bound_request_capability_counts",
                "data_categories",
                "host_attestation_fresh",
                "tentative_fields",
                "needs_verification",
                "_items",
                "_prepared_at",
                "_expires_at",
                "_context_fingerprint",
                "contract_version",
            },
            {item.name for item in fields(type(review))},
        )
        self.assertNotIn("preimages", {item.name for item in fields(type(review))})

    def test_candidates_and_review_are_token_gated(self) -> None:
        context, preimages = _prepared_materialization_review()
        review = assess_guided_provider_request_materialization_review(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_REVIEW_AT,
        )
        candidate = review._items[0]
        with self.assertRaises(ValueError):
            replace(candidate, topic=GuidedEvidenceTopic.ROUTE)
        with self.assertRaises(ValueError):
            replace(review, next_action="materialize_provider_request")
        with self.assertRaises(ValueError):
            GuidedProviderRequestContractCandidate(
                topic=candidate.topic,
                capability=candidate.capability,
                request_profile=candidate.request_profile,
                materialization_kind=candidate.materialization_kind,
                pricing_profile=candidate.pricing_profile,
                policy_profile=candidate.policy_profile,
                retention_profile=candidate.retention_profile,
                target_kind=candidate.target_kind,
                source_kind=candidate.source_kind,
                source_line_reference_count=1,
                user_stated_source_line_count=1,
                tentative_source_line_count=0,
                ai_candidate_source_line_count=0,
                _provider_transmitted_values=candidate._provider_transmitted_values,
                _local_review_context=candidate._local_review_context,
                _redacted_provider_identifier_fields=(
                    candidate._redacted_provider_identifier_fields
                ),
            )
        with self.assertRaises(ValueError):
            GuidedProviderRequestMaterializationReview(
                status=GuidedProviderRequestMaterializationReviewStatus.REVIEW_REQUIRED,
                next_action="capture_private_provider_request_materialization_response",
                accepted_scope_item_count=1,
                accepted_max_request_count=1,
                candidate_count=1,
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

    def test_public_surface_has_review_but_no_response_http_or_execution_path(self) -> None:
        for name in (
            "GUIDED_PROVIDER_REQUEST_MATERIALIZATION_REVIEW_VERSION",
            "GuidedProviderRequestContractCandidate",
            "GuidedProviderRequestMaterializationKind",
            "GuidedProviderRequestMaterializationReview",
            "GuidedProviderRequestMaterializationReviewStatus",
            "assess_guided_provider_request_materialization_review",
            "prepare_guided_provider_request_materialization_review",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        self.assertEqual(
            "guided-provider-request-materialization-review/v1",
            GUIDED_PROVIDER_REQUEST_MATERIALIZATION_REVIEW_VERSION,
        )
        for unsupported_name in (
            "capture_guided_provider_request_materialization_response",
            "materialize_guided_provider_request",
            "authorize_guided_provider_execution",
            "build_guided_provider_http_request",
            "execute_guided_provider_request",
            "call_guided_provider",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        tree = ast.parse(inspect.getsource(materialization_module))
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
