"""Deterministic, no-write schedule proposal and score facade for ``tripctl``.

The facade reuses the canonical scheduler's exact problem/candidate digests.
Only opaque refs and aggregate counts cross the public boundary; assignments,
activity IDs, times, places, and private plan content remain process-local.  A
later ``score`` call rebuilds the problem and candidate from the exact source,
then requires the caller's proposal ref to match trusted replay.

The disk-only entry points deliberately load no runtime evidence.  Separate
process-local entry points accept an exact ``EvidenceSnapshot`` object, bind the
snapshot-owned clock into composition and scheduling, and attach the existing
safe readiness projection.  No entry point opens a store or gains apply
authority.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from .canonical_tripctl import (
    CanonicalTripctlError,
    _CanonicalSourceSnapshot,
    _read_source_snapshot,
    _source_is_current,
)
from .codec import PlanCodecError, decode_plan
from .facts import EvidenceSnapshot, FactKey
from .loaders import LoadError
from .lodging import LodgingIntakeAssessment
from .lodging_confirmation import LodgingConfirmationReview
from .scheduler import solve_schedule
from .scheduling import (
    ScheduleCandidate,
    ScheduleContractError,
    ScheduleProblem,
    ScheduleResult,
    ScheduleScore,
    replay_schedule_candidate,
    schedule_problem_from_composed,
    schedule_problem_from_plan,
)
from .tripctl_runtime import (
    CanonicalRuntimeAssessment,
    CanonicalRuntimeError,
    _CanonicalRuntimeInputs,
    _load_canonical_runtime,
    _verify_runtime_source,
)


TRIPCTL_SCHEDULE_VERSION = "tripctl-schedule/v1"
"""Version of the aggregate proposal/score projection."""

_PROBLEM_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SCHEDULE_STATUSES = frozenset(
    {
        "solved",
        "needs_evidence",
        "proven_infeasible",
        "search_exhausted",
        "invalid_input",
        "engine_error",
    }
)


class TripctlScheduleError(ValueError):
    """One redacted proposal/score failure suitable for public mapping."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        if not isinstance(code, str) or _PROBLEM_CODE_RE.fullmatch(code) is None:
            raise ValueError("tripctl schedule error code must be bounded")
        if not isinstance(retryable, bool):
            raise TypeError("tripctl schedule retryable must be bool")
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CanonicalScheduleProposal:
    """Safe aggregate of one exact deterministic solver result."""

    source_digest: str
    plan_revision: str
    evaluation_at: datetime
    schedule_status: str
    problem_ref: str
    proposal_ref: str | None
    candidate_count: int
    evaluations_used: int
    evaluation_limit: int
    optimality: str
    failure_code: str | None = None
    failure_activity_count: int = 0
    failure_day_count: int = 0
    failure_constraint_count: int = 0
    missing_arc_count: int = 0
    kernel_issue_count: int = 0
    runtime: CanonicalRuntimeAssessment | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        _require_digest(self.source_digest, "source_digest")
        _require_plain_digest(self.plan_revision, "plan_revision")
        _require_evaluation_at(self.evaluation_at)
        if self.schedule_status not in _SCHEDULE_STATUSES:
            raise ValueError("schedule_status is unsupported")
        _require_digest(self.problem_ref, "problem_ref")
        if self.proposal_ref is not None:
            _require_digest(self.proposal_ref, "proposal_ref")
        for name in (
            "candidate_count",
            "evaluations_used",
            "evaluation_limit",
            "failure_activity_count",
            "failure_day_count",
            "failure_constraint_count",
            "missing_arc_count",
            "kernel_issue_count",
        ):
            _require_count(getattr(self, name), name)
        if self.candidate_count not in {0, 1}:
            raise ValueError("candidate_count must be zero or one")
        if (self.proposal_ref is None) != (self.candidate_count == 0):
            raise ValueError("proposal_ref and candidate_count disagree")
        if self.failure_code is not None and (
            _PROBLEM_CODE_RE.fullmatch(self.failure_code) is None
        ):
            raise ValueError("failure_code must be a bounded token")
        if not isinstance(self.optimality, str) or not self.optimality:
            raise ValueError("optimality must be non-empty text")
        _require_runtime_binding(
            self.runtime,
            source_digest=self.source_digest,
            plan_revision=self.plan_revision,
            evaluation_at=self.evaluation_at,
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "contract_version": TRIPCTL_SCHEDULE_VERSION,
            "source_digest": self.source_digest,
            "plan_revision": self.plan_revision,
            "evaluation_at": self.evaluation_at.isoformat(),
            "schedule_status": self.schedule_status,
            "problem_ref": self.problem_ref,
            "proposal_ref": self.proposal_ref,
            "candidate_count": self.candidate_count,
            "evaluations_used": self.evaluations_used,
            "evaluation_limit": self.evaluation_limit,
            "optimality": self.optimality,
            "failure_code": self.failure_code,
            "failure_activity_count": self.failure_activity_count,
            "failure_day_count": self.failure_day_count,
            "failure_constraint_count": self.failure_constraint_count,
            "missing_arc_count": self.missing_arc_count,
            "kernel_issue_count": self.kernel_issue_count,
            "runtime_evidence_loaded": False,
            "provisional": True,
        }
        if self.runtime is not None:
            result.update(
                {
                    "runtime_evidence_loaded": True,
                    "provisional": False,
                    "evidence_binding_ref": (
                        self.runtime.evidence_binding_ref
                    ),
                    "runtime_context_ref": (
                        self.runtime.runtime_context_ref
                    ),
                    "readiness_scope": "canonical_base",
                    "readiness": self.runtime.readiness.to_dict(),
                    "apply_authority": False,
                    "canonical_write_performed": False,
                }
            )
        return result


