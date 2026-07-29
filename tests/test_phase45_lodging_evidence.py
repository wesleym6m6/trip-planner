"""Offline contracts for Phase 4.5B lodging evidence projections."""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta, timezone

import trip_planner
import trip_planner.facts as facts_module
from trip_planner.facts import (
    GOOGLE_MAPS_NON_EEA_POLICY_PROFILE,
    EvidencePersistence,
    EvidenceLedger,
    EvidenceSnapshot,
    FactContractError,
    FactKey,
    FactKind,
    FactObservation,
    FactValue,
    ProviderProvenance,
    ProviderPolicy,
    ProviderPolicyRegistry,
    google_maps_policy_registry,
)
from trip_planner.evidence_session import EvidenceSession
from trip_planner.evidence_store import EvidenceStoreResult
from trip_planner.lodging import (
    IntentAuthority,
    LocationHint,
    LocationHintKind,
    LodgingIntentDraft,
    LodgingKind,
    PriceBasis,
    bind_lodging_candidate,
)
from trip_planner.lodging_evidence import (
    LodgingComparisonCapability,
    LodgingIdentityDisposition,
    LodgingRouteDirection,
    LodgingRouteDisposition,
    LodgingRouteProbe,
    assess_lodging_evidence,
)
from trip_planner.models import DecisionState, EvidenceState
from trip_planner.routes import (
    GoogleRoutesHttpResponse,
    RouteAttemptBudget,
    RouteMode,
    execute_google_route_batch,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)
DEPARTURE = NOW + timedelta(minutes=30)
CHECK_IN = date(2026, 8, 1)
CHECK_OUT = date(2026, 8, 4)
STAY_LOCATION = "private-stay-location"
ANCHOR_LOCATION = "private-arrival-anchor"
OTHER_LOCATION = "private-other-stay"
STAY_PLACE_ID = "ChIJ-private-stay-sentinel"
ANCHOR_PLACE_ID = "ChIJ-private-anchor-sentinel"
PRIVATE_LABEL = "Restricted lodging label sentinel"
PRIVATE_ADDRESS = "99 Restricted road token=secret"
PRIVATE_PRICE = 987_654


def _policies():
    return google_maps_policy_registry(
        GOOGLE_MAPS_NON_EEA_POLICY_PROFILE
    )


def _identity_observation(
    policies,
    *,
    location_id: str,
    place_id: str,
    retrieved_at: datetime = NOW - timedelta(minutes=5),
    valid_until: datetime = NOW + timedelta(days=30),
) -> FactObservation:
    key = FactKey(
        kind=FactKind.PLACE_IDENTITY,
        subject_ids=(location_id,),
        qualifiers=(("identity_provider", "google-places"),),
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
            request_fingerprint="a" * 64,
            retention_policy_id="google-place-id-v1",
            provider_record_id=place_id,
            attributions=(("Google Maps", None),),
        ),
        retrieved_at=retrieved_at,
        valid_until=valid_until,
        purge_at=None,
        confidence=1.0,
    )


def _snapshot(
    *observations: FactObservation,
    evaluation_at: datetime = NOW,
    purge_now: datetime = NOW,
    policies: ProviderPolicyRegistry | None = None,
) -> EvidenceSnapshot:
    policies = _policies() if policies is None else policies
    ledger = EvidenceLedger(
        policies,
        tuple(observations),
        generation=1 if observations else 0,
        _token=(
            facts_module._LEDGER_TOKEN if observations else None
        ),
    )
    return EvidenceSnapshot.from_ledger(
        ledger,
        evaluation_at=evaluation_at,
        purge_now=purge_now,
    )


def _fresh_identity_pair(
    *,
    stay_place_id: str = STAY_PLACE_ID,
) -> tuple[FactObservation, FactObservation]:
    policies = _policies()
    return (
        _identity_observation(
            policies,
            location_id=STAY_LOCATION,
            place_id=stay_place_id,
        ),
        _identity_observation(
            policies,
            location_id=ANCHOR_LOCATION,
            place_id=ANCHOR_PLACE_ID,
        ),
    )


