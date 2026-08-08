"""Phase 5.31 bounded injected-transport execution and quarantine."""

from __future__ import annotations

import ast
import copy
import inspect
import json
import pickle
import traceback
import unittest
from dataclasses import asdict, replace
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import trip_planner
from tests.test_phase530_guided_provider_pre_execution import (
    ASSESS_PRE_EXECUTION_AT,
    COMPOSE_AT,
    PREPARE_FINISH_AT,
    PREPARE_START_AT,
    PRIVATE_QUERY,
    TEST_SECRET,
    _RecordingResolver,
    _prepare_default,
)
from trip_planner import guided_provider_pre_execution as pre_execution_module
from trip_planner.guided_provider_execution import (
    GUIDED_PROVIDER_EXECUTION_VERSION,
    GuidedProviderDeliveryCertainty,
    GuidedProviderExecutionLimits,
    GuidedProviderExecutionProblemCode,
    GuidedProviderExecutionStatus,
    GuidedProviderRequestExecutionOutcomeKind,
    GuidedProviderTransportError,
    GuidedProviderTransportResponse,
    GuidedProviderWireRequest,
    execute_guided_provider_requests,
)
from trip_planner.place_details import GooglePlaceDetailsRequest
from trip_planner.places_identity import PlaceIdentityRequest
from trip_planner.routes import GoogleRouteRequest
from trip_planner.lodging_discovery import LodgingDiscoveryRequest
from trip_planner.facts import (
    EvidenceLedger,
    EvidenceSnapshot,
    ProviderPolicyRegistry,
)


EXECUTION_START_AT = ASSESS_PRE_EXECUTION_AT + timedelta(milliseconds=1)
PRIVATE_RESPONSE = b'{"privateProviderValue":"do-not-disclose"}'


class _TickingClock:
    def __init__(self, start=EXECUTION_START_AT, *, step_ms: int = 5):
        self.current = start
        self.step = timedelta(milliseconds=step_ms)
        self.calls = 0

    def __call__(self):
        result = self.current
        self.current += self.step
        self.calls += 1
        return result


class _SequenceClock:
    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        if not self.values:
            raise AssertionError("trusted clock exhausted")
        return self.values.pop(0)


class _ScriptedTransport:
    def __init__(self, *actions: object):
        self.actions = list(actions)
        self.wires: list[GuidedProviderWireRequest] = []
        self.captures: list[dict[str, object]] = []

    def send(
        self,
        request,
        *,
        connect_timeout_s,
        read_timeout_s,
        deadline_at,
        max_response_bytes,
        max_response_headers,
        max_header_name_bytes,
        max_header_value_bytes,
    ):
        if not self.actions:
            raise AssertionError("transport script exhausted")
        self.wires.append(request)
        self.captures.append(
            {
                "request_index": request.request_index,
                "wire_id": id(request),
                "url": request.url,
                "headers": request.headers,
                "body": request.body,
                "connect_timeout_s": connect_timeout_s,
                "read_timeout_s": read_timeout_s,
                "deadline_at": deadline_at,
                "max_response_bytes": max_response_bytes,
                "max_response_headers": max_response_headers,
                "max_header_name_bytes": max_header_name_bytes,
                "max_header_value_bytes": max_header_value_bytes,
            }
        )
        action = self.actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return action(request)
        return action


def _response(
    status_code: int = 200,
    *,
    body: bytes = PRIVATE_RESPONSE,
) -> GuidedProviderTransportResponse:
    return GuidedProviderTransportResponse(
        status_code=status_code,
        body=body,
        headers=(("X-Provider-Trace", "private-trace-value"),),
    )


def _identity_snapshot(preimages):
    for preimage in preimages:
        if type(preimage.target) is GooglePlaceDetailsRequest:
            return preimage.target.snapshot
    raise AssertionError("default fixture lacks the identity policy snapshot")


def _limits(bundle, **overrides) -> GuidedProviderExecutionLimits:
    values = {
        "max_attempt_count": bundle._limits.accepted_max_request_count,
        "max_google_cost_usd_micros": (
            bundle._limits.accepted_max_google_cost_usd_micros
        ),
        "max_serpapi_credit_count": bundle._limits.serpapi_credit_cap,
        "connect_timeout_s": 0.01,
        "read_timeout_s": 0.01,
        "deadline_at": bundle._expires_at - timedelta(milliseconds=1),
    }
    values.update(overrides)
    return GuidedProviderExecutionLimits(**values)


def _execute(transport, *, limits=None, clock=None):
    context, bundle, preimages, _, _ = _prepare_default()
    result = execute_guided_provider_requests(
        context,
        bundle,
        transport,
        limits or _limits(bundle),
        preimages=preimages,
        identity_snapshot=_identity_snapshot(preimages),
        clock=clock or _TickingClock(),
    )
    return result, context, bundle, preimages


