"""Offline end-to-end contracts for Phase 4.5C lodging recommendations."""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone

from trip_planner.availability import (
    ActivityAvailability,
    AvailabilityDisposition,
    AvailabilityInterval,
)
from trip_planner.composition import (
    ComposedTripState,
    EvidenceBinding,
    project_activity_availability,
)
from trip_planner.facts import (
    FactContractError,
    FactKey,
    FactKind,
    FactObservation,
    FactValue,
    ProviderProvenance,
)
from trip_planner.lodging import (
    IntentAuthority,
    LocationHint,
    LocationHintKind,
    LodgingIntentDraft,
    LodgingKind,
    ReportedDecisionClaim,
    bind_lodging_candidate,
)
from trip_planner.lodging_evidence import (
    LodgingComparisonAssessment,
    LodgingRouteDirection,
    LodgingRouteProbe,
    assess_lodging_evidence,
)
from trip_planner.lodging_itinerary import (
    LodgingDayAnchor,
    LodgingItineraryOption,
    LodgingItineraryStatus,
    LodgingRouteUse,
    assess_lodging_itineraries,
    build_lodging_itinerary_problem,
)
from trip_planner.models import (
    Activity,
    DaySpec,
    DecisionState,
    EvidenceState,
    Flexibility,
    TravelEstimate,
    TripState,
)
from trip_planner.routes import RouteMode
from trip_planner.scheduler import solve_schedule
from trip_planner.scheduling import (
    ScheduleContractError,
    ScheduleStatus,
    trip_state_digest,
)

from tests.test_phase45_lodging_evidence import (
    NOW,
    _identity_observation,
    _policies,
    _route_observation,
    _snapshot,
)


UTC = timezone.utc
EVALUATED = NOW + timedelta(minutes=2)
REVISION = "1" * 64
PRIVATE_SENTINEL = "private-lodging-label-never-serialize"


@dataclass(frozen=True)
class _Fixture:
    candidates: tuple
    comparison: LodgingComparisonAssessment
    snapshot: object
    route_probes: tuple[LodgingRouteProbe, ...]
    route_observations: tuple
    composed: ComposedTripState
    options: tuple[LodgingItineraryOption, ...]


def _candidate(
    location_id: str,
    *,
    label: str,
    check_in: date,
    check_out: date,
    kind: LodgingKind = LodgingKind.HOTEL,
    reported_state: DecisionState | None = None,
):
    reported = (
        ReportedDecisionClaim(
            decision_state=reported_state,
            source_ref="private-user-turn-ref",
        )
        if reported_state is not None
        else None
    )
    return bind_lodging_candidate(
        LodgingIntentDraft(
            kind=kind,
            label=label,
            location=LocationHint(
                kind=LocationHintKind.LOCATION_ID,
                label=label,
                location_id=location_id,
                country_code="jp",
            ),
            check_in=check_in,
            check_out=check_out,
            reported_decision=reported,
        ),
        authority=IntentAuthority.USER_STATED,
    )


def _binding(snapshot, observations: tuple) -> EvidenceBinding:
    return EvidenceBinding(
        policy_registry_revision=snapshot.policies.revision,
        store_revision=snapshot.store_revision,
        evidence_revision=snapshot.evidence_revision,
        outcome_revision=snapshot.outcome_revision,
        evaluation_at=snapshot.evaluation_at,
        purge_checked_at=snapshot.purge_checked_at,
        snapshot_id=snapshot.snapshot_id,
        used_observation_ids=tuple(
            item.observation_id for item in observations
        ),
    )


