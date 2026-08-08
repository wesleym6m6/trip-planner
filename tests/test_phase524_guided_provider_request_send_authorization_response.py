"""Phase 5.24 exact provider request-send authorization response gate."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace
from datetime import timedelta

import trip_planner
import trip_planner.guided_provider_request_send_authorization_response as response_module
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
    ASSESS_CONTRACTS_AT,
    MATERIALIZE_CONTRACTS_AT,
)
from tests.test_phase523_guided_provider_request_send_authorization_review import (
    ASSESS_SEND_REVIEW_AT,
    PREPARE_SEND_REVIEW_AT,
    _materialized_from_review,
    _prepared_send_review,
)
from trip_planner.guided_evidence_plan import GuidedEvidenceTopic
from trip_planner.guided_provider_request_send_authorization_review import (
    prepare_guided_provider_request_send_authorization_review,
)
from trip_planner.guided_provider_request_send_authorization_response import (
    GUIDED_PROVIDER_REQUEST_SEND_AUTHORIZATION_RESPONSE_VERSION,
    GuidedProviderRequestSendAuthorizationResponse,
    GuidedProviderRequestSendAuthorizationResponseKind,
    GuidedProviderRequestSendAuthorizationResponseReview,
    GuidedProviderRequestSendAuthorizationResponseStatus,
    assess_guided_provider_request_send_authorization_response,
    capture_guided_provider_request_send_authorization_response,
)
from trip_planner.guided_provider_scope import GuidedProviderCapability


CAPTURE_SEND_AUTHORIZATION_RESPONSE_AT = ASSESS_SEND_REVIEW_AT + timedelta(
    seconds=10
)
ASSESS_SEND_AUTHORIZATION_RESPONSE_AT = (
    CAPTURE_SEND_AUTHORIZATION_RESPONSE_AT + timedelta(seconds=10)
)


def _captured_send_authorization_response(
    kind: GuidedProviderRequestSendAuthorizationResponseKind = (
        GuidedProviderRequestSendAuthorizationResponseKind.ACCEPT_SEND
    ),
    *,
    context: tuple[object, ...] | None = None,
    preimages: tuple[object, ...] | None = None,
):
    if context is None:
        exact_context, exact_preimages = _prepared_send_review()
    else:
        exact_context = context
        if preimages is None:
            raise ValueError("explicit context requires explicit preimages")
        exact_preimages = preimages
    response = capture_guided_provider_request_send_authorization_response(
        *exact_context,
        preimages=exact_preimages,
        kind=kind,
        evaluation_at=CAPTURE_SEND_AUTHORIZATION_RESPONSE_AT,
    )
    return (*exact_context, response), exact_preimages


def _prepared_send_review_from_materialization_review(
    review_context: tuple[object, ...],
    preimages: tuple[object, ...],
):
    materialized_context, exact_preimages = _materialized_from_review(
        review_context,
        preimages,
    )
    return _prepared_send_review(
        materialized_context=materialized_context,
        preimages=exact_preimages,
    )


class GuidedProviderRequestSendAuthorizationResponseTests(unittest.TestCase):
    def test_accept_choice_only_reaches_a_separate_send_preparation_gate(
        self,
    ) -> None:
        context, preimages = _captured_send_authorization_response()
        assessed = assess_guided_provider_request_send_authorization_response(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_SEND_AUTHORIZATION_RESPONSE_AT,
        )
        safe = assessed.to_dict()
        handoff = safe["provider_request_send_authorization_response"]

        self.assertEqual(
            GuidedProviderRequestSendAuthorizationResponseStatus
            .READY_FOR_PRIVATE_PROVIDER_REQUEST_SEND_PREPARATION,
            assessed.status,
        )
        self.assertEqual(
            "prepare_private_provider_request_send_preparation",
            assessed.next_action,
        )
        self.assertFalse(safe["requires_user_response"])
        self.assertFalse(safe["requires_user_review"])
        self.assertFalse(safe["requires_user_decision"])
        self.assertEqual("accept_send", handoff["kind"])
        self.assertTrue(handoff["send_authorization_response_captured"])
        self.assertTrue(handoff["accepted_exact_private_send_authorization_review"])
        self.assertTrue(handoff["accepted_for_separate_send_preparation"])
        self.assertTrue(
            handoff["may_prepare_private_provider_request_send_preparation"]
        )
        self.assertEqual(2, handoff["materialized_request_contract_count"])
        self.assertEqual(2, handoff["bound_request_count"])
        self.assertEqual(
            2,
            handoff["eligible_for_separate_send_preparation_count"],
        )
        self.assertEqual(5, handoff["accepted_max_request_count"])
        self.assertEqual(2, handoff["bound_google_request_count"])
        self.assertEqual(0, handoff["bound_serpapi_request_count"])
        self.assertEqual(
            52_000,
            handoff["estimated_bound_first_paid_tier_google_cost_usd_micros"],
        )
        self.assertTrue(handoff["host_attestation_fresh"])
        self.assertTrue(handoff["pricing_policy_retention_recheck_preserved"])
        self.assertTrue(handoff["billing_region_recheck_preserved"])
        self.assertTrue(handoff["credential_availability_recheck_preserved"])
        self.assertTrue(handoff["serpapi_plan_state_recheck_preserved"])
        self.assertFalse(handoff["provider_request_contracts_are_sendable"])
        self.assertFalse(handoff["immediate_send_authority_granted"])
        self.assertFalse(handoff["send_authority_active"])
        self.assertFalse(handoff["execution_authority_active"])
        self.assertEqual(
            0,
            handoff[
                "executable_provider_request_contract_count_created_by_response"
            ],
        )
        self.assertEqual(0, handoff["http_request_count_created_by_response"])
        self.assertEqual(0, handoff["provider_call_count_observed"])
        self.assertFalse(handoff["provider_calls_permitted"])
        self.assertEqual("candidate", handoff["decision_state"])
        self.assertEqual("unverified", handoff["evidence_state"])
        self.assertIn(
            "provider_request_send_authorization_response",
            safe["needs_verification"],
        )

    def test_all_choices_have_non_executable_branch_semantics(self) -> None:
        cases = (
            (
                GuidedProviderRequestSendAuthorizationResponseKind
                .ACCEPT_SEND,
                "ready_for_private_provider_request_send_preparation",
                "prepare_private_provider_request_send_preparation",
                (True, False, False),
            ),
            (
                GuidedProviderRequestSendAuthorizationResponseKind.REQUEST_SMALLER,
                "ready_for_private_provider_execution_target_refinement",
                "refine_private_provider_execution_targets",
                (False, True, False),
            ),
            (
                GuidedProviderRequestSendAuthorizationResponseKind.CANCEL,
                "provider_request_send_cancelled",
                "continue_private_evidence_review",
                (False, False, True),
            ),
        )
        for kind, status, action, flags in cases:
            with self.subTest(kind=kind):
                context, preimages = _captured_send_authorization_response(kind)
                safe = assess_guided_provider_request_send_authorization_response(
                    *context,
                    preimages=preimages,
                    evaluation_at=ASSESS_SEND_AUTHORIZATION_RESPONSE_AT,
                ).to_dict()
                handoff = safe["provider_request_send_authorization_response"]
                self.assertEqual(status, safe["status"])
                self.assertEqual(action, safe["next_action"])
                self.assertEqual(
                    flags[0],
                    handoff[
                        "accepted_exact_private_send_authorization_review"
                    ],
                )
                self.assertEqual(
                    flags[1], handoff["requested_smaller_execution_target_set"]
                )
                self.assertEqual(
                    flags[2], handoff["current_request_send_cancelled"]
                )
                self.assertEqual(
                    2 if flags[0] else 0,
                    handoff[
                        "eligible_for_separate_send_preparation_count"
                    ],
                )
                self.assertTrue(
                    handoff[
                        "materialized_request_contracts_unchanged_by_response"
                    ]
                )
                self.assertTrue(handoff["request_caps_unchanged_by_response"])
                self.assertTrue(handoff["evidence_requirements_preserved"])
                self.assertFalse(handoff["send_authority_active"])
                self.assertFalse(handoff["execution_authority_active"])
                self.assertFalse(handoff["provider_calls_permitted"])

    def test_capture_requires_an_exact_enum_and_current_visible_review(self) -> None:
        context, preimages = _prepared_send_review()
        with self.assertRaises(TypeError):
            capture_guided_provider_request_send_authorization_response(
                *context,
                preimages=preimages,
                kind="accept_send",
                evaluation_at=CAPTURE_SEND_AUTHORIZATION_RESPONSE_AT,
            )
        with self.assertRaises(ValueError):
            capture_guided_provider_request_send_authorization_response(
                *context,
                preimages=preimages,
                kind=(
                    GuidedProviderRequestSendAuthorizationResponseKind
                    .ACCEPT_SEND
                ),
                evaluation_at=RECHECK_AT + timedelta(minutes=5),
            )

    def test_response_fails_closed_on_preimage_context_review_and_time_drift(self) -> None:
        context, preimages = _captured_send_authorization_response()
        changed_preimages = _default_preimages(
            identity=replace(
                preimages[0].target,
                text_query=PRIVATE_QUERY + " changed",
                intent_id="",
            )
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_send_authorization_response(
                *context,
                preimages=changed_preimages,
                evaluation_at=ASSESS_SEND_AUTHORIZATION_RESPONSE_AT,
            )
        changed_context = (
            replace(context[0], constraints=context[0].must_do),
            *context[1:],
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_send_authorization_response(
                *changed_context,
                preimages=preimages,
                evaluation_at=ASSESS_SEND_AUTHORIZATION_RESPONSE_AT,
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_send_authorization_response(
                *context,
                preimages=preimages,
                evaluation_at=(
                    CAPTURE_SEND_AUTHORIZATION_RESPONSE_AT
                    - timedelta(microseconds=1)
                ),
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_send_authorization_response(
                *context,
                preimages=preimages,
                evaluation_at=RECHECK_AT + timedelta(minutes=5),
            )

        review_context = context[:-2]
        other_review = prepare_guided_provider_request_send_authorization_review(
            *review_context,
            preimages=preimages,
            evaluation_at=PREPARE_SEND_REVIEW_AT + timedelta(seconds=1),
        )
        mismatched = (*context[:-2], other_review, context[-1])
        with self.assertRaises(ValueError):
            assess_guided_provider_request_send_authorization_response(
                *mismatched,
                preimages=preimages,
                evaluation_at=ASSESS_SEND_AUTHORIZATION_RESPONSE_AT,
            )

    def test_safe_views_do_not_expose_private_review_or_target_material(self) -> None:
        context, preimages = _captured_send_authorization_response()
        response = context[-1]
        assessed = assess_guided_provider_request_send_authorization_response(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_SEND_AUTHORIZATION_RESPONSE_AT,
        )
        details = preimages[1].target
        rendered = "\n".join(
            (
                repr(response),
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
            ASSESS_CONTRACTS_AT.isoformat(),
            PREPARE_SEND_REVIEW_AT.isoformat(),
            CAPTURE_SEND_AUTHORIZATION_RESPONSE_AT.isoformat(),
            EXPIRES_AT.isoformat(),
        ):
            self.assertNotIn(private_value, rendered)
        self.assertEqual(
            {"kind", "_captured_at", "_context_fingerprint"},
            {item.name for item in fields(type(response))},
        )
        self.assertNotIn("free_text", {item.name for item in fields(type(response))})
        self.assertNotIn("preimages", {item.name for item in fields(type(response))})
        self.assertFalse(
            assessed.to_dict()["provider_request_send_authorization_response"][
                "private_request_contract_values_exposed"
            ]
        )

    def test_multi_request_count_is_preserved_independently_of_scope_topics(self) -> None:
        review_context, preimages = _prepared_two_requests_for_one_scope_item()
        send_review_context, preimages = (
            _prepared_send_review_from_materialization_review(
                review_context,
                preimages,
            )
        )
        context, preimages = _captured_send_authorization_response(
            context=send_review_context,
            preimages=preimages,
        )
        assessed = assess_guided_provider_request_send_authorization_response(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_SEND_AUTHORIZATION_RESPONSE_AT,
        )

        self.assertEqual(1, assessed.accepted_scope_item_count)
        self.assertEqual(2, assessed.contract_count)
        self.assertEqual(2, assessed.bound_request_count)
        self.assertEqual(2, assessed.accepted_max_request_count)

    def test_serpapi_credit_context_is_preserved_without_currency_claim(self) -> None:
        review_context, preimages = _prepared_single_capability_review(
            GuidedEvidenceTopic.LODGING,
            GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
            _serpapi_item(GuidedEvidenceTopic.LODGING),
            _hotel_request(),
        )
        send_review_context, preimages = (
            _prepared_send_review_from_materialization_review(
                review_context,
                preimages,
            )
        )
        context, preimages = _captured_send_authorization_response(
            context=send_review_context,
            preimages=preimages,
        )
        safe = assess_guided_provider_request_send_authorization_response(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_SEND_AUTHORIZATION_RESPONSE_AT,
        ).to_dict()["provider_request_send_authorization_response"]

        self.assertEqual(1, safe["bound_request_count"])
        self.assertEqual(1, safe["materialized_request_contract_count"])
        self.assertEqual(1, safe["bound_serpapi_request_count"])
        self.assertEqual(1, safe["serpapi_bound_plan_credit_count"])
        self.assertEqual(1, safe["serpapi_plan_credit_cap"])
        self.assertEqual(0, safe["estimated_bound_first_paid_tier_google_cost_usd_micros"])
        self.assertFalse(safe["all_bound_provider_costs_have_currency_list_rate_estimates"])
        self.assertNotIn(PRIVATE_HOTEL_QUERY, json.dumps(safe, ensure_ascii=False))

    def test_response_and_assessment_cannot_be_forged(self) -> None:
        context, preimages = _captured_send_authorization_response()
        response = context[-1]
        assessed = assess_guided_provider_request_send_authorization_response(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_SEND_AUTHORIZATION_RESPONSE_AT,
        )
        with self.assertRaises(ValueError):
            GuidedProviderRequestSendAuthorizationResponse(
                kind=(
                    GuidedProviderRequestSendAuthorizationResponseKind
                    .ACCEPT_SEND
                ),
                _captured_at=CAPTURE_SEND_AUTHORIZATION_RESPONSE_AT,
                _context_fingerprint="a" * 64,
            )
        with self.assertRaises(ValueError):
            replace(response, kind=GuidedProviderRequestSendAuthorizationResponseKind.CANCEL)
        with self.assertRaises(ValueError):
            replace(assessed, next_action="send_provider_request")
        with self.assertRaises(ValueError):
            GuidedProviderRequestSendAuthorizationResponseReview(
                status=(
                    GuidedProviderRequestSendAuthorizationResponseStatus
                    .READY_FOR_PRIVATE_PROVIDER_REQUEST_SEND_PREPARATION
                ),
                response_kind=(
                    GuidedProviderRequestSendAuthorizationResponseKind
                    .ACCEPT_SEND
                ),
                next_action="prepare_private_provider_request_send_preparation",
                accepted_scope_item_count=1,
                accepted_max_request_count=1,
                contract_count=1,
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

    def test_response_surface_stays_response_only_and_has_no_execution_path(self) -> None:
        for name in (
            "GUIDED_PROVIDER_REQUEST_SEND_AUTHORIZATION_RESPONSE_VERSION",
            "GuidedProviderRequestSendAuthorizationResponse",
            "GuidedProviderRequestSendAuthorizationResponseKind",
            "GuidedProviderRequestSendAuthorizationResponseReview",
            "GuidedProviderRequestSendAuthorizationResponseStatus",
            "assess_guided_provider_request_send_authorization_response",
            "capture_guided_provider_request_send_authorization_response",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        self.assertEqual(
            "guided-provider-request-send-authorization-response/v1",
            GUIDED_PROVIDER_REQUEST_SEND_AUTHORIZATION_RESPONSE_VERSION,
        )
        self.assertFalse(
            hasattr(
                response_module,
                "materialize_guided_provider_request_contracts",
            )
        )
        for unsupported_name in (
            "materialize_guided_provider_request",
            "authorize_guided_provider_execution",
            "build_guided_provider_http_request",
            "execute_guided_provider_request",
            "call_guided_provider",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        tree = ast.parse(inspect.getsource(response_module))
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
