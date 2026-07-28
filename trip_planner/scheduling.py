"""Solver-independent contracts and scoring for Phase 3 scheduling.

This module is deliberately pure.  It does not read files, call providers,
write canonical plans, or commit mutations.  Solvers may only arrange existing
stable activity IDs and promote explicitly eligible candidates; the planning
kernel remains the authority on feasibility.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .codec import plan_to_trip_state
from .composition import ComposedTripState, EvidenceBinding
from .models import (
    Activity,
    CheckIssue,
    CheckReport,
    CheckStatus,
    ConstraintKind,
    ConstraintStrength,
    DaySpec,
    DecisionState,
    Flexibility,
    IssueSeverity,
    TripState,
)
from .mutations import (
    PATCH_VERSION,
    PLAN_PATCH_MAX_OPERATIONS,
    PlaceActivity,
    Placement,
    PlanPatch,
    UpdateActivity,
)
from .timeline import evaluate_timeline


SCHEDULE_PROBLEM_VERSION = "schedule-problem/v2"
SCHEDULE_CANDIDATE_VERSION = "schedule-candidate/v1"

_MAX_CANDIDATES = 1
_MAX_EVALUATIONS = 100_000
_ACTIVE_DECISIONS = frozenset(
    {
        DecisionState.SELECTED,
        DecisionState.FIXED,
        DecisionState.BOOKED,
    }
)
_COVERAGE_ISSUE_CODES = frozenset(
    {
        "MISSING_REQUIRED_ACTIVITY",
        "EXACTLY_ONCE_VIOLATION",
        "CHOOSE_N_VIOLATION",
        "REQUIRES_VIOLATION",
        "PRECEDENCE_SUBJECT_MISSING",
    }
)


class ScheduleContractError(ValueError):
    """A malformed solver-independent scheduling value."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class EvidencePolicy(str, Enum):
    """Whether a non-verified numeric fact may appear in a draft."""

    VERIFIED_ONLY = "verified_only"
    ALLOW_UNVERIFIED_DRAFT = "allow_unverified_draft"


class ScheduleStatus(str, Enum):
    """Outcome categories that do not confuse search failure with impossibility."""

    SOLVED = "solved"
    NEEDS_EVIDENCE = "needs_evidence"
    PROVEN_INFEASIBLE = "proven_infeasible"
    SEARCH_EXHAUSTED = "search_exhausted"
    INVALID_INPUT = "invalid_input"
    ENGINE_ERROR = "engine_error"


@dataclass(frozen=True, slots=True)
class ReplanScope:
    """The exact days and activities a scheduler is allowed to change."""

    day_ids: tuple[str, ...]
    mutable_activity_ids: tuple[str, ...]
    eligible_candidate_ids: tuple[str, ...] = ()
    max_accepted_changes: int = 12

    def __post_init__(self) -> None:
        for value, name in (
            (self.day_ids, "ReplanScope.day_ids"),
            (self.mutable_activity_ids, "ReplanScope.mutable_activity_ids"),
            (self.eligible_candidate_ids, "ReplanScope.eligible_candidate_ids"),
        ):
            _require_id_tuple(value, name)
        if not self.day_ids:
            raise ScheduleContractError(
                "INVALID_SCOPE", "ReplanScope.day_ids cannot be empty."
            )
        if (
            isinstance(self.max_accepted_changes, bool)
            or not isinstance(self.max_accepted_changes, int)
            or self.max_accepted_changes < 0
        ):
            raise ScheduleContractError(
                "INVALID_SCOPE",
                "ReplanScope.max_accepted_changes must be a non-negative integer.",
            )
        object.__setattr__(self, "day_ids", tuple(sorted(self.day_ids)))
        object.__setattr__(
            self,
            "mutable_activity_ids",
            tuple(sorted(self.mutable_activity_ids)),
        )
        object.__setattr__(
            self,
            "eligible_candidate_ids",
            tuple(sorted(self.eligible_candidate_ids)),
        )


@dataclass(frozen=True, slots=True)
class SchedulePreferences:
    """Concrete V1 pace and evidence preferences; no hidden weighted score."""

    evidence_policy: EvidencePolicy = EvidencePolicy.VERIFIED_ONLY
    minimum_end_slack_min: float = 0.0
    max_activities_by_day: tuple[tuple[str, int], ...] = ()
    max_service_min_by_day: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_policy, EvidencePolicy):
            raise ScheduleContractError(
                "INVALID_INPUT",
                "SchedulePreferences.evidence_policy must be EvidencePolicy.",
            )
        _require_non_negative_number(
            self.minimum_end_slack_min,
            "SchedulePreferences.minimum_end_slack_min",
        )
        object.__setattr__(
            self,
            "max_activities_by_day",
            _normalize_day_limits(
                self.max_activities_by_day,
                "SchedulePreferences.max_activities_by_day",
                integer=True,
            ),
        )
        object.__setattr__(
            self,
            "max_service_min_by_day",
            _normalize_day_limits(
                self.max_service_min_by_day,
                "SchedulePreferences.max_service_min_by_day",
                integer=False,
            ),
        )


@dataclass(frozen=True, slots=True)
class SearchLimits:
    """Deterministic evaluation bounds shared by every solver."""

    max_candidates: int = 1
    max_evaluations: int = 20_000

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_candidates, bool)
            or not isinstance(self.max_candidates, int)
            or not 1 <= self.max_candidates <= _MAX_CANDIDATES
        ):
            raise ScheduleContractError(
                "INVALID_INPUT",
                f"SearchLimits.max_candidates must be between 1 and {_MAX_CANDIDATES}.",
            )
        if (
            isinstance(self.max_evaluations, bool)
            or not isinstance(self.max_evaluations, int)
            or not 1 <= self.max_evaluations <= _MAX_EVALUATIONS
        ):
            raise ScheduleContractError(
                "INVALID_INPUT",
                (
                    "SearchLimits.max_evaluations must be between 1 and "
                    f"{_MAX_EVALUATIONS}."
                ),
            )


@dataclass(frozen=True, slots=True)
class ScheduleProblem:
    """One immutable, replayable scheduling request."""

    state: TripState = field(repr=False)
    evaluation_at: datetime
    scope: ReplanScope
    trip_id: str
    preferences: SchedulePreferences = field(default_factory=SchedulePreferences)
    limits: SearchLimits = field(default_factory=SearchLimits)
    contract_version: str = SCHEDULE_PROBLEM_VERSION
    base_state_digest: str = ""
    canonical_state_digest: str = ""
    evidence_binding: EvidenceBinding | None = None
    problem_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.state, TripState):
            raise ScheduleContractError(
                "INVALID_INPUT", "ScheduleProblem.state must be TripState."
            )
        if (
            not isinstance(self.evaluation_at, datetime)
            or self.evaluation_at.tzinfo is None
            or self.evaluation_at.utcoffset() is None
        ):
            raise ScheduleContractError(
                "INVALID_INPUT",
                "ScheduleProblem.evaluation_at must be timezone-aware.",
            )
        if not isinstance(self.scope, ReplanScope):
            raise ScheduleContractError(
                "INVALID_INPUT", "ScheduleProblem.scope must be ReplanScope."
            )
        _require_patch_identity(self.trip_id, "ScheduleProblem.trip_id")
        _require_patch_identity(
            self.state.revision,
            "ScheduleProblem.state.revision",
        )
        for day in self.state.days:
            _require_patch_identity(day.day_id, "DaySpec.day_id")
            for activity_id in day.activity_ids:
                _require_patch_identity(
                    activity_id,
                    "DaySpec.activity_ids item",
                )
        for activity in self.state.activities:
            _require_patch_identity(
                activity.activity_id,
                "Activity.activity_id",
            )
        for value, name in (
            (self.scope.day_ids, "ReplanScope.day_ids"),
            (
                self.scope.mutable_activity_ids,
                "ReplanScope.mutable_activity_ids",
            ),
            (
                self.scope.eligible_candidate_ids,
                "ReplanScope.eligible_candidate_ids",
            ),
        ):
            for item in value:
                _require_patch_identity(item, f"{name} item")
        if not isinstance(self.preferences, SchedulePreferences):
            raise ScheduleContractError(
                "INVALID_INPUT",
                "ScheduleProblem.preferences must be SchedulePreferences.",
            )
        if not isinstance(self.limits, SearchLimits):
            raise ScheduleContractError(
                "INVALID_INPUT", "ScheduleProblem.limits must be SearchLimits."
            )
        if self.contract_version != SCHEDULE_PROBLEM_VERSION:
            raise ScheduleContractError(
                "UNSUPPORTED_VERSION",
                (
                    f"Unsupported schedule problem version "
                    f"{self.contract_version!r}."
                ),
            )
        expected_state_digest = trip_state_digest(self.state)
        if self.base_state_digest and self.base_state_digest != expected_state_digest:
            raise ScheduleContractError(
                "DIGEST_MISMATCH",
                "ScheduleProblem.base_state_digest does not match state.",
            )
        object.__setattr__(self, "base_state_digest", expected_state_digest)
        canonical_state_digest = (
            self.canonical_state_digest or expected_state_digest
        )
        if not _is_sha256_digest(canonical_state_digest):
            raise ScheduleContractError(
                "INVALID_INPUT",
                "ScheduleProblem.canonical_state_digest must be a state digest.",
            )
        object.__setattr__(
            self, "canonical_state_digest", canonical_state_digest
        )
        evidence_binding_digest: str | None = None
        if self.evidence_binding is not None:
            if type(self.evidence_binding) is not EvidenceBinding:
                raise ScheduleContractError(
                    "INVALID_INPUT",
                    "ScheduleProblem.evidence_binding must be EvidenceBinding.",
                )
            if self.evidence_binding.evaluation_at != self.evaluation_at:
                raise ScheduleContractError(
                    "INVALID_INPUT",
                    "ScheduleProblem evidence and evaluation clocks differ.",
                )
            evidence_binding_digest = self.evidence_binding.binding_digest
        expected_problem_id = _digest(
            {
                "contract_version": self.contract_version,
                "trip_id": self.trip_id,
                "base_revision": self.state.revision,
                "base_state_digest": expected_state_digest,
                "canonical_state_digest": canonical_state_digest,
                "evidence_binding_digest": evidence_binding_digest,
                "evaluation_at": _utc_iso(self.evaluation_at),
                "scope": _stable_value(self.scope),
                "preferences": _stable_value(self.preferences),
                "limits": _stable_value(self.limits),
            },
            prefix="schedule-problem",
        )
        if self.problem_id and self.problem_id != expected_problem_id:
            raise ScheduleContractError(
                "DIGEST_MISMATCH",
                "ScheduleProblem.problem_id does not match semantic input.",
            )
        object.__setattr__(self, "problem_id", expected_problem_id)

    @property
    def base_revision(self) -> str:
        return self.state.revision