def _current_hours_observation(
    *,
    location_id: str,
    provider_place_id: str,
    target_date: date,
) -> FactObservation:
    key = FactKey(
        kind=FactKind.PLACE_OPENING_HOURS,
        subject_ids=(location_id,),
        qualifiers=(
            ("basis", "current"),
            ("identity_provider", "google-places"),
            ("provider_place_id", provider_place_id),
            ("target_end", target_date.isoformat()),
            ("target_start", target_date.isoformat()),
        ),
    )
    coverage_start = EVALUATED.astimezone(
        timezone(timedelta(hours=9))
    ).date()
    coverage_dates = tuple(
        coverage_start + timedelta(days=offset)
        for offset in range(7)
    )
    return FactObservation(
        key=key,
        value=FactValue.from_payload(
            FactKind.PLACE_OPENING_HOURS,
            {
                "provider_place_id": provider_place_id,
                "timezone": "Asia/Tokyo",
                "basis": "current",
                "coverage_start": coverage_dates[0].isoformat(),
                "coverage_end": coverage_dates[-1].isoformat(),
                "intervals": [
                    {
                        "start_at": "2026-08-02T08:00:00Z",
                        "end_at": "2026-08-02T12:00:00Z",
                    }
                ],
                "closed_dates": [
                    item.isoformat()
                    for item in coverage_dates
                    if item != target_date
                ],
            },
        ),
        provenance=ProviderProvenance(
            provider_id="google-places",
            adapter_id="google-places",
            adapter_version="v1",
            request_fingerprint="e" * 64,
            retention_policy_id="google-place-hours-runtime-v1",
            provider_record_id=provider_place_id,
            attributions=(("Google Maps", None),),
        ),
        retrieved_at=EVALUATED - timedelta(minutes=1),
        valid_until=EVALUATED + timedelta(hours=1),
        purge_at=EVALUATED + timedelta(hours=12),
        confidence=1.0,
    )


def _request_for_probe(probe, requests, candidate_by_id):
    lodging_id = candidate_by_id[
        probe.candidate_id
    ].draft.location.location_id
    expected = (
        (lodging_id, probe.anchor_location_id)
        if probe.direction is LodgingRouteDirection.FROM_LODGING
        else (probe.anchor_location_id, lodging_id)
    )
    matches = tuple(
        request
        for request in requests
        if (
            request.origin.location_id,
            request.destination.location_id,
        )
        == expected
        and request.mode is probe.mode
        and request.departure_at == probe.departure_at
    )
    if len(matches) != 1:
        raise AssertionError("fixture route request is not unique")
    return matches[0]


def _comparison_fixture(
    candidates: tuple,
    probe_specs: tuple[tuple[str, str, LodgingRouteDirection, RouteMode, str, float], ...],
    *,
    extra_observations: tuple = (),
):
    candidate_by_id = {
        item.candidate_id: item for item in candidates
    }
    locations = {
        item.draft.location.location_id for item in candidates
    }.union(spec[1] for spec in probe_specs)
    identities = tuple(
        _identity_observation(
            _policies(),
            location_id=location_id,
            place_id=f"ChIJ-{location_id}",
        )
        for location_id in sorted(locations)
    )
    bare_probes = tuple(
        LodgingRouteProbe(
            candidate_id=candidate_id,
            anchor_location_id=anchor_id,
            direction=direction,
            mode=mode,
            departure_at=departure_at,
        )
        for (
            candidate_id,
            anchor_id,
            direction,
            mode,
            departure_at,
            _duration,
        ) in probe_specs
    )
    pending = assess_lodging_evidence(
        candidates=candidates,
        snapshot=_snapshot(*identities),
        route_probes=bare_probes,
    )
    probes = tuple(
        replace(
            probe,
            basis_request=_request_for_probe(
                probe,
                pending.pending_route_requests,
                candidate_by_id,
            ),
        )
        for probe in bare_probes
    )
    duration_by_semantic_key = {
        (
            candidate_id,
            anchor_id,
            direction,
            mode,
        ): duration
        for (
            candidate_id,
            anchor_id,
            direction,
            mode,
            departure_at,
            duration,
        ) in probe_specs
    }
    observations_by_request = {}
    for probe in probes:
        request = probe.basis_request
        assert request is not None
        semantic_key = (
            probe.candidate_id,
            probe.anchor_location_id,
            probe.direction,
            probe.mode,
        )
        observations_by_request.setdefault(
            request.provider_request.request_fingerprint,
            _route_observation(
                request,
                duration_min=duration_by_semantic_key[semantic_key],
            ),
        )
    observations = tuple(
        observations_by_request[key]
        for key in sorted(observations_by_request)
    )
    snapshot = _snapshot(
        *identities,
        *observations,
        *extra_observations,
        evaluation_at=EVALUATED,
        purge_now=EVALUATED,
    )
    comparison = assess_lodging_evidence(
        candidates=candidates,
        snapshot=snapshot,
        route_probes=probes,
    )
    return (
        comparison,
        snapshot,
        probes,
        observations + extra_observations,
    )


