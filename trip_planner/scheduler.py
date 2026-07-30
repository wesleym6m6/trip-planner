"""Bounded deterministic best-first schedule search.

The search intentionally shares all materialization, feasibility, and score
semantics with other future solver experiments.  It performs no I/O, can cross
non-improving intermediate layouts, and makes no optimality claim.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from itertools import combinations
from typing import Iterable, Iterator, Mapping

from .models import (
    CheckIssue,
    CheckReport,
    CheckStatus,
    ConstraintKind,
    ConstraintStrength,
    DecisionState,
    Flexibility,
    IssueSeverity,
    TripState,
)
from .scheduling import (
    ScheduleAssignment,
    ScheduleContractError,
    ScheduleFailure,
    ScheduleProblem,
    ScheduleResult,
    ScheduleScore,
    ScheduleStatus,
    assignments_from_state,
    build_schedule_candidate,
    evaluate_schedule_state,
    materialize_schedule,
    _project_schedule_operations,
    schedule_key,
    score_schedule,
    validate_schedule_problem,
)


SOLVER_VERSION = "bounded-deterministic-best-first/v2"
_MAX_TIME_CLEAR_VARIANTS_PER_LAYOUT = 64
_TIME_REPAIRABLE_ERROR_CODES = frozenset(
    {
        "AMBIGUOUS_LOCAL_TIME",
        "DAY_WINDOW_VIOLATION",
        "FIXED_TIME_CONFLICT",
        "GLOBAL_TIMELINE_OVERLAP",
        "INBOUND_TIMELINE_OVERLAP",
        "NONEXISTENT_LOCAL_TIME",
        "PRECEDENCE_VIOLATION",
        "RETURN_AFTER_DAY_END",
        "RETURN_TIMELINE_OVERLAP",
        "SCHEDULED_START_CONFLICT",
        "TIME_WINDOW_VIOLATION",
    }
)
@dataclass(frozen=True, slots=True)
class _Layout:
    day_orders: tuple[tuple[str, tuple[str, ...]], ...]
    promoted_activity_ids: tuple[str, ...] = ()

    def as_mapping(self) -> dict[str, tuple[str, ...]]:
        return dict(self.day_orders)

    def key(self) -> tuple[object, ...]:
        return (self.day_orders, self.promoted_activity_ids)


@dataclass(frozen=True, slots=True)
class _Evaluated:
    layout: _Layout
    assignments: tuple[ScheduleAssignment, ...]
    state: TripState
    report: CheckReport
    score: ScheduleScore
    schedule_key: str

    def key(self) -> tuple[object, ...]:
        return (*self.score.objective_key(), self.schedule_key)


class _EvaluationBudget:
    def __init__(self, problem: ScheduleProblem) -> None:
        self.problem = problem
        self.limit = problem.limits.max_evaluations
        self.used = 0
        self.cache: dict[tuple[object, ...], _Evaluated | ScheduleContractError] = {}
        self.stageability_error: ScheduleContractError | None = None

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def evaluate(
        self,
        layout: _Layout,
        *,
        strict: bool = False,
    ) -> _Evaluated | None:
        key = layout.key()
        cached = self.cache.get(key)
        if isinstance(cached, _Evaluated):
            return cached
        if isinstance(cached, ScheduleContractError):
            if (
                strict
                and cached.code != "PATCH_OPERATION_LIMIT_EXCEEDED"
            ):
                raise cached
            return None
        if self.exhausted:
            return None
        evaluated, first_error = self._evaluate_layout(
            layout,
            strict=strict,
        )
        if evaluated is None:
            self.cache[key] = first_error or ScheduleContractError(
                "EVALUATION_FAILED",
                "No distinct complete assignment variant could be evaluated.",
            )
            return None
        self.cache[key] = evaluated
        return evaluated

    def _evaluate_layout(
        self,
        layout: _Layout,
        *,
        strict: bool,
    ) -> tuple[_Evaluated | None, ScheduleContractError | None]:
        """Evaluate distinct complete assignment variants within the bound.

        One unit is one complete assignment variant passed through trusted
        materialization, provisional kernel simulation, canonical assignment
        normalization, and final kernel replay.  Cache hits consume no units.
        """

        attempts: list[_Evaluated] = []
        first_error: ScheduleContractError | None = None
        seen_assignments: set[tuple[ScheduleAssignment, ...]] = set()

        def attempt(clear_time_ids: frozenset[str]) -> _Evaluated | None:
            nonlocal first_error
            assignments = _layout_assignments(
                self.problem,
                layout,
                clear_time_ids=clear_time_ids,
            )
            if assignments in seen_assignments or self.exhausted:
                return None
            seen_assignments.add(assignments)
            self.used += 1
            try:
                evaluated = _evaluate_assignment_variant(
                    self.problem, layout, assignments
                )
            except ScheduleContractError as exc:
                if exc.code == "PATCH_OPERATION_LIMIT_EXCEEDED":
                    if self.stageability_error is None:
                        self.stageability_error = exc
                else:
                    raise
                if first_error is None:
                    first_error = exc
                return None
            attempts.append(evaluated)
            return evaluated

        preserved = attempt(frozenset())
        if preserved is not None and _is_ready(preserved):
            return preserved, first_error

        clearable = _clearable_time_ids(self.problem, layout)
        blocking = _blocking_time_ids(
            layout, preserved, clearable
        )
        if not blocking:
            if not attempts:
                return None, first_error
            return min(attempts, key=_Evaluated.key), first_error
        positions = {
            activity_id: (day_index, activity_index)
            for day_index, (_, activity_ids) in enumerate(layout.day_orders)
            for activity_index, activity_id in enumerate(activity_ids)
        }
        ordered_blocking = tuple(
            sorted(blocking, key=positions.__getitem__)
        )
        ordered_clearable = tuple(
            sorted(clearable, key=positions.__getitem__)
        )
        clear_sets: list[frozenset[str]] = []
        seen_clear_sets: set[frozenset[str]] = set()
        clear_set_limit = min(
            _MAX_TIME_CLEAR_VARIANTS_PER_LAYOUT,
            self.limit - self.used,
        )

        def add_clear_set(activity_ids: Iterable[str]) -> None:
            if len(clear_sets) >= clear_set_limit:
                return
            clear_set = frozenset(activity_ids)
            if not clear_set or clear_set in seen_clear_sets:
                return
            seen_clear_sets.add(clear_set)
            clear_sets.append(clear_set)

        for activity_id in blocking:
            add_clear_set((activity_id,))
        add_clear_set(ordered_blocking)
        add_clear_set(ordered_clearable)
        for size in range(2, len(ordered_clearable)):
            for clear_ids in combinations(ordered_clearable, size):
                add_clear_set(clear_ids)
                if len(clear_sets) >= clear_set_limit:
                    break
            if len(clear_sets) >= clear_set_limit:
                break

        for clear_ids in clear_sets:
            attempt(clear_ids)
            if self.exhausted:
                break
        if not attempts:
            return None, first_error
        return min(attempts, key=_Evaluated.key), first_error


def solve_schedule(problem: ScheduleProblem) -> ScheduleResult:
    """Return a complete kernel-feasible schedule or one typed non-solution."""

    budget = _EvaluationBudget(problem)
    try:
        return _solve_schedule(problem, budget)
    except ScheduleContractError as exc:
        return _failure_result(
            problem,
            ScheduleStatus.ENGINE_ERROR,
            exc.code,
            exc.message,
            budget=budget,
        )
    except Exception as exc:
        return _failure_result(
            problem,
            ScheduleStatus.ENGINE_ERROR,
            "UNEXPECTED_ENGINE_ERROR",
            (
                "The scheduling engine failed unexpectedly "
                f"({type(exc).__name__}); no candidate is returned."
            ),
            budget=budget,
        )


def _solve_schedule(
    problem: ScheduleProblem, budget: _EvaluationBudget
) -> ScheduleResult:
    """Internal implementation with one observable evaluation budget."""

    failures = validate_schedule_problem(problem)
    if failures:
        failure = failures[0]
        return ScheduleResult(
            status=ScheduleStatus.INVALID_INPUT,
            problem_id=problem.problem_id,
            failure=failure,
            evaluations_used=0,
            evaluation_limit=problem.limits.max_evaluations,
        )

    initial = budget.evaluate(_initial_layout(problem), strict=True)
    if initial is None:
        if budget.exhausted:
            return _evaluation_limit_result(problem, budget)
        if budget.stageability_error is not None:
            return _failure_result(
                problem,
                ScheduleStatus.SEARCH_EXHAUSTED,
                budget.stageability_error.code,
                budget.stageability_error.message,
                budget=budget,
            )
        return _failure_result(
            problem,
            ScheduleStatus.ENGINE_ERROR,
            "INITIAL_MATERIALIZATION_FAILED",
            "The trusted initial schedule could not be materialized.",
            budget=budget,
        )

    current = _best_first_search(problem, budget, initial)

    ready = (
        current.report.status is CheckStatus.FEASIBLE
        and current.score.hard_violation_count == 0
        and current.score.missing_required_count == 0
    )
    within_authority = (
        current.score.protected_change_count == 0
        and current.score.accepted_activity_change_count
        <= problem.scope.max_accepted_changes
    )
    if ready and within_authority:
        try:
            candidate = build_schedule_candidate(
                problem,
                current.assignments,
                promoted_activity_ids=current.layout.promoted_activity_ids,
                solver=SOLVER_VERSION,
            )
        except ScheduleContractError as exc:
            if exc.code == "PATCH_OPERATION_LIMIT_EXCEEDED":
                if budget.exhausted:
                    return _evaluation_limit_result(
                        problem,
                        budget,
                        evaluated=current,
                    )
                return _failure_result(
                    problem,
                    ScheduleStatus.SEARCH_EXHAUSTED,
                    exc.code,
                    exc.message,
                    budget=budget,
                    evaluated=current,
                    activity_ids=current.score.changed_activity_ids,
                )
            return _failure_result(
                problem,
                ScheduleStatus.ENGINE_ERROR,
                exc.code,
                exc.message,
                budget=budget,
                evaluated=current,
            )
        return ScheduleResult(
            status=ScheduleStatus.SOLVED,
            problem_id=problem.problem_id,
            candidates=(candidate,),
            evaluations_used=budget.used,
            evaluation_limit=budget.limit,
            optimality="not_claimed",
        )

    if budget.exhausted:
        return _evaluation_limit_result(
            problem,
            budget,
            evaluated=current,
        )

    if ready and current.score.protected_change_count:
        return _failure_result(
            problem,
            ScheduleStatus.SEARCH_EXHAUSTED,
            "PROTECTED_CHANGE_REQUIRED",
            (
                "The feasible layout requires a protected activity "
                "change outside automatic scheduler authority."
            ),
            budget=budget,
            evaluated=current,
            activity_ids=current.score.protected_activity_ids,
        )
    if (
        ready
        and current.score.accepted_activity_change_count
        > problem.scope.max_accepted_changes
    ):
        return _failure_result(
            problem,
            ScheduleStatus.SEARCH_EXHAUSTED,
            "CHANGE_BUDGET_EXCEEDED",
            (
                "The best feasible layout found exceeds the accepted "
                "activity change budget."
            ),
            budget=budget,
            evaluated=current,
            activity_ids=current.score.changed_activity_ids,
        )

    if budget.stageability_error is not None:
        return _failure_result(
            problem,
            ScheduleStatus.SEARCH_EXHAUSTED,
            budget.stageability_error.code,
            budget.stageability_error.message,
            budget=budget,
            evaluated=current,
        )

    if (
        current.report.status is CheckStatus.NEEDS_VERIFICATION
        and not current.report.errors
    ):
        return _failure_result(
            problem,
            ScheduleStatus.NEEDS_EVIDENCE,
            "SCHEDULE_NEEDS_EVIDENCE",
            "The best complete layout still depends on missing or untrusted evidence.",
            budget=budget,
            evaluated=current,
        )

    movable_active = {
        activity_id
        for activity_id in problem.scope.mutable_activity_ids
        if problem.state.activity_by_id[activity_id].decision_state
        in {
            DecisionState.SELECTED,
            DecisionState.CANDIDATE,
        }
    }
    if not movable_active and not problem.scope.eligible_candidate_ids:
        status = ScheduleStatus.PROVEN_INFEASIBLE
        code = "FIXED_BASELINE_INFEASIBLE"
        message = "The only permitted frozen layout is kernel-infeasible."
    else:
        status = ScheduleStatus.SEARCH_EXHAUSTED
        code = "NO_SOLUTION_WITHIN_BUDGET"
        message = (
            "The bounded deterministic search found no complete feasible "
            "schedule; infeasibility is not claimed."
        )
    return _failure_result(
        problem,
        status,
        code,
        message,
        budget=budget,
        evaluated=current,
    )


def _best_first_search(
    problem: ScheduleProblem,
    budget: _EvaluationBudget,
    initial: _Evaluated,
) -> _Evaluated:
    """Return the best evaluated layout from one bounded graph traversal.

    Every distinct layout is evaluated at most once.  Unlike the original
    greedy hill climb, the frontier retains non-improving layouts so the search
    can cross a score valley that requires multiple relocations or an initially
    weaker candidate choice.  The trusted evaluation budget remains the sole
    work bound; exhausting it never becomes an optimality or infeasibility
    claim.
    """

    best = initial
    frontier: list[
        tuple[
            tuple[object, ...],
            tuple[object, ...],
            _Evaluated,
        ]
    ] = [(initial.key(), initial.layout.key(), initial)]
    discovered = {initial.layout.key()}

    while frontier and not budget.exhausted:
        _, _, expanded = heapq.heappop(frontier)
        for layout in _neighbor_layouts(problem, expanded.layout):
            layout_key = layout.key()
            if layout_key in discovered:
                continue
            discovered.add(layout_key)
            if not _preserves_protected_layout_positions(problem, layout):
                continue
            evaluated = budget.evaluate(layout)
            if evaluated is not None:
                if evaluated.key() < best.key():
                    best = evaluated
                heapq.heappush(
                    frontier,
                    (evaluated.key(), layout_key, evaluated),
                )
            if budget.exhausted:
                break
    return best


def _preserves_protected_layout_positions(
    problem: ScheduleProblem,
    layout: _Layout,
) -> bool:
    """Reject neighbors that cannot satisfy the V1 raw-position contract."""

    baseline_positions = {
        activity_id: (day.day_id, index)
        for day in problem.state.days
        for index, activity_id in enumerate(day.activity_ids)
    }
    layout_positions = {
        activity_id: (day_id, index)
        for day_id, activity_ids in layout.day_orders
        for index, activity_id in enumerate(activity_ids)
    }
    mutable = frozenset(problem.scope.mutable_activity_ids)
    return all(
        layout_positions.get(activity.activity_id)
        == baseline_positions[activity.activity_id]
        for activity in problem.state.activities
        if (
            activity.activity_id not in mutable
            or activity.flexibility is Flexibility.FIXED_DAY
        )
    )


def _initial_layout(problem: ScheduleProblem) -> _Layout:
    ordered_days = sorted(
        problem.state.days, key=lambda item: (item.date, item.day_id)
    )
    return _Layout(
        day_orders=tuple(
            (day.day_id, tuple(day.activity_ids)) for day in ordered_days
        )
    )


def _layout_assignments(
    problem: ScheduleProblem,
    layout: _Layout,
    *,
    clear_time_ids: frozenset[str],
) -> tuple[ScheduleAssignment, ...]:
    position = {
        activity_id: (day_id, index)
        for day_id, values in layout.day_orders
        for index, activity_id in enumerate(values)
    }
    baseline_active = {
        activity.activity_id
        for activity in problem.state.activities
        if activity.decision_state
        in {
            DecisionState.SELECTED,
            DecisionState.FIXED,
            DecisionState.BOOKED,
        }
    }
    active = baseline_active | set(layout.promoted_activity_ids)
    mutable = frozenset(problem.scope.mutable_activity_ids)
    ordered_active = tuple(
        sorted(
            active,
            key=lambda item: (
                _day_rank(problem)[position[item][0]],
                position[item][1],
                item,
            ),
        )
    )
    assignments: list[ScheduleAssignment] = []
    for activity_id in ordered_active:
        day_id, raw_order = position[activity_id]
        activity = problem.state.activity_by_id[activity_id]
        scheduled_start = activity.scheduled_start
        if activity_id in mutable and activity_id in clear_time_ids:
            scheduled_start = None
        assignments.append(
            ScheduleAssignment(
                activity_id=activity_id,
                day_id=day_id,
                order=raw_order,
                scheduled_start=scheduled_start,
            )
        )
    return tuple(assignments)


def _clearable_time_ids(
    problem: ScheduleProblem, layout: _Layout
) -> tuple[str, ...]:
    promoted = frozenset(layout.promoted_activity_ids)
    return tuple(
        sorted(
            activity_id
            for activity_id in problem.scope.mutable_activity_ids
            if (
                problem.state.activity_by_id[activity_id].decision_state
                is DecisionState.SELECTED
                or activity_id in promoted
            )
            and problem.state.activity_by_id[activity_id].flexibility
            is Flexibility.MOVABLE
            and problem.state.activity_by_id[activity_id].scheduled_start
            is not None
        )
    )


def _blocking_time_ids(
    layout: _Layout,
    evaluated: _Evaluated | None,
    clearable_activity_ids: tuple[str, ...],
) -> tuple[str, ...]:
    if evaluated is None:
        return ()
    clearable = frozenset(clearable_activity_ids)
    positions = {
        activity_id: (day_id, index)
        for day_id, values in layout.day_orders
        for index, activity_id in enumerate(values)
    }
    blocking: set[str] = set()
    for issue in evaluated.report.issues:
        if (
            issue.severity is not IssueSeverity.ERROR
            or issue.code not in _TIME_REPAIRABLE_ERROR_CODES
        ):
            continue
        direct = clearable.intersection(issue.activity_ids)
        blocking.update(direct)
        for activity_id in issue.activity_ids:
            target = positions.get(activity_id)
            if target is None:
                continue
            day_id, target_order = target
            blocking.update(
                candidate_id
                for candidate_id in clearable
                if positions[candidate_id][0] == day_id
                and positions[candidate_id][1] < target_order
            )
        if not issue.activity_ids:
            issue_day_id = dict(issue.details).get("day_id")
            if isinstance(issue_day_id, str):
                blocking.update(
                    candidate_id
                    for candidate_id in clearable
                    if positions[candidate_id][0] == issue_day_id
                )
    return tuple(
        sorted(blocking)
    )


def _is_ready(evaluated: _Evaluated) -> bool:
    return (
        evaluated.report.status is CheckStatus.FEASIBLE
        and evaluated.score.hard_violation_count == 0
        and evaluated.score.missing_required_count == 0
    )


def _evaluate_assignment_variant(
    problem: ScheduleProblem,
    layout: _Layout,
    assignments: tuple[ScheduleAssignment, ...],
) -> _Evaluated:
    provisional = materialize_schedule(
        problem,
        assignments,
        layout.promoted_activity_ids,
    )
    provisional_report = evaluate_schedule_state(problem, provisional)
    final_assignments = assignments_from_state(
        problem, provisional, provisional_report
    )
    final_state = materialize_schedule(
        problem,
        final_assignments,
        layout.promoted_activity_ids,
    )
    final_report = evaluate_schedule_state(problem, final_state)
    score = score_schedule(
        problem,
        final_state,
        final_report,
        promoted_activity_ids=layout.promoted_activity_ids,
    )
    if (
        final_report.status is CheckStatus.FEASIBLE
        and score.hard_violation_count == 0
        and score.missing_required_count == 0
        and score.protected_change_count == 0
        and score.accepted_activity_change_count
        <= problem.scope.max_accepted_changes
    ):
        _project_schedule_operations(
            problem,
            final_state,
            final_assignments,
            layout.promoted_activity_ids,
        )
    stable_schedule_key = schedule_key(
        final_assignments, layout.promoted_activity_ids
    )
    if {
        day_id: tuple(values) for day_id, values in layout.day_orders
    } != {
        day.day_id: tuple(day.activity_ids) for day in final_state.days
    }:
        raise ScheduleContractError(
            "LAYOUT_MATERIALIZATION_MISMATCH",
            "Trusted materialization did not reproduce the requested raw layout.",
        )
    return _Evaluated(
        layout=layout,
        assignments=final_assignments,
        state=final_state,
        report=final_report,
        score=score,
        schedule_key=stable_schedule_key,
    )


def _neighbor_layouts(
    problem: ScheduleProblem, current: _Layout
) -> Iterator[_Layout]:
    """Yield unique deterministic promote, relocate, and swap neighbors."""

    seen: set[tuple[object, ...]] = set()
    orders = current.as_mapping()
    promoted = frozenset(current.promoted_activity_ids)
    eligible = tuple(
        sorted(
            set(problem.scope.eligible_candidate_ids).difference(promoted)
        )
    )
    promotion_order = _candidate_promotion_order(
        problem,
        eligible,
        enabled_activity_ids=current.promoted_activity_ids,
    )
    active_mutable = tuple(
        sorted(
            activity_id
            for activity_id in problem.scope.mutable_activity_ids
            if problem.state.activity_by_id[activity_id].decision_state
            is DecisionState.SELECTED
            or activity_id in promoted
        )
    )
    promotion_variants: dict[
        str, tuple[dict[str, tuple[str, ...]], ...]
    ] = {}

    required_closures = _required_candidate_closures(
        problem,
        promotion_order,
        enabled_activity_ids=current.promoted_activity_ids,
    )
    for bundle in required_closures:
        layout = _make_layout(
            problem,
            orders,
            tuple(sorted((*promoted, *bundle))),
        )
        if layout.key() == current.key() or layout.key() in seen:
            continue
        seen.add(layout.key())
        yield layout

    for activity_id in promotion_order:
        layout = _make_layout(
            problem,
            orders,
            tuple(sorted((*promoted, activity_id))),
        )
        if layout.key() == current.key() or layout.key() in seen:
            continue
        seen.add(layout.key())
        yield layout

    for activity_id in promotion_order:
        variants = tuple(_placements(problem, orders, activity_id))
        promotion_variants[activity_id] = variants
        for moved in variants:
            layout = _make_layout(
                problem,
                moved,
                tuple(sorted((*promoted, activity_id))),
            )
            if layout.key() == current.key() or layout.key() in seen:
                continue
            seen.add(layout.key())
            yield layout

    for bundle in _linked_candidate_bundles(problem, eligible):
        for bundled_orders in _bundle_placements(
            problem, orders, bundle
        ):
            layout = _make_layout(
                problem,
                bundled_orders,
                tuple(sorted((*promoted, *bundle))),
            )
            if layout.key() == current.key() or layout.key() in seen:
                continue
            seen.add(layout.key())
            yield layout

    for candidate_id in promotion_order:
        for promoted_orders in promotion_variants[candidate_id]:
            for activity_id in active_mutable:
                for compounded in _placements(
                    problem, promoted_orders, activity_id
                ):
                    layout = _make_layout(
                        problem,
                        compounded,
                        tuple(sorted((*promoted, candidate_id))),
                    )
                    if (
                        layout.key() == current.key()
                        or layout.key() in seen
                    ):
                        continue
                    seen.add(layout.key())
                    yield layout

    for activity_id in active_mutable:
        for moved in _placements(problem, orders, activity_id):
            layout = _make_layout(
                problem, moved, current.promoted_activity_ids
            )
            if layout.key() == current.key() or layout.key() in seen:
                continue
            seen.add(layout.key())
            yield layout

    for index, left_id in enumerate(active_mutable):
        for right_id in active_mutable[index + 1 :]:
            swapped = _swap(problem, orders, left_id, right_id)
            if swapped is None:
                continue
            layout = _make_layout(
                problem, swapped, current.promoted_activity_ids
            )
            if layout.key() == current.key() or layout.key() in seen:
                continue
            seen.add(layout.key())
            yield layout


def _candidate_promotion_order(
    problem: ScheduleProblem,
    eligible: tuple[str, ...],
    *,
    enabled_activity_ids: Iterable[str] = (),
) -> tuple[str, ...]:
    """Prioritize candidates that can repair hard coverage immediately."""

    eligible_set = frozenset(eligible)
    active = {
        activity.activity_id
        for activity in problem.state.activities
        if activity.decision_state
        in {
            DecisionState.SELECTED,
            DecisionState.FIXED,
            DecisionState.BOOKED,
        }
    }
    active.update(enabled_activity_ids)
    coverage: set[str] = set()
    for constraint in problem.state.constraints:
        if constraint.strength is not ConstraintStrength.HARD:
            continue
        subjects = frozenset(constraint.subject_ids)
        if constraint.kind in {
            ConstraintKind.MUST_INCLUDE,
            ConstraintKind.BEFORE,
        }:
            coverage.update(eligible_set.intersection(subjects))
        elif constraint.kind is ConstraintKind.REQUIRES:
            if constraint.subject_ids and constraint.subject_ids[0] in active:
                coverage.update(
                    eligible_set.intersection(constraint.subject_ids[1:])
                )
        elif constraint.kind is ConstraintKind.EXACTLY_ONCE:
            if not active.intersection(subjects):
                coverage.update(eligible_set.intersection(subjects))
        elif constraint.kind is ConstraintKind.CHOOSE_N:
            expected = constraint.param("n")
            if (
                isinstance(expected, int)
                and not isinstance(expected, bool)
                and len(active.intersection(subjects)) < expected
            ):
                coverage.update(eligible_set.intersection(subjects))

    return tuple(
        sorted(
            eligible,
            key=lambda activity_id: (
                activity_id not in coverage,
                -problem.state.activity_by_id[activity_id].priority,
                activity_id,
            ),
        )
    )


def _required_candidate_closures(
    problem: ScheduleProblem,
    eligible: tuple[str, ...],
    *,
    enabled_activity_ids: Iterable[str] = (),
) -> Iterator[tuple[str, ...]]:
    """Lazily yield complete hard-REQUIRES promotion closures."""

    eligible_set = frozenset(eligible)
    active = {
        activity.activity_id
        for activity in problem.state.activities
        if activity.decision_state
        in {
            DecisionState.SELECTED,
            DecisionState.FIXED,
            DecisionState.BOOKED,
        }
    }
    active.update(enabled_activity_ids)
    adjacency: dict[str, set[str]] = {}
    for constraint in problem.state.constraints:
        if (
            constraint.strength is ConstraintStrength.HARD
            and constraint.kind is ConstraintKind.REQUIRES
            and len(constraint.subject_ids) >= 2
        ):
            trigger, *dependencies = constraint.subject_ids
            adjacency.setdefault(trigger, set()).update(dependencies)

    def close(initial: Iterable[str]) -> tuple[str, ...] | None:
        closure = set(initial)
        queue = sorted(
            trigger
            for trigger in adjacency
            if trigger in active or trigger in closure
        )
        queued = set(queue)
        index = 0
        complete = True
        while index < len(queue):
            trigger = queue[index]
            index += 1
            for dependency in sorted(adjacency[trigger]):
                if dependency in active or dependency in closure:
                    continue
                if dependency not in eligible_set:
                    complete = False
                    continue
                closure.add(dependency)
                if dependency in adjacency and dependency not in queued:
                    queue.append(dependency)
                    queued.add(dependency)
        if not complete:
            return None
        return tuple(sorted(closure))

    seen: set[tuple[str, ...]] = set()
    active_obligations = close(())
    if active_obligations:
        seen.add(active_obligations)
        yield active_obligations
    for trigger in eligible:
        if trigger not in adjacency:
            continue
        closure = close((trigger,))
        if closure is None or len(closure) < 2 or closure in seen:
            continue
        seen.add(closure)
        yield closure


def _linked_candidate_bundles(
    problem: ScheduleProblem,
    eligible: tuple[str, ...],
) -> tuple[tuple[str, ...], ...]:
    """Return pair-sized non-REQUIRES compound promotion bundles."""

    eligible_set = frozenset(eligible)
    bundles: set[tuple[str, ...]] = set()

    for constraint in problem.state.constraints:
        if constraint.strength is not ConstraintStrength.HARD:
            continue
        subjects = [
            subject_id
            for subject_id in constraint.subject_ids
            if subject_id in eligible_set
        ]
        if constraint.kind in {
            ConstraintKind.BEFORE,
            ConstraintKind.MUST_INCLUDE,
            ConstraintKind.CHOOSE_N,
        }:
            bundles.update(
                tuple(sorted((left, right)))
                for index, left in enumerate(subjects)
                for right in subjects[index + 1 :]
            )
    return tuple(sorted(bundles, key=lambda item: (len(item), item)))


def _bundle_placements(
    problem: ScheduleProblem,
    orders: Mapping[str, tuple[str, ...]],
    activity_ids: tuple[str, ...],
) -> Iterator[dict[str, tuple[str, ...]]]:
    """Yield deterministic placement products for one bounded closure bundle."""

    def visit(
        index: int, current_orders: Mapping[str, tuple[str, ...]]
    ) -> Iterator[dict[str, tuple[str, ...]]]:
        if index == len(activity_ids):
            yield {
                day_id: tuple(values)
                for day_id, values in current_orders.items()
            }
            return
        activity_id = activity_ids[index]
        for moved in _placements(
            problem, current_orders, activity_id
        ):
            yield from visit(index + 1, moved)

    yield from visit(0, orders)


def _placements(
    problem: ScheduleProblem,
    orders: Mapping[str, tuple[str, ...]],
    activity_id: str,
) -> Iterator[dict[str, tuple[str, ...]]]:
    source_day = next(
        day_id for day_id, values in orders.items() if activity_id in values
    )
    activity = problem.state.activity_by_id[activity_id]
    if activity.flexibility is Flexibility.FIXED_DAY:
        yield {
            day_id: tuple(values) for day_id, values in orders.items()
        }
        return
    allowed_days = _allowed_days(problem, activity_id)
    for day_id in sorted(allowed_days, key=_day_rank(problem).__getitem__):
        source_values = list(orders[source_day])
        source_values.remove(activity_id)
        target_values = (
            source_values
            if day_id == source_day
            else list(orders[day_id])
        )
        for index in range(len(target_values) + 1):
            moved = {key: tuple(value) for key, value in orders.items()}
            if day_id == source_day:
                candidate = list(source_values)
                candidate.insert(index, activity_id)
                moved[source_day] = tuple(candidate)
            else:
                candidate_source = list(orders[source_day])
                candidate_source.remove(activity_id)
                candidate_target = list(orders[day_id])
                candidate_target.insert(index, activity_id)
                moved[source_day] = tuple(candidate_source)
                moved[day_id] = tuple(candidate_target)
            yield moved


def _swap(
    problem: ScheduleProblem,
    orders: Mapping[str, tuple[str, ...]],
    left_id: str,
    right_id: str,
) -> dict[str, tuple[str, ...]] | None:
    left_day = next(
        day_id for day_id, values in orders.items() if left_id in values
    )
    right_day = next(
        day_id for day_id, values in orders.items() if right_id in values
    )
    if right_day not in _allowed_days(problem, left_id):
        return None
    if left_day not in _allowed_days(problem, right_id):
        return None
    left_activity = problem.state.activity_by_id[left_id]
    right_activity = problem.state.activity_by_id[right_id]
    if (
        left_activity.flexibility is Flexibility.FIXED_DAY
        and right_day != left_activity.day_id
    ) or (
        right_activity.flexibility is Flexibility.FIXED_DAY
        and left_day != right_activity.day_id
    ):
        return None
    result = {day_id: list(values) for day_id, values in orders.items()}
    left_index = result[left_day].index(left_id)
    right_index = result[right_day].index(right_id)
    result[left_day][left_index] = right_id
    result[right_day][right_index] = left_id
    return {day_id: tuple(values) for day_id, values in result.items()}


def _allowed_days(problem: ScheduleProblem, activity_id: str) -> frozenset[str]:
    allowed = frozenset(problem.scope.day_ids)
    for constraint in problem.state.constraints:
        if (
            constraint.strength is not ConstraintStrength.HARD
            or constraint.kind is not ConstraintKind.ALLOWED_DAY
            or activity_id not in constraint.subject_ids
        ):
            continue
        value = constraint.param("day_ids", constraint.param("day_id"))
        if not isinstance(value, str):
            continue
        constrained = frozenset(
            part.strip() for part in value.split(",") if part.strip()
        )
        allowed = allowed.intersection(constrained)
    return allowed


def _make_layout(
    problem: ScheduleProblem,
    orders: Mapping[str, Iterable[str]],
    promoted_activity_ids: Iterable[str],
) -> _Layout:
    return _Layout(
        day_orders=tuple(
            (day_id, tuple(orders[day_id]))
            for day_id in sorted(orders, key=_day_rank(problem).__getitem__)
        ),
        promoted_activity_ids=tuple(sorted(promoted_activity_ids)),
    )


def _day_rank(problem: ScheduleProblem) -> dict[str, int]:
    return {
        day.day_id: index
        for index, day in enumerate(
            sorted(problem.state.days, key=lambda item: (item.date, item.day_id))
        )
    }


def _failure_result(
    problem: ScheduleProblem,
    status: ScheduleStatus,
    code: str,
    message: str,
    *,
    budget: _EvaluationBudget,
    evaluated: _Evaluated | None = None,
    activity_ids: tuple[str, ...] = (),
) -> ScheduleResult:
    issues = evaluated.report.issues if evaluated is not None else ()
    missing_arcs = _missing_arcs(issues)
    issue_activity_ids = {
        activity_id for issue in issues for activity_id in issue.activity_ids
    }
    constraint_ids = {
        str(dict(issue.details)["constraint_id"])
        for issue in issues
        if dict(issue.details).get("constraint_id") is not None
    }
    day_ids = {
        str(dict(issue.details)["day_id"])
        for issue in issues
        if dict(issue.details).get("day_id") is not None
    }
    failure = ScheduleFailure(
        kind=status,
        code=code,
        problem_id=problem.problem_id,
        message=message,
        activity_ids=tuple(
            sorted(set(activity_ids) | issue_activity_ids)
        ),
        day_ids=tuple(sorted(day_ids)),
        constraint_ids=tuple(sorted(constraint_ids)),
        kernel_issues=issues,
        missing_arc_keys=missing_arcs,
        evaluations_used=budget.used,
        evaluation_limit=budget.limit,
    )
    return ScheduleResult(
        status=status,
        problem_id=problem.problem_id,
        failure=failure,
        evaluations_used=budget.used,
        evaluation_limit=budget.limit,
    )


def _evaluation_limit_result(
    problem: ScheduleProblem,
    budget: _EvaluationBudget,
    *,
    evaluated: _Evaluated | None = None,
) -> ScheduleResult:
    return _failure_result(
        problem,
        ScheduleStatus.SEARCH_EXHAUSTED,
        "NO_SOLUTION_WITHIN_EVALUATION_LIMIT",
        (
            "The deterministic assignment-variant evaluation limit was "
            "reached before a complete authorized candidate was found."
        ),
        budget=budget,
        evaluated=evaluated,
    )


def _missing_arcs(issues: Iterable[CheckIssue]) -> tuple[str, ...]:
    keys: set[str] = set()
    for issue in issues:
        if issue.code != "MISSING_TRAVEL_ESTIMATE":
            continue
        details = dict(issue.details)
        day_id = details.get("day_id")
        from_location = details.get("from_location_id")
        to_location = details.get("to_location_id")
        if all(isinstance(item, str) and item for item in (
            day_id,
            from_location,
            to_location,
        )):
            keys.add(f"{day_id}|{from_location}|{to_location}")
    return tuple(sorted(keys))


__all__ = ["SOLVER_VERSION", "solve_schedule"]
