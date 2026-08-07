"""Phase 5.17 private guided provider-execution authorization review."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace
from datetime import timedelta

import trip_planner
import trip_planner.guided_provider_execution_authorization_review as review_module
from tests.test_phase513_guided_provider_preflight import EXPIRES_AT, _google_item
from tests.test_phase515_guided_provider_execution_targets import (
    TARGETS_ASSESSED_AT,
    _single_capability_context,
)
from tests.test_phase516_guided_provider_execution_target_bindings import (
    ASSESS_AT,
    BIND_AT,
    PRIVATE_HOTEL_QUERY,
    PRIVATE_QUERY,
    _default_preimages,
    _hotel_request,
    _prepared_bindings,
    _route_request,
    _serpapi_item,
    details_fingerprint,
)
from trip_planner.guided_draft import BriefKnownState
from trip_planner.guided_evidence_plan import (
    GuidedEvidenceDisposition,
    GuidedEvidenceTopic,
)
from trip_planner.guided_provider_execution_authorization_review import (
    GuidedProviderExecutionAuthorizationReview,
    GuidedProviderExecutionAuthorizationReviewStatus,
    assess_guided_provider_execution_authorization_review,
    prepare_guided_provider_execution_authorization_review,
)
from trip_planner.guided_provider_execution_target_bindings import (
    GuidedProviderExecutionTargetPreimage,
    bind_guided_provider_execution_targets,
)
from trip_planner.guided_provider_execution_targets import (
    prepare_guided_provider_execution_targets,
)
from trip_planner.guided_provider_scope import GuidedProviderCapability
from trip_planner.models import DecisionState, EvidenceState


REVIEW_AT = ASSESS_AT + timedelta(minutes=1)
REASSESS_AT = REVIEW_AT + timedelta(minutes=1)


def _prepared_review(
    *,
    context: tuple[object, ...] | None = None,
    preimages: tuple[GuidedProviderExecutionTargetPreimage, ...] | None = None,
):
    exact_context, exact_preimages = _prepared_bindings(
        context=context,
        preimages=preimages,
    )
    review = prepare_guided_provider_execution_authorization_review(
        *exact_context,
        preimages=exact_preimages,
        evaluation_at=REVIEW_AT,
    )
    return (*exact_context, review), exact_preimages


def _single_topic_review(
    topic: GuidedEvidenceTopic,
    capability: GuidedProviderCapability,
    preflight_item: object,
    target: object,
):
    accepted = _single_capability_context(topic, capability, preflight_item)
    targets = prepare_guided_provider_execution_targets(
        *accepted,
        evaluation_at=TARGETS_ASSESSED_AT,
    )
    preimages = (
        GuidedProviderExecutionTargetPreimage(
            topic=topic,
            source_line_indexes=(0,),
            target=target,
        ),
    )
    bindings = bind_guided_provider_execution_targets(
        *accepted,
        targets,
        preimages=preimages,
        evaluation_at=BIND_AT,
    )
    context = (*accepted, targets, bindings)
    review = prepare_guided_provider_execution_authorization_review(
        *context,
        preimages=preimages,
        evaluation_at=REVIEW_AT,
    )
    return (*context, review), preimages


class GuidedProviderExecutionAuthorizationReviewTests(unittest.TestCase):
    def test_review_is_exact_bounded_and_requires_a_separate_response(self) -> None:
        context, preimages = _prepared_review()
        review = context[-1]
        safe = review.to_dict()
        authorization = safe["provider_execution_authorization_review"]

        self.assertEqual(
            GuidedProviderExecutionAuthorizationReviewStatus.REVIEW_REQUIRED,
            review.status,
        )
        self.assertEqual(
            "capture_private_provider_execution_authorization_response",
            safe["next_action"],
        )
        self.assertTrue(safe["requires_user_response"])
        self.assertTrue(safe["requires_user_review"])
        self.assertTrue(safe["requires_user_decision"])
        self.assertEqual(["accept", "request_smaller", "cancel"], safe["response_options"])
        self.assertEqual(2, authorization["target_item_count"])
        self.assertEqual(2, authorization["bound_request_count"])
        self.assertEqual(5, authorization["accepted_max_request_count"])
        self.assertEqual(2, authorization["bound_google_request_count"])
        self.assertEqual(0, authorization["bound_serpapi_request_count"])
        self.assertEqual(
            52_000,
            authorization[
                "estimated_bound_first_paid_tier_google_cost_usd_micros"
            ],
        )
        self.assertEqual(
            136_000,
            authorization[
                "accepted_max_first_paid_tier_google_cost_usd_micros"
            ],
        )
        self.assertTrue(authorization["requests_bounded_by_accepted_caps"])
        self.assertTrue(authorization["host_attestation_fresh"])
        self.assertFalse(authorization["authorization_response_captured"])
        self.assertFalse(authorization["provider_scope_authorized"])
        self.assertFalse(authorization["http_requests_created"])
        self.assertEqual(0, authorization["http_request_count_created_by_review"])
        self.assertEqual(0, authorization["provider_call_count_observed"])
        self.assertFalse(authorization["provider_calls_permitted"])
        self.assertEqual("candidate", authorization["decision_state"])
        self.assertEqual("unverified", authorization["evidence_state"])
        self.assertIn(
            "provider_execution_authorization_review",
            safe["needs_verification"],
        )
        self.assertEqual(DecisionState.CANDIDATE, review.decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, review.evidence_state)

        reassessed = assess_guided_provider_execution_authorization_review(
            *context,
            preimages=preimages,
            evaluation_at=REASSESS_AT,
        )
        self.assertIs(review, reassessed)

    def test_safe_view_redacts_values_and_ephemeral_view_is_explicit(self) -> None:
        context, preimages = _prepared_review()
        review = context[-1]
        safe_rendered = "\n".join(
            (
                repr(review),
                json.dumps(review.to_dict(), ensure_ascii=False),
            )
        )
        details = preimages[1].target
        for private_value in (
            PRIVATE_QUERY,
            "guided-private-location",
            details.endpoint.provider_place_id,
            details_fingerprint(details),
            REVIEW_AT.isoformat(),
            EXPIRES_AT.isoformat(),
        ):
            self.assertNotIn(private_value, safe_rendered)

        private = review.to_ephemeral_private_review_payload()
        private_rendered = json.dumps(private, ensure_ascii=False)
        self.assertEqual(
            "private_ephemeral_direct_human_review_only",
            private["payload_handling"],
        )
        self.assertIn(PRIVATE_QUERY, private_rendered)
        self.assertIn("guided-private-location", private_rendered)
        self.assertIn(REVIEW_AT.isoformat(), private_rendered)
        self.assertIn(EXPIRES_AT.isoformat(), private_rendered)
        self.assertNotIn(details.endpoint.provider_place_id, private_rendered)
        self.assertNotIn(details_fingerprint(details), private_rendered)
        self.assertFalse(private["provider_identifier_values_exposed"])
        self.assertFalse(private["target_fingerprints_exposed"])
        self.assertFalse(private["credentials_exposed"])
        self.assertFalse(private["authorization_response_captured"])
        self.assertTrue(private["must_reassess_before_response_capture"])
        self.assertFalse(private["provider_calls_permitted"])

        identity = next(
            item for item in private["targets"] if item["topic"] == "place_identity"
        )
        self.assertEqual(
            PRIVATE_QUERY,
            identity["provider_transmitted_values"]["text_query"],
        )
        self.assertEqual(
            "guided-private-location",
            identity["local_review_context"]["stable_local_location_id"],
        )
        self.assertEqual(
            {"user_stated": 1, "tentative": 0, "ai_candidate": 0},
            identity["source_state_counts"],
        )
        self.assertTrue(identity["all_source_lines_require_verification"])
        self.assertFalse(identity["source_values_are_authoritative"])
        self.assertFalse(
            identity["stable_local_review_identifiers_are_provider_place_ids"]
        )

    def test_route_review_discloses_local_endpoints_but_not_place_ids(self) -> None:
        target = _route_request()
        context, _ = _single_topic_review(
            GuidedEvidenceTopic.ROUTE,
            GuidedProviderCapability.GOOGLE_ROUTES,
            _google_item(
                GuidedEvidenceTopic.ROUTE,
                GuidedProviderCapability.GOOGLE_ROUTES,
                1,
            ),
            target,
        )
        review = context[-1]
        safe = review.to_dict()["provider_execution_authorization_review"]
        private = review.to_ephemeral_private_review_payload()
        rendered = json.dumps(private, ensure_ascii=False)
        item = private["targets"][0]

        self.assertEqual(1, safe["bound_request_count"])
        self.assertEqual(5_000, safe["estimated_bound_first_paid_tier_google_cost_usd_micros"])
        self.assertEqual(2, item["redacted_bound_provider_identifier_count"])
        self.assertEqual(
            "location-origin",
            item["local_review_context"][
                "origin_stable_local_location_id"
            ],
        )
        self.assertEqual(
            "location-destination",
            item["local_review_context"][
                "destination_stable_local_location_id"
            ],
        )
        self.assertEqual("driving", item["provider_transmitted_values"]["mode"])
        self.assertNotIn(target.origin.provider_place_id, rendered)
        self.assertNotIn(target.destination.provider_place_id, rendered)
        self.assertNotIn(target.provider_request.request_fingerprint, rendered)

    def test_hotel_review_counts_plan_credit_and_shows_exact_search(self) -> None:
        target = _hotel_request()
        context, _ = _single_topic_review(
            GuidedEvidenceTopic.LODGING,
            GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
            _serpapi_item(GuidedEvidenceTopic.LODGING),
            target,
        )
        review = context[-1]
        safe = review.to_dict()["provider_execution_authorization_review"]
        private = review.to_ephemeral_private_review_payload()
        item = private["targets"][0]

        self.assertEqual(0, safe["bound_google_request_count"])
        self.assertEqual(1, safe["bound_serpapi_request_count"])
        self.assertEqual(1, safe["serpapi_bound_plan_credit_count"])
        self.assertEqual(1, safe["serpapi_plan_credit_cap"])
        self.assertEqual(0, safe["estimated_bound_first_paid_tier_google_cost_usd_micros"])
        self.assertFalse(
            safe["all_bound_provider_costs_have_currency_list_rate_estimates"]
        )
        self.assertEqual(
            PRIVATE_HOTEL_QUERY,
            item["provider_transmitted_values"]["query"],
        )
        self.assertEqual(
            target.check_in.isoformat(),
            item["provider_transmitted_values"]["check_in"],
        )
        self.assertEqual(
            "serpapi-standard-provider-storage",
            item["retention_profile"],
        )

    def test_review_fails_closed_on_preimage_context_and_time_drift(self) -> None:
        context, preimages = _prepared_review()
        changed_preimages = _default_preimages(
            identity=replace(
                preimages[0].target,
                text_query=PRIVATE_QUERY + " changed",
                intent_id="",
            )
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_authorization_review(
                *context,
                preimages=changed_preimages,
                evaluation_at=REASSESS_AT,
            )
        changed_context = (
            replace(context[0], constraints=context[0].must_do),
            *context[1:],
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_authorization_review(
                *changed_context,
                preimages=preimages,
                evaluation_at=REASSESS_AT,
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_authorization_review(
                *context,
                preimages=preimages,
                evaluation_at=REVIEW_AT - timedelta(microseconds=1),
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_authorization_review(
                *context,
                preimages=preimages,
                evaluation_at=EXPIRES_AT,
            )

    def test_review_retains_only_derived_private_disclosures(self) -> None:
        context, _ = _prepared_review()
        review = context[-1]
        public_field_names = {item.name for item in fields(type(review))}

        self.assertNotIn("preimages", public_field_names)
        self.assertNotIn("bindings", public_field_names)
        self.assertNotIn("targets", public_field_names)
        for item in review._items:
            for _, value in (
                *item._provider_transmitted_values,
                *item._local_review_context,
            ):
                self.assertNotIsInstance(value, GuidedProviderExecutionTargetPreimage)
                self.assertNotIn(type(value).__name__, {
                    "PlaceIdentityIntent",
                    "GooglePlaceDetailsRequest",
                    "GoogleRouteRequest",
                    "LodgingDiscoveryRequest",
                })

    def test_source_state_summary_distinguishes_tentative_and_ai_lines(self) -> None:
        context, _ = _prepared_bindings()
        refinement = context[3]
        evidence_plan = context[7]
        identity = _default_preimages()[0]
        declarations = {
            item.source_line_index: item
            for item in evidence_plan.declarations
        }

        tentative_line = replace(
            refinement.direction.lines[0],
            known_state=BriefKnownState.TENTATIVE,
        )
        tentative_refinement = replace(
            refinement,
            direction=replace(
                refinement.direction,
                lines=(tentative_line, *refinement.direction.lines[1:]),
            ),
        )
        self.assertEqual(
            (0, 1, 0),
            review_module._source_state_counts(
                tentative_refinement,
                declarations,
                identity,
            ),
        )

        ai_declaration = replace(
            evidence_plan.declarations[1],
            disposition=GuidedEvidenceDisposition.REQUIRES_VERIFICATION,
            topics=(GuidedEvidenceTopic.PLACE_IDENTITY,),
        )
        ai_preimage = replace(identity, source_line_indexes=(1,))
        self.assertEqual(
            (0, 0, 1),
            review_module._source_state_counts(
                refinement,
                {**declarations, 1: ai_declaration},
                ai_preimage,
            ),
        )

    def test_ephemeral_projection_is_a_copy_and_does_not_change_review(self) -> None:
        context, _ = _prepared_review()
        review = context[-1]
        first = review.to_ephemeral_private_review_payload()
        first["targets"][0]["provider_transmitted_values"].clear()
        second = review.to_ephemeral_private_review_payload()

        self.assertNotEqual(first, second)
        self.assertTrue(second["targets"][0]["provider_transmitted_values"])
        self.assertFalse(review.to_dict()["provider_execution_authorization_review"]["authorization_response_captured"])

    def test_review_cannot_be_forged_or_replaced(self) -> None:
        context, _ = _prepared_review()
        review = context[-1]
        with self.assertRaises(ValueError):
            replace(review, next_action="execute_provider")
        with self.assertRaises(ValueError):
            replace(review, bound_request_count=1)
        with self.assertRaises(ValueError):
            GuidedProviderExecutionAuthorizationReview(
                status=GuidedProviderExecutionAuthorizationReviewStatus.REVIEW_REQUIRED,
                next_action="capture_private_provider_execution_authorization_response",
                accepted_scope_item_count=1,
                accepted_max_request_count=1,
                target_item_count=1,
                bound_request_count=1,
                max_request_count=1,
                bound_source_line_reference_count=1,
                bound_google_request_count=1,
                bound_serpapi_request_count=0,
                estimated_bound_first_paid_tier_google_cost_usd_micros=5_000,
                accepted_max_first_paid_tier_google_cost_usd_micros=5_000,
                serpapi_bound_plan_credit_count=0,
                serpapi_plan_credit_cap=0,
                host_attestation_fresh=True,
            )

    def test_public_contract_has_review_but_no_response_or_execution_path(self) -> None:
        for name in (
            "GUIDED_PROVIDER_EXECUTION_AUTHORIZATION_REVIEW_VERSION",
            "GuidedProviderExecutionAuthorizationReview",
            "GuidedProviderExecutionAuthorizationReviewStatus",
            "assess_guided_provider_execution_authorization_review",
            "prepare_guided_provider_execution_authorization_review",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        for unsupported_name in (
            "capture_guided_provider_execution_authorization_response",
            "authorize_guided_provider_execution",
            "build_guided_provider_http_request",
            "execute_guided_provider_request",
            "call_guided_provider",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        source = inspect.getsource(review_module)
        tree = ast.parse(source)
        allowed_imports = {"hashlib", "json", "re"}
        allowed_from_imports = {
            (0, "__future__"),
            (0, "dataclasses"),
            (0, "datetime"),
            (0, "enum"),
            (0, "typing"),
            (1, "guided_draft"),
            (1, "guided_evidence_plan"),
            (1, "guided_itinerary"),
            (1, "guided_proposal"),
            (1, "guided_provider_execution_target_bindings"),
            (1, "guided_provider_execution_targets"),
            (1, "guided_provider_preflight"),
            (1, "guided_provider_preflight_response"),
            (1, "guided_provider_scope"),
            (1, "guided_provider_scope_response"),
            (1, "guided_refinement"),
            (1, "lodging_discovery"),
            (1, "models"),
            (1, "place_details"),
            (1, "places_identity"),
            (1, "routes"),
        }
        function_names = {
            node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name, allowed_imports)
            elif isinstance(node, ast.ImportFrom):
                self.assertIn((node.level, node.module), allowed_from_imports)
        self.assertFalse(
            any(
                name.startswith(("capture_", "authorize_", "build_", "execute_"))
                for name in function_names
            )
        )
        for forbidden in (
            "os.environ",
            "getenv",
            "urlopen",
            "import requests",
            "subprocess",
            "Path(",
            "open(",
            "EvidenceStore",
            "build_google_place_details_http_request",
            "build_google_routes_http_request",
            "execute_google_",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