def _observation_for_probe(probe, observations):
    assert probe.basis_request is not None
    key = probe.basis_request.provider_request.fact_keys[0]
    matches = tuple(
        item for item in observations if item.key == key
    )
    if len(matches) != 1:
        raise AssertionError("fixture route observation is not unique")
    return matches[0]


def _travel_estimate(
    probe: LodgingRouteProbe,
    observation,
    *,
    day_id: str,
    buffer_min: int = 0,
) -> TravelEstimate:
    request = probe.basis_request
    assert request is not None
    return TravelEstimate(
        from_location_id=request.origin.location_id,
        to_location_id=request.destination.location_id,
        mode=request.mode.value,
        duration_min=observation.value.payload["duration_min"],
        day_id=day_id,
        buffer_min=buffer_min,
        evidence_state=EvidenceState.VERIFIED,
        fresh_until=observation.valid_until,
        evidence_ref=f"fact:{observation.observation_id}",
        source=f"fact:{observation.observation_id}",
        query_departure_at=datetime.fromisoformat(probe.departure_at),
    )


def _composed(
    state: TripState,
    snapshot,
    observations: tuple,
    *,
    activity_availability: tuple[ActivityAvailability, ...] = (),
) -> ComposedTripState:
    canonical = replace(state, travel_estimates=())
    return ComposedTripState(
        state=state,
        trip_id="phase45-fixture",
        plan_revision=state.revision,
        canonical_state_digest=trip_state_digest(canonical),
        composed_state_digest=trip_state_digest(state),
        evidence=_binding(snapshot, observations),
        activity_availability=activity_availability,
    )


def _route_probe(
    comparison: LodgingComparisonAssessment,
    candidate_id: str,
    direction: LodgingRouteDirection,
    *,
    anchor_location_id: str | None = None,
) -> LodgingRouteProbe:
    candidate = next(
        item
        for item in comparison.candidates
        if item.candidate_id == candidate_id
    )
    matches = tuple(
        item.probe
        for item in candidate.routes
        if item.probe.direction is direction
        and (
            anchor_location_id is None
            or item.probe.anchor_location_id == anchor_location_id
        )
    )
    if len(matches) != 1:
        raise AssertionError("fixture comparison route is not unique")
    return matches[0]


def _option(
    *,
    composed: ComposedTripState,
    comparison: LodgingComparisonAssessment,
    snapshot,
    anchors: tuple[LodgingDayAnchor, ...],
    route_uses: tuple[LodgingRouteUse, ...],
) -> LodgingItineraryOption:
    problem = build_lodging_itinerary_problem(
        composed=composed,
        comparison=comparison,
        snapshot=snapshot,
        anchors=anchors,
    )
    result = solve_schedule(problem)
    if result.status is not ScheduleStatus.SOLVED:
        raise AssertionError(
            f"fixture schedule did not solve: {result.status.value}"
        )
    return LodgingItineraryOption(
        problem=problem,
        result=result,
        anchors=anchors,
        route_uses=route_uses,
    )


