"""Phase 5.21 exact provider request-materialization response gate."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace
from datetime import timedelta

import trip_planner
from tests.phase5_fixture_cache import reuse_immutable_default_fixture
import trip_planner.guided_provider_request_materialization_response as response_module
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
    ASSESS_REVIEW_AT,
    PREPARE_REVIEW_AT,
    _prepared_materialization_review,
    _prepared_single_capability_review,
    _prepared_two_requests_for_one_scope_item,
)
from trip_planner.guided_evidence_plan import GuidedEvidenceTopic
from trip_planner.guided_provider_request_materialization_review import (
    prepare_guided_provider_request_materialization_review,
)
from trip_planner.guided_provider_request_materialization_response import (
    GUIDED_PROVIDER_REQUEST_MATERIALIZATION_RESPONSE_VERSION,
    GuidedProviderRequestMaterializationResponse,
    GuidedProviderRequestMaterializationResponseKind,
    GuidedProviderRequestMaterializationResponseReview,
    GuidedProviderRequestMaterializationResponseStatus,
    assess_guided_provider_request_materialization_response,
    capture_guided_provider_request_materialization_response,
)
from trip_planner.guided_provider_scope import GuidedProviderCapability


CAPTURE_MATERIALIZATION_RESPONSE_AT = ASSESS_REVIEW_AT + timedelta(seconds=15)
ASSESS_MATERIALIZATION_RESPONSE_AT = (
    CAPTURE_MATERIALIZATION_RESPONSE_AT + timedelta(seconds=15)
)


@reuse_immutable_default_fixture
def _captured_materialization_response(
    kind: GuidedProviderRequestMaterializationResponseKind = (
        GuidedProviderRequestMaterializationResponseKind.PREPARE_MATERIALIZATION
    ),
    *,
    context: tuple[object, ...] | None = None,
    preimages: tuple[object, ...] | None = None,
):
    if context is None:
        exact_context, exact_preimages = _prepared_materialization_review()
    else:
        exact_context = context
        if preimages is None:
            raise ValueError("explicit context requires explicit preimages")
        exact_preimages = preimages
    response = capture_guided_provider_request_materialization_response(
        *exact_context,
        preimages=exact_preimages,
        kind=kind,
        evaluation_at=CAPTURE_MATERIALIZATION_RESPONSE_AT,
    )
    return (*exact_context, response), exact_preimages


class GuidedProviderRequestMaterializationResponseTests(unittest.TestCase):
    def test_prepare_choice_only_reaches_a_separate_contract_gate(self) -> None:
        context, preimages = _captured_materialization_response()
        assessed = assess_guided_provider_request_materialization_response(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_MATERIALIZATION_RESPONSE_AT,
        )
        safe = assessed.to_dict()
        handoff = safe["provider_request_materialization_response"]

        self.assertEqual(
            GuidedProviderRequestMaterializationResponseStatus
            .READY_FOR_PRIVATE_PROVIDER_REQUEST_CONTRACT_MATERIALIZATION,
            assessed.status,
        )
        self.assertEqual(
            "prepare_private_provider_request_contract_materialization",
            assessed.next_action,
        )
        self.assertFalse(safe["requires_user_response"])
        self.assertFalse(safe["requires_user_review"])
        self.assertFalse(safe["requires_user_decision"])
        self.assertEqual("prepare_materialization", handoff["kind"])
        self.assertTrue(handoff["materialization_response_captured"])
        self.assertTrue(handoff["accepted_exact_private_materialization_review"])
        self.assertTrue(
            handoff[
                "accepted_for_request_contract_materialization_preparation"
            ]
        )
        self.assertTrue(
            handoff[
                "may_prepare_private_provider_request_contract_materialization"
            ]
        )
        self.assertEqual(2, handoff["request_contract_candidate_count"])
        self.assertEqual(2, handoff["bound_request_count"])
        self.assertEqual(
            2,
            handoff["eligible_for_request_contract_materialization_count"],
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
        self.assertFalse(handoff["immediate_request_materialization_authority_granted"])
        self.assertFalse(handoff["execution_authority_active"])
        self.assertEqual(0, handoff["request_contract_candidate_count_created_by_response"])
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
            "provider_request_materialization_response",
            safe["needs_verification"],
        )

    def test_all_choices_have_non_executable_branch_semantics(self) -> None:
        cases = (
            (
                GuidedProviderRequestMaterializationResponseKind
                .PREPARE_MATERIALIZATION,
                "ready_for_private_provider_request_contract_materialization",
                "prepare_private_provider_request_contract_materialization",
                (True, False, False),
            ),
            (
                GuidedProviderRequestMaterializationResponseKind.REQUEST_SMALLER,
                "ready_for_private_provider_execution_target_refinement",
                "refine_private_provider_execution_targets",
                (False, True, False),
            ),
            (
                GuidedProviderRequestMaterializationResponseKind.CANCEL,
                "provider_request_materialization_cancelled",
                "continue_private_evidence_review",
                (False, False, True),
            ),
        )
        for kind, status, action, flags in cases:
            with self.subTest(kind=kind):
                context, preimages = _captured_materialization_response(kind)
                safe = assess_guided_provider_request_materialization_response(
                    *context,
                    preimages=preimages,
                    evaluation_at=ASSESS_MATERIALIZATION_RESPONSE_AT,
                ).to_dict()
                handoff = safe["provider_request_materialization_response"]
                self.assertEqual(status, safe["status"])
                self.assertEqual(action, safe["next_action"])
                self.assertEqual(
                    flags[0],
                    handoff["accepted_exact_private_materialization_review"],
                )
                self.assertEqual(
                    flags[1], handoff["requested_smaller_execution_target_set"]
                )
                self.assertEqual(
                    flags[2], handoff["current_request_materialization_cancelled"]
                )
                self.assertEqual(
                    2 if flags[0] else 0,
                    handoff[
                        "eligible_for_request_contract_materialization_count"
                    ],
                )
                self.assertTrue(handoff["request_contract_candidates_unchanged_by_response"])
                self.assertTrue(handoff["request_caps_unchanged_by_response"])
                self.assertTrue(handoff["evidence_requirements_preserved"])
                self.assertFalse(handoff["execution_authority_active"])
                self.assertFalse(handoff["provider_calls_permitted"])

    def test_capture_requires_an_exact_enum_and_current_visible_review(self) -> None:
        context, preimages = _prepared_materialization_review()
        with self.assertRaises(TypeError):
            capture_guided_provider_request_materialization_response(
                *context,
                preimages=preimages,
                kind="prepare_materialization",
                evaluation_at=CAPTURE_MATERIALIZATION_RESPONSE_AT,
            )
        with self.assertRaises(ValueError):
            capture_guided_provider_request_materialization_response(
                *context,
                preimages=preimages,
                kind=(
                    GuidedProviderRequestMaterializationResponseKind
                    .PREPARE_MATERIALIZATION
                ),
                evaluation_at=RECHECK_AT + timedelta(minutes=5),
            )

    def test_response_fails_closed_on_preimage_context_review_and_time_drift(self) -> None:
        context, preimages = _captured_materialization_response()
        changed_preimages = _default_preimages(
            identity=replace(
                preimages[0].target,
                text_query=PRIVATE_QUERY + " changed",
                intent_id="",
            )
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_materialization_response(
                *context,
                preimages=changed_preimages,
                evaluation_at=ASSESS_MATERIALIZATION_RESPONSE_AT,
            )
        changed_context = (
            replace(context[0], constraints=context[0].must_do),
            *context[1:],
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_materialization_response(
                *changed_context,
                preimages=preimages,
                evaluation_at=ASSESS_MATERIALIZATION_RESPONSE_AT,
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_materialization_response(
                *context,
                preimages=preimages,
                evaluation_at=(
                    CAPTURE_MATERIALIZATION_RESPONSE_AT
                    - timedelta(microseconds=1)
                ),
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_materialization_response(
                *context,
                preimages=preimages,
                evaluation_at=RECHECK_AT + timedelta(minutes=5),
            )

        review_context = context[:-2]
        other_review = prepare_guided_provider_request_materialization_review(
            *review_context,
            preimages=preimages,
            evaluation_at=PREPARE_REVIEW_AT + timedelta(seconds=1),
        )
        mismatched = (*context[:-2], other_review, context[-1])
        with self.assertRaises(ValueError):
            assess_guided_provider_request_materialization_response(
                *mismatched,
                preimages=preimages,
                evaluation_at=ASSESS_MATERIALIZATION_RESPONSE_AT,
            )

    def test_safe_views_do_not_expose_private_review_or_target_material(self) -> None:
        context, preimages = _captured_materialization_response()
        response = context[-1]
        assessed = assess_guided_provider_request_materialization_response(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_MATERIALIZATION_RESPONSE_AT,
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
            assessed.to_dict()["provider_request_materialization_response"][
                "private_target_values_exposed"
            ]
        )

    def test_multi_request_count_is_preserved_independently_of_scope_topics(self) -> None:
        review_context, preimages = _prepared_two_requests_for_one_scope_item()
        context, preimages = _captured_materialization_response(
            context=review_context,
            preimages=preimages,
        )
        assessed = assess_guided_provider_request_materialization_response(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_MATERIALIZATION_RESPONSE_AT,
        )

        self.assertEqual(1, assessed.accepted_scope_item_count)
        self.assertEqual(2, assessed.candidate_count)
        self.assertEqual(2, assessed.bound_request_count)
        self.assertEqual(2, assessed.accepted_max_request_count)

    def test_serpapi_credit_context_is_preserved_without_currency_claim(self) -> None:
        review_context, preimages = _prepared_single_capability_review(
            GuidedEvidenceTopic.LODGING,
            GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
            _serpapi_item(GuidedEvidenceTopic.LODGING),
            _hotel_request(),
        )
        context, preimages = _captured_materialization_response(
            context=review_context,
            preimages=preimages,
        )
        safe = assess_guided_provider_request_materialization_response(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_MATERIALIZATION_RESPONSE_AT,
        ).to_dict()["provider_request_materialization_response"]

        self.assertEqual(1, safe["bound_request_count"])
        self.assertEqual(1, safe["request_contract_candidate_count"])
        self.assertEqual(1, safe["bound_serpapi_request_count"])
        self.assertEqual(1, safe["serpapi_bound_plan_credit_count"])
        self.assertEqual(1, safe["serpapi_plan_credit_cap"])
        self.assertEqual(0, safe["estimated_bound_first_paid_tier_google_cost_usd_micros"])
        self.assertFalse(safe["all_bound_provider_costs_have_currency_list_rate_estimates"])
        self.assertNotIn(PRIVATE_HOTEL_QUERY, json.dumps(safe, ensure_ascii=False))

    def test_response_and_assessment_cannot_be_forged(self) -> None:
        context, preimages = _captured_materialization_response()
        response = context[-1]
        assessed = assess_guided_provider_request_materialization_response(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_MATERIALIZATION_RESPONSE_AT,
        )
        with self.assertRaises(ValueError):
            GuidedProviderRequestMaterializationResponse(
                kind=(
                    GuidedProviderRequestMaterializationResponseKind
                    .PREPARE_MATERIALIZATION
                ),
                _captured_at=CAPTURE_MATERIALIZATION_RESPONSE_AT,
                _context_fingerprint="a" * 64,
            )
        with self.assertRaises(ValueError):
            replace(response, kind=GuidedProviderRequestMaterializationResponseKind.CANCEL)
        with self.assertRaises(ValueError):
            replace(assessed, next_action="materialize_provider_request")
        with self.assertRaises(ValueError):
            GuidedProviderRequestMaterializationResponseReview(
                status=(
                    GuidedProviderRequestMaterializationResponseStatus
                    .READY_FOR_PRIVATE_PROVIDER_REQUEST_CONTRACT_MATERIALIZATION
                ),
                response_kind=(
                    GuidedProviderRequestMaterializationResponseKind
                    .PREPARE_MATERIALIZATION
                ),
                next_action="prepare_private_provider_request_contract_materialization",
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

    def test_response_surface_stays_response_only_and_has_no_execution_path(self) -> None:
        for name in (
            "GUIDED_PROVIDER_REQUEST_MATERIALIZATION_RESPONSE_VERSION",
            "GuidedProviderRequestMaterializationResponse",
            "GuidedProviderRequestMaterializationResponseKind",
            "GuidedProviderRequestMaterializationResponseReview",
            "GuidedProviderRequestMaterializationResponseStatus",
            "assess_guided_provider_request_materialization_response",
            "capture_guided_provider_request_materialization_response",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        self.assertEqual(
            "guided-provider-request-materialization-response/v1",
            GUIDED_PROVIDER_REQUEST_MATERIALIZATION_RESPONSE_VERSION,
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