@dataclass(frozen=True, slots=True)
class ScheduleAssignment:
    """One active activity's final persisted day, raw order, and local start."""

    activity_id: str
    day_id: str
    order: int
    scheduled_start: time | None

    def __post_init__(self) -> None:
        _require_id(self.activity_id, "ScheduleAssignment.activity_id")
        _require_id(self.day_id, "ScheduleAssignment.day_id")
        if (
            isinstance(self.order, bool)
            or not isinstance(self.order, int)
            or self.order < 0
        ):
            raise ScheduleContractError(
                "INVALID_CANDIDATE",
                "ScheduleAssignment.order must be a non-negative integer.",
            )
        if self.scheduled_start is not None:
            if not isinstance(self.scheduled_start, time):
                raise ScheduleContractError(
                    "INVALID_CANDIDATE",
                    "ScheduleAssignment.scheduled_start must be time or None.",
                )
            if self.scheduled_start.tzinfo is not None:
                raise ScheduleContractError(
                    "INVALID_CANDIDATE",
                    "ScheduleAssignment.scheduled_start must be timezone-naive.",
                )
            if self.scheduled_start.fold:
                raise ScheduleContractError(
                    "INVALID_CANDIDATE",
                    (
                        "ScheduleAssignment.scheduled_start cannot encode "
                        "PEP 495 fold; persisted local ISO time has no fold field."
                    ),
                )


@dataclass(frozen=True, slots=True)
class ScheduleScore:
    """Transparent objective components; every numeric field is exact integer."""

    hard_violation_count: int
    missing_required_count: int
    protected_change_count: int
    accepted_activity_change_count: int
    accepted_day_move_count: int
    accepted_order_inversion_count: int
    accepted_time_shift_deci_min: int
    served_priority_points: int
    soft_constraint_violation_count: int
    verification_risk_count: int
    tight_slack_count: int
    slack_deficit_deci_min: int
    activity_count_overage: int
    service_overage_deci_min: int
    wait_deci_min: int
    travel_deci_min: int
    buffer_deci_min: int
    service_deci_min: int
    changed_activity_ids: tuple[str, ...] = ()
    protected_activity_ids: tuple[str, ...] = ()
    scheduled_optional_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        numeric = (
            "hard_violation_count",
            "missing_required_count",
            "protected_change_count",
            "accepted_activity_change_count",
            "accepted_day_move_count",
            "accepted_order_inversion_count",
            "accepted_time_shift_deci_min",
            "soft_constraint_violation_count",
            "verification_risk_count",
            "tight_slack_count",
            "slack_deficit_deci_min",
            "activity_count_overage",
            "service_overage_deci_min",
            "wait_deci_min",
            "travel_deci_min",
            "buffer_deci_min",
            "service_deci_min",
        )
        for name in numeric:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ScheduleContractError(
                    "INVALID_SCORE",
                    f"ScheduleScore.{name} must be a non-negative integer.",
                )
        if (
            isinstance(self.served_priority_points, bool)
            or not isinstance(self.served_priority_points, int)
        ):
            raise ScheduleContractError(
                "INVALID_SCORE",
                "ScheduleScore.served_priority_points must be an integer.",
            )
        for value, name in (
            (self.changed_activity_ids, "changed_activity_ids"),
            (self.protected_activity_ids, "protected_activity_ids"),
            (self.scheduled_optional_ids, "scheduled_optional_ids"),
        ):
            _require_id_tuple(value, f"ScheduleScore.{name}")
            if tuple(sorted(value)) != value:
                raise ScheduleContractError(
                    "INVALID_SCORE",
                    f"ScheduleScore.{name} must be sorted.",
                )

    def objective_key(self) -> tuple[int, ...]:
        """Return the solver-independent key; lower is always better."""

        return (
            self.hard_violation_count,
            self.missing_required_count,
            self.verification_risk_count,
            self.protected_change_count,
            self.accepted_activity_change_count,
            self.accepted_day_move_count,
            self.accepted_order_inversion_count,
            self.accepted_time_shift_deci_min,
            -self.served_priority_points,
            len(self.scheduled_optional_ids),
            self.soft_constraint_violation_count,
            self.tight_slack_count,
            self.slack_deficit_deci_min,
            self.activity_count_overage,
            self.service_overage_deci_min,
            self.wait_deci_min,
            self.travel_deci_min,
        )

    @property
    def travel_min(self) -> float:
        return self.travel_deci_min / 10.0

    @property
    def buffer_min(self) -> float:
        return self.buffer_deci_min / 10.0

    @property
    def service_min(self) -> float:
        return self.service_deci_min / 10.0

    @property
    def wait_min(self) -> float:
        return self.wait_deci_min / 10.0


@dataclass(frozen=True, slots=True)
class ScheduleCandidate:
    """One complete active schedule that can be independently replayed."""

    candidate_id: str
    problem_id: str
    base_revision: str
    base_state_digest: str
    assignments: tuple[ScheduleAssignment, ...]
    promoted_activity_ids: tuple[str, ...]
    required_arc_keys: tuple[str, ...]
    report: CheckReport = field(repr=False)
    score: ScheduleScore = field(repr=False)
    schedule_key: str
    solver: str
    contract_version: str = SCHEDULE_CANDIDATE_VERSION

    def __post_init__(self) -> None:
        for value, name in (
            (self.candidate_id, "ScheduleCandidate.candidate_id"),
            (self.problem_id, "ScheduleCandidate.problem_id"),
            (self.base_revision, "ScheduleCandidate.base_revision"),
            (self.base_state_digest, "ScheduleCandidate.base_state_digest"),
            (self.schedule_key, "ScheduleCandidate.schedule_key"),
            (self.solver, "ScheduleCandidate.solver"),
        ):
            _require_id(value, name)
        if self.contract_version != SCHEDULE_CANDIDATE_VERSION:
            raise ScheduleContractError(
                "UNSUPPORTED_VERSION",
                f"Unsupported candidate version {self.contract_version!r}.",
            )
        if not isinstance(self.assignments, tuple) or any(
            type(item) is not ScheduleAssignment
            for item in self.assignments
        ):
            raise ScheduleContractError(
                "INVALID_CANDIDATE",
                "ScheduleCandidate.assignments must contain ScheduleAssignment.",
            )
        assignment_ids = [item.activity_id for item in self.assignments]
        if len(set(assignment_ids)) != len(assignment_ids):
            raise ScheduleContractError(
                "INVALID_CANDIDATE",
                "ScheduleCandidate.assignments contains duplicate activity IDs.",
            )
        for value, name in (
            (self.promoted_activity_ids, "promoted_activity_ids"),
            (self.required_arc_keys, "required_arc_keys"),
        ):
            _require_id_tuple(value, f"ScheduleCandidate.{name}")
            if tuple(sorted(value)) != value:
                raise ScheduleContractError(
                    "INVALID_CANDIDATE",
                    f"ScheduleCandidate.{name} must be sorted.",
                )
        if type(self.report) is not CheckReport:
            raise ScheduleContractError(
                "INVALID_CANDIDATE",
                "ScheduleCandidate.report must be an exact CheckReport.",
            )
        if type(self.score) is not ScheduleScore:
            raise ScheduleContractError(
                "INVALID_CANDIDATE",
                "ScheduleCandidate.score must be an exact ScheduleScore.",
            )