def _busan_fixture(
    *,
    durations: tuple[float, float] = (12, 24),
    reported_first: bool = False,
    fake_availability: bool = False,
) -> _Fixture:
    stay_start = date(2026, 8, 1)
    stay_end = date(2026, 8, 4)
    candidates = (
        _candidate(
            "busan-hotel-a",
            label=PRIVATE_SENTINEL,
            check_in=stay_start,
            check_out=stay_end,
            reported_state=(
                DecisionState.BOOKED if reported_first else None
            ),
        ),
        _candidate(
            "busan-hotel-b",
            label="private-busan-hotel-b",
            check_in=stay_start,
            check_out=stay_end,
        ),
    )
    arrival_location = "busan-arrival-terminal"
    dinner_location = "busan-booked-dinner"
    arrival_to_stay_at = datetime(
        2026, 8, 1, 10, 30, tzinfo=timezone(timedelta(hours=9))
    ).isoformat()
    dinner_outbound_at = datetime(
        2026, 8, 2, 8, 0, tzinfo=timezone(timedelta(hours=9))
    ).isoformat()
    dinner_return_at = datetime(
        2026, 8, 2, 18, 30, tzinfo=timezone(timedelta(hours=9))
    ).isoformat()
    probe_specs = tuple(
        spec
        for candidate, duration in zip(candidates, durations, strict=True)
        for spec in (
            (
                candidate.candidate_id,
                arrival_location,
                LodgingRouteDirection.TO_LODGING,
                RouteMode.WALKING,
                arrival_to_stay_at,
                duration,
            ),
            (
                candidate.candidate_id,
                dinner_location,
                LodgingRouteDirection.FROM_LODGING,
                RouteMode.WALKING,
                dinner_outbound_at,
                duration,
            ),
            (
                candidate.candidate_id,
                dinner_location,
                LodgingRouteDirection.TO_LODGING,
                RouteMode.WALKING,
                dinner_return_at,
                duration,
            ),
        )
    )
    hours = _current_hours_observation(
        location_id=dinner_location,
        provider_place_id=f"ChIJ-{dinner_location}",
        target_date=stay_start + timedelta(days=1),
    )
    comparison, snapshot, probes, observations = _comparison_fixture(
        candidates,
        probe_specs,
        extra_observations=(hours,),
    )
    arrival = Activity(
        activity_id="booked-arrival-boundary",
        day_id="day-1",
        order=0,
        title="booked arrival boundary",
        location_id=arrival_location,
        scheduled_start=time(10),
        duration_min=30,
        decision_state=DecisionState.BOOKED,
        evidence_state=EvidenceState.VERIFIED,
        flexibility=Flexibility.FIXED_TIME,
    )
    dinner = Activity(
        activity_id="booked-dinner",
        day_id="day-2",
        order=0,
        title="booked dinner",
        location_id=dinner_location,
        scheduled_start=time(18),
        duration_min=30,
        decision_state=DecisionState.BOOKED,
        evidence_state=EvidenceState.VERIFIED,
        flexibility=Flexibility.FIXED_TIME,
    )
    days = (
        DaySpec(
            "day-1",
            stay_start,
            timezone="Asia/Tokyo",
            available_start=time(8),
            available_end=time(22),
            start_location_id=arrival_location,
            activity_ids=(arrival.activity_id,),
        ),
        DaySpec(
            "day-2",
            stay_start + timedelta(days=1),
            timezone="Asia/Tokyo",
            available_start=time(8),
            available_end=time(22),
            activity_ids=(dinner.activity_id,),
        ),
        DaySpec(
            "day-3",
            stay_start + timedelta(days=2),
            timezone="Asia/Tokyo",
            available_start=time(8),
            available_end=time(22),
        ),
    )
    estimates = tuple(
        _travel_estimate(
            probe,
            _observation_for_probe(probe, observations),
            day_id=(
                "day-1"
                if probe.anchor_location_id == arrival_location
                else "day-2"
            ),
        )
        for probe in probes
    )
    state = TripState(
        slug="busan-fixture",
        title="Busan fixture",
        timezone="Asia/Tokyo",
        revision=REVISION,
        days=days,
        activities=(arrival, dinner),
        travel_estimates=estimates,
    )
    availability, _used_hours = project_activity_availability(
        state,
        snapshot,
    )
    binding_observations = observations
    if fake_availability:
        fake_id = "f" * 64
        availability = (
            ActivityAvailability(
                activity_id=dinner.activity_id,
                disposition=AvailabilityDisposition.HARD_CURRENT,
                intervals=(
                    AvailabilityInterval(
                        datetime(2026, 8, 2, 8, tzinfo=UTC),
                        datetime(2026, 8, 2, 12, tzinfo=UTC),
                    ),
                ),
                evidence_refs=(f"fact:{fake_id}",),
                fresh_until=EVALUATED + timedelta(days=1),
            ),
        )
        fake_binding = replace(
            _binding(snapshot, observations),
            used_observation_ids=tuple(
                sorted(
                    {
                        *(item.observation_id for item in observations),
                        fake_id,
                    }
                )
            ),
        )
        composed = ComposedTripState(
            state=state,
            trip_id="phase45-fixture",
            plan_revision=state.revision,
            canonical_state_digest=trip_state_digest(
                replace(state, travel_estimates=())
            ),
            composed_state_digest=trip_state_digest(state),
            evidence=fake_binding,
            activity_availability=availability,
        )
    else:
        composed = _composed(
            state,
            snapshot,
            binding_observations,
            activity_availability=availability,
        )

    options = []
    for candidate in candidates:
        anchors = (
            LodgingDayAnchor(
                "day-1",
                end_candidate_id=candidate.candidate_id,
            ),
            LodgingDayAnchor(
                "day-2",
                candidate.candidate_id,
                candidate.candidate_id,
            ),
            LodgingDayAnchor(
                "day-3",
                candidate.candidate_id,
                candidate.candidate_id,
            ),
        )
        arrival_to_stay = _route_probe(
            comparison,
            candidate.candidate_id,
            LodgingRouteDirection.TO_LODGING,
            anchor_location_id=arrival_location,
        )
        dinner_outbound = _route_probe(
            comparison,
            candidate.candidate_id,
            LodgingRouteDirection.FROM_LODGING,
            anchor_location_id=dinner_location,
        )
        dinner_return = _route_probe(
            comparison,
            candidate.candidate_id,
            LodgingRouteDirection.TO_LODGING,
            anchor_location_id=dinner_location,
        )
        options.append(
            _option(
                composed=composed,
                comparison=comparison,
                snapshot=snapshot,
                anchors=anchors,
                route_uses=(
                    LodgingRouteUse(
                        "booked-arrival-to-stay",
                        "day-1",
                        candidate.candidate_id,
                        arrival_to_stay.probe_id,
                    ),
                    LodgingRouteUse(
                        "booked-dinner-outbound",
                        "day-2",
                        candidate.candidate_id,
                        dinner_outbound.probe_id,
                    ),
                    LodgingRouteUse(
                        "booked-dinner-return",
                        "day-2",
                        candidate.candidate_id,
                        dinner_return.probe_id,
                    ),
                ),
            )
        )
    return _Fixture(
        candidates=candidates,
        comparison=comparison,
        snapshot=snapshot,
        route_probes=probes,
        route_observations=observations,
        composed=composed,
        options=tuple(options),
    )


