"""Phase 5.22 exact private non-sendable provider request contracts."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace
from datetime import timedelta

import trip_planner
from tests.phase5_fixture_cache import reuse_immutable_default_fixture
import trip_planner.guided_provider_request_contract_materialization as contract_module
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
    ASSESS_MATERIALIZATION_RESPONSE_AT,
    CAPTURE_MATERIALIZATION_RESPONSE_AT,
    _captured_materialization_response,
)
from trip_planner.guided_evidence_plan import GuidedEvidenceTopic
from trip_planner.guided_provider_request_contract_materialization import (
    GUIDED_PROVIDER_REQUEST_CONTRACT_MATERIALIZATION_VERSION,
    GuidedProviderRequestContractMaterializationStatus,
    assess_guided_provider_request_contract_materialization,
    materialize_guided_provider_request_contracts,
)
from trip_planner.guided_provider_request_materialization_response import (
    GuidedProviderRequestMaterializationResponseKind,
)
from trip_planner.guided_provider_scope import GuidedProviderCapability
from trip_planner.models import DecisionState, EvidenceState


MATERIALIZE_CONTRACTS_AT = (
    ASSESS_MATERIALIZATION_RESPONSE_AT + timedelta(seconds=15)
)
ASSESS_CONTRACTS_AT = MATERIALIZE_CONTRACTS_AT + timedelta(seconds=15)


@reuse_immutable_default_fixture
def _materialized_contracts(
    *,
    response_context: tuple[object, ...] | None = None,
    preimages: tuple[object, ...] | None = None,
):
    if response_context is None:
        exact_context, exact_preimages = _captured_materialization_response()
    else:
        if preimages is None:
            raise ValueError("explicit context requires explicit preimages")
        exact_context = response_context
        exact_preimages = preimages
    materialization = materialize_guided_provider_request_contracts(
        *exact_context,
        preimages=exact_preimages,
        evaluation_at=MATERIALIZE_CONTRACTS_AT,
    )
    return (*exact_context, materialization), exact_preimages


class GuidedProviderRequestContractMaterializationTests(unittest.TestCase):
    def test_prepare_response_materializes_only_non_sendable_contracts(self) -> None:
        context, preimages = _materialized_contracts()
        materialization = context[-1]
        assessed = assess_guided_provider_request_contract_materialization(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_CONTRACTS_AT,
        )
        safe = assessed.to_dict()
        handoff = safe["provider_request_contract_materialization"]

        self.assertEqual(
            GuidedProviderRequestContractMaterializationStatus
            .READY_FOR_PRIVATE_PROVIDER_REQUEST_SEND_AUTHORIZATION_REVIEW,
            assessed.status,
        )
        self.assertEqual(
            "prepare_private_provider_request_send_authorization_review",
            assessed.next_action,
        )
        self.assertFalse(safe["requires_user_response"])
        self.assertFalse(safe["requires_user_review"])
        self.assertFalse(safe["requires_user_decision"])
        self.assertEqual(2, materialization.contract_count)
        self.assertEqual(2, assessed.contract_count)
        self.assertEqual(2, handoff["request_contract_candidate_count"])
        self.assertEqual(2, handoff["materialized_request_contract_count"])
        self.assertEqual(2, handoff["bound_request_count"])
        self.assertEqual(5, handoff["accepted_max_request_count"])
        self.assertEqual(
            52_000,
            handoff["estimated_bound_first_paid_tier_google_cost_usd_micros"],
        )
        self.assertTrue(handoff["exact_prepare_materialization_response_bound"])
        self.assertTrue(handoff["same_exact_target_preimages_revalidated"])
        self.assertTrue(handoff["short_lived_execution_recheck_preserved"])
        self.assertFalse(handoff["provider_request_contracts_are_executable"])
        self.assertFalse(handoff["provider_request_contracts_are_sendable"])
        self.assertFalse(handoff["transport_endpoints_selected"])
        self.assertFalse(handoff["http_methods_selected"])
        self.assertFalse(handoff["credential_slots_bound"])
        self.assertFalse(handoff["credential_values_included"])
        self.assertEqual(0, handoff["http_request_count_created_by_materialization"])
        self.assertFalse(handoff["send_authorization_active"])
        self.assertFalse(handoff["execution_authority_active"])
        self.assertEqual(0, handoff["provider_call_count_observed"])
        self.assertFalse(handoff["provider_calls_permitted"])
        self.assertFalse(handoff["is_travel_ready"])
        self.assertEqual(DecisionState.CANDIDATE, materialization.decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, materialization.evidence_state)
        self.assertEqual(DecisionState.CANDIDATE, assessed.decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, assessed.evidence_state)
        self.assertIn(
            "provider_request_contract_materialization",
            safe["needs_verification"],
        )
        for item in handoff["items"]:
            self.assertTrue(item["provider_request_contract_materialized"])
            self.assertFalse(item["provider_request_contract_is_executable"])
            self.assertFalse(item["provider_request_contract_is_sendable"])
            self.assertFalse(item["http_request_created"])
            self.assertFalse(item["provider_call_permitted"])
            self.assertEqual("candidate", item["decision_state"])
            self.assertEqual("unverified", item["evidence_state"])

    def test_only_exact_prepare_choice_can_materialize_contracts(self) -> None:
        for kind in (
            GuidedProviderRequestMaterializationResponseKind.REQUEST_SMALLER,
            GuidedProviderRequestMaterializationResponseKind.CANCEL,
        ):
            with self.subTest(kind=kind):
                context, preimages = _captured_materialization_response(kind)
                with self.assertRaises(ValueError):
                    materialize_guided_provider_request_contracts(
                        *context,
                        preimages=preimages,
                        evaluation_at=MATERIALIZE_CONTRACTS_AT,
                    )

    def test_all_four_typed_request_surfaces_retain_exact_private_values(self) -> None:
        default_context, default_preimages = _materialized_contracts()
        default_contracts = {
            item.materialization_kind.value: item
            for item in default_context[-1]._contracts
        }
        identity = default_contracts["google_places_text_search"]
        details = default_contracts["google_place_details"]
        identity_values = dict(identity._provider_transmitted_values)
        identity_local = dict(identity._local_result_binding)
        details_identifiers = dict(details._provider_identifier_values)

        self.assertEqual(PRIVATE_QUERY, identity_values["text_query"])
        self.assertEqual("zh-TW", identity_values["language_code"])
        self.assertEqual(
            "guided-private-location",
            identity_local["stable_local_location_id"],
        )
        self.assertEqual((), identity._provider_identifier_values)
        self.assertEqual(
            default_preimages[1].target.endpoint.provider_place_id,
            details_identifiers["provider_place_id"],
        )
        self.assertEqual(
            default_preimages[1].target.field_mask,
            dict(details._provider_transmitted_values)["field_mask"],
        )

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
        route_response, route_preimages = _captured_materialization_response(
            context=route_review,
            preimages=route_preimages,
        )
        route_context, _ = _materialized_contracts(
            response_context=route_response,
            preimages=route_preimages,
        )
        route = route_context[-1]._contracts[0]
        route_values = dict(route._provider_transmitted_values)
        route_identifiers = dict(route._provider_identifier_values)
        self.assertEqual("google_routes_compute_routes", route.materialization_kind.value)
        self.assertEqual("driving", route_values["mode"])
        self.assertEqual(
            route_preimages[0].target.origin.provider_place_id,
            route_identifiers["origin_provider_place_id"],
        )
        self.assertEqual(
            route_preimages[0].target.destination.provider_place_id,
            route_identifiers["destination_provider_place_id"],
        )

        hotel_review, hotel_preimages = _prepared_single_capability_review(
            GuidedEvidenceTopic.LODGING,
            GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
            _serpapi_item(GuidedEvidenceTopic.LODGING),
            _hotel_request(),
        )
        hotel_response, hotel_preimages = _captured_materialization_response(
            context=hotel_review,
            preimages=hotel_preimages,
        )
        hotel_context, _ = _materialized_contracts(
            response_context=hotel_response,
            preimages=hotel_preimages,
        )
        hotel = hotel_context[-1]._contracts[0]
        hotel_values = dict(hotel._provider_transmitted_values)
        self.assertEqual("serpapi_google_hotels", hotel.materialization_kind.value)
        self.assertEqual(PRIVATE_HOTEL_QUERY, hotel_values["query"])
        self.assertEqual("JPY", hotel_values["currency"])
        self.assertEqual((), hotel._provider_identifier_values)

    def test_safe_views_and_repr_hide_private_values_ids_fingerprints_and_times(
        self,
    ) -> None:
        context, preimages = _materialized_contracts()
        materialization = context[-1]
        assessed = assess_guided_provider_request_contract_materialization(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_CONTRACTS_AT,
        )
        details = preimages[1].target
        rendered = "\n".join(
            (
                repr(materialization),
                repr(assessed),
                *(repr(item) for item in materialization._contracts),
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
            EXPIRES_AT.isoformat(),
        ):
            self.assertNotIn(private_value, rendered)
        handoff = assessed.to_dict()["provider_request_contract_materialization"]
        self.assertFalse(handoff["exact_private_values_in_safe_output"])
        self.assertFalse(handoff["provider_identifier_values_exposed"])
        self.assertFalse(handoff["source_binding_fingerprints_exposed"])
        self.assertFalse(handoff["context_fingerprints_exposed"])
        self.assertEqual(
            {
                "_contracts",
                "_materialized_at",
                "_expires_at",
                "_context_fingerprint",
            },
            {item.name for item in fields(type(materialization))},
        )
        self.assertNotIn(
            "preimages",
            {item.name for item in fields(type(materialization))},
        )
        for contract in materialization._contracts:
            self.assertNotIn(
                "target",
                {item.name for item in fields(type(contract))},
            )

    def test_multiple_requests_for_one_scope_item_remain_distinct(self) -> None:
        review_context, preimages = _prepared_two_requests_for_one_scope_item()
        response_context, preimages = _captured_materialization_response(
            context=review_context,
            preimages=preimages,
        )
        context, preimages = _materialized_contracts(
            response_context=response_context,
            preimages=preimages,
        )
        assessed = assess_guided_provider_request_contract_materialization(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_CONTRACTS_AT,
        )

        self.assertEqual(1, assessed.accepted_scope_item_count)
        self.assertEqual(2, assessed.candidate_count)
        self.assertEqual(2, assessed.contract_count)
        self.assertEqual(2, assessed.bound_request_count)
        self.assertEqual(2, assessed.accepted_max_request_count)
        self.assertEqual(2, len(context[-1]._contracts))
        self.assertEqual(
            2,
            len(
                {
                    item._context_fingerprint
                    for item in context[-1]._contracts
                }
            ),
        )

    def test_serpapi_credit_context_is_aggregated_without_plan_state_leak(self) -> None:
        review_context, preimages = _prepared_single_capability_review(
            GuidedEvidenceTopic.LODGING,
            GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
            _serpapi_item(GuidedEvidenceTopic.LODGING),
            _hotel_request(),
        )
        response_context, preimages = _captured_materialization_response(
            context=review_context,
            preimages=preimages,
        )
        context, preimages = _materialized_contracts(
            response_context=response_context,
            preimages=preimages,
        )
        safe = assess_guided_provider_request_contract_materialization(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_CONTRACTS_AT,
        ).to_dict()["provider_request_contract_materialization"]
        rendered = json.dumps(safe, ensure_ascii=False)

        self.assertEqual(1, safe["bound_request_count"])
        self.assertEqual(1, safe["materialized_request_contract_count"])
        self.assertEqual(1, safe["bound_serpapi_request_count"])
        self.assertEqual(1, safe["serpapi_bound_plan_credit_count"])
        self.assertEqual(1, safe["serpapi_plan_credit_cap"])
        self.assertEqual(
            0,
            safe["estimated_bound_first_paid_tier_google_cost_usd_micros"],
        )
        self.assertFalse(
            safe["all_bound_provider_costs_have_currency_list_rate_estimates"]
        )
        self.assertNotIn("serpapi_remaining_plan_credits", rendered)
        self.assertNotIn("serpapi_automatic_renewal_enabled", rendered)
        self.assertNotIn(PRIVATE_HOTEL_QUERY, rendered)

    def test_materialization_cannot_outlive_the_execution_recheck(self) -> None:
        response_context, preimages = _captured_materialization_response()
        with self.assertRaises(ValueError):
            materialize_guided_provider_request_contracts(
                *response_context,
                preimages=preimages,
                evaluation_at=RECHECK_AT + timedelta(minutes=5),
            )

        context, preimages = _materialized_contracts(
            response_context=response_context,
            preimages=preimages,
        )
        self.assertEqual(
            RECHECK_AT + timedelta(minutes=5),
            context[-1]._expires_at,
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_contract_materialization(
                *context,
                preimages=preimages,
                evaluation_at=RECHECK_AT + timedelta(minutes=5),
            )

    def test_preimage_context_bundle_and_time_drift_fail_closed(self) -> None:
        context, preimages = _materialized_contracts()
        changed_preimages = _default_preimages(
            identity=replace(
                preimages[0].target,
                text_query=PRIVATE_QUERY + " changed",
                intent_id="",
            )
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_contract_materialization(
                *context,
                preimages=changed_preimages,
                evaluation_at=ASSESS_CONTRACTS_AT,
            )

        changed_context = (
            replace(context[0], constraints=context[0].must_do),
            *context[1:],
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_contract_materialization(
                *changed_context,
                preimages=preimages,
                evaluation_at=ASSESS_CONTRACTS_AT,
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_contract_materialization(
                *context,
                preimages=preimages,
                evaluation_at=MATERIALIZE_CONTRACTS_AT - timedelta(microseconds=1),
            )

        response_context = context[:-1]
        with self.assertRaises(ValueError):
            materialize_guided_provider_request_contracts(
                *response_context,
                preimages=changed_preimages,
                evaluation_at=MATERIALIZE_CONTRACTS_AT,
            )

    def test_contract_bundle_and_review_are_token_gated(self) -> None:
        context, preimages = _materialized_contracts()
        materialization = context[-1]
        assessed = assess_guided_provider_request_contract_materialization(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_CONTRACTS_AT,
        )
        with self.assertRaises(ValueError):
            replace(
                materialization._contracts[0],
                topic=GuidedEvidenceTopic.ROUTE,
            )
        with self.assertRaises(ValueError):
            replace(materialization, _expires_at=materialization._expires_at)
        with self.assertRaises(ValueError):
            replace(assessed, next_action="send_provider_request")
        self.assertTrue(
            {
                "_provider_transmitted_values",
                "_provider_identifier_values",
                "_local_result_binding",
                "_source_binding_fingerprint",
                "_context_fingerprint",
            }.issubset(
                {item.name for item in fields(type(materialization._contracts[0]))}
            )
        )
        self.assertFalse(
            {
                "credential",
                "credential_value",
                "http_method",
                "http_request",
                "endpoint_url",
                "preimages",
            }.intersection(
                {item.name for item in fields(type(materialization._contracts[0]))}
            )
        )

    def test_materialization_surface_has_no_direct_send_or_execution_path(
        self,
    ) -> None:
        for name in (
            "GUIDED_PROVIDER_REQUEST_CONTRACT_MATERIALIZATION_VERSION",
            "GuidedProviderRequestContract",
            "GuidedProviderRequestContractMaterialization",
            "GuidedProviderRequestContractMaterializationReview",
            "GuidedProviderRequestContractMaterializationStatus",
            "assess_guided_provider_request_contract_materialization",
            "materialize_guided_provider_request_contracts",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        self.assertEqual(
            "guided-provider-request-contract-materialization/v1",
            GUIDED_PROVIDER_REQUEST_CONTRACT_MATERIALIZATION_VERSION,
        )
        self.assertFalse(
            hasattr(
                contract_module,
                "prepare_guided_provider_request_send_authorization_review",
            )
        )
        for unsupported_name in (
            "authorize_guided_provider_request_send",
            "send_guided_provider_request",
            "build_guided_provider_http_request",
            "execute_guided_provider_request",
            "call_guided_provider",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        tree = ast.parse(inspect.getsource(contract_module))
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
