"""Offline contract tests for the Phase 4.3 Google Routes adapter."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import trip_planner.facts as facts_module
from trip_planner.evidence_session import EvidenceSession
from trip_planner.evidence_store import EvidenceStoreResult
from trip_planner.codec import build_plan
from trip_planner.composition import compose_trip_state
from trip_planner.facts import (
    GOOGLE_MAPS_NON_EEA_POLICY_PROFILE,
    AuthorizedProviderResult,
    EvidenceLedger,
    EvidenceSnapshot,
    FactContractError,
    FactKey,
    FactKind,
    FactObservation,
    FactValue,
    ProviderProblemCode,
    ProviderProvenance,
    ProviderRequest,
    ProviderResultStatus,
    google_maps_policy_registry,
)
from trip_planner.places_identity import (
    PlaceEndpointIdentity,
    _PLACE_ENDPOINT_TOKEN,
    extract_fresh_google_place_endpoint,
)
from trip_planner.routes import (
    GOOGLE_ROUTES_COMPUTE_URL,
    GOOGLE_ROUTES_FIELD_MASK,
    GoogleRouteBatchExecution,
    GoogleRouteRequest,
    GoogleRoutesHttpResponse,
    GoogleRoutesTransportError,
    GoogleRoutesTransportErrorKind,
    RouteAttemptBudget,
    RouteMode,
    TransitFallbackPolicy,
    build_google_route_request,
    build_google_routes_http_request,
    execute_google_route,
    execute_google_route_batch,
    parse_protobuf_duration_seconds,
)
from trip_planner.timeline import evaluate_timeline


UTC = timezone.utc
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
DEPARTURE = "2026-07-29T01:30:00+09:00"
ORIGIN_PLACE_ID = "ChIJ-origin-private-sentinel"
DESTINATION_PLACE_ID = "ChIJ-destination-private-sentinel"


def _endpoint(
    snapshot: EvidenceSnapshot,
    *,
    location_id: str,
    place_id: str,
    observation_seed: str,
    valid_until: datetime = NOW + timedelta(days=30),
) -> PlaceEndpointIdentity:
    value = FactValue.from_payload(
        FactKind.PLACE_IDENTITY,
        {"provider_place_id": place_id},
    )
    observation_id = (observation_seed * 64)[:64]
    return PlaceEndpointIdentity(
        location_id=location_id,
        provider_id="google-places",
        provider_place_id=place_id,
        observation_id=observation_id,
        value_digest=value.value_digest,
        valid_until=valid_until,
        snapshot_id=snapshot.snapshot_id,
        _token=_PLACE_ENDPOINT_TOKEN,
    )


def _success_response(
    *,
    duration: str = "630.5s",
    distance_m: int | None = 12_345,
    static_duration: str | None = "600s",
    warnings: list[str] | None = None,
    fallback_info: dict[str, str] | None = None,
) -> GoogleRoutesHttpResponse:
    route: dict[str, object] = {"duration": duration}
    if distance_m is not None:
        route["distanceMeters"] = distance_m
    if static_duration is not None:
        route["staticDuration"] = static_duration
    if warnings is not None:
        route["warnings"] = warnings
    body: dict[str, object] = {"routes": [route]}
    if fallback_info is not None:
        body["fallbackInfo"] = fallback_info
    return GoogleRoutesHttpResponse(
        status_code=200,
        body=json.dumps(body, separators=(",", ":")).encode(),
    )


class CannedTransport:
    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[object, float, float]] = []

    def send(
        self,
        request,
        *,
        connect_timeout_s: float,
        read_timeout_s: float,
    ) -> GoogleRoutesHttpResponse:
        self.calls.append(
            (request, connect_timeout_s, read_timeout_s)
        )
        if not self.outcomes:
            raise AssertionError("unexpected transport call")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if not isinstance(outcome, GoogleRoutesHttpResponse):
            raise AssertionError("canned outcome must be a response")
        return outcome


class MutableEvidenceSource:
    def __init__(self, result: EvidenceStoreResult) -> None:
        self.result = result

    def load(self) -> EvidenceStoreResult:
        return self.result


def _identity_observation(
    *,
    policies,
    location_id: str,
    place_id: str,
) -> FactObservation:
    key = FactKey(
        kind=FactKind.PLACE_IDENTITY,
        subject_ids=(location_id,),
        qualifiers=(("identity_provider", "google-places"),),
    )
    policy = policies.policy("google-place-id-v1")
    request = ProviderRequest(
        provider_id="google-places",
        adapter_id="google-places",
        adapter_version="v1",
        operation="resolve-place",
        fact_keys=(key,),
        policy_id=policy.policy_id,
        policy_digest=policy.policy_digest,
    )
    return FactObservation(
        key=key,
        value=FactValue.from_payload(
            FactKind.PLACE_IDENTITY,
            {"provider_place_id": place_id},
        ),
        provenance=ProviderProvenance(
            provider_id="google-places",
            adapter_id="google-places",
            adapter_version="v1",
            request_fingerprint=request.request_fingerprint,
            retention_policy_id=policy.policy_id,
            provider_record_id=place_id,
            attributions=(("Google Maps", None),),
        ),
        retrieved_at=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(days=365),
        purge_at=None,
        confidence=1.0,
    )


def _durable_source(*, revision: str = "c" * 64):
    policies = google_maps_policy_registry(
        GOOGLE_MAPS_NON_EEA_POLICY_PROFILE
    )
    observations = (
        _identity_observation(
            policies=policies,
            location_id="location-origin",
            place_id=ORIGIN_PLACE_ID,
        ),
        _identity_observation(
            policies=policies,
            location_id="location-destination",
            place_id=DESTINATION_PLACE_ID,
        ),
    )
    ledger = EvidenceLedger(
        policies,
        observations,
        generation=1,
        _token=facts_module._LEDGER_TOKEN,
    )
    result = EvidenceStoreResult(
        success=True,
        status="loaded",
        action="load",
        ledger=ledger,
        current_revision=revision,
        generation=ledger.generation,
        purge_checked_at=NOW,
    )
    return MutableEvidenceSource(result)


def _fallback_plan() -> dict[str, object]:
    return build_plan(
        trip_id="routes-integration-trip",
        generation=1,
        state={
            "trip": {
                "slug": "routes-integration-trip",
                "title": "Routes integration fixture",
                "timezone": "Asia/Taipei",
                "date_range": "2026-07-29 ~ 2026-07-29",
                "cities": ["Fixture City"],
            },
            "itinerary": {
                "available_modes": ["driving", "transit"],
                "days": [
                    {
                        "day_id": "day-1",
                        "day": 1,
                        "date": "2026-07-29",
                        "timezone": "Asia/Taipei",
                        "available_start": "00:00",
                        "available_end": "05:00",
                        "start_location_id": "location-origin",
                        "end_location_id": "location-destination",
                        "places": [
                            {
                                "activity_id": "activity-origin",
                                "title": "Origin",
                                "location_id": "location-origin",
                                "time": "00:00",
                                "duration_min": 30,
                                "decision_state": "selected",
                                "flexibility": "movable",
                                "evidence_state": "verified",
                            },
                            {
                                "activity_id": "activity-destination",
                                "title": "Destination",
                                "location_id": "location-destination",
                                "time": "02:00",
                                "duration_min": 30,
                                "decision_state": "selected",
                                "flexibility": "movable",
                                "evidence_state": "verified",
                            },
                        ],
                        "travel": [
                            {
                                "from_activity_id": "activity-origin",
                                "to_activity_id": "activity-destination",
                                "source": "canonical-fixture",
                                "recommended_mode": "driving",
                                "modes": {
                                    "driving": {
                                        "duration_min": 99,
                                        "buffer_min": 5,
                                        "distance_km": 9.9,
                                        "evidence_state": "unverified",
                                    }
                                },
                            }
                        ],
                    }
                ],
            },
        },
    )


class GoogleRoutesContractTests(unittest.TestCase):
    def setUp(self) -> None:
        policies = google_maps_policy_registry(
            GOOGLE_MAPS_NON_EEA_POLICY_PROFILE
        )
        self.snapshot = EvidenceSnapshot.from_ledger(
            EvidenceLedger(policies),
            evaluation_at=NOW,
            purge_now=NOW,
        )
        self.origin = _endpoint(
            self.snapshot,
            location_id="location-origin",
            place_id=ORIGIN_PLACE_ID,
            observation_seed="a",
        )
        self.destination = _endpoint(
            self.snapshot,
            location_id="location-destination",
            place_id=DESTINATION_PLACE_ID,
            observation_seed="b",
        )

    def assert_contract_error(self, code: str, function) -> None:
        with self.assertRaises(FactContractError) as caught:
            function()
        self.assertEqual(code, caught.exception.code)

    def request(
        self,
        *,
        mode: RouteMode = RouteMode.TRANSIT,
        departure_at: str = DEPARTURE,
        fallback: TransitFallbackPolicy = TransitFallbackPolicy.NONE,
    ):
        return build_google_route_request(
            self.snapshot,
            self.origin,
            self.destination,
            mode,
            departure_at=departure_at,
            transit_fallback_policy=fallback,
        )

    def execute(
        self,
        request,
        transport,
        *,
        budget: RouteAttemptBudget | None = None,
        **kwargs,
    ):
        return execute_google_route(
            request,
            transport,
            attempt_budget=budget or RouteAttemptBudget(10),
            clock=lambda: NOW + timedelta(minutes=1),
            **kwargs,
        )

    def test_exact_request_fingerprint_body_and_redaction(self) -> None:
        request = self.request()
        repeated = self.request()
        reversed_request = build_google_route_request(
            self.snapshot,
            self.destination,
            self.origin,
            RouteMode.TRANSIT,
            departure_at=DEPARTURE,
        )
        self.assertEqual(
            request.provider_request.request_fingerprint,
            repeated.provider_request.request_fingerprint,
        )
        self.assertNotEqual(
            request.provider_request.request_fingerprint,
            reversed_request.provider_request.request_fingerprint,
        )
        self.assertEqual(
            {
                "basis_evidence_revision",
                "basis_snapshot_id",
                "basis_store_revision",
                "destination_endpoint_id",
                "destination_observation_id",
                "destination_value_digest",
                "field_mask",
                "origin_endpoint_id",
                "origin_observation_id",
                "origin_value_digest",
            },
            {name for name, _value in request.provider_request.query_scope},
        )

        http = build_google_routes_http_request(request)
        self.assertEqual(GOOGLE_ROUTES_COMPUTE_URL, http.url)
        self.assertEqual(GOOGLE_ROUTES_FIELD_MASK, http.field_mask)
        self.assertEqual(
            {
                "computeAlternativeRoutes": False,
                "departureTime": "2026-07-28T16:30:00Z",
                "destination": {"placeId": DESTINATION_PLACE_ID},
                "origin": {"placeId": ORIGIN_PLACE_ID},
                "travelMode": "TRANSIT",
            },
            json.loads(http.body),
        )
        safe = json.dumps(
            {
                "request_repr": repr(request),
                "request_binding": request.to_binding_dict(),
                "http_repr": repr(http),
                "http_binding": http.to_binding_dict(),
            },
            sort_keys=True,
        )
        for sentinel in (
            ORIGIN_PLACE_ID,
            DESTINATION_PLACE_ID,
        ):
            self.assertNotIn(sentinel, safe)

    def test_request_is_factory_gated_and_endpoints_are_ordered_distinct(
        self,
    ) -> None:
        request = self.request()
        values = {
            "snapshot": request.snapshot,
            "origin": request.origin,
            "destination": request.destination,
            "mode": request.mode,
            "departure_at": request.departure_at,
            "transit_fallback_policy":
                request.transit_fallback_policy,
            "fallback_from_mode": request.fallback_from_mode,
            "provider_request": request.provider_request,
            "field_mask": request.field_mask,
        }
        self.assert_contract_error(
            "UNTRUSTED_PROVENANCE",
            lambda: GoogleRouteRequest(**values),
        )
        self.assert_contract_error(
            "INVALID_PROVIDER_REQUEST",
            lambda: build_google_route_request(
                self.snapshot,
                self.origin,
                self.origin,
                RouteMode.TRANSIT,
                departure_at=DEPARTURE,
            ),
        )

    def test_transit_horizon_is_inclusive_and_rejects_before_transport(
        self,
    ) -> None:
        for departure in (
            NOW - timedelta(days=7),
            NOW + timedelta(days=100),
        ):
            build_google_route_request(
                self.snapshot,
                self.origin,
                self.destination,
                RouteMode.TRANSIT,
                departure_at=departure.isoformat(),
            )
        transport = CannedTransport()
        for departure in (
            NOW - timedelta(days=7, microseconds=1),
            NOW + timedelta(days=100, microseconds=1),
        ):
            self.assert_contract_error(
                "OUTSIDE_PROVIDER_HORIZON",
                lambda departure=departure: build_google_route_request(
                    self.snapshot,
                    self.origin,
                    self.destination,
                    RouteMode.TRANSIT,
                    departure_at=departure.isoformat(),
                ),
            )
        self.assertEqual([], transport.calls)

    def test_fractional_protobuf_duration_parser_is_exact_and_bounded(
        self,
    ) -> None:
        self.assertEqual(
            Decimal("3.5"),
            parse_protobuf_duration_seconds("3.5s"),
        )
        self.assertEqual(
            Decimal("0.000000001"),
            parse_protobuf_duration_seconds("0.000000001s"),
        )
        for invalid in ("-1s", "1.1234567890s", "1", "NaNs", 1):
            self.assert_contract_error(
                "INVALID_PROVIDER_RESPONSE",
                lambda invalid=invalid:
                    parse_protobuf_duration_seconds(invalid),
            )

    def test_success_is_authorized_and_provider_text_is_not_retained(
        self,
    ) -> None:
        transport = CannedTransport(
            _success_response(
                warnings=["FREE FORM PROVIDER WARNING SENTINEL"],
                fallback_info={
                    "routingMode": "TRAFFIC_UNAWARE",
                    "reason": "SERVER_ERROR",
                },
            )
        )
        execution = self.execute(self.request(), transport)
        self.assertIsInstance(
            execution.primary_result,
            AuthorizedProviderResult,
        )
        result = execution.primary_result.result
        self.assertEqual(ProviderResultStatus.SUCCESS, result.status)
        payload = result.observations[0].value.payload
        self.assertAlmostEqual(10.5083333333, payload["duration_min"])
        self.assertEqual(12.345, payload["distance_km"])
        self.assertEqual(10, payload["static_duration_min"])
        self.assertEqual(
            (
                "provider_route_warning",
                "provider_routing_fallback",
            ),
            execution.warnings,
        )
        safe = json.dumps(
            {
                "execution": execution.to_binding_dict(),
                "result": result.to_dict(),
                "repr": repr(execution),
            },
            sort_keys=True,
        )
        self.assertNotIn("FREE FORM PROVIDER WARNING SENTINEL", safe)

    def test_strict_response_rejects_duplicate_nan_deep_and_oversized(
        self,
    ) -> None:
        bad_bodies = (
            b'{"routes":[],"routes":[]}',
            b'{"routes":[{"duration":NaN}]}',
            (
                b'{"routes":[{"duration":"1s","unknown":1}]}'
            ),
            b"x" * (64 * 1024 + 1),
        )
        for body in bad_bodies:
            with self.subTest(body=body[:30]):
                execution = self.execute(
                    self.request(),
                    CannedTransport(
                        GoogleRoutesHttpResponse(
                            status_code=200,
                            body=body,
                        )
                    ),
                )
                self.assertEqual(
                    ProviderProblemCode.INVALID_PROVIDER_RESPONSE,
                    execution.primary_result.result.problems[0].code,
                )

    def test_http_and_transport_errors_are_typed_without_raw_messages(
        self,
    ) -> None:
        cases = (
            (400, "INVALID_ARGUMENT",
             ProviderProblemCode.INVALID_PROVIDER_REQUEST),
            (403, "PERMISSION_DENIED",
             ProviderProblemCode.AUTH_FAILED),
            (404, "NOT_FOUND", ProviderProblemCode.NOT_FOUND),
            (429, "RESOURCE_EXHAUSTED",
             ProviderProblemCode.QUOTA_EXHAUSTED),
            (501, "UNIMPLEMENTED",
             ProviderProblemCode.UNSUPPORTED_MODE),
            (503, "UNAVAILABLE",
             ProviderProblemCode.PROVIDER_UNAVAILABLE),
        )
        for status_code, provider_status, expected in cases:
            with self.subTest(status_code=status_code):
                raw_message = "private provider diagnostic sentinel"
                response = GoogleRoutesHttpResponse(
                    status_code=status_code,
                    body=json.dumps(
                        {
                            "error": {
                                "code": status_code,
                                "status": provider_status,
                                "message": raw_message,
                            }
                        }
                    ).encode(),
                )
                execution = self.execute(
                    self.request(),
                    CannedTransport(response),
                )
                result = execution.primary_result.result
                self.assertEqual(expected, result.problems[0].code)
                self.assertNotIn(
                    raw_message,
                    json.dumps(result.to_dict()),
                )

    def test_attempt_budget_charges_each_send_and_retry_is_opt_in(
        self,
    ) -> None:
        no_budget_transport = CannedTransport(_success_response())
        exhausted = self.execute(
            self.request(),
            no_budget_transport,
            budget=RouteAttemptBudget(0),
        )
        self.assertEqual(0, exhausted.attempts_used)
        self.assertEqual([], no_budget_transport.calls)
        self.assertEqual(
            ProviderProblemCode.PROVIDER_BUDGET_EXHAUSTED,
            exhausted.primary_result.result.problems[0].code,
        )

        sleeps: list[float] = []
        retry_transport = CannedTransport(
            GoogleRoutesTransportError(
                GoogleRoutesTransportErrorKind.READ_TIMEOUT
            ),
            _success_response(),
        )
        budget = RouteAttemptBudget(2)
        retried = self.execute(
            self.request(),
            retry_transport,
            budget=budget,
            max_attempts=2,
            sleeper=sleeps.append,
        )
        self.assertEqual(ProviderResultStatus.SUCCESS,
                         retried.primary_result.result.status)
        self.assertEqual(2, retried.attempts_used)
        self.assertEqual(2, budget.used_attempts)
        self.assertEqual([1.0], sleeps)
        self.assertEqual(2, len(retry_transport.calls))

    def test_default_single_attempt_does_not_retry_transient_failure(
        self,
    ) -> None:
        transport = CannedTransport(
            GoogleRoutesHttpResponse(
                status_code=503,
                body=b'{"error":{"status":"UNAVAILABLE"}}',
            ),
            _success_response(),
        )
        execution = self.execute(self.request(), transport)
        self.assertEqual(1, execution.attempts_used)
        self.assertEqual(1, len(transport.calls))
        self.assertEqual(
            ProviderProblemCode.PROVIDER_UNAVAILABLE,
            execution.primary_result.result.problems[0].code,
        )

    def test_execution_rechecks_endpoint_freshness_without_spending(
        self,
    ) -> None:
        origin = _endpoint(
            self.snapshot,
            location_id="location-origin",
            place_id=ORIGIN_PLACE_ID,
            observation_seed="c",
            valid_until=NOW + timedelta(seconds=30),
        )
        request = build_google_route_request(
            self.snapshot,
            origin,
            self.destination,
            RouteMode.WALKING,
            departure_at=DEPARTURE,
        )
        transport = CannedTransport(_success_response())

        execution = self.execute(request, transport)

        self.assertEqual(0, execution.attempts_used)
        self.assertEqual([], transport.calls)
        self.assertEqual(
            ProviderProblemCode.STALE_EVIDENCE,
            execution.primary_result.result.problems[0].code,
        )

    def test_transit_unavailable_runs_one_exact_driving_fallback(
        self,
    ) -> None:
        transport = CannedTransport(
            GoogleRoutesHttpResponse(
                status_code=200,
                body=b'{"routes":[]}',
            ),
            _success_response(duration="900s", distance_m=8_000),
        )
        execution = self.execute(
            self.request(fallback=TransitFallbackPolicy.DRIVING),
            transport,
        )
        self.assertEqual(2, execution.attempts_used)
        self.assertIsNotNone(execution.fallback_request)
        self.assertIsNotNone(execution.fallback_result)
        assert execution.fallback_request is not None
        assert execution.fallback_result is not None
        self.assertEqual(
            {"mode": "driving", "departure_at": "2026-07-28T16:30:00Z"},
            execution.fallback_request.provider_request.fact_keys[
                0
            ].qualifier_map,
        )
        self.assertEqual(
            "transit",
            dict(
                execution.fallback_request.provider_request.query_scope
            )["fallback_from_mode"],
        )
        self.assertEqual(
            {
                ProviderProblemCode.NOT_FOUND,
                ProviderProblemCode.TRANSIT_UNAVAILABLE,
            },
            {
                problem.code
                for problem in execution.primary_result.result.problems
            },
        )
        payload = (
            execution.fallback_result.result.observations[0].value.payload
        )
        self.assertEqual("driving", payload["mode"])
        self.assertEqual("transit", payload["fallback_from_mode"])
        self.assertIn("transit_unavailable", execution.warnings)
        sent_bodies = [
            json.loads(call[0].body) for call in transport.calls
        ]
        self.assertEqual(
            ["TRANSIT", "DRIVE"],
            [body["travelMode"] for body in sent_bodies],
        )

    def test_auth_failure_never_triggers_transit_fallback(self) -> None:
        transport = CannedTransport(
            GoogleRoutesHttpResponse(
                status_code=403,
                body=b'{"error":{"status":"PERMISSION_DENIED"}}',
            ),
            _success_response(),
        )
        execution = self.execute(
            self.request(fallback=TransitFallbackPolicy.DRIVING),
            transport,
        )
        self.assertIsNone(execution.fallback_result)
        self.assertEqual(1, len(transport.calls))

    def test_beta_warning_codes_are_fixed(self) -> None:
        expected = {
            RouteMode.WALKING: "walking_route_beta",
            RouteMode.BICYCLING: "bicycling_route_beta",
            RouteMode.TWO_WHEELER: "two_wheeler_route_beta",
        }
        for mode, warning in expected.items():
            with self.subTest(mode=mode):
                execution = self.execute(
                    self.request(mode=mode),
                    CannedTransport(_success_response()),
                )
                self.assertIn(warning, execution.warnings)
                payload = (
                    execution.primary_result.result.observations[
                        0
                    ].value.payload
                )
                self.assertIn(warning, payload["warning_codes"])

    def test_batch_merges_partial_results_and_retains_exact_lkg(
        self,
    ) -> None:
        source = _durable_source()
        session = EvidenceSession(
            source,
            clock=lambda: NOW + timedelta(minutes=1),
        )
        initial = session.load()
        snapshot = initial.snapshot(
            evaluation_at=NOW + timedelta(minutes=1)
        )
        origin = extract_fresh_google_place_endpoint(
            snapshot,
            "location-origin",
        )
        destination = extract_fresh_google_place_endpoint(
            snapshot,
            "location-destination",
        )
        transit = build_google_route_request(
            snapshot,
            origin,
            destination,
            RouteMode.TRANSIT,
            departure_at=DEPARTURE,
        )
        seeded = execute_google_route_batch(
            (transit,),
            CannedTransport(_success_response(duration="720s")),
            session=session,
            attempt_budget=RouteAttemptBudget(1),
            clock=lambda: NOW + timedelta(minutes=1),
        )
        self.assertIsInstance(seeded, GoogleRouteBatchExecution)

        current_snapshot = seeded.current.snapshot(
            evaluation_at=NOW + timedelta(minutes=1)
        )
        current_origin = extract_fresh_google_place_endpoint(
            current_snapshot,
            "location-origin",
        )
        current_destination = extract_fresh_google_place_endpoint(
            current_snapshot,
            "location-destination",
        )
        walking = build_google_route_request(
            current_snapshot,
            current_origin,
            current_destination,
            RouteMode.WALKING,
            departure_at=DEPARTURE,
        )
        same_transit = build_google_route_request(
            current_snapshot,
            current_origin,
            current_destination,
            RouteMode.TRANSIT,
            departure_at=DEPARTURE,
        )
        batch = execute_google_route_batch(
            (walking, same_transit),
            CannedTransport(
                _success_response(duration="300s", distance_m=700),
                GoogleRoutesTransportError(
                    GoogleRoutesTransportErrorKind.READ_TIMEOUT
                ),
            ),
            session=session,
            attempt_budget=RouteAttemptBudget(2),
            clock=lambda: NOW + timedelta(minutes=1),
        )

        self.assertEqual(2, batch.attempts_used)
        self.assertEqual(2, len(batch.executions))
        self.assertEqual(
            ProviderProblemCode.TIMEOUT,
            batch.current.provider_problems[0].code,
        )
        final_snapshot = batch.current.snapshot(
            evaluation_at=NOW + timedelta(minutes=1)
        )
        walking_resolution = final_snapshot.resolve(
            walking.provider_request.fact_keys[0]
        )
        transit_resolution = final_snapshot.resolve(
            same_transit.provider_request.fact_keys[0]
        )
        self.assertEqual(
            5,
            walking_resolution.selected.value.payload["duration_min"],
        )
        self.assertEqual(
            12,
            transit_resolution.selected.value.payload["duration_min"],
        )

    def test_batch_rejects_memory_or_durable_basis_drift_before_send(
        self,
    ) -> None:
        source = _durable_source()
        session = EvidenceSession(
            source,
            clock=lambda: NOW + timedelta(minutes=1),
        )
        initial = session.load()
        snapshot = initial.snapshot(
            evaluation_at=NOW + timedelta(minutes=1)
        )
        origin = extract_fresh_google_place_endpoint(
            snapshot,
            "location-origin",
        )
        destination = extract_fresh_google_place_endpoint(
            snapshot,
            "location-destination",
        )
        stale_request = build_google_route_request(
            snapshot,
            origin,
            destination,
            RouteMode.TRANSIT,
            departure_at=DEPARTURE,
        )
        execution = execute_google_route(
            stale_request,
            CannedTransport(_success_response()),
            attempt_budget=RouteAttemptBudget(1),
            clock=lambda: NOW + timedelta(minutes=1),
        )
        session.merge(execution.primary_result)
        memory_drift_transport = CannedTransport(_success_response())

        self.assert_contract_error(
            "EVIDENCE_REVISION_CHANGED",
            lambda: execute_google_route_batch(
                (stale_request,),
                memory_drift_transport,
                session=session,
                attempt_budget=RouteAttemptBudget(1),
                clock=lambda: NOW + timedelta(minutes=1),
            ),
        )
        self.assertEqual([], memory_drift_transport.calls)

        current = session.load()
        current_snapshot = current.snapshot(
            evaluation_at=NOW + timedelta(minutes=1)
        )
        current_request = build_google_route_request(
            current_snapshot,
            extract_fresh_google_place_endpoint(
                current_snapshot,
                "location-origin",
            ),
            extract_fresh_google_place_endpoint(
                current_snapshot,
                "location-destination",
            ),
            RouteMode.WALKING,
            departure_at=DEPARTURE,
        )
        source.result = EvidenceStoreResult(
            success=True,
            status="loaded",
            action="load",
            ledger=source.result.ledger,
            current_revision="d" * 64,
            generation=source.result.generation,
            purge_checked_at=NOW,
        )
        durable_drift_transport = CannedTransport(_success_response())
        self.assert_contract_error(
            "EVIDENCE_REVISION_CHANGED",
            lambda: execute_google_route_batch(
                (current_request,),
                durable_drift_transport,
                session=session,
                attempt_budget=RouteAttemptBudget(1),
                clock=lambda: NOW + timedelta(minutes=1),
            ),
        )
        self.assertEqual([], durable_drift_transport.calls)

    def test_merge_rejects_response_after_durable_drift(self) -> None:
        source = _durable_source()
        session = EvidenceSession(
            source,
            clock=lambda: NOW + timedelta(minutes=1),
        )
        current = session.load()
        snapshot = current.snapshot(
            evaluation_at=NOW + timedelta(minutes=1)
        )
        request = build_google_route_request(
            snapshot,
            extract_fresh_google_place_endpoint(
                snapshot,
                "location-origin",
            ),
            extract_fresh_google_place_endpoint(
                snapshot,
                "location-destination",
            ),
            RouteMode.WALKING,
            departure_at=DEPARTURE,
        )
        execution = execute_google_route(
            request,
            CannedTransport(_success_response()),
            attempt_budget=RouteAttemptBudget(1),
            clock=lambda: NOW + timedelta(minutes=1),
        )
        source.result = EvidenceStoreResult(
            success=True,
            status="loaded",
            action="load",
            ledger=source.result.ledger,
            current_revision="e" * 64,
            generation=source.result.generation,
            purge_checked_at=NOW,
        )

        self.assert_contract_error(
            "EVIDENCE_REVISION_CHANGED",
            lambda: session.merge(execution.primary_result),
        )
        self.assertEqual(
            2,
            len(session.load().ledger.observations),
        )

    def test_real_fallback_flows_through_session_composition_and_timeline(
        self,
    ) -> None:
        source = _durable_source()
        session = EvidenceSession(
            source,
            clock=lambda: NOW + timedelta(minutes=1),
        )
        initial = session.load()
        snapshot = initial.snapshot(
            evaluation_at=NOW + timedelta(minutes=1)
        )
        request = build_google_route_request(
            snapshot,
            extract_fresh_google_place_endpoint(
                snapshot,
                "location-origin",
            ),
            extract_fresh_google_place_endpoint(
                snapshot,
                "location-destination",
            ),
            RouteMode.TRANSIT,
            departure_at=DEPARTURE,
            transit_fallback_policy=TransitFallbackPolicy.DRIVING,
        )
        batch = execute_google_route_batch(
            (request,),
            CannedTransport(
                GoogleRoutesHttpResponse(
                    status_code=200,
                    body=b'{"routes":[]}',
                ),
                _success_response(duration="900s", distance_m=8_000),
            ),
            session=session,
            attempt_budget=RouteAttemptBudget(2),
            clock=lambda: NOW + timedelta(minutes=1),
        )
        evidence = batch.current.snapshot(
            evaluation_at=NOW + timedelta(minutes=1)
        )
        composed = compose_trip_state(_fallback_plan(), evidence)
        estimate = next(
            item
            for item in composed.state.travel_estimates
            if item.from_location_id == "location-origin"
            and item.to_location_id == "location-destination"
            and item.mode == "driving"
            and item.evidence_state.value == "verified"
        )
        report = evaluate_timeline(
            composed.state,
            now=NOW + timedelta(minutes=1),
        )

        self.assertEqual("transit", estimate.fallback_from_mode)
        self.assertEqual(15, estimate.duration_min)
        self.assertIn("transit_unavailable", batch.warnings)
        self.assertIn(
            ProviderProblemCode.TRANSIT_UNAVAILABLE,
            {
                problem.code
                for problem in batch.current.provider_problems
            },
        )
        disclosure = next(
            issue
            for issue in report.issues
            if issue.code == "TRANSIT_FALLBACK_DISCLOSURE"
        )
        self.assertEqual(
            "transit",
            dict(disclosure.details)["fallback_from_mode"],
        )
        self.assertEqual(
            "none",
            dict(disclosure.details)["status_effect"],
        )


if __name__ == "__main__":
    unittest.main()
