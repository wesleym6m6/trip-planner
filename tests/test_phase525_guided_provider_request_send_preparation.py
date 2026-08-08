"""Phase 5.25 exact private provider request send preparation."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace
from datetime import timedelta

import trip_planner
import trip_planner.guided_provider_request_send_preparation as preparation_module
from tests.test_phase513_guided_provider_preflight import _google_item
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
)
from tests.test_phase522_guided_provider_request_contract_materialization import (
    MATERIALIZE_CONTRACTS_AT,
)
from tests.test_phase523_guided_provider_request_send_authorization_review import (
    PREPARE_SEND_REVIEW_AT,
)
from tests.test_phase524_guided_provider_request_send_authorization_response import (
    ASSESS_SEND_AUTHORIZATION_RESPONSE_AT,
    CAPTURE_SEND_AUTHORIZATION_RESPONSE_AT,
    _captured_send_authorization_response,
    _prepared_send_review_from_materialization_review,
)
from trip_planner.guided_evidence_plan import GuidedEvidenceTopic
from trip_planner.guided_provider_request_send_authorization_response import (
    GuidedProviderRequestSendAuthorizationResponseKind,
)
from trip_planner.guided_provider_request_send_preparation import (
    GUIDED_PROVIDER_REQUEST_SEND_PREPARATION_VERSION,
    GuidedProviderRequestCredentialSlot,
    GuidedProviderRequestHTTPMethod,
    GuidedProviderRequestSendPreparation,
    GuidedProviderRequestSendPreparationStatus,
    GuidedProviderRequestTransportProfile,
    GuidedProviderRequestValuePlacement,
    assess_guided_provider_request_send_preparation,
    prepare_guided_provider_request_send_preparation,
)
from trip_planner.guided_provider_scope import GuidedProviderCapability
from trip_planner.models import DecisionState, EvidenceState


PREPARE_SEND_PREPARATION_AT = (
    ASSESS_SEND_AUTHORIZATION_RESPONSE_AT + timedelta(seconds=2)
)
ASSESS_SEND_PREPARATION_AT = PREPARE_SEND_PREPARATION_AT + timedelta(seconds=2)


def _prepared_send_preparation(
    *,
    response_context: tuple[object, ...] | None = None,
    preimages: tuple[object, ...] | None = None,
):
    if response_context is None:
        exact_context, exact_preimages = _captured_send_authorization_response()
    else:
        if preimages is None:
            raise ValueError("explicit context requires explicit preimages")
        exact_context = response_context
        exact_preimages = preimages
    preparation = prepare_guided_provider_request_send_preparation(
        *exact_context,
        preimages=exact_preimages,
        evaluation_at=PREPARE_SEND_PREPARATION_AT,
    )
    return (*exact_context, preparation), exact_preimages


def _accepted_response_from_materialization_review(
    review_context: tuple[object, ...],
    preimages: tuple[object, ...],
):
    send_review_context, exact_preimages = (
        _prepared_send_review_from_materialization_review(
            review_context,
            preimages,
        )
    )
    return _captured_send_authorization_response(
        context=send_review_context,
        preimages=exact_preimages,
    )


class GuidedProviderRequestSendPreparationTests(unittest.TestCase):
    def test_accept_send_prepares_only_transport_metadata_bindings(self) -> None:
        context, preimages = _prepared_send_preparation()
        preparation = context[-1]
        assessed = assess_guided_provider_request_send_preparation(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_SEND_PREPARATION_AT,
        )
        safe = assessed.to_dict()
        handoff = safe["provider_request_send_preparation"]

        self.assertEqual(
            GuidedProviderRequestSendPreparationStatus
            .READY_FOR_PRIVATE_PROVIDER_REQUEST_CREDENTIAL_BINDING_REVIEW,
            assessed.status,
        )
        self.assertEqual(
            "prepare_private_provider_request_credential_binding_review",
            assessed.next_action,
        )
        self.assertFalse(safe["requires_user_response"])
        self.assertFalse(safe["requires_user_review"])
        self.assertFalse(safe["requires_user_decision"])
        self.assertEqual(2, preparation.binding_count)
        self.assertEqual(2, assessed.binding_count)
        self.assertEqual(2, assessed.contract_count)
        self.assertEqual(2, handoff["transport_binding_count"])
        self.assertEqual(
            2,
            handoff["eligible_for_credential_binding_review_count"],
        )
        self.assertTrue(handoff["exact_accept_send_response_bound"])
        self.assertTrue(handoff["same_exact_target_preimages_revalidated"])
        self.assertTrue(handoff["same_exact_materialized_contracts_revalidated"])
        self.assertTrue(handoff["allowlisted_transport_profiles_bound"])
        self.assertTrue(handoff["transport_endpoints_selected"])
        self.assertTrue(handoff["http_methods_selected"])
        self.assertTrue(handoff["provider_field_placements_selected"])
        self.assertTrue(handoff["credential_slots_bound"])
        self.assertFalse(handoff["credential_values_bound"])
        self.assertFalse(handoff["credential_values_included"])
        self.assertFalse(handoff["environment_read"])
        self.assertFalse(handoff["vault_accessed"])
        self.assertFalse(handoff["network_accessed"])
        self.assertFalse(handoff["provider_request_contracts_are_executable"])
        self.assertFalse(handoff["provider_request_contracts_are_sendable"])
        self.assertEqual(0, handoff["http_request_count_created_by_preparation"])
        self.assertFalse(handoff["immediate_send_authority_granted"])
        self.assertFalse(handoff["send_authority_active"])
        self.assertFalse(handoff["execution_authority_active"])
        self.assertEqual(0, handoff["provider_call_count_observed"])
        self.assertFalse(handoff["provider_calls_permitted"])
        self.assertEqual(DecisionState.CANDIDATE, preparation.decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, preparation.evidence_state)
        self.assertEqual(DecisionState.CANDIDATE, assessed.decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, assessed.evidence_state)
        self.assertIn("provider_request_send_preparation", safe["needs_verification"])
        for item in handoff["items"]:
            self.assertTrue(item["transport_endpoint_selected"])
            self.assertTrue(item["http_method_selected"])
            self.assertTrue(item["credential_slot_bound"])
            self.assertFalse(item["credential_value_bound"])
            self.assertFalse(item["http_request_created"])
            self.assertFalse(item["provider_call_permitted"])
            self.assertEqual("candidate", item["decision_state"])
            self.assertEqual("unverified", item["evidence_state"])

    def test_all_four_contract_surfaces_use_exact_allowlisted_transport_profiles(
        self,
    ) -> None:
        default_context, default_preimages = _prepared_send_preparation()
        safe_items = {
            item["materialization_kind"]: item
            for item in assess_guided_provider_request_send_preparation(
                *default_context,
                preimages=default_preimages,
                evaluation_at=ASSESS_SEND_PREPARATION_AT,
            ).to_dict()["provider_request_send_preparation"]["items"]
        }

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
        route_response, route_preimages = (
            _accepted_response_from_materialization_review(
                route_review,
                route_preimages,
            )
        )
        route_context, route_preimages = _prepared_send_preparation(
            response_context=route_response,
            preimages=route_preimages,
        )
        route_item = assess_guided_provider_request_send_preparation(
            *route_context,
            preimages=route_preimages,
            evaluation_at=ASSESS_SEND_PREPARATION_AT,
        ).to_dict()["provider_request_send_preparation"]["items"][0]
        safe_items[route_item["materialization_kind"]] = route_item

        hotel_review, hotel_preimages = _prepared_single_capability_review(
            GuidedEvidenceTopic.LODGING,
            GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
            _serpapi_item(GuidedEvidenceTopic.LODGING),
            _hotel_request(),
        )
        hotel_response, hotel_preimages = (
            _accepted_response_from_materialization_review(
                hotel_review,
                hotel_preimages,
            )
        )
        hotel_context, hotel_preimages = _prepared_send_preparation(
            response_context=hotel_response,
            preimages=hotel_preimages,
        )
        hotel_item = assess_guided_provider_request_send_preparation(
            *hotel_context,
            preimages=hotel_preimages,
            evaluation_at=ASSESS_SEND_PREPARATION_AT,
        ).to_dict()["provider_request_send_preparation"]["items"][0]
        safe_items[hotel_item["materialization_kind"]] = hotel_item

        identity = safe_items["google_places_text_search"]
        self.assertEqual(
            "google_places_text_search_v1",
            identity["transport_profile"],
        )
        self.assertEqual(
            "https://places.googleapis.com/v1/places:searchText",
            identity["endpoint_template"],
        )
        self.assertEqual("POST", identity["http_method"])
        self.assertEqual(
            "google_maps_api_key_header",
            identity["credential"]["slot"],
        )
        self.assertEqual(
            "X-Goog-Api-Key",
            identity["credential"]["provider_field_name"],
        )
        self.assertEqual("header", identity["credential"]["placement"])
        identity_fields = {
            item["contract_field_name"]: (
                item["provider_field_name"],
                item["placement"],
            )
            for item in identity["provider_transmitted_field_bindings"]
        }
        self.assertEqual(("textQuery", "json_body"), identity_fields["text_query"])
        self.assertEqual(("X-Goog-FieldMask", "header"), identity_fields["field_mask"])

        details = safe_items["google_place_details"]
        self.assertEqual("google_place_details_v1", details["transport_profile"])
        self.assertEqual(
            "https://places.googleapis.com/v1/places/{provider_place_id}",
            details["endpoint_template"],
        )
        self.assertEqual("GET", details["http_method"])
        self.assertEqual(
            [
                {
                    "contract_field_name": "provider_place_id",
                    "provider_field_name": "provider_place_id",
                    "placement": "url_path",
                }
            ],
            details["provider_identifier_field_bindings"],
        )

        route = safe_items["google_routes_compute_routes"]
        self.assertEqual(
            "google_routes_compute_routes_v2",
            route["transport_profile"],
        )
        self.assertEqual(
            "https://routes.googleapis.com/directions/v2:computeRoutes",
            route["endpoint_template"],
        )
        self.assertEqual("POST", route["http_method"])
        route_identifiers = {
            item["contract_field_name"]: item["provider_field_name"]
            for item in route["provider_identifier_field_bindings"]
        }
        self.assertEqual(
            "origin.placeId",
            route_identifiers["origin_provider_place_id"],
        )
        self.assertEqual(
            "destination.placeId",
            route_identifiers["destination_provider_place_id"],
        )

        hotel = safe_items["serpapi_google_hotels"]
        self.assertEqual("serpapi_google_hotels_v1", hotel["transport_profile"])
        self.assertEqual("https://serpapi.com/search.json", hotel["endpoint_template"])
        self.assertEqual("GET", hotel["http_method"])
        self.assertEqual(
            "serpapi_api_key_query_parameter",
            hotel["credential"]["slot"],
        )
        self.assertEqual("api_key", hotel["credential"]["provider_field_name"])
        self.assertEqual("query_parameter", hotel["credential"]["placement"])
        self.assertEqual(
            [
                {
                    "provider_field_name": "engine",
                    "value": "google_hotels",
                    "placement": "query_parameter",
                }
            ],
            hotel["fixed_public_parameters"],
        )

    def test_only_exact_accept_send_response_can_prepare_transport_bindings(
        self,
    ) -> None:
        for kind in (
            GuidedProviderRequestSendAuthorizationResponseKind.REQUEST_SMALLER,
            GuidedProviderRequestSendAuthorizationResponseKind.CANCEL,
        ):
            with self.subTest(kind=kind):
                context, preimages = _captured_send_authorization_response(kind)
                with self.assertRaises(ValueError):
                    prepare_guided_provider_request_send_preparation(
                        *context,
                        preimages=preimages,
                        evaluation_at=PREPARE_SEND_PREPARATION_AT,
                    )

    def test_multiple_requests_for_one_scope_item_remain_distinct(self) -> None:
        review_context, preimages = _prepared_two_requests_for_one_scope_item()
        response_context, preimages = (
            _accepted_response_from_materialization_review(
                review_context,
                preimages,
            )
        )
        context, preimages = _prepared_send_preparation(
            response_context=response_context,
            preimages=preimages,
        )
        assessed = assess_guided_provider_request_send_preparation(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_SEND_PREPARATION_AT,
        )
        safe = assessed.to_dict()["provider_request_send_preparation"]

        self.assertEqual(1, assessed.accepted_scope_item_count)
        self.assertEqual(2, assessed.contract_count)
        self.assertEqual(2, assessed.binding_count)
        self.assertEqual(2, safe["transport_binding_count"])
        self.assertEqual(2, safe["accepted_max_request_count"])

    def test_serpapi_credit_and_query_credential_slot_remain_non_currency_claims(
        self,
    ) -> None:
        review_context, preimages = _prepared_single_capability_review(
            GuidedEvidenceTopic.LODGING,
            GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
            _serpapi_item(GuidedEvidenceTopic.LODGING),
            _hotel_request(),
        )
        response_context, preimages = (
            _accepted_response_from_materialization_review(
                review_context,
                preimages,
            )
        )
        context, preimages = _prepared_send_preparation(
            response_context=response_context,
            preimages=preimages,
        )
        safe = assess_guided_provider_request_send_preparation(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_SEND_PREPARATION_AT,
        ).to_dict()["provider_request_send_preparation"]
        item = safe["items"][0]

        self.assertEqual(1, safe["bound_serpapi_request_count"])
        self.assertEqual(1, safe["serpapi_bound_plan_credit_count"])
        self.assertEqual(1, safe["serpapi_plan_credit_cap"])
        self.assertEqual(
            0,
            safe["estimated_bound_first_paid_tier_google_cost_usd_micros"],
        )
        self.assertEqual(
            "serpapi_api_key_query_parameter",
            item["credential"]["slot"],
        )
        self.assertFalse(item["credential"]["value_bound"])
        self.assertFalse(item["credential"]["value_included"])
        self.assertNotIn(PRIVATE_HOTEL_QUERY, json.dumps(safe, ensure_ascii=False))

    def test_preimage_context_response_and_time_drift_fail_closed(self) -> None:
        context, preimages = _prepared_send_preparation()
        changed_preimages = _default_preimages(
            identity=replace(
                preimages[0].target,
                text_query=PRIVATE_QUERY + " changed",
                intent_id="",
            )
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_send_preparation(
                *context,
                preimages=changed_preimages,
                evaluation_at=ASSESS_SEND_PREPARATION_AT,
            )
        changed_context = (
            replace(context[0], constraints=context[0].must_do),
            *context[1:],
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_send_preparation(
                *changed_context,
                preimages=preimages,
                evaluation_at=ASSESS_SEND_PREPARATION_AT,
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_send_preparation(
                *context,
                preimages=preimages,
                evaluation_at=(
                    PREPARE_SEND_PREPARATION_AT - timedelta(microseconds=1)
                ),
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_send_preparation(
                *context,
                preimages=preimages,
                evaluation_at=RECHECK_AT + timedelta(minutes=5),
            )

    def test_safe_views_hide_private_values_identifiers_fingerprints_and_times(
        self,
    ) -> None:
        context, preimages = _prepared_send_preparation()
        preparation = context[-1]
        assessed = assess_guided_provider_request_send_preparation(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_SEND_PREPARATION_AT,
        )
        details = preimages[1].target
        rendered = "\n".join(
            (
                repr(preparation),
                repr(assessed),
                *(repr(item) for item in preparation._bindings),
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
            CAPTURE_SEND_AUTHORIZATION_RESPONSE_AT.isoformat(),
            PREPARE_SEND_PREPARATION_AT.isoformat(),
        ):
            self.assertNotIn(private_value, rendered)
        self.assertEqual(
            {"_bindings", "_prepared_at", "_expires_at", "_context_fingerprint"},
            {item.name for item in fields(type(preparation))},
        )
        handoff = assessed.to_dict()["provider_request_send_preparation"]
        self.assertFalse(handoff["credential_values_included"])
        self.assertFalse(handoff["provider_request_contracts_are_sendable"])

    def test_transport_binding_preparation_and_review_cannot_be_forged(self) -> None:
        context, preimages = _prepared_send_preparation()
        preparation = context[-1]
        binding = preparation._bindings[0]
        assessed = assess_guided_provider_request_send_preparation(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_SEND_PREPARATION_AT,
        )
        with self.assertRaises(ValueError):
            replace(
                binding,
                endpoint_template="https://example.invalid/provider",
            )
        with self.assertRaises(ValueError):
            replace(preparation)
        with self.assertRaises(ValueError):
            replace(assessed, next_action="send_provider_request")
        with self.assertRaises(ValueError):
            GuidedProviderRequestSendPreparation(
                _bindings=preparation._bindings,
                _prepared_at=PREPARE_SEND_PREPARATION_AT,
                _expires_at=RECHECK_AT + timedelta(minutes=5),
                _context_fingerprint="a" * 64,
            )

    def test_surface_exports_transport_metadata_but_has_no_http_or_credential_path(
        self,
    ) -> None:
        for name in (
            "GUIDED_PROVIDER_REQUEST_SEND_PREPARATION_VERSION",
            "GuidedProviderRequestCredentialSlot",
            "GuidedProviderRequestHTTPMethod",
            "GuidedProviderRequestSendPreparation",
            "GuidedProviderRequestSendPreparationReview",
            "GuidedProviderRequestSendPreparationStatus",
            "GuidedProviderRequestTransportBinding",
            "GuidedProviderRequestTransportProfile",
            "GuidedProviderRequestValuePlacement",
            "assess_guided_provider_request_send_preparation",
            "prepare_guided_provider_request_send_preparation",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        self.assertEqual(
            "guided-provider-request-send-preparation/v1",
            GUIDED_PROVIDER_REQUEST_SEND_PREPARATION_VERSION,
        )
        self.assertEqual("GET", GuidedProviderRequestHTTPMethod.GET.value)
        self.assertEqual("POST", GuidedProviderRequestHTTPMethod.POST.value)
        self.assertEqual(
            "header",
            GuidedProviderRequestValuePlacement.HEADER.value,
        )
        self.assertEqual(
            "google_maps_api_key_header",
            GuidedProviderRequestCredentialSlot
            .GOOGLE_MAPS_API_KEY_HEADER.value,
        )
        self.assertEqual(
            "google_routes_compute_routes_v2",
            GuidedProviderRequestTransportProfile
            .GOOGLE_ROUTES_COMPUTE_ROUTES_V2.value,
        )
        for unsupported_name in (
            "bind_guided_provider_credential_value",
            "build_guided_provider_http_request",
            "execute_guided_provider_request",
            "send_guided_provider_request",
            "call_guided_provider",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        tree = ast.parse(inspect.getsource(preparation_module))
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
