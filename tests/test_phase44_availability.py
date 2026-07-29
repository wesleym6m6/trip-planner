"""Runtime composition and scheduling tests for opening-hours sidecars."""

from __future__ import annotations

import copy
import unittest
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from tests.test_phase44_place_details import (
    NOW,
    _Fixture,
    _SequenceClock,
    _Transport,
    _current_body,
    _regular_body,
    _response,
)
from trip_planner.availability import (
    ActivityAvailability,
    AvailabilityDisposition,
    AvailabilityInterval,
)
from trip_planner.codec import build_plan
from trip_planner.composition import compose_trip_state
from trip_planner.models import (
    Activity,
    DaySpec,
    DecisionState,
    EvidenceState,
    Flexibility,
    TimeWindow,
    TripState,
)
from trip_planner.place_details import (
    PlaceDetailsAttemptBudget,
    PlaceDetailsKind,
    execute_google_place_details_batch,
)
from trip_planner.timeline import (
    evaluate_composed_timeline,
    evaluate_timeline,
)


UTC = timezone.utc


def _state(
    *,
    flexibility: Flexibility = Flexibility.FIXED_TIME,
    scheduled_start: time = time(10),
    duration_min: int = 120,
    windows: tuple[TimeWindow, ...] = (),
) -> TripState:
    day = DaySpec(
        day_id="day",
        date=date(2026, 10, 3),
        timezone="Asia/Seoul",
        available_start=time(9),
        available_end=time(20),
        start_location_id="venue",
        end_location_id="venue",
        activity_ids=("visit",),
    )
    activity = Activity(
        activity_id="visit",
        day_id="day",
        order=0,
        title="Visit",
        location_id="venue",
        scheduled_start=scheduled_start,
        duration_min=duration_min,
        decision_state=DecisionState.SELECTED,
        flexibility=flexibility,
        evidence_state=EvidenceState.VERIFIED,
        allowed_windows=windows,
    )
    return TripState(
        "trip",
        "Trip",
        "Asia/Seoul",
        (day,),
        (activity,),
        revision="r",
    )


def _interval(
    local_start_hour: int,
    local_end_hour: int,
) -> AvailabilityInterval:
    # 2026-10-03 Asia/Seoul is UTC+09:00.
    return AvailabilityInterval(
        datetime(
            2026,
            10,
            3,
            local_start_hour - 9,
            tzinfo=UTC,
        ),
        datetime(
            2026,
            10,
            3,
            local_end_hour - 9,
            tzinfo=UTC,
        ),
    )


def _hard(
    *intervals: AvailabilityInterval,
    fresh_until: datetime = datetime(2026, 10, 4, tzinfo=UTC),
    refs: tuple[str, ...] = ("fact:hours",),
) -> ActivityAvailability:
    return ActivityAvailability(
        activity_id="visit",
        disposition=AvailabilityDisposition.HARD_CURRENT,
        intervals=tuple(intervals),
        evidence_refs=refs,
        fresh_until=fresh_until,
    )


def _canonical_plan() -> dict[str, object]:
    return build_plan(
        trip_id="phase44-trip",
        generation=1,
        state={
            "trip": {
                "slug": "phase44-trip",
                "title": "Phase 4.4 fixture",
                "timezone": "Asia/Seoul",
                "date_range": "2026-07-29 ~ 2026-07-29",
                "cities": ["Busan"],
            },
            "itinerary": {
                "available_modes": ["walking"],
                "days": [
                    {
                        "day_id": "day-1",
                        "day": 1,
                        "date": "2026-07-29",
                        "timezone": "Asia/Seoul",
                        "available_start": "08:00",
                        "available_end": "20:00",
                        "start_location_id": "venue",
                        "end_location_id": "venue",
                        "places": [
                            {
                                "activity_id": "visit",
                                "title": "Visit",
                                "location_id": "venue",
                                "time": "10:00",
                                "duration_min": 120,
                                "decision_state": "selected",
                                "flexibility": "fixed_time",
                                "evidence_state": "verified",
                            }
                        ],
                        "travel": [],
                    }
                ],
            },
        },
    )


