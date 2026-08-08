"""Phase 5.28 explicit live credential-binding preparation/review gate."""

from __future__ import annotations

import ast
import inspect
import json
import unittest
from dataclasses import fields, replace
from datetime import timedelta
from unittest.mock import patch

import trip_planner
from tests.phase5_fixture_cache import reuse_immutable_default_fixture
from trip_planner import (
    guided_provider_request_credential_binding_response as response_module,
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
    ASSESS_CREDENTIAL_BINDING_RESPONSE_AT,
    CAPTURE_CREDENTIAL_BINDING_RESPONSE_AT,
    _captured_credential_binding_response,
)
from trip_planner.guided_provider_preflight import (
    GuidedProviderCredentialStatus,
)
from trip_planner.guided_provider_request_credential_binding_response import (
    GuidedProviderRequestCredentialBindingResponseKind,
    GuidedProviderRequestCredentialBindingResponseReview,
)
from trip_planner.guided_provider_request_live_credential_binding_review import (
    GUIDED_PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_REVIEW_VERSION,
    GuidedProviderRequestCredentialAvailabilityAttestation,
    GuidedProviderRequestLiveCredentialBindingReview,
    GuidedProviderRequestLiveCredentialBindingReviewProblemCode,
    GuidedProviderRequestLiveCredentialBindingReviewStatus,
    assess_guided_provider_request_live_credential_binding_review,
    prepare_guided_provider_request_live_credential_binding_review,
)
from trip_planner.guided_provider_request_send_preparation import (
    GuidedProviderRequestCredentialSlot,
)


PREPARE_LIVE_CREDENTIAL_BINDING_REVIEW_AT = (
    ASSESS_CREDENTIAL_BINDING_RESPONSE_AT
    + timedelta(milliseconds=250)
)
ASSESS_LIVE_CREDENTIAL_BINDING_REVIEW_AT = (
    PREPARE_LIVE_CREDENTIAL_BINDING_REVIEW_AT
    + timedelta(milliseconds=250)
)


def _availability_attestations(
    response_context: tuple[object, ...],
    status: GuidedProviderCredentialStatus = (
        GuidedProviderCredentialStatus.AVAILABLE
    ),
) -> tuple[GuidedProviderRequestCredentialAvailabilityAttestation, ...]:
    send_preparation = response_context[-3]
    slots = tuple(
        sorted(
            {item.credential_slot for item in send_preparation._bindings},
            key=lambda item: item.value,
        )
    )
    return tuple(
        GuidedProviderRequestCredentialAvailabilityAttestation(
            credential_slot=slot,
            availability_status=status,
        )
        for slot in slots
    )


@reuse_immutable_default_fixture
def _prepared_live_credential_binding_review():
    response_context, preimages = _captured_credential_binding_response()
    attestations = _availability_attestations(response_context)
    review = prepare_guided_provider_request_live_credential_binding_review(
        *response_context,
        preimages=preimages,
        availability_attestations=attestations,
        evaluation_at=PREPARE_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
    )
    return (*response_context, review), preimages, attestations


