"""Phase 5.26 exact private provider request credential-binding review."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace
from datetime import timedelta

import trip_planner
import trip_planner.guided_provider_request_credential_binding_review as review_module
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
    CAPTURE_SEND_AUTHORIZATION_RESPONSE_AT,
)
from tests.test_phase525_guided_provider_request_send_preparation import (
    ASSESS_SEND_PREPARATION_AT,
    PREPARE_SEND_PREPARATION_AT,
    _accepted_response_from_materialization_review,
    _prepared_send_preparation,
)
from trip_planner.guided_evidence_plan import GuidedEvidenceTopic
from trip_planner.guided_provider_request_credential_binding_review import (
    GUIDED_PROVIDER_REQUEST_CREDENTIAL_BINDING_REVIEW_VERSION,
    GuidedProviderRequestCredentialBindingReview,
    GuidedProviderRequestCredentialBindingReviewStatus,
    assess_guided_provider_request_credential_binding_review,
    prepare_guided_provider_request_credential_binding_review,
)
from trip_planner.guided_provider_scope import GuidedProviderCapability
from trip_planner.models import DecisionState, EvidenceState


PREPARE_CREDENTIAL_BINDING_REVIEW_AT = (
    ASSESS_SEND_PREPARATION_AT + timedelta(seconds=1)
)
ASSESS_CREDENTIAL_BINDING_REVIEW_AT = (
    PREPARE_CREDENTIAL_BINDING_REVIEW_AT + timedelta(seconds=1)
)


def _prepared_credential_binding_review(
    *,
    preparation_context: tuple[object, ...] | None = None,
    preimages: tuple[object, ...] | None = None,
):
    if preparation_context is None:
        exact_context, exact_preimages = _prepared_send_preparation()
    else:
        if preimages is None:
            raise ValueError("explicit context requires explicit preimages")
        exact_context = preparation_context
        exact_preimages = preimages
    review = prepare_guided_provider_request_credential_binding_review(
        *exact_context,
        preimages=exact_preimages,
        evaluation_at=PREPARE_CREDENTIAL_BINDING_REVIEW_AT,
    )
    return (*exact_context, review), exact_preimages


class GuidedProviderRequestCredentialBindingReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.default_context, cls.default_preimages = (
            _prepared_credential_binding_review()
        )
        cls.default_review = cls.default_context[-1]
        cls.default_assessed = (
            assess_guided_provider_request_credential_binding_review(
                *cls.default_context,
                preimages=cls.default_preimages,
                evaluation_at=ASSESS_CREDENTIAL_BINDING_REVIEW_AT,
            )
        )

    def test_fresh_send_preparation_yields_only_a_typed_private_review(
        self,
    ) -> None:
        review = self.default_review
        assessed = self.default_assessed
        safe = assessed.to_dict()
        handoff = safe["provider_request_credential_binding_review"]

        self.assertIs(review, assessed)
        self.assertEqual(
            GuidedProviderRequestCredentialBindingReviewStatus.REVIEW_REQUIRED,
            assessed.status,
        )
        self.assertEqual(
            "capture_private_provider_request_credential_binding_response",
            assessed.next_action,
        )
        self.assertTrue(safe["requires_user_response"])
        self.assertTrue(safe["requires_user_review"])
        self.assertTrue(safe["requires_user_decision"])
        self.assertEqual(
            ["accept_credential_binding", "request_smaller", "cancel"],
            safe["response_options"],
        )
        self.assertTrue(handoff["exact_send_preparation_bound"])
        self.assertTrue(handoff["same_exact_target_preimages_revalidated"])
        self.assertTrue(handoff["short_lived_execution_recheck_preserved"])
        self.assertTrue(handoff["must_reassess_before_response_capture"])
        self.assertEqual(2, assessed.binding_count)
        self.assertEqual(2, assessed.contract_count)
        self.assertEqual(2, handoff["reviewed_transport_binding_count"])
        self.assertEqual(
            2,
            handoff["eligible_for_credential_binding_response_count"],
        )
        self.assertTrue(handoff["private_review_payload_available"])
        self.assertTrue(handoff["transport_endpoints_selected"])
        self.assertTrue(handoff["http_methods_selected"])
        self.assertTrue(handoff["credential_slots_bound"])
        self.assertFalse(handoff["credential_binding_response_captured"])
        self.assertFalse(handoff["credential_binding_authority_active"])
        self.assertFalse(handoff["credential_value_access_permitted"])
        self.assertFalse(handoff["credential_values_accessed"])
        self.assertFalse(handoff["credential_values_bound"])
        self.assertFalse(handoff["credential_values_included"])
        self.assertFalse(handoff["environment_read"])
        self.assertFalse(handoff["vault_accessed"])
        self.assertFalse(handoff["network_accessed"])
        self.assertEqual(0, handoff["http_request_count_created_by_review"])
        self.assertFalse(handoff["provider_calls_permitted"])
        self.assertFalse(handoff["send_authority_active"])
        self.assertFalse(handoff["execution_authority_active"])
        self.assertEqual(DecisionState.CANDIDATE, assessed.decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, assessed.evidence_state)
        self.assertIn(
            "provider_request_credential_binding_review",
            safe["needs_verification"],
        )

    def test_explicit_ephemeral_payload_shows_values_but_never_identifiers_or_keys(
        self,
    ) -> None:
        payload = self.default_review.to_ephemeral_private_review_payload()
        rendered = json.dumps(payload, ensure_ascii=False)
        details = self.default_preimages[1].target

        self.assertIn(PRIVATE_QUERY, rendered)
        self.assertNotIn(details.endpoint.provider_place_id, rendered)
        self.assertNotIn(details_fingerprint(details), rendered)
        self.assertEqual(
            "private_ephemeral_direct_human_review_only",
            payload["payload_handling"],
        )
        self.assertEqual(
            ["accept_credential_binding", "request_smaller", "cancel"],
            payload["response_options"],
        )
        self.assertEqual(2, payload["transport_binding_count"])
        self.assertTrue(
            payload[
                "exact_non_identifier_provider_transmitted_values_included"
            ]
        )
        self.assertTrue(payload["credential_slot_names_included"])
        self.assertFalse(payload["provider_identifier_values_exposed"])
        self.assertFalse(payload["credential_values_accessed"])
        self.assertFalse(payload["credential_values_exposed"])
        self.assertFalse(payload["credential_values_bound"])
        self.assertFalse(payload["url_path_identifiers_expanded"])
        self.assertFalse(payload["http_requests_created"])
        profiles = {
            item["transport_profile"]: item
            for item in payload["transport_bindings"]
        }
        identity = profiles["google_places_text_search_v1"]
        fields_by_name = {
            item["contract_field_name"]: item
            for item in identity["provider_transmitted_fields"]
        }
        self.assertEqual(PRIVATE_QUERY, fields_by_name["text_query"]["value"])
        self.assertEqual(
            "textQuery",
            fields_by_name["text_query"]["provider_field_name"],
        )
        details_item = profiles["google_place_details_v1"]
        self.assertFalse(
            details_item["provider_identifier_fields"][0]["value_exposed"]
        )
        self.assertFalse(details_item["url_path_identifier_expanded"])

    def test_all_four_transport_profiles_reach_the_same_typed_review_boundary(
        self,
    ) -> None:
        default_profiles = {
            item["transport_profile"]
            for item in self.default_review.to_dict()[
                "provider_request_credential_binding_review"
            ]["items"]
        }

        route_source, route_preimages = _prepared_single_capability_review(
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
                route_source,
                route_preimages,
            )
        )
        route_preparation, route_preimages = _prepared_send_preparation(
            response_context=route_response,
            preimages=route_preimages,
        )
        route_context, route_preimages = _prepared_credential_binding_review(
            preparation_context=route_preparation,
            preimages=route_preimages,
        )
        route_payload = route_context[-1].to_ephemeral_private_review_payload()
        route_item = route_payload["transport_bindings"][0]

        hotel_source, hotel_preimages = _prepared_single_capability_review(
            GuidedEvidenceTopic.LODGING,
            GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
            _serpapi_item(GuidedEvidenceTopic.LODGING),
            _hotel_request(),
        )
        hotel_response, hotel_preimages = (
            _accepted_response_from_materialization_review(
                hotel_source,
                hotel_preimages,
            )
        )
        hotel_preparation, hotel_preimages = _prepared_send_preparation(
            response_context=hotel_response,
            preimages=hotel_preimages,
        )
        hotel_context, hotel_preimages = _prepared_credential_binding_review(
            preparation_context=hotel_preparation,
            preimages=hotel_preimages,
        )
        hotel_review = hotel_context[-1]
        hotel_payload = hotel_review.to_ephemeral_private_review_payload()
        hotel_item = hotel_payload["transport_bindings"][0]

        self.assertEqual(
            {
                "google_places_text_search_v1",
                "google_place_details_v1",
            },
            default_profiles,
        )
        self.assertEqual(
            "google_routes_compute_routes_v2",
            route_item["transport_profile"],
        )
        route_values = {
            item["contract_field_name"]: item["value"]
            for item in route_item["provider_transmitted_fields"]
        }
        self.assertEqual("driving", route_values["mode"])
        self.assertFalse(
            any(
                item["value_exposed"]
                for item in route_item["provider_identifier_fields"]
            )
        )
        self.assertEqual(
            "serpapi_google_hotels_v1",
            hotel_item["transport_profile"],
        )
        self.assertIn(
            PRIVATE_HOTEL_QUERY,
            json.dumps(hotel_payload, ensure_ascii=False),
        )
        self.assertEqual(
            "serpapi_api_key_query_parameter",
            hotel_item["credential"]["slot"],
        )
        self.assertFalse(hotel_item["credential"]["value_accessed"])
        self.assertFalse(hotel_item["credential"]["value_bound"])
        self.assertEqual(
            ["accept_credential_binding", "request_smaller", "cancel"],
            hotel_payload["response_options"],
        )

    def test_multiple_requests_for_one_scope_item_remain_distinct(self) -> None:
        source_context, preimages = _prepared_two_requests_for_one_scope_item()
        response_context, preimages = (
            _accepted_response_from_materialization_review(
                source_context,
                preimages,
            )
        )
        preparation_context, preimages = _prepared_send_preparation(
            response_context=response_context,
            preimages=preimages,
        )
        context, preimages = _prepared_credential_binding_review(
            preparation_context=preparation_context,
            preimages=preimages,
        )
        review = context[-1]
        safe = review.to_dict()["provider_request_credential_binding_review"]

        self.assertEqual(1, review.accepted_scope_item_count)
        self.assertEqual(2, review.contract_count)
        self.assertEqual(2, review.binding_count)
        self.assertEqual(2, safe["reviewed_transport_binding_count"])
        self.assertEqual(2, safe["accepted_max_request_count"])

    def test_preimage_context_and_time_drift_fail_closed(self) -> None:
        context = self.default_context
        preimages = self.default_preimages
        changed_preimages = _default_preimages(
            identity=replace(
                preimages[0].target,
                text_query=PRIVATE_QUERY + " changed",
                intent_id="",
            )
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_credential_binding_review(
                *context,
                preimages=changed_preimages,
                evaluation_at=ASSESS_CREDENTIAL_BINDING_REVIEW_AT,
            )
        changed_context = (
            replace(context[0], constraints=context[0].must_do),
            *context[1:],
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_credential_binding_review(
                *changed_context,
                preimages=preimages,
                evaluation_at=ASSESS_CREDENTIAL_BINDING_REVIEW_AT,
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_credential_binding_review(
                *context,
                preimages=preimages,
                evaluation_at=(
                    PREPARE_CREDENTIAL_BINDING_REVIEW_AT
                    - timedelta(microseconds=1)
                ),
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_credential_binding_review(
                *context,
                preimages=preimages,
                evaluation_at=RECHECK_AT + timedelta(minutes=5),
            )

    def test_safe_views_hide_private_values_identifiers_fingerprints_and_times(
        self,
    ) -> None:
        review = self.default_review
        safe = review.to_dict()
        details = self.default_preimages[1].target
        rendered = "\n".join(
            (
                repr(review),
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
            PREPARE_REVIEW_AT.isoformat(),
            CAPTURE_MATERIALIZATION_RESPONSE_AT.isoformat(),
            MATERIALIZE_CONTRACTS_AT.isoformat(),
            PREPARE_SEND_REVIEW_AT.isoformat(),
            CAPTURE_SEND_AUTHORIZATION_RESPONSE_AT.isoformat(),
            PREPARE_SEND_PREPARATION_AT.isoformat(),
            PREPARE_CREDENTIAL_BINDING_REVIEW_AT.isoformat(),
        ):
            self.assertNotIn(private_value, rendered)
        self.assertFalse(
            safe["provider_request_credential_binding_review"][
                "exact_private_values_in_safe_output"
            ]
        )
        self.assertEqual(
            {
                "status",
                "next_action",
                "_preparation_review",
                "_prepared_at",
                "_expires_at",
                "_context_fingerprint",
                "contract_version",
            },
            {item.name for item in fields(type(review))},
        )
        self.assertNotIn("preimages", {item.name for item in fields(type(review))})

    def test_review_is_token_gated_and_cannot_be_forged(self) -> None:
        review = self.default_review
        with self.assertRaises(ValueError):
            replace(review, next_action="bind_credential_value")
        with self.assertRaises(ValueError):
            GuidedProviderRequestCredentialBindingReview(
                status=(
                    GuidedProviderRequestCredentialBindingReviewStatus
                    .REVIEW_REQUIRED
                ),
                next_action=(
                    "capture_private_provider_request_credential_binding_response"
                ),
                _preparation_review=review._preparation_review,
                _prepared_at=PREPARE_CREDENTIAL_BINDING_REVIEW_AT,
                _expires_at=RECHECK_AT + timedelta(minutes=5),
                _context_fingerprint="a" * 64,
            )

    def test_review_module_has_no_response_credential_http_or_network_path(
        self,
    ) -> None:
        for name in (
            "GUIDED_PROVIDER_REQUEST_CREDENTIAL_BINDING_REVIEW_VERSION",
            "GuidedProviderRequestCredentialBindingReview",
            "GuidedProviderRequestCredentialBindingReviewStatus",
            "assess_guided_provider_request_credential_binding_review",
            "prepare_guided_provider_request_credential_binding_review",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        self.assertEqual(
            "guided-provider-request-credential-binding-review/v1",
            GUIDED_PROVIDER_REQUEST_CREDENTIAL_BINDING_REVIEW_VERSION,
        )
        self.assertFalse(
            hasattr(
                review_module,
                "capture_guided_provider_request_credential_binding_response",
            )
        )
        for unsupported_name in (
            "bind_guided_provider_credential_value",
            "build_guided_provider_http_request",
            "execute_guided_provider_request",
            "send_guided_provider_request",
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