def _hokkaido_fixture(*, projected_buffer: int) -> _Fixture:
    stay_start = date(2026, 12, 10)
    stay_end = date(2026, 12, 13)
    hotel = _candidate(
        "hokkaido-hotel-a",
        label="private-hokkaido-a",
        check_in=stay_start,
        check_out=stay_start + timedelta(days=1),
    )
    ryokan = _candidate(
        "hokkaido-ryokan-b",
        label="private-hokkaido-b",
        check_in=stay_start + timedelta(days=1),
        check_out=stay_end,
        kind=LodgingKind.RYOKAN,
    )
    departure_at = datetime(
        2026, 12, 11, 8, 0, tzinfo=timezone(timedelta(hours=9))
    ).isoformat()
    comparison, snapshot, probes, observations = _comparison_fixture(
        (hotel, ryokan),
        (
            (
                hotel.candidate_id,
                ryokan.draft.location.location_id,
                LodgingRouteDirection.FROM_LODGING,
                RouteMode.DRIVING,
                departure_at,
                180,
            ),
        ),
    )
    check_in = Activity(
        activity_id="booked-ryokan-checkin",
        day_id="day-2",
        order=0,
        title="booked ryokan check-in",
        location_id=ryokan.draft.location.location_id,
        scheduled_start=time(16),
        duration_min=30,
        decision_state=DecisionState.BOOKED,
        evidence_state=EvidenceState.VERIFIED,
        flexibility=Flexibility.FIXED_TIME,
    )
    days = (
        DaySpec(
            "day-1",
            stay_start,
            timezone="Asia/Tokyo",
            available_start=time(8),
            available_end=time(22),
        ),
        DaySpec(
            "day-2",
            stay_start + timedelta(days=1),
            timezone="Asia/Tokyo",
            available_start=time(8),
            available_end=time(22),
            activity_ids=(check_in.activity_id,),
        ),
        DaySpec(
            "day-3",
            stay_start + timedelta(days=2),
            timezone="Asia/Tokyo",
            available_start=time(8),
            available_end=time(22),
        ),
    )
    estimate = _travel_estimate(
        probes[0],
        _observation_for_probe(probes[0], observations),
        day_id="day-2",
        buffer_min=projected_buffer,
    )
    state = TripState(
        slug="hokkaido-fixture",
        title="Hokkaido fixture",
        timezone="Asia/Tokyo",
        revision=REVISION,
        days=days,
        activities=(check_in,),
        travel_estimates=(estimate,),
    )
    composed = _composed(state, snapshot, observations)
    anchors = (
        LodgingDayAnchor(
            "day-1",
            hotel.candidate_id,
            hotel.candidate_id,
        ),
        LodgingDayAnchor(
            "day-2",
            hotel.candidate_id,
            ryokan.candidate_id,
        ),
        LodgingDayAnchor(
            "day-3",
            ryokan.candidate_id,
            ryokan.candidate_id,
        ),
    )
    option = _option(
        composed=composed,
        comparison=comparison,
        snapshot=snapshot,
        anchors=anchors,
        route_uses=(
            LodgingRouteUse(
                "winter-cross-city-transfer",
                "day-2",
                hotel.candidate_id,
                probes[0].probe_id,
                minimum_buffer_min=45,
            ),
        ),
    )
    return _Fixture(
        candidates=(hotel, ryokan),
        comparison=comparison,
        snapshot=snapshot,
        route_probes=probes,
        route_observations=observations,
        composed=composed,
        options=(option,),
    )