class GuidedProviderRequestLiveCredentialBindingReviewTests(
    unittest.TestCase
):
    @classmethod
    def setUpClass(cls) -> None:
        (
            cls.default_context,
            cls.default_preimages,
            cls.default_attestations,
        ) = _prepared_live_credential_binding_review()
        cls.response_context = cls.default_context[:-1]
        cls.default_review = cls.default_context[-1]
        cls.response_review = (
            cls.default_review._credential_binding_response_review
        )
        with patch.object(
            review_module,
            "assess_guided_provider_request_credential_binding_response",
            return_value=cls.response_review,
        ):
            cls.default_assessed = (
                assess_guided_provider_request_live_credential_binding_review(
                    *cls.default_context,
                    preimages=cls.default_preimages,
                    evaluation_at=ASSESS_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
                )
            )

    def test_available_slots_yield_only_an_exact_current_user_review(
        self,
    ) -> None:
        review = self.default_assessed
        safe = review.to_dict()
        handoff = safe["provider_request_live_credential_binding_review"]

        self.assertIs(self.default_review, review)
        self.assertEqual(
            GuidedProviderRequestLiveCredentialBindingReviewStatus
            .REVIEW_REQUIRED,
            review.status,
        )
        self.assertEqual(
            "capture_private_provider_request_live_credential_binding_response",
            review.next_action,
        )
        self.assertTrue(safe["requires_user_response"])
        self.assertTrue(safe["requires_user_review"])
        self.assertTrue(safe["requires_user_decision"])
        self.assertEqual(
            ["accept_live_credential_binding", "request_smaller", "cancel"],
            safe["response_options"],
        )
        self.assertTrue(
            handoff["exact_accepted_credential_binding_response_bound"]
        )
        self.assertTrue(
            handoff["host_credential_availability_attestation_fresh"]
        )
        self.assertEqual(1, handoff["credential_slot_count"])
        self.assertEqual(
            "google_maps_api_key_header",
            handoff["credential_availability_attestations"][0][
                "credential_slot"
            ],
        )
        self.assertTrue(
            handoff["credential_availability_attestations"][0][
                "credential_available"
            ]
        )
        self.assertTrue(handoff["all_credentials_available"])
        self.assertEqual(2, handoff["transport_binding_count"])
        self.assertEqual(
            2,
            handoff[
                "eligible_for_live_credential_binding_response_count"
            ],
        )
        self.assertTrue(
            handoff[
                "requires_exact_typed_live_credential_binding_response"
            ]
        )
        self.assertFalse(
            handoff["generic_continue_is_live_credential_binding_authority"]
        )
        self.assertFalse(handoff["credential_value_access_permitted"])
        self.assertFalse(handoff["credential_values_accessed"])
        self.assertFalse(handoff["credential_values_bound"])
        self.assertFalse(handoff["credential_values_included"])
        self.assertFalse(handoff["credential_binding_authority_active"])
        self.assertFalse(handoff["send_authority_active"])
        self.assertFalse(handoff["execution_authority_active"])
        self.assertEqual(0, handoff["http_request_count_created_by_review"])
        self.assertEqual(0, handoff["provider_call_count_observed"])
        self.assertFalse(handoff["provider_calls_permitted"])
        self.assertEqual("candidate", handoff["decision_state"])
        self.assertEqual("unverified", handoff["evidence_state"])

    def test_unavailable_slot_is_blocked_without_a_response_choice(self) -> None:
        unavailable = _availability_attestations(
            self.response_context,
            GuidedProviderCredentialStatus.UNAVAILABLE,
        )
        with patch.object(
            review_module,
            "assess_guided_provider_request_credential_binding_response",
            return_value=self.response_review,
        ):
            review = (
                prepare_guided_provider_request_live_credential_binding_review(
                    *self.response_context,
                    preimages=self.default_preimages,
                    availability_attestations=unavailable,
                    evaluation_at=PREPARE_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
                )
            )
        safe = review.to_dict()
        handoff = safe["provider_request_live_credential_binding_review"]

        self.assertEqual(
            GuidedProviderRequestLiveCredentialBindingReviewStatus.BLOCKED,
            review.status,
        )
        self.assertEqual(
            "refresh_private_provider_request_credential_availability_attestation",
            review.next_action,
        )
        self.assertFalse(safe["requires_user_response"])
        self.assertFalse(safe["requires_user_review"])
        self.assertFalse(safe["requires_user_decision"])
        self.assertEqual([], safe["response_options"])
        self.assertEqual(
            [
                GuidedProviderRequestLiveCredentialBindingReviewProblemCode
                .CREDENTIAL_UNAVAILABLE.value
            ],
            safe["problems"],
        )
        self.assertFalse(handoff["all_credentials_available"])
        self.assertEqual(
            0,
            handoff[
                "eligible_for_live_credential_binding_response_count"
            ],
        )
        self.assertFalse(handoff["credential_values_accessed"])
        self.assertFalse(handoff["provider_calls_permitted"])

    def test_slot_attestations_require_exact_complete_unique_coverage(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            prepare_guided_provider_request_live_credential_binding_review(
                *self.response_context,
                preimages=self.default_preimages,
                availability_attestations=(),
                evaluation_at=PREPARE_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
            )
        duplicate = (
            self.default_attestations[0],
            self.default_attestations[0],
        )
        with self.assertRaises(ValueError):
            prepare_guided_provider_request_live_credential_binding_review(
                *self.response_context,
                preimages=self.default_preimages,
                availability_attestations=duplicate,
                evaluation_at=PREPARE_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
            )
        extra = (
            *self.default_attestations,
            GuidedProviderRequestCredentialAvailabilityAttestation(
                credential_slot=(
                    GuidedProviderRequestCredentialSlot
                    .SERPAPI_API_KEY_QUERY_PARAMETER
                ),
                availability_status=GuidedProviderCredentialStatus.AVAILABLE,
            ),
        )
        with self.assertRaises(ValueError):
            prepare_guided_provider_request_live_credential_binding_review(
                *self.response_context,
                preimages=self.default_preimages,
                availability_attestations=extra,
                evaluation_at=PREPARE_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
            )
        with self.assertRaises(TypeError):
            GuidedProviderRequestCredentialAvailabilityAttestation(
                credential_slot="google_maps_api_key_header",
                availability_status=GuidedProviderCredentialStatus.AVAILABLE,
            )
        with self.assertRaises(TypeError):
            GuidedProviderRequestCredentialAvailabilityAttestation(
                credential_slot=(
                    GuidedProviderRequestCredentialSlot
                    .GOOGLE_MAPS_API_KEY_HEADER
                ),
                availability_status="available",
            )

    def test_preparation_requires_the_exact_accepted_phase527_branch(
        self,
    ) -> None:
        kind = (
            GuidedProviderRequestCredentialBindingResponseKind.REQUEST_SMALLER
        )
        status, action = response_module._response_branch(kind)
        smaller_review = GuidedProviderRequestCredentialBindingResponseReview(
            status=status,
            response_kind=kind,
            next_action=action,
            transport_profile_counts=(
                self.response_review.transport_profile_counts
            ),
            _send_authorization_response_review=(
                self.response_review._send_authorization_response_review
            ),
            _token=response_module._REVIEW_TOKEN,
        )
        with patch.object(
            review_module,
            "assess_guided_provider_request_credential_binding_response",
            return_value=smaller_review,
        ):
            with self.assertRaisesRegex(ValueError, "accepted response"):
                prepare_guided_provider_request_live_credential_binding_review(
                    *self.response_context,
                    preimages=self.default_preimages,
                    availability_attestations=self.default_attestations,
                    evaluation_at=PREPARE_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
                )

    def test_assessment_rechecks_original_and_current_response_fail_closed(
        self,
    ) -> None:
        with patch.object(
            review_module,
            "assess_guided_provider_request_credential_binding_response",
            return_value=self.response_review,
        ) as reassess:
            assessed = (
                assess_guided_provider_request_live_credential_binding_review(
                    *self.default_context,
                    preimages=self.default_preimages,
                    evaluation_at=ASSESS_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
                )
            )
        self.assertIs(self.default_review, assessed)
        self.assertEqual(2, reassess.call_count)
        self.assertEqual(
            [
                PREPARE_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
                ASSESS_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
            ],
            [
                call.kwargs["evaluation_at"]
                for call in reassess.call_args_list
            ],
        )

        with patch.object(
            review_module,
            "assess_guided_provider_request_credential_binding_response",
            side_effect=ValueError("original response stale"),
        ):
            with self.assertRaisesRegex(ValueError, "original response stale"):
                assess_guided_provider_request_live_credential_binding_review(
                    *self.default_context,
                    preimages=self.default_preimages,
                    evaluation_at=ASSESS_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
                )
        with patch.object(
            review_module,
            "assess_guided_provider_request_credential_binding_response",
            side_effect=(
                self.response_review,
                ValueError("current response stale"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "current response stale"):
                assess_guided_provider_request_live_credential_binding_review(
                    *self.default_context,
                    preimages=self.default_preimages,
                    evaluation_at=ASSESS_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
                )

    def test_clock_rollback_expiry_and_fingerprint_tamper_fail_closed(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            assess_guided_provider_request_live_credential_binding_review(
                *self.default_context,
                preimages=self.default_preimages,
                evaluation_at=(
                    PREPARE_LIVE_CREDENTIAL_BINDING_REVIEW_AT
                    - timedelta(microseconds=1)
                ),
            )
        with self.assertRaises(ValueError):
            assess_guided_provider_request_live_credential_binding_review(
                *self.default_context,
                preimages=self.default_preimages,
                evaluation_at=self.default_review._expires_at,
            )

        tampered = GuidedProviderRequestLiveCredentialBindingReview(
            status=self.default_review.status,
            next_action=self.default_review.next_action,
            availability_attestations=self.default_attestations,
            _credential_binding_response_review=self.response_review,
            _prepared_at=PREPARE_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
            _expires_at=self.default_review._expires_at,
            _context_fingerprint="a" * 64,
            _token=review_module._REVIEW_TOKEN,
        )
        with patch.object(
            review_module,
            "assess_guided_provider_request_credential_binding_response",
            return_value=self.response_review,
        ):
            with self.assertRaisesRegex(ValueError, "differs from exact context"):
                assess_guided_provider_request_live_credential_binding_review(
                    *self.response_context,
                    tampered,
                    preimages=self.default_preimages,
                    evaluation_at=ASSESS_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
                )

    def test_safe_views_hide_private_values_identifiers_fingerprints_and_times(
        self,
    ) -> None:
        review = self.default_review
        details = self.default_preimages[1].target
        rendered = "\n".join(
            (
                repr(review),
                repr(self.default_attestations[0]),
                json.dumps(review.to_dict(), ensure_ascii=False),
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
            EXPIRES_AT.isoformat(),
        ):
            self.assertNotIn(private_value, rendered)
        self.assertEqual(
            {
                "status",
                "next_action",
                "availability_attestations",
                "_credential_binding_response_review",
                "_prepared_at",
                "_expires_at",
                "_context_fingerprint",
                "contract_version",
            },
            {item.name for item in fields(type(review))},
        )
        self.assertNotIn("preimages", {item.name for item in fields(type(review))})
        safe = review.to_dict()[
            "provider_request_live_credential_binding_review"
        ]
        self.assertFalse(safe["private_request_contract_values_exposed"])
        self.assertFalse(safe["provider_identifier_values_exposed"])
        self.assertFalse(safe["source_binding_fingerprints_exposed"])
        self.assertFalse(safe["context_fingerprints_exposed"])
        self.assertFalse(safe["exact_attestation_times_exposed"])
        self.assertFalse(safe["raw_target_preimages_retained"])

    def test_review_is_token_gated_and_cannot_be_forged(self) -> None:
        review = self.default_review
        with self.assertRaises(ValueError):
            replace(review, next_action="bind_live_credential")
        with self.assertRaises(ValueError):
            GuidedProviderRequestLiveCredentialBindingReview(
                status=(
                    GuidedProviderRequestLiveCredentialBindingReviewStatus
                    .REVIEW_REQUIRED
                ),
                next_action=(
                    "capture_private_provider_request_live_credential_binding_response"
                ),
                availability_attestations=self.default_attestations,
                _credential_binding_response_review=self.response_review,
                _prepared_at=PREPARE_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
                _expires_at=self.default_review._expires_at,
                _context_fingerprint="a" * 64,
            )
        with self.assertRaises(ValueError):
            GuidedProviderRequestLiveCredentialBindingReview(
                status=(
                    GuidedProviderRequestLiveCredentialBindingReviewStatus.BLOCKED
                ),
                next_action=(
                    "refresh_private_provider_request_"
                    "credential_availability_attestation"
                ),
                availability_attestations=self.default_attestations,
                _credential_binding_response_review=self.response_review,
                _prepared_at=PREPARE_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
                _expires_at=self.default_review._expires_at,
                _context_fingerprint="a" * 64,
                _token=review_module._REVIEW_TOKEN,
            )

    def test_module_exports_review_only_and_has_no_credential_or_network_path(
        self,
    ) -> None:
        for name in (
            "GUIDED_PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_REVIEW_VERSION",
            "GuidedProviderRequestCredentialAvailabilityAttestation",
            "GuidedProviderRequestLiveCredentialBindingReview",
            "GuidedProviderRequestLiveCredentialBindingReviewProblemCode",
            "GuidedProviderRequestLiveCredentialBindingReviewStatus",
            "assess_guided_provider_request_live_credential_binding_review",
            "prepare_guided_provider_request_live_credential_binding_review",
        ):
            self.assertTrue(hasattr(trip_planner, name))
            self.assertIn(name, trip_planner.__all__)
        self.assertEqual(
            "guided-provider-request-live-credential-binding-review/v1",
            GUIDED_PROVIDER_REQUEST_LIVE_CREDENTIAL_BINDING_REVIEW_VERSION,
        )
        for unsupported_name in (
            "capture_guided_provider_request_live_credential_binding_response",
            "bind_guided_provider_credential_value",
            "read_guided_provider_credential",
            "build_guided_provider_http_request",
            "execute_guided_provider_request",
            "send_guided_provider_request",
            "call_guided_provider",
        ):
            self.assertFalse(hasattr(review_module, unsupported_name))

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