@dataclass(frozen=True, slots=True)
class CanonicalScheduleScore:
    """Safe score projection after exact candidate replay."""

    source_digest: str
    plan_revision: str
    evaluation_at: datetime
    problem_ref: str
    proposal_ref: str
    timeline_status: str
    objective_key: tuple[int, ...]
    hard_violation_count: int
    missing_required_count: int
    verification_risk_count: int
    protected_change_count: int
    accepted_activity_change_count: int
    accepted_day_move_count: int
    accepted_order_inversion_count: int
    accepted_time_shift_deci_min: int
    served_priority_points: int
    scheduled_optional_count: int
    soft_constraint_violation_count: int
    tight_slack_count: int
    slack_deficit_deci_min: int
    activity_count_overage: int
    service_overage_deci_min: int
    wait_deci_min: int
    travel_deci_min: int
    buffer_deci_min: int
    service_deci_min: int
    runtime: CanonicalRuntimeAssessment | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        _require_digest(self.source_digest, "source_digest")
        _require_plain_digest(self.plan_revision, "plan_revision")
        _require_evaluation_at(self.evaluation_at)
        _require_digest(self.problem_ref, "problem_ref")
        _require_digest(self.proposal_ref, "proposal_ref")
        if self.timeline_status not in {
            "feasible",
            "infeasible",
            "needs_verification",
        }:
            raise ValueError("timeline_status is unsupported")
        if not isinstance(self.objective_key, tuple) or any(
            type(item) is not int for item in self.objective_key
        ):
            raise TypeError("objective_key must contain exact integers")
        for name in (
            "hard_violation_count",
            "missing_required_count",
            "verification_risk_count",
            "protected_change_count",
            "accepted_activity_change_count",
            "accepted_day_move_count",
            "accepted_order_inversion_count",
            "accepted_time_shift_deci_min",
            "scheduled_optional_count",
            "soft_constraint_violation_count",
            "tight_slack_count",
            "slack_deficit_deci_min",
            "activity_count_overage",
            "service_overage_deci_min",
            "wait_deci_min",
            "travel_deci_min",
            "buffer_deci_min",
            "service_deci_min",
        ):
            _require_count(getattr(self, name), name)
        if type(self.served_priority_points) is not int:
            raise TypeError("served_priority_points must be an exact integer")
        _require_runtime_binding(
            self.runtime,
            source_digest=self.source_digest,
            plan_revision=self.plan_revision,
            evaluation_at=self.evaluation_at,
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "contract_version": TRIPCTL_SCHEDULE_VERSION,
            "source_digest": self.source_digest,
            "plan_revision": self.plan_revision,
            "evaluation_at": self.evaluation_at.isoformat(),
            "problem_ref": self.problem_ref,
            "proposal_ref": self.proposal_ref,
            "timeline_status": self.timeline_status,
            "objective_key": list(self.objective_key),
            "hard_violation_count": self.hard_violation_count,
            "missing_required_count": self.missing_required_count,
            "verification_risk_count": self.verification_risk_count,
            "protected_change_count": self.protected_change_count,
            "accepted_activity_change_count": (
                self.accepted_activity_change_count
            ),
            "accepted_day_move_count": self.accepted_day_move_count,
            "accepted_order_inversion_count": (
                self.accepted_order_inversion_count
            ),
            "accepted_time_shift_deci_min": (
                self.accepted_time_shift_deci_min
            ),
            "served_priority_points": self.served_priority_points,
            "scheduled_optional_count": self.scheduled_optional_count,
            "soft_constraint_violation_count": (
                self.soft_constraint_violation_count
            ),
            "tight_slack_count": self.tight_slack_count,
            "slack_deficit_deci_min": self.slack_deficit_deci_min,
            "activity_count_overage": self.activity_count_overage,
            "service_overage_deci_min": self.service_overage_deci_min,
            "wait_deci_min": self.wait_deci_min,
            "travel_deci_min": self.travel_deci_min,
            "buffer_deci_min": self.buffer_deci_min,
            "service_deci_min": self.service_deci_min,
            "runtime_evidence_loaded": False,
            "provisional": True,
        }
        if self.runtime is not None:
            result.update(
                {
                    "runtime_evidence_loaded": True,
                    "provisional": False,
                    "evidence_binding_ref": (
                        self.runtime.evidence_binding_ref
                    ),
                    "runtime_context_ref": (
                        self.runtime.runtime_context_ref
                    ),
                    "readiness_scope": "canonical_base",
                    "readiness": self.runtime.readiness.to_dict(),
                    "apply_authority": False,
                    "canonical_write_performed": False,
                }
            )
        return result


