"""Offline adversarial tests for the deterministic planning kernel.

These tests intentionally use tiny synthetic trips.  They protect scheduling
semantics without relying on Google APIs, a network connection, or local trip
data.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from trip_planner.loaders import LoadError, load_legacy_trip
from trip_planner.models import (
    Activity,
    CheckStatus,
    Constraint,
    ConstraintKind,
    ConstraintStrength,
    DaySpec,
    DecisionState,
    EvidenceState,
    Flexibility,
    IssueSeverity,
    TimeWindow,
    TravelEstimate,
    TripState,
)
from trip_planner.timeline import evaluate_timeline


VERIFIED = EvidenceState.VERIFIED
HARD = ConstraintStrength.HARD


def repair_scope(issue) -> tuple[object, ...]:
    """Test-only preview of the structural identity Phase 2 will hash."""

    code = str(issue.code)
    family = code.removeprefix("POSSIBLE_")
    return (
        family,
        issue.activity_ids,
        issue.evidence_refs,
        issue.details,
    )


def make_activity(
    activity_id: str,
    *,
    day_id: str = "day-1",
    order: int = 0,
    location_id: str | None = None,
    scheduled_start: time | None = time(10, 0),
    duration_min: int | None = 60,
    decision_state: DecisionState = DecisionState.SELECTED,
    flexibility: Flexibility = Flexibility.MOVABLE,
    evidence_state: EvidenceState = VERIFIED,
    allowed_windows: tuple[TimeWindow, ...] = (),
) -> Activity:
    return Activity(
        activity_id=activity_id,
        day_id=day_id,
        order=order,
        title=activity_id,
        location_id=location_id or f"loc-{activity_id}",
        scheduled_start=scheduled_start,
        duration_min=duration_min,
        priority=50,
        decision_state=decision_state,
        flexibility=flexibility,
        evidence_state=evidence_state,
        allowed_windows=allowed_windows,
    )


def make_day(
    *activity_ids: str,
    day_id: str = "day-1",
    trip_date: date = date(2026, 10, 1),
    timezone: str = "Asia/Taipei",
    available_start: time = time(9, 0),
    available_end: time = time(18, 0),
    start_location_id: str = "hotel",
    end_location_id: str = "hotel",
    allowed_modes: tuple[str, ...] = ("walking",),
) -> DaySpec:
    return DaySpec(
        day_id=day_id,
        date=trip_date,
        timezone=timezone,
        available_start=available_start,
        available_end=available_end,
        start_location_id=start_location_id,
        end_location_id=end_location_id,
        allowed_modes=allowed_modes,
        activity_ids=tuple(activity_ids),
    )


def make_edge(
    from_location_id: str,
    to_location_id: str,
    duration_min: int,
    *,
    day_id: str | None = "day-1",
    evidence_state: EvidenceState = VERIFIED,
    mode: str = "walking",
    buffer_min: int = 0,
    fresh_until: datetime | None = None,
    query_departure_at: datetime | None = None,
    query_arrival_at: datetime | None = None,
) -> TravelEstimate:
    return TravelEstimate(
        from_location_id=from_location_id,
        to_location_id=to_location_id,
        mode=mode,
        duration_min=duration_min,
        day_id=day_id,
        buffer_min=buffer_min,
        evidence_state=evidence_state,
        fresh_until=fresh_until,
        source="fixture",
        recommended=True,
        query_departure_at=query_departure_at,
        query_arrival_at=query_arrival_at,
    )


def make_constraint(
    constraint_id: str,
    kind: ConstraintKind,
    *subject_ids: str,
    params: tuple[tuple[str, object], ...] = (),
) -> Constraint:
    return Constraint(
        constraint_id=constraint_id,
        kind=kind,
        strength=HARD,
        subject_ids=tuple(subject_ids),
        params=params,
        origin="test",
        confidence=1.0,
    )


def make_state(
    *,
    days: tuple[DaySpec, ...],
    activities: tuple[Activity, ...],
    travel: tuple[TravelEstimate, ...] = (),
    constraints: tuple[Constraint, ...] = (),
    timezone: str = "Asia/Taipei",
) -> TripState:
    return TripState(
        slug="fixture-trip",
        title="Fixture trip",
        timezone=timezone,
        days=days,
        activities=activities,
        travel_estimates=travel,
        constraints=constraints,
        start_date=min(day.date for day in days),
        end_date=max(day.date for day in days),
    )


class PlanningKernelTests(unittest.TestCase):
    def assert_issue(self, report, expected_code: str) -> None:
        codes = {
            getattr(issue.code, "value", issue.code)
            for issue in report.issues
        }
        self.assertIn(expected_code, codes, report)

    def test_normal_plan_is_feasible(self) -> None:
        museum = make_activity("museum", order=0, scheduled_start=time(10, 0))
        cafe = make_activity(
            "cafe",
            order=1,
            scheduled_start=time(12, 0),
            duration_min=45,
        )
        state = make_state(
            days=(make_day("museum", "cafe"),),
            activities=(museum, cafe),
            travel=(
                make_edge("hotel", "loc-museum", 30),
                make_edge("loc-museum", "loc-cafe", 15),
                make_edge("loc-cafe", "hotel", 20),
            ),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.FEASIBLE, report.status)
        self.assertEqual(("museum", "cafe"), tuple(e.activity_id for e in report.timeline))

    def test_single_day_single_activity_is_supported(self) -> None:
        activity = make_activity("only", scheduled_start=time(10, 0))
        state = make_state(
            days=(make_day("only"),),
            activities=(activity,),
            travel=(
                make_edge("hotel", "loc-only", 15),
                make_edge("loc-only", "hotel", 15),
            ),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.FEASIBLE, report.status)
        self.assertEqual(1, len(report.timeline))

    def test_full_duration_must_fit_inside_allowed_window(self) -> None:
        activity = make_activity(
            "late-museum",
            scheduled_start=time(16, 30),
            duration_min=60,
            allowed_windows=(TimeWindow(time(16, 0), time(17, 0)),),
        )
        state = make_state(
            days=(make_day("late-museum"),),
            activities=(activity,),
            travel=(
                make_edge("hotel", "loc-late-museum", 0),
                make_edge("loc-late-museum", "hotel", 0),
            ),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "TIME_WINDOW_VIOLATION")

    def test_return_to_base_must_fit_before_day_end(self) -> None:
        activity = make_activity(
            "sunset",
            scheduled_start=time(17, 0),
            duration_min=50,
        )
        state = make_state(
            days=(make_day("sunset"),),
            activities=(activity,),
            travel=(
                make_edge("hotel", "loc-sunset", 0),
                make_edge("loc-sunset", "hotel", 20),
            ),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "RETURN_AFTER_DAY_END")

    def test_unverified_fixed_time_outside_day_is_structurally_infeasible(
        self,
    ) -> None:
        activity = make_activity(
            "late-fixed",
            scheduled_start=time(21, 0),
            duration_min=30,
            flexibility=Flexibility.FIXED_TIME,
            evidence_state=EvidenceState.UNVERIFIED,
        )
        state = make_state(
            days=(
                make_day(
                    "late-fixed",
                    available_start=time(8, 0),
                    available_end=time(20, 0),
                ),
            ),
            activities=(activity,),
            travel=(
                make_edge(
                    "hotel",
                    "loc-late-fixed",
                    15,
                    evidence_state=EvidenceState.UNVERIFIED,
                ),
                make_edge(
                    "loc-late-fixed",
                    "hotel",
                    15,
                    evidence_state=EvidenceState.UNVERIFIED,
                ),
            ),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "DAY_WINDOW_VIOLATION")
        self.assertNotIn(
            "POSSIBLE_DAY_WINDOW_VIOLATION",
            {issue.code for issue in report.issues},
        )

    def test_missing_travel_is_unknown_not_zero(self) -> None:
        activity = make_activity("unrouted")
        state = make_state(
            days=(make_day("unrouted"),),
            activities=(activity,),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.NEEDS_VERIFICATION, report.status)
        self.assert_issue(report, "MISSING_TRAVEL_ESTIMATE")

    def test_missing_duration_requires_verification(self) -> None:
        activity = make_activity("unknown-duration", duration_min=None)
        state = make_state(
            days=(make_day("unknown-duration"),),
            activities=(activity,),
            travel=(
                make_edge("hotel", "loc-unknown-duration", 10),
                make_edge("loc-unknown-duration", "hotel", 10),
            ),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.NEEDS_VERIFICATION, report.status)
        self.assert_issue(report, "MISSING_DURATION")

    def test_late_arrival_to_fixed_time_is_infeasible(self) -> None:
        first = make_activity(
            "first",
            order=0,
            scheduled_start=time(9, 0),
            duration_min=60,
            flexibility=Flexibility.FIXED_TIME,
        )
        second = make_activity(
            "second",
            order=1,
            scheduled_start=time(9, 30),
            duration_min=30,
            flexibility=Flexibility.FIXED_TIME,
        )
        state = make_state(
            days=(
                make_day(
                    "first",
                    "second",
                    available_start=time(8, 30),
                ),
            ),
            activities=(first, second),
            travel=(
                make_edge("hotel", "loc-first", 10),
                make_edge("loc-first", "loc-second", 10),
                make_edge("loc-second", "hotel", 10),
            ),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "FIXED_TIME_CONFLICT")

    def test_before_constraint_checks_actual_order(self) -> None:
        first = make_activity("first", order=0, scheduled_start=time(10, 0))
        second = make_activity("second", order=1, scheduled_start=time(12, 0))
        require_second_before_first = make_constraint(
            "reverse-order",
            ConstraintKind.BEFORE,
            "second",
            "first",
        )
        state = make_state(
            days=(make_day("first", "second"),),
            activities=(first, second),
            travel=(
                make_edge("hotel", "loc-first", 10),
                make_edge("loc-first", "loc-second", 10),
                make_edge("loc-second", "hotel", 10),
            ),
            constraints=(require_second_before_first,),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "PRECEDENCE_VIOLATION")

    def test_requires_constraint_rejects_missing_dependency(self) -> None:
        selected = make_activity("selected")
        dependency = make_activity(
            "dependency",
            order=1,
            decision_state=DecisionState.CANDIDATE,
        )
        requires = make_constraint(
            "selected-needs-dependency",
            ConstraintKind.REQUIRES,
            "selected",
            "dependency",
        )
        state = make_state(
            days=(make_day("selected"),),
            activities=(selected, dependency),
            travel=(
                make_edge("hotel", "loc-selected", 10),
                make_edge("loc-selected", "hotel", 10),
            ),
            constraints=(requires,),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "REQUIRES_VIOLATION")

    def test_choose_n_counts_only_included_activities(self) -> None:
        chosen = make_activity("chosen")
        candidate_1 = make_activity(
            "candidate-1",
            order=1,
            decision_state=DecisionState.CANDIDATE,
        )
        candidate_2 = make_activity(
            "candidate-2",
            order=2,
            decision_state=DecisionState.CANDIDATE,
        )
        choose_two = make_constraint(
            "choose-two",
            ConstraintKind.CHOOSE_N,
            "chosen",
            "candidate-1",
            "candidate-2",
            params=(("n", 2),),
        )
        state = make_state(
            days=(make_day("chosen"),),
            activities=(chosen, candidate_1, candidate_2),
            travel=(
                make_edge("hotel", "loc-chosen", 10),
                make_edge("loc-chosen", "hotel", 10),
            ),
            constraints=(choose_two,),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "CHOOSE_N_VIOLATION")

    def test_allowed_day_constraint_rejects_wrong_day(self) -> None:
        activity = make_activity("day-two-only", day_id="day-1")
        allowed_day = make_constraint(
            "allowed-day",
            ConstraintKind.ALLOWED_DAY,
            "day-two-only",
            params=(("day_ids", "day-2"),),
        )
        state = make_state(
            days=(
                make_day("day-two-only", day_id="day-1"),
                make_day(
                    day_id="day-2",
                    trip_date=date(2026, 10, 2),
                ),
            ),
            activities=(activity,),
            travel=(
                make_edge("hotel", "loc-day-two-only", 10),
                make_edge("loc-day-two-only", "hotel", 10),
            ),
            constraints=(allowed_day,),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "ALLOWED_DAY_VIOLATION")

    def test_must_include_constraint_requires_exactly_one_occurrence(self) -> None:
        missing = make_activity(
            "missing-activity",
            decision_state=DecisionState.CANDIDATE,
        )
        required = make_constraint(
            "must-see",
            ConstraintKind.MUST_INCLUDE,
            "missing-activity",
        )
        state = make_state(
            days=(make_day("missing-activity"),),
            activities=(missing,),
            constraints=(required,),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "MISSING_REQUIRED_ACTIVITY")

    def test_must_include_unknown_subject_is_invalid_constraint(self) -> None:
        required = make_constraint(
            "must-see",
            ConstraintKind.MUST_INCLUDE,
            "unknown-activity",
        )
        state = make_state(
            days=(make_day(),),
            activities=(),
            constraints=(required,),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "INVALID_CONSTRAINT")

    def test_exactly_once_group_rejects_no_active_variant(self) -> None:
        morning = make_activity(
            "choice-morning",
            decision_state=DecisionState.CANDIDATE,
        )
        afternoon = make_activity(
            "choice-afternoon",
            order=1,
            decision_state=DecisionState.CANDIDATE,
        )
        exactly_once = make_constraint(
            "one-choice",
            ConstraintKind.EXACTLY_ONCE,
            "choice-morning",
            "choice-afternoon",
        )
        state = make_state(
            days=(make_day(),),
            activities=(morning, afternoon),
            constraints=(exactly_once,),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "EXACTLY_ONCE_VIOLATION")

    def test_at_most_once_rejects_multiple_active_variants(self) -> None:
        morning = make_activity(
            "optional-morning",
            order=0,
            scheduled_start=time(10, 0),
        )
        afternoon = make_activity(
            "optional-afternoon",
            order=1,
            scheduled_start=time(14, 0),
        )
        at_most_once = make_constraint(
            "optional-once",
            ConstraintKind.AT_MOST_ONCE,
            "optional-morning",
            "optional-afternoon",
        )
        state = make_state(
            days=(make_day("optional-morning", "optional-afternoon"),),
            activities=(morning, afternoon),
            travel=(
                make_edge("hotel", "loc-optional-morning", 10),
                make_edge("loc-optional-morning", "loc-optional-afternoon", 10),
                make_edge("loc-optional-afternoon", "hotel", 10),
            ),
            constraints=(at_most_once,),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "AT_MOST_ONCE_VIOLATION")

    def test_canonical_day_rejects_duplicate_stable_id(self) -> None:
        with self.assertRaises(ValueError):
            make_day("same-id", "same-id")

    def test_day_rejects_ambiguous_equal_bounds(self) -> None:
        with self.assertRaises(ValueError):
            make_day(
                available_start=time(0, 0),
                available_end=time(0, 0),
            )

    def test_timeline_datetimes_are_timezone_aware(self) -> None:
        activity = make_activity("tokyo", scheduled_start=time(10, 0))
        state = make_state(
            days=(
                make_day(
                    "tokyo",
                    timezone="Asia/Tokyo",
                    trip_date=date(2027, 1, 15),
                ),
            ),
            activities=(activity,),
            travel=(
                make_edge("hotel", "loc-tokyo", 10),
                make_edge("loc-tokyo", "hotel", 10),
            ),
            timezone="Asia/Tokyo",
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.FEASIBLE, report.status)
        self.assertIsNotNone(report.timeline[0].start_at.tzinfo)
        self.assertEqual(
            timedelta(hours=9),
            report.timeline[0].start_at.utcoffset(),
        )

    def test_overnight_day_places_after_midnight_on_next_date(self) -> None:
        activity = make_activity(
            "late-show",
            scheduled_start=time(0, 30),
            duration_min=45,
        )
        state = make_state(
            days=(
                make_day(
                    "late-show",
                    trip_date=date(2026, 10, 1),
                    available_start=time(22, 0),
                    available_end=time(2, 0),
                ),
            ),
            activities=(activity,),
            travel=(
                make_edge("hotel", "loc-late-show", 10),
                make_edge("loc-late-show", "hotel", 10),
            ),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.FEASIBLE, report.status)
        self.assertEqual(date(2026, 10, 2), report.timeline[0].start_at.date())

    def test_legal_mode_beats_disallowed_recommended_mode(self) -> None:
        activity = make_activity("museum")
        state = make_state(
            days=(make_day("museum"),),
            activities=(activity,),
            travel=(
                make_edge(
                    "hotel",
                    "loc-museum",
                    5,
                    mode="driving",
                ),
                TravelEstimate(
                    from_location_id="hotel",
                    to_location_id="loc-museum",
                    mode="walking",
                    duration_min=15,
                    day_id="day-1",
                    evidence_state=VERIFIED,
                    source="fixture",
                    recommended=False,
                ),
                make_edge("loc-museum", "hotel", 15),
            ),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.FEASIBLE, report.status)
        self.assertEqual(15, report.timeline[0].travel_duration_min)
        self.assertNotIn("DISALLOWED_MODE", {issue.code for issue in report.issues})

    def test_hard_allowed_mode_participates_in_route_choice(self) -> None:
        activity = make_activity("museum")
        walking_only = make_constraint(
            "walking-only",
            ConstraintKind.ALLOWED_MODE,
            "museum",
            params=(("modes", "walking"),),
        )
        state = make_state(
            days=(
                make_day(
                    "museum",
                    allowed_modes=("walking", "driving"),
                ),
            ),
            activities=(activity,),
            travel=(
                make_edge(
                    "hotel",
                    "loc-museum",
                    5,
                    mode="driving",
                ),
                TravelEstimate(
                    from_location_id="hotel",
                    to_location_id="loc-museum",
                    mode="walking",
                    duration_min=20,
                    day_id="day-1",
                    evidence_state=VERIFIED,
                    source="fixture",
                    recommended=False,
                ),
                make_edge("loc-museum", "hotel", 15),
            ),
            constraints=(walking_only,),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.FEASIBLE, report.status)
        self.assertEqual(20, report.timeline[0].travel_duration_min)

    def test_exact_timed_departure_route_beats_untimed_fallback(self) -> None:
        query_at = datetime.fromisoformat("2026-10-01T09:00:00+08:00")
        activity = make_activity(
            "museum",
            scheduled_start=None,
        )
        timed = make_edge(
            "hotel",
            "loc-museum",
            25,
            query_departure_at=query_at,
        )
        untimed = make_edge("hotel", "loc-museum", 5)
        state = make_state(
            days=(
                make_day(
                    "museum",
                    end_location_id="loc-museum",
                ),
            ),
            activities=(activity,),
            travel=(untimed, timed),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.FEASIBLE, report.status)
        self.assertEqual(25, report.timeline[0].travel_duration_min)

    def test_timed_departure_route_is_not_reused_after_clock_moves(self) -> None:
        first = make_activity(
            "breakfast",
            order=0,
            location_id="hotel",
            scheduled_start=None,
            duration_min=60,
        )
        second = make_activity(
            "museum",
            order=1,
            scheduled_start=None,
        )
        nine_am_route = make_edge(
            "hotel",
            "loc-museum",
            15,
            query_departure_at=datetime.fromisoformat(
                "2026-10-01T09:00:00+08:00"
            ),
        )
        state = make_state(
            days=(
                make_day(
                    "breakfast",
                    "museum",
                    end_location_id="loc-museum",
                ),
            ),
            activities=(first, second),
            travel=(nine_am_route,),
        )

        report = evaluate_timeline(state)

        self.assertEqual(0, report.timeline[1].travel_duration_min)
        self.assertTrue(
            any(
                issue.code == "MISSING_TRAVEL_ESTIMATE"
                and issue.activity_ids == ("museum",)
                for issue in report.issues
            )
        )

    def test_untimed_route_falls_back_when_timed_context_misses(self) -> None:
        activity = make_activity("museum", scheduled_start=None)
        wrong_time = make_edge(
            "hotel",
            "loc-museum",
            5,
            query_departure_at=datetime.fromisoformat(
                "2026-10-01T10:00:00+08:00"
            ),
        )
        fallback = make_edge("hotel", "loc-museum", 20)
        state = make_state(
            days=(
                make_day(
                    "museum",
                    end_location_id="loc-museum",
                ),
            ),
            activities=(activity,),
            travel=(wrong_time, fallback),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.FEASIBLE, report.status)
        self.assertEqual(20, report.timeline[0].travel_duration_min)

    def test_arrival_context_excludes_route_buffer(self) -> None:
        activity = make_activity("museum", scheduled_start=None)
        arrival_bound = make_edge(
            "hotel",
            "loc-museum",
            30,
            buffer_min=15,
            query_arrival_at=datetime.fromisoformat(
                "2026-10-01T09:30:00+08:00"
            ),
        )
        fallback = make_edge("hotel", "loc-museum", 10)
        state = make_state(
            days=(
                make_day(
                    "museum",
                    end_location_id="loc-museum",
                ),
            ),
            activities=(activity,),
            travel=(fallback, arrival_bound),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.FEASIBLE, report.status)
        self.assertEqual(30, report.timeline[0].travel_duration_min)
        self.assertEqual(
            datetime.fromisoformat("2026-10-01T09:45:00+08:00"),
            report.timeline[0].arrival_at,
        )

    def test_arrival_context_requires_an_exact_instant(self) -> None:
        activity = make_activity("museum", scheduled_start=None)
        almost_matching = make_edge(
            "hotel",
            "loc-museum",
            30,
            query_arrival_at=datetime.fromisoformat(
                "2026-10-01T09:30:01+08:00"
            ),
        )
        fallback = make_edge("hotel", "loc-museum", 20)
        state = make_state(
            days=(
                make_day(
                    "museum",
                    end_location_id="loc-museum",
                ),
            ),
            activities=(activity,),
            travel=(almost_matching, fallback),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.FEASIBLE, report.status)
        self.assertEqual(20, report.timeline[0].travel_duration_min)

    def test_travel_fresh_until_is_exclusive(self) -> None:
        evaluated_at = datetime.fromisoformat("2026-09-01T00:00:00+00:00")
        activity = make_activity("museum", scheduled_start=None)
        state = make_state(
            days=(
                make_day(
                    "museum",
                    end_location_id="loc-museum",
                ),
            ),
            activities=(activity,),
            travel=(
                make_edge(
                    "hotel",
                    "loc-museum",
                    15,
                    fresh_until=evaluated_at,
                ),
            ),
        )

        report = evaluate_timeline(state, now=evaluated_at)

        self.assertEqual(CheckStatus.NEEDS_VERIFICATION, report.status)
        self.assert_issue(report, "STALE_EVIDENCE")
        self.assertFalse(report.day_summaries[0].timing_verified)

    def test_route_query_context_requires_one_aware_timestamp(self) -> None:
        with self.assertRaises(ValueError):
            make_edge(
                "hotel",
                "museum",
                15,
                query_departure_at=datetime(2026, 10, 1, 9, 0),
            )

        aware = datetime.fromisoformat("2026-10-01T09:00:00+08:00")
        with self.assertRaises(ValueError):
            make_edge(
                "hotel",
                "museum",
                15,
                query_departure_at=aware,
                query_arrival_at=aware,
            )

    def test_activity_specific_edges_do_not_leak_between_repeated_locations(
        self,
    ) -> None:
        first = make_activity(
            "first-x",
            order=0,
            location_id="x",
            scheduled_start=time(10, 0),
            duration_min=30,
        )
        first_y = make_activity(
            "first-y",
            order=1,
            location_id="y",
            scheduled_start=time(11, 15),
            duration_min=30,
        )
        second_x = make_activity(
            "second-x",
            order=2,
            location_id="x",
            scheduled_start=time(13, 0),
            duration_min=30,
        )
        second_y = make_activity(
            "second-y",
            order=3,
            location_id="y",
            scheduled_start=time(13, 45),
            duration_min=30,
        )

        def activity_edge(
            from_id: str,
            to_id: str,
            from_location: str,
            to_location: str,
            duration: int,
        ) -> TravelEstimate:
            return TravelEstimate(
                from_location_id=from_location,
                to_location_id=to_location,
                mode="walking",
                duration_min=duration,
                day_id="day-1",
                evidence_state=VERIFIED,
                source="fixture",
                recommended=True,
                from_activity_id=from_id,
                to_activity_id=to_id,
            )

        state = make_state(
            days=(
                make_day(
                    "first-x",
                    "first-y",
                    "second-x",
                    "second-y",
                    start_location_id="x",
                    end_location_id="y",
                ),
            ),
            activities=(first, first_y, second_x, second_y),
            travel=(
                activity_edge("first-x", "first-y", "x", "y", 60),
                activity_edge("first-y", "second-x", "y", "x", 10),
                activity_edge("second-x", "second-y", "x", "y", 10),
            ),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assertEqual(60, report.timeline[1].travel_duration_min)
        self.assert_issue(report, "SCHEDULED_START_CONFLICT")

    def test_elapsed_duration_crosses_dst_in_real_minutes(self) -> None:
        activity = make_activity(
            "dst-event",
            location_id="venue",
            scheduled_start=time(1, 30),
            duration_min=120,
        )
        day = make_day(
            "dst-event",
            trip_date=date(2026, 3, 8),
            timezone="America/New_York",
            available_start=time(0, 0),
            available_end=time(6, 0),
            start_location_id="venue",
            end_location_id="venue",
        )
        state = make_state(
            days=(day,),
            activities=(activity,),
            timezone="America/New_York",
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.FEASIBLE, report.status)
        entry = report.timeline[0]
        self.assertEqual((4, 30), (entry.end_at.hour, entry.end_at.minute))
        self.assertEqual(
            120 * 60,
            entry.end_at.timestamp() - entry.start_at.timestamp(),
        )

    def test_nonexistent_dst_constraint_window_is_not_green(self) -> None:
        activity = make_activity(
            "after-gap",
            location_id="venue",
            scheduled_start=time(3, 10),
            duration_min=10,
        )
        window = make_constraint(
            "gap-window",
            ConstraintKind.ALLOWED_WINDOW,
            "after-gap",
            params=(("start", "02:00"), ("end", "03:30")),
        )
        day = make_day(
            "after-gap",
            trip_date=date(2026, 3, 8),
            timezone="America/New_York",
            available_start=time(0, 0),
            available_end=time(6, 0),
            start_location_id="venue",
            end_location_id="venue",
        )
        state = make_state(
            days=(day,),
            activities=(activity,),
            constraints=(window,),
            timezone="America/New_York",
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "NONEXISTENT_LOCAL_TIME")
        issue = next(
            issue
            for issue in report.issues
            if issue.code == "NONEXISTENT_LOCAL_TIME"
        )
        self.assertEqual(
            {
                "day_id": "day-1",
                "local_datetime": "2026-03-08T02:00:00",
                "timezone": "America/New_York",
            },
            dict(issue.details),
        )

    def test_possible_and_definite_issue_keep_the_same_structural_scope(
        self,
    ) -> None:
        activity = make_activity(
            "timed-event",
            location_id="venue",
            scheduled_start=time(10, 0),
            duration_min=30,
        )
        day = make_day(
            "timed-event",
            available_start=time(9, 0),
            start_location_id="hotel",
            end_location_id="venue",
        )

        def report_for(evidence_state: EvidenceState):
            return evaluate_timeline(
                make_state(
                    days=(day,),
                    activities=(activity,),
                    travel=(
                        make_edge(
                            "hotel",
                            "venue",
                            90,
                            evidence_state=evidence_state,
                        ),
                    ),
                )
            )

        possible = next(
            issue
            for issue in report_for(EvidenceState.UNVERIFIED).issues
            if issue.code == "POSSIBLE_SCHEDULED_START_CONFLICT"
        )
        definite = next(
            issue
            for issue in report_for(EvidenceState.VERIFIED).issues
            if issue.code == "SCHEDULED_START_CONFLICT"
        )
        rewritten = replace(
            definite,
            message="The same scoped issue with different reader-facing text.",
        )

        self.assertNotEqual(possible.code, definite.code)
        self.assertNotEqual(possible.severity, definite.severity)
        self.assertEqual(repair_scope(possible), repair_scope(definite))
        self.assertEqual(repair_scope(definite), repair_scope(rewritten))

    def test_missing_bounds_infers_monotonic_midnight_rollover(self) -> None:
        late = make_activity(
            "late",
            order=0,
            location_id="venue",
            scheduled_start=time(23, 30),
            duration_min=60,
        )
        after_midnight = make_activity(
            "after-midnight",
            order=1,
            location_id="venue",
            scheduled_start=time(0, 45),
            duration_min=30,
        )
        day = DaySpec(
            day_id="day-1",
            date=date(2026, 10, 1),
            timezone="Asia/Taipei",
            activity_ids=("late", "after-midnight"),
        )
        state = make_state(
            days=(day,),
            activities=(late, after_midnight),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.NEEDS_VERIFICATION, report.status)
        self.assertEqual(date(2026, 10, 2), report.timeline[1].start_at.date())
        self.assertNotIn(
            "SCHEDULED_START_CONFLICT",
            {issue.code for issue in report.issues},
        )
        self.assert_issue(report, "SCHEDULE_DATE_ROLLOVER_INFERRED")

    def test_unverified_travel_makes_timing_conflict_possible(self) -> None:
        activity = make_activity(
            "tight",
            scheduled_start=time(9, 5),
        )
        state = make_state(
            days=(make_day("tight"),),
            activities=(activity,),
            travel=(
                make_edge(
                    "hotel",
                    "loc-tight",
                    10,
                    evidence_state=EvidenceState.UNVERIFIED,
                ),
                make_edge("loc-tight", "hotel", 10),
            ),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.NEEDS_VERIFICATION, report.status)
        self.assert_issue(report, "POSSIBLE_SCHEDULED_START_CONFLICT")
        self.assertEqual((), report.errors)

    def test_adjacent_day_activities_cannot_overlap_in_absolute_time(self) -> None:
        overnight = make_activity(
            "overnight",
            day_id="day-1",
            location_id="venue",
            scheduled_start=time(23, 0),
            duration_min=180,
        )
        next_day = make_activity(
            "next-day",
            day_id="day-2",
            location_id="venue",
            scheduled_start=time(1, 0),
            duration_min=30,
        )
        state = make_state(
            days=(
                make_day(
                    "overnight",
                    day_id="day-1",
                    trip_date=date(2026, 1, 1),
                    available_start=time(20, 0),
                    available_end=time(3, 0),
                    start_location_id="venue",
                    end_location_id="venue",
                ),
                make_day(
                    "next-day",
                    day_id="day-2",
                    trip_date=date(2026, 1, 2),
                    available_start=time(0, 0),
                    available_end=time(4, 0),
                    start_location_id="venue",
                    end_location_id="venue",
                ),
            ),
            activities=(overnight, next_day),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "GLOBAL_TIMELINE_OVERLAP")

    def test_return_travel_cannot_overlap_next_day_activity(self) -> None:
        late = make_activity(
            "late",
            day_id="day-1",
            location_id="remote",
            scheduled_start=time(23, 0),
            duration_min=30,
        )
        early = make_activity(
            "early",
            day_id="day-2",
            location_id="hotel",
            scheduled_start=time(1, 0),
            duration_min=30,
        )
        state = make_state(
            days=(
                make_day(
                    "late",
                    day_id="day-1",
                    trip_date=date(2026, 1, 1),
                    available_start=time(20, 0),
                    available_end=time(3, 0),
                    start_location_id="remote",
                    end_location_id="hotel",
                ),
                make_day(
                    "early",
                    day_id="day-2",
                    trip_date=date(2026, 1, 2),
                    available_start=time(0, 0),
                    available_end=time(4, 0),
                    start_location_id="hotel",
                    end_location_id="hotel",
                ),
            ),
            activities=(late, early),
            travel=(
                make_edge(
                    "remote",
                    "hotel",
                    120,
                    day_id="day-1",
                ),
            ),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "RETURN_TIMELINE_OVERLAP")

    def test_previous_day_cannot_overlap_next_day_inbound_travel(self) -> None:
        overnight = make_activity(
            "overnight",
            day_id="day-1",
            location_id="hotel",
            scheduled_start=time(0, 30),
            duration_min=30,
        )
        early = make_activity(
            "early",
            day_id="day-2",
            location_id="venue",
            scheduled_start=time(2, 0),
            duration_min=30,
        )
        state = make_state(
            days=(
                make_day(
                    "overnight",
                    day_id="day-1",
                    trip_date=date(2026, 1, 1),
                    available_start=time(20, 0),
                    available_end=time(2, 0),
                    start_location_id="hotel",
                    end_location_id="hotel",
                ),
                make_day(
                    "early",
                    day_id="day-2",
                    trip_date=date(2026, 1, 2),
                    available_start=time(0, 0),
                    available_end=time(4, 0),
                    start_location_id="hotel",
                    end_location_id="venue",
                ),
            ),
            activities=(overnight, early),
            travel=(make_edge("hotel", "venue", 120, day_id="day-2"),),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "INBOUND_TIMELINE_OVERLAP")

    def test_unverified_inbound_overlap_is_not_a_hard_failure(self) -> None:
        overnight = make_activity(
            "overnight",
            day_id="day-1",
            location_id="hotel",
            scheduled_start=time(0, 30),
            duration_min=30,
        )
        early = make_activity(
            "early",
            day_id="day-2",
            location_id="venue",
            scheduled_start=time(2, 0),
            duration_min=30,
        )
        state = make_state(
            days=(
                make_day(
                    "overnight",
                    day_id="day-1",
                    trip_date=date(2026, 1, 1),
                    available_start=time(20, 0),
                    available_end=time(2, 0),
                    start_location_id="hotel",
                    end_location_id="hotel",
                ),
                make_day(
                    "early",
                    day_id="day-2",
                    trip_date=date(2026, 1, 2),
                    available_start=time(0, 0),
                    available_end=time(4, 0),
                    start_location_id="hotel",
                    end_location_id="venue",
                ),
            ),
            activities=(overnight, early),
            travel=(
                make_edge(
                    "hotel",
                    "venue",
                    120,
                    day_id="day-2",
                    evidence_state=EvidenceState.UNVERIFIED,
                ),
            ),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.NEEDS_VERIFICATION, report.status)
        self.assert_issue(report, "POSSIBLE_INBOUND_TIMELINE_OVERLAP")
        self.assertEqual((), report.errors)

    def test_invalid_hard_daily_limit_is_not_ignored(self) -> None:
        activity = make_activity("museum")
        invalid_limit = make_constraint(
            "bad-limit",
            ConstraintKind.DAILY_LIMIT,
            params=(("max_activities", "zero"),),
        )
        state = make_state(
            days=(make_day("museum"),),
            activities=(activity,),
            travel=(
                make_edge("hotel", "loc-museum", 10),
                make_edge("loc-museum", "hotel", 10),
            ),
            constraints=(invalid_limit,),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "INVALID_CONSTRAINT")

    def test_unverified_interday_continuity_is_not_green(self) -> None:
        first_day = make_day(
            day_id="day-1",
            end_location_id="city-a",
        )
        second_day = make_day(
            day_id="day-2",
            trip_date=date(2026, 10, 2),
            start_location_id="city-b",
        )
        continuity = make_constraint(
            "continuous-trip",
            ConstraintKind.LOCATION_CONTINUITY,
        )
        state = make_state(
            days=(first_day, second_day),
            activities=(),
            travel=(
                make_edge(
                    "city-a",
                    "city-b",
                    60,
                    day_id=None,
                    evidence_state=EvidenceState.UNVERIFIED,
                ),
            ),
            constraints=(continuity,),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.NEEDS_VERIFICATION, report.status)
        self.assert_issue(report, "UNVERIFIED_EVIDENCE")

    def test_continuity_issue_scope_distinguishes_each_day_pair(
        self,
    ) -> None:
        first = make_day(
            day_id="day-1",
            start_location_id="city-a",
            end_location_id="city-a",
        )
        second = make_day(
            day_id="day-2",
            trip_date=date(2026, 10, 2),
            start_location_id="city-b",
            end_location_id="city-c",
        )
        third = make_day(
            day_id="day-3",
            trip_date=date(2026, 10, 3),
            start_location_id="city-d",
            end_location_id="city-d",
        )
        continuity = make_constraint(
            "continuous-trip",
            ConstraintKind.LOCATION_CONTINUITY,
        )
        travel = (
            make_edge(
                "city-a",
                "city-b",
                60,
                day_id=None,
                evidence_state=EvidenceState.UNVERIFIED,
            ),
            make_edge(
                "city-c",
                "city-d",
                60,
                day_id=None,
                evidence_state=EvidenceState.UNVERIFIED,
            ),
        )
        kwargs = {
            "days": (first, second, third),
            "activities": (),
            "constraints": (continuity,),
        }

        unknown_report = evaluate_timeline(
            make_state(travel=travel, **kwargs)
        )
        unknown = [
            dict(issue.details)
            for issue in unknown_report.issues
            if issue.code == "CONSTRAINT_NEEDS_VERIFICATION"
        ]
        violation_report = evaluate_timeline(
            make_state(travel=(), **kwargs)
        )
        violations = [
            dict(issue.details)
            for issue in violation_report.issues
            if issue.code == "LOCATION_CONTINUITY_VIOLATION"
        ]

        expected_pairs = {
            (
                "day-1",
                "day-2",
                "city-a",
                "city-b",
            ),
            (
                "day-2",
                "day-3",
                "city-c",
                "city-d",
            ),
        }
        for scoped_issues in (unknown, violations):
            self.assertEqual(2, len(scoped_issues))
            self.assertEqual(
                expected_pairs,
                {
                    (
                        details["previous_day_id"],
                        details["following_day_id"],
                        details["previous_end_location_id"],
                        details["following_start_location_id"],
                    )
                    for details in scoped_issues
                },
            )

    def test_wrong_subject_domain_is_invalid_constraint(self) -> None:
        wrong_subject = make_constraint(
            "wrong-domain",
            ConstraintKind.ALLOWED_DAY,
            "day-1",
            params=(("day_ids", "day-1"),),
        )
        state = make_state(
            days=(make_day(day_id="day-1"),),
            activities=(),
            constraints=(wrong_subject,),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "INVALID_CONSTRAINT")

    def test_constraint_time_with_offset_is_invalid(self) -> None:
        activity = make_activity("museum", scheduled_start=time(9, 0))
        fixed = make_constraint(
            "bad-time",
            ConstraintKind.FIXED_TIME,
            "museum",
            params=(("time", "09:00+09:00"),),
        )
        state = make_state(
            days=(
                make_day(
                    "museum",
                    available_start=time(8, 0),
                    start_location_id="loc-museum",
                    end_location_id="loc-museum",
                ),
            ),
            activities=(activity,),
            constraints=(fixed,),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "INVALID_CONSTRAINT")

    def test_fixed_time_field_contradiction_is_always_infeasible(self) -> None:
        activity = make_activity(
            "museum",
            scheduled_start=time(11, 0),
            evidence_state=EvidenceState.UNVERIFIED,
        )
        fixed = make_constraint(
            "fixed-at-ten",
            ConstraintKind.FIXED_TIME,
            "museum",
            params=(("time", "10:00"),),
        )
        state = make_state(
            days=(
                make_day(
                    "museum",
                    available_start=time(8, 0),
                    start_location_id="loc-museum",
                    end_location_id="loc-museum",
                ),
            ),
            activities=(activity,),
            constraints=(fixed,),
        )

        report = evaluate_timeline(state)

        self.assertEqual(CheckStatus.INFEASIBLE, report.status)
        self.assert_issue(report, "FIXED_TIME_CONFLICT")

    def test_trip_state_rejects_duplicate_constraint_ids(self) -> None:
        first = make_constraint(
            "same-id",
            ConstraintKind.MUST_INCLUDE,
            "missing-a",
        )
        second = make_constraint(
            "same-id",
            ConstraintKind.MUST_INCLUDE,
            "missing-b",
        )
        with self.assertRaises(ValueError):
            make_state(
                days=(make_day(),),
                activities=(),
                constraints=(first, second),
            )

    def test_fixed_time_activity_requires_a_start(self) -> None:
        with self.assertRaises(ValueError):
            make_activity(
                "broken-fixed-time",
                scheduled_start=None,
                flexibility=Flexibility.FIXED_TIME,
            )

    def test_non_finite_travel_duration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TravelEstimate(
                from_location_id="a",
                to_location_id="b",
                mode="walking",
                duration_min=float("nan"),
            )

    def test_same_input_produces_equal_report(self) -> None:
        activity = make_activity("repeatable")
        state = make_state(
            days=(make_day("repeatable"),),
            activities=(activity,),
            travel=(
                make_edge("hotel", "loc-repeatable", 10),
                make_edge("loc-repeatable", "hotel", 10),
            ),
        )
        frozen_now = datetime.fromisoformat("2026-09-01T00:00:00+00:00")

        first = evaluate_timeline(state, now=frozen_now)
        second = evaluate_timeline(state, now=frozen_now)

        self.assertEqual(first, second)

    def test_legacy_loader_uses_explicit_timezone_fallback_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "trip.json").write_text(
                json.dumps(
                    {
                        "title": "Legacy",
                        "slug": "legacy",
                        "date_range": "2026-10-01 ~ 2026-10-01",
                        "cities": ["Somewhere"],
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "itinerary.json").write_text(
                json.dumps(
                    {
                        "days": [
                            {
                                "day": 1,
                                "date": "2026-10-01",
                                "places": [
                                    {
                                        "title": "Museum",
                                        "time": "10:00",
                                        "duration_min": 60,
                                        "lat": 25.0,
                                        "lng": 121.5,
                                    }
                                ],
                                "travel": [],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            state = load_legacy_trip(data_dir)

        self.assertEqual("UTC", state.timezone)
        issue_codes = {
            getattr(issue.code, "value", issue.code)
            for issue in state.load_issues
        }
        self.assertIn("TIMEZONE_FALLBACK", issue_codes)

    def test_complete_day_timezones_downgrade_global_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "trip.json").write_text(
                json.dumps(
                    {
                        "title": "Per-day zones",
                        "slug": "per-day-zones",
                        "date_range": "2026-10-01 ~ 2026-10-01",
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "itinerary.json").write_text(
                json.dumps(
                    {
                        "days": [
                            {
                                "day": 1,
                                "date": "2026-10-01",
                                "timezone": "Asia/Taipei",
                                "places": [
                                    {
                                        "title": "Museum",
                                        "time": "10:00",
                                        "duration_min": 60,
                                        "lat": 25.0,
                                        "lng": 121.5,
                                    }
                                ],
                                "travel": [],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            state = load_legacy_trip(data_dir)

        fallback = next(
            issue
            for issue in state.load_issues
            if issue.code == "TIMEZONE_FALLBACK"
        )
        self.assertEqual(IssueSeverity.INFO, fallback.severity)
        self.assertEqual("Asia/Taipei", state.days[0].timezone)

    def test_invalid_day_timezone_issues_keep_distinct_day_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "trip.json").write_text(
                json.dumps(
                    {
                        "title": "Invalid per-day zones",
                        "slug": "invalid-per-day-zones",
                        "timezone": "Asia/Taipei",
                        "date_range": "2026-10-01 ~ 2026-10-02",
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "itinerary.json").write_text(
                json.dumps(
                    {
                        "days": [
                            {
                                "day": day,
                                "date": f"2026-10-0{day}",
                                "timezone": "Not/AZone",
                                "places": [],
                                "travel": [],
                            }
                            for day in (1, 2)
                        ]
                    }
                ),
                encoding="utf-8",
            )

            state = load_legacy_trip(data_dir)

        issues = tuple(
            issue
            for issue in state.load_issues
            if issue.code == "DAY_TIMEZONE_INVALID"
        )
        self.assertEqual(2, len(issues))
        self.assertEqual(
            2,
            len({dict(issue.details)["day_id"] for issue in issues}),
        )

    def test_legacy_loader_ignores_dangling_derived_travel_edge(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "trip.json").write_text(
                json.dumps(
                    {
                        "title": "Broken legacy trip",
                        "slug": "broken",
                        "timezone": "Asia/Taipei",
                        "date_range": "2026-10-01 ~ 2026-10-01",
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "itinerary.json").write_text(
                json.dumps(
                    {
                        "days": [
                            {
                                "day": 1,
                                "date": "2026-10-01",
                                "places": [
                                    {
                                        "title": "First place",
                                        "time": "10:00",
                                        "duration_min": 60,
                                        "lat": 25.0,
                                        "lng": 121.5,
                                    },
                                    {
                                        "title": "Second place",
                                        "time": "12:00",
                                        "duration_min": 60,
                                        "lat": 25.1,
                                        "lng": 121.6,
                                    }
                                ],
                                "travel": [
                                    {
                                        "from": 0,
                                        "to": 2,
                                        "recommended_mode": "walking",
                                        "modes": {
                                            "walking": {
                                                "duration_min": 5,
                                            }
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            state = load_legacy_trip(data_dir)
            report = evaluate_timeline(state)

        issue_codes = {
            getattr(issue.code, "value", issue.code)
            for issue in state.load_issues
        }
        self.assertIn("INVALID_TRAVEL_REFERENCE", issue_codes)
        self.assertEqual((), state.travel_estimates)
        self.assertEqual(CheckStatus.NEEDS_VERIFICATION, report.status)

    def test_legacy_loader_rejects_non_object_day(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "trip.json").write_text(
                json.dumps(
                    {
                        "title": "Malformed legacy trip",
                        "slug": "malformed",
                        "timezone": "Asia/Taipei",
                        "date_range": "2026-10-01 ~ 2026-10-01",
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "itinerary.json").write_text(
                json.dumps({"days": ["not-an-object"]}),
                encoding="utf-8",
            )

            with self.assertRaises(LoadError):
                load_legacy_trip(data_dir)

    def test_legacy_loader_preserves_global_modes_and_flags_bad_recommendation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "trip.json").write_text(
                json.dumps(
                    {
                        "title": "Legacy modes",
                        "slug": "legacy-modes",
                        "timezone": "Asia/Taipei",
                        "date_range": "2026-10-01 ~ 2026-10-01",
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "itinerary.json").write_text(
                json.dumps(
                    {
                        "available_modes": ["walking"],
                        "days": [
                            {
                                "day": 0,
                                "date": "2026-10-01",
                                "places": [
                                    {
                                        "title": "A",
                                        "time": "10:00",
                                        "duration_min": 30,
                                        "lat": 25.0,
                                        "lng": 121.5,
                                    },
                                    {
                                        "title": "B",
                                        "time": "11:00",
                                        "duration_min": 30,
                                        "lat": 25.1,
                                        "lng": 121.6,
                                    },
                                ],
                                "travel": [
                                    {
                                        "from": 0,
                                        "to": 1,
                                        "recommended_mode": "transit",
                                        "modes": {
                                            "driving": {
                                                "duration_min": 10,
                                            }
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            state = load_legacy_trip(data_dir)

        self.assertEqual(("walking",), state.days[0].allowed_modes)
        self.assertEqual(date(2026, 10, 1), state.days[0].date)
        issue_codes = {issue.code for issue in state.load_issues}
        self.assertIn("RECOMMENDED_MODE_UNAVAILABLE", issue_codes)


if __name__ == "__main__":
    unittest.main()