class ActivityAvailabilityTests(unittest.TestCase):
    def test_full_duration_and_explicitly_closed_current_are_hard(
        self,
    ) -> None:
        state = _state()
        short = _hard(_interval(10, 11))
        report = evaluate_timeline(
            state,
            now=datetime(2026, 10, 3, tzinfo=UTC),
            activity_availability=(short,),
        )
        self.assertIn(
            "OPENING_HOURS_VIOLATION",
            {item.code for item in report.issues},
        )
        self.assertEqual((), state.activities[0].allowed_windows)

        closed = _hard()
        closed_report = evaluate_timeline(
            state,
            now=datetime(2026, 10, 3, tzinfo=UTC),
            activity_availability=(closed,),
        )
        self.assertIn(
            "OPENING_HOURS_VIOLATION",
            {item.code for item in closed_report.issues},
        )

    def test_regular_stale_or_expired_only_needs_verification(self) -> None:
        state = _state()
        regular = ActivityAvailability(
            activity_id="visit",
            disposition=AvailabilityDisposition.NEEDS_VERIFICATION,
            evidence_refs=("fact:regular",),
            reason="regular_opening_hours",
        )
        report = evaluate_timeline(
            state,
            activity_availability=(regular,),
        )
        codes = {item.code for item in report.issues}
        self.assertIn("OPENING_HOURS_NEEDS_VERIFICATION", codes)
        self.assertNotIn("OPENING_HOURS_VIOLATION", codes)
        self.assertFalse(report.day_summaries[0].timing_verified)

        expired = _hard(
            _interval(9, 18),
            fresh_until=datetime(2026, 10, 3, tzinfo=UTC),
        )
        expired_report = evaluate_timeline(
            state,
            now=datetime(2026, 10, 3, tzinfo=UTC),
            activity_availability=(expired,),
        )
        self.assertIn(
            "OPENING_HOURS_NEEDS_VERIFICATION",
            {item.code for item in expired_report.issues},
        )

    def test_manual_provider_intersection_controls_move_and_slack(
        self,
    ) -> None:
        state = _state(
            flexibility=Flexibility.MOVABLE,
            duration_min=60,
            windows=(TimeWindow(time(9), time(18)),),
        )
        report = evaluate_timeline(
            state,
            now=datetime(2026, 10, 3, tzinfo=UTC),
            activity_availability=(_hard(_interval(11, 13)),),
        )
        entry = report.timeline[0]
        self.assertEqual(
            time(11),
            entry.start_at.astimezone(ZoneInfo("Asia/Seoul")).time(),
        )
        self.assertEqual(60.0, entry.slack_min)

    def test_fixed_time_is_never_moved_to_provider_opening(self) -> None:
        report = evaluate_timeline(
            _state(duration_min=60),
            now=datetime(2026, 10, 3, tzinfo=UTC),
            activity_availability=(_hard(_interval(11, 13)),),
        )
        self.assertEqual(
            time(10),
            report.timeline[0]
            .start_at.astimezone(ZoneInfo("Asia/Seoul"))
            .time(),
        )
        self.assertIn(
            "OPENING_HOURS_VIOLATION",
            {item.code for item in report.issues},
        )

    def test_sidecar_contract_rejects_malformed_hard_or_speculative_data(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            ActivityAvailability(
                activity_id="visit",
                disposition=AvailabilityDisposition.HARD_CURRENT,
            )
        with self.assertRaises(ValueError):
            ActivityAvailability(
                activity_id="visit",
                disposition=AvailabilityDisposition.NEEDS_VERIFICATION,
                intervals=(_interval(9, 18),),
                reason="regular_opening_hours",
            )

    def test_empty_optional_sidecar_preserves_existing_api(self) -> None:
        self.assertEqual(
            evaluate_timeline(_state()).timeline,
            evaluate_timeline(
                _state(),
                activity_availability=(),
            ).timeline,
        )


class AvailabilityCompositionTests(unittest.TestCase):
    def _compose_current_pair(
        self,
        *,
        second_periods: list[object] | None = None,
    ) -> object:
        fixture = _Fixture()
        first = fixture.request(
            PlaceDetailsKind.CURRENT_HOURS,
            target_start=date(2026, 7, 29),
            target_end=date(2026, 7, 29),
        )
        second = fixture.request(
            PlaceDetailsKind.CURRENT_HOURS,
            target_start=date(2026, 7, 29),
            target_end=date(2026, 7, 30),
        )
        second_body = (
            _current_body()
            if second_periods is None
            else _current_body(periods=second_periods)
        )
        batch = execute_google_place_details_batch(
            (first, second),
            _Transport(
                _response(_current_body()),
                _response(second_body),
            ),
            session=fixture.session,
            attempt_budget=PlaceDetailsAttemptBudget(2),
            clock=lambda: NOW,
        )
        snapshot = batch.current.snapshot(evaluation_at=NOW)
        return compose_trip_state(
            _canonical_plan(),
            snapshot,
            availability_keys=(
                first.provider_request.fact_keys[0],
                second.provider_request.fact_keys[0],
            ),
        )

    def test_equivalent_current_facts_remain_hard_and_bind_attribution(
        self,
    ) -> None:
        composed = self._compose_current_pair()
        availability = composed.activity_availability[0]
        self.assertEqual(
            AvailabilityDisposition.HARD_CURRENT,
            availability.disposition,
        )
        self.assertEqual(2, len(availability.evidence_refs))
        self.assertEqual(2, len(composed.evidence.used_observation_ids))
        self.assertEqual(2, len(composed.live_attributions))

    def test_equivalent_current_facts_expire_at_earliest_deadline(
        self,
    ) -> None:
        later = NOW + timedelta(hours=1)
        fixture = _Fixture(
            session_clock=_SequenceClock(
                NOW,
                NOW,
                NOW,
                NOW,
                later,
                later,
            )
        )
        first = fixture.request(
            PlaceDetailsKind.CURRENT_HOURS,
            target_start=date(2026, 7, 29),
            target_end=date(2026, 7, 29),
        )
        second = fixture.request(
            PlaceDetailsKind.CURRENT_HOURS,
            target_start=date(2026, 7, 29),
            target_end=date(2026, 7, 30),
        )
        batch = execute_google_place_details_batch(
            (first, second),
            _Transport(
                _response(_current_body()),
                _response(_current_body()),
            ),
            session=fixture.session,
            attempt_budget=PlaceDetailsAttemptBudget(2),
            clock=_SequenceClock(
                NOW,
                NOW,
                NOW,
                NOW,
                later,
                later,
                later,
                later,
            ),
        )
        composed = compose_trip_state(
            _canonical_plan(),
            batch.current.snapshot(evaluation_at=later),
            availability_keys=(
                first.provider_request.fact_keys[0],
                second.provider_request.fact_keys[0],
            ),
        )
        self.assertEqual(
            NOW + timedelta(hours=12),
            composed.activity_availability[0].fresh_until,
        )
        report = evaluate_composed_timeline(
            composed,
            now=NOW + timedelta(hours=12),
        )
        self.assertIn(
            "OPENING_HOURS_NEEDS_VERIFICATION",
            {item.code for item in report.issues},
        )

    def test_conflicting_current_facts_fail_closed_with_all_refs(self) -> None:
        composed = self._compose_current_pair(
            second_periods=[
                {
                    "open": {
                        "date": {
                            "year": 2026,
                            "month": 7,
                            "day": 29,
                        },
                        "day": 3,
                        "hour": 10,
                        "minute": 0,
                    },
                    "close": {
                        "date": {
                            "year": 2026,
                            "month": 7,
                            "day": 29,
                        },
                        "day": 3,
                        "hour": 18,
                        "minute": 0,
                    },
                }
            ],
        )
        availability = composed.activity_availability[0]
        self.assertEqual(
            AvailabilityDisposition.NEEDS_VERIFICATION,
            availability.disposition,
        )
        self.assertEqual(
            "conflicting_current_opening_hours",
            availability.reason,
        )
        self.assertEqual(2, len(availability.evidence_refs))
        report = evaluate_composed_timeline(composed, now=NOW)
        issue = next(
            item
            for item in report.issues
            if item.code == "OPENING_HOURS_NEEDS_VERIFICATION"
        )
        self.assertEqual(2, len(issue.evidence_refs))

    def test_regular_and_missing_facts_are_explicit_runtime_only_needs(
        self,
    ) -> None:
        fixture = _Fixture()
        regular = fixture.request(
            PlaceDetailsKind.REGULAR_HOURS,
            target_start=date(2026, 7, 29),
            target_end=date(2026, 7, 29),
        )
        batch = execute_google_place_details_batch(
            (regular,),
            _Transport(
                _response(
                    _regular_body(
                        [
                            {
                                "open": {"day": 3, "hour": 9},
                                "close": {"day": 3, "hour": 17},
                            }
                        ]
                    )
                )
            ),
            session=fixture.session,
            attempt_budget=PlaceDetailsAttemptBudget(1),
            clock=lambda: NOW,
        )
        canonical = _canonical_plan()
        before = copy.deepcopy(canonical)
        composed = compose_trip_state(
            canonical,
            batch.current.snapshot(evaluation_at=NOW),
            availability_keys=(regular.provider_request.fact_keys[0],),
        )
        self.assertEqual(before, canonical)
        self.assertNotIn("activity_availability", composed.to_dict())
        self.assertEqual(
            "regular_opening_hours",
            composed.activity_availability[0].reason,
        )

        missing_fixture = _Fixture()
        missing = missing_fixture.request(
            PlaceDetailsKind.CURRENT_HOURS,
            target_start=date(2026, 7, 29),
            target_end=date(2026, 7, 29),
        )
        missing_composed = compose_trip_state(
            canonical,
            missing_fixture.snapshot,
            availability_keys=(missing.provider_request.fact_keys[0],),
        )
        self.assertEqual(
            "missing_opening_hours",
            missing_composed.activity_availability[0].reason,
        )


if __name__ == "__main__":
    unittest.main()