def propose_canonical_schedule(
    path: str | Path,
    *,
    evaluation_at: datetime,
) -> CanonicalScheduleProposal:
    """Run one bounded solver pass and return only an opaque proposal ref."""

    normalized_at = _normalized_evaluation_at(evaluation_at)
    snapshot, plan, problem, result = _solve_exact(
        Path(path),
        evaluation_at=normalized_at,
    )
    proposal = _proposal_projection(
        snapshot,
        plan_revision=str(plan["revision"]),
        evaluation_at=normalized_at,
        problem=problem,
        result=result,
    )
    _verify_source(snapshot)
    return proposal


def propose_canonical_schedule_with_evidence(
    path: str | Path,
    *,
    evidence_snapshot: EvidenceSnapshot,
    availability_keys: tuple[FactKey, ...] = (),
    lodging_intake: LodgingIntakeAssessment | None = None,
    pending_lodging_review: LodgingConfirmationReview | None = None,
) -> CanonicalScheduleProposal:
    """Solve one schedule bound to an exact process-local evidence snapshot."""

    inputs, problem, result = _solve_exact_with_evidence(
        Path(path),
        evidence_snapshot=evidence_snapshot,
        availability_keys=availability_keys,
        lodging_intake=lodging_intake,
        pending_lodging_review=pending_lodging_review,
    )
    proposal = _proposal_projection(
        inputs.source,
        plan_revision=str(inputs.plan["revision"]),
        evaluation_at=inputs.composed.evidence.evaluation_at,
        problem=problem,
        result=result,
        runtime=inputs.assessment,
    )
    _verify_evidence_source(inputs)
    return proposal


