"""Phase 5.32 provider-specific quarantine and evidence routing."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tests.test_phase4_evidence_store import (
    MutableClock,
    authorized_success,
    policy_registry,
    route_key,
    route_observation,
)
from tests.test_phase4_composition import _canonical_plan
from tests.test_phase44_place_details import (
    _Fixture as PlaceDetailsFixture,
    _current_body,
)
from tests.test_phase516_guided_provider_execution_target_bindings import (
    _hotel_request,
    _identity_intent,
    _route_request,
    _serpapi_item,
)
from tests.test_phase513_guided_provider_preflight import _google_item
from tests.test_phase531_guided_provider_execution import (
    EXECUTION_START_AT,
    _ScriptedTransport,
    _TickingClock,
    _execute,
    _limits,
    _prepare_single_profile,
    _response,
)
from trip_planner import guided_provider_pre_execution as pre_execution_module
from trip_planner.evidence_session import EvidenceSession
from trip_planner.evidence_store import EvidenceStore, EvidenceStoreResult
from trip_planner.composition import compose_trip_state
from trip_planner.codec import compute_revision
from trip_planner.facts import (
    AuthorizedProviderResult,
    EvidenceLedger,
    FactKind,
    FactContractError,
    GOOGLE_MAPS_NON_EEA_POLICY_PROFILE,
    google_maps_policy_registry,
)
from trip_planner.guided_evidence_plan import GuidedEvidenceTopic
from trip_planner.guided_provider_evidence_workflow import (
    GUIDED_PROVIDER_EVIDENCE_WORKFLOW_VERSION,
    GuidedProviderEvidenceAssessmentStatus,
    GuidedProviderEvidenceItemStatus,
    assess_guided_provider_quarantined_responses,
    finalize_guided_provider_identity_evidence,
    merge_guided_provider_identity_evidence,
    route_guided_provider_memory_evidence,
)
from trip_planner.guided_provider_execution import (
    execute_guided_provider_requests,
)
from trip_planner.guided_provider_scope import GuidedProviderCapability
from trip_planner.place_details import (
    GooglePlaceDetailsHttpResponse,
    PlaceDetailsKind,
    authorize_google_place_details_http_response,
)
from trip_planner.places_identity import (
    PlaceIdentityIntent,
    PlaceIdentityReviewAuthority,
    PlaceIdentityReviewStatus,
)
from trip_planner.routes import (
    GoogleRoutesHttpResponse,
    authorize_google_route_http_response,
    build_google_route_request,
)
from trip_planner.readiness import TripReadiness, assess_trip_readiness
from trip_planner.scheduling import (
    ScheduleProblem,
    schedule_problem_from_composed,
    validate_schedule_problem,
)


UTC = timezone.utc


class _StaticDurableSource:
    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot
        self.ledger = EvidenceLedger(
            snapshot.policies,
            snapshot.observations,
            generation=1,
            _token=__import__(
                "trip_planner.facts", fromlist=["_LEDGER_TOKEN"]
            )._LEDGER_TOKEN,
        )

    def load(self) -> EvidenceStoreResult:
        return EvidenceStoreResult(
            success=True,
            status="loaded",
            action="load",
            ledger=self.ledger,
            current_revision=self.snapshot.store_revision,
            generation=self.ledger.generation,
            purge_checked_at=self.snapshot.purge_checked_at,
        )


def _assessment(*actions: object):
    execution, context, bundle, preimages = _execute(
        _ScriptedTransport(*actions)
    )
    assessed = assess_guided_provider_quarantined_responses(
        context,
        bundle,
        execution,
        preimages=preimages,
        evaluation_at=execution._completed_at + timedelta(milliseconds=1),
    )
    return assessed, execution, context, bundle, preimages


def _identity_body(intent: PlaceIdentityIntent) -> bytes:
    locality = intent.expected_locality or "Fixture locality"
    primary_type = (
        intent.expected_primary_types[0]
        if intent.expected_primary_types
        else "tourist_attraction"
    )
    return json.dumps(
        {
            "places": [
                {
                    "id": "ChIJ-phase532-private-provider-id",
                    "displayName": {
                        "text": intent.expected_name,
                        "languageCode": "en",
                    },
                    "formattedAddress": f"{locality}, {intent.region_code}",
                    "location": {"latitude": 35.0, "longitude": 135.0},
                    "primaryType": primary_type,
                    "types": [primary_type],
                    "addressComponents": [
                        {
                            "longText": locality,
                            "shortText": locality,
                            "types": ["locality", "political"],
                            "languageCode": "en",
                        },
                        {
                            "longText": "Fixture Country",
                            "shortText": intent.region_code,
                            "types": ["country", "political"],
                            "languageCode": "en",
                        },
                    ],
                }
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


class GuidedProviderEvidenceWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        with pre_execution_module._CONSENT_CLAIM_LOCK:
            pre_execution_module._CONSENT_CLAIMS.clear()

    def test_default_execution_revalidates_and_routes_only_memory_results(self) -> None:
        assessment, execution, _, _, preimages = _assessment(
            _response(body=b"{}"),
            _response(body=b'{"places":[]}'),
        )
        self.assertEqual(
            GUIDED_PROVIDER_EVIDENCE_WORKFLOW_VERSION,
            assessment.to_dict()["contract_version"],
        )
        self.assertEqual(
            GuidedProviderEvidenceAssessmentStatus.READY_FOR_EVIDENCE,
            assessment.status,
        )
        self.assertEqual(
            GuidedProviderEvidenceItemStatus.AUTHORIZED_RESULT,
            assessment._items[0].status,
        )
        self.assertEqual(
            PlaceIdentityReviewStatus.FAILED,
            assessment.identity_review(1).status,
        )
        self.assertFalse(assessment._items[1].supports_evidence_routing)

        details_target = next(
            item.target
            for item in preimages
            if type(item.target).__name__ == "GooglePlaceDetailsRequest"
        )
        session = EvidenceSession(
            _StaticDurableSource(details_target.snapshot),
            clock=lambda: execution._completed_at + timedelta(seconds=1),
        )
        routed = route_guided_provider_memory_evidence(assessment, session)
        self.assertEqual(1, routed.merge_count)
        self.assertFalse(routed.to_dict()["writes_to_disk"])
        self.assertFalse(routed.to_dict()["canonical_authority"])

    def test_memory_evidence_flows_through_existing_planning_kernel(self) -> None:
        route = _route_request()
        context, bundle, preimages, _ = _prepare_single_profile(
            GuidedEvidenceTopic.ROUTE,
            GuidedProviderCapability.GOOGLE_ROUTES,
            _google_item(
                GuidedEvidenceTopic.ROUTE,
                GuidedProviderCapability.GOOGLE_ROUTES,
                1,
            ),
            route,
        )
        execution = execute_guided_provider_requests(
            context,
            bundle,
            _ScriptedTransport(
                _response(
                    body=(
                        b'{"routes":[{"duration":"600s",'
                        b'"distanceMeters":1200}]}'
                    )
                )
            ),
            _limits(bundle),
            preimages=preimages,
            identity_snapshot=None,
            clock=_TickingClock(),
        )
        assessment = assess_guided_provider_quarantined_responses(
            context,
            bundle,
            execution,
            preimages=preimages,
            evaluation_at=execution._completed_at + timedelta(milliseconds=1),
        )
        session = EvidenceSession(
            _StaticDurableSource(route.snapshot),
            clock=lambda: execution._completed_at + timedelta(seconds=1),
        )
        routed = route_guided_provider_memory_evidence(assessment, session)
        evaluation_at = execution._completed_at + timedelta(seconds=2)
        snapshot = routed.current.snapshot(evaluation_at=evaluation_at)
        plan = _canonical_plan(route_mode="driving")
        day = plan["state"]["itinerary"]["days"][0]
        day["date"] = "2026-10-13"
        day["places"][0]["location_id"] = route.origin.location_id
        day["places"][1]["location_id"] = route.destination.location_id
        day["start_location_id"] = route.origin.location_id
        day["end_location_id"] = route.destination.location_id
        plan["state"]["trip"]["date_range"] = (
            "2026-10-13 ~ 2026-10-13"
        )
        plan["revision"] = compute_revision(plan)
        before = json.dumps(plan, sort_keys=True, separators=(",", ":"))

        composed = compose_trip_state(plan, snapshot)
        readiness = assess_trip_readiness(
            canonical_plan=plan,
            composed=composed,
            snapshot=snapshot,
        )
        problem = schedule_problem_from_composed(composed)
        failures = validate_schedule_problem(problem)

        self.assertIs(type(readiness), TripReadiness)
        self.assertIs(type(problem), ScheduleProblem)
        self.assertEqual(composed.evidence, problem.evidence_binding)
        route_observations = tuple(
            item
            for item in snapshot.observations
            if item.key.kind is FactKind.ROUTE_ESTIMATE
        )
        self.assertEqual(1, len(route_observations))
        self.assertEqual(
            (route_observations[0].observation_id,),
            composed.evidence.used_observation_ids,
        )
        self.assertNotEqual(
            composed.canonical_state_digest,
            composed.composed_state_digest,
        )
        self.assertTrue(
            any(
                estimate.duration_min == 10
                and estimate.evidence_ref
                == f"fact:{route_observations[0].observation_id}"
                for estimate in composed.state.travel_estimates
            )
        )
        self.assertIsInstance(failures, tuple)
        self.assertEqual(
            before,
            json.dumps(plan, sort_keys=True, separators=(",", ":")),
        )

    def test_identity_ready_requires_existing_finalizer_and_safe_view_is_redacted(self) -> None:
        # The default request order is Details then Places Text Search.
        _, _, _, preimages = _execute(
            _ScriptedTransport(_response(body=b"{}"), _response(body=b"{}"))
        )
        intent = next(
            item.target
            for item in preimages
            if type(item.target) is PlaceIdentityIntent
        )
        with pre_execution_module._CONSENT_CLAIM_LOCK:
            pre_execution_module._CONSENT_CLAIMS.clear()
        assessment, execution, _, _, _ = _assessment(
            _response(body=b"{}"),
            _response(body=_identity_body(intent)),
        )
        review = assessment.identity_review(1)
        self.assertEqual(PlaceIdentityReviewStatus.READY, review.status)
        safe = json.dumps(assessment.to_dict(), ensure_ascii=False)
        self.assertNotIn(intent.expected_name, safe)
        self.assertNotIn("ChIJ-phase532-private-provider-id", safe)
        explicit = json.dumps(
            assessment.identity_review_payload(1), ensure_ascii=False
        )
        self.assertIn(intent.expected_name, explicit)
        authorized = finalize_guided_provider_identity_evidence(
            assessment,
            1,
            review.request.snapshot,
            PlaceIdentityReviewAuthority(
                reviewer_id="phase532-host",
                clock=lambda: execution._completed_at + timedelta(seconds=1),
            ),
        )
        self.assertIs(type(authorized), AuthorizedProviderResult)

    def test_identity_workflow_merges_through_durable_cas_boundary(self) -> None:
        intent = _identity_intent()
        context, bundle, preimages, _ = _prepare_single_profile(
            GuidedEvidenceTopic.PLACE_IDENTITY,
            GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP,
            _google_item(
                GuidedEvidenceTopic.PLACE_IDENTITY,
                GuidedProviderCapability.GOOGLE_PLACES_IDENTITY_LOOKUP,
                1,
            ),
            intent,
        )
        with tempfile.TemporaryDirectory() as temporary:
            trips_root = Path(temporary) / "trips"
            (trips_root / "phase532-identity" / "data").mkdir(parents=True)
            policies = google_maps_policy_registry(
                GOOGLE_MAPS_NON_EEA_POLICY_PROFILE
            )
            clock = MutableClock(EXECUTION_START_AT - timedelta(minutes=1))
            store = EvidenceStore(
                trips_root,
                "phase532-identity",
                "phase532-identity-trip",
                policies,
                clock=clock,
            )
            initial = store.load()
            current_snapshot = initial.snapshot(
                evaluation_at=EXECUTION_START_AT - timedelta(seconds=30)
            )
            execution = execute_guided_provider_requests(
                context,
                bundle,
                _ScriptedTransport(_response(body=_identity_body(intent))),
                _limits(bundle),
                preimages=preimages,
                identity_snapshot=current_snapshot,
                clock=_TickingClock(),
            )
            assessment = assess_guided_provider_quarantined_responses(
                context,
                bundle,
                execution,
                preimages=preimages,
                evaluation_at=(
                    execution._completed_at + timedelta(milliseconds=1)
                ),
            )
            self.assertEqual(
                PlaceIdentityReviewStatus.READY,
                assessment.identity_review(0).status,
            )
            clock.value = execution._completed_at + timedelta(seconds=2)
            merged = merge_guided_provider_identity_evidence(
                assessment,
                0,
                store,
                current_snapshot,
                PlaceIdentityReviewAuthority(
                    reviewer_id="phase532-durable-host",
                    clock=lambda: execution._completed_at
                    + timedelta(seconds=1),
                ),
            )
            self.assertTrue(merged.success, merged.to_dict())
            self.assertEqual("merged", merged.status)
            self.assertEqual(1, len(merged.ledger.observations))
            self.assertNotIn(
                "ChIJ-phase532-private-provider-id",
                json.dumps(merged.to_dict()),
            )

    def test_quarantine_or_clock_drift_fails_before_adapter_use(self) -> None:
        execution, context, bundle, preimages = _execute(
            _ScriptedTransport(
                _response(body=b"{}"),
                _response(body=b'{"places":[]}'),
            )
        )
        with self.assertRaisesRegex(ValueError, "clock rolled back"):
            assess_guided_provider_quarantined_responses(
                context,
                bundle,
                execution,
                preimages=preimages,
                evaluation_at=execution._started_at,
            )
        quarantine = execution._outcomes[0]._quarantine
        assert quarantine is not None
        original = quarantine._body
        object.__setattr__(quarantine, "_body", b'{"tampered":true}')
        try:
            with self.assertRaisesRegex(ValueError, "no longer matches"):
                assess_guided_provider_quarantined_responses(
                    context,
                    bundle,
                    execution,
                    preimages=preimages,
                    evaluation_at=execution._completed_at,
                )
        finally:
            object.__setattr__(quarantine, "_body", original)

    def test_assessment_safe_views_reject_post_assessment_drift(self) -> None:
        assessment, _, _, _, _ = _assessment(
            _response(body=b"{}"),
            _response(body=b'{"places":[]}'),
        )
        original_action = assessment.next_action
        object.__setattr__(assessment, "next_action", "private-provider-value")
        try:
            with self.assertRaises(ValueError):
                assessment.to_dict()
        finally:
            object.__setattr__(assessment, "next_action", original_action)

        item = assessment._items[0]
        original_problem = item.problem_code
        object.__setattr__(item, "problem_code", "private-provider-value")
        try:
            with self.assertRaises(ValueError):
                item.to_safe_dict()
        finally:
            object.__setattr__(item, "problem_code", original_problem)

    def test_identity_strict_json_rejects_duplicate_deep_and_oversized_data(self) -> None:
        bodies = (
            b'{"places":[],"places":[]}',
            (b'{"a":' * 13) + b"0" + (b"}" * 13),
            b'{"places":[],"padding":"' + b"x" * 65_536 + b'"}',
        )
        for body in bodies:
            with self.subTest(size=len(body)):
                with pre_execution_module._CONSENT_CLAIM_LOCK:
                    pre_execution_module._CONSENT_CLAIMS.clear()
                assessment, _, _, _, _ = _assessment(
                    _response(body=b"{}"),
                    _response(body=body),
                )
                self.assertEqual(
                    GuidedProviderEvidenceItemStatus.REJECTED,
                    assessment._items[1].status,
                )
                self.assertEqual(
                    "provider_adapter_rejected_response",
                    assessment._items[1].problem_code,
                )

    def test_current_hours_uses_private_send_time_across_local_midnight(self) -> None:
        sent_at = datetime(2026, 7, 29, 14, 59, tzinfo=UTC)
        completed_at = sent_at + timedelta(minutes=2)
        fixture = PlaceDetailsFixture(now=sent_at)
        request = fixture.request(
            PlaceDetailsKind.CURRENT_HOURS,
            target_start=date(2026, 7, 29),
            target_end=date(2026, 7, 29),
        )
        response = GooglePlaceDetailsHttpResponse(
            status_code=200,
            body=json.dumps(_current_body()).encode("utf-8"),
        )
        authorized = authorize_google_place_details_http_response(
            request,
            response,
            sent_at=sent_at,
            completed_at=completed_at,
            attempts_used=1,
        )
        payload = authorized.result.observations[0].value.payload
        self.assertEqual("2026-07-29", payload["coverage_start"])
        self.assertEqual(sent_at, authorized.result.observations[0].retrieved_at)
        self.assertEqual(completed_at, authorized.result.completed_at)

    def test_google_adapter_caps_apply_to_non_success_statuses(self) -> None:
        sent_at = EXECUTION_START_AT
        details_fixture = PlaceDetailsFixture(now=sent_at)
        details = details_fixture.request(PlaceDetailsKind.PROFILE)
        oversized = b"x" * (65_536 + 1)
        with self.assertRaises(FactContractError):
            authorize_google_place_details_http_response(
                details,
                GooglePlaceDetailsHttpResponse(500, oversized),
                sent_at=sent_at,
                completed_at=sent_at,
                attempts_used=1,
            )
        route = _route_request()
        with self.assertRaises(FactContractError):
            authorize_google_route_http_response(
                route,
                GoogleRoutesHttpResponse(500, oversized),
                sent_at=EXECUTION_START_AT,
                completed_at=EXECUTION_START_AT,
                attempts_used=1,
            )

    def test_route_horizon_is_checked_before_send_and_again_before_authorization(self) -> None:
        from trip_planner import guided_provider_execution as execution_module

        route_basis = _route_request()
        departure = EXECUTION_START_AT + timedelta(milliseconds=1)
        route = build_google_route_request(
            route_basis.snapshot,
            route_basis.origin,
            route_basis.destination,
            route_basis.mode,
            departure_at=departure.isoformat().replace("+00:00", "Z"),
            transit_fallback_policy=route_basis.transit_fallback_policy,
        )
        after_departure = departure + timedelta(milliseconds=1)
        self.assertTrue(
            execution_module._normalization_target_fresh_at(
                route,
                now=EXECUTION_START_AT,
            )
        )
        self.assertFalse(
            execution_module._normalization_target_fresh_at(
                route,
                now=after_departure,
            )
        )
        with self.assertRaises(FactContractError):
            authorize_google_route_http_response(
                route,
                GoogleRoutesHttpResponse(
                    200,
                    b'{"routes":[{"duration":"600s"}]}',
                ),
                sent_at=after_departure,
                completed_at=after_departure,
                attempts_used=1,
            )

    def test_routes_and_hotels_dispatch_to_distinct_non_generic_outputs(self) -> None:
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
                b'{"routes":[{"duration":"600s","distanceMeters":1200}]}',
                GuidedProviderEvidenceItemStatus.AUTHORIZED_RESULT,
            ),
            (
                GuidedEvidenceTopic.LODGING,
                GuidedProviderCapability.SERPAPI_GOOGLE_HOTELS,
                _serpapi_item(GuidedEvidenceTopic.LODGING),
                _hotel_request(),
                json.dumps(
                    {
                        "search_metadata": {"status": "Success", "id": "private-search"},
                        "properties": [
                            {
                                "name": "Private Hotel",
                                "gps_coordinates": {
                                    "latitude": 35.0,
                                    "longitude": 135.0,
                                },
                                "price": {
                                    "amount_minor": 12_000,
                                    "currency": "JPY",
                                    "minor_unit": 0,
                                    "basis": "nightly",
                                },
                                "booking_token": "private-booking-token",
                            }
                        ],
                    }
                ).encode("utf-8"),
                GuidedProviderEvidenceItemStatus.LODGING_CANDIDATES,
            ),
        )
        for topic, capability, preflight, target, body, expected in cases:
            with self.subTest(profile=capability.value):
                with pre_execution_module._CONSENT_CLAIM_LOCK:
                    pre_execution_module._CONSENT_CLAIMS.clear()
                context, bundle, preimages, _ = _prepare_single_profile(
                    topic,
                    capability,
                    preflight,
                    target,
                )
                execution = execute_guided_provider_requests(
                    context,
                    bundle,
                    _ScriptedTransport(_response(body=body)),
                    _limits(bundle),
                    preimages=preimages,
                    identity_snapshot=None,
                    clock=_TickingClock(),
                )
                assessed = assess_guided_provider_quarantined_responses(
                    context,
                    bundle,
                    execution,
                    preimages=preimages,
                    evaluation_at=(
                        execution._completed_at + timedelta(milliseconds=1)
                    ),
                )
                self.assertEqual(expected, assessed._items[0].status)
                if expected is GuidedProviderEvidenceItemStatus.LODGING_CANDIDATES:
                    self.assertFalse(assessed._items[0].supports_evidence_routing)
                    safe = json.dumps(assessed.to_dict())
                    self.assertNotIn("Private Hotel", safe)
                    self.assertNotIn("private-booking-token", safe)

    def test_evidence_store_expected_revision_is_an_atomic_cas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trips_root = Path(temporary) / "trips"
            data_dir = trips_root / "phase532-cas" / "data"
            data_dir.mkdir(parents=True)
            policies = policy_registry()
            clock = MutableClock()
            store = EvidenceStore(
                trips_root,
                "phase532-cas",
                "trip-evidence-fixture",
                policies,
                clock=clock,
            )
            initial = store.load()
            assert initial.current_revision is not None
            key = route_key()
            first = route_observation(
                policies,
                key,
                source_suffix="phase532-first",
            )
            merged = store.merge(
                authorized_success(policies, first),
                expected_revision=initial.current_revision,
            )
            self.assertTrue(merged.success)
            second = route_observation(
                policies,
                key,
                duration_min=45,
                source_suffix="phase532-second",
            )
            rejected = store.merge(
                authorized_success(policies, second),
                expected_revision=initial.current_revision,
            )
            self.assertFalse(rejected.success)
            self.assertEqual("EVIDENCE_REVISION_CHANGED", rejected.problems[0].code)
            self.assertEqual(merged.current_revision, rejected.current_revision)

    def test_evidence_store_cas_exact_retry_reconciles_lost_ack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trips_root = Path(temporary) / "trips"
            data_dir = trips_root / "phase532-cas-replay" / "data"
            data_dir.mkdir(parents=True)
            policies = policy_registry()
            clock = MutableClock()
            base = EvidenceStore(
                trips_root,
                "phase532-cas-replay",
                "trip-evidence-fixture",
                policies,
                clock=clock,
            ).load()
            assert base.current_revision is not None
            item = route_observation(
                policies,
                route_key(),
                source_suffix="phase532-lost-ack",
            )

            def fail_after_replace(stage: str) -> None:
                if stage == "after_replace":
                    raise RuntimeError("forced lost acknowledgement")

            uncertain = EvidenceStore(
                trips_root,
                "phase532-cas-replay",
                "trip-evidence-fixture",
                policies,
                clock=clock,
                fault_hook=fail_after_replace,
            ).merge(
                authorized_success(policies, item),
                expected_revision=base.current_revision,
            )
            self.assertFalse(uncertain.success)
            self.assertEqual("outcome_unknown", uncertain.status)

            replay = EvidenceStore(
                trips_root,
                "phase532-cas-replay",
                "trip-evidence-fixture",
                policies,
                clock=clock,
            ).merge(
                authorized_success(policies, item),
                expected_revision=base.current_revision,
            )
            self.assertTrue(replay.success, replay.to_dict())
            self.assertEqual("no_op", replay.status)
            self.assertTrue(replay.replayed)

    def test_evidence_store_cas_purges_before_revision_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trips_root = Path(temporary) / "trips"
            (trips_root / "phase532-cas-purge" / "data").mkdir(parents=True)
            policies = policy_registry()
            clock = MutableClock()
            store = EvidenceStore(
                trips_root,
                "phase532-cas-purge",
                "trip-evidence-fixture",
                policies,
                clock=clock,
            )
            base = store.load()
            first = route_observation(
                policies,
                route_key(),
                purge_at=clock.value + timedelta(minutes=1),
                source_suffix="phase532-expiring",
            )
            merged = store.merge(
                authorized_success(policies, first),
                expected_revision=base.current_revision,
            )
            self.assertTrue(merged.success)
            clock.value += timedelta(minutes=2)
            second = route_observation(
                policies,
                route_key(),
                retrieved_at=clock.value,
                source_suffix="phase532-after-expiry",
            )
            rejected = store.merge(
                authorized_success(policies, second),
                expected_revision=merged.current_revision,
            )
            self.assertFalse(rejected.success)
            self.assertEqual("EVIDENCE_REVISION_CHANGED", rejected.problems[0].code)
            self.assertTrue(rejected.changed)
            self.assertEqual((), store.load().ledger.observations)


if __name__ == "__main__":
    unittest.main()
