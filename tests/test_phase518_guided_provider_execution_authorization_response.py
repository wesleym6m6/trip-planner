"""Phase 5.18 exact provider-execution authorization response gate."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace
from datetime import timedelta

import trip_planner
import trip_planner.guided_provider_execution_authorization_response as response_module
from tests.test_phase513_guided_provider_preflight import EXPIRES_AT
from tests.test_phase516_guided_provider_execution_target_bindings import (
    BIND_AT,
    PRIVATE_QUERY,
    _default_preimages,
    _place_details_request,
    details_fingerprint,
)
from tests.test_phase517_guided_provider_execution_authorization_review import (
    REASSESS_AT,
    REVIEW_AT,
    _prepared_review,
    _single_topic_review,
)
from tests.test_phase516_guided_provider_execution_target_bindings import (
    PRIVATE_HOTEL_QUERY,
    _hotel_request,
    _serpapi_item,
)
from trip_planner.guided_evidence_plan import GuidedEvidenceTopic
from trip_planner.guided_provider_execution_authorization_response import (
    GuidedProviderExecutionAuthorizationResponse,
    GuidedProviderExecutionAuthorizationResponseKind,
    GuidedProviderExecutionAuthorizationResponseReview,
    GuidedProviderExecutionAuthorizationResponseStatus,
    assess_guided_provider_execution_authorization_response,
    capture_guided_provider_execution_authorization_response,
)
from trip_planner.guided_provider_execution_authorization_review import (
    prepare_guided_provider_execution_authorization_review,
)
from trip_planner.guided_provider_scope import GuidedProviderCapability


CAPTURE_AT = REASSESS_AT + timedelta(minutes=1)
ASSESS_RESPONSE_AT = CAPTURE_AT + timedelta(minutes=1)


def _captured_response(
    kind: GuidedProviderExecutionAuthorizationResponseKind = (
        GuidedProviderExecutionAuthorizationResponseKind.ACCEPT
    ),
    *,
    context: tuple[object, ...] | None = None,
    preimages: tuple[object, ...] | None = None,
):
    exact_context, exact_preimages = _prepared_review(
        context=context,
        preimages=preimages,
    )
    response = capture_guided_provider_execution_authorization_response(
        *exact_context,
        preimages=exact_preimages,
        kind=kind,
        evaluation_at=CAPTURE_AT,
    )
    return (*exact_context, response), exact_preimages


class GuidedProviderExecutionAuthorizationResponseTests(unittest.TestCase):
    def test_accept_only_reaches_a_separate_execution_time_recheck(self) -> None:
        context, preimages = _captured_response()
        response = context[-1]
        assessed = assess_guided_provider_execution_authorization_response(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_RESPONSE_AT,
        )
        safe = assessed.to_dict()
        handoff = safe["provider_execution_authorization_response"]

        self.assertEqual(
            GuidedProviderExecutionAuthorizationResponseStatus
            .READY_FOR_PRIVATE_PROVIDER_EXECUTION_TIME_RECHECK,
            assessed.status,
        )
        self.assertEqual(
            "prepare_private_provider_execution_time_recheck",
            safe["next_action"],
        )
        self.assertFalse(safe["requires_user_response"])
        self.assertFalse(safe["requires_user_review"])
        self.assertFalse(safe["requires_user_decision"])
        self.assertEqual("accept", handoff["kind"])
        self.assertTrue(handoff["authorization_response_captured"])
        self.assertTrue(handoff["accepted_exact_private_review"])
        self.assertTrue(
            handoff["accepted_for_execution_time_recheck_preparation"]
        )
        self.assertTrue(
            handoff["may_prepare_private_provider_execution_time_recheck"]
        )
        self.assertEqual(
            handoff["bound_request_count"],
            handoff["eligible_for_execution_time_recheck_request_count"],
        )
        self.assertEqual(2, handoff["bound_request_count"])
        self.assertEqual(5, handoff["accepted_max_request_count"])
        self.assertEqual(2, handoff["bound_google_request_count"])
        self.assertEqual(0, handoff["bound_serpapi_request_count"])
        self.assertEqual(
            52_000,
            handoff[
                "estimated_bound_first_paid_tier_google_cost_usd_micros"
            ],
        )
        self.assertEqual(
            136_000,
            handoff[
                "accepted_max_first_paid_tier_google_cost_usd_micros"
            ],
        )
        self.assertEqual(
            {
                "google_places_current_hours": 1,
                "google_places_identity_lookup": 1,
                "google_routes": 0,
                "serpapi_google_hotels": 0,
            },
            handoff["bound_request_capability_counts"],
        )
        self.assertEqual(
            {"user_stated": 2, "tentative": 0, "ai_candidate": 0},
            handoff["source_state_counts"],
        )
        self.assertTrue(handoff["all_source_lines_require_verification"])
        self.assertFalse(handoff["source_values_are_authoritative"])
        self.assertTrue(
            handoff[
                "execution_time_pricing_policy_retention_recheck_required"
            ]
        )
        self.assertTrue(handoff["credential_availability_recheck_required"])
        self.assertTrue(handoff["exact_target_preimages_required_for_recheck"])
        self.assertFalse(
            handoff["immediate_provider_execution_authority_granted"]
        )
        self.assertFalse(handoff["execution_authority_active"])
        self.assertFalse(handoff["partial_execution_authorization_permitted"])
        self.assertEqual(
            0,
            handoff["provider_request_contract_count_created_by_response"],
        )
        self.assertEqual(0, handoff["http_request_count_created_by_response"])
        self.assertEqual(0, handoff["provider_call_count_observed"])
        self.assertFalse(handoff["provider_calls_permitted"])
        self.assertEqual("candidate", handoff["decision_state"])
        self.assertEqual("unverified", handoff["evidence_state"])
        self.assertIn(
            "provider_execution_authorization_response",
            safe["needs_verification"],
        )
        self.assertEqual(
            GuidedProviderExecutionAuthorizationResponseKind.ACCEPT,
            response.kind,
        )

    def test_all_exact_choices_have_non_executable_branch_semantics(self) -> None:
        cases = (
            (
                GuidedProviderExecutionAuthorizationResponseKind.ACCEPT,
                "ready_for_private_provider_execution_time_recheck",
                "prepare_private_provider_execution_time_recheck",
                (True, False, False),
            ),
            (
                GuidedProviderExecutionAuthorizationResponseKind.REQUEST_SMALLER,
                "ready_for_private_provider_execution_target_refinement",
                "refine_private_provider_execution_targets",
                (False, True, False),
            ),
            (
                GuidedProviderExecutionAuthorizationResponseKind.CANCEL,
                "provider_execution_cancelled",
                "continue_private_evidence_review",
                (False, False, True),
            ),
        )
        for kind, status, action, flags in cases:
            with self.subTest(kind=kind):
                context, preimages = _captured_response(kind)
                safe = assess_guided_provider_execution_authorization_response(
                    *context,
                    preimages=preimages,
                    evaluation_at=ASSESS_RESPONSE_AT,
                ).to_dict()
                handoff = safe["provider_execution_authorization_response"]
                self.assertEqual(status, safe["status"])
                self.assertEqual(action, safe["next_action"])
                self.assertEqual(flags[0], handoff["accepted_exact_private_review"])
                self.assertEqual(
                    flags[1],
                    handoff["requested_smaller_execution_target_set"],
                )
                self.assertEqual(
                    flags[2],
                    handoff["current_external_execution_cancelled"],
                )
                self.assertEqual(
                    2 if flags[0] else 0,
                    handoff[
                        "eligible_for_execution_time_recheck_request_count"
                    ],
                )
                self.assertEqual(
                    flags[1],
                    handoff[
                        "new_exact_review_required_after_target_refinement"
                    ],
                )
                self.assertEqual(
                    flags[2],
                    handoff[
                        "new_exact_review_required_before_cancelled_path_reopens"
                    ],
                )
                self.assertTrue(handoff["execution_targets_unchanged_by_response"])
                self.assertTrue(handoff["request_caps_unchanged_by_response"])
                self.assertTrue(handoff["evidence_requirements_preserved"])
                self.assertFalse(handoff["response_free_text_retained"])
                self.assertFalse(handoff["execution_authority_active"])
                self.assertFalse(handoff["provider_calls_permitted"])

    def test_capture_requires_an_exact_enum_and_current_visible_review(self) -> None:
        context, preimages = _prepared_review()
        with self.assertRaises(TypeError):
            capture_guided_provider_execution_authorization_response(
                *context,
                preimages=preimages,
                kind="accept",
                evaluation_at=CAPTURE_AT,
            )
        with self.assertRaises(ValueError):
            capture_guided_provider_execution_authorization_response(
                *context,
                preimages=preimages,
                kind=GuidedProviderExecutionAuthorizationResponseKind.ACCEPT,
                evaluation_at=EXPIRES_AT,
            )

    def test_response_fails_closed_on_preimage_context_review_and_time_drift(self) -> None:
        context, preimages = _captured_response()
        changed_preimages = _default_preimages(
            identity=replace(
                preimages[0].target,
                text_query=PRIVATE_QUERY + " changed",
                intent_id="",
            )
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_authorization_response(
                *context,
                preimages=changed_preimages,
                evaluation_at=ASSESS_RESPONSE_AT,
            )
        changed_context = (
            replace(context[0], constraints=context[0].must_do),
            *context[1:],
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_authorization_response(
                *changed_context,
                preimages=preimages,
                evaluation_at=ASSESS_RESPONSE_AT,
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_authorization_response(
                *context,
                preimages=preimages,
                evaluation_at=CAPTURE_AT - timedelta(microseconds=1),
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_authorization_response(
                *context,
                preimages=preimages,
                evaluation_at=EXPIRES_AT,
            )

        earlier_context = context[:-2]
        other_review = prepare_guided_provider_execution_authorization_review(
            *earlier_context,
            preimages=preimages,
            evaluation_at=REVIEW_AT + timedelta(seconds=1),
        )
        mismatched_review_context = (
            *context[:-2],
            other_review,
            context[-1],
        )
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_authorization_response(
                *mismatched_review_context,
                preimages=preimages,
                evaluation_at=ASSESS_RESPONSE_AT,
            )

    def test_trusted_endpoint_freshness_is_rechecked_after_capture(self) -> None:
        details = _place_details_request(
            now=BIND_AT - timedelta(hours=23, minutes=55)
        )
        preimages = _default_preimages(details=details)
        context, preimages = _captured_response(preimages=preimages)
        self.assertLess(details.endpoint.valid_until, EXPIRES_AT)
        with self.assertRaises(ValueError):
            assess_guided_provider_execution_authorization_response(
                *context,
                preimages=preimages,
                evaluation_at=details.endpoint.valid_until,
            )

    def test_safe_views_do_not_expose_private_review_or_target_material(self) -> None:
        context, preimages = _captured_response()
        response = context[-1]
        assessed = assess_guided_provider_execution_authorization_response(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_RESPONSE_AT,
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
            EXPIRES_AT.isoformat(),
        ):
            self.assertNotIn(private_value, rendered)
        self.assertEqual(
            {"kind", "_captured_at", "_context_fingerprint"},
            {item.name for item in fields(type(response))},
        )
        self.assertNotIn("free_text", {item.name for item in fields(type(response))})
        self.assertNotIn("target_digest", {item.name for item in fields(type(response))})
        self.assertNotIn("preimages", {item.name for item in fields(type(response))})
        self.assertFalse(
            assessed.to_dict()["provider_execution_authorization_response"][
                "private_target_values_exposed"
            ]
        )

    def test_serpapi_credit_context_is_preserved_without_currency_claim(self) -> None:
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
        safe = assess_guided_provider_execution_authorization_response(
            *context,
            response,
            preimages=preimages,
            evaluation_at=ASSESS_RESPONSE_AT,
        ).to_dict()["provider_execution_authorization_response"]

        self.assertEqual(1, safe["bound_request_count"])
        self.assertEqual(1, safe["bound_serpapi_request_count"])
        self.assertEqual(1, safe["serpapi_bound_plan_credit_count"])
        self.assertEqual(1, safe["serpapi_plan_credit_cap"])
        self.assertEqual(
            1,
            safe["bound_request_capability_counts"]["serpapi_google_hotels"],
        )
        self.assertEqual(
            0,
            safe["estimated_bound_first_paid_tier_google_cost_usd_micros"],
        )
        self.assertFalse(
            safe["all_bound_provider_costs_have_currency_list_rate_estimates"]
        )
        self.assertNotIn(PRIVATE_HOTEL_QUERY, json.dumps(safe, ensure_ascii=False))

    def test_response_and_assessment_cannot_be_forged(self) -> None:
        context, preimages = _captured_response()
        response = context[-1]
        assessed = assess_guided_provider_execution_authorization_response(
            *context,
            preimages=preimages,
            evaluation_at=ASSESS_RESPONSE_AT,
        )
        with self.assertRaises(ValueError):
            GuidedProviderExecutionAuthorizationResponse(
                kind=GuidedProviderExecutionAuthorizationResponseKind.ACCEPT,
                _captured_at=CAPTURE_AT,
                _context_fingerprint="a" * 64,
            )
        with self.assertRaises(ValueError):
            replace(
                response,
                kind=(
                    GuidedProviderExecutionAuthorizationResponseKind.CANCEL
                ),
            )
        with self.assertRaises(ValueError):
            replace(assessed, next_action="execute_provider")
        with self.assertRaises(ValueError):
            GuidedProviderExecutionAuthorizationResponseReview(
                status=(
                    GuidedProviderExecutionAuthorizationResponseStatus
                    .READY_FOR_PRIVATE_PROVIDER_EXECUTION_TIME_RECHECK
                ),
                response_kind=(
                    GuidedProviderExecutionAuthorizationResponseKind.ACCEPT
                ),
                next_action="prepare_private_provider_execution_time_recheck",
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
                host_attestation_fresh=True,
            )

    def test_public_contract_has_response_but_no_execution_time_path(self) -> None:
        for name in (
            "GUIDED_PROVIDER_EXECUTION_AUTHORIZATION_RESPONSE_VERSION",
            "GuidedProviderExecutionAuthorizationResponse",
            "GuidedProviderExecutionAuthorizationResponseKind",
            "GuidedProviderExecutionAuthorizationResponseReview",
            "GuidedProviderExecutionAuthorizationResponseStatus",
            "assess_guided_provider_execution_authorization_response",
            "capture_guided_provider_execution_authorization_response",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        for unsupported_name in (
            "prepare_guided_provider_execution_time_recheck",
            "materialize_guided_provider_request",
            "authorize_guided_provider_execution",
            "build_guided_provider_http_request",
            "execute_guided_provider_request",
            "call_guided_provider",
        ):
            self.assertFalse(hasattr(trip_planner, unsupported_name))

        source = inspect.getsource(response_module)
        tree = ast.parse(source)
        allowed_imports = {"hashlib", "json", "re"}
        allowed_from_imports = {
            (0, "__future__"),
            (0, "collections"),
            (0, "dataclasses"),
            (0, "datetime"),
            (0, "enum"),
            (0, "typing"),
            (1, "guided_draft"),
            (1, "guided_evidence_plan"),
            (1, "guided_itinerary"),
            (1, "guided_proposal"),
            (1, "guided_provider_execution_authorization_review"),
            (1, "guided_provider_execution_target_bindings"),
            (1, "guided_provider_execution_targets"),
            (1, "guided_provider_preflight"),
            (1, "guided_provider_preflight_response"),
            (1, "guided_provider_scope"),
            (1, "guided_provider_scope_response"),
            (1, "guided_refinement"),
            (1, "models"),
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
                name.startswith(("authorize_", "build_", "execute_", "materialize_"))
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