def _location_candidate(
    *,
    location_id: str = STAY_LOCATION,
    label: str = PRIVATE_LABEL,
    amount: int | None = PRIVATE_PRICE,
    authority: IntentAuthority = IntentAuthority.USER_STATED,
):
    draft = LodgingIntentDraft(
        kind=LodgingKind.HOTEL,
        label=label,
        location=LocationHint(
            kind=LocationHintKind.LOCATION_ID,
            label=label,
            location_id=location_id,
            country_code="jp",
        ),
        check_in=CHECK_IN,
        check_out=CHECK_OUT,
        price_amount_minor=amount,
        currency="JPY" if amount is not None else None,
        price_basis=PriceBasis.TOTAL if amount is not None else None,
        price_is_estimate=False if amount is not None else None,
    )
    return bind_lodging_candidate(draft, authority=authority)


def _candidate_with_hint(hint: LocationHint):
    return bind_lodging_candidate(
        LodgingIntentDraft(
            kind=LodgingKind.UNSPECIFIED,
            label=PRIVATE_LABEL,
            location=hint,
            check_in=CHECK_IN,
            check_out=CHECK_OUT,
        )
    )


def _probe(candidate_id: str, *, basis_request=None):
    return LodgingRouteProbe(
        candidate_id=candidate_id,
        anchor_location_id=ANCHOR_LOCATION,
        direction=LodgingRouteDirection.FROM_LODGING,
        mode=RouteMode.WALKING,
        departure_at=DEPARTURE.isoformat(),
        basis_request=basis_request,
    )


def _route_observation(
    request,
    *,
    duration_min: float = 15,
    distance_km: float = 1.2,
    retrieved_at: datetime = NOW + timedelta(minutes=1),
    valid_until: datetime = NOW + timedelta(hours=2),
) -> FactObservation:
    return FactObservation(
        key=request.provider_request.fact_keys[0],
        value=FactValue.from_payload(
            FactKind.ROUTE_ESTIMATE,
            {
                "mode": request.mode.value,
                "duration_min": duration_min,
                "distance_km": distance_km,
                "departure_at": request.departure_at,
                "arrival_at": (
                    datetime.fromisoformat(request.departure_at)
                    + timedelta(minutes=duration_min)
                ).isoformat(),
                "warning_codes": ["walking_route_beta"],
            },
        ),
        provenance=ProviderProvenance(
            provider_id="google-routes",
            adapter_id="google-routes",
            adapter_version="v1",
            request_fingerprint=(
                request.provider_request.request_fingerprint
            ),
            retention_policy_id="google-route-runtime-v1",
            attributions=(("Google Maps", None),),
        ),
        retrieved_at=retrieved_at,
        valid_until=valid_until,
        purge_at=NOW + timedelta(hours=12),
        confidence=1.0,
    )


def _alternate_route_observation(
    request,
) -> tuple[ProviderPolicy, FactObservation]:
    policy = ProviderPolicy(
        policy_id="fixture-route-memory-v1",
        provider_id="fixture-routes",
        adapter_id="fixture-routes",
        adapter_version="v1",
        contract_region="test",
        allowed_fact_kinds=(FactKind.ROUTE_ESTIMATE,),
        allowed_value_fields=(
            "arrival_at",
            "departure_at",
            "distance_km",
            "duration_min",
            "fallback_from_mode",
            "mode",
            "static_duration_min",
            "warning_codes",
        ),
        allowed_operations=("compute-route",),
        persistence=EvidencePersistence.MEMORY_ONLY,
        max_validity_seconds=24 * 60 * 60,
        max_retention_seconds=24 * 60 * 60,
        required_attribution_labels=("Fixture Routes",),
    )
    observation = FactObservation(
        key=request.provider_request.fact_keys[0],
        value=FactValue.from_payload(
            FactKind.ROUTE_ESTIMATE,
            {
                "mode": request.mode.value,
                "duration_min": 20,
                "distance_km": 1.4,
                "departure_at": request.departure_at,
                "arrival_at": (
                    datetime.fromisoformat(request.departure_at)
                    + timedelta(minutes=20)
                ).isoformat(),
            },
        ),
        provenance=ProviderProvenance(
            provider_id=policy.provider_id,
            adapter_id=policy.adapter_id,
            adapter_version=policy.adapter_version,
            request_fingerprint="b" * 64,
            retention_policy_id=policy.policy_id,
            attributions=(("Fixture Routes", None),),
        ),
        retrieved_at=NOW + timedelta(minutes=1),
        valid_until=NOW + timedelta(hours=2),
        purge_at=NOW + timedelta(hours=12),
        confidence=1.0,
    )
    return policy, observation


