"""Exact small-instance gates for the Phase 3 solver decision.

These tests deliberately enumerate tiny layout spaces independently of the
production frontier order.  They keep the pure-Python solver honest without
turning an external optimization package into a production dependency.
"""

from __future__ import annotations

import itertools
import json
import unittest
from dataclasses import replace
from datetime import date, time
from unittest.mock import patch

from tests.test_phase3_scheduling_contracts import (
    EVALUATION_AT,
    activity,
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
)
from trip_planner.scheduler import (
    _EvaluationBudget,
    _Evaluated,
    _Layout,
    _evaluate_assignment_variant,
    _neighbor_layouts,
    solve_schedule,
)
from trip_planner.scheduling import (
    ScheduleContractError,
    ScheduleProblem,
    ScheduleStatus,
)


def _ordered_partitions(
    activity_ids: tuple[str, ...],
    day_ids: tuple[str, ...],
    *,
    promoted_activity_ids: tuple[str, ...] = (),
) -> tuple[_Layout, ...]:
    """Enumerate every ordered allocation of IDs across ordered days."""

    layouts: list[_Layout] = []
    for values in itertools.permutations(activity_ids):
        for cuts in itertools.combinations_with_replacement(
            range(len(values) + 1),
            len(day_ids) - 1,
        ):
            boundaries = (0, *cuts, len(values))
            layouts.append(
                _Layout(
                    day_orders=tuple(
                        (
                            day_id,
                            values[boundaries[index] : boundaries[index + 1]],
                        )
                        for index, day_id in enumerate(day_ids)
                    ),
                    promoted_activity_ids=promoted_activity_ids,
                )
            )
    return tuple(layouts)


def _one_day_candidate_layouts(
    activity_ids: tuple[str, ...],
) -> tuple[_Layout, ...]:
    """Enumerate every order and promoted subset for one candidate day."""

    layouts: list[_Layout] = []
    for values in itertools.permutations(activity_ids):
        for size in range(len(activity_ids) + 1):
            for promoted in itertools.combinations(activity_ids, size):
                layouts.append(
                    _Layout(
                        day_orders=(("day-1", values),),
                        promoted_activity_ids=tuple(sorted(promoted)),
                    )
                )
    return tuple(layouts)


def _exact_best(
    schedule_problem: ScheduleProblem,
    layouts: tuple[_Layout, ...],
) -> tuple[_Evaluated, int]:
    """Evaluate an explicit finite layout set with the trusted kernel."""

    if schedule_problem.limits.max_evaluations < len(layouts):
        raise AssertionError("The exact reference needs one unit per layout.")
    budget = _EvaluationBudget(schedule_problem)
    evaluated: list[_Evaluated] = []
    for layout in layouts:
        try:
            result = budget.evaluate(layout)
        except ScheduleContractError as exc:
            if exc.code != "LAYOUT_MATERIALIZATION_MISMATCH":
                raise
            continue
        if result is not None:
            evaluated.append(result)
    if not evaluated:
        raise AssertionError("The exact reference set produced no evaluation.")
    return min(evaluated, key=_Evaluated.key), budget.used


def _two_move_plateau_problem() -> ScheduleProblem:
    first = activity(
        "a",
        day_id="day-2",
        order=0,
        location_id="hotel",
        duration_min=30,
        decision=DecisionState.SELECTED,
    )
    second = activity(
        "c",
        day_id="day-2",
        order=1,
        location_id="hotel",
        duration_min=30,
        decision=DecisionState.SELECTED,
    )
    trip = state(
        days=(
            day(
                day_id="day-1",
                trip_date=date(2026, 10, 1),
            ),
            day(
                "a",
                "c",
                day_id="day-2",
                trip_date=date(2026, 10, 2),
            ),
        ),
        activities=(first, second),
        travel=(),
        constraints=(
            constraint(
                "a-before-c",
                ConstraintKind.BEFORE,
                "a",
                "c",
            ),
            constraint(
                "c-on-day-1",
                ConstraintKind.ALLOWED_DAY,
                "c",
                params=(("day_id", "day-1"),),
            ),
        ),
    )
    return problem(trip, max_changes=2, evaluations=6)


