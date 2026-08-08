"""Phase 5.30 composed provider pre-execution facade."""

from __future__ import annotations

import ast
import copy
import inspect
import json
import pickle
import traceback
import unittest
from dataclasses import asdict, fields, is_dataclass
from datetime import timedelta

import trip_planner
from tests.phase5_fixture_cache import reuse_immutable_default_fixture
from tests.test_phase513_guided_provider_preflight import _google_item
from tests.test_phase516_guided_provider_execution_target_bindings import (
    PRIVATE_QUERY,
    _hotel_request,
    _route_request,
    _serpapi_item,
)
from tests.test_phase520_guided_provider_request_materialization_review import (
    _prepared_single_capability_review,
)
from tests.test_phase525_guided_provider_request_send_preparation import (
    _accepted_response_from_materialization_review,
    _prepared_send_preparation,
)
from tests.test_phase529_guided_provider_request_live_credential_binding_response import (
    ASSESS_LIVE_CREDENTIAL_BINDING_RESPONSE_AT,
    _captured_live_credential_binding_response,
)
from trip_planner import guided_provider_pre_execution as pre_execution_module
from trip_planner.guided_evidence_plan import GuidedEvidenceTopic
from trip_planner.guided_provider_pre_execution import (
    GUIDED_PROVIDER_PRE_EXECUTION_VERSION,
    GuidedProviderCredentialBindingError,
    GuidedProviderPreExecution,
    GuidedProviderPreExecutionContext,
    GuidedProviderPreExecutionStatus,
    assess_guided_provider_pre_execution,
    compose_guided_provider_pre_execution_context,
    prepare_guided_provider_pre_execution,
)
from trip_planner.guided_provider_request_materialization_review import (
    GuidedProviderRequestMaterializationKind,
)
from trip_planner.guided_provider_request_send_preparation import (
    GuidedProviderRequestCredentialSlot,
)
from trip_planner.guided_provider_scope import GuidedProviderCapability


COMPOSE_AT = ASSESS_LIVE_CREDENTIAL_BINDING_RESPONSE_AT + timedelta(
    milliseconds=250
)
PREPARE_START_AT = COMPOSE_AT + timedelta(milliseconds=250)
PREPARE_FINISH_AT = PREPARE_START_AT + timedelta(milliseconds=250)
ASSESS_PRE_EXECUTION_AT = PREPARE_FINISH_AT + timedelta(milliseconds=125)
TEST_SECRET = "phase530-test-only-secret"


class _SequenceClock:
    def __init__(self, *values):
        self.values = list(values)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self.values:
            raise AssertionError("trusted clock exhausted")
        return self.values.pop(0)


class _RecordingResolver:
    def __init__(self, value: object = TEST_SECRET):
        self.value = value
        self.calls: list[GuidedProviderRequestCredentialSlot] = []

    def resolve(self, slot: GuidedProviderRequestCredentialSlot) -> str:
        self.calls.append(slot)
        return self.value  # type: ignore[return-value]


@reuse_immutable_default_fixture
def _composed_pre_execution_context():
    response_context, preimages, _ = (
        _captured_live_credential_binding_response()
    )
    context = compose_guided_provider_pre_execution_context(
        *response_context,
        preimages=preimages,
        evaluation_at=COMPOSE_AT,
    )
    return context, preimages


def _prepare_default():
    context, preimages = _composed_pre_execution_context()
    resolver = _RecordingResolver()
    clock = _SequenceClock(PREPARE_START_AT, PREPARE_FINISH_AT)
    bundle = prepare_guided_provider_pre_execution(
        context,
        resolver,
        preimages=preimages,
        clock=clock,
    )
    return context, bundle, preimages, resolver, clock


def _single_transport_binding(topic, capability, item, request):
    materialization_context, preimages = _prepared_single_capability_review(
        topic,
        capability,
        item,
        request,
    )
    response_context, preimages = (
        _accepted_response_from_materialization_review(
            materialization_context,
            preimages,
        )
    )
    send_context, _ = _prepared_send_preparation(
        response_context=response_context,
        preimages=preimages,
    )
    return send_context[-1]._bindings[0]


class GuidedProviderPreExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        with pre_execution_module._CONSENT_CLAIM_LOCK:
            pre_execution_module._CONSENT_CLAIMS.clear()

    def test_full_chain_builds_one_redacted_non_executable_facade(self) -> None:
        context, bundle, preimages, resolver, clock = _prepare_default()
        review = assess_guided_provider_pre_execution(
            context,
            bundle,
            preimages=preimages,
            evaluation_at=ASSESS_PRE_EXECUTION_AT,
        )
        safe = review.to_dict()
        handoff = safe["provider_pre_execution"]

        self.assertEqual(GUIDED_PROVIDER_PRE_EXECUTION_VERSION, review.contract_version)
        self.assertEqual(
            GuidedProviderPreExecutionStatus.READY_FOR_BOUNDED_PROVIDER_EXECUTION,
            review.status,
        )
        self.assertEqual("execute_bounded_provider_requests", review.next_action)
        self.assertEqual(2, context.request_count)
        self.assertEqual(2, bundle.request_count)
        self.assertEqual(1, bundle.credential_slot_count)
        self.assertEqual(
            [GuidedProviderRequestCredentialSlot.GOOGLE_MAPS_API_KEY_HEADER],
            resolver.calls,
        )
        self.assertEqual(2, clock.calls)
        self.assertTrue(handoff["exact_phase529_consent_revalidated"])
        self.assertTrue(
            handoff["same_exact_target_preimages_revalidated_not_retained"]
        )
        self.assertTrue(handoff["final_post_construction_recheck_complete"])
        self.assertTrue(handoff["credential_values_resolved_once_per_slot"])
        self.assertTrue(handoff["credential_values_bound_process_locally"])
        self.assertFalse(handoff["credential_values_in_safe_output"])
        self.assertFalse(handoff["prepared_bundle_serializable"])
        self.assertFalse(handoff["prepared_bundle_is_replayable_authority"])
        self.assertFalse(handoff["send_method_exposed"])
        self.assertFalse(handoff["send_authority_active"])
        self.assertFalse(handoff["execution_authority_active"])
        self.assertEqual(0, handoff["provider_call_count_observed"])
        self.assertFalse(handoff["provider_calls_permitted_by_phase530"])
        self.assertEqual("candidate", handoff["decision_state"])
        self.assertEqual("unverified", handoff["evidence_state"])
        self.assertFalse(safe["requires_user_response"])
        self.assertFalse(safe["requires_user_review"])
        self.assertFalse(safe["requires_user_decision"])

        rendered = json.dumps(safe, ensure_ascii=False, sort_keys=True)
        private_values = (
            TEST_SECRET,
            PRIVATE_QUERY,
            "ChIJ-place/private value",
            bundle._requests[0]._request_fingerprint,
            bundle._requests[1]._request_fingerprint,
        )
        for value in private_values:
            self.assertNotIn(value, rendered)
            self.assertNotIn(value, repr(context))
            self.assertNotIn(value, repr(bundle))
            self.assertNotIn(value, repr(review))
        self.assertFalse(is_dataclass(context))
        self.assertFalse(is_dataclass(bundle._requests[0]))
        self.assertNotIn("preimages", context.__slots__)
        self.assertNotIn("preimages", {item.name for item in fields(bundle)})

    def test_private_builders_preserve_exact_provider_wire_semantics(self) -> None:
        _, bundle, _, _, _ = _prepare_default()
        by_kind = {
            item.materialization_kind: item for item in bundle._requests
        }
        identity = by_kind[
            GuidedProviderRequestMaterializationKind.GOOGLE_PLACES_TEXT_SEARCH
        ]
        identity_body = json.loads(identity._json_body)
        self.assertEqual(5, identity_body["pageSize"])
        self.assertEqual(PRIVATE_QUERY, identity_body["textQuery"])
        self.assertEqual(
            ",".join(identity._contract._provider_transmitted_values[0][1]),
            dict(identity._headers)["X-Goog-FieldMask"],
        )
        self.assertNotIn("X-Goog-Api-Key", dict(identity._headers))

        details = by_kind[
            GuidedProviderRequestMaterializationKind.GOOGLE_PLACE_DETAILS
        ]
        self.assertIn("ChIJ-place%2Fprivate%20value", details._endpoint)
        self.assertNotIn("ChIJ-place/private value", details._endpoint)
        self.assertEqual(
            {"languageCode": "en-US", "regionCode": "KR"},
            dict(details._query_parameters),
        )
        self.assertNotIn("X-Goog-Api-Key", dict(details._headers))

        route_binding = _single_transport_binding(
            GuidedEvidenceTopic.ROUTE,
            GuidedProviderCapability.GOOGLE_ROUTES,
            _google_item(
                GuidedEvidenceTopic.ROUTE,
                GuidedProviderCapability.GOOGLE_ROUTES,
                1,
            ),
            _route_request(),
        )
        route_shape = pre_execution_module._build_private_http_shape(
            route_binding
        )
        route_body = json.loads(route_shape[3])
        semantic_mode = dict(
            route_binding._contract._provider_transmitted_values
        )["mode"]
        expected_mode = {
            "driving": "DRIVE",
            "walking": "WALK",
            "transit": "TRANSIT",
            "bicycling": "BICYCLE",
            "two_wheeler": "TWO_WHEELER",
        }[semantic_mode]
        self.assertEqual(expected_mode, route_body["travelMode"])
        self.assertFalse(route_body["computeAlternativeRoutes"])

        hotel_binding = _single_transport_binding(
            GuidedEvidenceTopic.LODGING,
            GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
            _serpapi_item(GuidedEvidenceTopic.LODGING),
            _hotel_request(),
        )
        hotel_shape = pre_execution_module._build_private_http_shape(
            hotel_binding
        )
        hotel_query = dict(hotel_shape[2])
        self.assertEqual("google_hotels", hotel_query["engine"])
        self.assertEqual(_hotel_request().query, hotel_query["q"])
        self.assertNotIn("api_key", hotel_query)

    def test_expiry_and_rollback_fail_closed_around_credential_access(self) -> None:
        context, preimages = _composed_pre_execution_context()

        stale_resolver = _RecordingResolver()
        with self.assertRaises(ValueError):
            prepare_guided_provider_pre_execution(
                context,
                stale_resolver,
                preimages=preimages,
                clock=_SequenceClock(context._expires_at),
            )
        self.assertEqual([], stale_resolver.calls)

        expired_during_binding = _RecordingResolver()
        with self.assertRaises(ValueError):
            prepare_guided_provider_pre_execution(
                context,
                expired_during_binding,
                preimages=preimages,
                clock=_SequenceClock(PREPARE_START_AT, context._expires_at),
            )
        self.assertEqual(1, len(expired_during_binding.calls))

        with pre_execution_module._CONSENT_CLAIM_LOCK:
            pre_execution_module._CONSENT_CLAIMS.clear()
        traceback_secret = "traceback-visible-test-secret"
        try:
            prepare_guided_provider_pre_execution(
                context,
                _RecordingResolver(traceback_secret),
                preimages=preimages,
                clock=_SequenceClock(PREPARE_START_AT, context._expires_at),
            )
        except ValueError as error:
            visible_strings: list[str] = []
            for frame, _ in traceback.walk_tb(error.__traceback__):
                if frame.f_code.co_name != "prepare_guided_provider_pre_execution":
                    continue
                for local in frame.f_locals.values():
                    if isinstance(local, str):
                        visible_strings.append(local)
                    elif isinstance(local, dict):
                        visible_strings.extend(
                            item for item in local.values()
                            if isinstance(item, str)
                        )
            self.assertNotIn(traceback_secret, visible_strings)
        else:  # pragma: no cover - the expiry must fail closed
            self.fail("binding that crosses expiry unexpectedly succeeded")

        with pre_execution_module._CONSENT_CLAIM_LOCK:
            pre_execution_module._CONSENT_CLAIMS.clear()
        rollback_resolver = _RecordingResolver()
        with self.assertRaisesRegex(ValueError, "rolled back"):
            prepare_guided_provider_pre_execution(
                context,
                rollback_resolver,
                preimages=preimages,
                clock=_SequenceClock(
                    PREPARE_START_AT,
                    PREPARE_START_AT - timedelta(microseconds=1),
                ),
            )
        self.assertEqual(1, len(rollback_resolver.calls))

    def test_invalid_credential_values_fail_with_sanitized_errors(self) -> None:
        context, preimages = _composed_pre_execution_context()
        values = ("", " leading", "line\nbreak", "x" * 4097, object())
        for value in values:
            with self.subTest(value_type=type(value).__name__):
                with pre_execution_module._CONSENT_CLAIM_LOCK:
                    pre_execution_module._CONSENT_CLAIMS.clear()
                resolver = _RecordingResolver(value)
                with self.assertRaises(GuidedProviderCredentialBindingError) as caught:
                    prepare_guided_provider_pre_execution(
                        context,
                        resolver,
                        preimages=preimages,
                        clock=_SequenceClock(
                            PREPARE_START_AT,
                            PREPARE_FINISH_AT,
                        ),
                    )
                message = str(caught.exception)
                self.assertIn("google_maps_api_key_header", message)
                if isinstance(value, str) and value:
                    self.assertNotIn(value, message)
                self.assertEqual(1, len(resolver.calls))

        class _FailingResolver:
            def resolve(self, slot):
                del slot
                raise RuntimeError(TEST_SECRET)

        with pre_execution_module._CONSENT_CLAIM_LOCK:
            pre_execution_module._CONSENT_CLAIMS.clear()
        with self.assertRaises(GuidedProviderCredentialBindingError) as caught:
            prepare_guided_provider_pre_execution(
                context,
                _FailingResolver(),
                preimages=preimages,
                clock=_SequenceClock(PREPARE_START_AT, PREPARE_FINISH_AT),
            )
        self.assertNotIn(TEST_SECRET, str(caught.exception))
        self.assertIsNone(caught.exception.__context__)

    def test_credential_bearing_objects_reject_serialization_and_copy(self) -> None:
        context, bundle, preimages, _, _ = _prepare_default()
        review = assess_guided_provider_pre_execution(
            context,
            bundle,
            preimages=preimages,
            evaluation_at=ASSESS_PRE_EXECUTION_AT,
        )
        objects = (
            context,
            bundle,
            bundle._credential_lease,
            bundle._requests[0],
            review,
        )
        for value in objects:
            with self.subTest(type=type(value).__name__):
                with self.assertRaises(TypeError):
                    pickle.dumps(value)
                with self.assertRaises(TypeError):
                    copy.copy(value)
                with self.assertRaises(TypeError):
                    copy.deepcopy(value)
        with self.assertRaises(TypeError):
            asdict(bundle)
        with self.assertRaises(TypeError):
            asdict(context)
        with self.assertRaises(TypeError):
            asdict(bundle._requests[0])
        with self.assertRaises(TypeError):
            json.dumps(bundle)

    def test_consent_and_execution_claims_are_single_use(self) -> None:
        context, bundle, preimages, _, _ = _prepare_default()
        second_resolver = _RecordingResolver("second-test-secret")
        with self.assertRaisesRegex(ValueError, "already consumed"):
            prepare_guided_provider_pre_execution(
                context,
                second_resolver,
                preimages=preimages,
                clock=_SequenceClock(PREPARE_START_AT, PREPARE_FINISH_AT),
            )
        self.assertEqual([], second_resolver.calls)

        review = assess_guided_provider_pre_execution(
            context,
            bundle,
            preimages=preimages,
            evaluation_at=ASSESS_PRE_EXECUTION_AT,
        )
        before = review.to_dict()
        slot = GuidedProviderRequestCredentialSlot.GOOGLE_MAPS_API_KEY_HEADER
        self.assertEqual(
            TEST_SECRET,
            bundle._credential_lease._value(
                slot,
                evaluation_at=ASSESS_PRE_EXECUTION_AT,
                _token=pre_execution_module._CREDENTIAL_ACCESS_TOKEN,
            ),
        )
        with self.assertRaisesRegex(ValueError, "expired"):
            bundle._credential_lease._value(
                slot,
                evaluation_at=bundle._expires_at,
                _token=pre_execution_module._CREDENTIAL_ACCESS_TOKEN,
            )
        bundle._claim.claim(
            _token=pre_execution_module._EXECUTION_CLAIM_TOKEN
        )
        bundle._credential_lease._clear(
            _token=pre_execution_module._CREDENTIAL_ACCESS_TOKEN
        )
        self.assertEqual(before, review.to_dict())
        self.assertTrue(
            before["provider_pre_execution"][
                "single_use_execution_claim_unconsumed_at_assessment"
            ]
        )
        with self.assertRaisesRegex(ValueError, "already claimed"):
            assess_guided_provider_pre_execution(
                context,
                bundle,
                preimages=preimages,
                evaluation_at=ASSESS_PRE_EXECUTION_AT,
            )

    def test_private_runtime_objects_are_sealed_and_context_bound(self) -> None:
        context, bundle, preimages, _, _ = _prepare_default()
        request = bundle._requests[0]
        assignments = (
            (context, "_context_fingerprint", "a" * 64),
            (request, "_endpoint", "https://attacker.example/"),
            (request, "endpoint_template", TEST_SECRET),
            (
                bundle._credential_lease,
                "_expires_at",
                bundle._expires_at + timedelta(days=365),
            ),
            (
                bundle._credential_lease,
                "_context_fingerprint",
                "0" * 64,
            ),
            (bundle._claim, "_claimed", False),
        )
        for target, name, value in assignments:
            with self.subTest(type=type(target).__name__, name=name):
                with self.assertRaises(AttributeError):
                    setattr(target, name, value)

        shape = pre_execution_module._build_private_http_shape(
            request._binding
        )
        alternate = pre_execution_module._prepared_request_from_shape(
            request._binding,
            shape,
            bundle._credential_lease,
            context_fingerprint="b" * 64,
        )
        self.assertNotEqual(
            request._request_fingerprint,
            alternate._request_fingerprint,
        )
        assess_guided_provider_pre_execution(
            context,
            bundle,
            preimages=preimages,
            evaluation_at=ASSESS_PRE_EXECUTION_AT,
        )

    def test_assessment_rejects_a_cleared_credential_lease(self) -> None:
        context, bundle, preimages, _, _ = _prepare_default()
        bundle._credential_lease._clear(
            _token=pre_execution_module._CREDENTIAL_ACCESS_TOKEN
        )
        with self.assertRaisesRegex(ValueError, "Credential lease differs"):
            assess_guided_provider_pre_execution(
                context,
                bundle,
                preimages=preimages,
                evaluation_at=ASSESS_PRE_EXECUTION_AT,
            )

    def test_public_exports_and_module_have_no_execution_surface(self) -> None:
        exported = (
            "GUIDED_PROVIDER_PRE_EXECUTION_VERSION",
            "GuidedProviderCredentialBindingError",
            "GuidedProviderCredentialResolver",
            "GuidedProviderPreExecution",
            "GuidedProviderPreExecutionContext",
            "GuidedProviderPreExecutionReview",
            "GuidedProviderPreExecutionStatus",
            "GuidedProviderPreparedRequest",
            "assess_guided_provider_pre_execution",
            "compose_guided_provider_pre_execution_context",
            "prepare_guided_provider_pre_execution",
        )
        for name in exported:
            self.assertTrue(hasattr(trip_planner, name), name)

        source = inspect.getsource(pre_execution_module)
        tree = ast.parse(source)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        forbidden = {
            "requests",
            "httpx",
            "urllib.request",
            "socket",
            "subprocess",
            "pathlib",
            "os",
            "trip_planner.store",
            "trip_planner.evidence_store",
            "trip_planner.evidence_session",
        }
        self.assertTrue(forbidden.isdisjoint(imported))
        self.assertNotIn(".send(", source)
        self.assertNotIn("provider_calls_permitted_by_phase530\": True", source)

    def test_direct_construction_cannot_mint_context_or_bundle(self) -> None:
        with self.assertRaises(ValueError):
            GuidedProviderPreExecutionContext(
                _assessment_args=(),
                _response=None,  # type: ignore[arg-type]
                _materialization=None,  # type: ignore[arg-type]
                _send_preparation=None,  # type: ignore[arg-type]
                _limits=None,  # type: ignore[arg-type]
                _composed_at=COMPOSE_AT,
                _expires_at=COMPOSE_AT + timedelta(minutes=1),
            )
        with self.assertRaises(ValueError):
            GuidedProviderPreExecution(
                _requests=(),
                _credential_lease=None,  # type: ignore[arg-type]
                _claim=None,  # type: ignore[arg-type]
                _limits=None,  # type: ignore[arg-type]
                _prepared_at=COMPOSE_AT,
                _expires_at=COMPOSE_AT + timedelta(minutes=1),
            )


if __name__ == "__main__":
    unittest.main()