def score_canonical_schedule(
    path: str | Path,
    *,
    proposal_ref: str,
    evaluation_at: datetime,
) -> CanonicalScheduleScore:
    """Rebuild and replay one exact proposal ref, then expose its safe score."""

    if not isinstance(proposal_ref, str) or _DIGEST_RE.fullmatch(proposal_ref) is None:
        raise TripctlScheduleError("INVALID_PROPOSAL_REF")
    normalized_at = _normalized_evaluation_at(evaluation_at)
    snapshot, plan, problem, result = _solve_exact(
        Path(path),
        evaluation_at=normalized_at,
    )
    candidate = result.candidate
    if candidate is None or candidate.candidate_id != proposal_ref:
        _verify_source(snapshot)
        raise TripctlScheduleError("STALE_PROPOSAL_REF")
    try:
        replay_schedule_candidate(problem, candidate)
    except ScheduleContractError:
        _raise_after_source_check(snapshot, "PROPOSAL_REPLAY_UNAVAILABLE")
    score = _score_projection(
        snapshot,
        plan_revision=str(plan["revision"]),
        evaluation_at=normalized_at,
        problem=problem,
        candidate=candidate,
    )
    _verify_source(snapshot)
    return score


def score_canonical_schedule_with_evidence(
    path: str | Path,
    *,
    proposal_ref: str,
    evidence_snapshot: EvidenceSnapshot,
    availability_keys: tuple[FactKey, ...] = (),
    lodging_intake: LodgingIntakeAssessment | None = None,
    pending_lodging_review: LodgingConfirmationReview | None = None,
) -> CanonicalScheduleScore:
    """Replay an evidence-bound proposal without serialized authority."""

    if not isinstance(proposal_ref, str) or _DIGEST_RE.fullmatch(proposal_ref) is None:
        raise TripctlScheduleError("INVALID_PROPOSAL_REF")
    inputs, problem, result = _solve_exact_with_evidence(
        Path(path),
        evidence_snapshot=evidence_snapshot,
        availability_keys=availability_keys,
        lodging_intake=lodging_intake,
        pending_lodging_review=pending_lodging_review,
    )
    candidate = result.candidate
    expected_ref = (
        _runtime_proposal_ref(
            candidate.candidate_id,
            inputs.assessment.runtime_context_ref,
        )
        if candidate is not None
        else None
    )
    if expected_ref != proposal_ref:
        _verify_evidence_source(inputs)
        raise TripctlScheduleError("STALE_PROPOSAL_REF")
    try:
        replay_schedule_candidate(problem, candidate)
    except ScheduleContractError:
        _raise_after_evidence_source_check(
            inputs,
            "PROPOSAL_REPLAY_UNAVAILABLE",
        )
    score = _score_projection(
        inputs.source,
        plan_revision=str(inputs.plan["revision"]),
        evaluation_at=inputs.composed.evidence.evaluation_at,
        problem=problem,
        candidate=candidate,
        runtime=inputs.assessment,
        proposal_ref=proposal_ref,
    )
    _verify_evidence_source(inputs)
    return score


def _solve_exact(
    data_dir: Path,
    *,
    evaluation_at: datetime,
) -> tuple[
    _CanonicalSourceSnapshot,
    dict[str, Any],
    ScheduleProblem,
    ScheduleResult,
]:
    try:
        snapshot = _read_source_snapshot(data_dir)
    except CanonicalTripctlError as exc:
        raise TripctlScheduleError(exc.code, retryable=exc.retryable) from exc
    try:
        plan = decode_plan(snapshot.raw)
        problem = schedule_problem_from_plan(
            plan,
            evaluation_at=evaluation_at,
        )
        result = solve_schedule(problem)
    except PlanCodecError:
        _raise_after_source_check(snapshot, "CANONICAL_PLAN_MALFORMED")
    except (
        AssertionError,
        KeyError,
        LoadError,
        MemoryError,
        OverflowError,
        RecursionError,
        ScheduleContractError,
        TypeError,
        ValueError,
    ):
        _raise_after_source_check(snapshot, "SCHEDULE_PROPOSAL_UNAVAILABLE")
    except OSError:
        _raise_after_source_check(snapshot, "SCHEDULE_PROPOSAL_UNAVAILABLE")
    _verify_source(snapshot)
    return snapshot, plan, problem, result