def _route_fixture():
    identities = _fresh_identity_pair()
    basis_snapshot = _snapshot(*identities)
    candidate = _location_candidate()
    pending = assess_lodging_evidence(
        candidates=(candidate,),
        snapshot=basis_snapshot,
        route_probes=(_probe(candidate.candidate_id),),
    )
    request = pending.pending_route_requests[0]
    route = _route_observation(request)
    current = _snapshot(
        *identities,
        route,
        evaluation_at=NOW + timedelta(minutes=2),
        purge_now=NOW + timedelta(minutes=2),
    )
    return candidate, request, current, identities, route


class _EvidenceSource:
    def __init__(self, result: EvidenceStoreResult) -> None:
        self.result = result

    def load(self) -> EvidenceStoreResult:
        return self.result


class _RouteTransport:
    def __init__(self) -> None:
        self.calls = 0

    def send(
        self,
        request,
        *,
        connect_timeout_s: float,
        read_timeout_s: float,
    ) -> GoogleRoutesHttpResponse:
        del request, connect_timeout_s, read_timeout_s
        self.calls += 1
        return GoogleRoutesHttpResponse(
            status_code=200,
            body=(
                b'{"routes":[{"duration":"900s",'
                b'"distanceMeters":1200}]}'
            ),
        )


def _session_with_identities():
    observations = _fresh_identity_pair()
    policies = _policies()
    ledger = EvidenceLedger(
        policies,
        observations,
        generation=1,
        _token=facts_module._LEDGER_TOKEN,
    )
    source = _EvidenceSource(
        EvidenceStoreResult(
            success=True,
            status="loaded",
            action="load",
            ledger=ledger,
            current_revision="c" * 64,
            generation=ledger.generation,
            purge_checked_at=NOW,
        )
    )
    session = EvidenceSession(
        source,
        clock=lambda: NOW + timedelta(minutes=1),
    )
    snapshot = session.load().snapshot(evaluation_at=NOW)
    return session, snapshot


