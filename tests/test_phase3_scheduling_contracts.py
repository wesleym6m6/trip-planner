"""Offline contract and invariant tests for Phase 3 scheduling."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, time, timezone
from unittest.mock import patch

import trip_planner
from trip_planner.availability import (
    ActivityAvailability,
    AvailabilityDisposition,
    AvailabilityInterval,
)
from trip_planner.composition import ComposedTripState, EvidenceBinding
from trip_planner.models import (
    Activity,
    CheckIssue,
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
from trip_planner.mutations import PlaceActivity, UpdateActivity
from trip_planner.scheduler import solve_schedule
from trip_planner.scheduling import (
    EvidencePolicy,
    ReplanScope,
    ScheduleAssignment,
    ScheduleContractError,
    SchedulePreferences,
    ScheduleProblem,
    ScheduleStatus,
    SearchLimits,
    build_schedule_candidate,
    candidate_to_plan_patch,
    default_replan_scope,
    evaluate_schedule_state,
    materialize_schedule,
    replay_schedule_candidate,
    schedule_problem_from_composed,
    trip_state_digest,
    validate_schedule_problem,
)
from trip_planner.timeline import evaluate_timeline


EVALUATION_AT = datetime(2026, 7, 28, tzinfo=timezone.utc)
HOURS_OBSERVATION_ID = "a" * 64
VERIFIED = EvidenceState.VERIFIED
HARD = ConstraintStrength.HARD


def activity(
    activity_id: str,
    *,
    day_id: str = "day-1",
    order: int = 0,
    location_id: str | None = None,
    duration_min: int = 60,
    priority: int = 0,
    decision: DecisionState = DecisionState.CANDIDATE,
    flexibility: Flexibility = Flexibility.MOVABLE,
    scheduled_start: time | None = None,
    windows: tuple[TimeWindow, ...] = (),
) -> Activity:
    return Activity(
        activity_id=activity_id,
        day_id=day_id,
        order=order,
        title=activity_id,
        location_id=location_id or f"loc-{activity_id}",
        scheduled_start=scheduled_start,
        duration_min=duration_min,
        priority=priority,
        decision_state=decision,
        flexibility=flexibility,
        evidence_state=VERIFIED,
        allowed_windows=windows,
    )


def day(
    *activity_ids: str,
    day_id: str = "day-1",
    trip_date: date = date(2026, 10, 1),
    start: time = time(9),
    end: time = time(18),
    base: str = "hotel",
) -> DaySpec:
    return DaySpec(
        day_id=day_id,
        date=trip_date,
        timezone="Asia/Seoul",
        available_start=start,
        available_end=end,
        start_location_id=base,
        end_location_id=base,
        allowed_modes=("transit",),
        activity_ids=tuple(activity_ids),
    )


def edge(
    from_location: str,
    to_location: str,
    duration: int,
    *,
    buffer: int = 0,
    day_id: str | None = None,
    evidence: EvidenceState = VERIFIED,
) -> TravelEstimate:
    return TravelEstimate(
        from_location_id=from_location,
        to_location_id=to_location,
        mode="transit",
        duration_min=duration,
        day_id=day_id,
        buffer_min=buffer,
        evidence_state=evidence,
        source="fixture",
        recommended=True,
    )


def complete_edges(
    locations: tuple[str, ...],
    *,
    duration: int = 10,
    buffer: int = 0,
) -> tuple[TravelEstimate, ...]:
    return tuple(
        edge(left, right, duration, buffer=buffer)
        for left in locations
        for right in locations
        if left != right
    )


def constraint(
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
        origin="fixture",
    )


def state(
    *,
    days: tuple[DaySpec, ...],
    activities: tuple[Activity, ...],
    travel: tuple[TravelEstimate, ...],
    constraints: tuple[Constraint, ...] = (),
) -> TripState:
    return TripState(
        slug="phase3-fixture",
        title="Phase 3 fixture",
        timezone="Asia/Seoul",
        days=days,
        activities=activities,
        travel_estimates=travel,
        constraints=constraints,
        revision="revision-1",
        schema_version="trip-planner.plan/v1",
        start_date=min(item.date for item in days),
        end_date=max(item.date for item in days),
    )


def problem(
    trip: TripState,
    *,
    eligible: tuple[str, ...] = (),
    day_ids: tuple[str, ...] | None = None,
    max_changes: int = 12,
    evaluations: int = 2_000,
) -> ScheduleProblem:
    return ScheduleProblem(
        state=trip,
        evaluation_at=EVALUATION_AT,
        scope=default_replan_scope(
            trip,
            day_ids=day_ids,
            eligible_candidate_ids=eligible,
            max_accepted_changes=max_changes,
        ),
        trip_id="canonical-phase3-fixture",
        limits=SearchLimits(max_evaluations=evaluations),
    )


def _hard_availability(
    activity_id: str,
    *,
    start_hour_utc: int = 2,
    end_hour_utc: int = 4,
) -> ActivityAvailability:
    return ActivityAvailability(
        activity_id=activity_id,
        disposition=AvailabilityDisposition.HARD_CURRENT,
        intervals=(
            AvailabilityInterval(
                datetime(2026, 10, 1, start_hour_utc, tzinfo=timezone.utc),
                datetime(2026, 10, 1, end_hour_utc, tzinfo=timezone.utc),
            ),
        ),
        evidence_refs=(f"fact:{HOURS_OBSERVATION_ID}",),
        fresh_until=datetime(2026, 10, 2, tzinfo=timezone.utc),
    )


class Phase3KernelSummaryTests(unittest.TestCase):
    def test_day_summary_includes_buffer_and_return_to_base(self) -> None:
        visit = activity(
            "visit",
            decision=DecisionState.SELECTED,
            scheduled_start=None,
        )
        trip = state(
            days=(day("visit", end=time(17)),),
            activities=(visit,),
            travel=(
                edge("hotel", "loc-visit", 30, buffer=20),
                edge("loc-visit", "hotel", 30, buffer=20),
            ),
        )

        report = evaluate_timeline(trip, now=EVALUATION_AT)

        self.assertEqual(CheckStatus.FEASIBLE, report.status)
        summary = report.day_summaries[0]
        self.assertEqual(time(11, 40), summary.completes_at.time())
        self.assertEqual(320.0, summary.end_slack_min)
        self.assertEqual(60.0, summary.travel_min)
        self.assertEqual(40.0, summary.buffer_min)
        self.assertEqual(60.0, summary.service_min)
        self.assertTrue(summary.timing_verified)


class Phase3ProblemContractTests(unittest.TestCase):
    def test_schedule_state_evaluator_is_exported(self) -> None:
        self.assertIs(
            evaluate_schedule_state,
            trip_planner.evaluate_schedule_state,
        )

    def test_process_local_v2_problem_is_rejected_after_v3_bump(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ScheduleContractError,
            "Unsupported schedule problem version",
        ):
            replace(
                problem(
                    state(
                        days=(day(),),
                        activities=(),
                        travel=(),
                    )
                ),
                contract_version="schedule-problem/v2",
                problem_id="",
            )

    def test_problem_id_tracks_activity_availability_sidecar(self) -> None:
        visit = activity("visit", decision=DecisionState.SELECTED)
        trip = state(
            days=(day("visit"),),
            activities=(visit,),
            travel=complete_edges(("hotel", "loc-visit"), duration=0),
        )
        baseline = problem(trip)
        with_availability = replace(
            baseline,
            activity_availability=(_hard_availability("visit"),),
            problem_id="",
        )

        self.assertNotEqual(baseline.problem_id, with_availability.problem_id)
        self.assertEqual(
            (_hard_availability("visit"),),
            with_availability.activity_availability,
        )

    def test_problem_rejects_unknown_or_duplicate_availability_activity(self) -> None:
        visit = activity("visit", decision=DecisionState.SELECTED)
        trip = state(
            days=(day("visit"),),
            activities=(visit,),
            travel=complete_edges(("hotel", "loc-visit"), duration=0),
        )
        with self.assertRaisesRegex(ScheduleContractError, "unknown activities"):
            replace(
                problem(trip),
                activity_availability=(_hard_availability("missing"),),
                problem_id="",
            )
        sidecar = _hard_availability("visit")
        with self.assertRaisesRegex(ScheduleContractError, "must be unique"):
            replace(
                problem(trip),
                activity_availability=(sidecar, sidecar),
                problem_id="",
            )

    def test_composed_problem_carries_activity_availability(self) -> None:
        visit = activity("visit", decision=DecisionState.SELECTED)
        canonical = state(
            days=(day("visit"),),
            activities=(visit,),
            travel=(),
        )
        revision = "1" * 64
        composed_state = replace(canonical, revision=revision)
        binding = EvidenceBinding(
            policy_registry_revision="2" * 64,
            store_revision="3" * 64,
            evidence_revision="4" * 64,
            evaluation_at=EVALUATION_AT,
            purge_checked_at=EVALUATION_AT,
            snapshot_id="5" * 64,
            used_observation_ids=(HOURS_OBSERVATION_ID,),
        )
        composed = ComposedTripState(
            state=composed_state,
            trip_id="canonical-phase3-fixture",
            plan_revision=revision,
            canonical_state_digest=trip_state_digest(canonical),
            composed_state_digest=trip_state_digest(composed_state),
            evidence=binding,
            activity_availability=(_hard_availability("visit"),),
        )

        self.assertEqual(
            composed.activity_availability,
            schedule_problem_from_composed(composed).activity_availability,
        )

    def test_evidence_bound_problem_rejects_unbound_hard_availability(
        self,
    ) -> None:
        visit = activity("visit", decision=DecisionState.SELECTED)
        trip = state(
            days=(day("visit"),),
            activities=(visit,),
            travel=(),
        )
        binding = EvidenceBinding(
            policy_registry_revision="2" * 64,
            store_revision="3" * 64,
            evidence_revision="4" * 64,
            evaluation_at=EVALUATION_AT,
            purge_checked_at=EVALUATION_AT,
            snapshot_id="5" * 64,
        )

        with self.assertRaisesRegex(
            ScheduleContractError,
            "absent from its evidence binding",
        ):
            replace(
                problem(trip),
                evidence_binding=binding,
                activity_availability=(_hard_availability("visit"),),
                problem_id="",
            )

    def test_problem_rechecks_hard_availability_reference_format(self) -> None:
        visit = activity("visit", decision=DecisionState.SELECTED)
        trip = state(
            days=(day("visit"),),
            activities=(visit,),
            travel=(),
        )
        malformed_refs = (
            f"xxxxx{HOURS_OBSERVATION_ID}",
            "fact:not-a-digest",
        )

        for reference in malformed_refs:
            with self.subTest(reference=reference):
                sidecar = _hard_availability("visit")
                object.__setattr__(sidecar, "evidence_refs", (reference,))
                with self.assertRaisesRegex(
                    ScheduleContractError,
                    r"fact:<sha256>",
                ):
                    replace(
                        problem(trip),
                        activity_availability=(sidecar,),
                        problem_id="",
                    )

    def test_default_empty_sidecar_keeps_legacy_evaluation(self) -> None:
        visit = activity("visit", decision=DecisionState.SELECTED)
        trip = state(
            days=(day("visit"),),
            activities=(visit,),
            travel=(),
        )
        schedule_problem = problem(trip)

        self.assertEqual(
            evaluate_timeline(trip, now=EVALUATION_AT),
            evaluate_schedule_state(schedule_problem, trip),
        )

    def test_current_availability_constrains_solver_and_replay(self) -> None:
        visit = activity("visit", decision=DecisionState.SELECTED)
        trip = state(
            days=(day("visit"),),
            activities=(visit,),
            travel=complete_edges(("hotel", "loc-visit"), duration=0),
        )
        schedule_problem = replace(
            problem(trip),
            activity_availability=(_hard_availability("visit"),),
            problem_id="",
        )

        baseline = solve_schedule(problem(trip))
        constrained = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SOLVED, baseline.status)
        self.assertNotEqual(ScheduleStatus.SOLVED, constrained.status)

        valid_visit = activity(
            "visit",
            decision=DecisionState.SELECTED,
            scheduled_start=time(11),
        )
        valid_trip = state(
            days=(day("visit"),),
            activities=(valid_visit,),
            travel=complete_edges(("hotel", "loc-visit"), duration=0),
        )
        valid_problem = replace(
            problem(valid_trip),
            activity_availability=(_hard_availability("visit"),),
            problem_id="",
        )
        result = solve_schedule(valid_problem)

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        replayed = replay_schedule_candidate(valid_problem, result.candidate)
        self.assertEqual(
            result.candidate.report,
            evaluate_schedule_state(valid_problem, replayed),
        )

    def test_verification_availability_never_becomes_green(self) -> None:
        visit = activity("visit", decision=DecisionState.SELECTED)
        trip = state(
            days=(day("visit"),),
            activities=(visit,),
            travel=complete_edges(("hotel", "loc-visit"), duration=0),
        )
        needs_verification = ActivityAvailability(
            activity_id="visit",
            disposition=AvailabilityDisposition.NEEDS_VERIFICATION,
            evidence_refs=("fact:regular-hours",),
            reason="regular_opening_hours",
        )
        schedule_problem = replace(
            problem(trip),
            activity_availability=(needs_verification,),
            problem_id="",
        )

        result = solve_schedule(schedule_problem)
        report = evaluate_schedule_state(schedule_problem, trip)

        self.assertEqual(ScheduleStatus.NEEDS_EVIDENCE, result.status)
        self.assertEqual(CheckStatus.NEEDS_VERIFICATION, report.status)
        self.assertIn(
            "OPENING_HOURS_NEEDS_VERIFICATION",
            {item.code for item in report.issues},
        )

    def test_composed_problem_binds_canonical_and_evidence_identity(
        self,
    ) -> None:
        canonical = state(
            days=(day(),),
            activities=(),
            travel=(),
        )
        revision = "1" * 64
        composed_state = replace(canonical, revision=revision)
        binding = EvidenceBinding(
            policy_registry_revision="2" * 64,
            store_revision="3" * 64,
            evidence_revision="4" * 64,
            evaluation_at=EVALUATION_AT,
            purge_checked_at=EVALUATION_AT,
            snapshot_id="5" * 64,
        )
        composed = ComposedTripState(
            state=composed_state,
            trip_id="canonical-phase3-fixture",
            plan_revision=revision,
            canonical_state_digest=trip_state_digest(canonical),
            composed_state_digest=trip_state_digest(composed_state),
            evidence=binding,
        )

        schedule_problem = schedule_problem_from_composed(composed)

        self.assertIs(schedule_problem.state, composed_state)
        self.assertEqual(
            composed.canonical_state_digest,
            schedule_problem.canonical_state_digest,
        )
        self.assertEqual(binding, schedule_problem.evidence_binding)
        self.assertEqual(
            binding.evaluation_at,
            schedule_problem.evaluation_at,
        )
        self.assertNotIn("Phase 3 fixture", repr(schedule_problem))

    def test_problem_id_tracks_stable_evidence_binding_only(self) -> None:
        trip = state(days=(day(),), activities=(), travel=())
        first_binding = EvidenceBinding(
            policy_registry_revision="2" * 64,
            store_revision="3" * 64,
            evidence_revision="4" * 64,
            evaluation_at=EVALUATION_AT,
            purge_checked_at=EVALUATION_AT,
            snapshot_id="5" * 64,
        )
        purge_clock_only = replace(
            first_binding,
            purge_checked_at=EVALUATION_AT.replace(hour=1),
            snapshot_id="6" * 64,
            binding_digest="",
        )
        changed_store = replace(
            first_binding,
            store_revision="7" * 64,
            binding_digest="",
        )

        def evidence_problem(binding: EvidenceBinding) -> ScheduleProblem:
            return ScheduleProblem(
                state=trip,
                evaluation_at=EVALUATION_AT,
                scope=default_replan_scope(trip),
                trip_id="canonical-phase3-fixture",
                canonical_state_digest=trip_state_digest(trip),
                evidence_binding=binding,
            )

        first = evidence_problem(first_binding)
        same_semantics = evidence_problem(purge_clock_only)
        changed = evidence_problem(changed_store)

        self.assertEqual(
            first_binding.binding_digest,
            purge_clock_only.binding_digest,
        )
        self.assertEqual(first.problem_id, same_semantics.problem_id)
        self.assertNotEqual(first.problem_id, changed.problem_id)

    def test_problem_digest_ignores_nonsemantic_container_permutation(self) -> None:
        alpha = activity("alpha", order=0, decision=DecisionState.SELECTED)
        beta = activity("beta", order=1)
        travel = complete_edges(("hotel", "loc-alpha", "loc-beta"))
        first = state(
            days=(day("alpha", "beta"),),
            activities=(alpha, beta),
            travel=travel,
        )
        permuted = replace(
            first,
            activities=tuple(reversed(first.activities)),
            travel_estimates=tuple(reversed(first.travel_estimates)),
        )

        first_problem = problem(first, eligible=("beta",))
        second_problem = problem(permuted, eligible=("beta",))

        self.assertEqual(
            first_problem.base_state_digest,
            second_problem.base_state_digest,
        )
        self.assertEqual(first_problem.problem_id, second_problem.problem_id)

    def test_semantic_state_digest_excludes_revision_but_problem_id_does_not(
        self,
    ) -> None:
        visit = activity("visit", decision=DecisionState.SELECTED)
        first = state(
            days=(day("visit"),),
            activities=(visit,),
            travel=(
                edge("hotel", "loc-visit", 10),
                edge("loc-visit", "hotel", 10),
            ),
        )
        second = replace(first, revision="revision-2")

        first_problem = problem(first)
        second_problem = problem(second)

        self.assertEqual(
            first_problem.base_state_digest,
            second_problem.base_state_digest,
        )
        self.assertNotEqual(first_problem.problem_id, second_problem.problem_id)

    def test_numeric_and_text_constraint_params_have_distinct_digests(
        self,
    ) -> None:
        numeric = state(
            days=(day(),),
            activities=(),
            travel=(),
            constraints=(
                constraint(
                    "daily",
                    ConstraintKind.DAILY_LIMIT,
                    "day-1",
                    params=(("max_minutes", 10.0),),
                ),
            ),
        )
        text = replace(
            numeric,
            constraints=(
                constraint(
                    "daily",
                    ConstraintKind.DAILY_LIMIT,
                    "day-1",
                    params=(("max_minutes", "10.0"),),
                ),
            ),
        )

        numeric_problem = problem(numeric)
        text_problem = problem(text)

        self.assertNotEqual(
            numeric_problem.base_state_digest,
            text_problem.base_state_digest,
        )
        self.assertNotEqual(
            numeric_problem.problem_id,
            text_problem.problem_id,
        )
        self.assertEqual(
            ScheduleStatus.SOLVED,
            solve_schedule(numeric_problem).status,
        )
        invalid = solve_schedule(text_problem)
        self.assertEqual(ScheduleStatus.INVALID_INPUT, invalid.status)
        self.assertEqual("INVALID_CONSTRAINT", invalid.failure.code)

    def test_mapping_like_param_order_is_canonicalized(self) -> None:
        first_constraint = constraint(
            "daily",
            ConstraintKind.DAILY_LIMIT,
            "day-1",
            params=(
                ("max_minutes", 180.0),
                ("max_activities", 3),
            ),
        )
        permuted_constraint = constraint(
            "daily",
            ConstraintKind.DAILY_LIMIT,
            "day-1",
            params=tuple(reversed(first_constraint.params)),
        )
        first_issue = CheckIssue(
            code="FIXTURE",
            severity=IssueSeverity.INFO,
            message="fixture",
            details=(("z", 1), ("a", 2)),
        )
        permuted_issue = CheckIssue(
            code="FIXTURE",
            severity=IssueSeverity.INFO,
            message="fixture",
            details=tuple(reversed(first_issue.details)),
        )
        first = state(
            days=(day(),),
            activities=(),
            travel=(),
            constraints=(first_constraint,),
        )
        permuted = replace(
            first,
            constraints=(permuted_constraint,),
            load_issues=(permuted_issue,),
        )
        with_issue = replace(first, load_issues=(first_issue,))

        self.assertEqual(first_constraint, permuted_constraint)
        self.assertEqual(first_issue, permuted_issue)
        self.assertEqual(
            problem(with_issue).base_state_digest,
            problem(permuted).base_state_digest,
        )
        self.assertEqual(
            problem(with_issue).problem_id,
            problem(permuted).problem_id,
        )

    def test_protected_activity_cannot_be_declared_mutable(self) -> None:
        dinner = activity(
            "dinner",
            decision=DecisionState.BOOKED,
            flexibility=Flexibility.FIXED_TIME,
            scheduled_start=time(18),
        )
        trip = state(
            days=(day("dinner"),),
            activities=(dinner,),
            travel=(
                edge("hotel", "loc-dinner", 10),
                edge("loc-dinner", "hotel", 10),
            ),
        )
        schedule_problem = ScheduleProblem(
            state=trip,
            evaluation_at=EVALUATION_AT,
            scope=ReplanScope(
                day_ids=("day-1",),
                mutable_activity_ids=("dinner",),
            ),
            trip_id="canonical-phase3-fixture",
        )

        failures = validate_schedule_problem(schedule_problem)

        self.assertEqual("PROTECTED_ACTIVITY_MUTABLE", failures[0].code)

    def test_materializer_requires_complete_active_schedule(self) -> None:
        selected = activity("selected", decision=DecisionState.SELECTED)
        trip = state(
            days=(day("selected"),),
            activities=(selected,),
            travel=(
                edge("hotel", "loc-selected", 10),
                edge("loc-selected", "hotel", 10),
            ),
        )
        schedule_problem = problem(trip)

        with self.assertRaisesRegex(
            ScheduleContractError, "complete active schedule"
        ):
            materialize_schedule(schedule_problem, ())

    def test_materializer_rejects_mutable_move_outside_day_scope(self) -> None:
        selected = activity(
            "selected",
            day_id="day-1",
            decision=DecisionState.SELECTED,
        )
        trip = state(
            days=(
                day("selected", day_id="day-1"),
                day(
                    day_id="day-2",
                    trip_date=date(2026, 10, 2),
                ),
            ),
            activities=(selected,),
            travel=(),
        )
        schedule_problem = problem(trip, day_ids=("day-1",))

        with self.assertRaisesRegex(
            ScheduleContractError, "cannot move outside"
        ):
            materialize_schedule(
                schedule_problem,
                (
                    ScheduleAssignment(
                        activity_id="selected",
                        day_id="day-2",
                        order=0,
                        scheduled_start=time(10),
                    ),
                ),
            )

    def test_common_candidate_builder_enforces_change_budget(self) -> None:
        first = activity(
            "a",
            order=1,
            decision=DecisionState.SELECTED,
        )
        second = activity(
            "b",
            order=0,
            decision=DecisionState.SELECTED,
        )
        trip = state(
            days=(day("b", "a"),),
            activities=(first, second),
            travel=complete_edges(("hotel", "loc-a", "loc-b")),
            constraints=(
                constraint("order", ConstraintKind.BEFORE, "a", "b"),
            ),
        )
        schedule_problem = problem(trip, max_changes=0)

        with self.assertRaisesRegex(
            ScheduleContractError, "max_accepted_changes"
        ):
            build_schedule_candidate(
                schedule_problem,
                (
                    ScheduleAssignment("a", "day-1", 0, time(9, 10)),
                    ScheduleAssignment("b", "day-1", 1, time(10, 20)),
                ),
                solver="test",
            )

    def test_preferences_reject_non_finite_numbers(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ScheduleContractError, "finite"
                ):
                    SchedulePreferences(minimum_end_slack_min=value)

    def test_problem_rejects_identities_that_plan_patch_cannot_encode(
        self,
    ) -> None:
        trip = state(
            days=(day(),),
            activities=(),
            travel=(),
        )
        for invalid_trip_id in ("x" * 257, "canonical\x00trip"):
            with self.subTest(trip_id=repr(invalid_trip_id)):
                with self.assertRaises(ScheduleContractError) as raised:
                    ScheduleProblem(
                        state=trip,
                        evaluation_at=EVALUATION_AT,
                        scope=default_replan_scope(trip),
                        trip_id=invalid_trip_id,
                    )
                self.assertEqual("INVALID_INPUT", raised.exception.code)

        with self.assertRaises(ScheduleContractError) as raised:
            ScheduleProblem(
                state=replace(trip, revision="r" * 257),
                evaluation_at=EVALUATION_AT,
                scope=default_replan_scope(trip),
                trip_id="canonical-phase3-fixture",
            )
        self.assertEqual("INVALID_INPUT", raised.exception.code)

    def test_local_time_fold_is_rejected_as_non_persistable(self) -> None:
        with self.assertRaisesRegex(ValueError, "fold"):
            TimeWindow(time(1, 30, fold=1), time(2, 30))
        with self.assertRaises(ScheduleContractError) as raised:
            ScheduleAssignment(
                "activity",
                "day-1",
                0,
                time(1, 30, fold=1),
            )
        self.assertEqual("INVALID_CANDIDATE", raised.exception.code)


class Phase3DeterministicSchedulerTests(unittest.TestCase):
    def _priority_trip(
        self,
        *,
        optional_ids: tuple[str, str] = ("optional-high", "optional-low"),
        priorities: tuple[int, int] = (90, 40),
    ) -> TripState:
        must = activity(
            "must",
            order=0,
            duration_min=120,
            windows=(TimeWindow(time(9, 30), time(12)),),
        )
        first = activity(
            optional_ids[0],
            order=1,
            duration_min=90,
            priority=priorities[0],
        )
        second = activity(
            optional_ids[1],
            order=2,
            duration_min=60,
            priority=priorities[1],
        )
        locations = (
            "hotel",
            "loc-must",
            f"loc-{optional_ids[0]}",
            f"loc-{optional_ids[1]}",
        )
        return state(
            days=(day("must", *optional_ids, end=time(15)),),
            activities=(must, first, second),
            travel=complete_edges(locations, duration=20, buffer=10),
            constraints=(
                constraint("must-do", ConstraintKind.MUST_INCLUDE, "must"),
                constraint(
                    "choose-one",
                    ConstraintKind.CHOOSE_N,
                    *optional_ids,
                    params=(("n", 1),),
                ),
            ),
        )

    def test_must_do_and_priority_beat_shorter_optional(self) -> None:
        trip = self._priority_trip()
        schedule_problem = problem(
            trip,
            eligible=("must", "optional-high", "optional-low"),
        )

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual(
            ("must", "optional-high"),
            result.candidate.promoted_activity_ids,
        )
        self.assertEqual(90, result.candidate.score.served_priority_points)
        self.assertEqual(CheckStatus.FEASIBLE, result.candidate.report.status)

    def test_equal_priority_tie_uses_stable_activity_id(self) -> None:
        trip = self._priority_trip(
            optional_ids=("opt-b", "opt-a"),
            priorities=(50, 50),
        )
        # Equalize every non-ID property so input order cannot decide the tie.
        activities = tuple(
            replace(item, duration_min=60)
            if item.activity_id in {"opt-a", "opt-b"}
            else item
            for item in trip.activities
        )
        trip = replace(trip, activities=tuple(reversed(activities)))
        schedule_problem = problem(
            trip,
            eligible=("must", "opt-a", "opt-b"),
        )

        results = [solve_schedule(schedule_problem) for _ in range(10)]

        self.assertTrue(
            all(result.status is ScheduleStatus.SOLVED for result in results)
        )
        self.assertTrue(
            all(
                result.candidate.promoted_activity_ids == ("must", "opt-a")
                for result in results
            )
        )
        self.assertEqual(
            1, len({result.candidate.candidate_id for result in results})
        )

    def test_booked_time_and_full_activity_window_are_preserved(self) -> None:
        museum = activity(
            "museum",
            order=0,
            duration_min=180,
            windows=(TimeWindow(time(10), time(16)),),
        )
        dinner = activity(
            "dinner",
            order=1,
            duration_min=120,
            decision=DecisionState.BOOKED,
            flexibility=Flexibility.FIXED_TIME,
            scheduled_start=time(18),
        )
        trip = state(
            days=(day("museum", "dinner", end=time(21)),),
            activities=(museum, dinner),
            travel=(
                edge("hotel", "loc-museum", 30, buffer=10),
                edge("loc-museum", "loc-dinner", 30, buffer=10),
                edge("loc-dinner", "hotel", 20, buffer=10),
                edge("hotel", "loc-dinner", 20, buffer=10),
                edge("loc-dinner", "loc-museum", 30, buffer=10),
                edge("loc-museum", "hotel", 30, buffer=10),
            ),
            constraints=(
                constraint(
                    "museum-required",
                    ConstraintKind.MUST_INCLUDE,
                    "museum",
                ),
            ),
        )
        schedule_problem = problem(trip, eligible=("museum",))

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        assignments = {
            item.activity_id: item for item in result.candidate.assignments
        }
        self.assertEqual(time(10), assignments["museum"].scheduled_start)
        self.assertEqual(time(18), assignments["dinner"].scheduled_start)
        self.assertEqual(1, assignments["dinner"].order)
        self.assertEqual(0, result.candidate.score.protected_change_count)
        summary = result.candidate.report.day_summaries[0]
        self.assertEqual(time(20, 30), summary.completes_at.time())
        self.assertEqual(30.0, summary.end_slack_min)

    def test_feasible_accepted_start_time_is_not_silently_pulled_earlier(
        self,
    ) -> None:
        accepted = activity(
            "accepted",
            decision=DecisionState.SELECTED,
            scheduled_start=time(13),
        )
        trip = state(
            days=(day("accepted"),),
            activities=(accepted,),
            travel=(
                edge("hotel", "loc-accepted", 10),
                edge("loc-accepted", "hotel", 10),
            ),
        )
        schedule_problem = problem(trip)

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual(
            time(13), result.candidate.assignments[0].scheduled_start
        )
        self.assertEqual(
            0, result.candidate.score.accepted_activity_change_count
        )
        self.assertEqual(
            0, result.candidate.score.accepted_time_shift_deci_min
        )

    def test_buffer_and_return_capacity_exclude_false_feasible_optional(self) -> None:
        first = activity("a", order=0, duration_min=150)
        second = activity("b", order=1, duration_min=150)
        optional = activity("c", order=2, duration_min=30, priority=100)
        trip = state(
            days=(day("a", "b", "c", end=time(17)),),
            activities=(first, second, optional),
            travel=complete_edges(
                ("hotel", "loc-a", "loc-b", "loc-c"),
                duration=30,
                buffer=20,
            ),
            constraints=(
                constraint(
                    "required",
                    ConstraintKind.MUST_INCLUDE,
                    "a",
                    "b",
                ),
            ),
        )
        schedule_problem = problem(trip, eligible=("a", "b", "c"))

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual(("a", "b"), result.candidate.promoted_activity_ids)
        summary = result.candidate.report.day_summaries[0]
        self.assertEqual(90.0, summary.travel_min)
        self.assertEqual(60.0, summary.buffer_min)
        self.assertEqual(time(16, 30), summary.completes_at.time())
        self.assertEqual(30.0, summary.end_slack_min)

    def test_missing_required_arc_returns_evidence_failure_without_candidate(
        self,
    ) -> None:
        first = activity("a", order=0)
        second = activity("b", order=1)
        trip = state(
            days=(day("a", "b"),),
            activities=(first, second),
            travel=(
                edge("hotel", "loc-a", 10),
                edge("loc-b", "hotel", 10),
            ),
            constraints=(
                constraint(
                    "required",
                    ConstraintKind.MUST_INCLUDE,
                    "a",
                    "b",
                ),
                constraint("order", ConstraintKind.BEFORE, "a", "b"),
            ),
        )
        schedule_problem = problem(trip, eligible=("a", "b"))

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.NEEDS_EVIDENCE, result.status)
        self.assertIsNone(result.candidate)
        self.assertIn(
            "day-1|loc-a|loc-b",
            result.failure.missing_arc_keys,
        )

    def test_verified_lower_priority_choice_beats_unverified_high_priority(
        self,
    ) -> None:
        high = activity("high", order=0, priority=100)
        low = activity("low", order=1, priority=10)
        trip = state(
            days=(day("high", "low"),),
            activities=(high, low),
            travel=(
                edge(
                    "hotel",
                    "loc-high",
                    10,
                    evidence=EvidenceState.UNVERIFIED,
                ),
                edge(
                    "loc-high",
                    "hotel",
                    10,
                    evidence=EvidenceState.UNVERIFIED,
                ),
                edge("hotel", "loc-low", 10),
                edge("loc-low", "hotel", 10),
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
        schedule_problem = problem(trip, eligible=("high", "low"))

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual(("low",), result.candidate.promoted_activity_ids)
        self.assertEqual(0, result.candidate.score.verification_risk_count)

    def test_partial_replan_keeps_out_of_scope_day_exact(self) -> None:
        anchor = activity(
            "day-1-anchor",
            day_id="day-1",
            order=0,
            decision=DecisionState.SELECTED,
            scheduled_start=time(10),
        )
        candidate = activity(
            "day-2-candidate",
            day_id="day-2",
            order=0,
        )
        trip = state(
            days=(
                day("day-1-anchor", day_id="day-1"),
                day(
                    "day-2-candidate",
                    day_id="day-2",
                    trip_date=date(2026, 10, 2),
                ),
            ),
            activities=(anchor, candidate),
            travel=(
                edge("hotel", "loc-day-1-anchor", 10, day_id="day-1"),
                edge("loc-day-1-anchor", "hotel", 10, day_id="day-1"),
                edge("hotel", "loc-day-2-candidate", 10, day_id="day-2"),
                edge("loc-day-2-candidate", "hotel", 10, day_id="day-2"),
            ),
            constraints=(
                constraint(
                    "required",
                    ConstraintKind.MUST_INCLUDE,
                    "day-2-candidate",
                ),
            ),
        )
        schedule_problem = problem(
            trip,
            eligible=("day-2-candidate",),
            day_ids=("day-2",),
        )

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        anchor_assignment = next(
            item
            for item in result.candidate.assignments
            if item.activity_id == "day-1-anchor"
        )
        self.assertEqual(
            ScheduleAssignment(
                activity_id="day-1-anchor",
                day_id="day-1",
                order=0,
                scheduled_start=time(10),
            ),
            anchor_assignment,
        )

    def test_change_budget_blocks_required_accepted_reorder(self) -> None:
        first = activity(
            "a",
            order=1,
            decision=DecisionState.SELECTED,
        )
        second = activity(
            "b",
            order=0,
            decision=DecisionState.SELECTED,
        )
        trip = state(
            days=(day("b", "a"),),
            activities=(first, second),
            travel=complete_edges(("hotel", "loc-a", "loc-b")),
            constraints=(
                constraint("order", ConstraintKind.BEFORE, "a", "b"),
            ),
        )
        schedule_problem = problem(trip, max_changes=0)

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SEARCH_EXHAUSTED, result.status)
        self.assertEqual("CHANGE_BUDGET_EXCEEDED", result.failure.code)
        self.assertIsNone(result.candidate)

    def test_fixed_day_reorder_is_not_claimed_solved_or_proven(self) -> None:
        fixed_day = activity(
            "a",
            order=1,
            decision=DecisionState.SELECTED,
            flexibility=Flexibility.FIXED_DAY,
        )
        movable = activity(
            "b",
            order=0,
            decision=DecisionState.SELECTED,
        )
        trip = state(
            days=(day("b", "a"),),
            activities=(fixed_day, movable),
            travel=complete_edges(("hotel", "loc-a", "loc-b")),
            constraints=(
                constraint("order", ConstraintKind.BEFORE, "a", "b"),
            ),
        )
        schedule_problem = problem(trip)

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SEARCH_EXHAUSTED, result.status)
        self.assertEqual(
            "NO_SOLUTION_WITHIN_BUDGET", result.failure.code
        )
        self.assertIn("a", result.failure.activity_ids)
        self.assertIsNone(result.candidate)

    def test_candidate_patch_uses_only_v1_schedule_operations(self) -> None:
        trip = self._priority_trip()
        schedule_problem = problem(
            trip,
            eligible=("must", "optional-high", "optional-low"),
        )
        result = solve_schedule(schedule_problem)
        self.assertEqual(ScheduleStatus.SOLVED, result.status)

        patch = candidate_to_plan_patch(
            schedule_problem, result.candidate
        )

        self.assertEqual("canonical-phase3-fixture", patch.trip_id)
        self.assertNotEqual(schedule_problem.state.slug, patch.trip_id)
        self.assertTrue(patch.operations)
        self.assertTrue(
            all(
                isinstance(operation, (UpdateActivity, PlaceActivity))
                for operation in patch.operations
            )
        )
        promoted_updates = {
            operation.activity_id
            for operation in patch.operations
            if isinstance(operation, UpdateActivity)
        }
        self.assertEqual(
            set(result.candidate.promoted_activity_ids),
            promoted_updates,
        )

    def test_candidate_patch_preserves_subminute_start_time(self) -> None:
        selected = activity(
            "selected",
            decision=DecisionState.SELECTED,
            location_id="hotel",
        )
        trip = state(
            days=(day("selected"),),
            activities=(selected,),
            travel=(),
        )
        schedule_problem = problem(trip)
        candidate = build_schedule_candidate(
            schedule_problem,
            (
                ScheduleAssignment(
                    "selected",
                    "day-1",
                    0,
                    time(9, 0, 30, 123456),
                ),
            ),
            solver="test",
        )

        patch = candidate_to_plan_patch(schedule_problem, candidate)

        place = next(
            operation
            for operation in patch.operations
            if isinstance(operation, PlaceActivity)
        )
        self.assertEqual("09:00:30.123456", place.scheduled_start)


class Phase3CompositeBenchmarkTests(unittest.TestCase):
    def test_busan_transit_hotel_anchors_choose_priority_without_moving_booking(
        self,
    ) -> None:
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
        travel = list(
            complete_edges(locations, duration=30, buffer=10)
        )
        # Make the expected neighborhood slightly better without changing
        # the priority layer that decides which optional activities are served.
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
        schedule_problem = problem(
            trip,
            eligible=("b-coast", "b-art", "b-market", "b-park", "b-view"),
            evaluations=10_000,
        )

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual(
            ("b-art", "b-coast", "b-market", "b-park"),
            result.candidate.promoted_activity_ids,
        )
        self.assertEqual(140, result.candidate.score.served_priority_points)
        assignments = {
            item.activity_id: item for item in result.candidate.assignments
        }
        self.assertEqual("day-1", assignments["b-dinner"].day_id)
        self.assertEqual(2, assignments["b-dinner"].order)
        self.assertEqual(time(18), assignments["b-dinner"].scheduled_start)
        self.assertEqual(0, result.candidate.score.protected_change_count)
        summaries = {
            item.day_id: item for item in result.candidate.report.day_summaries
        }
        self.assertEqual("busan-hotel", trip.days[0].end_location_id)
        self.assertGreaterEqual(summaries["day-1"].end_slack_min, 0)
        self.assertGreaterEqual(summaries["day-2"].end_slack_min, 0)

    def test_hokkaido_winter_buffers_and_cross_city_end_anchor_are_elapsed(
        self,
    ) -> None:
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
        schedule_problem = problem(
            trip,
            eligible=(
                "snow-garden",
                "indoor-gallery",
                "morning-market",
                "night-walk",
            ),
            evaluations=10_000,
        )

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual(
            (
                "indoor-gallery",
                "morning-market",
                "night-walk",
                "snow-garden",
            ),
            result.candidate.promoted_activity_ids,
        )
        assignments = {
            item.activity_id: item for item in result.candidate.assignments
        }
        self.assertEqual(time(8, 35), assignments["morning-market"].scheduled_start)
        self.assertEqual(time(16), assignments["ryokan-checkin"].scheduled_start)
        self.assertEqual(290.0, result.candidate.score.travel_min)
        self.assertEqual(165.0, result.candidate.score.buffer_min)
        summaries = {
            item.day_id: item for item in result.candidate.report.day_summaries
        }
        self.assertEqual(time(14, 55), summaries["day-1"].completes_at.time())
        self.assertEqual(time(18, 40), summaries["day-2"].completes_at.time())
        self.assertEqual(80.0, summaries["day-2"].end_slack_min)
        self.assertTrue(summaries["day-2"].timing_verified)

    def test_hokkaido_early_checkin_never_drops_winter_buffer_for_a_fake_solution(
        self,
    ) -> None:
        market = activity(
            "morning-market",
            day_id="day-1",
            order=0,
            duration_min=60,
            windows=(TimeWindow(time(8, 30), time(10)),),
        )
        checkin = activity(
            "ryokan-checkin",
            day_id="day-1",
            order=1,
            location_id="city-b-ryokan",
            duration_min=30,
            decision=DecisionState.BOOKED,
            flexibility=Flexibility.FIXED_TIME,
            scheduled_start=time(13),
        )
        trip = state(
            days=(
                replace(
                    day(
                        "morning-market",
                        "ryokan-checkin",
                        start=time(8),
                        end=time(20),
                        base="hotel-a",
                    ),
                    timezone="Asia/Tokyo",
                    end_location_id="city-b-ryokan",
                ),
            ),
            activities=(market, checkin),
            travel=(
                edge("hotel-a", "loc-morning-market", 15, buffer=20),
                edge(
                    "loc-morning-market",
                    "city-b-ryokan",
                    180,
                    buffer=45,
                ),
                edge("hotel-a", "city-b-ryokan", 240, buffer=45),
                edge(
                    "city-b-ryokan",
                    "loc-morning-market",
                    180,
                    buffer=45,
                ),
            ),
            constraints=(
                constraint(
                    "market-required",
                    ConstraintKind.MUST_INCLUDE,
                    "morning-market",
                ),
            ),
        )
        trip = replace(trip, timezone="Asia/Tokyo")
        schedule_problem = problem(
            trip,
            eligible=("morning-market",),
        )

        result = solve_schedule(schedule_problem)

        self.assertNotEqual(ScheduleStatus.SOLVED, result.status)
        self.assertIsNone(result.candidate)
        self.assertTrue(
            any(
                issue.code == "MISSING_REQUIRED_ACTIVITY"
                for issue in result.failure.kernel_issues
            )
        )
        self.assertGreater(result.evaluations_used, 1)


class Phase3AdversarialRegressionTests(unittest.TestCase):
    def test_patch_operation_limit_is_a_typed_stageability_failure(
        self,
    ) -> None:
        activity_ids = tuple(f"accepted-{index:03d}" for index in range(129))
        accepted = tuple(
            activity(
                activity_id,
                order=index,
                location_id="hotel",
                duration_min=1,
                decision=DecisionState.SELECTED,
            )
            for index, activity_id in enumerate(activity_ids)
        )
        trip = state(
            days=(day(*activity_ids),),
            activities=accepted,
            travel=(),
        )
        schedule_problem = problem(
            trip,
            max_changes=129,
            evaluations=1,
        )
        assignments = tuple(
            ScheduleAssignment(
                activity_id,
                "day-1",
                index,
                None,
            )
            for index, activity_id in enumerate(activity_ids)
        )

        with self.assertRaises(ScheduleContractError) as raised:
            build_schedule_candidate(
                schedule_problem,
                assignments,
                solver="stageability-regression",
            )
        self.assertEqual(
            "PATCH_OPERATION_LIMIT_EXCEEDED",
            raised.exception.code,
        )

        result = solve_schedule(schedule_problem)
        self.assertEqual(ScheduleStatus.SEARCH_EXHAUSTED, result.status)
        self.assertEqual(
            "NO_SOLUTION_WITHIN_EVALUATION_LIMIT",
            result.failure.code,
        )

    def test_common_builder_scores_normalized_assignment_fixed_point(
        self,
    ) -> None:
        first = activity(
            "a",
            order=0,
            location_id="hotel",
            duration_min=10,
            decision=DecisionState.SELECTED,
        )
        placeholder = activity(
            "x",
            order=1,
            location_id="hotel",
            duration_min=10,
        )
        booking = activity(
            "booked",
            order=2,
            location_id="hotel",
            duration_min=10,
            decision=DecisionState.BOOKED,
        )
        last = activity(
            "b",
            order=3,
            location_id="hotel",
            duration_min=10,
            decision=DecisionState.SELECTED,
        )
        trip = state(
            days=(day("a", "x", "booked", "b"),),
            activities=(first, placeholder, booking, last),
            travel=(),
        )
        schedule_problem = problem(trip, eligible=("x",))

        candidate = build_schedule_candidate(
            schedule_problem,
            (
                ScheduleAssignment("a", "day-1", 0, None),
                ScheduleAssignment("booked", "day-1", 2, None),
                ScheduleAssignment("b", "day-1", 3, None),
            ),
            solver="adversarial-test",
        )

        self.assertEqual(
            2, candidate.score.accepted_activity_change_count
        )
        self.assertEqual(("a", "b"), candidate.score.changed_activity_ids)
        replayed = replay_schedule_candidate(
            schedule_problem, candidate
        )
        assignments = {
            item.activity_id: item for item in candidate.assignments
        }
        self.assertEqual(
            time(9), assignments["a"].scheduled_start
        )
        self.assertEqual(
            time(9, 20), assignments["b"].scheduled_start
        )
        self.assertEqual(
            ("a", "x", "booked", "b"),
            replayed.days[0].activity_ids,
        )
        self.assertEqual(
            time(9), replayed.activity_by_id["a"].scheduled_start
        )
        self.assertEqual(
            time(9, 20), replayed.activity_by_id["b"].scheduled_start
        )
        candidate_to_plan_patch(schedule_problem, candidate)

    def test_common_builder_rejects_unstageable_stationary_anchor_reorder(
        self,
    ) -> None:
        accepted = activity(
            "a",
            order=0,
            location_id="hotel",
            decision=DecisionState.SELECTED,
        )
        booking = activity(
            "booked",
            order=1,
            location_id="hotel",
            decision=DecisionState.BOOKED,
        )
        placeholder = activity(
            "placeholder",
            order=2,
            location_id="hotel",
        )
        trip = state(
            days=(day("a", "booked", "placeholder"),),
            activities=(accepted, booking, placeholder),
            travel=(),
        )
        schedule_problem = problem(
            trip, eligible=("placeholder",)
        )

        with self.assertRaisesRegex(
            ScheduleContractError, "stationary activities"
        ):
            build_schedule_candidate(
                schedule_problem,
                (
                    ScheduleAssignment("booked", "day-1", 1, None),
                    ScheduleAssignment("a", "day-1", 2, None),
                ),
                solver="adversarial-test",
            )

    def test_compound_promotion_and_relocation_cross_capacity_valley(
        self,
    ) -> None:
        accepted = activity(
            "accepted",
            order=0,
            decision=DecisionState.SELECTED,
        )
        required = activity("required", order=1)
        trip = state(
            days=(
                day("accepted", "required", day_id="day-1"),
                day(
                    day_id="day-2",
                    trip_date=date(2026, 10, 2),
                ),
            ),
            activities=(accepted, required),
            travel=complete_edges(
                ("hotel", "loc-accepted", "loc-required")
            ),
            constraints=(
                constraint(
                    "required-coverage",
                    ConstraintKind.MUST_INCLUDE,
                    "required",
                ),
                constraint(
                    "required-day",
                    ConstraintKind.ALLOWED_DAY,
                    "required",
                    params=(("day_id", "day-1"),),
                ),
                constraint(
                    "day-one-capacity",
                    ConstraintKind.DAILY_LIMIT,
                    "day-1",
                    params=(("max_activities", 1),),
                ),
            ),
        )

        result = solve_schedule(problem(trip, eligible=("required",)))

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        assignments = {
            item.activity_id: item for item in result.candidate.assignments
        }
        self.assertEqual("day-2", assignments["accepted"].day_id)
        self.assertEqual("day-1", assignments["required"].day_id)

    def test_optional_requires_pair_is_promoted_as_one_semantic_bundle(
        self,
    ) -> None:
        trigger = activity(
            "trigger",
            order=0,
            location_id="hotel",
            priority=100,
        )
        dependency = activity(
            "dependency",
            order=1,
            location_id="hotel",
            priority=-10,
        )
        trip = state(
            days=(day("trigger", "dependency"),),
            activities=(trigger, dependency),
            travel=(),
            constraints=(
                constraint(
                    "trigger-requires-dependency",
                    ConstraintKind.REQUIRES,
                    "trigger",
                    "dependency",
                ),
            ),
        )

        result = solve_schedule(
            problem(trip, eligible=("trigger", "dependency"))
        )

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual(
            ("dependency", "trigger"),
            result.candidate.promoted_activity_ids,
        )
        self.assertEqual(90, result.candidate.score.served_priority_points)

    def test_requires_closure_promotes_all_dependencies_together(self) -> None:
        trigger = activity("trigger", order=0, location_id="hotel")
        first = activity("dependency-a", order=1, location_id="hotel")
        second = activity("dependency-b", order=2, location_id="hotel")
        trip = state(
            days=(day("trigger", "dependency-a", "dependency-b"),),
            activities=(trigger, first, second),
            travel=(),
            constraints=(
                constraint(
                    "trigger-required",
                    ConstraintKind.MUST_INCLUDE,
                    "trigger",
                ),
                constraint(
                    "trigger-closure",
                    ConstraintKind.REQUIRES,
                    "trigger",
                    "dependency-a",
                    "dependency-b",
                ),
            ),
        )

        result = solve_schedule(
            problem(
                trip,
                eligible=("trigger", "dependency-a", "dependency-b"),
            )
        )

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual(
            ("dependency-a", "dependency-b", "trigger"),
            result.candidate.promoted_activity_ids,
        )

    def test_selective_time_clear_preserves_unrelated_accepted_time(
        self,
    ) -> None:
        before = activity(
            "before",
            order=0,
            decision=DecisionState.SELECTED,
            scheduled_start=time(14),
        )
        booking = activity(
            "booking",
            order=1,
            decision=DecisionState.BOOKED,
            flexibility=Flexibility.FIXED_TIME,
            scheduled_start=time(12),
        )
        later = activity(
            "later",
            order=2,
            decision=DecisionState.SELECTED,
            scheduled_start=time(17),
        )
        trip = state(
            days=(day("before", "booking", "later", end=time(20)),),
            activities=(before, booking, later),
            travel=complete_edges(
                ("hotel", "loc-before", "loc-booking", "loc-later")
            ),
            constraints=(
                constraint(
                    "before-booking",
                    ConstraintKind.BEFORE,
                    "before",
                    "booking",
                ),
            ),
        )

        result = solve_schedule(problem(trip, max_changes=1))

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        assignments = {
            item.activity_id: item for item in result.candidate.assignments
        }
        self.assertLess(
            assignments["before"].scheduled_start,
            assignments["booking"].scheduled_start,
        )
        self.assertEqual(time(17), assignments["later"].scheduled_start)
        self.assertEqual(
            1, result.candidate.score.accepted_activity_change_count
        )

    def test_fixed_day_candidate_promotion_does_not_change_time_or_position(
        self,
    ) -> None:
        fixed_day = activity(
            "fixed-day",
            decision=DecisionState.CANDIDATE,
            flexibility=Flexibility.FIXED_DAY,
            location_id="hotel",
        )
        trip = state(
            days=(day("fixed-day"),),
            activities=(fixed_day,),
            travel=(),
            constraints=(
                constraint(
                    "fixed-day-required",
                    ConstraintKind.MUST_INCLUDE,
                    "fixed-day",
                ),
            ),
        )
        schedule_problem = problem(trip, eligible=("fixed-day",))

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        assignment = result.candidate.assignments[0]
        self.assertIsNone(assignment.scheduled_start)
        self.assertEqual(0, assignment.order)
        self.assertEqual(0, result.candidate.score.protected_change_count)
        patch_value = candidate_to_plan_patch(
            schedule_problem, result.candidate
        )
        self.assertEqual(1, len(patch_value.operations))
        self.assertIsInstance(patch_value.operations[0], UpdateActivity)

    def test_computed_start_is_counted_as_a_persisted_accepted_change(
        self,
    ) -> None:
        accepted = activity(
            "accepted",
            decision=DecisionState.SELECTED,
            location_id="hotel",
        )
        trip = state(
            days=(day("accepted"),),
            activities=(accepted,),
            travel=(),
        )

        result = solve_schedule(problem(trip, max_changes=0))

        self.assertEqual(ScheduleStatus.SEARCH_EXHAUSTED, result.status)
        self.assertEqual("CHANGE_BUDGET_EXCEEDED", result.failure.code)

    def test_raw_move_around_inactive_anchor_consumes_change_budget(
        self,
    ) -> None:
        inactive = activity(
            "inactive",
            order=0,
            location_id="hotel",
            priority=-1,
        )
        accepted = activity(
            "accepted",
            order=1,
            location_id="hotel",
            decision=DecisionState.SELECTED,
            scheduled_start=time(9),
        )
        trip = state(
            days=(day("inactive", "accepted"),),
            activities=(inactive, accepted),
            travel=(),
        )

        result = solve_schedule(
            problem(trip, eligible=("inactive",), max_changes=0)
        )

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual((), result.candidate.promoted_activity_ids)
        self.assertEqual(1, result.candidate.assignments[0].order)
        self.assertEqual(
            0, result.candidate.score.accepted_activity_change_count
        )

    def test_zero_priority_unconstrained_candidate_is_not_promoted(self) -> None:
        optional = activity(
            "a-optional",
            order=0,
            location_id="hotel",
            priority=0,
        )
        accepted = activity(
            "z-accepted",
            order=1,
            location_id="hotel",
            decision=DecisionState.SELECTED,
            scheduled_start=time(9),
        )
        trip = state(
            days=(day("a-optional", "z-accepted"),),
            activities=(optional, accepted),
            travel=(),
        )

        result = solve_schedule(
            problem(trip, eligible=("a-optional",))
        )

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual((), result.candidate.promoted_activity_ids)

    def test_one_evaluation_means_one_complete_assignment_variant(
        self,
    ) -> None:
        accepted = activity(
            "accepted",
            decision=DecisionState.SELECTED,
            location_id="hotel",
            scheduled_start=time(13),
        )
        trip = state(
            days=(day("accepted"),),
            activities=(accepted,),
            travel=(),
        )
        schedule_problem = problem(trip, evaluations=1)

        with patch(
            "trip_planner.scheduler.evaluate_schedule_state",
            wraps=evaluate_schedule_state,
        ) as evaluate:
            result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.SOLVED, result.status)
        self.assertEqual(1, result.evaluations_used)
        self.assertEqual(2, evaluate.call_count)

    def test_evaluation_limit_never_masquerades_as_scope_proof(
        self,
    ) -> None:
        accepted = activity(
            "accepted",
            order=0,
            location_id="hotel",
            decision=DecisionState.SELECTED,
            scheduled_start=time(14),
        )
        booking = activity(
            "booking",
            order=1,
            location_id="hotel",
            decision=DecisionState.BOOKED,
            flexibility=Flexibility.FIXED_TIME,
            scheduled_start=time(12),
        )
        trip = state(
            days=(day("accepted", "booking"),),
            activities=(accepted, booking),
            travel=(),
            constraints=(
                constraint(
                    "accepted-before-booking",
                    ConstraintKind.BEFORE,
                    "accepted",
                    "booking",
                ),
            ),
        )

        limited = solve_schedule(
            problem(trip, max_changes=1, evaluations=1)
        )
        enough = solve_schedule(
            problem(trip, max_changes=1, evaluations=2)
        )

        self.assertEqual(ScheduleStatus.SEARCH_EXHAUSTED, limited.status)
        self.assertEqual(
            "NO_SOLUTION_WITHIN_EVALUATION_LIMIT",
            limited.failure.code,
        )
        self.assertEqual(1, limited.evaluations_used)
        self.assertEqual(ScheduleStatus.SOLVED, enough.status)
        self.assertEqual(
            time(9), enough.candidate.assignments[0].scheduled_start
        )

    def test_evaluation_limit_precedes_tentative_evidence_diagnosis(
        self,
    ) -> None:
        first = activity(
            "a",
            order=0,
            decision=DecisionState.SELECTED,
        )
        second = activity(
            "b",
            order=1,
            decision=DecisionState.SELECTED,
        )
        trip = state(
            days=(day("a", "b"),),
            activities=(first, second),
            travel=(
                edge("hotel", "loc-a", 10),
                edge(
                    "loc-a",
                    "loc-b",
                    10,
                    evidence=EvidenceState.UNVERIFIED,
                ),
                edge("loc-b", "hotel", 10),
                edge("hotel", "loc-b", 10),
                edge("loc-b", "loc-a", 10),
                edge("loc-a", "hotel", 10),
            ),
        )

        limited = solve_schedule(problem(trip, evaluations=1))
        enough = solve_schedule(problem(trip, evaluations=2))

        self.assertEqual(ScheduleStatus.SEARCH_EXHAUSTED, limited.status)
        self.assertEqual(
            "NO_SOLUTION_WITHIN_EVALUATION_LIMIT",
            limited.failure.code,
        )
        self.assertEqual(ScheduleStatus.SOLVED, enough.status)
        self.assertEqual(
            ("b", "a"),
            tuple(
                assignment.activity_id
                for assignment in enough.candidate.assignments
            ),
        )

    def test_invalid_constraint_reference_is_invalid_input_not_infeasible(
        self,
    ) -> None:
        trip = state(
            days=(day(),),
            activities=(),
            travel=(),
            constraints=(
                constraint(
                    "ghost-required",
                    ConstraintKind.MUST_INCLUDE,
                    "ghost",
                ),
            ),
        )

        result = solve_schedule(problem(trip))

        self.assertEqual(ScheduleStatus.INVALID_INPUT, result.status)
        self.assertEqual("INVALID_CONSTRAINT", result.failure.code)
        self.assertEqual(("ghost-required",), result.failure.constraint_ids)
        self.assertEqual(0, result.evaluations_used)

    def test_activity_order_field_must_match_day_membership_index(
        self,
    ) -> None:
        booking = activity(
            "booking",
            order=5,
            location_id="hotel",
            decision=DecisionState.BOOKED,
            flexibility=Flexibility.FIXED_TIME,
            scheduled_start=time(12),
        )
        trip = state(
            days=(day("booking"),),
            activities=(booking,),
            travel=(),
        )

        result = solve_schedule(problem(trip))

        self.assertEqual(ScheduleStatus.INVALID_INPUT, result.status)
        self.assertEqual(
            "INCONSISTENT_ACTIVITY_PLACEMENT", result.failure.code
        )
        self.assertEqual(("booking",), result.failure.activity_ids)

    def test_invalid_failure_selection_is_constraint_permutation_invariant(
        self,
    ) -> None:
        constraints = (
            constraint(
                "z-invalid-before",
                ConstraintKind.BEFORE,
                "ghost-left",
                "ghost-right",
            ),
            constraint(
                "a-invalid-required",
                ConstraintKind.MUST_INCLUDE,
                "missing",
            ),
        )
        first_state = state(
            days=(day(),),
            activities=(),
            travel=(),
            constraints=constraints,
        )
        second_state = replace(
            first_state, constraints=tuple(reversed(constraints))
        )
        first_problem = problem(first_state)
        second_problem = problem(second_state)

        first = solve_schedule(first_problem)
        second = solve_schedule(second_problem)

        self.assertEqual(first_problem.problem_id, second_problem.problem_id)
        self.assertEqual(first, second)
        self.assertEqual(
            ("a-invalid-required",), first.failure.constraint_ids
        )

    def test_unverified_draft_policy_is_explicitly_reserved(self) -> None:
        accepted = activity(
            "accepted",
            decision=DecisionState.SELECTED,
            location_id="hotel",
        )
        trip = state(
            days=(day("accepted"),),
            activities=(accepted,),
            travel=(),
        )
        schedule_problem = ScheduleProblem(
            state=trip,
            evaluation_at=EVALUATION_AT,
            scope=default_replan_scope(trip),
            trip_id="canonical-phase3-fixture",
            preferences=SchedulePreferences(
                evidence_policy=EvidencePolicy.ALLOW_UNVERIFIED_DRAFT
            ),
        )

        result = solve_schedule(schedule_problem)

        self.assertEqual(ScheduleStatus.INVALID_INPUT, result.status)
        self.assertEqual(
            "UNSUPPORTED_EVIDENCE_POLICY", result.failure.code
        )

    def test_v1_rejects_unimplemented_multiple_candidate_output(self) -> None:
        with self.assertRaisesRegex(
            ScheduleContractError, "between 1 and 1"
        ):
            SearchLimits(max_candidates=2)

    def test_overnight_time_shift_uses_planning_day_not_clock_distance(
        self,
    ) -> None:
        accepted = activity(
            "accepted",
            decision=DecisionState.SELECTED,
            location_id="hotel",
            duration_min=10,
            scheduled_start=time(23, 50),
        )
        trip = state(
            days=(
                day(
                    "accepted",
                    start=time(22),
                    end=time(2),
                ),
            ),
            activities=(accepted,),
            travel=(),
        )
        schedule_problem = problem(trip)
        shortly_after_midnight = build_schedule_candidate(
            schedule_problem,
            (
                ScheduleAssignment(
                    "accepted", "day-1", 0, time(0, 10)
                ),
            ),
            solver="adversarial-test",
        )
        earlier_same_evening = build_schedule_candidate(
            schedule_problem,
            (
                ScheduleAssignment(
                    "accepted", "day-1", 0, time(23)
                ),
            ),
            solver="adversarial-test",
        )

        self.assertEqual(
            200,
            shortly_after_midnight.score.accepted_time_shift_deci_min,
        )
        self.assertEqual(
            500,
            earlier_same_evening.score.accepted_time_shift_deci_min,
        )
        self.assertLess(
            shortly_after_midnight.score.objective_key(),
            earlier_same_evening.score.objective_key(),
        )

    def test_cross_day_time_shift_compares_resolved_instants(self) -> None:
        accepted = activity(
            "accepted",
            day_id="day-1",
            decision=DecisionState.SELECTED,
            location_id="hotel",
            duration_min=5,
            scheduled_start=time(0, 10),
        )
        trip = state(
            days=(
                day(
                    "accepted",
                    day_id="day-1",
                    trip_date=date(2026, 10, 1),
                    start=time(22),
                    end=time(2),
                ),
                day(
                    day_id="day-2",
                    trip_date=date(2026, 10, 2),
                    start=time(0),
                    end=time(23, 59),
                ),
            ),
            activities=(accepted,),
            travel=(),
        )
        schedule_problem = problem(trip)
        same_instant = build_schedule_candidate(
            schedule_problem,
            (
                ScheduleAssignment(
                    "accepted", "day-2", 0, time(0, 10)
                ),
            ),
            solver="adversarial-test",
        )
        much_later = build_schedule_candidate(
            schedule_problem,
            (
                ScheduleAssignment(
                    "accepted", "day-2", 0, time(23, 50)
                ),
            ),
            solver="adversarial-test",
        )

        self.assertEqual(
            0, same_instant.score.accepted_time_shift_deci_min
        )
        self.assertEqual(
            14200, much_later.score.accepted_time_shift_deci_min
        )
        self.assertLess(
            same_instant.score.objective_key(),
            much_later.score.objective_key(),
        )

    def test_verified_travel_beats_shorter_unverified_recommendation(
        self,
    ) -> None:
        visit = activity(
            "visit",
            decision=DecisionState.SELECTED,
        )
        unverified_recommended = replace(
            edge(
                "hotel",
                "loc-visit",
                5,
                evidence=EvidenceState.UNVERIFIED,
            ),
            recommended=True,
        )
        verified = edge("hotel", "loc-visit", 10)
        trip = state(
            days=(day("visit"),),
            activities=(visit,),
            travel=(
                unverified_recommended,
                verified,
                edge("loc-visit", "hotel", 10),
            ),
        )

        report = evaluate_timeline(trip, now=EVALUATION_AT)

        self.assertEqual(CheckStatus.FEASIBLE, report.status)
        self.assertEqual(20.0, dict(report.metrics)["travel_min"])
        self.assertFalse(
            any(
                issue.code == "UNVERIFIED_EVIDENCE"
                for issue in report.issues
            )
        )

    def test_verified_generic_travel_beats_unverified_specific_arc(
        self,
    ) -> None:
        visit = activity(
            "visit",
            decision=DecisionState.SELECTED,
        )
        unverified_specific = replace(
            edge(
                "hotel",
                "loc-visit",
                10,
                evidence=EvidenceState.UNVERIFIED,
            ),
            to_activity_id="visit",
        )
        trip = state(
            days=(day("visit"),),
            activities=(visit,),
            travel=(
                unverified_specific,
                edge("hotel", "loc-visit", 12),
                edge("loc-visit", "hotel", 10),
            ),
        )

        report = evaluate_timeline(trip, now=EVALUATION_AT)

        self.assertEqual(CheckStatus.FEASIBLE, report.status)
        self.assertEqual(22.0, dict(report.metrics)["travel_min"])
        self.assertFalse(
            any(
                issue.code == "UNVERIFIED_EVIDENCE"
                for issue in report.issues
            )
        )

    def test_unexpected_scheduler_exception_becomes_typed_engine_error(
        self,
    ) -> None:
        accepted = activity(
            "accepted",
            decision=DecisionState.SELECTED,
            location_id="hotel",
        )
        trip = state(
            days=(day("accepted"),),
            activities=(accepted,),
            travel=(),
        )

        with patch(
            "trip_planner.scheduler._initial_layout",
            side_effect=RuntimeError("fixture boom"),
        ):
            result = solve_schedule(problem(trip))

        self.assertEqual(ScheduleStatus.ENGINE_ERROR, result.status)
        self.assertEqual("UNEXPECTED_ENGINE_ERROR", result.failure.code)
        self.assertIsNone(result.candidate)


if __name__ == "__main__":
    unittest.main()