@dataclass(frozen=True, slots=True)
class ScheduleFailure:
    """A typed non-solution; never carries a commit-ready partial candidate."""

    kind: ScheduleStatus
    code: str
    problem_id: str
    message: str
    activity_ids: tuple[str, ...] = ()
    day_ids: tuple[str, ...] = ()
    constraint_ids: tuple[str, ...] = ()
    kernel_issues: tuple[CheckIssue, ...] = ()
    missing_arc_keys: tuple[str, ...] = ()
    evaluations_used: int = 0
    evaluation_limit: int = 0

    def __post_init__(self) -> None:
        if self.kind is ScheduleStatus.SOLVED:
            raise ScheduleContractError(
                "INVALID_RESULT", "ScheduleFailure.kind cannot be SOLVED."
            )
        for value, name in (
            (self.code, "ScheduleFailure.code"),
            (self.problem_id, "ScheduleFailure.problem_id"),
            (self.message, "ScheduleFailure.message"),
        ):
            _require_id(value, name)
        for value, name in (
            (self.activity_ids, "activity_ids"),
            (self.day_ids, "day_ids"),
            (self.constraint_ids, "constraint_ids"),
            (self.missing_arc_keys, "missing_arc_keys"),
        ):
            _require_id_tuple(value, f"ScheduleFailure.{name}")
        if not isinstance(self.kernel_issues, tuple) or any(
            not isinstance(item, CheckIssue) for item in self.kernel_issues
        ):
            raise ScheduleContractError(
                "INVALID_RESULT",
                "ScheduleFailure.kernel_issues must contain CheckIssue.",
            )
        for value, name in (
            (self.evaluations_used, "evaluations_used"),
            (self.evaluation_limit, "evaluation_limit"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ScheduleContractError(
                    "INVALID_RESULT",
                    f"ScheduleFailure.{name} must be a non-negative integer.",
                )


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    """A solved candidate set or exactly one typed failure."""

    status: ScheduleStatus
    problem_id: str
    candidates: tuple[ScheduleCandidate, ...] = ()
    failure: ScheduleFailure | None = None
    evaluations_used: int = 0
    evaluation_limit: int = 0
    optimality: str = "not_claimed"

    def __post_init__(self) -> None:
        _require_id(self.problem_id, "ScheduleResult.problem_id")
        if not isinstance(self.status, ScheduleStatus):
            raise ScheduleContractError(
                "INVALID_RESULT", "ScheduleResult.status must be ScheduleStatus."
            )
        if not isinstance(self.candidates, tuple) or any(
            not isinstance(item, ScheduleCandidate) for item in self.candidates
        ):
            raise ScheduleContractError(
                "INVALID_RESULT",
                "ScheduleResult.candidates must contain ScheduleCandidate.",
            )
        if self.status is ScheduleStatus.SOLVED:
            if not self.candidates or self.failure is not None:
                raise ScheduleContractError(
                    "INVALID_RESULT",
                    "SOLVED requires candidates and no failure.",
                )
        elif self.candidates or self.failure is None:
            raise ScheduleContractError(
                "INVALID_RESULT",
                "A non-solved result requires one failure and no candidates.",
            )
        if self.failure is not None:
            if self.failure.kind is not self.status:
                raise ScheduleContractError(
                    "INVALID_RESULT",
                    "ScheduleResult status and failure kind must match.",
                )
            if self.failure.problem_id != self.problem_id:
                raise ScheduleContractError(
                    "INVALID_RESULT",
                    "ScheduleResult and failure problem IDs must match.",
                )
        for value, name in (
            (self.evaluations_used, "evaluations_used"),
            (self.evaluation_limit, "evaluation_limit"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ScheduleContractError(
                    "INVALID_RESULT",
                    f"ScheduleResult.{name} must be a non-negative integer.",
                )
        _require_id(self.optimality, "ScheduleResult.optimality")

    @property
    def candidate(self) -> ScheduleCandidate | None:
        return self.candidates[0] if self.candidates else None


def default_replan_scope(
    state: TripState,
    *,
    day_ids: Iterable[str] | None = None,
    eligible_candidate_ids: Iterable[str] = (),
    max_accepted_changes: int = 12,
) -> ReplanScope:
    """Build the largest safe V1 scope without granting protected authority."""

    target_days = frozenset(
        day_ids if day_ids is not None else (day.day_id for day in state.days)
    )
    eligible = frozenset(eligible_candidate_ids)
    mutable = tuple(
        sorted(
            activity.activity_id
            for activity in state.activities
            if activity.day_id in target_days
            and activity.decision_state not in {
                DecisionState.FIXED,
                DecisionState.BOOKED,
            }
            and activity.flexibility is not Flexibility.FIXED_TIME
            and (
                activity.flexibility is not Flexibility.FIXED_DAY
                or (
                    activity.decision_state is DecisionState.CANDIDATE
                    and activity.activity_id in eligible
                )
            )
            and (
                activity.decision_state is DecisionState.SELECTED
                or activity.activity_id in eligible
            )
        )
    )
    return ReplanScope(
        day_ids=tuple(target_days),
        mutable_activity_ids=mutable,
        eligible_candidate_ids=tuple(eligible),
        max_accepted_changes=max_accepted_changes,
    )


def schedule_problem_from_plan(
    plan: Mapping[str, Any],
    *,
    evaluation_at: datetime,
    scope: ReplanScope | None = None,
    preferences: SchedulePreferences | None = None,
    limits: SearchLimits | None = None,
) -> ScheduleProblem:
    """Build a scheduling problem from canonical plan identity and state.

    Callers must not infer canonical identity from the presentation slug.
    ``plan_to_trip_state`` strictly validates the document; its top-level
    ``trip_id`` is then bound into the replay digest.
    """

    state = plan_to_trip_state(plan)
    trip_id = plan.get("trip_id")
    _require_id(trip_id, "canonical plan trip_id")
    return ScheduleProblem(
        state=state,
        evaluation_at=evaluation_at,
        scope=scope if scope is not None else default_replan_scope(state),
        trip_id=trip_id,
        preferences=(
            preferences if preferences is not None else SchedulePreferences()
        ),
        limits=limits if limits is not None else SearchLimits(),
    )


def schedule_problem_from_composed(
    composed: ComposedTripState,
    *,
    scope: ReplanScope | None = None,
    preferences: SchedulePreferences | None = None,
    limits: SearchLimits | None = None,
) -> ScheduleProblem:
    """Build a problem pinned to one exact runtime evidence composition.

    The evidence snapshot owns the sole evaluation clock.  Callers cannot
    substitute a different timestamp while retaining the same evidence
    binding.
    """

    if type(composed) is not ComposedTripState:
        raise TypeError(
            "schedule_problem_from_composed requires ComposedTripState"
        )
    state = composed.state
    return ScheduleProblem(
        state=state,
        evaluation_at=composed.evidence.evaluation_at,
        scope=scope if scope is not None else default_replan_scope(state),
        trip_id=composed.trip_id,
        preferences=(
            preferences if preferences is not None else SchedulePreferences()
        ),
        limits=limits if limits is not None else SearchLimits(),
        canonical_state_digest=composed.canonical_state_digest,
        evidence_binding=composed.evidence,
    )


def trip_state_digest(state: TripState) -> str:
    """Hash semantic state while ignoring tuple container permutations."""

    return _digest(_trip_state_payload(state), prefix="trip-state")


def validate_schedule_problem(
    problem: ScheduleProblem,
) -> tuple[ScheduleFailure, ...]:
    """Return deterministic cross-reference and authority failures."""

    failures: list[ScheduleFailure] = []
    state = problem.state
    day_ids = frozenset(day.day_id for day in state.days)
    activity_by_id = state.activity_by_id
    scope_days = frozenset(problem.scope.day_ids)
    mutable = frozenset(problem.scope.mutable_activity_ids)
    eligible = frozenset(problem.scope.eligible_candidate_ids)

    if not state.revision:
        failures.append(
            _invalid_failure(
                problem,
                "MISSING_BASE_REVISION",
                "Scheduling requires a canonical non-empty base revision.",
            )
        )
    unknown_days = tuple(sorted(scope_days.difference(day_ids)))
    if unknown_days:
        failures.append(
            _invalid_failure(
                problem,
                "INVALID_REFERENCE",
                "Replan scope refers to unknown days.",
                day_ids=unknown_days,
            )
        )
    unknown_mutable = tuple(sorted(mutable.difference(activity_by_id)))
    if unknown_mutable:
        failures.append(
            _invalid_failure(
                problem,
                "INVALID_REFERENCE",
                "Replan scope refers to unknown mutable activities.",
                activity_ids=unknown_mutable,
            )
        )
    unknown_eligible = tuple(sorted(eligible.difference(activity_by_id)))
    if unknown_eligible:
        failures.append(
            _invalid_failure(
                problem,
                "INVALID_REFERENCE",
                "Replan scope refers to unknown eligible candidates.",
                activity_ids=unknown_eligible,
            )
        )
    if not eligible.issubset(mutable):
        failures.append(
            _invalid_failure(
                problem,
                "INVALID_SCOPE",
                "Every eligible candidate must also be mutable.",
                activity_ids=tuple(sorted(eligible.difference(mutable))),
            )
        )

    for activity_id in sorted(mutable.intersection(activity_by_id)):
        activity = activity_by_id[activity_id]
        if activity.day_id not in scope_days:
            failures.append(
                _invalid_failure(
                    problem,
                    "INVALID_SCOPE",
                    "Mutable activity is outside the target day scope.",
                    activity_ids=(activity_id,),
                    day_ids=(activity.day_id,),
                )
            )
        if activity.decision_state in {
            DecisionState.FIXED,
            DecisionState.BOOKED,
        } or activity.flexibility is Flexibility.FIXED_TIME:
            failures.append(
                _invalid_failure(
                    problem,
                    "PROTECTED_ACTIVITY_MUTABLE",
                    "Fixed, booked, and fixed-time activities cannot be mutable.",
                    activity_ids=(activity_id,),
                    day_ids=(activity.day_id,),
                )
            )
        if (
            activity.decision_state is DecisionState.CANDIDATE
            and activity_id not in eligible
        ) or activity.decision_state in {
            DecisionState.CANCELLED,
            DecisionState.EXCLUDED,
        }:
            failures.append(
                _invalid_failure(
                    problem,
                    "INVALID_SCOPE",
                    (
                        "Mutable activities must be selected or explicitly "
                        "eligible candidates."
                    ),
                    activity_ids=(activity_id,),
                )
            )
        if (
            activity.flexibility is Flexibility.FIXED_DAY
            and not (
                activity.decision_state is DecisionState.CANDIDATE
                and activity_id in eligible
            )
        ):
            failures.append(
                _invalid_failure(
                    problem,
                    "PROTECTED_ACTIVITY_MUTABLE",
                    (
                        "A fixed-day activity is mutable only for an "
                        "in-place eligible-candidate promotion."
                    ),
                    activity_ids=(activity_id,),
                    day_ids=(activity.day_id,),
                )
            )
    for activity_id in sorted(eligible.intersection(activity_by_id)):
        activity = activity_by_id[activity_id]
        if activity.decision_state is not DecisionState.CANDIDATE:
            failures.append(
                _invalid_failure(
                    problem,
                    "INVALID_SCOPE",
                    "Eligible IDs must currently have candidate decision state.",
                    activity_ids=(activity_id,),
                )
            )

    referenced: list[str] = [
        activity_id for day in state.days for activity_id in day.activity_ids
    ]
    referenced_set = frozenset(referenced)
    missing_membership = tuple(sorted(set(activity_by_id).difference(referenced_set)))
    duplicate_membership = tuple(
        sorted(
            activity_id
            for activity_id in set(referenced)
            if referenced.count(activity_id) > 1
        )
    )
    if missing_membership or duplicate_membership:
        failures.append(
            _invalid_failure(
                problem,
                "INVALID_REFERENCE",
                "Every activity must appear in exactly one day activity list.",
                activity_ids=tuple(
                    sorted(set(missing_membership) | set(duplicate_membership))
                ),
            )
        )
    placement_mismatch = tuple(
        sorted(
            activity_id
            for day in state.days
            for index, activity_id in enumerate(day.activity_ids)
            if activity_id in activity_by_id
            and (
                activity_by_id[activity_id].day_id != day.day_id
                or activity_by_id[activity_id].order != index
            )
        )
    )
    if placement_mismatch:
        failures.append(
            _invalid_failure(
                problem,
                "INCONSISTENT_ACTIVITY_PLACEMENT",
                (
                    "Activity day_id/order fields must exactly match their "
                    "DaySpec.activity_ids membership and raw index."
                ),
                activity_ids=placement_mismatch,
            )
        )

    preference_days = {
        day_id for day_id, _ in problem.preferences.max_activities_by_day
    } | {
        day_id for day_id, _ in problem.preferences.max_service_min_by_day
    }
    unknown_preference_days = tuple(sorted(preference_days.difference(day_ids)))
    if unknown_preference_days:
        failures.append(
            _invalid_failure(
                problem,
                "INVALID_REFERENCE",
                "Schedule preferences refer to unknown days.",
                day_ids=unknown_preference_days,
            )
        )
    if problem.preferences.evidence_policy is not EvidencePolicy.VERIFIED_ONLY:
        failures.append(
            _invalid_failure(
                problem,
                "UNSUPPORTED_EVIDENCE_POLICY",
                (
                    "schedule-problem/v2 currently supports only "
                    "verified_only; draft evidence remains fail-closed."
                ),
            )
        )
    failures.extend(_constraint_input_failures(problem))
    return tuple(
        sorted(
            _deduplicate_failures(failures),
            key=lambda failure: (
                failure.kind.value,
                failure.code,
                failure.constraint_ids,
                failure.day_ids,
                failure.activity_ids,
                failure.message,
            ),
        )
    )


def materialize_schedule(
    problem: ScheduleProblem,
    assignments: Sequence[ScheduleAssignment],
    promoted_activity_ids: Iterable[str] = (),
) -> TripState:
    """Materialize a complete assignment into a detached immutable state.

    The function preserves every entity and every non-mutable raw placement.
    It never mutates ``problem.state`` and never writes canonical storage.
    """

    failures = validate_schedule_problem(problem)
    if failures:
        first = failures[0]
        raise ScheduleContractError(first.code, first.message)
    if not isinstance(assignments, (tuple, list)):
        raise ScheduleContractError(
            "INVALID_CANDIDATE", "assignments must be a sequence."
        )
    assignment_tuple = tuple(assignments)
    if any(not isinstance(item, ScheduleAssignment) for item in assignment_tuple):
        raise ScheduleContractError(
            "INVALID_CANDIDATE",
            "assignments must contain ScheduleAssignment values.",
        )
    assignment_by_id = {
        assignment.activity_id: assignment for assignment in assignment_tuple
    }
    if len(assignment_by_id) != len(assignment_tuple):
        raise ScheduleContractError(
            "INVALID_CANDIDATE", "assignments contains duplicate activity IDs."
        )

    promoted = frozenset(promoted_activity_ids)
    if any(not isinstance(item, str) or not item for item in promoted):
        raise ScheduleContractError(
            "INVALID_CANDIDATE", "promoted activity IDs must be non-empty strings."
        )
    eligible = frozenset(problem.scope.eligible_candidate_ids)
    if not promoted.issubset(eligible):
        raise ScheduleContractError(
            "INVALID_CANDIDATE",
            "Only explicitly eligible candidates may be promoted.",
        )

    baseline_active = {
        activity.activity_id
        for activity in problem.state.activities
        if activity.decision_state in _ACTIVE_DECISIONS
    }
    expected_active = baseline_active | promoted
    if frozenset(assignment_by_id) != expected_active:
        missing = sorted(expected_active.difference(assignment_by_id))
        extra = sorted(set(assignment_by_id).difference(expected_active))
        raise ScheduleContractError(
            "INVALID_CANDIDATE",
            f"Assignments must cover the complete active schedule; missing={missing}, extra={extra}.",
        )

    day_ids = frozenset(problem.state.day_by_id)
    seen_slots: set[tuple[str, int]] = set()
    for assignment in assignment_tuple:
        if assignment.day_id not in day_ids:
            raise ScheduleContractError(
                "INVALID_CANDIDATE",
                f"Assignment {assignment.activity_id!r} uses an unknown day.",
            )
        slot = (assignment.day_id, assignment.order)
        if slot in seen_slots:
            raise ScheduleContractError(
                "INVALID_CANDIDATE",
                f"Assignments collide at raw slot {slot!r}.",
            )
        seen_slots.add(slot)

    baseline_positions = _position_map(problem.state)
    mutable = frozenset(problem.scope.mutable_activity_ids)
    activity_by_id = problem.state.activity_by_id
    for activity_id in sorted(expected_active.difference(mutable)):
        assignment = assignment_by_id[activity_id]
        activity = activity_by_id[activity_id]
        base_day, base_order = baseline_positions[activity_id]
        if (
            assignment.day_id != base_day
            or assignment.order != base_order
            or assignment.scheduled_start != activity.scheduled_start
        ):
            raise ScheduleContractError(
                "FROZEN_ACTIVITY_CHANGED",
                f"Frozen activity {activity_id!r} changed placement or time.",
            )
    for activity_id in sorted(expected_active.intersection(mutable)):
        activity = activity_by_id[activity_id]
        assignment = assignment_by_id[activity_id]
        if assignment.day_id not in problem.scope.day_ids:
            raise ScheduleContractError(
                "SCOPE_VIOLATION",
                (
                    f"Mutable activity {activity_id!r} cannot move outside "
                    "ReplanScope.day_ids."
                ),
            )
        if (
            activity.flexibility is Flexibility.FIXED_DAY
            and (
                assignment.day_id != activity.day_id
                or assignment.order != baseline_positions[activity_id][1]
                or assignment.scheduled_start != activity.scheduled_start
            )
        ):
            raise ScheduleContractError(
                "FIXED_DAY_CHANGED",
                (
                    f"Fixed-day activity {activity_id!r} may only be "
                    "promoted in place without changing its persisted time."
                ),
            )

    target_membership: dict[str, list[str]] = {
        day.day_id: [] for day in problem.state.days
    }
    for activity in problem.state.activities:
        assignment = assignment_by_id.get(activity.activity_id)
        target_day = (
            assignment.day_id if assignment is not None else activity.day_id
        )
        target_membership[target_day].append(activity.activity_id)

    final_orders: dict[str, tuple[str, ...]] = {}
    for day in problem.state.days:
        members = target_membership[day.day_id]
        slots: list[str | None] = [None] * len(members)

        def occupy(index: int, activity_id: str) -> None:
            if index >= len(slots):
                raise ScheduleContractError(
                    "INVALID_CANDIDATE",
                    (
                        f"Activity {activity_id!r} targets raw order {index}, "
                        f"but day {day.day_id!r} has {len(slots)} activities."
                    ),
                )
            if slots[index] is not None:
                raise ScheduleContractError(
                    "INVALID_CANDIDATE",
                    (
                        f"Activity {activity_id!r} collides with "
                        f"{slots[index]!r} on day {day.day_id!r}."
                    ),
                )
            slots[index] = activity_id

        flexible_inactive: list[str] = []
        for activity_id in members:
            assignment = assignment_by_id.get(activity_id)
            if assignment is not None:
                occupy(assignment.order, activity_id)
                continue
            base_day, base_order = baseline_positions[activity_id]
            if activity_id not in mutable:
                if base_day != day.day_id:
                    raise ScheduleContractError(
                        "FROZEN_ACTIVITY_CHANGED",
                        f"Frozen activity {activity_id!r} moved days.",
                    )
                occupy(base_order, activity_id)
            else:
                flexible_inactive.append(activity_id)

        open_slots = [index for index, value in enumerate(slots) if value is None]
        flexible_inactive.sort(
            key=lambda activity_id: baseline_positions[activity_id]
        )
        if len(open_slots) != len(flexible_inactive):
            raise ScheduleContractError(
                "INVALID_CANDIDATE",
                f"Day {day.day_id!r} assignment does not form a complete raw order.",
            )
        for index, activity_id in zip(open_slots, flexible_inactive):
            slots[index] = activity_id
        if any(value is None for value in slots):
            raise ScheduleContractError(
                "INVALID_CANDIDATE",
                f"Day {day.day_id!r} assignment contains raw-order gaps.",
            )
        final_orders[day.day_id] = tuple(
            value for value in slots if value is not None
        )

    final_position: dict[str, tuple[str, int]] = {
        activity_id: (day_id, index)
        for day_id, order in final_orders.items()
        for index, activity_id in enumerate(order)
    }
    directly_movable = expected_active.intersection(mutable)
    stationary = frozenset(activity_by_id).difference(directly_movable)
    for day in problem.state.days:
        baseline_stationary = tuple(
            activity_id
            for activity_id in day.activity_ids
            if activity_id in stationary
        )
        final_stationary = tuple(
            activity_id
            for activity_id in final_orders[day.day_id]
            if activity_id in stationary
        )
        if baseline_stationary != final_stationary:
            raise ScheduleContractError(
                "UNSTAGEABLE_ANCHOR_REORDER",
                (
                    f"Assignments indirectly reorder stationary activities "
                    f"on day {day.day_id!r}; no V1 patch can reproduce that "
                    "layout without additional authority."
                ),
            )
    final_activities: list[Activity] = []
    for activity in problem.state.activities:
        activity_id = activity.activity_id
        day_id, order = final_position[activity_id]
        assignment = assignment_by_id.get(activity_id)
        decision = (
            DecisionState.SELECTED
            if activity_id in promoted
            else activity.decision_state
        )
        scheduled_start = (
            assignment.scheduled_start
            if assignment is not None and activity_id in mutable
            else activity.scheduled_start
        )
        final_activities.append(
            replace(
                activity,
                day_id=day_id,
                order=order,
                scheduled_start=scheduled_start,
                decision_state=decision,
            )
        )
    final_days = tuple(
        replace(day, activity_ids=final_orders[day.day_id])
        for day in problem.state.days
    )
    return replace(
        problem.state,
        days=final_days,
        activities=tuple(
            sorted(final_activities, key=lambda item: item.activity_id)
        ),
        travel_estimates=tuple(
            sorted(
                problem.state.travel_estimates,
                key=lambda item: _canonical_json(_stable_value(item)),
            )
        ),
        constraints=tuple(
            sorted(
                problem.state.constraints,
                key=lambda item: item.constraint_id,
            )
        ),
        load_issues=tuple(
            sorted(
                problem.state.load_issues,
                key=lambda item: _canonical_json(_stable_value(item)),
            )
        ),
    )


def assignments_from_state(
    problem: ScheduleProblem,
    state: TripState,
    report: CheckReport,
) -> tuple[ScheduleAssignment, ...]:
    """Return the complete active assignment in deterministic day/raw order."""

    entry_by_id = {entry.activity_id: entry for entry in report.timeline}
    activity_by_id = state.activity_by_id
    mutable = frozenset(problem.scope.mutable_activity_ids)
    day_rank = {
        day.day_id: index
        for index, day in enumerate(
            sorted(state.days, key=lambda item: (item.date, item.day_id))
        )
    }
    assignments: list[ScheduleAssignment] = []
    for day in state.days:
        for raw_order, activity_id in enumerate(day.activity_ids):
            activity = activity_by_id[activity_id]
            if activity.decision_state not in _ACTIVE_DECISIONS:
                continue
            scheduled_start = activity.scheduled_start
            if (
                activity_id in mutable
                and activity.flexibility is not Flexibility.FIXED_DAY
                and activity_id in entry_by_id
            ):
                scheduled_start = (
                    entry_by_id[activity_id]
                    .start_at.timetz()
                    .replace(tzinfo=None)
                )
            assignments.append(
                ScheduleAssignment(
                    activity_id=activity_id,
                    day_id=day.day_id,
                    order=raw_order,
                    scheduled_start=scheduled_start,
                )
            )
    assignments.sort(
        key=lambda item: (
            day_rank[item.day_id],
            item.order,
            item.activity_id,
        )
    )
    return tuple(assignments)


def score_schedule(
    problem: ScheduleProblem,
    candidate_state: TripState,
    report: CheckReport,
    *,
    promoted_activity_ids: Iterable[str] = (),
) -> ScheduleScore:
    """Compute the one common lexicographic score used by every solver."""

    promoted = frozenset(promoted_activity_ids)
    base = problem.state
    base_positions = _position_map(base)
    candidate_positions = _position_map(candidate_state)
    comparable_base_positions = _position_map_excluding(base, promoted)
    comparable_candidate_positions = _position_map_excluding(
        candidate_state, promoted
    )
    candidate_activity = candidate_state.activity_by_id
    active_ids = {
        activity.activity_id
        for activity in candidate_state.activities
        if activity.decision_state in _ACTIVE_DECISIONS
    }

    hard_violation_count = sum(
        issue.severity is IssueSeverity.ERROR
        and issue.code not in _COVERAGE_ISSUE_CODES
        for issue in report.issues
    )
    missing_required_count = _missing_required_count(
        candidate_state, frozenset(active_ids)
    )

    protected_ids: set[str] = set()
    accepted_changed: set[str] = set()
    accepted_day_moves = 0
    accepted_time_shift = 0
    changed_ids: set[str] = set(promoted)

    for activity in base.activities:
        activity_id = activity.activity_id
        candidate = candidate_activity[activity_id]
        placement_changed = (
            base_positions[activity_id] != candidate_positions[activity_id]
        )
        time_changed = candidate.scheduled_start != activity.scheduled_start
        decision_changed = candidate.decision_state is not activity.decision_state
        fully_protected = (
            activity.decision_state
            in {DecisionState.FIXED, DecisionState.BOOKED}
            or activity.flexibility is Flexibility.FIXED_TIME
        )
        fixed_day_change = (
            activity.flexibility is Flexibility.FIXED_DAY
            and (placement_changed or time_changed)
        )
        if (
            fully_protected
            and (placement_changed or time_changed or decision_changed)
        ) or fixed_day_change:
            protected_ids.add(activity_id)
            changed_ids.add(activity_id)

    accepted_for_order = [
        activity.activity_id
        for activity in base.activities
        if activity.decision_state in _ACTIVE_DECISIONS
        and activity.decision_state
        not in {DecisionState.FIXED, DecisionState.BOOKED}
        and activity.flexibility
        not in {Flexibility.FIXED_DAY, Flexibility.FIXED_TIME}
    ]
    for activity_id in accepted_for_order:
        activity = base.activity_by_id[activity_id]
        candidate = candidate_activity[activity_id]
        day_changed = (
            comparable_base_positions[activity_id][0]
            != comparable_candidate_positions[activity_id][0]
        )
        placement_changed = (
            comparable_base_positions[activity_id]
            != comparable_candidate_positions[activity_id]
        )
        time_changed = candidate.scheduled_start != activity.scheduled_start
        decision_changed = candidate.decision_state is not activity.decision_state
        if day_changed or placement_changed or time_changed or decision_changed:
            accepted_changed.add(activity_id)
            changed_ids.add(activity_id)
        if day_changed:
            accepted_day_moves += 1
        if (
            activity.scheduled_start is not None
            and candidate.scheduled_start is not None
        ):
            base_day = base.day_by_id[base_positions[activity_id][0]]
            candidate_day = candidate_state.day_by_id[
                candidate_positions[activity_id][0]
            ]
            accepted_time_shift += _planning_time_shift_deci(
                activity.scheduled_start,
                base_day,
                candidate.scheduled_start,
                candidate_day,
                default_timezone=base.timezone,
            )

    accepted_order_inversions = 0
    for index, left_id in enumerate(accepted_for_order):
        for right_id in accepted_for_order[index + 1 :]:
            base_left = base_positions[left_id]
            base_right = base_positions[right_id]
            candidate_left = candidate_positions[left_id]
            candidate_right = candidate_positions[right_id]
            if (
                base_left[0] == base_right[0]
                and candidate_left[0] == candidate_right[0]
                and (base_left[1] < base_right[1])
                != (candidate_left[1] < candidate_right[1])
            ):
                accepted_order_inversions += 1

    soft_constraint_violations = sum(
        issue.severity is IssueSeverity.WARNING
        and dict(issue.details).get("strength") == ConstraintStrength.SOFT.value
        for issue in report.issues
    )
    verification_risk = sum(
        issue.severity is IssueSeverity.WARNING
        and dict(issue.details).get("status_effect") != "none"
        for issue in report.issues
    )

    minimum_slack = Decimal(str(problem.preferences.minimum_end_slack_min))
    tight_slack_count = 0
    slack_deficit = Decimal("0")
    max_activities = dict(problem.preferences.max_activities_by_day)
    max_service = dict(problem.preferences.max_service_min_by_day)
    activity_count_overage = 0
    service_overage = Decimal("0")
    for summary in report.day_summaries:
        if summary.end_slack_min is not None:
            observed = Decimal(str(summary.end_slack_min))
            if observed < minimum_slack:
                tight_slack_count += 1
                slack_deficit += minimum_slack - observed
        activity_limit = max_activities.get(summary.day_id)
        if (
            activity_limit is not None
            and summary.activity_count > activity_limit
        ):
            activity_count_overage += summary.activity_count - activity_limit
        service_limit = max_service.get(summary.day_id)
        if (
            service_limit is not None
            and Decimal(str(summary.service_min))
            > Decimal(str(service_limit))
        ):
            service_overage += (
                Decimal(str(summary.service_min))
                - Decimal(str(service_limit))
            )

    metrics = dict(report.metrics)
    served_priority = sum(
        candidate_activity[activity_id].priority for activity_id in active_ids
    )
    return ScheduleScore(
        hard_violation_count=int(hard_violation_count),
        missing_required_count=missing_required_count,
        protected_change_count=len(protected_ids),
        accepted_activity_change_count=len(accepted_changed),
        accepted_day_move_count=accepted_day_moves,
        accepted_order_inversion_count=accepted_order_inversions,
        accepted_time_shift_deci_min=accepted_time_shift,
        served_priority_points=served_priority,
        soft_constraint_violation_count=int(soft_constraint_violations),
        verification_risk_count=int(verification_risk),
        tight_slack_count=tight_slack_count,
        slack_deficit_deci_min=_deci(slack_deficit),
        activity_count_overage=activity_count_overage,
        service_overage_deci_min=_deci(service_overage),
        wait_deci_min=_deci(metrics.get("wait_min", 0.0)),
        travel_deci_min=_deci(metrics.get("travel_min", 0.0)),
        buffer_deci_min=_deci(metrics.get("buffer_min", 0.0)),
        service_deci_min=_deci(metrics.get("service_min", 0.0)),
        changed_activity_ids=tuple(sorted(changed_ids)),
        protected_activity_ids=tuple(sorted(protected_ids)),
        scheduled_optional_ids=tuple(sorted(promoted)),
    )


def build_schedule_candidate(
    problem: ScheduleProblem,
    assignments: Sequence[ScheduleAssignment],
    *,
    promoted_activity_ids: Iterable[str] = (),
    solver: str,
) -> ScheduleCandidate:
    """Replay and build a commit-stageable, kernel-feasible candidate."""

    promoted = tuple(sorted(set(promoted_activity_ids)))
    provisional_state = materialize_schedule(
        problem, assignments, promoted
    )
    provisional_report = evaluate_timeline(
        provisional_state, now=problem.evaluation_at
    )
    normalized_assignments = assignments_from_state(
        problem, provisional_state, provisional_report
    )
    state = materialize_schedule(
        problem, normalized_assignments, promoted
    )
    report = evaluate_timeline(state, now=problem.evaluation_at)
    fixed_point_assignments = assignments_from_state(
        problem, state, report
    )
    if fixed_point_assignments != normalized_assignments:
        raise ScheduleContractError(
            "NON_IDEMPOTENT_SCHEDULE_NORMALIZATION",
            (
                "Schedule assignment normalization did not reach a fixed "
                "point after trusted replay."
            ),
        )
    score = score_schedule(
        problem,
        state,
        report,
        promoted_activity_ids=promoted,
    )
    if report.status is not CheckStatus.FEASIBLE:
        raise ScheduleContractError(
            "CANDIDATE_NOT_READY",
            (
                "A ScheduleCandidate must be kernel-feasible; non-ready "
                "attempts belong in a typed failure."
            ),
        )
    if score.protected_change_count:
        raise ScheduleContractError(
            "PROTECTED_CHANGE_REQUIRED",
            "A stageable schedule candidate cannot contain protected changes.",
        )
    if (
        score.accepted_activity_change_count
        > problem.scope.max_accepted_changes
    ):
        raise ScheduleContractError(
            "CHANGE_BUDGET_EXCEEDED",
            (
                "Schedule candidate exceeds ReplanScope.max_accepted_changes."
            ),
        )
    _project_schedule_operations(
        problem,
        state,
        normalized_assignments,
        promoted,
    )
    stable_schedule_key = schedule_key(normalized_assignments, promoted)
    candidate_id = _digest(
        {
            "problem_id": problem.problem_id,
            "assignments": _stable_value(normalized_assignments),
            "promoted_activity_ids": list(promoted),
        },
        prefix="schedule-candidate",
    )
    return ScheduleCandidate(
        candidate_id=candidate_id,
        problem_id=problem.problem_id,
        base_revision=problem.base_revision,
        base_state_digest=problem.base_state_digest,
        assignments=normalized_assignments,
        promoted_activity_ids=promoted,
        required_arc_keys=_required_arc_keys(state),
        report=report,
        score=score,
        schedule_key=stable_schedule_key,
        solver=solver,
    )


def replay_schedule_candidate(
    problem: ScheduleProblem,
    candidate: ScheduleCandidate,
) -> TripState:
    """Fail closed if any candidate-owned report, score, diff, or ID is forged."""

    if type(candidate) is not ScheduleCandidate:
        raise ScheduleContractError(
            "INVALID_CANDIDATE",
            "trusted replay requires an exact ScheduleCandidate value.",
        )
    if any(
        type(item) is not ScheduleAssignment
        for item in candidate.assignments
    ):
        raise ScheduleContractError(
            "INVALID_CANDIDATE",
            "trusted replay requires exact ScheduleAssignment values.",
        )
    if type(candidate.report) is not CheckReport:
        raise ScheduleContractError(
            "INVALID_CANDIDATE",
            "trusted replay requires an exact CheckReport value.",
        )
    if type(candidate.score) is not ScheduleScore:
        raise ScheduleContractError(
            "INVALID_CANDIDATE",
            "trusted replay requires an exact ScheduleScore value.",
        )
    if candidate.problem_id != problem.problem_id:
        raise ScheduleContractError(
            "STALE_CANDIDATE", "Candidate belongs to a different schedule problem."
        )
    if (
        candidate.base_revision != problem.base_revision
        or candidate.base_state_digest != problem.base_state_digest
    ):
        raise ScheduleContractError(
            "STALE_CANDIDATE", "Candidate base state no longer matches."
        )
    rebuilt = build_schedule_candidate(
        problem,
        candidate.assignments,
        promoted_activity_ids=candidate.promoted_activity_ids,
        solver=candidate.solver,
    )
    for observed, expected, name in (
        (candidate.candidate_id, rebuilt.candidate_id, "candidate_id"),
        (candidate.schedule_key, rebuilt.schedule_key, "schedule_key"),
        (candidate.assignments, rebuilt.assignments, "assignments"),
        (candidate.required_arc_keys, rebuilt.required_arc_keys, "required_arc_keys"),
        (candidate.report, rebuilt.report, "report"),
        (candidate.score, rebuilt.score, "score"),
    ):
        if observed != expected:
            raise ScheduleContractError(
                "CANDIDATE_REPLAY_MISMATCH",
                f"Candidate {name} does not match trusted replay.",
            )
    return materialize_schedule(
        problem,
        candidate.assignments,
        candidate.promoted_activity_ids,
    )


def candidate_to_plan_patch(
    problem: ScheduleProblem,
    candidate: ScheduleCandidate,
) -> PlanPatch:
    """Project a replayed candidate to the only V1-authorized patch operations."""

    final_state = replay_schedule_candidate(problem, candidate)
    operations = _project_schedule_operations(
        problem,
        final_state,
        candidate.assignments,
        candidate.promoted_activity_ids,
    )
    if not operations:
        raise ScheduleContractError(
            "EMPTY_SCHEDULE_PATCH",
            "Candidate is identical to the canonical scheduling state.",
        )
    return PlanPatch(
        trip_id=problem.trip_id,
        base_revision=problem.base_revision,
        idempotency_key=f"schedule:{candidate.candidate_id}",
        operations=operations,
        intent=(
            "Apply trusted schedule candidate "
            f"{candidate.candidate_id} for problem {problem.problem_id}."
        ),
        patch_version=PATCH_VERSION,
    )


def _project_schedule_operations(
    problem: ScheduleProblem,
    final_state: TripState,
    assignments: Sequence[ScheduleAssignment],
    promoted_activity_ids: Sequence[str],
) -> tuple[UpdateActivity | PlaceActivity, ...]:
    """Return the exact V1 patch projection or a typed stageability failure."""

    mutable = frozenset(problem.scope.mutable_activity_ids)
    final_positions = _position_map(final_state)
    final_activity = final_state.activity_by_id
    base_activity = problem.state.activity_by_id
    projected_decisions = {
        activity.activity_id: activity.decision_state
        for activity in problem.state.activities
    }
    projected_times = {
        activity.activity_id: activity.scheduled_start
        for activity in problem.state.activities
    }

    operations: list[UpdateActivity | PlaceActivity] = []
    op_number = 1
    for activity_id in promoted_activity_ids:
        operations.append(
            UpdateActivity(
                op_id=f"schedule-op-{op_number:03d}",
                activity_id=activity_id,
                fields={"decision_state": DecisionState.SELECTED.value},
            )
        )
        projected_decisions[activity_id] = DecisionState.SELECTED
        op_number += 1

    current_orders: dict[str, list[str]] = {
        day.day_id: list(day.activity_ids) for day in problem.state.days
    }
    assignment_by_id = {
        assignment.activity_id: assignment
        for assignment in assignments
    }
    ordered_days = sorted(
        final_state.days, key=lambda item: (item.date, item.day_id)
    )
    for day in ordered_days:
        desired = list(day.activity_ids)
        for index in range(len(desired) - 1, -1, -1):
            activity_id = desired[index]
            if activity_id not in mutable or activity_id not in assignment_by_id:
                continue
            source_day_id = next(
                current_day_id
                for current_day_id, values in current_orders.items()
                if activity_id in values
            )
            source_index = current_orders[source_day_id].index(activity_id)
            assignment = assignment_by_id[activity_id]
            base = base_activity[activity_id]
            placement_changed = (
                source_day_id != day.day_id
                or source_index != index
            )
            time_changed = assignment.scheduled_start != base.scheduled_start
            if not placement_changed and not time_changed:
                continue

            next_anchor = desired[index + 1] if index + 1 < len(desired) else None
            position = Placement.BEFORE if next_anchor is not None else Placement.END
            scheduled_start = (
                assignment.scheduled_start.isoformat()
                if assignment.scheduled_start is not None
                else None
            )
            operations.append(
                PlaceActivity(
                    op_id=f"schedule-op-{op_number:03d}",
                    activity_id=activity_id,
                    day_id=day.day_id,
                    position=position,
                    anchor_activity_id=next_anchor,
                    scheduled_start=scheduled_start,
                )
            )
            projected_times[activity_id] = assignment.scheduled_start
            op_number += 1

            current_orders[source_day_id].pop(source_index)
            target = current_orders[day.day_id]
            if next_anchor is None:
                target.append(activity_id)
            else:
                target.insert(target.index(next_anchor), activity_id)

    if len(operations) > PLAN_PATCH_MAX_OPERATIONS:
        raise ScheduleContractError(
            "PATCH_OPERATION_LIMIT_EXCEEDED",
            (
                "Schedule candidate needs "
                f"{len(operations)} patch operations; the atomic plan-patch "
                f"limit is {PLAN_PATCH_MAX_OPERATIONS}."
            ),
        )
    if any(
        final_positions[activity_id] != _position_in_orders(current_orders, activity_id)
        for activity_id in final_positions
    ):
        raise ScheduleContractError(
            "PATCH_PROJECTION_MISMATCH",
            "Projected placement operations do not reproduce the candidate order.",
        )
    if any(
        projected_decisions[activity_id]
        is not final_activity[activity_id].decision_state
        or projected_times[activity_id]
        != final_activity[activity_id].scheduled_start
        for activity_id in final_activity
    ):
        raise ScheduleContractError(
            "PATCH_PROJECTION_MISMATCH",
            "Projected operations do not reproduce candidate decision/time state.",
        )
    return tuple(operations)


def _position_map(state: TripState) -> dict[str, tuple[str, int]]:
    return {
        activity_id: (day.day_id, index)
        for day in state.days
        for index, activity_id in enumerate(day.activity_ids)
    }


def _position_map_excluding(
    state: TripState, excluded_activity_ids: frozenset[str]
) -> dict[str, tuple[str, int]]:
    """Return raw placement after removing newly promoted activities.

    A candidate insertion must not make every following accepted activity look
    changed.  Existing inactive, frozen, and out-of-scope activities remain in
    the comparison so moving an accepted activity around one of those anchors
    is still an explicit, budgeted canonical change.
    """

    result: dict[str, tuple[str, int]] = {}
    for day in state.days:
        order = 0
        for activity_id in day.activity_ids:
            if activity_id in excluded_activity_ids:
                continue
            result[activity_id] = (day.day_id, order)
            order += 1
    return result


def _position_in_orders(
    orders: Mapping[str, Sequence[str]], activity_id: str
) -> tuple[str, int]:
    for day_id, values in orders.items():
        if activity_id in values:
            return day_id, values.index(activity_id)
    raise ScheduleContractError(
        "PATCH_PROJECTION_MISMATCH",
        f"Projected orders lost activity {activity_id!r}.",
    )


def _missing_required_count(
    state: TripState, active_ids: frozenset[str]
) -> int:
    missing = 0
    for constraint in state.constraints:
        if constraint.strength is not ConstraintStrength.HARD:
            continue
        count = sum(
            subject_id in active_ids for subject_id in constraint.subject_ids
        )
        if constraint.kind is ConstraintKind.MUST_INCLUDE:
            missing += sum(
                subject_id not in active_ids
                for subject_id in constraint.subject_ids
            )
        elif constraint.kind is ConstraintKind.EXACTLY_ONCE:
            missing += abs(1 - count)
        elif constraint.kind is ConstraintKind.CHOOSE_N:
            expected = constraint.param("n")
            if (
                isinstance(expected, int)
                and not isinstance(expected, bool)
                and expected >= 0
            ):
                missing += abs(expected - count)
        elif constraint.kind is ConstraintKind.REQUIRES:
            if constraint.subject_ids:
                trigger, *dependencies = constraint.subject_ids
                if trigger in active_ids:
                    missing += sum(
                        dependency not in active_ids
                        for dependency in dependencies
                    )
        elif constraint.kind is ConstraintKind.BEFORE:
            missing += sum(
                subject_id not in active_ids
                for subject_id in constraint.subject_ids
            )
    return missing


def _required_arc_keys(state: TripState) -> tuple[str, ...]:
    activity_by_id = state.activity_by_id
    keys: set[str] = set()
    for day in state.days:
        active = [
            activity_by_id[activity_id]
            for activity_id in day.activity_ids
            if activity_by_id[activity_id].decision_state in _ACTIVE_DECISIONS
        ]
        locations = [activity.location_id for activity in active]
        if day.start_location_id is not None:
            locations.insert(0, day.start_location_id)
        if day.end_location_id is not None:
            locations.append(day.end_location_id)
        for from_location, to_location in zip(locations, locations[1:]):
            if from_location == to_location:
                continue
            keys.add(
                _canonical_json(
                    {
                        "day_id": day.day_id,
                        "from_location_id": from_location,
                        "to_location_id": to_location,
                    }
                )
            )
    return tuple(sorted(keys))


def schedule_key(
    assignments: Sequence[ScheduleAssignment],
    promoted_activity_ids: Sequence[str],
) -> str:
    """Return the shared canonical structural tie-break for all solvers."""

    return _canonical_json(
        {
            "assignments": _stable_value(tuple(assignments)),
            "promoted_activity_ids": list(promoted_activity_ids),
        }
    )


def _planning_time_shift_deci(
    base_time: time,
    base_day: DaySpec,
    candidate_time: time,
    candidate_day: DaySpec,
    *,
    default_timezone: str,
) -> int:
    """Measure persisted start movement as actual planning instants."""

    base_at = _planning_instant(
        base_time, base_day, default_timezone
    )
    candidate_at = _planning_instant(
        candidate_time, candidate_day, default_timezone
    )
    minutes = (
        Decimal(str(abs((candidate_at - base_at).total_seconds())))
        / Decimal(60)
    )
    return _deci(minutes)


def _planning_instant(
    value: time, day: DaySpec, default_timezone: str
) -> datetime:
    planning_date = day.date
    if (
        day.available_start is not None
        and day.available_end is not None
        and day.available_end <= day.available_start
        and value < day.available_start
    ):
        planning_date += timedelta(days=1)
    naive = datetime.combine(planning_date, value)
    try:
        zone = ZoneInfo(day.timezone or default_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return naive
    return naive.replace(tzinfo=zone).astimezone(timezone.utc).replace(
        tzinfo=None
    )


def _constraint_input_failures(
    problem: ScheduleProblem,
) -> tuple[ScheduleFailure, ...]:
    """Validate structural constraint semantics before search begins."""

    activity_ids = frozenset(problem.state.activity_by_id)
    day_ids = frozenset(problem.state.day_by_id)
    activity_subject_kinds = {
        ConstraintKind.MUST_INCLUDE,
        ConstraintKind.EXACTLY_ONCE,
        ConstraintKind.AT_MOST_ONCE,
        ConstraintKind.BEFORE,
        ConstraintKind.REQUIRES,
        ConstraintKind.CHOOSE_N,
        ConstraintKind.ALLOWED_DAY,
        ConstraintKind.FIXED_TIME,
        ConstraintKind.ALLOWED_WINDOW,
        ConstraintKind.ALLOWED_MODE,
    }
    failures: list[ScheduleFailure] = []

    def reject(
        constraint_id: str,
        message: str,
        *,
        subjects: tuple[str, ...] = (),
    ) -> None:
        failures.append(
            _invalid_failure(
                problem,
                "INVALID_CONSTRAINT",
                message,
                activity_ids=subjects,
                constraint_ids=(constraint_id,),
            )
        )

    for constraint in sorted(
        problem.state.constraints,
        key=lambda item: item.constraint_id,
    ):
        kind = constraint.kind
        if kind in activity_subject_kinds:
            if not constraint.subject_ids:
                reject(
                    constraint.constraint_id,
                    f"{kind.value} needs activity subjects.",
                )
                continue
            unknown = tuple(
                subject_id
                for subject_id in constraint.subject_ids
                if subject_id not in activity_ids
            )
            if unknown:
                reject(
                    constraint.constraint_id,
                    (
                        f"{kind.value} refers to unknown activities: "
                        f"{', '.join(unknown)}."
                    ),
                    subjects=unknown,
                )
                continue

        if kind is ConstraintKind.BEFORE and len(constraint.subject_ids) != 2:
            reject(
                constraint.constraint_id,
                "before needs exactly two activity subjects.",
                subjects=constraint.subject_ids,
            )
        elif (
            kind is ConstraintKind.REQUIRES
            and len(constraint.subject_ids) < 2
        ):
            reject(
                constraint.constraint_id,
                "requires needs a trigger and at least one dependency.",
                subjects=constraint.subject_ids,
            )
        elif kind is ConstraintKind.CHOOSE_N:
            expected = constraint.param("n")
            if (
                isinstance(expected, bool)
                or not isinstance(expected, int)
                or expected < 0
                or expected > len(constraint.subject_ids)
            ):
                reject(
                    constraint.constraint_id,
                    (
                        "choose_n needs a non-negative integer n no larger "
                        "than its subject count."
                    ),
                    subjects=constraint.subject_ids,
                )
        elif kind is ConstraintKind.ALLOWED_DAY:
            allowed = _csv_values(
                constraint.param("day_ids", constraint.param("day_id"))
            )
            unknown_days = tuple(sorted(allowed.difference(day_ids)))
            if not allowed or unknown_days:
                reject(
                    constraint.constraint_id,
                    (
                        "allowed_day needs known day_id/day_ids"
                        + (
                            f"; unknown: {', '.join(unknown_days)}"
                            if unknown_days
                            else ""
                        )
                        + "."
                    ),
                    subjects=constraint.subject_ids,
                )
        elif kind is ConstraintKind.FIXED_TIME:
            if not _is_local_time_text(
                constraint.param("time", constraint.param("start"))
            ):
                reject(
                    constraint.constraint_id,
                    "fixed_time needs a timezone-naive ISO local time.",
                    subjects=constraint.subject_ids,
                )
        elif kind is ConstraintKind.ALLOWED_WINDOW:
            if not _is_local_time_text(
                constraint.param("start")
            ) or not _is_local_time_text(constraint.param("end")):
                reject(
                    constraint.constraint_id,
                    "allowed_window needs timezone-naive ISO start and end.",
                    subjects=constraint.subject_ids,
                )
        elif kind is ConstraintKind.ALLOWED_MODE:
            if not _csv_values(
                constraint.param("modes", constraint.param("mode"))
            ):
                reject(
                    constraint.constraint_id,
                    "allowed_mode needs mode or modes.",
                    subjects=constraint.subject_ids,
                )
        elif kind is ConstraintKind.DAILY_LIMIT:
            maximum_activities = constraint.param("max_activities")
            maximum_minutes = constraint.param("max_minutes")
            valid_activities = maximum_activities is None or (
                isinstance(maximum_activities, int)
                and not isinstance(maximum_activities, bool)
                and maximum_activities >= 0
            )
            valid_minutes = maximum_minutes is None or (
                isinstance(maximum_minutes, (int, float))
                and not isinstance(maximum_minutes, bool)
                and Decimal(str(maximum_minutes)).is_finite()
                and Decimal(str(maximum_minutes)) >= 0
            )
            unknown_days = tuple(
                subject_id
                for subject_id in constraint.subject_ids
                if subject_id not in day_ids
            )
            if (
                maximum_activities is None
                and maximum_minutes is None
            ) or not valid_activities or not valid_minutes or unknown_days:
                reject(
                    constraint.constraint_id,
                    (
                        "daily_limit needs valid non-negative limits and "
                        "known day subjects."
                    ),
                    subjects=constraint.subject_ids,
                )
        elif kind is ConstraintKind.LOCATION_CONTINUITY:
            unknown_days = tuple(
                subject_id
                for subject_id in constraint.subject_ids
                if subject_id not in day_ids
            )
            if unknown_days:
                reject(
                    constraint.constraint_id,
                    "location_continuity refers to unknown days.",
                    subjects=unknown_days,
                )

    return tuple(failures)


def _csv_values(value: object) -> frozenset[str]:
    if not isinstance(value, str):
        return frozenset()
    return frozenset(
        part.strip() for part in value.split(",") if part.strip()
    )


def _is_local_time_text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = time.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is None


def _invalid_failure(
    problem: ScheduleProblem,
    code: str,
    message: str,
    *,
    activity_ids: tuple[str, ...] = (),
    day_ids: tuple[str, ...] = (),
    constraint_ids: tuple[str, ...] = (),
) -> ScheduleFailure:
    return ScheduleFailure(
        kind=ScheduleStatus.INVALID_INPUT,
        code=code,
        problem_id=problem.problem_id,
        message=message,
        activity_ids=activity_ids,
        day_ids=day_ids,
        constraint_ids=constraint_ids,
        evaluation_limit=problem.limits.max_evaluations,
    )


def _deduplicate_failures(
    failures: Iterable[ScheduleFailure],
) -> list[ScheduleFailure]:
    seen: set[tuple[Any, ...]] = set()
    result: list[ScheduleFailure] = []
    for failure in failures:
        key = (
            failure.kind,
            failure.code,
            failure.activity_ids,
            failure.day_ids,
            failure.constraint_ids,
            failure.message,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(failure)
    return result


def _trip_state_payload(state: TripState) -> dict[str, Any]:
    return {
        "slug": state.slug,
        "title": state.title,
        "timezone": state.timezone,
        "days": [
            _stable_value(day)
            for day in sorted(state.days, key=lambda item: (item.date, item.day_id))
        ],
        "activities": [
            _stable_value(activity)
            for activity in sorted(
                state.activities, key=lambda item: item.activity_id
            )
        ],
        "travel_estimates": sorted(
            (_stable_value(item) for item in state.travel_estimates),
            key=_canonical_json,
        ),
        "constraints": [
            _stable_value(constraint)
            for constraint in sorted(
                state.constraints, key=lambda item: item.constraint_id
            )
        ],
        "load_issues": sorted(
            (_stable_value(issue) for issue in state.load_issues),
            key=_canonical_json,
        ),
        "schema_version": state.schema_version,
        "start_date": _stable_value(state.start_date),
        "end_date": _stable_value(state.end_date),
        "subtitle": state.subtitle,
        "cities": list(state.cities),
    }


def _stable_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return {"$type": "float", "value": str(Decimal(str(value)))}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _utc_iso(value) if value.tzinfo is not None else value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if is_dataclass(value):
        return {
            item.name: _stable_value(getattr(value, item.name))
            for item in fields(value)
            if item.name not in {"base_state_digest", "problem_id"}
        }
    if isinstance(value, Mapping):
        return {
            str(key): _stable_value(value[key])
            for key in sorted(value, key=str)
        }
    if isinstance(value, (tuple, list)):
        return [_stable_value(item) for item in value]
    raise TypeError(f"Unsupported stable scheduling value {type(value).__name__}")


def _digest(value: Any, *, prefix: str) -> str:
    material = f"{prefix}\n{_canonical_json(value)}".encode("utf-8")
    return f"sha256:{hashlib.sha256(material).hexdigest()}"


def _is_sha256_digest(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    payload = value.removeprefix("sha256:")
    return (
        len(payload) == 64
        and all(character in "0123456789abcdef" for character in payload)
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _require_id(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ScheduleContractError(
            "INVALID_INPUT", f"{name} must be a non-empty trimmed string."
        )


def _require_patch_identity(value: Any, name: str) -> None:
    _require_id(value, name)
    if len(value) > 256 or any(
        unicodedata.category(character).startswith("C")
        for character in value
    ):
        raise ScheduleContractError(
            "INVALID_INPUT",
            (
                f"{name} must be at most 256 visible characters without "
                "control characters."
            ),
        )


def _require_id_tuple(value: Any, name: str) -> None:
    if not isinstance(value, tuple):
        raise ScheduleContractError("INVALID_INPUT", f"{name} must be a tuple.")
    for item in value:
        _require_id(item, f"{name} item")
    if len(set(value)) != len(value):
        raise ScheduleContractError(
            "INVALID_INPUT", f"{name} must not contain duplicates."
        )


def _require_non_negative_number(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScheduleContractError(
            "INVALID_INPUT", f"{name} must be numeric."
        )
    try:
        decimal_value = Decimal(str(value))
    except Exception as exc:
        raise ScheduleContractError(
            "INVALID_INPUT", f"{name} must be a finite number."
        ) from exc
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ScheduleContractError(
            "INVALID_INPUT", f"{name} must be finite and non-negative."
        )


def _normalize_day_limits(
    value: Any,
    name: str,
    *,
    integer: bool,
) -> tuple[tuple[str, int | float], ...]:
    if not isinstance(value, tuple):
        raise ScheduleContractError("INVALID_INPUT", f"{name} must be a tuple.")
    result: list[tuple[str, int | float]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ScheduleContractError(
                "INVALID_INPUT", f"{name} items must be (day_id, limit) pairs."
            )
        day_id, limit = item
        _require_id(day_id, f"{name} day_id")
        if day_id in seen:
            raise ScheduleContractError(
                "INVALID_INPUT", f"{name} contains duplicate day {day_id!r}."
            )
        seen.add(day_id)
        if integer:
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or limit < 0
            ):
                raise ScheduleContractError(
                    "INVALID_INPUT",
                    f"{name} limits must be non-negative integers.",
                )
        else:
            _require_non_negative_number(limit, f"{name} limit")
        result.append((day_id, limit))
    return tuple(sorted(result, key=lambda item: item[0]))


def _deci(value: int | float) -> int:
    return int(
        (Decimal(str(value)) * Decimal("10")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


__all__ = [
    "SCHEDULE_CANDIDATE_VERSION",
    "SCHEDULE_PROBLEM_VERSION",
    "EvidencePolicy",
    "ReplanScope",
    "ScheduleAssignment",
    "ScheduleCandidate",
    "ScheduleContractError",
    "ScheduleFailure",
    "SchedulePreferences",
    "ScheduleProblem",
    "ScheduleResult",
    "ScheduleScore",
    "ScheduleStatus",
    "SearchLimits",
    "assignments_from_state",
    "build_schedule_candidate",
    "candidate_to_plan_patch",
    "default_replan_scope",
    "materialize_schedule",
    "replay_schedule_candidate",
    "schedule_key",
    "schedule_problem_from_plan",
    "schedule_problem_from_composed",
    "score_schedule",
    "trip_state_digest",
    "validate_schedule_problem",
]