def _prepare_single_profile(topic, capability, preflight_item, target):
    from tests.test_phase520_guided_provider_request_materialization_review import (
        _prepared_single_capability_review,
    )
    from tests.test_phase525_guided_provider_request_send_preparation import (
        _accepted_response_from_materialization_review,
        _prepared_send_preparation,
    )
    from tests.test_phase526_guided_provider_request_credential_binding_review import (
        _prepared_credential_binding_review,
    )
    from tests.test_phase527_guided_provider_request_credential_binding_response import (
        _captured_credential_binding_response,
    )
    from tests.test_phase528_guided_provider_request_live_credential_binding_review import (
        PREPARE_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
        _availability_attestations,
    )
    from tests.test_phase529_guided_provider_request_live_credential_binding_response import (
        CAPTURE_LIVE_CREDENTIAL_BINDING_RESPONSE_AT,
    )
    from trip_planner.guided_provider_pre_execution import (
        compose_guided_provider_pre_execution_context,
        prepare_guided_provider_pre_execution,
    )
    from trip_planner.guided_provider_request_live_credential_binding_review import (
        prepare_guided_provider_request_live_credential_binding_review,
    )
    from trip_planner.guided_provider_request_live_credential_binding_response import (
        GuidedProviderRequestLiveCredentialBindingResponseKind,
        capture_guided_provider_request_live_credential_binding_response,
    )

    materialization_context, preimages = _prepared_single_capability_review(
        topic,
        capability,
        preflight_item,
        target,
    )
    response_context, preimages = _accepted_response_from_materialization_review(
        materialization_context,
        preimages,
    )
    preparation_context, preimages = _prepared_send_preparation(
        response_context=response_context,
        preimages=preimages,
    )
    credential_review_context, preimages = _prepared_credential_binding_review(
        preparation_context=preparation_context,
        preimages=preimages,
    )
    credential_response_context, preimages = _captured_credential_binding_response(
        context=credential_review_context,
        preimages=preimages,
    )
    attestations = _availability_attestations(credential_response_context)
    live_review = prepare_guided_provider_request_live_credential_binding_review(
        *credential_response_context,
        preimages=preimages,
        availability_attestations=attestations,
        evaluation_at=PREPARE_LIVE_CREDENTIAL_BINDING_REVIEW_AT,
    )
    live_context = (*credential_response_context, live_review)
    live_response = capture_guided_provider_request_live_credential_binding_response(
        *live_context,
        preimages=preimages,
        kind=(
            GuidedProviderRequestLiveCredentialBindingResponseKind
            .ACCEPT_LIVE_CREDENTIAL_BINDING
        ),
        evaluation_at=CAPTURE_LIVE_CREDENTIAL_BINDING_RESPONSE_AT,
    )
    response_context = (*live_context, live_response)
    context = compose_guided_provider_pre_execution_context(
        *response_context,
        preimages=preimages,
        evaluation_at=COMPOSE_AT,
    )
    resolver = _RecordingResolver()
    bundle = prepare_guided_provider_pre_execution(
        context,
        resolver,
        preimages=preimages,
        clock=_SequenceClock(PREPARE_START_AT, PREPARE_FINISH_AT),
    )
    return context, bundle, preimages, resolver


class GuidedProviderExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        with pre_execution_module._CONSENT_CLAIM_LOCK:
            pre_execution_module._CONSENT_CLAIMS.clear()

    def test_success_quarantines_exact_provider_specific_targets(self) -> None:
        transport = _ScriptedTransport(_response(), _response(204, body=b""))
        result, _, bundle, preimages = _execute(transport)

        self.assertEqual(GUIDED_PROVIDER_EXECUTION_VERSION, result.to_dict()["contract_version"])
        self.assertEqual(
            GuidedProviderExecutionStatus.ALL_RESPONSES_QUARANTINED,
            result.status,
        )
        self.assertEqual(
            "assess_provider_specific_quarantined_responses",
            result.next_action,
        )
        self.assertEqual(2, result.attempts_used)
        self.assertEqual(52_000, result.reserved_google_cost_usd_micros)
        self.assertEqual(0, result.reserved_serpapi_credit_count)
        self.assertEqual(2, result.quarantine_count)

        details = result._outcomes[0]._quarantine
        identity = result._outcomes[1]._quarantine
        self.assertIsNotNone(details)
        self.assertIsNotNone(identity)
        assert details is not None and identity is not None
        self.assertIsInstance(details._normalization_target, GooglePlaceDetailsRequest)
        self.assertIsInstance(identity._normalization_target, PlaceIdentityRequest)
        self.assertIs(
            _identity_snapshot(preimages),
            identity._normalization_target.snapshot,
        )
        self.assertIs(bundle._requests[0], details._prepared_request)
        self.assertIs(bundle._requests[1], identity._prepared_request)
        self.assertEqual(
            bundle._context_fingerprint,
            identity._bundle_context_fingerprint,
        )
        self.assertEqual(
            bundle._requests[1]._contract._source_binding_fingerprint,
            identity._source_binding_fingerprint,
        )

        first, second = transport.captures
        self.assertEqual(1_048_576, first["max_response_bytes"])
        self.assertEqual(32, first["max_response_headers"])
        self.assertEqual(256, first["max_header_name_bytes"])
        self.assertEqual(4096, first["max_header_value_bytes"])
        self.assertEqual(bundle._expires_at - timedelta(milliseconds=1), first["deadline_at"])
        self.assertIn("X-Goog-Api-Key", dict(first["headers"]))
        self.assertEqual(TEST_SECRET, dict(first["headers"])["X-Goog-Api-Key"])
        self.assertNotIn("ChIJ-place/private value", str(first["url"]))
        identity_body = json.loads(second["body"])
        self.assertEqual(5, identity_body["pageSize"])
        self.assertEqual(PRIVATE_QUERY, identity_body["textQuery"])

        safe = json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)
        for private in (
            TEST_SECRET,
            PRIVATE_QUERY,
            "ChIJ-place/private value",
            "do-not-disclose",
            "private-trace-value",
            bundle._context_fingerprint,
            bundle._requests[0]._request_fingerprint,
            identity._target_fingerprint,
        ):
            self.assertNotIn(private, safe)
            self.assertNotIn(private, repr(result))
        self.assertEqual("candidate", result.to_dict()["provider_execution"]["decision_state"])
        self.assertEqual("unverified", result.to_dict()["provider_execution"]["evidence_state"])
        for wire in transport.wires:
            self.assertFalse(hasattr(wire, "_source_request"))
            with self.assertRaises(RuntimeError):
                _ = wire.url
        with self.assertRaises(ValueError):
            bundle._credential_lease._value(
                bundle._requests[0].credential_slot,
                evaluation_at=EXECUTION_START_AT,
                _token=pre_execution_module._CREDENTIAL_ACCESS_TOKEN,
            )

    def test_unknown_retry_is_fair_same_object_and_conservatively_charged(self) -> None:
        transport = _ScriptedTransport(
            GuidedProviderTransportError(
                GuidedProviderDeliveryCertainty.OUTCOME_UNKNOWN
            ),
            _response(),
            _response(),
        )
        result, _, _, _ = _execute(transport)

        self.assertEqual([0, 1, 0], [item["request_index"] for item in transport.captures])
        self.assertEqual(
            transport.captures[0]["wire_id"],
            transport.captures[2]["wire_id"],
        )
        self.assertEqual(3, result.attempts_used)
        self.assertEqual(72_000, result.reserved_google_cost_usd_micros)
        first = result._outcomes[0]
        self.assertEqual(
            GuidedProviderRequestExecutionOutcomeKind.RESPONSE_QUARANTINED,
            first.kind,
        )
        self.assertEqual(2, first.attempts_used)
        self.assertEqual(1, first.retry_count)
        self.assertEqual(1, first.unknown_attempt_count)

    def test_second_unknown_is_terminal_and_never_rebuilt_or_retried_again(self) -> None:
        transport = _ScriptedTransport(
            GuidedProviderTransportError(
                GuidedProviderDeliveryCertainty.OUTCOME_UNKNOWN
            ),
            _response(),
            RuntimeError("transport-secret-that-must-not-surface"),
        )
        result, _, _, _ = _execute(transport)

        self.assertEqual([0, 1, 0], [item["request_index"] for item in transport.captures])
        self.assertEqual(GuidedProviderExecutionStatus.OUTCOME_UNKNOWN, result.status)
        first = result._outcomes[0]
        self.assertEqual(GuidedProviderRequestExecutionOutcomeKind.OUTCOME_UNKNOWN, first.kind)
        self.assertEqual(2, first.unknown_attempt_count)
        self.assertEqual(1, first.retry_count)
        self.assertEqual(
            GuidedProviderExecutionProblemCode.TRANSPORT_OUTCOME_UNKNOWN,
            first.problem_code,
        )
        self.assertNotIn(
            "transport-secret-that-must-not-surface",
            json.dumps(result.to_dict(), sort_keys=True),
        )

    def test_attempt_cap_still_gives_every_request_its_initial_chance(self) -> None:
        context, bundle, preimages, _, _ = _prepare_default()
        transport = _ScriptedTransport(
            GuidedProviderTransportError(
                GuidedProviderDeliveryCertainty.OUTCOME_UNKNOWN
            ),
            _response(),
        )
        result = execute_guided_provider_requests(
            context,
            bundle,
            transport,
            _limits(bundle, max_attempt_count=2),
            preimages=preimages,
            identity_snapshot=_identity_snapshot(preimages),
            clock=_TickingClock(),
        )

        self.assertEqual([0, 1], [item["request_index"] for item in transport.captures])
        self.assertEqual(2, result.attempts_used)
        self.assertEqual(
            GuidedProviderExecutionProblemCode.UNKNOWN_RETRY_NOT_AVAILABLE,
            result._outcomes[0].problem_code,
        )
        self.assertEqual(
            GuidedProviderRequestExecutionOutcomeKind.RESPONSE_QUARANTINED,
            result._outcomes[1].kind,
        )

    def test_known_not_sent_is_final_but_invalid_return_stays_unknown(self) -> None:
        transport = _ScriptedTransport(
            GuidedProviderTransportError(
                GuidedProviderDeliveryCertainty.KNOWN_NOT_SENT
            ),
            object(),
            object(),
        )
        result, _, _, _ = _execute(transport)

        self.assertEqual(3, result.attempts_used)
        self.assertEqual(GuidedProviderExecutionStatus.OUTCOME_UNKNOWN, result.status)
        self.assertEqual(
            GuidedProviderRequestExecutionOutcomeKind.KNOWN_NOT_SENT,
            result._outcomes[0].kind,
        )
        self.assertEqual(
            GuidedProviderRequestExecutionOutcomeKind.OUTCOME_UNKNOWN,
            result._outcomes[1].kind,
        )
        self.assertEqual(0, result._outcomes[0].retry_count)
        self.assertEqual(1, result._outcomes[1].retry_count)
        self.assertEqual(2, result._outcomes[1].unknown_attempt_count)

    def test_malformed_typed_transport_error_never_proves_known_not_sent(self) -> None:
        malformed = GuidedProviderTransportError(
            GuidedProviderDeliveryCertainty.KNOWN_NOT_SENT
        )
        malformed.certainty = object()
        transport = _ScriptedTransport(malformed, _response(), _response())
        result, _, _, _ = _execute(transport)

        first = result._outcomes[0]
        self.assertEqual(
            GuidedProviderRequestExecutionOutcomeKind.RESPONSE_QUARANTINED,
            first.kind,
        )
        self.assertEqual(1, first.unknown_attempt_count)
        self.assertEqual(1, first.retry_count)
        self.assertEqual([0, 1, 0], [item["request_index"] for item in transport.captures])

    def test_late_or_invalid_post_send_clock_is_terminal_manual_reconcile(self) -> None:
        context, bundle, preimages, _, _ = _prepare_default()
        late_clock = _SequenceClock(
            EXECUTION_START_AT,
            EXECUTION_START_AT + timedelta(milliseconds=1),
            bundle._expires_at,
            bundle._expires_at + timedelta(milliseconds=1),
        )
        result = execute_guided_provider_requests(
            context,
            bundle,
            _ScriptedTransport(_response()),
            _limits(bundle),
            preimages=preimages,
            identity_snapshot=_identity_snapshot(preimages),
            clock=late_clock,
        )
        self.assertEqual(GuidedProviderExecutionStatus.OUTCOME_UNKNOWN, result.status)
        self.assertEqual(
            GuidedProviderRequestExecutionOutcomeKind.OUTCOME_UNKNOWN,
            result._outcomes[0].kind,
        )
        self.assertEqual(
            GuidedProviderExecutionProblemCode.DEADLINE_EXCEEDED,
            result._outcomes[0].problem_code,
        )
        self.assertIsNone(result._outcomes[0]._quarantine)
        self.assertEqual(1, result.attempts_used)

        with pre_execution_module._CONSENT_CLAIM_LOCK:
            pre_execution_module._CONSENT_CLAIMS.clear()
        context, bundle, preimages, _, _ = _prepare_default()
        invalid_clock = _SequenceClock(
            EXECUTION_START_AT,
            EXECUTION_START_AT + timedelta(milliseconds=1),
            "not-a-datetime",
            EXECUTION_START_AT + timedelta(milliseconds=2),
            EXECUTION_START_AT + timedelta(milliseconds=3),
        )
        result = execute_guided_provider_requests(
            context,
            bundle,
            _ScriptedTransport(_response(), _response()),
            _limits(bundle),
            preimages=preimages,
            identity_snapshot=_identity_snapshot(preimages),
            clock=invalid_clock,
        )
        self.assertEqual(GuidedProviderExecutionStatus.OUTCOME_UNKNOWN, result.status)
        self.assertEqual(
            GuidedProviderExecutionProblemCode.TRUSTED_CLOCK_INVALID,
            result._outcomes[0].problem_code,
        )
        self.assertEqual(
            GuidedProviderRequestExecutionOutcomeKind.RESPONSE_QUARANTINED,
            result._outcomes[1].kind,
        )

    def test_unknown_then_untrusted_retry_response_preserves_unknown_state(self) -> None:
        context, bundle, preimages, _, _ = _prepare_default()
        transport = _ScriptedTransport(
            GuidedProviderTransportError(
                GuidedProviderDeliveryCertainty.OUTCOME_UNKNOWN
            ),
            _response(),
            _response(),
        )
        clock = _SequenceClock(
            EXECUTION_START_AT,
            EXECUTION_START_AT + timedelta(milliseconds=1),
            EXECUTION_START_AT + timedelta(milliseconds=2),
            EXECUTION_START_AT + timedelta(milliseconds=3),
            EXECUTION_START_AT + timedelta(milliseconds=4),
            EXECUTION_START_AT + timedelta(milliseconds=5),
            "invalid-after-retry-send",
        )
        result = execute_guided_provider_requests(
            context,
            bundle,
            transport,
            _limits(bundle),
            preimages=preimages,
            identity_snapshot=_identity_snapshot(preimages),
            clock=clock,
        )
        first = result._outcomes[0]
        self.assertEqual(GuidedProviderExecutionStatus.OUTCOME_UNKNOWN, result.status)
        self.assertEqual(GuidedProviderRequestExecutionOutcomeKind.OUTCOME_UNKNOWN, first.kind)
        self.assertEqual(2, first.unknown_attempt_count)
        self.assertEqual(1, first.retry_count)
        self.assertEqual(
            GuidedProviderExecutionProblemCode.TRUSTED_CLOCK_INVALID,
            first.problem_code,
        )
        self.assertIsNone(first._quarantine)

    def test_mutated_oversize_response_is_revalidated_before_quarantine(self) -> None:
        oversized = _response()
        object.__setattr__(oversized, "_body", b"x" * 1_048_577)
        transport = _ScriptedTransport(oversized, _response(), _response())
        result, _, _, _ = _execute(transport)

        self.assertEqual(3, result.attempts_used)
        self.assertEqual(
            GuidedProviderRequestExecutionOutcomeKind.RESPONSE_QUARANTINED,
            result._outcomes[0].kind,
        )
        self.assertEqual(1, result._outcomes[0].unknown_attempt_count)
        self.assertEqual(1, result._outcomes[0].retry_count)
        quarantine = result._outcomes[0]._quarantine
        assert quarantine is not None
        self.assertLessEqual(len(quarantine._body), 1_048_576)

    def test_identity_snapshot_policy_and_purge_check_bind_before_claim(self) -> None:
        context, bundle, preimages, _, _ = _prepare_default()
        basis = _identity_snapshot(preimages)
        wrong_registry = ProviderPolicyRegistry(
            policies=tuple(
                replace(
                    policy,
                    contract_region="google-maps-eea-test",
                    policy_digest="",
                )
                for policy in basis.policies.policies
            )
        )
        wrong_snapshot = EvidenceSnapshot.from_ledger(
            EvidenceLedger(wrong_registry),
            evaluation_at=EXECUTION_START_AT,
            purge_now=EXECUTION_START_AT,
        )
        transport = _ScriptedTransport(_response(), _response())
        with self.assertRaisesRegex(ValueError, "accepted profile"):
            execute_guided_provider_requests(
                context,
                bundle,
                transport,
                _limits(bundle),
                preimages=preimages,
                identity_snapshot=wrong_snapshot,
                clock=_TickingClock(),
            )
        self.assertFalse(bundle._claim.claimed)
        self.assertEqual([], transport.captures)

        future_purge_snapshot = EvidenceSnapshot.from_ledger(
            EvidenceLedger(basis.policies),
            evaluation_at=EXECUTION_START_AT,
            purge_now=EXECUTION_START_AT + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(ValueError, "purge check"):
            execute_guided_provider_requests(
                context,
                bundle,
                transport,
                _limits(bundle),
                preimages=preimages,
                identity_snapshot=future_purge_snapshot,
                clock=_TickingClock(),
            )
        self.assertFalse(bundle._claim.claimed)
        self.assertEqual([], transport.captures)

    def test_target_drift_or_endpoint_expiry_cannot_cross_a_later_send(self) -> None:
        context, bundle, preimages, _, _ = _prepare_default()
        details_target = next(
            item.target
            for item in preimages
            if type(item.target) is GooglePlaceDetailsRequest
        )
        original_region = details_target.region_code

        def mutate_target(_request):
            object.__setattr__(details_target, "region_code", "JP")
            return _response()

        try:
            result = execute_guided_provider_requests(
                context,
                bundle,
                _ScriptedTransport(mutate_target, _response()),
                _limits(bundle),
                preimages=preimages,
                identity_snapshot=_identity_snapshot(preimages),
                clock=_TickingClock(),
            )
        finally:
            object.__setattr__(details_target, "region_code", original_region)
        first = result._outcomes[0]
        self.assertEqual(GuidedProviderRequestExecutionOutcomeKind.OUTCOME_UNKNOWN, first.kind)
        self.assertEqual(
            GuidedProviderExecutionProblemCode.NORMALIZATION_TARGET_DRIFT,
            first.problem_code,
        )
        self.assertIsNone(first._quarantine)

        with pre_execution_module._CONSENT_CLAIM_LOCK:
            pre_execution_module._CONSENT_CLAIMS.clear()
        context, bundle, preimages, _, _ = _prepare_default()
        details_target = next(
            item.target
            for item in preimages
            if type(item.target) is GooglePlaceDetailsRequest
        )
        transport = _ScriptedTransport(
            GuidedProviderTransportError(
                GuidedProviderDeliveryCertainty.OUTCOME_UNKNOWN
            ),
            _response(),
        )
        clock = _SequenceClock(
            EXECUTION_START_AT,
            EXECUTION_START_AT + timedelta(milliseconds=1),
            EXECUTION_START_AT + timedelta(milliseconds=2),
            EXECUTION_START_AT + timedelta(milliseconds=3),
            EXECUTION_START_AT + timedelta(milliseconds=4),
            details_target.endpoint.valid_until,
        )
        result = execute_guided_provider_requests(
            context,
            bundle,
            transport,
            _limits(bundle),
            preimages=preimages,
            identity_snapshot=_identity_snapshot(preimages),
            clock=clock,
        )
        first = result._outcomes[0]
        self.assertEqual([0, 1], [item["request_index"] for item in transport.captures])
        self.assertEqual(GuidedProviderRequestExecutionOutcomeKind.OUTCOME_UNKNOWN, first.kind)
        self.assertEqual(
            GuidedProviderExecutionProblemCode.NORMALIZATION_TARGET_STALE,
            first.problem_code,
        )
        self.assertEqual(1, first.attempts_used)
        self.assertEqual(0, first.retry_count)

    def test_nested_snapshot_endpoint_and_policy_drift_fail_closed(self) -> None:
        context, bundle, preimages, _, _ = _prepare_default()
        details_target = next(
            item.target
            for item in preimages
            if type(item.target) is GooglePlaceDetailsRequest
        )
        basis = details_target.snapshot
        identity_snapshot = EvidenceSnapshot.from_ledger(
            EvidenceLedger(basis.policies),
            evaluation_at=EXECUTION_START_AT,
            purge_now=EXECUTION_START_AT,
        )
        original_purge = basis.purge_checked_at

        def mutate_nested_snapshot(_request):
            object.__setattr__(
                basis,
                "purge_checked_at",
                original_purge + timedelta(seconds=1),
            )
            return _response()

        try:
            transport = _ScriptedTransport(
                mutate_nested_snapshot,
                _response(),
            )
            result = execute_guided_provider_requests(
                context,
                bundle,
                transport,
                _limits(bundle),
                preimages=preimages,
                identity_snapshot=identity_snapshot,
                clock=_TickingClock(),
            )
        finally:
            object.__setattr__(basis, "purge_checked_at", original_purge)
        self.assertEqual([0, 1], [item["request_index"] for item in transport.captures])
        self.assertEqual(
            GuidedProviderRequestExecutionOutcomeKind.OUTCOME_UNKNOWN,
            result._outcomes[0].kind,
        )
        self.assertEqual(
            GuidedProviderExecutionProblemCode.NORMALIZATION_TARGET_DRIFT,
            result._outcomes[0].problem_code,
        )
        self.assertIsNone(result._outcomes[0]._quarantine)
        self.assertIsNotNone(result._outcomes[1]._quarantine)

        with pre_execution_module._CONSENT_CLAIM_LOCK:
            pre_execution_module._CONSENT_CLAIMS.clear()
        context, bundle, preimages, _, _ = _prepare_default()
        details_target = next(
            item.target
            for item in preimages
            if type(item.target) is GooglePlaceDetailsRequest
        )
        endpoint = details_target.endpoint
        original_place_id = endpoint.provider_place_id

        def mutate_private_endpoint(_request):
            object.__setattr__(endpoint, "provider_place_id", "evil-place-id")
            return _response()

        try:
            result = execute_guided_provider_requests(
                context,
                bundle,
                _ScriptedTransport(mutate_private_endpoint, _response()),
                _limits(bundle),
                preimages=preimages,
                identity_snapshot=_identity_snapshot(preimages),
                clock=_TickingClock(),
            )
        finally:
            object.__setattr__(endpoint, "provider_place_id", original_place_id)
        self.assertEqual(
            GuidedProviderExecutionProblemCode.NORMALIZATION_TARGET_DRIFT,
            result._outcomes[0].problem_code,
        )
        self.assertIsNone(result._outcomes[0]._quarantine)

        with pre_execution_module._CONSENT_CLAIM_LOCK:
            pre_execution_module._CONSENT_CLAIMS.clear()
        context, bundle, preimages, _, _ = _prepare_default()
        details_target = next(
            item.target
            for item in preimages
            if type(item.target) is GooglePlaceDetailsRequest
        )
        policy = details_target.snapshot.policies.policy(
            details_target.provider_request.policy_id
        )
        original_region = policy.contract_region

        def mutate_selected_policy(_request):
            object.__setattr__(policy, "contract_region", "evil-region")
            return _response()

        try:
            transport = _ScriptedTransport(mutate_selected_policy)
            result = execute_guided_provider_requests(
                context,
                bundle,
                transport,
                _limits(bundle),
                preimages=preimages,
                identity_snapshot=_identity_snapshot(preimages),
                clock=_TickingClock(),
            )
        finally:
            object.__setattr__(policy, "contract_region", original_region)
        self.assertEqual([0], [item["request_index"] for item in transport.captures])
        self.assertEqual(0, result.quarantine_count)
        self.assertEqual(
            GuidedProviderExecutionProblemCode.NORMALIZATION_TARGET_DRIFT,
            result._outcomes[0].problem_code,
        )
        self.assertEqual(
            GuidedProviderExecutionProblemCode.NORMALIZATION_TARGET_DRIFT,
            result._outcomes[1].problem_code,
        )

    def test_prepared_request_and_caller_limits_cannot_drift_budget_or_type(self) -> None:
        context, bundle, preimages, _, _ = _prepare_default()
        later = bundle._requests[1]
        original_cost = later.estimated_google_cost_usd_micros
        original_kind = later.materialization_kind

        def mutate_later_request(_request):
            object.__setattr__(later, "estimated_google_cost_usd_micros", 0)
            object.__setattr__(
                later,
                "materialization_kind",
                type(original_kind).SERPAPI_GOOGLE_HOTELS,
            )
            return _response()

        try:
            transport = _ScriptedTransport(mutate_later_request)
            result = execute_guided_provider_requests(
                context,
                bundle,
                transport,
                _limits(bundle),
                preimages=preimages,
                identity_snapshot=_identity_snapshot(preimages),
                clock=_TickingClock(),
            )
        finally:
            object.__setattr__(
                later,
                "estimated_google_cost_usd_micros",
                original_cost,
            )
            object.__setattr__(later, "materialization_kind", original_kind)
        self.assertEqual([0], [item["request_index"] for item in transport.captures])
        self.assertEqual(1, result.attempts_used)
        self.assertEqual(20_000, result.reserved_google_cost_usd_micros)
        self.assertEqual(
            GuidedProviderExecutionProblemCode.PREPARED_REQUEST_DRIFT,
            result._outcomes[1].problem_code,
        )
        self.assertIsNone(result._outcomes[1]._quarantine)

        with pre_execution_module._CONSENT_CLAIM_LOCK:
            pre_execution_module._CONSENT_CLAIMS.clear()
        context, bundle, preimages, _, _ = _prepare_default()
        limits = _limits(bundle, max_attempt_count=2)
        original_deadline = limits.deadline_at

        def expand_caller_limits(_request):
            object.__setattr__(limits, "max_attempt_count", 3)
            object.__setattr__(limits, "connect_timeout_s", 60.0)
            object.__setattr__(
                limits,
                "deadline_at",
                original_deadline + timedelta(days=1),
            )
            raise GuidedProviderTransportError(
                GuidedProviderDeliveryCertainty.OUTCOME_UNKNOWN
            )

        transport = _ScriptedTransport(expand_caller_limits, _response())
        result = execute_guided_provider_requests(
            context,
            bundle,
            transport,
            limits,
            preimages=preimages,
            identity_snapshot=_identity_snapshot(preimages),
            clock=_TickingClock(),
        )
        self.assertEqual([0, 1], [item["request_index"] for item in transport.captures])
        self.assertEqual(2, result.attempts_used)
        self.assertEqual(0.01, transport.captures[1]["connect_timeout_s"])
        self.assertEqual(original_deadline, transport.captures[1]["deadline_at"])
        self.assertEqual(
            GuidedProviderExecutionProblemCode.UNKNOWN_RETRY_NOT_AVAILABLE,
            result._outcomes[0].problem_code,
        )

    def test_bundle_lease_replacement_cannot_bypass_original_lease_cleanup(self) -> None:
        context, bundle, preimages, _, _ = _prepare_default()
        original_lease = bundle._credential_lease

        def replace_bundle_lease(_request):
            object.__setattr__(bundle, "_credential_lease", object())
            return _response()

        transport = _ScriptedTransport(replace_bundle_lease, _response())
        try:
            result = execute_guided_provider_requests(
                context,
                bundle,
                transport,
                _limits(bundle),
                preimages=preimages,
                identity_snapshot=_identity_snapshot(preimages),
                clock=_TickingClock(),
            )
        finally:
            object.__setattr__(bundle, "_credential_lease", original_lease)
        self.assertEqual(2, result.quarantine_count)
        self.assertEqual((), original_lease.slots)
        for wire in transport.wires:
            with self.assertRaises(RuntimeError):
                _ = wire.url

    def test_invalid_limits_or_identity_binding_fail_before_claim_and_send(self) -> None:
        context, bundle, preimages, _, _ = _prepare_default()
        transport = _ScriptedTransport(_response(), _response())
        with self.assertRaises(ValueError):
            execute_guided_provider_requests(
                context,
                bundle,
                transport,
                _limits(bundle, max_google_cost_usd_micros=51_999),
                preimages=preimages,
                identity_snapshot=_identity_snapshot(preimages),
                clock=_TickingClock(),
            )
        self.assertEqual([], transport.captures)
        self.assertFalse(bundle._claim.claimed)

        with self.assertRaises(ValueError):
            execute_guided_provider_requests(
                context,
                bundle,
                transport,
                _limits(bundle),
                preimages=preimages,
                identity_snapshot=None,
                clock=_TickingClock(),
            )
        self.assertEqual([], transport.captures)
        self.assertFalse(bundle._claim.claimed)

        result = execute_guided_provider_requests(
            context,
            bundle,
            transport,
            _limits(bundle),
            preimages=preimages,
            identity_snapshot=_identity_snapshot(preimages),
            clock=_TickingClock(),
        )
        self.assertEqual(2, result.quarantine_count)

    def test_bundle_replay_is_rejected_before_a_second_transport_call(self) -> None:
        transport = _ScriptedTransport(_response(), _response())
        result, context, bundle, preimages = _execute(transport)
        self.assertEqual(2, result.attempts_used)
        with self.assertRaises(ValueError):
            execute_guided_provider_requests(
                context,
                bundle,
                transport,
                _limits(bundle),
                preimages=preimages,
                identity_snapshot=_identity_snapshot(preimages),
                clock=_TickingClock(),
            )
        self.assertEqual(2, len(transport.captures))

    def test_partial_materialization_and_exception_frames_clear_credentials(self) -> None:
        context, bundle, preimages, _, _ = _prepare_default()
        created: list[GuidedProviderWireRequest] = []
        from trip_planner import guided_provider_execution as module

        original = module._materialize_wire_request

        def fail_second(request, *, request_index, evaluation_at):
            if request_index == 1:
                raise RuntimeError("forced-materialization-stop")
            wire = original(
                request,
                request_index=request_index,
                evaluation_at=evaluation_at,
            )
            created.append(wire)
            return wire

        with patch.object(module, "_materialize_wire_request", fail_second):
            with self.assertRaisesRegex(RuntimeError, "forced-materialization-stop"):
                execute_guided_provider_requests(
                    context,
                    bundle,
                    _ScriptedTransport(),
                    _limits(bundle),
                    preimages=preimages,
                    identity_snapshot=_identity_snapshot(preimages),
                    clock=_TickingClock(),
                )
        self.assertEqual(1, len(created))
        with self.assertRaises(RuntimeError):
            _ = created[0].url

        with pre_execution_module._CONSENT_CLAIM_LOCK:
            pre_execution_module._CONSENT_CLAIMS.clear()
        context, bundle, preimages, _, _ = _prepare_default()
        caught = None
        with patch.object(
            GuidedProviderWireRequest,
            "__init__",
            side_effect=RuntimeError("forced-wire-construction-stop"),
        ):
            try:
                execute_guided_provider_requests(
                    context,
                    bundle,
                    _ScriptedTransport(),
                    _limits(bundle),
                    preimages=preimages,
                    identity_snapshot=_identity_snapshot(preimages),
                    clock=_TickingClock(),
                )
            except RuntimeError as exc:
                caught = exc
        self.assertIsNotNone(caught)
        materialize_frames = [
            frame
            for frame, _ in traceback.walk_tb(caught.__traceback__)
            if frame.f_code.co_name == "_materialize_wire_request"
        ]
        self.assertEqual(1, len(materialize_frames))
        frame_values = materialize_frames[0].f_locals
        self.assertIsNone(frame_values["credential"])
        self.assertEqual([], frame_values["headers"])
        self.assertEqual([], frame_values["query"])
        self.assertEqual((), frame_values["wire_headers"])
        self.assertIsNone(frame_values["url"])
        self.assertIsNone(frame_values["wire"])
        self.assertNotIn(TEST_SECRET, repr(frame_values))

    def test_every_post_claim_baseexception_and_wire_clear_failure_scrubs_lease(self) -> None:
        from trip_planner import guided_provider_execution as module

        context, bundle, preimages, _, _ = _prepare_default()
        with patch.object(
            module,
            "_RunClock",
            side_effect=KeyboardInterrupt("forced-post-claim-stop"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                execute_guided_provider_requests(
                    context,
                    bundle,
                    _ScriptedTransport(),
                    _limits(bundle),
                    preimages=preimages,
                    identity_snapshot=_identity_snapshot(preimages),
                    clock=_TickingClock(),
                )
        self.assertTrue(bundle._claim.claimed)
        self.assertEqual((), bundle._credential_lease.slots)

        with pre_execution_module._CONSENT_CLAIM_LOCK:
            pre_execution_module._CONSENT_CLAIMS.clear()
        context, bundle, preimages, _, _ = _prepare_default()
        transport = _ScriptedTransport(_response(), _response())
        with patch.object(
            GuidedProviderWireRequest,
            "_clear",
            side_effect=RuntimeError("forced-wire-clear-stop"),
        ):
            with self.assertRaisesRegex(RuntimeError, "wire cleanup"):
                execute_guided_provider_requests(
                    context,
                    bundle,
                    transport,
                    _limits(bundle),
                    preimages=preimages,
                    identity_snapshot=_identity_snapshot(preimages),
                    clock=_TickingClock(),
                )
        self.assertEqual((), bundle._credential_lease.slots)
        self.assertEqual(2, len(transport.wires))
        for wire in transport.wires:
            with self.assertRaises(RuntimeError):
                _ = wire.url

    def test_routes_and_hotel_profiles_reach_exact_private_quarantine(self) -> None:
        from tests.test_phase513_guided_provider_preflight import _google_item
        from tests.test_phase516_guided_provider_execution_target_bindings import (
            _hotel_request,
            _route_request,
            _serpapi_item,
        )
        from trip_planner.guided_evidence_plan import GuidedEvidenceTopic
        from trip_planner.guided_provider_scope import GuidedProviderCapability

        cases = (
            (
                GuidedEvidenceTopic.ROUTE,
                GuidedProviderCapability.GOOGLE_ROUTES,
                _google_item(
                    GuidedEvidenceTopic.ROUTE,
                    GuidedProviderCapability.GOOGLE_ROUTES,
                    1,
                ),
                _route_request(),
                GoogleRouteRequest,
            ),
            (
                GuidedEvidenceTopic.LODGING,
                GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
                _serpapi_item(GuidedEvidenceTopic.LODGING),
                _hotel_request(),
                LodgingDiscoveryRequest,
            ),
        )
        for topic, capability, item, target, target_type in cases:
            with self.subTest(topic=topic.value):
                with pre_execution_module._CONSENT_CLAIM_LOCK:
                    pre_execution_module._CONSENT_CLAIMS.clear()
                context, bundle, preimages, resolver = _prepare_single_profile(
                    topic,
                    capability,
                    item,
                    target,
                )
                transport = _ScriptedTransport(_response())
                result = execute_guided_provider_requests(
                    context,
                    bundle,
                    transport,
                    _limits(bundle),
                    preimages=preimages,
                    identity_snapshot=None,
                    clock=_TickingClock(),
                )
                self.assertEqual(1, result.quarantine_count)
                quarantine = result._outcomes[0]._quarantine
                assert quarantine is not None
                self.assertIsInstance(quarantine._normalization_target, target_type)
                self.assertEqual(
                    bundle._requests[0]._contract._source_binding_fingerprint,
                    quarantine._source_binding_fingerprint,
                )
                self.assertEqual(1, len(resolver.calls))
                captured = transport.captures[0]
                if target_type is GoogleRouteRequest:
                    body = json.loads(captured["body"])
                    self.assertEqual("DRIVE", body["travelMode"])
                    self.assertFalse(body["computeAlternativeRoutes"])
                    self.assertEqual(TEST_SECRET, dict(captured["headers"])["X-Goog-Api-Key"])
                    self.assertEqual(5_000, result.reserved_google_cost_usd_micros)
                else:
                    query = parse_qs(urlsplit(captured["url"]).query)
                    self.assertEqual(["google_hotels"], query["engine"])
                    self.assertEqual([TEST_SECRET], query["api_key"])
                    self.assertEqual([target.query], query["q"])
                    self.assertEqual(1, result.reserved_serpapi_credit_count)
                safe = json.dumps(result.to_dict(), ensure_ascii=False)
                self.assertNotIn(TEST_SECRET, safe)
                self.assertNotIn(getattr(target, "query", "absent-private-query"), safe)

    def test_private_hotel_query_drift_cannot_reuse_a_stale_request_id(self) -> None:
        from tests.test_phase516_guided_provider_execution_target_bindings import (
            _hotel_request,
            _serpapi_item,
        )
        from trip_planner.guided_evidence_plan import GuidedEvidenceTopic
        from trip_planner.guided_provider_scope import GuidedProviderCapability

        target = _hotel_request()
        context, bundle, preimages, _ = _prepare_single_profile(
            GuidedEvidenceTopic.LODGING,
            GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
            _serpapi_item(GuidedEvidenceTopic.LODGING),
            target,
        )
        original_query = target.query

        def mutate_private_query(_request):
            object.__setattr__(target, "query", f"{original_query} drift")
            return _response()

        try:
            transport = _ScriptedTransport(mutate_private_query)
            result = execute_guided_provider_requests(
                context,
                bundle,
                transport,
                _limits(bundle),
                preimages=preimages,
                identity_snapshot=None,
                clock=_TickingClock(),
            )
        finally:
            object.__setattr__(target, "query", original_query)
        self.assertEqual(1, result.attempts_used)
        self.assertEqual(0, result.quarantine_count)
        self.assertEqual(
            GuidedProviderRequestExecutionOutcomeKind.OUTCOME_UNKNOWN,
            result._outcomes[0].kind,
        )
        self.assertEqual(
            GuidedProviderExecutionProblemCode.NORMALIZATION_TARGET_DRIFT,
            result._outcomes[0].problem_code,
        )

    def test_runtime_objects_are_sealed_nonserializable_and_redacted(self) -> None:
        response = _response()
        with self.assertRaises(AttributeError):
            response._body = b"changed"
        for serializer in (copy.copy, copy.deepcopy, pickle.dumps):
            with self.assertRaises(TypeError):
                serializer(response)

        transport = _ScriptedTransport(response, _response())
        result, _, _, _ = _execute(transport)
        quarantine = result._outcomes[0]._quarantine
        assert quarantine is not None
        with self.assertRaises(AttributeError):
            quarantine._target_fingerprint = "0" * 64
        with self.assertRaises(AttributeError):
            result._outcomes = ()
        self.assertFalse(hasattr(result, "_token"))
        for value in (quarantine, result):
            for serializer in (copy.copy, copy.deepcopy, pickle.dumps):
                with self.assertRaises(TypeError):
                    serializer(value)
        with self.assertRaises(TypeError):
            asdict(result._outcomes[0])
        for value in (PRIVATE_RESPONSE.decode(), "private-trace-value", TEST_SECRET):
            self.assertNotIn(value, repr(response))
            self.assertNotIn(value, repr(quarantine))
            self.assertNotIn(value, repr(result))

    def test_module_has_no_live_client_or_evidence_canonical_promotion(self) -> None:
        import trip_planner.guided_provider_execution as module

        for name in module.__all__:
            self.assertTrue(hasattr(trip_planner, name), name)
        source = inspect.getsource(module)
        tree = ast.parse(source)
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertFalse(
            imports
            & {
                "requests",
                "httpx",
                "urllib.request",
                "http.client",
                "socket",
            }
        )
        self.assertNotIn("_source_request", source)
        called_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertFalse(
            called_names
            & {
                "EvidenceStore",
                "EvidenceSession",
                "TripStore",
                "PlanPatch",
                "ProviderResult",
                "AuthorizedProviderResult",
                "normalize_serpapi_hotel_discovery",
            }
        )
        with self.assertRaises(ValueError):
            GuidedProviderWireRequest(
                request_index=0,
                transport_profile=None,
                http_method=None,
                url="https://example.invalid",
                headers=(),
                body=None,
            )


if __name__ == "__main__":
    unittest.main()
