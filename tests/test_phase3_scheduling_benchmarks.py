"""Solver-independent hardening benchmarks for Phase 3 scheduling.

The fixtures stay deliberately abstract and offline.  They use typed domain
objects, fixed evaluation time, directed travel minutes, and explicit buffers;
they do not model real provider facts for Busan or Hokkaido.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import date, time

from tests.test_phase3_scheduling_contracts import (
    activity,
    complete_edges,
    constraint,
    day,
    edge,
    problem,
    state,
)
from trip_planner.models import (
    CheckStatus,
    ConstraintKind,
    DecisionState,
    Flexibility,
    TimeWindow,
)
from trip_planner.scheduler import solve_schedule
from trip_planner.scheduling import (
    ScheduleAssignment,
    ScheduleProblem,
    ScheduleScore,
    ScheduleStatus,
    materialize_schedule,
    replay_schedule_candidate,
)
from trip_planner.timeline import evaluate_timeline


def build_busan_problem() -> ScheduleProblem:
    """Return the typed two-day transit/hotel-anchor benchmark."""

    coast = activity(
        "b-coast",
        day_id="day-1",
        order=0,
        duration_min=120,
        windows=(TimeWindow(time(9, 30), time(13)),),
    )
    art = activity(
        "b-art",
        day_id="day-1",
        order=1,
        duration_min=90,
        priority=80,
    )
    dinner = activity(
        "b-dinner",
        day_id="day-1",
        order=2,
        duration_min=90,
        decision=DecisionState.BOOKED,
        flexibility=Flexibility.FIXED_TIME,
        scheduled_start=time(18),
    )
    market = activity(
        "b-market",
        day_id="day-2",
        order=0,
        duration_min=90,
        windows=(TimeWindow(time(10), time(15)),),
    )
    park = activity(
        "b-park",
        day_id="day-2",
        order=1,
        duration_min=90,
        priority=60,
    )
    view = activity(
        "b-view",
        day_id="day-2",
        order=2,
        duration_min=60,
        priority=30,
    )
    locations = (
        "busan-hotel",
        "loc-b-coast",
        "loc-b-art",
        "loc-b-dinner",
        "loc-b-market",
        "loc-b-park",
        "loc-b-view",
    )
    travel = list(complete_edges(locations, duration=30, buffer=10))
    travel.extend(
        (
            edge("loc-b-market", "loc-b-park", 20, buffer=10),
            edge("loc-b-park", "busan-hotel", 20, buffer=10),
            edge("loc-b-art", "loc-b-dinner", 30, buffer=10),
            edge("loc-b-dinner", "busan-hotel", 20, buffer=10),
        )
    )
    constraints = [
        constraint(
            "busan-must",
            ConstraintKind.MUST_INCLUDE,
            "b-coast",
            "b-market",
        ),
        constraint(
            "busan-optional-count",
            ConstraintKind.CHOOSE_N,
            "b-art",
            "b-park",
            "b-view",
            params=(("n", 2),),
        ),
    ]
    for activity_id, day_id in (
        ("b-coast", "day-1"),
        ("b-art", "day-1"),
        ("b-dinner", "day-1"),
        ("b-market", "day-2"),
        ("b-park", "day-2"),
        ("b-view", "day-2"),
    ):
        constraints.append(
            constraint(
                f"allowed-{activity_id}",
                ConstraintKind.ALLOWED_DAY,
                activity_id,
                params=(("day_id", day_id),),
            )
        )
    trip = state(
        days=(
            replace(
                day(
                    "b-coast",
                    "b-art",
                    "b-dinner",
                    day_id="day-1",
                    trip_date=date(2026, 10, 1),
                    end=time(20, 30),
                ),
                start_location_id="busan-hotel",
                end_location_id="busan-hotel",
            ),
            replace(
                day(
                    "b-market",
                    "b-park",
                    "b-view",
                    day_id="day-2",
                    trip_date=date(2026, 10, 2),
                    end=time(19),
                ),
                start_location_id="busan-hotel",
                end_location_id="busan-hotel",
            ),
        ),
        activities=(coast, art, dinner, market, park, view),
        travel=tuple(travel),
        constraints=tuple(constraints),
    )
    return problem(
        trip,
        eligible=("b-coast", "b-art", "b-market", "b-park", "b-view"),
        evaluations=10_000,
    )


def build_hokkaido_problem() -> ScheduleProblem:
    """Return the consecutive-day winter-buffer/cross-city benchmark."""

    snow = activity(
        "snow-garden",
        day_id="day-1",
        order=0,
        duration_min=120,
        windows=(TimeWindow(time(10), time(14)),),
    )
    gallery = activity(
        "indoor-gallery",
        day_id="day-1",
        order=1,
        duration_min=90,
        priority=60,
    )
    market = activity(
        "morning-market",
        day_id="day-2",
        order=0,
        duration_min=60,
        windows=(TimeWindow(time(8, 30), time(10)),),
    )
    checkin = activity(
        "ryokan-checkin",
        day_id="day-2",
        order=1,
        location_id="city-b-ryokan",
        duration_min=30,
        decision=DecisionState.BOOKED,
        flexibility=Flexibility.FIXED_TIME,
        scheduled_start=time(16),
    )
    night = activity(
        "night-walk",
        day_id="day-2",
        order=2,
        duration_min=60,
        priority=40,
        windows=(TimeWindow(time(17), time(19)),),
    )
    trip = state(
        days=(
            replace(
                day(
                    "snow-garden",
                    "indoor-gallery",
                    day_id="day-1",
                    trip_date=date(2027, 1, 9),
                    end=time(18),
                    base="hotel-a",
                ),
                timezone="Asia/Tokyo",
            ),
            replace(
                day(
                    "morning-market",
                    "ryokan-checkin",
                    "night-walk",
                    day_id="day-2",
                    trip_date=date(2027, 1, 10),
                    start=time(8),
                    end=time(20),
                    base="hotel-a",
                ),
                timezone="Asia/Tokyo",
                end_location_id="hotel-b",
            ),
        ),
        activities=(snow, gallery, market, checkin, night),
        travel=(
            edge("hotel-a", "loc-snow-garden", 20, buffer=20),
            edge("loc-snow-garden", "loc-indoor-gallery", 25, buffer=20),
            edge("loc-indoor-gallery", "hotel-a", 20, buffer=20),
            edge("hotel-a", "loc-morning-market", 15, buffer=20),
            edge(
                "loc-morning-market",
                "city-b-ryokan",
                180,
                buffer=45,
            ),
            edge("city-b-ryokan", "loc-night-walk", 20, buffer=20),
            edge("loc-night-walk", "hotel-b", 10, buffer=20),
            # Deterministic alternatives for explored but losing orders.
            edge("hotel-a", "loc-indoor-gallery", 20, buffer=20),
            edge("loc-indoor-gallery", "loc-snow-garden", 25, buffer=20),
            edge("loc-snow-garden", "hotel-a", 20, buffer=20),
            edge("hotel-a", "city-b-ryokan", 240, buffer=45),
            edge("city-b-ryokan", "loc-morning-market", 180, buffer=45),
            edge("loc-morning-market", "loc-night-walk", 190, buffer=45),
            edge("loc-night-walk", "loc-morning-market", 190, buffer=45),
            edge("city-b-ryokan", "hotel-b", 20, buffer=20),
        ),
        constraints=(
            constraint(
                "hokkaido-must",
                ConstraintKind.MUST_INCLUDE,
                "snow-garden",
                "morning-market",
            ),
            constraint(
                "snow-before-gallery",
                ConstraintKind.BEFORE,
                "snow-garden",
                "indoor-gallery",
            ),
            constraint(
                "snow-day",
                ConstraintKind.ALLOWED_DAY,
                "snow-garden",
                "indoor-gallery",
                params=(("day_id", "day-1"),),
            ),
            constraint(
                "city-day",
                ConstraintKind.ALLOWED_DAY,
                "morning-market",
                "ryokan-checkin",
                "night-walk",
                params=(("day_id", "day-2"),),
            ),
        ),
    )
    trip = replace(trip, timezone="Asia/Tokyo")
    return problem(
        trip,
        eligible=(
            "snow-garden",
            "indoor-gallery",
            "morning-market",
            "night-walk",
        ),
        evaluations=10_000,
    )


def _permuted_problem(schedule_problem: ScheduleProblem) -> ScheduleProblem:
    """Reverse non-semantic containers while preserving semantic day order."""

    source = schedule_problem.state
    permuted_state = replace(
        source,
        days=tuple(reversed(source.days)),
        activities=tuple(reversed(source.activities)),
        travel_estimates=tuple(reversed(source.travel_estimates)),
        constraints=tuple(reversed(source.constraints)),
    )
    return ScheduleProblem(
        state=permuted_state,
        evaluation_at=schedule_problem.evaluation_at,
        scope=schedule_problem.scope,
        trip_id=schedule_problem.trip_id,
        preferences=schedule_problem.preferences,
        limits=schedule_problem.limits,
    )


def _arc_tuples(candidate_arcs: tuple[str, ...]) -> set[tuple[str, str, str]]:
    result: set[tuple[str, str, str]] = set()
    for encoded in candidate_arcs:
        item = json.loads(encoded)
        result.add(
            (
                item["day_id"],
                item["from_location_id"],
                item["to_location_id"],
            )
        )
    return result


class Phase3CompositeGoldenTests(unittest.TestCase):
    def test_busan_trusted_replay_has_exact_score_and_hotel_returns(self) -> None:
        schedule_problem = build_busan_problem()

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        candidate = result.candidate
        self.assertEqual(
            (
                ScheduleAssignment("b-coast", "day-1", 0, time(9, 40)),
                ScheduleAssignment("b-art", "day-1", 1, time(12, 20)),
                ScheduleAssignment("b-dinner", "day-1", 2, time(18)),
                ScheduleAssignment("b-park", "day-2", 0, time(9, 40)),
                ScheduleAssignment("b-market", "day-2", 1, time(11, 50)),
            ),
            candidate.assignments,
        )
        self.assertEqual(
            ScheduleScore(
                hard_violation_count=0,
                missing_required_count=0,
                protected_change_count=0,
                accepted_activity_change_count=0,
                accepted_day_move_count=0,
                accepted_order_inversion_count=0,
                accepted_time_shift_deci_min=0,
                served_priority_points=140,
                soft_constraint_violation_count=0,
                verification_risk_count=0,
                tight_slack_count=0,
                slack_deficit_deci_min=0,
                activity_count_overage=0,
                service_overage_deci_min=0,
                wait_deci_min=2100,
                travel_deci_min=2000,
                buffer_deci_min=700,
                service_deci_min=4800,
                changed_activity_ids=(
                    "b-art",
                    "b-coast",
                    "b-market",
                    "b-park",
                ),
                protected_activity_ids=(),
                scheduled_optional_ids=(
                    "b-art",
                    "b-coast",
                    "b-market",
                    "b-park",
                ),
            ),
            candidate.score,
        )
        replayed = replay_schedule_candidate(schedule_problem, candidate)
        self.assertEqual(
            candidate.report,
            evaluate_timeline(replayed, now=schedule_problem.evaluation_at),
        )
        self.assertEqual(
            (
                ("day-1", "busan-hotel", "busan-hotel"),
                ("day-2", "busan-hotel", "busan-hotel"),
            ),
            tuple(
                (
                    item.day_id,
                    item.start_location_id,
                    item.end_location_id,
                )
                for item in sorted(
                    replayed.days, key=lambda value: (value.date, value.day_id)
                )
            ),
        )
        self.assertEqual(
            {
                ("day-1", "busan-hotel", "loc-b-coast"),
                ("day-1", "loc-b-coast", "loc-b-art"),
                ("day-1", "loc-b-art", "loc-b-dinner"),
                ("day-1", "loc-b-dinner", "busan-hotel"),
                ("day-2", "busan-hotel", "loc-b-park"),
                ("day-2", "loc-b-park", "loc-b-market"),
                ("day-2", "loc-b-market", "busan-hotel"),
            },
            _arc_tuples(candidate.required_arc_keys),
        )

    def test_hokkaido_trusted_replay_has_exact_score_and_winter_returns(
        self,
    ) -> None:
        schedule_problem = build_hokkaido_problem()

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        candidate = result.candidate
        self.assertEqual(
            (
                ScheduleAssignment("snow-garden", "day-1", 0, time(10)),
                ScheduleAssignment(
                    "indoor-gallery", "day-1", 1, time(12, 45)
                ),
                ScheduleAssignment(
                    "morning-market", "day-2", 0, time(8, 35)
                ),
                ScheduleAssignment(
                    "ryokan-checkin", "day-2", 1, time(16)
                ),
                ScheduleAssignment("night-walk", "day-2", 2, time(17, 10)),
            ),
            candidate.assignments,
        )
        self.assertEqual(
            ScheduleScore(
                hard_violation_count=0,
                missing_required_count=0,
                protected_change_count=0,
                accepted_activity_change_count=0,
                accepted_day_move_count=0,
                accepted_order_inversion_count=0,
                accepted_time_shift_deci_min=0,
                served_priority_points=100,
                soft_constraint_violation_count=0,
                verification_risk_count=0,
                tight_slack_count=0,
                slack_deficit_deci_min=0,
                activity_count_overage=0,
                service_overage_deci_min=0,
                wait_deci_min=1800,
                travel_deci_min=2900,
                buffer_deci_min=1650,
                service_deci_min=3600,
                changed_activity_ids=(
                    "indoor-gallery",
                    "morning-market",
                    "night-walk",
                    "snow-garden",
                ),
                protected_activity_ids=(),
                scheduled_optional_ids=(
                    "indoor-gallery",
                    "morning-market",
                    "night-walk",
                    "snow-garden",
                ),
            ),
            candidate.score,
        )
        replayed = replay_schedule_candidate(schedule_problem, candidate)
        self.assertEqual(
            candidate.report,
            evaluate_timeline(replayed, now=schedule_problem.evaluation_at),
        )
        ordered_days = sorted(
            replayed.days, key=lambda value: (value.date, value.day_id)
        )
        self.assertEqual(
            (date(2027, 1, 9), date(2027, 1, 10)),
            tuple(item.date for item in ordered_days),
        )
        self.assertEqual(
            (
                ("day-1", "hotel-a", "hotel-a"),
                ("day-2", "hotel-a", "hotel-b"),
            ),
            tuple(
                (
                    item.day_id,
                    item.start_location_id,
                    item.end_location_id,
                )
                for item in ordered_days
            ),
        )
        self.assertEqual(
            {
                ("day-1", "hotel-a", "loc-snow-garden"),
                ("day-1", "loc-snow-garden", "loc-indoor-gallery"),
                ("day-1", "loc-indoor-gallery", "hotel-a"),
                ("day-2", "hotel-a", "loc-morning-market"),
                ("day-2", "loc-morning-market", "city-b-ryokan"),
                ("day-2", "city-b-ryokan", "loc-night-walk"),
                ("day-2", "loc-night-walk", "hotel-b"),
            },
            _arc_tuples(candidate.required_arc_keys),
        )

    def test_composite_results_are_identical_after_safe_permutation(self) -> None:
        for builder in (build_busan_problem, build_hokkaido_problem):
            with self.subTest(builder=builder.__name__):
                schedule_problem = builder()
                permuted = _permuted_problem(schedule_problem)

                first = solve_schedule(schedule_problem)
                repeated = solve_schedule(schedule_problem)
                reordered = solve_schedule(permuted)

                self.assertEqual(
                    schedule_problem.problem_id,
                    permuted.problem_id,
                )
                self.assertEqual(first, repeated)
                self.assertEqual(first, reordered)

    def test_full_duration_window_rejects_higher_priority_late_finish(
        self,
    ) -> None:
        high = activity(
            "high",
            order=0,
            duration_min=120,
            priority=100,
            windows=(TimeWindow(time(10), time(11)),),
        )
        low = activity(
            "low",
            order=1,
            duration_min=60,
            priority=10,
            windows=(TimeWindow(time(9), time(13)),),
        )
        trip = state(
            days=(
                day(
                    "high",
                    "low",
                    trip_date=date(2026, 10, 3),
                    end=time(14),
                ),
            ),
            activities=(high, low),
            travel=complete_edges(
                ("hotel", "loc-high", "loc-low"),
                duration=10,
            ),
            constraints=(
                constraint(
                    "choose-one",
                    ConstraintKind.CHOOSE_N,
                    "high",
                    "low",
                    params=(("n", 1),),
                ),
            ),
        )
        schedule_problem = problem(
            trip,
            eligible=("high", "low"),
        )

        high_only = materialize_schedule(
            schedule_problem,
            (ScheduleAssignment("high", "day-1", 0, time(10)),),
            promoted_activity_ids=("high",),
        )
        high_report = evaluate_timeline(
            high_only, now=schedule_problem.evaluation_at
        )
        result = solve_schedule(schedule_problem)

        self.assertEqual(CheckStatus.INFEASIBLE, high_report.status)
        self.assertTrue(
            any(
                issue.code == "TIME_WINDOW_VIOLATION"
                for issue in high_report.issues
            )
        )
        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual(("low",), result.candidate.promoted_activity_ids)
        self.assertEqual(CheckStatus.FEASIBLE, result.candidate.report.status)
        self.assertNotIn(
            "high",
            {
                assignment.activity_id
                for assignment in result.candidate.assignments
            },
        )


if __name__ == "__main__":
    unittest.main()