def _solve_exact_with_evidence(
    data_dir: Path,
    *,
    evidence_snapshot: EvidenceSnapshot,
    availability_keys: tuple[FactKey, ...],
    lodging_intake: LodgingIntakeAssessment | None,
    pending_lodging_review: LodgingConfirmationReview | None,
) -> tuple[_CanonicalRuntimeInputs, ScheduleProblem, ScheduleResult]:
    try:
        inputs = _load_canonical_runtime(
            data_dir,
            evidence_snapshot=evidence_snapshot,
            availability_keys=availability_keys,
            lodging_intake=lodging_intake,
            pending_lodging_review=pending_lodging_review,
        )
    except CanonicalRuntimeError as exc:
        raise TripctlScheduleError(
            exc.code,
            retryable=exc.retryable,
        ) from exc
    try:
        problem = schedule_problem_from_composed(inputs.composed)
        result = solve_schedule(problem)
    except (
        AssertionError,
        KeyError,
        LoadError,
        MemoryError,
        OverflowError,
        RecursionError,
        ScheduleContractError,
        TypeError,
        ValueError,
    ):
        _raise_after_evidence_source_check(
            inputs,
            "SCHEDULE_PROPOSAL_UNAVAILABLE",
        )
    except OSError:
        _raise_after_evidence_source_check(
            inputs,
            "SCHEDULE_PROPOSAL_UNAVAILABLE",
        )
    _verify_evidence_source(inputs)
    return inputs, problem, result


def _proposal_projection(
    snapshot: _CanonicalSourceSnapshot,
    *,
    plan_revision: str,
    evaluation_at: datetime,
    problem: ScheduleProblem,
    result: ScheduleResult,
    runtime: CanonicalRuntimeAssessment | None = None,
) -> CanonicalScheduleProposal:
    candidate = result.candidate
    failure = result.failure
    proposal_ref = candidate.candidate_id if candidate is not None else None
    if proposal_ref is not None and runtime is not None:
        proposal_ref = _runtime_proposal_ref(
            proposal_ref,
            runtime.runtime_context_ref,
        )
    return CanonicalScheduleProposal(
        source_digest=snapshot.source_digest,
        plan_revision=plan_revision,
        evaluation_at=evaluation_at,
        schedule_status=result.status.value,
        problem_ref=problem.problem_id,
        proposal_ref=proposal_ref,
        candidate_count=len(result.candidates),
        evaluations_used=result.evaluations_used,
        evaluation_limit=result.evaluation_limit,
        optimality=result.optimality,
        failure_code=_safe_failure_code(failure.code if failure else None),
        failure_activity_count=len(failure.activity_ids) if failure else 0,
        failure_day_count=len(failure.day_ids) if failure else 0,
        failure_constraint_count=len(failure.constraint_ids) if failure else 0,
        missing_arc_count=len(failure.missing_arc_keys) if failure else 0,
        kernel_issue_count=len(failure.kernel_issues) if failure else 0,
        runtime=runtime,
    )