class LodgingEvidenceContractTests(unittest.TestCase):
    def assert_contract_error(self, code: str, function) -> None:
        with self.assertRaises(FactContractError) as caught:
            function()
        self.assertEqual(code, caught.exception.code)

    def test_public_api_exports_projection_but_no_promotion_or_scoring(
        self,
    ) -> None:
        for name in (
            "assess_lodging_evidence",
            "LodgingComparisonCandidate",
            "LodgingRouteProbe",
            "normalize_serpapi_hotel_discovery",
            "LodgingDiscoveryRequest",
        ):
            self.assertTrue(hasattr(trip_planner, name), name)
        for name in (
            "promote_lodging_evidence",
            "select_lodging_candidate",
            "score_lodging_candidates",
            "apply_lodging_candidate",
        ):
            self.assertFalse(hasattr(trip_planner, name), name)

    def test_fresh_identity_wraps_but_never_promotes_candidate(self) -> None:
        candidate = _location_candidate()
        result = assess_lodging_evidence(
            candidates=(candidate,),
            snapshot=_snapshot(*_fresh_identity_pair()),
        )
        comparison = result.candidates[0]

        self.assertEqual(
            LodgingIdentityDisposition.VERIFIED,
            comparison.identity.disposition,
        )
        self.assertEqual(
            LodgingComparisonCapability.IDENTITY_BOUND,
            comparison.capability,
        )
        self.assertEqual(DecisionState.CANDIDATE, candidate.decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, candidate.evidence_state)
        self.assertEqual((), candidate.evidence_refs)
        self.assertEqual(
            EvidenceState.UNVERIFIED,
            comparison.to_dict()["candidate_evidence_state"],
        )

    def test_private_hint_kinds_never_become_trusted_endpoints(self) -> None:
        hints = (
            LocationHint(
                kind=LocationHintKind.PLACE_ID,
                label=PRIVATE_LABEL,
                provider_place_id=STAY_PLACE_ID,
                country_code="jp",
            ),
            LocationHint(
                kind=LocationHintKind.ADDRESS,
                label=PRIVATE_LABEL,
                input_text=PRIVATE_ADDRESS,
                country_code="jp",
            ),
            LocationHint(
                kind=LocationHintKind.COORDINATES,
                label=PRIVATE_LABEL,
                latitude=35.1,
                longitude=139.1,
                country_code="jp",
            ),
            LocationHint(
                kind=LocationHintKind.AREA,
                label=PRIVATE_LABEL,
                input_text="Private approximate area",
                country_code="jp",
            ),
        )
        expected = (
            LodgingIdentityDisposition.UNREVIEWED_PROVIDER_ID,
            LodgingIdentityDisposition.ADDRESS_NEEDS_IDENTITY,
            LodgingIdentityDisposition.COORDINATES_NEED_IDENTITY,
            LodgingIdentityDisposition.APPROXIMATE_AREA,
        )
        for hint, disposition in zip(hints, expected, strict=True):
            with self.subTest(disposition=disposition):
                result = assess_lodging_evidence(
                    candidates=(_candidate_with_hint(hint),),
                    snapshot=_snapshot(),
                )
                identity = result.candidates[0].identity
                self.assertEqual(disposition, identity.disposition)
                self.assertIsNone(identity.endpoint)
                self.assertEqual(
                    EvidenceState.UNVERIFIED,
                    identity.evidence_state,
                )

    def test_missing_and_stale_identity_are_explicit(self) -> None:
        candidate = _location_candidate()
        missing = assess_lodging_evidence(
            candidates=(candidate,),
            snapshot=_snapshot(),
        )
        self.assertEqual(
            LodgingIdentityDisposition.MISSING,
            missing.candidates[0].identity.disposition,
        )

        policies = _policies()
        stale_observation = _identity_observation(
            policies,
            location_id=STAY_LOCATION,
            place_id=STAY_PLACE_ID,
            retrieved_at=NOW - timedelta(days=2),
            valid_until=NOW - timedelta(days=1),
        )
        stale = assess_lodging_evidence(
            candidates=(candidate,),
            snapshot=_snapshot(stale_observation),
        )
        self.assertEqual(
            LodgingIdentityDisposition.STALE,
            stale.candidates[0].identity.disposition,
        )
        self.assertEqual(
            EvidenceState.STALE,
            stale.candidates[0].identity.evidence_state,
        )

    def test_initial_route_probe_builds_request_without_claiming_evidence(
        self,
    ) -> None:
        identities = _fresh_identity_pair()
        candidate = _location_candidate()
        result = assess_lodging_evidence(
            candidates=(candidate,),
            snapshot=_snapshot(*identities),
            route_probes=(_probe(candidate.candidate_id),),
        )
        route = result.candidates[0].routes[0]

        self.assertEqual(
            LodgingRouteDisposition.BASIS_RECEIPT_REQUIRED,
            route.disposition,
        )
        self.assertEqual(EvidenceState.UNVERIFIED, route.evidence_state)
        self.assertEqual(1, len(result.pending_route_requests))
        self.assertIsNone(route.duration_min)
        self.assertIsNone(route.distance_km)

    def test_duplicate_semantic_requests_are_deduped_for_route_batch(
        self,
    ) -> None:
        session, snapshot = _session_with_identities()
        first = _location_candidate(label="First private label")
        second = _location_candidate(label="Second private label")
        result = assess_lodging_evidence(
            candidates=(first, second),
            snapshot=snapshot,
            route_probes=(
                _probe(first.candidate_id),
                _probe(second.candidate_id),
            ),
        )

        self.assertEqual(2, sum(len(item.routes) for item in result.candidates))
        self.assertEqual(1, len(result.pending_route_requests))
        transport = _RouteTransport()
        batch = execute_google_route_batch(
            result.pending_route_requests,
            transport,
            session=session,
            attempt_budget=RouteAttemptBudget(1),
            clock=lambda: NOW + timedelta(minutes=1),
        )
        self.assertEqual(1, transport.calls)
        self.assertEqual(1, len(batch.executions))

    def test_exact_receipt_and_current_snapshot_make_route_usable(self) -> None:
        candidate, request, current, _identities, route_observation = (
            _route_fixture()
        )
        result = assess_lodging_evidence(
            candidates=(candidate,),
            snapshot=current,
            route_probes=(
                _probe(
                    candidate.candidate_id,
                    basis_request=request,
                ),
            ),
        )
        route = result.candidates[0].routes[0]

        self.assertEqual(
            LodgingRouteDisposition.VERIFIED,
            route.disposition,
        )
        self.assertEqual(EvidenceState.VERIFIED, route.evidence_state)
        self.assertEqual(15.0, route.duration_min)
        self.assertEqual(1.2, route.distance_km)
        self.assertEqual(
            route_observation.observation_id,
            route.used_observation_id,
        )
        self.assertEqual((), result.pending_route_requests)
        self.assertEqual(
            LodgingComparisonCapability.ROUTE_BOUND,
            result.candidates[0].capability,
        )
        self.assertIn(
            route_observation.observation_id,
            result.basis.used_observation_ids,
        )

    def test_stale_route_is_not_exposed_and_gets_refresh_request(self) -> None:
        identities = _fresh_identity_pair()
        basis = _snapshot(*identities)
        candidate = _location_candidate()
        pending = assess_lodging_evidence(
            candidates=(candidate,),
            snapshot=basis,
            route_probes=(_probe(candidate.candidate_id),),
        )
        request = pending.pending_route_requests[0]
        stale = _route_observation(
            request,
            retrieved_at=NOW + timedelta(minutes=1),
            valid_until=NOW + timedelta(minutes=2),
        )
        current = _snapshot(
            *identities,
            stale,
            evaluation_at=NOW + timedelta(minutes=3),
            purge_now=NOW + timedelta(minutes=3),
        )
        result = assess_lodging_evidence(
            candidates=(candidate,),
            snapshot=current,
            route_probes=(
                _probe(candidate.candidate_id, basis_request=request),
            ),
        )
        route = result.candidates[0].routes[0]

        self.assertEqual(LodgingRouteDisposition.STALE, route.disposition)
        self.assertEqual(EvidenceState.STALE, route.evidence_state)
        self.assertIsNone(route.duration_min)
        self.assertEqual(1, len(result.pending_route_requests))

    def test_conflicted_route_never_exposes_a_numeric_winner(self) -> None:
        identities = _fresh_identity_pair()
        basis = _snapshot(*identities)
        candidate = _location_candidate()
        pending = assess_lodging_evidence(
            candidates=(candidate,),
            snapshot=basis,
            route_probes=(_probe(candidate.candidate_id),),
        )
        request = pending.pending_route_requests[0]
        google_route = _route_observation(request)
        alternate_policy, alternate_route = (
            _alternate_route_observation(request)
        )
        policies = ProviderPolicyRegistry(
            policies=(
                *_policies().policies,
                alternate_policy,
            )
        )
        current = _snapshot(
            *identities,
            google_route,
            alternate_route,
            evaluation_at=NOW + timedelta(minutes=2),
            purge_now=NOW + timedelta(minutes=2),
            policies=policies,
        )
        result = assess_lodging_evidence(
            candidates=(candidate,),
            snapshot=current,
            route_probes=(
                _probe(candidate.candidate_id, basis_request=request),
            ),
        )
        route = result.candidates[0].routes[0]

        self.assertEqual(
            LodgingRouteDisposition.CONFLICTED,
            route.disposition,
        )
        self.assertEqual(EvidenceState.CONFLICTED, route.evidence_state)
        self.assertIsNone(route.duration_min)
        self.assertIsNone(route.distance_km)
        self.assertEqual(
            LodgingComparisonCapability.BLOCKED,
            result.candidates[0].capability,
        )
        self.assertEqual(1, len(result.pending_route_requests))

    def test_route_without_matching_receipt_is_never_reused(self) -> None:
        candidate, _request, current, _identities, _route = _route_fixture()
        result = assess_lodging_evidence(
            candidates=(candidate,),
            snapshot=current,
            route_probes=(_probe(candidate.candidate_id),),
        )
        route = result.candidates[0].routes[0]

        self.assertEqual(
            LodgingRouteDisposition.BASIS_RECEIPT_REQUIRED,
            route.disposition,
        )
        self.assertIsNone(route.duration_min)
        self.assertEqual(1, len(result.pending_route_requests))

    def test_endpoint_observation_drift_invalidates_old_route(self) -> None:
        candidate, request, _current, _identities, route = _route_fixture()
        changed_identities = _fresh_identity_pair(
            stay_place_id="ChIJ-changed-stay-sentinel",
        )
        changed = _snapshot(
            *changed_identities,
            route,
            evaluation_at=NOW + timedelta(minutes=2),
            purge_now=NOW + timedelta(minutes=2),
        )
        result = assess_lodging_evidence(
            candidates=(candidate,),
            snapshot=changed,
            route_probes=(
                _probe(candidate.candidate_id, basis_request=request),
            ),
        )

        route_result = result.candidates[0].routes[0]
        self.assertEqual(
            LodgingRouteDisposition.BASIS_CHANGED,
            route_result.disposition,
        )
        self.assertIsNone(route_result.duration_min)
        self.assertEqual(1, len(result.pending_route_requests))

    def test_receipt_for_another_candidate_fails_closed(self) -> None:
        candidate, request, current, identities, route = _route_fixture()
        other_identity = _identity_observation(
            _policies(),
            location_id=OTHER_LOCATION,
            place_id="ChIJ-other-private-stay",
        )
        other = _location_candidate(location_id=OTHER_LOCATION)
        expanded = _snapshot(
            *identities,
            other_identity,
            route,
            evaluation_at=current.evaluation_at,
            purge_now=current.purge_checked_at,
        )
        self.assert_contract_error(
            "EVIDENCE_BINDING_MISMATCH",
            lambda: assess_lodging_evidence(
                candidates=(candidate, other),
                snapshot=expanded,
                route_probes=(
                    _probe(other.candidate_id, basis_request=request),
                ),
            ),
        )

    def test_assessment_rejects_snapshot_drift(self) -> None:
        identities = _fresh_identity_pair()
        candidate = _location_candidate()
        original = _snapshot(*identities)
        result = assess_lodging_evidence(
            candidates=(candidate,),
            snapshot=original,
        )
        changed = _snapshot(
            *identities,
            evaluation_at=NOW + timedelta(minutes=1),
            purge_now=NOW + timedelta(minutes=1),
        )

        result.require_snapshot(original)
        self.assert_contract_error(
            "EVIDENCE_REVISION_CHANGED",
            lambda: result.require_snapshot(changed),
        )

    def test_permutation_is_deterministic_within_one_intake_session(self) -> None:
        policies = _policies()
        stay_a, anchor = _fresh_identity_pair()
        stay_b = _identity_observation(
            policies,
            location_id=OTHER_LOCATION,
            place_id="ChIJ-other-private-stay",
        )
        snapshot = _snapshot(stay_a, anchor, stay_b)
        first = _location_candidate()
        second = _location_candidate(location_id=OTHER_LOCATION)
        probe_a = _probe(first.candidate_id)
        probe_b = _probe(second.candidate_id)

        one = assess_lodging_evidence(
            candidates=(first, second),
            snapshot=snapshot,
            route_probes=(probe_a, probe_b),
        )
        two = assess_lodging_evidence(
            candidates=(second, first),
            snapshot=snapshot,
            route_probes=(probe_b, probe_a),
        )
        self.assertEqual(one.assessment_id, two.assessment_id)
        self.assertEqual(one.to_dict(), two.to_dict())

    def test_duplicate_and_unknown_inputs_fail_before_projection(self) -> None:
        candidate = _location_candidate()
        snapshot = _snapshot(*_fresh_identity_pair())
        probe = _probe(candidate.candidate_id)
        with self.assertRaises(ValueError):
            assess_lodging_evidence(
                candidates=(candidate, candidate),
                snapshot=snapshot,
            )
        with self.assertRaises(ValueError):
            assess_lodging_evidence(
                candidates=(candidate,),
                snapshot=snapshot,
                route_probes=(probe, probe),
            )
        with self.assertRaises(ValueError):
            assess_lodging_evidence(
                candidates=(candidate,),
                snapshot=snapshot,
                route_probes=(_probe("f" * 64),),
            )

    def test_bounds_are_enforced_before_candidate_fan_out(self) -> None:
        candidates = tuple(
            _location_candidate(
                location_id=f"stay-{index}",
                label=f"Stay {index}",
                amount=None,
            )
            for index in range(257)
        )
        with self.assertRaises(ValueError):
            assess_lodging_evidence(
                candidates=candidates,
                snapshot=_snapshot(),
            )

    def test_safe_views_hide_location_route_time_label_and_price(self) -> None:
        candidate, request, current, _identities, _route = _route_fixture()
        result = assess_lodging_evidence(
            candidates=(candidate,),
            snapshot=current,
            route_probes=(
                _probe(candidate.candidate_id, basis_request=request),
            ),
        )
        safe = (
            repr(result)
            + repr(result.basis)
            + repr(result.candidates[0])
            + repr(result.candidates[0].identity)
            + repr(result.candidates[0].routes[0])
            + repr(result.candidates[0].routes[0].probe)
            + json.dumps(result.to_dict(), sort_keys=True)
        )
        for private in (
            PRIVATE_LABEL,
            PRIVATE_ADDRESS,
            STAY_LOCATION,
            ANCHOR_LOCATION,
            STAY_PLACE_ID,
            ANCHOR_PLACE_ID,
            DEPARTURE.isoformat(),
            str(PRIVATE_PRICE),
        ):
            self.assertNotIn(private, safe)

    def test_provider_discovery_authority_stays_candidate_only(self) -> None:
        candidate = _location_candidate(
            authority=IntentAuthority.PROVIDER_DISCOVERED,
        )
        result = assess_lodging_evidence(
            candidates=(candidate,),
            snapshot=_snapshot(*_fresh_identity_pair()),
        )
        projected = result.candidates[0]
        self.assertEqual(DecisionState.CANDIDATE, candidate.decision_state)
        self.assertEqual(EvidenceState.UNVERIFIED, candidate.evidence_state)
        self.assertEqual(
            IntentAuthority.PROVIDER_DISCOVERED,
            projected.candidate.authority,
        )


if __name__ == "__main__":
    unittest.main()