def _assess(fixture: _Fixture):
    return assess_lodging_itineraries(
        composed=fixture.composed,
        comparison=fixture.comparison,
        snapshot=fixture.snapshot,
        stay_start=min(
            item.check_in for item in fixture.candidates
        ),
        stay_end=max(
            item.check_out for item in fixture.candidates
        ),
        options=fixture.options,
    )


class Phase45LodgingItineraryContractTests(unittest.TestCase):
    def test_busan_daily_anchors_rank_without_moving_booked_dinner(self):
        fixture = _busan_fixture()
        before_state = fixture.composed.state
        before_candidates = fixture.candidates

        result = _assess(fixture)

        self.assertEqual(LodgingItineraryStatus.RANKED, result.status)
        self.assertEqual(
            fixture.options[0].option_id,
            result.priority_review_option_id,
        )
        for option in fixture.options:
            self.assertEqual(
                AvailabilityDisposition.HARD_CURRENT,
                option.problem.activity_availability[0].disposition,
            )
            assignments = {
                item.activity_id: item
                for item in option.result.candidate.assignments
            }
            self.assertEqual(
                time(10),
                assignments["booked-arrival-boundary"].scheduled_start,
            )
            self.assertEqual(
                time(18),
                assignments["booked-dinner"].scheduled_start,
            )
        self.assertEqual(before_state, fixture.composed.state)
        self.assertEqual(before_candidates, fixture.candidates)
        self.assertTrue(
            all(
                item.decision_state is DecisionState.CANDIDATE
                and item.evidence_state is EvidenceState.UNVERIFIED
                and item.evidence_refs == ()
                for item in fixture.candidates
            )
        )
        self.assertFalse(result.supports_authoritative_use)
        self.assertFalse(hasattr(result, "apply"))
        self.assertFalse(hasattr(result, "promote"))
        self.assertIn(
            "LODGING_PRICE_NOT_SCORED",
            {item.code for item in result.issues},
        )
        self.assertNotIn(PRIVATE_SENTINEL, json.dumps(result.to_dict()))

    def test_busan_permutation_is_stable_and_equal_scores_do_not_choose(self):
        fixture = _busan_fixture(durations=(12, 12))

        forward = _assess(fixture)
        reverse = assess_lodging_itineraries(
            composed=fixture.composed,
            comparison=fixture.comparison,
            snapshot=fixture.snapshot,
            stay_start=date(2026, 8, 1),
            stay_end=date(2026, 8, 4),
            options=tuple(reversed(fixture.options)),
        )

        self.assertEqual(LodgingItineraryStatus.NOT_RANKED, forward.status)
        self.assertIsNone(forward.priority_review_option_id)
        self.assertEqual(forward.assessment_id, reverse.assessment_id)
        self.assertEqual(forward.review_order, reverse.review_order)

    def test_no_options_is_safe_and_option_fanout_is_bounded(self):
        fixture = _busan_fixture()
        empty = assess_lodging_itineraries(
            composed=fixture.composed,
            comparison=fixture.comparison,
            snapshot=fixture.snapshot,
            stay_start=date(2026, 8, 1),
            stay_end=date(2026, 8, 4),
            options=(),
        )
        self.assertEqual(
            LodgingItineraryStatus.NO_LODGING_OPTIONS,
            empty.status,
        )
        self.assertEqual([], empty.to_dict()["options"])
        self.assertFalse(empty.supports_authoritative_use)

        with self.assertRaisesRegex(ValueError, "option limit"):
            assess_lodging_itineraries(
                composed=fixture.composed,
                comparison=fixture.comparison,
                snapshot=fixture.snapshot,
                stay_start=date(2026, 8, 1),
                stay_end=date(2026, 8, 4),
                options=fixture.options * 33,
            )

    def test_route_slots_must_keep_equivalent_semantics(self):
        fixture = _busan_fixture()
        original = fixture.options[1]
        by_slot = {
            item.slot_id: item for item in original.route_uses
        }
        arrival = by_slot["booked-arrival-to-stay"]
        outbound = by_slot["booked-dinner-outbound"]
        swapped = tuple(
            replace(
                item,
                probe_id=(
                    outbound.probe_id
                    if item.slot_id == "booked-arrival-to-stay"
                    else (
                        arrival.probe_id
                        if item.slot_id == "booked-dinner-outbound"
                        else item.probe_id
                    )
                ),
                use_id="",
            )
            for item in original.route_uses
        )
        malformed = LodgingItineraryOption(
            problem=original.problem,
            result=original.result,
            anchors=original.anchors,
            route_uses=swapped,
        )

        with self.assertRaisesRegex(ValueError, "equivalent uses"):
            assess_lodging_itineraries(
                composed=fixture.composed,
                comparison=fixture.comparison,
                snapshot=fixture.snapshot,
                stay_start=date(2026, 8, 1),
                stay_end=date(2026, 8, 4),
                options=(fixture.options[0], malformed),
            )

    def test_missing_receipt_and_reported_booking_never_rank(self):
        fixture = _busan_fixture(reported_first=True)

        reported = _assess(fixture)

        self.assertEqual(
            LodgingItineraryStatus.NEEDS_VERIFICATION,
            reported.status,
        )
        self.assertEqual((), reported.review_order)
        self.assertIn(
            "REPORTED_LODGING_DECISION_AWAITS_CONFIRMATION",
            {item.code for item in reported.issues},
        )

        missing_probes = tuple(
            replace(probe, basis_request=None)
            if index == 0
            else probe
            for index, probe in enumerate(fixture.route_probes)
        )
        missing_comparison = assess_lodging_evidence(
            candidates=fixture.candidates,
            snapshot=fixture.snapshot,
            route_probes=missing_probes,
        )
        missing_options = []
        for original in fixture.options:
            candidate_id = original.candidate_ids[0]
            arrival_to_stay = _route_probe(
                missing_comparison,
                candidate_id,
                LodgingRouteDirection.TO_LODGING,
                anchor_location_id="busan-arrival-terminal",
            )
            dinner_outbound = _route_probe(
                missing_comparison,
                candidate_id,
                LodgingRouteDirection.FROM_LODGING,
                anchor_location_id="busan-booked-dinner",
            )
            dinner_return = _route_probe(
                missing_comparison,
                candidate_id,
                LodgingRouteDirection.TO_LODGING,
                anchor_location_id="busan-booked-dinner",
            )
            missing_options.append(
                _option(
                    composed=fixture.composed,
                    comparison=missing_comparison,
                    snapshot=fixture.snapshot,
                    anchors=original.anchors,
                    route_uses=(
                        LodgingRouteUse(
                            "booked-arrival-to-stay",
                            "day-1",
                            candidate_id,
                            arrival_to_stay.probe_id,
                        ),
                        LodgingRouteUse(
                            "booked-dinner-outbound",
                            "day-2",
                            candidate_id,
                            dinner_outbound.probe_id,
                        ),
                        LodgingRouteUse(
                            "booked-dinner-return",
                            "day-2",
                            candidate_id,
                            dinner_return.probe_id,
                        ),
                    ),
                )
            )
        missing = assess_lodging_itineraries(
            composed=fixture.composed,
            comparison=missing_comparison,
            snapshot=fixture.snapshot,
            stay_start=date(2026, 8, 1),
            stay_end=date(2026, 8, 4),
            options=tuple(missing_options),
        )
        self.assertEqual(
            LodgingItineraryStatus.NEEDS_VERIFICATION,
            missing.status,
        )
        affected = next(
            item
            for item in missing.options
            if "LODGING_ROUTE_EVIDENCE_REQUIRED" in item.issue_codes
        )
        self.assertIsNone(affected.score)
        self.assertIsNone(missing.priority_review_option_id)

    def test_snapshot_drift_and_fake_hours_fail_closed(self):
        fixture = _busan_fixture()
        drifted = _snapshot(
            *fixture.snapshot.observations,
            evaluation_at=EVALUATED + timedelta(days=2),
            purge_now=EVALUATED + timedelta(days=2),
        )
        with self.assertRaises(FactContractError):
            assess_lodging_itineraries(
                composed=fixture.composed,
                comparison=fixture.comparison,
                snapshot=drifted,
                stay_start=date(2026, 8, 1),
                stay_end=date(2026, 8, 4),
                options=fixture.options,
            )

        injected = _busan_fixture(fake_availability=True)
        with self.assertRaisesRegex(
            FactContractError,
            "availability",
        ):
            _assess(injected)

    def test_undeclared_option_state_mutation_is_rejected(self):
        fixture = _busan_fixture()
        original = fixture.options[0]
        mutated_state = replace(
            original.problem.state,
            days=tuple(
                replace(day, available_end=time(23))
                if day.day_id == "day-2"
                else day
                for day in original.problem.state.days
            ),
        )
        mutated_problem = replace(
            original.problem,
            state=mutated_state,
            base_state_digest="",
            problem_id="",
        )
        mutated = LodgingItineraryOption(
            problem=mutated_problem,
            result=solve_schedule(mutated_problem),
            anchors=original.anchors,
            route_uses=original.route_uses,
        )
        with self.assertRaisesRegex(
            ScheduleContractError,
            "undeclared itinerary mutation",
        ):
            assess_lodging_itineraries(
                composed=fixture.composed,
                comparison=fixture.comparison,
                snapshot=fixture.snapshot,
                stay_start=date(2026, 8, 1),
                stay_end=date(2026, 8, 4),
                options=(mutated, fixture.options[1]),
            )

    def test_hokkaido_split_stay_preserves_1600_checkin_and_winter_buffer(self):
        insufficient = _hokkaido_fixture(projected_buffer=20)

        blocked = _assess(insufficient)

        self.assertEqual(
            LodgingItineraryStatus.INFEASIBLE,
            blocked.status,
        )
        self.assertIn(
            "LODGING_TRANSFER_BUFFER_INSUFFICIENT",
            {item.code for item in blocked.issues},
        )

        safe = _hokkaido_fixture(projected_buffer=60)
        accepted = _assess(safe)

        self.assertEqual(LodgingItineraryStatus.RANKED, accepted.status)
        self.assertEqual(
            1,
            accepted.options[0].score.split_stay_count,
        )
        assignment = safe.options[0].result.candidate.assignments[0]
        self.assertEqual(time(16), assignment.scheduled_start)
        self.assertEqual(
            DecisionState.BOOKED,
            safe.options[0].problem.state.activities[0].decision_state,
        )


if __name__ == "__main__":
    unittest.main()
