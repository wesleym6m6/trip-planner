"""Phase 5.29 exact live credential-binding response-only gate."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace
from datetime import timedelta
from unittest.mock import patch

import trip_planner
from trip_planner import (
    guided_provider_request_live_credential_binding_response as response_module,
)
from trip_planner import (
    guided_provider_request_live_credential_binding_review as review_module,
)
from tests.test_phase513_guided_provider_preflight import EXPIRES_AT
from tests.test_phase516_guided_provider_execution_target_bindings import (
    PRIVATE_QUERY,
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
    PREPARE_SEND_PREPARATION_AT,
)
from tests.test_phase526_guided_provider_request_credential_binding_review import (
    PREPARE_CREDENTIAL_BINDING_REVIEW_AT,
)
from tests.test_phase527_guided_provider_request_credential_binding_response import (
    CAPTURE_CREDENTIAL_BINDING_RESPONSE_AT,
    _captured_credential_binding_response,
)
from tests.test_phase528_guided_provider_request_live_credential_binding_review import (
    ASSESS_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
    PREPARE_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
    _availability_attestations,
)
from trip_planner.guided_provider_preflight import (
    GuidedProviderCredentialStatus,
)
from trip_planner.guided_provider_request_live_credential_binding_response import (
    GUIDED_PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_RESPONSE_VERSION,
    GuidedProviderRequestLiveCredentialBindingResponse,
    GuidedProviderRequestLiveCredentialBindingResponseKind,
    GuidedProviderRequestLiveCredentialBindingResponseReview,
    GuidedProviderRequestLiveCredentialBindingResponseStatus,
    assess_guided_provider_request_live_credential_binding_response,
    capture_guided_provider_request_live_credential_binding_response,
)
from trip_planner.guided_provider_request_live_credential_binding_review import (
    GuidedProviderRequestCredentialAvailabilityAttestation,
    GuidedProviderRequestLiveCredentialBindingReview,
    GuidedProviderRequestLiveCredentialBindingReviewStatus,
    prepare_guided_provider_request_live_credential_binding_review,
)


CAPTURE_LIVE_CREDENTIAL_BINDING_RESPONSE_AT = (
    ASSESS_LIVE_CREDENTIAL_BINDING_REVIEW_AT
    + timedelta(milliseconds=250)
)
ASSESS_LIVE_CREDENTIAL_BINDING_RESPONSE_AT = (
    CAPTURE_LIVE_CREDENTIAL_BINDING_RESPONSE_AT
    + timedelta(milliseconds=250)
)


class GuidedProviderRequestLiveCredentialBindingResponseTests(
    unittest.TestCase
):
    @classmethod
    def setUpClass(cls) -> None:
        cls.credential_response_context, cls.default_preimages = (
            _captured_credential_binding_response()
        )
        cls.default_attestations = _availability_attestations(
            cls.credential_response_context
        )
        cls.live_review = (
            prepare_guided_provider_request_live_credential_binding_review(
                *cls.credential_response_context,
                preimages=cls.default_preimages,
                availability_attestations=cls.default_attestations,
                evaluation_at=PREPARE_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
            )
        )
        cls.review_context = (
            *cls.credential_response_context,
            cls.live_review,
        )
        cls.default_response = (
            capture_guided_provider_request_live_credential_binding_response(
                *cls.review_context,
                preimages=cls.default_preimages,
                kind=(
                    GuidedProviderRequestLiveCredentialBindingResponseKind
                    .ACCEPT_LIVE_CREDENTIAL_BINDING
                ),
                evaluation_at=CAPTURE_LIVE_CREDENTIAL_BINDING_RESPONSE_AT,
            )
        )
        cls.default_context = (*cls.review_context, cls.default_response)
        with patch.object(
            response_module,
            "assess_guided_provider_request_live_credential_binding_review",
            return_value=cls.live_review,
        ):
            cls.default_assessed = (
                assess_guided_provider_request_live_credential_binding_response(
                    *cls.default_context,
                    preimages=cls.default_preimages,
                    evaluation_at=ASSESS_LIVE_CREDENTIAL_BINDING_RESPONSE_AT,
                )
            )

    def test_accept_only_prepares_an_ephemeral_credential_value_gate(
        self,
    ) -> None:
        assessed = self.default_assessed
        safe = assessed.to_dict()
        handoff = safe[
            "provider_request_live_credential_binding_response"
        ]

        self.assertEqual(
            GuidedProviderRequestLiveCredentialBindingResponseStatus
            .READY_FOR_PRIVATE_PROVIDER_REQUEST_EPHEMERAL_CREDENTIAL_VALUE_BINDING_GATE,
            assessed.status,
        )
        self.assertEqual(
            "prepare_private_provider_request_ephemeral_credential_value_binding_gate",
            assessed.next_action,
        )
        self.assertFalse(safe["requires_user_response"])
        self.assertFalse(safe["requires_user_review"])
        self.assertFalse(safe["requires_user_decision"])
        self.assertEqual("accept_live_credential_binding", handoff["kind"])
        self.assertTrue(handoff["live_credential_binding_response_captured"])
        self.assertFalse(
            handoff["requires_exact_typed_live_credential_binding_response"]
        )
        self.assertTrue(
            handoff["accepted_exact_private_live_credential_binding_review"]
        )
        self.assertTrue(
            handoff[
                "accepted_for_separate_ephemeral_credential_value_binding_gate"
            ]
        )
        self.assertTrue(
            handoff[
                "may_prepare_private_provider_request_ephemeral_credential_value_binding_gate"
            ]
        )
        self.assertEqual(2, handoff["transport_binding_count"])
        self.assertEqual(
            2,
            handoff[
                "eligible_for_separate_ephemeral_credential_value_binding_gate_count"
            ],
        )
        self.assertTrue(
            handoff[
                "same_exact_credential_availability_attestations_revalidated"
            ]
        )
        self.assertFalse(handoff["credential_value_access_permitted"])
        self.assertFalse(handoff["credential_values_accessed"])
        self.assertFalse(handoff["credential_values_bound"])
        self.assertFalse(handoff["credential_values_included"])
        self.assertFalse(handoff["provider_request_contracts_are_executable"])
        self.assertFalse(handoff["provider_request_contracts_are_sendable"])
        self.assertFalse(handoff["credential_binding_authority_active"])
        self.assertFalse(handoff["send_authority_active"])
        self.assertFalse(handoff["execution_authority_active"])
        self.assertEqual(0, handoff["http_request_count_created_by_response"])
        self.assertEqual(0, handoff["provider_call_count_observed"])
        self.assertFalse(handoff["provider_calls_permitted"])
        self.assertEqual("candidate", handoff["decision_state"])
        self.assertEqual("unverified", handoff["evidence_state"])
        self.assertIn(
            "provider_request_live_credential_binding_response",
            safe["needs_verification"],
        )

    def test_all_choices_have_non_executable_branch_semantics(self) -> None:
        cases = (
            (
                GuidedProviderRequestLiveCredentialBindingResponseKind
                .ACCEPT_LIVE_CREDENTIAL_BINDING,
                (
                    "ready_for_private_provider_request_"
                    "ephemeral_credential_value_binding_gate"
                ),
                (
                    "prepare_private_provider_request_"
                    "ephemeral_credential_value_binding_gate"
                ),
                (True, False, False),
            ),
            (
                GuidedProviderRequestLiveCredentialBindingResponseKind
                .REQUEST_SMALLER,
                "ready_for_private_provider_execution_target_refinement",
                "refine_private_provider_execution_targets",
                (False, True, False),
            ),
            (
                GuidedProviderRequestLiveCredentialBindingResponseKind.CANCEL,
                "provider_request_live_credential_binding_cancelled",
                "continue_private_evidence_review",
                (False, False, True),
            ),
        )
        for kind, status, action, flags in cases:
            with self.subTest(kind=kind):
                branch_status, branch_action = response_module._response_branch(
                    kind
                )
                assessed = (
                    GuidedProviderRequestLiveCredentialBindingResponseReview(
                        status=branch_status,
                        response_kind=kind,
                        next_action=branch_action,
                        _live_credential_binding_review=self.live_review,
                        _token=response_module._REVIEW_TOKEN,
                    )
                )
                safe = assessed.to_dict()
                handoff = safe[
                    "provider_request_live_credential_binding_response"
                ]
                self.assertEqual(status, safe["status"])
                self.assertEqual(action, safe["next_action"])
                self.assertEqual(
                    flags[0],
                    handoff[
                        "accepted_exact_private_live_credential_binding_review"
                    ],
                )
                self.assertEqual(
                    flags[1], handoff["requested_smaller_provider_scope"]
                )
                self.assertEqual(
                    flags[2], handoff["live_credential_binding_path_cancelled"]
                )
                self.assertEqual(
                    2 if flags[0] else 0,
                    handoff[
                        "eligible_for_separate_ephemeral_credential_value_binding_gate_count"
                    ],
                )
                self.assertFalse(handoff["credential_values_accessed"])
                self.assertFalse(handoff["credential_values_bound"])
                self.assertFalse(handoff["provider_calls_permitted"])

    def test_capture_requires_an_exact_enum_not_free_text(self) -> None:
        for value in (
            "accept_live_credential_binding",
            "yes, continue and bind the credential",
        ):
            with self.subTest(value=value), self.assertRaises(TypeError):
                capture_guided_provider_request_live_credential_binding_response(
                    *self.review_context,
                    preimages=self.default_preimages,
                    kind=value,
                    evaluation_at=CAPTURE_LIVE_CREDENTIAL_BINDING_RESPONSE_AT,
                )

    def test_capture_rejects_a_blocked_or_unavailable_review(self) -> None:
        unavailable = tuple(
            GuidedProviderRequestCredentialAvailabilityAttestation(
                credential_slot=item.credential_slot,
                availability_status=GuidedProviderCredentialStatus.UNAVAILABLE,
            )
            for item in self.default_attestations
        )
        blocked = GuidedProviderRequestLiveCredentialBindingReview(
            status=GuidedProviderRequestLiveCredentialBindingReviewStatus.BLOCKED,
            next_action=(
                "refresh_private_provider_request_"
                "credential_availability_attestation"
            ),
            availability_attestations=unavailable,
            _credential_binding_response_review=(
                self.live_review._credential_binding_response_review
            ),
            _prepared_at=PREPARE_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
            _expires_at=self.live_review._expires_at,
            _context_fingerprint="a" * 64,
            _token=review_module._REVIEW_TOKEN,
        )
        with patch.object(
            response_module,
            "assess_guided_provider_request_live_credential_binding_review",
            return_value=blocked,
        ):
            with self.assertRaisesRegex(ValueError, "current review"):
                capture_guided_provider_request_live_credential_binding_response(
                    *self.review_context,
                    preimages=self.default_preimages,
                    kind=(
                        GuidedProviderRequestLiveCredentialBindingResponseKind
                        .ACCEPT_LIVE_CREDENTIAL_BINDING
                    ),
                    evaluation_at=CAPTURE_LIVE_CREDENTIAL_BINDING_RESPONSE_AT,
                )

    def test_original_and_current_review_rechecks_fail_closed(self) -> None:
        with patch.object(
            response_module,
            "assess_guided_provider_request_live_credential_binding_review",
            return_value=self.live_review,
        ) as reassess:
            assessed = (
                assess_guided_provider_request_live_credential_binding_response(
                    *self.default_context,
                    preimages=self.default_preimages,
                    evaluation_at=ASSESS_LIVE_CREDENTIAL_BINDING_RESPONSE_AT,
                )
            )
        self.assertEqual(self.default_assessed.to_dict(), assessed.to_dict())
        self.assertEqual(2, reassess.call_count)
        self.assertEqual(
            [
                CAPTURE_LIVE_CREDENTIAL_BINDING_RESPONSE_AT,
                ASSESS_LIVE_CREDENTIAL_BINDING_RESPONSE_AT,
            ],
            [
                call.kwargs["evaluation_at"]
                for call in reassess.call_args_list
            ],
        )

        with patch.object(
            response_module,
            "assess_guided_provider_request_live_credential_binding_review",
            side_effect=ValueError("original live review stale"),
        ):
            with self.assertRaisesRegex(ValueError, "original live review stale"):
                assess_guided_provider_request_live_credential_binding_response(
                    *self.default_context,
                    preimages=self.default_preimages,
                    evaluation_at=ASSESS_LIVE_CREDENTIAL_BINDING_RESPONSE_AT,
                )
        with patch.object(
            response_module,
            "assess_guided_provider_request_live_credential_binding_review",
            side_effect=(self.live_review, ValueError("current review expired")),
        ):
            with self.assertRaisesRegex(ValueError, "current review expired"):
                assess_guided_provider_request_live_credential_binding_response(
                    *self.default_context,
                    preimages=self.default_preimages,
                    evaluation_at=self.live_review._expires_at,
                )

    def test_clock_rollback_and_fingerprint_tamper_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            assess_guided_provider_request_live_credential_binding_response(
                *self.default_context,
                preimages=self.default_preimages,
                evaluation_at=(
                    CAPTURE_LIVE_CREDENTIAL_BINDING_RESPONSE_AT
                    - timedelta(microseconds=1)
                ),
            )

        tampered = GuidedProviderRequestLiveCredentialBindingResponse(
            kind=(
                GuidedProviderRequestLiveCredentialBindingResponseKind
                .ACCEPT_LIVE_CREDENTIAL_BINDING
            ),
            _captured_at=CAPTURE_LIVE_CREDENTIAL_BINDING_RESPONSE_AT,
            _context_fingerprint="a" * 64,
            _token=response_module._RESPONSE_TOKEN,
        )
        with patch.object(
            response_module,
            "assess_guided_provider_request_live_credential_binding_review",
            return_value=self.live_review,
        ):
            with self.assertRaisesRegex(ValueError, "differs from exact context"):
                assess_guided_provider_request_live_credential_binding_response(
                    *self.default_context[:-1],
                    tampered,
                    preimages=self.default_preimages,
                    evaluation_at=ASSESS_LIVE_CREDENTIAL_BINDING_RESPONSE_AT,
                )

    def test_safe_views_hide_private_values_identifiers_fingerprints_and_times(
        self,
    ) -> None:
        response = self.default_response
        assessed = self.default_assessed
        details = self.default_preimages[1].target
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
            PREPARE_SEND_REVIEW_AT.isoformat(),
            CAPTURE_SEND_AUTHORIZATION_RESPONSE_AT.isoformat(),
            PREPARE_SEND_PREPARATION_AT.isoformat(),
            PREPARE_CREDENTIAL_BINDING_REVIEW_AT.isoformat(),
            CAPTURE_CREDENTIAL_BINDING_RESPONSE_AT.isoformat(),
            PREPARE_LIVE_CREDENTIAL_BINDING_REVIEW_AT.isoformat(),
            CAPTURE_LIVE_CREDENTIAL_BINDING_RESPONSE_AT.isoformat(),
            EXPIRES_AT.isoformat(),
        ):
            self.assertNotIn(private_value, rendered)
        self.assertEqual(
            {"kind", "_captured_at", "_context_fingerprint"},
            {item.name for item in fields(type(response))},
        )
        self.assertNotIn("free_text", {item.name for item in fields(type(response))})
        self.assertNotIn("preimages", {item.name for item in fields(type(response))})
        safe = assessed.to_dict()[
            "provider_request_live_credential_binding_response"
        ]
        self.assertFalse(safe["private_request_contract_values_exposed"])
        self.assertFalse(safe["provider_identifier_values_exposed"])
        self.assertFalse(safe["source_binding_fingerprints_exposed"])
        self.assertFalse(safe["context_fingerprints_exposed"])
        self.assertFalse(safe["exact_response_times_exposed"])
        self.assertFalse(safe["raw_target_preimages_retained"])

    def test_upstream_counts_and_boolean_attestations_survive_exactly(
        self,
    ) -> None:
        assessed = self.default_assessed
        safe = assessed.to_dict()[
            "provider_request_live_credential_binding_response"
        ]
        source = assessed._live_credential_binding_review

        self.assertEqual(
            source.accepted_scope_item_count,
            assessed.accepted_scope_item_count,
        )
        self.assertEqual(source.contract_count, assessed.contract_count)
        self.assertEqual(source.binding_count, assessed.binding_count)
        self.assertEqual(
            source.accepted_max_request_count,
            assessed.accepted_max_request_count,
        )
        self.assertEqual(
            source.availability_attestations,
            assessed.availability_attestations,
        )
        self.assertEqual(1, safe["credential_slot_count"])
        self.assertEqual(
            [
                {
                    "credential_slot": "google_maps_api_key_header",
                    "credential_available": True,
                }
            ],
            safe["credential_availability_attestations"],
        )
        self.assertEqual(
            52_000,
            safe["estimated_bound_first_paid_tier_google_cost_usd_micros"],
        )
        self.assertFalse(
            safe["credential_availability_attestations_modified_by_response"]
        )
        self.assertFalse(safe["credential_values_accessed"])
        self.assertFalse(safe["credential_values_bound"])

    def test_response_and_assessment_are_token_gated(self) -> None:
        with self.assertRaises(ValueError):
            GuidedProviderRequestLiveCredentialBindingResponse(
                kind=(
                    GuidedProviderRequestLiveCredentialBindingResponseKind
                    .ACCEPT_LIVE_CREDENTIAL_BINDING
                ),
                _captured_at=CAPTURE_LIVE_CREDENTIAL_BINDING_RESPONSE_AT,
                _context_fingerprint="a" * 64,
            )
        with self.assertRaises(ValueError):
            replace(
                self.default_response,
                kind=GuidedProviderRequestLiveCredentialBindingResponseKind.CANCEL,
            )
        with self.assertRaises(ValueError):
            replace(self.default_assessed, next_action="bind_credential_value")
        with self.assertRaises(ValueError):
            GuidedProviderRequestLiveCredentialBindingResponseReview(
                status=(
                    GuidedProviderRequestLiveCredentialBindingResponseStatus
                    .READY_FOR_PRIVATE_PROVIDER_REQUEST_EPHEMERAL_CREDENTIAL_VALUE_BINDING_GATE
                ),
                response_kind=(
                    GuidedProviderRequestLiveCredentialBindingResponseKind
                    .ACCEPT_LIVE_CREDENTIAL_BINDING
                ),
                next_action=(
                    "prepare_private_provider_request_"
                    "ephemeral_credential_value_binding_gate"
                ),
                _live_credential_binding_review=self.live_review,
            )

    def test_module_exports_response_only_and_has_no_secret_or_network_path(
        self,
    ) -> None:
        for name in (
            "GUIDED_PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_RESPONSE_VERSION",
            "GuidedProviderRequestLiveCredentialBindingResponse",
            "GuidedProviderRequestLiveCredentialBindingResponseKind",
            "GuidedProviderRequestLiveCredentialBindingResponseReview",
            "GuidedProviderRequestLiveCredentialBindingResponseStatus",
            "assess_guided_provider_request_live_credential_binding_response",
            "capture_guided_provider_request_live_credential_binding_response",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        self.assertEqual(
            "guided-provider-request-live-credential-binding-response/v1",
            GUIDED_PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_RESPONSE_VERSION,
        )
        for unsupported_name in (
            "bind_guided_provider_credential_value",
            "read_guided_provider_credential",
            "build_guided_provider_http_request",
            "execute_guided_provider_request",
            "send_guided_provider_request",
            "call_guided_provider",
        ):
            self.assertFalse(hasattr(response_module, unsupported_name))

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
            if isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
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