def _candidate_replacement_problem() -> ScheduleProblem:
    high = activity(
        "high",
        order=0,
        location_id="hotel",
        duration_min=120,
        priority=10,
    )
    low_a = activity(
        "low-a",
        order=1,
        location_id="hotel",
        duration_min=60,
        priority=6,
    )
    low_b = activity(
        "low-b",
        order=2,
        location_id="hotel",
        duration_min=60,
        priority=6,
    )
    trip = state(
        days=(
            day(
                "high",
                "low-a",
                "low-b",
                trip_date=date(2026, 10, 1),
                end=time(11),
            ),
        ),
        activities=(high, low_a, low_b),
        travel=(),
    )
    return problem(
        trip,
        eligible=("high", "low-a", "low-b"),
        evaluations=200,
    )


class Phase3SolverSelectionTests(unittest.TestCase):
    def test_two_activity_constraint_corpus_matches_complete_enumeration(
        self,
    ) -> None:
        activity_ids = ("a", "b")
        layouts = _ordered_partitions(
            activity_ids,
            ("day-1", "day-2"),
        )
        precedence_options = (None, ("a", "b"), ("b", "a"))
        allowed_options = (None, "day-1", "day-2")
        feasible_cases = 0
        total_cases = 0

        for initial in layouts:
            initial_orders = initial.as_mapping()
            positions = {
                activity_id: (day_id, order)
                for day_id, values in initial.day_orders
                for order, activity_id in enumerate(values)
            }
            for precedence in precedence_options:
                for allowed_a, allowed_b in itertools.product(
                    allowed_options,
                    repeat=2,
                ):
                    total_cases += 1
                    constraints = []
                    if precedence is not None:
                        constraints.append(
                            constraint(
                                "precedence",
                                ConstraintKind.BEFORE,
                                *precedence,
                            )
                        )
                    for activity_id, allowed_day in (
                        ("a", allowed_a),
                        ("b", allowed_b),
                    ):
                        if allowed_day is not None:
                            constraints.append(
                                constraint(
                                    f"allowed-{activity_id}",
                                    ConstraintKind.ALLOWED_DAY,
                                    activity_id,
                                    params=(("day_id", allowed_day),),
                                )
                            )
                    activities = tuple(
                        activity(
                            activity_id,
                            day_id=positions[activity_id][0],
                            order=positions[activity_id][1],
                            location_id="hotel",
                            duration_min=30,
                            decision=DecisionState.SELECTED,
                        )
                        for activity_id in activity_ids
                    )
                    trip = state(
                        days=(
                            day(
                                *initial_orders["day-1"],
                                day_id="day-1",
                                trip_date=date(2026, 10, 1),
                            ),
                            day(
                                *initial_orders["day-2"],
                                day_id="day-2",
                                trip_date=date(2026, 10, 2),
                            ),
                        ),
                        activities=activities,
                        travel=(),
                        constraints=tuple(constraints),
                    )
                    schedule_problem = problem(
                        trip,
                        max_changes=2,
                        evaluations=6,
                    )

                    result = solve_schedule(schedule_problem)
                    exact, _ = _exact_best(schedule_problem, layouts)
                    exact_is_feasible = (
                        exact.report.status is CheckStatus.FEASIBLE
                        and exact.score.hard_violation_count == 0
                        and exact.score.missing_required_count == 0
                    )

                    with self.subTest(
                        initial=initial.day_orders,
                        precedence=precedence,
                        allowed=(allowed_a, allowed_b),
                    ):
                        if exact_is_feasible:
                            feasible_cases += 1
                            self.assertEqual(
                                ScheduleStatus.SOLVED,
                                result.status,
                            )
                            self.assertEqual(
                                exact.score.objective_key(),
                                result.candidate.score.objective_key(),
                            )
                            self.assertEqual(
                                exact.schedule_key,
                                result.candidate.schedule_key,
                            )
                        else:
                            self.assertNotEqual(
                                ScheduleStatus.SOLVED,
                                result.status,
                            )

        self.assertEqual(162, total_cases)
        self.assertEqual(150, feasible_cases)

    def test_two_coordinated_moves_cross_a_hard_constraint_plateau(
        self,
    ) -> None:
        schedule_problem = _two_move_plateau_problem()
        layouts = _ordered_partitions(
            ("a", "c"),
            ("day-1", "day-2"),
        )

        result = solve_schedule(schedule_problem)
        exact, exact_evaluations = _exact_best(schedule_problem, layouts)

        self.assertEqual(6, len(layouts))
        self.assertEqual(6, exact_evaluations)
        self.assertEqual(CheckStatus.FEASIBLE, exact.report.status)
        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertLessEqual(result.evaluations_used, 6)
        self.assertEqual(
            (("a", "day-1"), ("c", "day-1")),
            tuple(
                (assignment.activity_id, assignment.day_id)
                for assignment in result.candidate.assignments
            ),
        )
        self.assertEqual(
            exact.score.objective_key(),
            result.candidate.score.objective_key(),
        )
        self.assertEqual(exact.schedule_key, result.candidate.schedule_key)

    def test_candidate_replacement_matches_the_exact_quality_optimum(
        self,
    ) -> None:
        schedule_problem = _candidate_replacement_problem()
        layouts = _one_day_candidate_layouts(("high", "low-a", "low-b"))

        result = solve_schedule(schedule_problem)
        exact, exact_evaluations = _exact_best(schedule_problem, layouts)

        self.assertEqual(48, len(layouts))
        self.assertEqual(48, exact_evaluations)
        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual(
            ("low-a", "low-b"),
            result.candidate.promoted_activity_ids,
        )
        self.assertEqual(12, result.candidate.score.served_priority_points)
        self.assertEqual(
            exact.score.objective_key(),
            result.candidate.score.objective_key(),
        )
        self.assertEqual(exact.schedule_key, result.candidate.schedule_key)

    def test_hard_coverage_candidate_precedes_lexical_placement_variants(
        self,
    ) -> None:
        activities = tuple(
            activity(
                activity_id,
                order=index,
                location_id="hotel",
                duration_min=30,
            )
            for index, activity_id in enumerate(("a", "b", "z-required"))
        )
        trip = state(
            days=(day("a", "b", "z-required"),),
            activities=activities,
            travel=(),
            constraints=(
                constraint(
                    "required-coverage",
                    ConstraintKind.MUST_INCLUDE,
                    "z-required",
                ),
            ),
        )
        schedule_problem = problem(
            trip,
            eligible=("a", "b", "z-required"),
            evaluations=2,
        )

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual(2, result.evaluations_used)
        self.assertEqual(
            ("z-required",),
            result.candidate.promoted_activity_ids,
        )

    def test_non_time_error_does_not_spend_budget_clearing_start_time(
        self,
    ) -> None:
        selected = activity(
            "x",
            day_id="day-2",
            order=0,
            location_id="hotel",
            duration_min=30,
            decision=DecisionState.SELECTED,
            scheduled_start=time(10),
        )
        trip = state(
            days=(
                day(day_id="day-1", trip_date=date(2026, 10, 1)),
                day(
                    "x",
                    day_id="day-2",
                    trip_date=date(2026, 10, 2),
                ),
            ),
            activities=(selected,),
            travel=(),
            constraints=(
                constraint(
                    "x-on-day-1",
                    ConstraintKind.ALLOWED_DAY,
                    "x",
                    params=(("day_id", "day-1"),),
                ),
            ),
        )
        schedule_problem = problem(
            trip,
            max_changes=1,
            evaluations=2,
        )

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual(2, result.evaluations_used)
        self.assertEqual(
            ("day-1", time(10)),
            (
                result.candidate.assignments[0].day_id,
                result.candidate.assignments[0].scheduled_start,
            ),
        )

    def test_evaluation_limit_precedes_tentative_change_budget_diagnosis(
        self,
    ) -> None:
        selected = activity(
            "x",
            day_id="day-2",
            order=0,
            location_id="hotel",
            duration_min=30,
            decision=DecisionState.SELECTED,
        )
        trip = state(
            days=(
                day(day_id="day-1", trip_date=date(2026, 10, 1)),
                day(
                    "x",
                    day_id="day-2",
                    trip_date=date(2026, 10, 2),
                ),
            ),
            activities=(selected,),
            travel=(),
            constraints=(
                constraint(
                    "x-on-day-1",
                    ConstraintKind.ALLOWED_DAY,
                    "x",
                    params=(("day_id", "day-1"),),
                ),
            ),
        )
        schedule_problem = problem(
            trip,
            max_changes=0,
            evaluations=2,
        )

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SEARCH_EXHAUSTED, result.status)
        self.assertEqual(
            "NO_SOLUTION_WITHIN_EVALUATION_LIMIT",
            result.failure.code,
        )
        self.assertEqual(2, result.evaluations_used)

    def test_internal_contract_failure_is_not_hidden_by_evaluation_limit(
        self,
    ) -> None:
        selected = activity(
            "x",
            location_id="hotel",
            decision=DecisionState.SELECTED,
        )
        trip = state(
            days=(day("x"),),
            activities=(selected,),
            travel=(),
        )
        schedule_problem = problem(trip, evaluations=1)

        with patch(
            "trip_planner.scheduler._evaluate_assignment_variant",
            side_effect=ScheduleContractError(
                "LAYOUT_MATERIALIZATION_MISMATCH",
                "trusted materialization diverged",
            ),
        ):
            result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.ENGINE_ERROR, result.status)
        self.assertEqual(
            "LAYOUT_MATERIALIZATION_MISMATCH",
            result.failure.code,
        )
        self.assertEqual(1, result.evaluations_used)

    def test_neighbor_invariant_failures_are_engine_errors(
        self,
    ) -> None:
        selected = activity(
            "x",
            day_id="day-2",
            location_id="hotel",
            decision=DecisionState.SELECTED,
        )
        trip = state(
            days=(
                day(day_id="day-1", trip_date=date(2026, 10, 1)),
                day(
                    "x",
                    day_id="day-2",
                    trip_date=date(2026, 10, 2),
                ),
            ),
            activities=(selected,),
            travel=(),
            constraints=(
                constraint(
                    "x-on-day-1",
                    ConstraintKind.ALLOWED_DAY,
                    "x",
                    params=(("day_id", "day-1"),),
                ),
            ),
        )
        schedule_problem = problem(trip, evaluations=10)
        initial_layout = _Layout(
            day_orders=(
                ("day-1", ()),
                ("day-2", ("x",)),
            )
        )

        for error_code in (
            "PATCH_PROJECTION_MISMATCH",
            "FROZEN_ACTIVITY_CHANGED",
            "UNSTAGEABLE_ANCHOR_REORDER",
        ):
            with self.subTest(error_code=error_code):

                def evaluate_or_fail(
                    current_problem: ScheduleProblem,
                    layout: _Layout,
                    assignments: tuple,
                ) -> _Evaluated:
                    if layout != initial_layout:
                        raise ScheduleContractError(
                            error_code,
                            "trusted projection diverged",
                        )
                    return _evaluate_assignment_variant(
                        current_problem,
                        layout,
                        assignments,
                    )

                with patch(
                    "trip_planner.scheduler._evaluate_assignment_variant",
                    side_effect=evaluate_or_fail,
                ):
                    result = solve_schedule(schedule_problem)

                self.assertEqual(
                    ScheduleStatus.ENGINE_ERROR,
                    result.status,
                )
                self.assertEqual(error_code, result.failure.code)
                self.assertEqual(2, result.evaluations_used)

    def test_fixed_day_candidate_is_promoted_only_at_its_protected_position(
        self,
    ) -> None:
        selected = activity(
            "selected",
            order=0,
            location_id="hotel",
            duration_min=30,
            decision=DecisionState.SELECTED,
        )
        fixed_day = activity(
            "fixed-day",
            order=1,
            location_id="hotel",
            duration_min=30,
            flexibility=Flexibility.FIXED_DAY,
        )
        trip = state(
            days=(day("selected", "fixed-day"),),
            activities=(selected, fixed_day),
            travel=(),
            constraints=(
                constraint(
                    "fixed-day-required",
                    ConstraintKind.MUST_INCLUDE,
                    "fixed-day",
                ),
            ),
        )
        schedule_problem = problem(
            trip,
            eligible=("fixed-day",),
            evaluations=20,
        )

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual(
            ("fixed-day",),
            result.candidate.promoted_activity_ids,
        )
        self.assertEqual(
            (
                ("selected", 0),
                ("fixed-day", 1),
            ),
            tuple(
                (assignment.activity_id, assignment.order)
                for assignment in result.candidate.assignments
            ),
        )
        self.assertEqual(
            0,
            result.candidate.score.protected_change_count,
        )

    def test_directed_roundtrip_cost_beats_short_outbound_bias(
        self,
    ) -> None:
        first = activity(
            "a-short-outbound",
            order=0,
            duration_min=60,
            priority=10,
        )
        second = activity(
            "b-short-roundtrip",
            order=1,
            duration_min=60,
            priority=10,
        )
        trip = state(
            days=(day("a-short-outbound", "b-short-roundtrip"),),
            activities=(first, second),
            travel=(
                edge("hotel", "loc-a-short-outbound", 1),
                edge("loc-a-short-outbound", "hotel", 100),
                edge("hotel", "loc-b-short-roundtrip", 30),
                edge("loc-b-short-roundtrip", "hotel", 30),
            ),
            constraints=(
                constraint(
                    "choose-one",
                    ConstraintKind.CHOOSE_N,
                    "a-short-outbound",
                    "b-short-roundtrip",
                    params=(("n", 1),),
                ),
            ),
        )
        schedule_problem = problem(
            trip,
            eligible=("a-short-outbound", "b-short-roundtrip"),
            evaluations=16,
        )
        permuted_problem = problem(
            replace(
                trip,
                activities=tuple(reversed(trip.activities)),
                travel_estimates=tuple(reversed(trip.travel_estimates)),
            ),
            eligible=("a-short-outbound", "b-short-roundtrip"),
            evaluations=16,
        )
        layouts = _one_day_candidate_layouts(
            ("a-short-outbound", "b-short-roundtrip")
        )

        result = solve_schedule(schedule_problem)
        permuted = solve_schedule(permuted_problem)
        exact, exact_evaluations = _exact_best(schedule_problem, layouts)

        self.assertEqual(8, exact_evaluations)
        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual(result, permuted)
        self.assertEqual(
            ("b-short-roundtrip",),
            result.candidate.promoted_activity_ids,
        )
        self.assertEqual(60.0, result.candidate.score.travel_min)
        self.assertEqual(
            exact.score.objective_key(),
            result.candidate.score.objective_key(),
        )
        self.assertEqual(exact.schedule_key, result.candidate.schedule_key)
        self.assertEqual(
            {
                (
                    "day-1",
                    "hotel",
                    "loc-b-short-roundtrip",
                ),
                (
                    "day-1",
                    "loc-b-short-roundtrip",
                    "hotel",
                ),
            },
            {
                (
                    item["day_id"],
                    item["from_location_id"],
                    item["to_location_id"],
                )
                for encoded in result.candidate.required_arc_keys
                for item in (json.loads(encoded),)
            },
        )

    def test_three_move_plateau_matches_all_ordered_day_partitions(
        self,
    ) -> None:
        activities = tuple(
            activity(
                activity_id,
                day_id="day-2",
                order=index,
                location_id="hotel",
                duration_min=30,
                decision=DecisionState.SELECTED,
            )
            for index, activity_id in enumerate(("a", "b", "c"))
        )
        trip = state(
            days=(
                day(day_id="day-1", trip_date=date(2026, 10, 1)),
                day(
                    "a",
                    "b",
                    "c",
                    day_id="day-2",
                    trip_date=date(2026, 10, 2),
                ),
            ),
            activities=activities,
            travel=(),
            constraints=(
                constraint(
                    "a-before-b",
                    ConstraintKind.BEFORE,
                    "a",
                    "b",
                ),
                constraint(
                    "b-before-c",
                    ConstraintKind.BEFORE,
                    "b",
                    "c",
                ),
                constraint(
                    "c-on-day-1",
                    ConstraintKind.ALLOWED_DAY,
                    "c",
                    params=(("day_id", "day-1"),),
                ),
            ),
        )
        schedule_problem = problem(
            trip,
            max_changes=3,
            evaluations=24,
        )
        layouts = _ordered_partitions(
            ("a", "b", "c"),
            ("day-1", "day-2"),
        )

        result = solve_schedule(schedule_problem)
        exact, exact_evaluations = _exact_best(schedule_problem, layouts)

        self.assertEqual(24, len(layouts))
        self.assertEqual(24, exact_evaluations)
        self.assertEqual(CheckStatus.FEASIBLE, exact.report.status)
        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertLessEqual(result.evaluations_used, 24)
        self.assertEqual(
            exact.score.objective_key(),
            result.candidate.score.objective_key(),
        )
        self.assertEqual(exact.schedule_key, result.candidate.schedule_key)

    def test_large_requires_closure_is_propagated_before_subset_explosion(
        self,
    ) -> None:
        dependency_ids = tuple(
            f"dependency-{index:02d}" for index in range(8)
        )
        activity_ids = ("trigger", *dependency_ids)
        activities = (
            activity(
                "trigger",
                order=0,
                location_id="hotel",
                duration_min=30,
                priority=100,
            ),
            *tuple(
                activity(
                    activity_id,
                    order=index,
                    location_id="hotel",
                    duration_min=30,
                )
                for index, activity_id in enumerate(
                    dependency_ids,
                    start=1,
                )
            ),
        )
        trip = state(
            days=(day(*activity_ids),),
            activities=activities,
            travel=(),
            constraints=(
                constraint(
                    "trigger-requires-all",
                    ConstraintKind.REQUIRES,
                    *activity_ids,
                ),
            ),
        )
        schedule_problem = problem(
            trip,
            eligible=activity_ids,
            evaluations=128,
        )
        baseline_layout = _Layout(
            day_orders=(("day-1", activity_ids),)
        )
        initial_neighbors = tuple(
            _neighbor_layouts(schedule_problem, baseline_layout)
        )
        trusted_budget = _EvaluationBudget(schedule_problem)
        baseline = trusted_budget.evaluate(baseline_layout)
        complete = trusted_budget.evaluate(
            _Layout(
                day_orders=(("day-1", activity_ids),),
                promoted_activity_ids=tuple(sorted(activity_ids)),
            )
        )

        result = solve_schedule(schedule_problem)

        self.assertIsNotNone(baseline)
        self.assertIsNotNone(complete)
        self.assertEqual(82, len(initial_neighbors))
        self.assertEqual(CheckStatus.FEASIBLE, complete.report.status)
        self.assertLess(complete.key(), baseline.key())
        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual(
            tuple(sorted(activity_ids)),
            result.candidate.promoted_activity_ids,
        )
        self.assertEqual(100, result.candidate.score.served_priority_points)
        self.assertLessEqual(result.evaluations_used, 128)

    def test_time_repair_can_clear_a_suffix_without_changing_its_anchor(
        self,
    ) -> None:
        activities = (
            activity(
                "a",
                order=0,
                location_id="hotel",
                duration_min=60,
                decision=DecisionState.SELECTED,
                scheduled_start=time(9, 30),
            ),
            activity(
                "b",
                order=1,
                location_id="hotel",
                duration_min=60,
                decision=DecisionState.SELECTED,
                scheduled_start=time(12),
            ),
            activity(
                "c",
                order=2,
                location_id="hotel",
                duration_min=60,
                decision=DecisionState.SELECTED,
                scheduled_start=time(13),
            ),
        )
        trip = state(
            days=(
                day(
                    "a",
                    "b",
                    "c",
                    end=time(12, 30),
                ),
            ),
            activities=activities,
            travel=(),
        )
        schedule_problem = problem(
            trip,
            max_changes=2,
            evaluations=100,
        )

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual(
            (
                ("a", time(9, 30)),
                ("b", time(10, 30)),
                ("c", time(11, 30)),
            ),
            tuple(
                (
                    assignment.activity_id,
                    assignment.scheduled_start,
                )
                for assignment in result.candidate.assignments
            ),
        )
        self.assertEqual(
            2,
            result.candidate.score.accepted_activity_change_count,
        )
        self.assertEqual(
            ("b", "c"),
            result.candidate.score.changed_activity_ids,
        )

    def test_time_repair_can_clear_noncontiguous_explicit_starts(
        self,
    ) -> None:
        starts = (
            ("a", time(9, 30)),
            ("b", time(12)),
            ("c", time(12)),
            ("d", time(15)),
            ("e", time(14)),
        )
        activities = tuple(
            activity(
                activity_id,
                order=index,
                location_id="hotel",
                duration_min=60,
                decision=DecisionState.SELECTED,
                scheduled_start=scheduled_start,
            )
            for index, (activity_id, scheduled_start) in enumerate(starts)
        )
        trip = state(
            days=(day(*(item[0] for item in starts), end=time(15)),),
            activities=activities,
            travel=(),
            constraints=tuple(
                constraint(
                    f"{left}-before-{right}",
                    ConstraintKind.BEFORE,
                    left,
                    right,
                )
                for (left, _), (right, _) in itertools.pairwise(starts)
            ),
        )
        schedule_problem = problem(
            trip,
            max_changes=2,
            evaluations=64,
        )

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual(
            (
                ("a", time(9, 30)),
                ("b", time(10, 30)),
                ("c", time(12)),
                ("d", time(13)),
                ("e", time(14)),
            ),
            tuple(
                (
                    assignment.activity_id,
                    assignment.scheduled_start,
                )
                for assignment in result.candidate.assignments
            ),
        )
        self.assertEqual(
            ("b", "d"),
            result.candidate.score.changed_activity_ids,
        )

    def test_time_repair_composes_independent_clear_sets_across_days(
        self,
    ) -> None:
        starts = {
            "a": time(9, 30),
            "b": time(12),
            "c": time(13),
            "d": time(9, 30),
            "e": time(12),
            "f": time(13),
        }
        activities = tuple(
            activity(
                activity_id,
                day_id=day_id,
                order=order,
                location_id="hotel",
                duration_min=60,
                decision=DecisionState.SELECTED,
                scheduled_start=starts[activity_id],
            )
            for day_id, day_activity_ids in (
                ("day-1", ("a", "b", "c")),
                ("day-2", ("d", "e", "f")),
            )
            for order, activity_id in enumerate(day_activity_ids)
        )
        trip = state(
            days=(
                day(
                    "a",
                    "b",
                    "c",
                    day_id="day-1",
                    trip_date=date(2026, 10, 1),
                    end=time(12, 30),
                ),
                day(
                    "d",
                    "e",
                    "f",
                    day_id="day-2",
                    trip_date=date(2026, 10, 2),
                    end=time(12, 30),
                ),
            ),
            activities=activities,
            travel=(),
            constraints=(
                constraint(
                    "a-before-b",
                    ConstraintKind.BEFORE,
                    "a",
                    "b",
                ),
                constraint(
                    "b-before-c",
                    ConstraintKind.BEFORE,
                    "b",
                    "c",
                ),
                constraint(
                    "d-before-e",
                    ConstraintKind.BEFORE,
                    "d",
                    "e",
                ),
                constraint(
                    "e-before-f",
                    ConstraintKind.BEFORE,
                    "e",
                    "f",
                ),
                constraint(
                    "day-1-only",
                    ConstraintKind.ALLOWED_DAY,
                    "a",
                    "b",
                    "c",
                    params=(("day_id", "day-1"),),
                ),
                constraint(
                    "day-2-only",
                    ConstraintKind.ALLOWED_DAY,
                    "d",
                    "e",
                    "f",
                    params=(("day_id", "day-2"),),
                ),
            ),
        )
        schedule_problem = problem(
            trip,
            max_changes=4,
            evaluations=128,
        )

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual(
            ("b", "c", "e", "f"),
            result.candidate.score.changed_activity_ids,
        )
        assignments = {
            assignment.activity_id: assignment
            for assignment in result.candidate.assignments
        }
        self.assertEqual(time(9, 30), assignments["a"].scheduled_start)
        self.assertEqual(time(9, 30), assignments["d"].scheduled_start)
        self.assertEqual(time(10, 30), assignments["b"].scheduled_start)
        self.assertEqual(time(10, 30), assignments["e"].scheduled_start)

    def test_exact_reference_is_time_stable(self) -> None:
        schedule_problem = _candidate_replacement_problem()
        layouts = _one_day_candidate_layouts(("high", "low-a", "low-b"))

        first, _ = _exact_best(schedule_problem, layouts)
        second, _ = _exact_best(
            replace(schedule_problem, evaluation_at=EVALUATION_AT),
            tuple(reversed(layouts)),
        )

        self.assertEqual(first.key(), second.key())


if __name__ == "__main__":
    unittest.main()
