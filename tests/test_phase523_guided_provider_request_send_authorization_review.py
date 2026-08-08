"""Phase 5.23 exact private provider request send-authorization review."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace
from datetime import timedelta

import trip_planner
import trip_planner.guided_provider_request_send_authorization_review as review_module
from tests.test_phase513_guided_provider_preflight import EXPIRES_AT, _google_item
from tests.test_phase516_guided_provider_execution_target_bindings import (
    PRIVATE_HOTEL_QUERY,
    PRIVATE_QUERY,
    _default_preimages,
    _hotel_request,
    _route_request,
    _serpapi_item,
    details_fingerprint,
)
from tests.test_phase517_guided_provider_execution_authorization_review import (
    REVIEW_AT,
)
from tests.test_phase518_guided_provider_execution_authorization_response import (
    CAPTURE_AT,
)
from tests.test_phase519_guided_provider_execution_time_recheck import RECHECK_AT
from tests.test_phase520_guided_provider_request_materialization_review import (
    PREPARE_REVIEW_AT,
    _prepared_single_capability_review,
    _prepared_two_requests_for_one_scope_item,
)
from tests.test_phase521_guided_provider_request_materialization_response import (
    CAPTURE_MATERIALIZATION_RESPONSE_AT,
    _captured_materialization_response,
)
from tests.test_phase522_guided_provider_request_contract_materialization import (
    ASSESS_CONTRACTS_AT,
    MATERIALIZE_CONTRACTS_AT,
    _materialized_contracts,
)
from trip_planner.guided_evidence_plan import GuidedEvidenceTopic
from trip_planner.guided_provider_request_send_authorization_review import (
    GUIDED_PROVIDER_REQUEST_SEND_AUTHORIZATION_REVIEW_VERSION,
    GuidedProviderRequestSendAuthorizationReviewStatus,
    assess_guided_provider_request_send_authorization_review,
    prepare_guided_provider_request_send_authorization_review,
)
from trip_planner.guided_provider_scope import GuidedProviderCapability
from trip_planner.models import DecisionState, EvidenceState


PREPARE_SEND_REVIEW_AT = ASSESS_CONTRACTS_AT + timedelta(seconds=15)
ASSESS_SEND_REVIEW_AT = PREPARE_SEND_REVIEW_AT + timedelta(seconds=15)


def _prepared_send_review(
    *,
    materialized_context: tuple[object, ...] | None = None,
    preimages: tuple[object, ...] | None = None,
):
    if materialized_context is None:
        exact_context, exact_preimages = _materialized_contracts()
    else:
        if preimages is None:
            raise ValueError("explicit context requires explicit preimages")
        exact_context = materialized_context
        exact_preimages = preimages
    review = prepare_guided_provider_request_send_authorization_review(
        *exact_context,
        preimages=exact_preimages,
        evaluation_at=PREPARE_SEND_REVIEW_AT,
    )
    return (*exact_context, review), exact_preimages


def _materialized_from_review(
    review_context: tuple[object, ...],
    preimages: tuple[object, ...],
):
    response_context, exact_preimages = _captured_materialization_response(
        context=review_context,
        preimages=preimages,
    )
    return _materialized_contracts(
        response_context=response_context,
        preimages=exact_preimages,
    )


class GuidedProviderRequestSendAuthorizationReviewTests(unittest.TestCase):
    def test_fresh_materialization_prepares_only_a_typed_private_review(
        self,
    ) -> None:
        context, preimages = _prepared_send_review()
        review = context[-1]
        assessed = assess_guided_provider_request_send_authorization_review(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_SEND_REVIEW_AT,
        )
        safe = assessed.to_dict()
        handoff = safe["provider_request_send_authorization_review"]

        self.assertIs(review, assessed)
        self.assertEqual(
            GuidedProviderRequestSendAuthorizationReviewStatus.REVIEW_REQUIRED,
            assessed.status,
        )
        self.assertEqual(
            "capture_private_provider_request_send_authorization_response",
            assessed.next_action,
        )
        self.assertTrue(safe["requires_user_response"])
        self.assertTrue(safe["requires_user_review"])
        self.assertTrue(safe["requires_user_decision"])
        self.assertEqual(
            ["accept_send", "request_smaller", "cancel"],
            safe["response_options"],
        )
        self.assertTrue(handoff["exact_request_contract_materialization_bound"])
        self.assertTrue(handoff["same_exact_target_preimages_revalidated"])
        self.assertTrue(handoff["short_lived_execution_recheck_preserved"])
        self.assertTrue(handoff["must_reassess_before_response_capture"])
        self.assertEqual(2, assessed.contract_count)
        self.assertEqual(2, handoff["materialized_request_contract_count"])
        self.assertEqual(2, handoff["reviewed_request_contract_count"])
        self.assertEqual(2, handoff["eligible_for_send_response_count"])
        self.assertEqual(2, handoff["bound_request_count"])
        self.assertEqual(5, handoff["accepted_max_request_count"])
        self.assertTrue(handoff["private_review_payload_available"])
        self.assertFalse(handoff["provider_request_contracts_are_executable"])
        self.assertFalse(handoff["provider_request_contracts_are_sendable"])
        self.assertFalse(handoff["transport_endpoints_selected"])
        self.assertFalse(handoff["http_methods_selected"])
        self.assertFalse(handoff["credential_slots_bound"])
        self.assertFalse(handoff["credential_values_included"])
        self.assertEqual(0, handoff["http_request_count_created_by_review"])
        self.assertFalse(handoff["send_response_captured"])
        self.assertFalse(handoff["send_authorization_active"])
        self.assertFalse(handoff["execution_authority_active"])
        self.assertEqual(0, handoff["provider_call_count_observed"])
        self.assertFalse(handoff["provider_calls_permitted"])
        self.assertEqual(DecisionState.CANDIDATE, assessed.decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, assessed.evidence_state)
        self.assertIn(
            "provider_request_send_authorization_review",
            safe["needs_verification"],
        )

    def test_private_payload_shows_exact_values_but_redacts_provider_ids(
        self,
    ) -> None:
        context, preimages = _prepared_send_review()
        assessed = assess_guided_provider_request_send_authorization_review(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_SEND_REVIEW_AT,
        )
        safe_rendered = json.dumps(assessed.to_dict(), ensure_ascii=False)
        private = assessed.to_ephemeral_private_review_payload()
        private_rendered = json.dumps(private, ensure_ascii=False)
        by_kind = {
            item["materialization_kind"]: item
            for item in private["request_contracts"]
        }
        identity = by_kind["google_places_text_search"]
        details = by_kind["google_place_details"]
        provider_place_id = preimages[1].target.endpoint.provider_place_id

        self.assertNotIn(PRIVATE_QUERY, safe_rendered)
        self.assertNotIn("guided-private-location", safe_rendered)
        self.assertEqual(
            PRIVATE_QUERY,
            identity["provider_transmitted_values"]["text_query"],
        )
        self.assertEqual(
            "guided-private-location",
            identity["local_result_binding"]["stable_local_location_id"],
        )
        self.assertEqual(
            ["provider_place_id"],
            details["redacted_bound_provider_identifier_fields"],
        )
        self.assertNotIn(provider_place_id, safe_rendered)
        self.assertNotIn(provider_place_id, private_rendered)
        self.assertNotIn(details_fingerprint(preimages[1].target), private_rendered)
        self.assertTrue(
            private[
                "exact_non_identifier_provider_transmitted_values_included"
            ]
        )
        self.assertTrue(private["exact_local_result_binding_values_included"])
        self.assertFalse(private["provider_identifier_values_exposed"])
        self.assertFalse(private["credentials_exposed"])
        self.assertFalse(private["transport_endpoints_selected"])
        self.assertFalse(private["http_methods_selected"])
        self.assertFalse(private["http_requests_created"])
        self.assertFalse(private["send_response_captured"])
        self.assertFalse(private["send_authorization_active"])
        self.assertFalse(private["provider_calls_permitted"])
        for item in private["request_contracts"]:
            self.assertFalse(item["exact_private_values_included"])
            self.assertTrue(
                item[
                    "exact_non_identifier_provider_transmitted_values_included"
                ]
            )
            self.assertTrue(item["exact_local_result_binding_values_included"])

    def test_route_and_hotel_private_surfaces_remain_non_sendable(self) -> None:
        route_review, route_preimages = _prepared_single_capability_review(
            GuidedEvidenceTopic.ROUTE,
            GuidedProviderCapability.GOOGLE_ROUTES,
            _google_item(
                GuidedEvidenceTopic.ROUTE,
                GuidedProviderCapability.GOOGLE_ROUTES,
                1,
            ),
            _route_request(),
        )
        route_materialized, route_preimages = _materialized_from_review(
            route_review,
            route_preimages,
        )
        route_context, route_preimages = _prepared_send_review(
            materialized_context=route_materialized,
            preimages=route_preimages,
        )
        route_reviewed = assess_guided_provider_request_send_authorization_review(
            *route_context,
            preimages=route_preimages,
            evaluation_at=ASSESS_SEND_REVIEW_AT,
        )
        route_private = route_reviewed.to_ephemeral_private_review_payload()
        route_item = route_private["request_contracts"][0]
        route_rendered = json.dumps(route_private, ensure_ascii=False)
        self.assertEqual("google_routes_compute_routes", route_item["materialization_kind"])
        self.assertEqual("driving", route_item["provider_transmitted_values"]["mode"])
        self.assertEqual(
            ["destination_provider_place_id", "origin_provider_place_id"],
            route_item["redacted_bound_provider_identifier_fields"],
        )
        self.assertNotIn(
            route_preimages[0].target.origin.provider_place_id,
            route_rendered,
        )
        self.assertNotIn(
            route_preimages[0].target.destination.provider_place_id,
            route_rendered,
        )

        hotel_review, hotel_preimages = _prepared_single_capability_review(
            GuidedEvidenceTopic.LODGING,
            GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
            _serpapi_item(GuidedEvidenceTopic.LODGING),
            _hotel_request(),
        )
        hotel_materialized, hotel_preimages = _materialized_from_review(
            hotel_review,
            hotel_preimages,
        )
        hotel_context, hotel_preimages = _prepared_send_review(
            materialized_context=hotel_materialized,
            preimages=hotel_preimages,
        )
        hotel_reviewed = assess_guided_provider_request_send_authorization_review(
            *hotel_context,
            preimages=hotel_preimages,
            evaluation_at=ASSESS_SEND_REVIEW_AT,
        )
        hotel_safe = hotel_reviewed.to_dict()[
            "provider_request_send_authorization_review"
        ]
        hotel_private = hotel_reviewed.to_ephemeral_private_review_payload()
        hotel_item = hotel_private["request_contracts"][0]
        self.assertEqual("serpapi_google_hotels", hotel_item["materialization_kind"])
        self.assertEqual(
            PRIVATE_HOTEL_QUERY,
            hotel_item["provider_transmitted_values"]["query"],
        )
        self.assertEqual(1, hotel_safe["bound_serpapi_request_count"])
        self.assertFalse(
            hotel_safe["all_bound_provider_costs_have_currency_list_rate_estimates"]
        )
        self.assertFalse(hotel_item["provider_request_contract_is_sendable"])

    def test_multiple_requests_for_one_scope_item_remain_distinct(self) -> None:
        review_context, preimages = _prepared_two_requests_for_one_scope_item()
        materialized_context, preimages = _materialized_from_review(
            review_context,
            preimages,
        )
        context, preimages = _prepared_send_review(
            materialized_context=materialized_context,
            preimages=preimages,
        )
        assessed = assess_guided_provider_request_send_authorization_review(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_SEND_REVIEW_AT,
        )
        private = assessed.to_ephemeral_private_review_payload()

        self.assertEqual(1, assessed.accepted_scope_item_count)
        self.assertEqual(2, assessed.contract_count)
        self.assertEqual(2, assessed.bound_request_count)
        self.assertEqual(2, assessed.accepted_max_request_count)
        self.assertEqual(2, len(private["request_contracts"]))
        self.assertEqual(
            2,
            len(
                {
                    item["provider_transmitted_values"]["text_query"]
                    for item in private["request_contracts"]
                }
            ),
        )

    def test_serpapi_plan_context_stays_aggregate_only(self) -> None:
        review_context, preimages = _prepared_single_capability_review(
            GuidedEvidenceTopic.LODGING,
            GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
            _serpapi_item(GuidedEvidenceTopic.LODGING),
            _hotel_request(),
        )
        materialized_context, preimages = _materialized_from_review(
            review_context,
            preimages,
        )
        context, preimages = _prepared_send_review(
            materialized_context=materialized_context,
            preimages=preimages,
        )
        safe = assess_guided_provider_request_send_authorization_review(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_SEND_REVIEW_AT,
        ).to_dict()["provider_request_send_authorization_review"]
        rendered = json.dumps(safe, ensure_ascii=False)

        self.assertEqual(1, safe["serpapi_bound_plan_credit_count"])
        self.assertEqual(1, safe["serpapi_plan_credit_cap"])
        self.assertEqual(
            0,
            safe["estimated_bound_first_paid_tier_google_cost_usd_micros"],
        )
        self.assertNotIn("serpapi_remaining_plan_credits", rendered)
        self.assertNotIn("serpapi_automatic_renewal_enabled", rendered)
        self.assertNotIn(PRIVATE_HOTEL_QUERY, rendered)

    def test_review_cannot_outlive_the_execution_recheck(self) -> None:
        materialized_context, preimages = _materialized_contracts()
        with self.assertRaises(ValueError):
            prepare_guided_provider_request_send_authorization_review(
                *materialized_context,
                preimages=preimages,
                evaluation_at=RECHECK_AT + timedelta(minutes=5),
            )

        context, preimages = _prepared_send_review(
            materialized_context=materialized_context,
            preimages=preimages,
        )
        self.assertEqual(RECHECK_AT + timedelta(minutes=5), context[-1]._expires_at)
        with self.assertRaises(ValueError):
            assess_guided_provider_request_send_authorization_review(
                *context,
                preimages=preimages,
                evaluation_at=RECHECK_AT + timedelta(minutes=5),
            )

    def test_preimage_context_review_and_time_drift_fail_closed(self) -> None:
        context, preimages = _prepared_send_review()
        changed_preimages = _default_preimages(
            identity=replace(
                preimages[0].target,
                text_query=PRIVATE_QUERY + " changed",
                intent_id="",
            )
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_send_authorization_review(
                *context,
                preimages=changed_preimages,
                evaluation_at=ASSESS_SEND_REVIEW_AT,
            )

        changed_context = (
            replace(context[0], constraints=context[0].must_do),
            *context[1:],
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_send_authorization_review(
                *changed_context,
                preimages=preimages,
                evaluation_at=ASSESS_SEND_REVIEW_AT,
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_send_authorization_review(
                *context,
                preimages=preimages,
                evaluation_at=PREPARE_SEND_REVIEW_AT - timedelta(microseconds=1),
            )

    def test_safe_views_repr_and_token_gate_hide_exact_binding_state(self) -> None:
        context, preimages = _prepared_send_review()
        review = context[-1]
        assessed = assess_guided_provider_request_send_authorization_review(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_SEND_REVIEW_AT,
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
            CAPTURE_MATERIALIZATION_RESPONSE_AT.isoformat(),
            MATERIALIZE_CONTRACTS_AT.isoformat(),
            PREPARE_SEND_REVIEW_AT.isoformat(),
            EXPIRES_AT.isoformat(),
        ):
            self.assertNotIn(private_value, rendered)
        self.assertEqual(
            {
                "status",
                "next_action",
                "_materialization_review",
                "_prepared_at",
                "_expires_at",
                "_context_fingerprint",
                "contract_version",
            },
            {item.name for item in fields(type(review))},
        )
        self.assertNotIn("preimages", {item.name for item in fields(type(review))})
        with self.assertRaises(ValueError):
            replace(review, next_action="send_provider_request")

    def test_public_surface_has_review_but_no_response_send_or_network_path(
        self,
    ) -> None:
        for name in (
            "GUIDED_PROVIDER_REQUEST_SEND_AUTHORIZATION_REVIEW_VERSION",
            "GuidedProviderRequestSendAuthorizationReview",
            "GuidedProviderRequestSendAuthorizationReviewStatus",
            "assess_guided_provider_request_send_authorization_review",
            "prepare_guided_provider_request_send_authorization_review",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        self.assertEqual(
            "guided-provider-request-send-authorization-review/v1",
            GUIDED_PROVIDER_REQUEST_SEND_AUTHORIZATION_REVIEW_VERSION,
        )
        for unsupported_name in (
            "capture_guided_provider_request_send_authorization_response",
            "authorize_guided_provider_request_send",
            "send_guided_provider_request",
            "build_guided_provider_http_request",
            "execute_guided_provider_request",
            "call_guided_provider",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        tree = ast.parse(inspect.getsource(review_module))
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