def _score_projection(
    snapshot: _CanonicalSourceSnapshot,
    *,
    plan_revision: str,
    evaluation_at: datetime,
    problem: ScheduleProblem,
    candidate: ScheduleCandidate,
    runtime: CanonicalRuntimeAssessment | None = None,
    proposal_ref: str | None = None,
) -> CanonicalScheduleScore:
    score: ScheduleScore = candidate.score
    return CanonicalScheduleScore(
        source_digest=snapshot.source_digest,
        plan_revision=plan_revision,
        evaluation_at=evaluation_at,
        problem_ref=problem.problem_id,
        proposal_ref=(
            proposal_ref
            if proposal_ref is not None
            else candidate.candidate_id
        ),
        timeline_status=candidate.report.status.value,
        objective_key=score.objective_key(),
        hard_violation_count=score.hard_violation_count,
        missing_required_count=score.missing_required_count,
        verification_risk_count=score.verification_risk_count,
        protected_change_count=score.protected_change_count,
        accepted_activity_change_count=score.accepted_activity_change_count,
        accepted_day_move_count=score.accepted_day_move_count,
        accepted_order_inversion_count=score.accepted_order_inversion_count,
        accepted_time_shift_deci_min=score.accepted_time_shift_deci_min,
        served_priority_points=score.served_priority_points,
        scheduled_optional_count=len(score.scheduled_optional_ids),
        soft_constraint_violation_count=score.soft_constraint_violation_count,
        tight_slack_count=score.tight_slack_count,
        slack_deficit_deci_min=score.slack_deficit_deci_min,
        activity_count_overage=score.activity_count_overage,
        service_overage_deci_min=score.service_overage_deci_min,
        wait_deci_min=score.wait_deci_min,
        travel_deci_min=score.travel_deci_min,
        buffer_deci_min=score.buffer_deci_min,
        service_deci_min=score.service_deci_min,
        runtime=runtime,
    )


def _verify_source(snapshot: _CanonicalSourceSnapshot) -> None:
    if not _source_is_current(snapshot):
        raise TripctlScheduleError("STALE_CANONICAL_PLAN", retryable=True)


def _verify_evidence_source(inputs: _CanonicalRuntimeInputs) -> None:
    try:
        _verify_runtime_source(inputs)
    except CanonicalRuntimeError as exc:
        raise TripctlScheduleError(
            exc.code,
            retryable=exc.retryable,
        ) from exc


def _raise_after_source_check(
    snapshot: _CanonicalSourceSnapshot,
    code: str,
) -> NoReturn:
    _verify_source(snapshot)
    raise TripctlScheduleError(code)


def _raise_after_evidence_source_check(
    inputs: _CanonicalRuntimeInputs,
    code: str,
) -> NoReturn:
    _verify_evidence_source(inputs)
    raise TripctlScheduleError(code)


def _normalized_evaluation_at(value: datetime) -> datetime:
    _require_evaluation_at(value)
    return value.astimezone(timezone.utc)


def _require_evaluation_at(value: object) -> None:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise TripctlScheduleError("INVALID_EVALUATION_AT")


def _require_digest(value: object, name: str) -> None:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 reference")


def _require_plain_digest(value: object, name: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{name} must be a plain SHA-256 digest")


def _require_count(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _require_runtime_binding(
    runtime: CanonicalRuntimeAssessment | None,
    *,
    source_digest: str,
    plan_revision: str,
    evaluation_at: datetime,
) -> None:
    if runtime is None:
        return
    if type(runtime) is not CanonicalRuntimeAssessment:
        raise TypeError("runtime must be exact CanonicalRuntimeAssessment")
    if (
        runtime.source_digest != source_digest
        or runtime.plan_revision != plan_revision
        or runtime.readiness.evaluated_at != evaluation_at
    ):
        raise ValueError("runtime assessment differs from schedule identity")


def _runtime_proposal_ref(candidate_ref: str, runtime_context_ref: str) -> str:
    _require_digest(candidate_ref, "candidate_ref")
    _require_plain_digest(runtime_context_ref, "runtime_context_ref")
    payload = (
        b"trip-planner.tripctl-runtime-proposal/v1\0"
        + candidate_ref.encode("ascii")
        + b"\0"
        + runtime_context_ref.encode("ascii")
    )
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _safe_failure_code(value: str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and _PROBLEM_CODE_RE.fullmatch(value) is not None:
        return value
    return "SCHEDULE_FAILURE_REDACTED"


__all__ = [
    "TRIPCTL_SCHEDULE_VERSION",
    "CanonicalScheduleProposal",
    "CanonicalScheduleScore",
    "TripctlScheduleError",
    "propose_canonical_schedule",
    "propose_canonical_schedule_with_evidence",
    "score_canonical_schedule",
    "score_canonical_schedule_with_evidence",
]
