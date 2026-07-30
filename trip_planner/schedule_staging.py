"""Schedule-specific review and commit seam.

This module deliberately does not route schedule candidates through the Phase 2
repair proposal contract.  A schedule can improve a feasible itinerary without
owning a repair issue, so it gets one small review boundary that still reuses
the canonical store's exact approval, CAS, receipt, and atomic-write semantics.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

from .codec import (
    FrozenJsonValue,
    canonical_json_bytes,
    compute_revision,
    deep_copy_json,
    freeze_json,
    plan_to_trip_state,
)
from .composition import compose_trip_state
from .facts import EvidenceSnapshot
from .models import CheckReport, CheckStatus, TripState
from .mutations import (
    ApprovalGrant,
    ChangeRecord,
    PlanPatch,
    apply_patch_to_plan,
    patch_digest,
)
from .repair_loop import HumanCheckpointGrant, PlanRepository
from .scheduler import SOLVER_VERSION
from .scheduling import (
    ScheduleCandidate,
    ScheduleContractError,
    ScheduleProblem,
    ScheduleScore,
    assignments_from_state,
    candidate_to_plan_patch,
    evaluate_schedule_state,
    replay_schedule_candidate,
    schedule_key,
    schedule_problem_from_composed,
    schedule_problem_from_plan,
    score_schedule,
)
from .store import StoreProblem, StoreResult


_OBJECTIVE_NAMES = (
    "hard_violation_count",
    "missing_required_count",
    "verification_risk_count",
    "protected_change_count",
    "accepted_activity_change_count",
    "accepted_day_move_count",
    "accepted_order_inversion_count",
    "accepted_time_shift_deci_min",
    "served_priority_points",
    "scheduled_optional_count",
    "soft_constraint_violation_count",
    "tight_slack_count",
    "slack_deficit_deci_min",
    "activity_count_overage",
    "service_overage_deci_min",
    "wait_deci_min",
    "travel_deci_min",
)


class EvidenceLoadResult(Protocol):
    """One current evidence load that can issue an immutable snapshot."""

    def snapshot(self, *, evaluation_at: datetime) -> EvidenceSnapshot:
        """Return evidence pinned to the requested semantic instant."""


class EvidenceSource(Protocol):
    """Reloadable evidence boundary used to detect review-time drift."""

    def load(self) -> EvidenceLoadResult:
        """Return the source's current evidence result."""


class ScheduleStageState(str, Enum):
    """Observable states for one bounded schedule review."""

    READY = "ready"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_EXTERNAL = "waiting_external"
    APPLIED = "applied"
    REPLAY_CONFIRMED = "replay_confirmed"
    REJECTED = "rejected"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True, slots=True)
class ScheduleStageProblem:
    """Machine-readable staging or commit failure."""

    code: str
    message: str
    details: Mapping[str, FrozenJsonValue] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _safe_code(self.code))
        object.__setattr__(self, "message", _safe_message(self.message))
        frozen = freeze_json(self.details)
        if not isinstance(frozen, Mapping):
            raise TypeError("ScheduleStageProblem.details must be a mapping")
        object.__setattr__(self, "details", frozen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": deep_copy_json(self.details),
        }


@dataclass(frozen=True, slots=True)
class ScheduleStageReview:
    """Read-only review of one exact schedule candidate and store diff."""

    state: ScheduleStageState
    problem_id: str
    candidate_id: str
    base_revision: str
    review_id: str | None = None
    patch_digest: str | None = None
    expected_state_digest: str | None = None
    expected_applied_revision: str | None = None
    baseline_score: ScheduleScore | None = field(default=None, repr=False)
    candidate_score: ScheduleScore | None = field(default=None, repr=False)
    evidence_binding_digest: str | None = None
    baseline_schedule_key: str | None = None
    candidate_schedule_key: str | None = None
    decisive_objective: str | None = None
    change_count: int = 0
    changes: tuple[ChangeRecord, ...] = ()
    protected_changes: tuple[ChangeRecord, ...] = ()
    affected_day_ids: tuple[str, ...] = ()
    invalidated_day_ids: tuple[str, ...] = ()
    risk_codes: tuple[str, ...] = ()
    required_approval_scope: str | None = None
    post_preview_status: str | None = None
    store_status: str | None = None
    current_revision: str | None = None
    problems: tuple[ScheduleStageProblem, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, ScheduleStageState):
            raise TypeError("ScheduleStageReview.state must be ScheduleStageState")
        for value, name in (
            (self.problem_id, "problem_id"),
            (self.candidate_id, "candidate_id"),
            (self.base_revision, "base_revision"),
        ):
            _require_text(value, f"ScheduleStageReview.{name}")
        for value, name in (
            (self.review_id, "review_id"),
            (self.patch_digest, "patch_digest"),
            (self.expected_state_digest, "expected_state_digest"),
            (
                self.expected_applied_revision,
                "expected_applied_revision",
            ),
            (self.baseline_schedule_key, "baseline_schedule_key"),
            (self.candidate_schedule_key, "candidate_schedule_key"),
            (self.decisive_objective, "decisive_objective"),
            (self.required_approval_scope, "required_approval_scope"),
            (self.post_preview_status, "post_preview_status"),
            (self.store_status, "store_status"),
            (self.current_revision, "current_revision"),
        ):
            _optional_text(value, f"ScheduleStageReview.{name}")
        if (
            self.evidence_binding_digest is not None
            and not _is_sha256_digest(self.evidence_binding_digest)
        ):
            raise ValueError(
                "ScheduleStageReview.evidence_binding_digest must be a "
                "SHA-256 digest"
            )
        for value, name in (
            (self.baseline_score, "baseline_score"),
            (self.candidate_score, "candidate_score"),
        ):
            if value is not None and not isinstance(value, ScheduleScore):
                raise TypeError(f"ScheduleStageReview.{name} must be ScheduleScore")
        _require_non_negative_int(self.change_count, "change_count")
        if not isinstance(self.changes, tuple) or any(
            not isinstance(item, ChangeRecord) for item in self.changes
        ):
            raise TypeError(
                "ScheduleStageReview.changes must contain ChangeRecord values"
            )
        if self.change_count != len(self.changes):
            raise ValueError("ScheduleStageReview.change_count must match changes")
        if not isinstance(self.protected_changes, tuple) or any(
            not isinstance(item, ChangeRecord)
            for item in self.protected_changes
        ):
            raise TypeError(
                "ScheduleStageReview.protected_changes must contain "
                "ChangeRecord values"
            )
        for value, name in (
            (self.affected_day_ids, "affected_day_ids"),
            (self.invalidated_day_ids, "invalidated_day_ids"),
            (self.risk_codes, "risk_codes"),
        ):
            _require_text_tuple(value, f"ScheduleStageReview.{name}")
        if not isinstance(self.problems, tuple) or any(
            not isinstance(item, ScheduleStageProblem) for item in self.problems
        ):
            raise TypeError(
                "ScheduleStageReview.problems must contain ScheduleStageProblem"
            )
        if self.state not in {
            ScheduleStageState.READY,
            ScheduleStageState.WAITING_APPROVAL,
            ScheduleStageState.REPLAY_CONFIRMED,
            ScheduleStageState.REJECTED,
        }:
            raise ValueError(
                "ScheduleStageReview.state is not a staging outcome"
            )
        if self.state in {
            ScheduleStageState.READY,
            ScheduleStageState.WAITING_APPROVAL,
        }:
            required = (
                self.review_id,
                self.patch_digest,
                self.expected_state_digest,
                self.expected_applied_revision,
                self.baseline_score,
                self.candidate_score,
                self.baseline_schedule_key,
                self.candidate_schedule_key,
                self.decisive_objective,
                self.store_status,
                self.current_revision,
            )
            if any(value is None for value in required) or self.problems:
                raise ValueError(
                    "commit-ready reviews require the complete exact effect"
                )
        if self.state is ScheduleStageState.READY and (
            self.risk_codes or self.required_approval_scope is not None
        ):
            raise ValueError("READY review cannot have an approval gate")
        if self.state is ScheduleStageState.WAITING_APPROVAL and not (
            self.risk_codes or self.required_approval_scope is not None
        ):
            raise ValueError(
                "WAITING_APPROVAL review requires an approval gate"
            )
        if self.state is ScheduleStageState.REJECTED and not self.problems:
            raise ValueError("REJECTED review requires a problem")
        if self.state is ScheduleStageState.REPLAY_CONFIRMED and (
            self.problems or self.patch_digest is None
        ):
            raise ValueError(
                "REPLAY_CONFIRMED review requires an exact patch digest"
            )

    @property
    def ready_to_commit(self) -> bool:
        return (
            self.review_id is not None
            and not self.problems
            and self.state
            in {
                ScheduleStageState.READY,
                ScheduleStageState.WAITING_APPROVAL,
            }
        )

    @property
    def requires_human_checkpoint(self) -> bool:
        return bool(self.risk_codes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "problem_id": self.problem_id,
            "candidate_id": self.candidate_id,
            "base_revision": self.base_revision,
            "review_id": self.review_id,
            "patch_digest": self.patch_digest,
            "expected_state_digest": self.expected_state_digest,
            "expected_applied_revision": self.expected_applied_revision,
            "baseline_score": _safe_score_dict(
                self.baseline_score,
                evidence_bound=self.evidence_binding_digest is not None,
            ),
            "candidate_score": _safe_score_dict(
                self.candidate_score,
                evidence_bound=self.evidence_binding_digest is not None,
            ),
            "evidence_binding_digest": self.evidence_binding_digest,
            "baseline_schedule_key": self.baseline_schedule_key,
            "candidate_schedule_key": self.candidate_schedule_key,
            "decisive_objective": self.decisive_objective,
            "change_count": self.change_count,
            "changes": [change.to_dict() for change in self.changes],
            "protected_changes": [
                change.to_dict() for change in self.protected_changes
            ],
            "affected_day_ids": list(self.affected_day_ids),
            "invalidated_day_ids": list(self.invalidated_day_ids),
            "risk_codes": list(self.risk_codes),
            "required_approval_scope": self.required_approval_scope,
            "post_preview_status": self.post_preview_status,
            "store_status": self.store_status,
            "current_revision": self.current_revision,
            "problems": [problem.to_dict() for problem in self.problems],
        }


@dataclass(frozen=True, slots=True)
class ScheduleCommitResult:
    """Durable outcome of committing one reviewed schedule candidate."""

    state: ScheduleStageState
    applied: bool
    review_id: str | None = None
    problem_id: str | None = None
    candidate_id: str | None = None
    store_status: str | None = None
    transaction_id: str | None = None
    applied_revision: str | None = None
    current_revision: str | None = None
    replayed: bool = False
    candidate_is_current: bool = False
    check_report: CheckReport | None = field(default=None, repr=False)
    required_arc_keys: tuple[str, ...] = ()
    affected_day_ids: tuple[str, ...] = ()
    invalidated_day_ids: tuple[str, ...] = ()
    change_count: int = 0
    changes: tuple[ChangeRecord, ...] = ()
    protected_changes: tuple[ChangeRecord, ...] = ()
    problems: tuple[ScheduleStageProblem, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, ScheduleStageState):
            raise TypeError("ScheduleCommitResult.state must be ScheduleStageState")
        if not isinstance(self.applied, bool):
            raise TypeError("ScheduleCommitResult.applied must be bool")
        for value, name in (
            (self.review_id, "review_id"),
            (self.problem_id, "problem_id"),
            (self.candidate_id, "candidate_id"),
            (self.store_status, "store_status"),
            (self.transaction_id, "transaction_id"),
            (self.applied_revision, "applied_revision"),
            (self.current_revision, "current_revision"),
        ):
            _optional_text(value, f"ScheduleCommitResult.{name}")
        if not isinstance(self.replayed, bool) or not isinstance(
            self.candidate_is_current, bool
        ):
            raise TypeError("ScheduleCommitResult replay flags must be bool")
        if self.check_report is not None and not isinstance(
            self.check_report, CheckReport
        ):
            raise TypeError("ScheduleCommitResult.check_report must be CheckReport")
        for value, name in (
            (self.required_arc_keys, "required_arc_keys"),
            (self.affected_day_ids, "affected_day_ids"),
            (self.invalidated_day_ids, "invalidated_day_ids"),
        ):
            _require_text_tuple(value, f"ScheduleCommitResult.{name}")
        _require_non_negative_int(self.change_count, "change_count")
        if not isinstance(self.changes, tuple) or any(
            not isinstance(item, ChangeRecord) for item in self.changes
        ):
            raise TypeError(
                "ScheduleCommitResult.changes must contain ChangeRecord values"
            )
        if self.change_count != len(self.changes):
            raise ValueError("ScheduleCommitResult.change_count must match changes")
        if not isinstance(self.protected_changes, tuple) or any(
            not isinstance(item, ChangeRecord)
            for item in self.protected_changes
        ):
            raise TypeError(
                "ScheduleCommitResult.protected_changes must contain "
                "ChangeRecord values"
            )
        if not isinstance(self.problems, tuple) or any(
            not isinstance(item, ScheduleStageProblem) for item in self.problems
        ):
            raise TypeError(
                "ScheduleCommitResult.problems must contain ScheduleStageProblem"
            )
        if self.state is ScheduleStageState.READY:
            raise ValueError("ScheduleCommitResult cannot be READY")
        if self.state in {
            ScheduleStageState.APPLIED,
            ScheduleStageState.WAITING_EXTERNAL,
            ScheduleStageState.REPLAY_CONFIRMED,
        } and not self.applied:
            raise ValueError(f"{self.state.value} requires applied=True")
        if self.state in {
            ScheduleStageState.REJECTED,
            ScheduleStageState.WAITING_APPROVAL,
        } and self.applied:
            raise ValueError(f"{self.state.value} requires applied=False")
        if self.state in {
            ScheduleStageState.REJECTED,
            ScheduleStageState.WAITING_APPROVAL,
            ScheduleStageState.OUTCOME_UNKNOWN,
        } and not self.problems:
            raise ValueError(f"{self.state.value} requires a problem")
        if self.applied:
            required = (
                self.review_id,
                self.problem_id,
                self.candidate_id,
                self.store_status,
                self.transaction_id,
                self.applied_revision,
                self.current_revision,
            )
            if any(value is None for value in required):
                raise ValueError(
                    "applied results require complete canonical receipt identity"
                )
        if self.state is ScheduleStageState.APPLIED and (
            not self.candidate_is_current
            or self.current_revision != self.applied_revision
            or self.check_report is None
            or self.problems
        ):
            raise ValueError(
                f"{self.state.value} requires a trusted current candidate report"
            )
        if self.state is ScheduleStageState.WAITING_EXTERNAL:
            evidence_drift = (
                not self.candidate_is_current
                and self.current_revision == self.applied_revision
                and self.check_report is not None
                and bool(self.problems)
                and any(
                    problem.code
                    in {
                        "EVIDENCE_REVISION_CHANGED",
                        "EVIDENCE_READ_FAILED",
                    }
                    for problem in self.problems
                )
                and all(
                    problem.code
                    in {
                        "EVIDENCE_REVISION_CHANGED",
                        "EVIDENCE_READ_FAILED",
                        "POST_COMMIT_PLAN_INFEASIBLE",
                    }
                    for problem in self.problems
                )
            )
            trusted_current = (
                self.candidate_is_current
                and self.current_revision == self.applied_revision
                and self.check_report is not None
                and not self.problems
            )
            if not (trusted_current or evidence_drift):
                raise ValueError(
                    "waiting_external requires a current report or typed "
                    "post-commit evidence drift"
                )
        if (
            self.state is ScheduleStageState.APPLIED
            and self.check_report is not None
            and self.check_report.status is not CheckStatus.FEASIBLE
        ):
            raise ValueError("APPLIED requires a feasible persisted report")
        if (
            self.state is ScheduleStageState.WAITING_EXTERNAL
            and self.check_report is not None
            and self.candidate_is_current
            and self.check_report.status is not CheckStatus.NEEDS_VERIFICATION
        ):
            raise ValueError(
                "WAITING_EXTERNAL requires a needs-verification report"
            )
        if self.state is ScheduleStageState.REPLAY_CONFIRMED and (
            not self.replayed
            or self.candidate_is_current
            or self.check_report is not None
            or self.problems
        ):
            raise ValueError(
                "REPLAY_CONFIRMED requires a historical exact receipt"
            )
        if (
            self.state is ScheduleStageState.OUTCOME_UNKNOWN
            and self.candidate_is_current
        ):
            raise ValueError(
                "OUTCOME_UNKNOWN cannot claim candidate_is_current"
            )
        if self.state in {
            ScheduleStageState.REJECTED,
            ScheduleStageState.WAITING_APPROVAL,
        } and (
            self.transaction_id is not None
            or self.applied_revision is not None
            or self.replayed
            or self.candidate_is_current
            or self.check_report is not None
        ):
            raise ValueError(
                f"{self.state.value} cannot carry a durable confirmation"
            )
        if self.state is ScheduleStageState.WAITING_APPROVAL and any(
            value is None
            for value in (
                self.review_id,
                self.problem_id,
                self.candidate_id,
            )
        ):
            raise ValueError(
                "WAITING_APPROVAL requires an identifiable pending review"
            )
        if self.candidate_is_current and (
            not self.applied
            or self.applied_revision is None
            or self.current_revision != self.applied_revision
            or self.check_report is None
        ):
            raise ValueError(
                "candidate_is_current requires a verified current revision"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "applied": self.applied,
            "review_id": self.review_id,
            "problem_id": self.problem_id,
            "candidate_id": self.candidate_id,
            "store_status": self.store_status,
            "transaction_id": self.transaction_id,
            "applied_revision": self.applied_revision,
            "current_revision": self.current_revision,
            "replayed": self.replayed,
            "candidate_is_current": self.candidate_is_current,
            "check_status": (
                self.check_report.status.value
                if self.check_report is not None
                else None
            ),
            "required_arc_keys": list(self.required_arc_keys),
            "affected_day_ids": list(self.affected_day_ids),
            "invalidated_day_ids": list(self.invalidated_day_ids),
            "change_count": self.change_count,
            "changes": [change.to_dict() for change in self.changes],
            "protected_changes": [
                change.to_dict() for change in self.protected_changes
            ],
            "problems": [problem.to_dict() for problem in self.problems],
        }


@dataclass(frozen=True, slots=True)
class _ScheduleAssessment:
    final_state: TripState
    baseline_report: CheckReport
    baseline_score: ScheduleScore
    candidate_score: ScheduleScore
    baseline_schedule_key: str
    candidate_key: tuple[Any, ...]
    baseline_key: tuple[Any, ...]
    decisive_objective: str


@dataclass(frozen=True, slots=True)
class _PendingScheduleReview:
    review: ScheduleStageReview
    problem: ScheduleProblem
    candidate: ScheduleCandidate
    patch: PlanPatch
    expected_schedule_key: tuple[Any, ...]
    expected_state_digest: str
    expected_applied_revision: str


@dataclass(frozen=True, slots=True)
class _PatchReceipt:
    status: str
    transaction_id: str
    applied_revision: str
    check_status: str | None
    required_approval_scope: str | None


class ScheduleStager:
    """One in-memory, exact-review boundary for schedule candidates."""

    def __init__(
        self,
        repository: PlanRepository,
        *,
        run_id: str,
        max_changes: int = 40,
        max_auto_changes: int = 12,
        expected_solver: str | None = SOLVER_VERSION,
        evidence_source: EvidenceSource | None = None,
    ) -> None:
        _require_text(run_id, "run_id")
        _require_non_negative_int(max_changes, "max_changes")
        _require_non_negative_int(max_auto_changes, "max_auto_changes")
        if max_auto_changes > max_changes:
            raise ValueError("max_auto_changes cannot exceed max_changes")
        if expected_solver is not None:
            _require_text(expected_solver, "expected_solver")
        if evidence_source is not None and not callable(
            getattr(evidence_source, "load", None)
        ):
            raise TypeError("evidence_source must provide load() or be None")
        self._repository = repository
        self._evidence_source = evidence_source
        self.run_id = run_id
        self.max_changes = max_changes
        self.max_auto_changes = max_auto_changes
        self.expected_solver = expected_solver
        self._pending: _PendingScheduleReview | None = None
        self._commit_attempts = 0

    @property
    def has_pending_review(self) -> bool:
        return self._pending is not None

    def cancel_pending(self) -> None:
        self._pending = None
        self._commit_attempts = 0

    def stage_schedule_candidate(
        self,
        problem: ScheduleProblem,
        candidate: ScheduleCandidate,
    ) -> ScheduleStageReview:
        """Reload, replay, compare, project, and preview one exact candidate."""

        if not isinstance(problem, ScheduleProblem):
            raise TypeError("problem must be ScheduleProblem")
        if not isinstance(candidate, ScheduleCandidate):
            raise TypeError("candidate must be ScheduleCandidate")
        if self._pending is not None:
            return _problem_review(
                problem,
                candidate,
                "PENDING_SCHEDULE_REVIEW",
                "commit or cancel the current schedule review first",
            )
        if (
            self.expected_solver is not None
            and candidate.solver != self.expected_solver
        ):
            return _problem_review(
                problem,
                candidate,
                "UNSUPPORTED_SOLVER_VERSION",
                "candidate solver version is not accepted by this stager",
                details={
                    "expected_solver": self.expected_solver,
                    "candidate_solver": candidate.solver,
                },
            )
        try:
            final_state = replay_schedule_candidate(problem, candidate)
        except (ScheduleContractError, TypeError, ValueError) as exc:
            return _problem_review(
                problem,
                candidate,
                getattr(exc, "code", "CANDIDATE_REPLAY_FAILED"),
                str(exc),
            )

        try:
            current_plan = self._repository.load_plan()
        except Exception as exc:
            return _problem_review(
                problem,
                candidate,
                "REPOSITORY_READ_FAILED",
                str(exc),
            )
        current_revision = _plan_revision(current_plan)
        current_problem, problem_error = _rebuild_current_problem(
            current_plan,
            problem,
            evidence_source=self._evidence_source,
        )
        if problem_error is not None:
            return _problem_review(
                problem,
                candidate,
                problem_error.code,
                problem_error.message,
                details=problem_error.details,
                current_revision=current_revision,
            )
        assert current_problem is not None
        if current_problem.problem_id != problem.problem_id:
            try:
                patch = candidate_to_plan_patch(problem, candidate)
                request_digest = patch_digest(patch)
            except (ScheduleContractError, TypeError, ValueError) as exc:
                return _problem_review(
                    problem,
                    candidate,
                    getattr(exc, "code", "SCHEDULE_PROJECTION_FAILED"),
                    str(exc),
                    current_revision=current_revision,
                )
            receipt, receipt_problem = _inspect_patch_receipt(
                current_plan,
                patch,
            )
            if receipt_problem is not None:
                return _problem_review(
                    problem,
                    candidate,
                    receipt_problem.code,
                    receipt_problem.message,
                    details=receipt_problem.details,
                    current_revision=current_revision,
                )
            if receipt is not None and receipt.status == "applied":
                return ScheduleStageReview(
                    state=ScheduleStageState.REPLAY_CONFIRMED,
                    problem_id=problem.problem_id,
                    candidate_id=candidate.candidate_id,
                    base_revision=problem.base_revision,
                    evidence_binding_digest=(
                        problem.evidence_binding.binding_digest
                        if problem.evidence_binding is not None
                        else None
                    ),
                    patch_digest=request_digest,
                    store_status="replayed",
                    current_revision=current_revision,
                )
            details = {
                "expected_problem_id": problem.problem_id,
                "current_problem_id": current_problem.problem_id,
                "expected_revision": problem.base_revision,
                "current_revision": current_revision,
            }
            if receipt is not None and receipt.status == "rolled_back":
                return _problem_review(
                    problem,
                    candidate,
                    "SCHEDULE_PATCH_ROLLED_BACK",
                    "this exact schedule patch was applied and later rolled back",
                    details=details,
                    current_revision=current_revision,
                )
            drift_code, drift_message = _schedule_problem_drift(
                problem, current_problem
            )
            return _problem_review(
                problem,
                candidate,
                drift_code,
                drift_message,
                details=details,
                current_revision=current_revision,
            )

        try:
            assessment = _assess_strict_improvement(
                current_problem,
                candidate,
                final_state=final_state,
            )
        except ScheduleContractError as exc:
            return _problem_review(
                problem,
                candidate,
                exc.code,
                exc.message,
                current_revision=current_revision,
            )
        try:
            patch = candidate_to_plan_patch(problem, candidate)
            request_digest = patch_digest(patch)
        except (ScheduleContractError, TypeError, ValueError) as exc:
            return _problem_review(
                problem,
                candidate,
                getattr(exc, "code", "SCHEDULE_PROJECTION_FAILED"),
                str(exc),
                current_revision=current_revision,
            )

        preview = self._preview_exact_patch(
            patch,
            (),
            evaluation_at=problem.evaluation_at,
        )
        if preview is None:
            return _problem_review(
                problem,
                candidate,
                "REPOSITORY_PREVIEW_FAILED",
                "repository preview raised an exception",
                current_revision=current_revision,
            )
        if _is_exact_replay(preview) or preview.status == "replayed_rolled_back":
            return self._reconcile_stage_replay(
                problem,
                candidate,
                patch,
                request_digest=request_digest,
                store_status=preview.status,
            )
        preview_problem = _preview_problem(
            preview,
            allow_approval_required=True,
        )
        if preview_problem is not None:
            return _problem_review(
                problem,
                candidate,
                preview_problem.code,
                preview_problem.message,
                details=preview_problem.details,
                current_revision=preview.current_revision or current_revision,
            )
        contract_problem = _validate_preview_contract(
            current_plan,
            patch,
            (),
            preview,
            problem=problem,
        )
        if contract_problem is not None:
            return _problem_review(
                problem,
                candidate,
                contract_problem.code,
                contract_problem.message,
                details=contract_problem.details,
                current_revision=preview.current_revision or current_revision,
            )
        if not _is_store_approval_required(preview):
            projection_problem = _verify_store_projection(
                preview, final_state
            )
            if projection_problem is not None:
                return _problem_review(
                    problem,
                    candidate,
                    projection_problem.code,
                    projection_problem.message,
                    details=projection_problem.details,
                    current_revision=preview.current_revision or current_revision,
                )

        expected_state_digest = _preview_state_digest(preview)
        expected_applied_revision = _expected_applied_revision(
            current_plan,
            preview,
        )
        if (
            expected_state_digest is None
            or expected_applied_revision is None
        ):
            return _problem_review(
                problem,
                candidate,
                "MALFORMED_PREVIEW",
                "schedule preview omitted its complete semantic state",
                current_revision=preview.current_revision or current_revision,
            )
        changes = _changes(preview)
        if len(changes) > self.max_changes:
            return _problem_review(
                problem,
                candidate,
                "CHANGE_BUDGET_EXCEEDED",
                "schedule preview exceeds the full persistent change budget",
                details={
                    "change_count": len(changes),
                    "max_changes": self.max_changes,
                },
                current_revision=current_revision,
            )
        protected_changes = _protected_changes(preview)
        affected_day_ids = _affected_day_ids(preview)
        invalidated_day_ids = _invalidated_day_ids(preview)
        risk_codes = _schedule_risks(
            change_count=len(changes),
            max_auto_changes=self.max_auto_changes,
        )
        required_scope = preview.required_approval_scope
        review_id = _review_id(
            self.run_id,
            problem,
            candidate,
            request_digest=request_digest,
            expected_state_digest=expected_state_digest,
            expected_applied_revision=expected_applied_revision,
            assessment=assessment,
            changes=changes,
            protected_changes=protected_changes,
            affected_day_ids=affected_day_ids,
            invalidated_day_ids=invalidated_day_ids,
            risk_codes=risk_codes,
            required_approval_scope=required_scope,
            post_preview_status=preview.check_status,
        )
        state = (
            ScheduleStageState.WAITING_APPROVAL
            if risk_codes or required_scope is not None
            else ScheduleStageState.READY
        )
        review = ScheduleStageReview(
            state=state,
            problem_id=problem.problem_id,
            candidate_id=candidate.candidate_id,
            base_revision=problem.base_revision,
            review_id=review_id,
            patch_digest=request_digest,
            expected_state_digest=expected_state_digest,
            expected_applied_revision=expected_applied_revision,
            baseline_score=assessment.baseline_score,
            candidate_score=assessment.candidate_score,
            evidence_binding_digest=(
                problem.evidence_binding.binding_digest
                if problem.evidence_binding is not None
                else None
            ),
            baseline_schedule_key=assessment.baseline_schedule_key,
            candidate_schedule_key=candidate.schedule_key,
            decisive_objective=assessment.decisive_objective,
            change_count=len(changes),
            changes=changes,
            protected_changes=protected_changes,
            affected_day_ids=affected_day_ids,
            invalidated_day_ids=invalidated_day_ids,
            risk_codes=risk_codes,
            required_approval_scope=required_scope,
            post_preview_status=preview.check_status,
            store_status=preview.status,
            current_revision=current_revision,
        )
        self._pending = _PendingScheduleReview(
            review=review,
            problem=problem,
            candidate=candidate,
            patch=patch,
            expected_schedule_key=_schedule_state_key(final_state),
            expected_state_digest=expected_state_digest,
            expected_applied_revision=expected_applied_revision,
        )
        self._commit_attempts = 0
        return review

    def _reconcile_stage_replay(
        self,
        problem: ScheduleProblem,
        candidate: ScheduleCandidate,
        patch: PlanPatch,
        *,
        request_digest: str,
        store_status: str,
    ) -> ScheduleStageReview:
        try:
            observed_plan = self._repository.load_plan()
        except Exception as exc:
            return _problem_review(
                problem,
                candidate,
                "REPLAY_RECONCILIATION_FAILED",
                str(exc),
            )
        current_revision = _plan_revision(observed_plan)
        _, canonical_problem = _canonical_plan_state(
            observed_plan,
            expected_trip_id=patch.trip_id,
        )
        if canonical_problem is not None:
            return _problem_review(
                problem,
                candidate,
                canonical_problem.code,
                canonical_problem.message,
                details=canonical_problem.details,
                current_revision=current_revision,
            )
        receipt, receipt_problem = _inspect_patch_receipt(
            observed_plan,
            patch,
        )
        if receipt_problem is not None:
            return _problem_review(
                problem,
                candidate,
                receipt_problem.code,
                receipt_problem.message,
                details=receipt_problem.details,
                current_revision=current_revision,
            )
        if receipt is None:
            return _problem_review(
                problem,
                candidate,
                "REPLAY_RECEIPT_MISSING",
                "repository replay has no matching canonical receipt",
                current_revision=current_revision,
            )
        if receipt.status == "rolled_back":
            return _problem_review(
                problem,
                candidate,
                "SCHEDULE_PATCH_ROLLED_BACK",
                "this exact schedule patch was applied and later rolled back",
                current_revision=current_revision,
            )
        return ScheduleStageReview(
            state=ScheduleStageState.REPLAY_CONFIRMED,
            problem_id=problem.problem_id,
            candidate_id=candidate.candidate_id,
            base_revision=problem.base_revision,
            evidence_binding_digest=(
                problem.evidence_binding.binding_digest
                if problem.evidence_binding is not None
                else None
            ),
            patch_digest=request_digest,
            store_status=store_status,
            current_revision=current_revision,
        )

    def commit(
        self,
        review_id: str,
        *,
        human_grant: HumanCheckpointGrant | None = None,
        approvals: Sequence[ApprovalGrant] = (),
    ) -> ScheduleCommitResult:
        """Revalidate, re-preview, and atomically commit one exact review."""

        _require_text(review_id, "review_id")
        pending = self._pending
        if pending is None or pending.review.review_id != review_id:
            return _problem_result(
                "UNKNOWN_SCHEDULE_REVIEW",
                "no pending schedule review matches the supplied review ID",
                review_id=review_id,
            )
        review = pending.review
        try:
            current_plan = self._repository.load_plan()
        except Exception as exc:
            return _problem_result(
                "REPOSITORY_READ_FAILED",
                str(exc),
                pending=pending,
            )
        current_revision = _plan_revision(current_plan)
        _, canonical_problem = _canonical_plan_state(
            current_plan,
            expected_trip_id=pending.patch.trip_id,
        )
        if canonical_problem is not None:
            return _problem_result(
                canonical_problem.code,
                canonical_problem.message,
                details=canonical_problem.details,
                pending=pending,
                current_revision=current_revision,
            )
        receipt, receipt_problem = _inspect_patch_receipt(
            current_plan,
            pending.patch,
        )
        if receipt_problem is not None:
            self._pending = None
            return _problem_result(
                receipt_problem.code,
                receipt_problem.message,
                details=receipt_problem.details,
                pending=pending,
                current_revision=current_revision,
            )
        if receipt is not None:
            if receipt.status == "rolled_back":
                self._pending = None
                return _problem_result(
                    "SCHEDULE_PATCH_ROLLED_BACK",
                    "this exact schedule patch was applied and later rolled back",
                    pending=pending,
                    store_status="replayed_rolled_back",
                    current_revision=current_revision,
                )
            return self._reconcile_canonical_result(
                pending,
                store_status="replayed",
                replayed=True,
            )

        if review.risk_codes and (
            human_grant is None
            or not isinstance(human_grant, HumanCheckpointGrant)
            or human_grant.review_id != review_id
        ):
            return _problem_result(
                "HUMAN_CHECKPOINT_REQUIRED",
                "the full persistent diff exceeds automatic schedule policy",
                pending=pending,
                state=ScheduleStageState.WAITING_APPROVAL,
            )
        required_scope = review.required_approval_scope
        if required_scope is not None and not any(
            isinstance(grant, ApprovalGrant)
            and grant.scope_digest == required_scope
            for grant in approvals
        ):
            return _problem_result(
                "STORE_APPROVAL_REQUIRED",
                "the exact protected-change approval grant is missing",
                pending=pending,
                state=ScheduleStageState.WAITING_APPROVAL,
            )

        current_problem, problem_error = _rebuild_current_problem(
            current_plan,
            pending.problem,
            evidence_source=self._evidence_source,
        )
        if (
            problem_error is not None
            or current_problem is None
            or current_problem.problem_id != pending.problem.problem_id
        ):
            self._pending = None
            drift_code, drift_message = _schedule_problem_drift(
                pending.problem, current_problem
            )
            if drift_code == "STALE_SCHEDULE_PROBLEM":
                drift_code = "STALE_SCHEDULE_REVIEW"
                drift_message = (
                    "canonical state changed after schedule review"
                )
            return _problem_result(
                problem_error.code if problem_error is not None else drift_code,
                (
                    problem_error.message
                    if problem_error is not None
                    else drift_message
                ),
                pending=pending,
                current_revision=current_revision,
            )

        try:
            final_state = replay_schedule_candidate(
                current_problem, pending.candidate
            )
            assessment = _assess_strict_improvement(
                current_problem,
                pending.candidate,
                final_state=final_state,
            )
        except ScheduleContractError as exc:
            self._pending = None
            return _problem_result(
                exc.code,
                exc.message,
                pending=pending,
                current_revision=current_revision,
            )
        if _schedule_state_key(final_state) != pending.expected_schedule_key:
            self._pending = None
            return _problem_result(
                "CANDIDATE_REPLAY_MISMATCH",
                "trusted replay no longer reproduces the reviewed schedule",
                pending=pending,
                current_revision=current_revision,
            )

        preview = self._preview_exact_patch(
            pending.patch,
            approvals,
            evaluation_at=pending.problem.evaluation_at,
        )
        if preview is None:
            return _problem_result(
                "REPOSITORY_PREVIEW_FAILED",
                "repository re-preview raised an exception",
                pending=pending,
                current_revision=current_revision,
            )
        if _is_exact_replay(preview) or preview.status == "replayed_rolled_back":
            return self._reconcile_canonical_result(
                pending,
                store_status=preview.status,
                replayed=_is_exact_replay(preview),
            )
        preview_problem = _preview_problem(
            preview,
            allow_approval_required=False,
        )
        if preview_problem is not None:
            if _is_store_approval_required(preview):
                return _problem_result(
                    "STORE_APPROVAL_REQUIRED",
                    "repository still requires an exact protected-change approval",
                    pending=pending,
                    state=ScheduleStageState.WAITING_APPROVAL,
                    store_status=preview.status,
                    current_revision=preview.current_revision or current_revision,
                )
            self._pending = None
            return _problem_result(
                preview_problem.code,
                preview_problem.message,
                details=preview_problem.details,
                pending=pending,
                store_status=preview.status,
                current_revision=preview.current_revision or current_revision,
            )
        contract_problem = _validate_preview_contract(
            current_plan,
            pending.patch,
            approvals,
            preview,
            problem=pending.problem,
        )
        if contract_problem is not None:
            self._pending = None
            return _problem_result(
                contract_problem.code,
                contract_problem.message,
                details=contract_problem.details,
                pending=pending,
                store_status=preview.status,
                current_revision=preview.current_revision or current_revision,
            )
        projection_problem = _verify_store_projection(preview, final_state)
        if projection_problem is not None:
            self._pending = None
            return _problem_result(
                projection_problem.code,
                projection_problem.message,
                details=projection_problem.details,
                pending=pending,
                store_status=preview.status,
                current_revision=preview.current_revision or current_revision,
            )

        expected_state_digest = _preview_state_digest(preview)
        expected_applied_revision = _expected_applied_revision(
            current_plan,
            preview,
        )
        if (
            expected_state_digest is None
            or expected_applied_revision is None
        ):
            self._pending = None
            return _problem_result(
                "MALFORMED_PREVIEW",
                "schedule re-preview omitted its complete semantic state",
                pending=pending,
                store_status=preview.status,
                current_revision=preview.current_revision or current_revision,
            )
        changes = _changes(preview)
        if len(changes) > self.max_changes:
            self._pending = None
            return _problem_result(
                "CHANGE_BUDGET_EXCEEDED",
                "schedule re-preview exceeds the full persistent change budget",
                details={
                    "change_count": len(changes),
                    "max_changes": self.max_changes,
                },
                pending=pending,
                current_revision=current_revision,
            )
        protected_changes = _protected_changes(preview)
        affected_day_ids = _affected_day_ids(preview)
        invalidated_day_ids = _invalidated_day_ids(preview)
        risk_codes = _schedule_risks(
            change_count=len(changes),
            max_auto_changes=self.max_auto_changes,
        )
        if (
            assessment.baseline_key
            != review.baseline_score.objective_key()
            or assessment.candidate_key
            != review.candidate_score.objective_key()
            or assessment.decisive_objective != review.decisive_objective
            or expected_state_digest != review.expected_state_digest
            or expected_state_digest != pending.expected_state_digest
            or expected_applied_revision
            != review.expected_applied_revision
            or expected_applied_revision
            != pending.expected_applied_revision
            or changes != review.changes
            or protected_changes != review.protected_changes
            or affected_day_ids != review.affected_day_ids
            or invalidated_day_ids != review.invalidated_day_ids
            or risk_codes != review.risk_codes
            or preview.required_approval_scope
            != review.required_approval_scope
            or (
                review.post_preview_status is not None
                and preview.check_status != review.post_preview_status
            )
        ):
            self._pending = None
            return _problem_result(
                "NONDETERMINISTIC_SCHEDULE_PREVIEW",
                "re-preview did not reproduce the exact reviewed schedule effect",
                pending=pending,
                store_status=preview.status,
                current_revision=current_revision,
            )

        if self._commit_attempts >= 2:
            self._pending = None
            return _problem_result(
                "COMMIT_RETRY_LIMIT_EXCEEDED",
                "the exact pending review exhausted its two write attempts",
                pending=pending,
                state=ScheduleStageState.OUTCOME_UNKNOWN,
                current_revision=current_revision,
            )
        try:
            result = self._apply_pending_patch(
                pending,
                approvals,
            )
        except Exception as first_exc:
            if self._commit_attempts >= 2:
                return self._reconcile_canonical_result(
                    pending,
                    store_status="commit_exception",
                    replayed=True,
                    ambiguity_message=str(first_exc),
                )
            try:
                result = self._apply_pending_patch(
                    pending,
                    approvals,
                )
            except Exception as second_exc:
                return self._reconcile_canonical_result(
                    pending,
                    store_status="commit_exception",
                    replayed=True,
                    ambiguity_message=(
                        "repository raised during both exact commit attempts: "
                        f"{first_exc}; {second_exc}"
                    ),
                )
        else:
            if (
                result.status == "commit_outcome_unknown"
                and self._commit_attempts < 2
            ):
                try:
                    result = self._apply_pending_patch(
                        pending,
                        approvals,
                    )
                except Exception as exc:
                    return self._reconcile_canonical_result(
                        pending,
                        store_status="commit_exception",
                        replayed=True,
                        ambiguity_message=str(exc),
                    )
        if result.status == "commit_outcome_unknown":
            reconciled = self._reconcile_canonical_result(
                pending,
                store_status=result.status,
                replayed=True,
                ambiguity_message=(
                    "exact retry could not determine the durable schedule outcome"
                ),
            )
            if reconciled.state is ScheduleStageState.OUTCOME_UNKNOWN:
                self._pending = None
            return reconciled
        if result.status == "replayed_rolled_back":
            return self._reconcile_canonical_result(
                pending,
                store_status=result.status,
                replayed=True,
            )
        if not _confirmed_apply(result):
            self._pending = None
            return _problem_result(
                "COMMIT_NOT_CONFIRMED",
                "repository did not confirm the exact schedule patch",
                details={
                    "store_problems": [
                        problem.to_dict() for problem in result.problems
                    ]
                },
                pending=pending,
                store_status=result.status,
                current_revision=result.current_revision or current_revision,
                extra_problems=tuple(
                    _store_problem(problem) for problem in result.problems
                ),
            )

        return self._reconcile_canonical_result(
            pending,
            store_status=result.status,
            replayed=result.status == "replayed",
        )

    def _apply_pending_patch(
        self,
        pending: _PendingScheduleReview,
        approvals: Sequence[ApprovalGrant],
    ) -> StoreResult:
        if self._commit_attempts >= 2:
            raise RuntimeError("schedule commit write-attempt limit exhausted")
        self._commit_attempts += 1
        return self._repository.apply_patch(
            pending.patch,
            approvals,
            evaluation_at=pending.problem.evaluation_at,
        )

    def _reconcile_canonical_result(
        self,
        pending: _PendingScheduleReview,
        *,
        store_status: str,
        replayed: bool,
        ambiguity_message: str | None = None,
    ) -> ScheduleCommitResult:
        """Confirm outcome from canonical state and receipt, never from an ACK."""

        try:
            observed_plan = self._repository.load_plan()
        except Exception as exc:
            uncertain = _problem_result(
                "COMMIT_OUTCOME_UNKNOWN",
                ambiguity_message or str(exc),
                pending=pending,
                state=ScheduleStageState.OUTCOME_UNKNOWN,
                store_status=store_status,
            )
            if self._commit_attempts >= 2:
                self._pending = None
            return uncertain
        current_revision = _plan_revision(observed_plan)
        observed_state, canonical_problem = _canonical_plan_state(
            observed_plan,
            expected_trip_id=pending.patch.trip_id,
        )
        if canonical_problem is not None or observed_state is None:
            uncertain = _problem_result(
                "COMMIT_OUTCOME_UNKNOWN",
                (
                    ambiguity_message
                    or "canonical schedule state could not be reconciled"
                ),
                details={
                    "reconciliation_problem": (
                        canonical_problem.to_dict()
                        if canonical_problem is not None
                        else None
                    )
                },
                pending=pending,
                state=ScheduleStageState.OUTCOME_UNKNOWN,
                store_status=store_status,
                current_revision=current_revision,
                extra_problems=(
                    (canonical_problem,)
                    if canonical_problem is not None
                    else ()
                ),
            )
            if self._commit_attempts >= 2:
                self._pending = None
            return uncertain
        receipt, receipt_problem = _inspect_patch_receipt(
            observed_plan,
            pending.patch,
        )
        if receipt_problem is not None:
            uncertain = _problem_result(
                "COMMIT_OUTCOME_UNKNOWN",
                (
                    ambiguity_message
                    or "canonical schedule receipt could not be reconciled"
                ),
                details={
                    "reconciliation_problem": receipt_problem.to_dict(),
                },
                pending=pending,
                state=ScheduleStageState.OUTCOME_UNKNOWN,
                store_status=store_status,
                current_revision=current_revision,
                extra_problems=(receipt_problem,),
            )
            if self._commit_attempts >= 2:
                self._pending = None
            return uncertain
        if receipt is None:
            uncertain = _problem_result(
                "COMMIT_OUTCOME_UNKNOWN",
                (
                    ambiguity_message
                    or "canonical state has no receipt for the exact schedule patch"
                ),
                pending=pending,
                state=ScheduleStageState.OUTCOME_UNKNOWN,
                store_status=store_status,
                current_revision=current_revision,
            )
            if self._commit_attempts >= 2:
                self._pending = None
            return uncertain
        if receipt.status == "rolled_back":
            self._pending = None
            return _problem_result(
                "SCHEDULE_PATCH_ROLLED_BACK",
                "this exact schedule patch was applied and later rolled back",
                pending=pending,
                store_status=store_status,
                current_revision=current_revision,
            )
        self._pending = None
        return _confirmed_result(
            pending,
            store_status=store_status,
            observed_plan=observed_plan,
            observed_state=observed_state,
            receipt=receipt,
            replayed=replayed,
            evidence_source=self._evidence_source,
        )

    def _preview_exact_patch(
        self,
        patch: PlanPatch,
        approvals: Sequence[ApprovalGrant],
        *,
        evaluation_at: datetime,
    ) -> StoreResult | None:
        try:
            return self._repository.preview_patch(
                patch,
                approvals,
                evaluation_at=evaluation_at,
            )
        except Exception:
            return None


def _rebuild_current_problem(
    plan: Mapping[str, Any],
    original: ScheduleProblem,
    *,
    evidence_source: EvidenceSource | None = None,
) -> tuple[ScheduleProblem | None, ScheduleStageProblem | None]:
    try:
        if original.evidence_binding is None:
            current = schedule_problem_from_plan(
                plan,
                evaluation_at=original.evaluation_at,
                scope=original.scope,
                preferences=original.preferences,
                limits=original.limits,
            )
        else:
            if evidence_source is None:
                return None, ScheduleStageProblem(
                    "EVIDENCE_SOURCE_REQUIRED",
                    (
                        "evidence-bound scheduling requires a current "
                        "EvidenceSource"
                    ),
                )
            snapshot = evidence_source.load().snapshot(
                evaluation_at=original.evaluation_at
            )
            if type(snapshot) is not EvidenceSnapshot:
                raise TypeError(
                    "EvidenceSource load result must return EvidenceSnapshot"
                )
            composed = compose_trip_state(plan, snapshot)
            current = schedule_problem_from_composed(
                composed,
                scope=original.scope,
                preferences=original.preferences,
                limits=original.limits,
            )
    except (ScheduleContractError, TypeError, ValueError) as exc:
        return None, ScheduleStageProblem(
            getattr(
                exc,
                "code",
                (
                    "EVIDENCE_COMPOSITION_FAILED"
                    if original.evidence_binding is not None
                    else "MALFORMED_CANONICAL_PLAN"
                ),
            ),
            (
                "current evidence could not be composed safely"
                if original.evidence_binding is not None
                else str(exc)
            ),
        )
    except Exception:
        return None, ScheduleStageProblem(
            (
                "EVIDENCE_READ_FAILED"
                if original.evidence_binding is not None
                else "MALFORMED_CANONICAL_PLAN"
            ),
            (
                "current evidence could not be loaded safely"
                if original.evidence_binding is not None
                else "canonical plan could not be loaded safely"
            ),
        )
    return current, None


def _schedule_problem_drift(
    expected: ScheduleProblem,
    current: ScheduleProblem | None,
) -> tuple[str, str]:
    if (
        current is not None
        and expected.base_revision == current.base_revision
        and expected.canonical_state_digest
        == current.canonical_state_digest
        and expected.evidence_binding is not None
        and current.evidence_binding is not None
        and expected.evidence_binding.binding_digest
        != current.evidence_binding.binding_digest
    ):
        return (
            "EVIDENCE_REVISION_CHANGED",
            "evidence changed after the schedule problem was created",
        )
    return (
        "STALE_SCHEDULE_PROBLEM",
        "canonical state no longer matches the schedule problem",
    )


def _assess_strict_improvement(
    problem: ScheduleProblem,
    candidate: ScheduleCandidate,
    *,
    final_state: TripState | None = None,
) -> _ScheduleAssessment:
    final = final_state or replay_schedule_candidate(problem, candidate)
    baseline_report = evaluate_schedule_state(problem, problem.state)
    baseline_score = score_schedule(
        problem,
        problem.state,
        baseline_report,
    )
    baseline_assignments = assignments_from_state(
        problem,
        problem.state,
        baseline_report,
    )
    baseline_schedule_key = schedule_key(baseline_assignments, ())
    baseline_key = baseline_score.objective_key()
    candidate_report = evaluate_schedule_state(problem, final)
    candidate_score = score_schedule(
        problem,
        final,
        candidate_report,
        promoted_activity_ids=candidate.promoted_activity_ids,
    )
    candidate_key = candidate_score.objective_key()
    if not candidate_key < baseline_key:
        raise ScheduleContractError(
            "NO_STRICT_SCHEDULE_IMPROVEMENT",
            "candidate does not strictly improve the canonical baseline score",
        )
    decisive_index = next(
        index
        for index, (candidate_value, baseline_value) in enumerate(
            zip(candidate_key, baseline_key, strict=True)
        )
        if candidate_value != baseline_value
    )
    return _ScheduleAssessment(
        final_state=final,
        baseline_report=baseline_report,
        baseline_score=baseline_score,
        candidate_score=candidate_score,
        baseline_schedule_key=baseline_schedule_key,
        candidate_key=candidate_key,
        baseline_key=baseline_key,
        decisive_objective=_OBJECTIVE_NAMES[decisive_index],
    )


def _preview_problem(
    result: StoreResult,
    *,
    allow_approval_required: bool,
) -> ScheduleStageProblem | None:
    if allow_approval_required and _is_store_approval_required(result):
        if result.draft is None or result.required_approval_scope is None:
            return ScheduleStageProblem(
                "MALFORMED_APPROVAL_CHECKPOINT",
                "repository requested approval without an exact draft scope",
            )
        return None
    if (
        result.success
        and result.status == "preview_ready"
        and result.changed
        and result.draft is not None
        and result.candidate_plan is not None
    ):
        return None
    if result.problems:
        return _store_problem(result.problems[0])
    return ScheduleStageProblem(
        "PREVIEW_NOT_APPLICABLE",
        "repository preview did not produce an applicable schedule candidate",
        {
            "store_status": result.status,
            "changed": result.changed,
        },
    )


def _validate_preview_contract(
    current_plan: Mapping[str, Any],
    patch: PlanPatch,
    approvals: Sequence[ApprovalGrant],
    result: StoreResult,
    *,
    problem: ScheduleProblem,
) -> ScheduleStageProblem | None:
    """Verify a repository preview against the trusted pure mutation engine."""

    try:
        expected_draft = apply_patch_to_plan(
            current_plan,
            patch,
            approvals=approvals,
        )
        expected_digest = patch_digest(patch)
        current_revision = _plan_revision(current_plan)
        current_generation = current_plan.get("generation")
        if (
            current_revision is None
            or isinstance(current_generation, bool)
            or not isinstance(current_generation, int)
            or current_generation < 0
        ):
            raise ValueError("canonical plan identity is incomplete")
    except (TypeError, ValueError) as exc:
        return ScheduleStageProblem(
            "PREVIEW_CONTRACT_VALIDATION_FAILED",
            str(exc),
        )

    mismatches: list[str] = []
    for observed, expected, name in (
        (result.action, "patch", "action"),
        (result.trip_id, patch.trip_id, "trip_id"),
        (result.base_revision, patch.base_revision, "base_revision"),
        (result.current_revision, current_revision, "current_revision"),
        (
            result.required_approval_scope,
            expected_draft.required_approval_scope,
            "required_approval_scope",
        ),
    ):
        if observed != expected:
            mismatches.append(name)
    if not result.dry_run:
        mismatches.append("dry_run")
    if (
        type(result.draft) is not type(expected_draft)
        or result.draft != expected_draft
    ):
        mismatches.append("draft")
    elif result.draft.patch_digest != expected_digest:
        mismatches.append("draft.patch_digest")

    expected_candidate = expected_draft.to_plan_dict()
    state_changed = (
        canonical_json_bytes(expected_candidate.get("state"))
        != canonical_json_bytes(current_plan.get("state"))
    )
    approval_blocked = bool(expected_draft.problems)
    if approval_blocked:
        expected_candidate = deep_copy_json(current_plan)
        expected_applied_revision = None
        expected_generation = current_generation
        expected_report = None
    elif state_changed:
        expected_candidate["generation"] = current_generation + 1
        expected_candidate["revision"] = compute_revision(expected_candidate)
        expected_candidate["receipts"] = deep_copy_json(
            current_plan.get("receipts", {})
        )
        expected_applied_revision = expected_candidate["revision"]
        expected_generation = current_generation + 1
        try:
            expected_state = plan_to_trip_state(expected_candidate)
            expected_report = evaluate_schedule_state(problem, expected_state)
        except (TypeError, ValueError) as exc:
            return ScheduleStageProblem(
                "PREVIEW_CONTRACT_VALIDATION_FAILED",
                str(exc),
            )
    else:
        expected_candidate = deep_copy_json(current_plan)
        expected_applied_revision = current_revision
        expected_generation = current_generation
        try:
            expected_state = plan_to_trip_state(expected_candidate)
            expected_report = evaluate_schedule_state(problem, expected_state)
        except (TypeError, ValueError) as exc:
            return ScheduleStageProblem(
                "PREVIEW_CONTRACT_VALIDATION_FAILED",
                str(exc),
            )

    candidate_plan = result.mutable_candidate_plan()
    if candidate_plan is None or (
        canonical_json_bytes(candidate_plan)
        != canonical_json_bytes(expected_candidate)
    ):
        mismatches.append("candidate_plan")
    for observed, expected, name in (
        (
            result.applied_revision,
            expected_applied_revision,
            "applied_revision",
        ),
        (result.generation, expected_generation, "generation"),
        (
            result.check_status,
            (
                expected_report.status.value
                if expected_report is not None
                else None
            ),
            "check_status",
        ),
    ):
        if observed != expected:
            mismatches.append(name)
    if expected_report is None:
        if result.check_report is not None:
            mismatches.append("check_report")
    elif (
        type(result.check_report) is not CheckReport
        or result.check_report != expected_report
    ):
        mismatches.append("check_report")

    if mismatches:
        return ScheduleStageProblem(
            "PREVIEW_CONTRACT_MISMATCH",
            "repository preview does not match the trusted canonical effect",
            {
                "mismatched_fields": sorted(set(mismatches)),
                "patch_digest": expected_digest,
            },
        )
    return None


def _verify_store_projection(
    preview: StoreResult,
    expected_state: TripState,
) -> ScheduleStageProblem | None:
    candidate_plan = preview.mutable_candidate_plan()
    if candidate_plan is None:
        return ScheduleStageProblem(
            "MALFORMED_PREVIEW",
            "successful schedule preview omitted its canonical candidate",
        )
    try:
        observed_state = plan_to_trip_state(candidate_plan)
    except (TypeError, ValueError) as exc:
        return ScheduleStageProblem(
            "MALFORMED_PREVIEW",
            str(exc),
        )
    if _schedule_state_key(observed_state) != _schedule_state_key(expected_state):
        return ScheduleStageProblem(
            "SCHEDULE_PROJECTION_MISMATCH",
            "store preview does not reproduce candidate placement, time, and decision state",
        )
    return None


def _preview_state_digest(preview: StoreResult) -> str | None:
    if preview.draft is None:
        return None
    state = preview.draft.plan.get("state")
    if not isinstance(state, Mapping):
        return None
    return "sha256:" + hashlib.sha256(
        canonical_json_bytes(state)
    ).hexdigest()


def _expected_applied_revision(
    current_plan: Mapping[str, Any],
    preview: StoreResult,
) -> str | None:
    if preview.draft is None:
        return None
    current_generation = current_plan.get("generation")
    current_revision = _plan_revision(current_plan)
    if (
        isinstance(current_generation, bool)
        or not isinstance(current_generation, int)
        or current_generation < 0
        or current_revision is None
    ):
        return None
    candidate = preview.draft.to_plan_dict()
    if (
        canonical_json_bytes(candidate.get("state"))
        == canonical_json_bytes(current_plan.get("state"))
    ):
        return current_revision
    candidate["generation"] = current_generation + 1
    candidate["receipts"] = deep_copy_json(current_plan.get("receipts", {}))
    try:
        return compute_revision(candidate)
    except (TypeError, ValueError):
        return None


def _semantic_state_digest(plan: Mapping[str, Any]) -> str | None:
    state = plan.get("state")
    if not isinstance(state, Mapping):
        return None
    return "sha256:" + hashlib.sha256(
        canonical_json_bytes(state)
    ).hexdigest()


def _schedule_state_key(state: TripState) -> tuple[Any, ...]:
    activity_by_id = state.activity_by_id
    days = tuple(
        (
            day.day_id,
            tuple(day.activity_ids),
        )
        for day in sorted(state.days, key=lambda item: (item.date, item.day_id))
    )
    activities = tuple(
        (
            activity.activity_id,
            activity.day_id,
            activity.order,
            activity.decision_state.value,
            (
                activity.scheduled_start.isoformat()
                if activity.scheduled_start is not None
                else None
            ),
        )
        for activity in sorted(
            activity_by_id.values(), key=lambda item: item.activity_id
        )
    )
    return days, activities


def _review_id(
    run_id: str,
    problem: ScheduleProblem,
    candidate: ScheduleCandidate,
    *,
    request_digest: str,
    expected_state_digest: str,
    expected_applied_revision: str,
    assessment: _ScheduleAssessment,
    changes: tuple[ChangeRecord, ...],
    protected_changes: tuple[ChangeRecord, ...],
    affected_day_ids: tuple[str, ...],
    invalidated_day_ids: tuple[str, ...],
    risk_codes: tuple[str, ...],
    required_approval_scope: str | None,
    post_preview_status: str | None,
) -> str:
    digest = hashlib.sha256(
        b"trip-planner.schedule-review/v1\0"
        + canonical_json_bytes(
            {
                "run_id": run_id,
                "problem_id": problem.problem_id,
                "candidate_id": candidate.candidate_id,
                "patch_digest": request_digest,
                "expected_state_digest": expected_state_digest,
                "expected_applied_revision": expected_applied_revision,
                "baseline_key": list(assessment.baseline_key),
                "candidate_key": list(assessment.candidate_key),
                "baseline_schedule_key": assessment.baseline_schedule_key,
                "candidate_schedule_key": candidate.schedule_key,
                "decisive_objective": assessment.decisive_objective,
                "changes": [change.to_dict() for change in changes],
                "protected_changes": [
                    change.to_dict() for change in protected_changes
                ],
                "affected_day_ids": list(affected_day_ids),
                "invalidated_day_ids": list(invalidated_day_ids),
                "risk_codes": list(risk_codes),
                "required_approval_scope": required_approval_scope,
                "post_preview_status": post_preview_status,
            }
        )
    ).hexdigest()
    return f"schedule-review-{digest}"


def _confirmed_result(
    pending: _PendingScheduleReview,
    *,
    store_status: str,
    observed_plan: Mapping[str, Any],
    observed_state: TripState,
    receipt: _PatchReceipt,
    replayed: bool,
    evidence_source: EvidenceSource | None,
) -> ScheduleCommitResult:
    current_revision = _plan_revision(observed_plan)
    revision_is_current = (
        current_revision is not None
        and current_revision == receipt.applied_revision
    )
    expected_revision_matches = (
        receipt.applied_revision == pending.expected_applied_revision
    )
    candidate_is_current = False
    report: CheckReport | None = None
    problems: tuple[ScheduleStageProblem, ...] = ()
    evidence_problem = False
    if not expected_revision_matches:
        problems = (
            ScheduleStageProblem(
                "COMMIT_REVISION_DIVERGED",
                (
                    "canonical receipt applied revision does not match the "
                    "complete reviewed candidate revision"
                ),
                {
                    "expected_applied_revision": (
                        pending.expected_applied_revision
                    ),
                    "observed_applied_revision": receipt.applied_revision,
                },
            ),
        )
    if revision_is_current:
        canonical_report = evaluate_schedule_state(
            pending.problem, observed_state
        )
        report = canonical_report
        state_digest_matches = (
            _semantic_state_digest(observed_plan)
            == pending.expected_state_digest
        )
        schedule_matches = (
            _schedule_state_key(observed_state)
            == pending.expected_schedule_key
        )
        if not state_digest_matches or not schedule_matches:
            problems += (
                ScheduleStageProblem(
                    "COMMIT_STATE_DIVERGED",
                    (
                        "persisted semantic state does not reproduce the "
                        "complete reviewed candidate effect"
                    ),
                    {
                        "state_digest_matches": state_digest_matches,
                        "schedule_key_matches": schedule_matches,
                    },
                ),
            )
        elif expected_revision_matches:
            candidate_is_current = True
        if (
            receipt.check_status is not None
            and canonical_report.status.value != receipt.check_status
        ):
            problems += (
                ScheduleStageProblem(
                    "POST_COMMIT_REPORT_MISMATCH",
                    "persisted report does not match the canonical receipt",
                    {
                        "receipt_status": receipt.check_status,
                        "observed_status": canonical_report.status.value,
                    },
                ),
            )
        if pending.problem.evidence_binding is not None:
            try:
                if evidence_source is None:
                    raise ValueError(
                        "current evidence source is unavailable"
                    )
                evidence_snapshot = evidence_source.load().snapshot(
                    evaluation_at=pending.problem.evaluation_at
                )
                if type(evidence_snapshot) is not EvidenceSnapshot:
                    raise TypeError(
                        "EvidenceSource load result must return "
                        "EvidenceSnapshot"
                    )
                if (
                    evidence_snapshot.evaluation_at
                    != pending.problem.evaluation_at
                ):
                    raise ValueError(
                        "evidence source returned a different evaluation time"
                    )
                composed = compose_trip_state(
                    observed_plan, evidence_snapshot
                )
                current_problem = schedule_problem_from_composed(
                    composed,
                    scope=pending.problem.scope,
                    preferences=pending.problem.preferences,
                    limits=pending.problem.limits,
                )
                availability_matches = (
                    current_problem.activity_availability
                    == pending.problem.activity_availability
                )
                report = evaluate_schedule_state(
                    current_problem,
                    composed.state,
                )
                if (
                    composed.evidence.binding_digest
                    != pending.problem.evidence_binding.binding_digest
                    or not availability_matches
                ):
                    evidence_problem = True
                    candidate_is_current = False
                    problems += (
                        ScheduleStageProblem(
                            "EVIDENCE_REVISION_CHANGED",
                            (
                                "canonical schedule commit succeeded, but "
                                "the reviewed evidence is no longer current"
                            ),
                        ),
                    )
            except Exception:
                evidence_problem = True
                candidate_is_current = False
                problems += (
                    ScheduleStageProblem(
                        "EVIDENCE_READ_FAILED",
                        (
                            "canonical schedule commit succeeded, but "
                            "current evidence could not be composed safely"
                        ),
                    ),
                )
        if report.status is CheckStatus.INFEASIBLE:
            candidate_is_current = False
            problems += (
                ScheduleStageProblem(
                    "POST_COMMIT_PLAN_INFEASIBLE",
                    "persisted schedule is infeasible after confirmed commit",
                ),
            )
    if evidence_problem and all(
        problem.code
        in {
            "EVIDENCE_REVISION_CHANGED",
            "EVIDENCE_READ_FAILED",
            "POST_COMMIT_PLAN_INFEASIBLE",
        }
        for problem in problems
    ):
        state = ScheduleStageState.WAITING_EXTERNAL
    elif problems:
        candidate_is_current = False
        state = ScheduleStageState.OUTCOME_UNKNOWN
    elif not revision_is_current:
        state = ScheduleStageState.REPLAY_CONFIRMED
    elif report is not None and report.status is CheckStatus.NEEDS_VERIFICATION:
        state = ScheduleStageState.WAITING_EXTERNAL
    elif report is not None and report.status is CheckStatus.FEASIBLE:
        state = ScheduleStageState.APPLIED
    else:
        state = ScheduleStageState.WAITING_EXTERNAL
    return ScheduleCommitResult(
        state=state,
        applied=True,
        review_id=pending.review.review_id,
        problem_id=pending.problem.problem_id,
        candidate_id=pending.candidate.candidate_id,
        store_status=store_status,
        transaction_id=receipt.transaction_id,
        applied_revision=receipt.applied_revision,
        current_revision=current_revision,
        replayed=(
            replayed or state is ScheduleStageState.REPLAY_CONFIRMED
        ),
        candidate_is_current=candidate_is_current,
        check_report=report,
        required_arc_keys=_required_arcs_for_days(
            pending.candidate.required_arc_keys,
            pending.review.invalidated_day_ids,
        ),
        affected_day_ids=pending.review.affected_day_ids,
        invalidated_day_ids=pending.review.invalidated_day_ids,
        change_count=pending.review.change_count,
        changes=pending.review.changes,
        protected_changes=pending.review.protected_changes,
        problems=problems,
    )


def _problem_review(
    problem: ScheduleProblem,
    candidate: ScheduleCandidate,
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] = MappingProxyType({}),
    current_revision: str | None = None,
) -> ScheduleStageReview:
    return ScheduleStageReview(
        state=ScheduleStageState.REJECTED,
        problem_id=problem.problem_id,
        candidate_id=candidate.candidate_id,
        base_revision=problem.base_revision,
        evidence_binding_digest=(
            problem.evidence_binding.binding_digest
            if problem.evidence_binding is not None
            else None
        ),
        current_revision=current_revision,
        problems=(ScheduleStageProblem(code, message, details),),
    )


def _problem_result(
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] = MappingProxyType({}),
    pending: _PendingScheduleReview | None = None,
    review_id: str | None = None,
    state: ScheduleStageState = ScheduleStageState.REJECTED,
    store_status: str | None = None,
    current_revision: str | None = None,
    extra_problems: tuple[ScheduleStageProblem, ...] = (),
) -> ScheduleCommitResult:
    return ScheduleCommitResult(
        state=state,
        applied=False,
        review_id=(
            pending.review.review_id if pending is not None else review_id
        ),
        problem_id=(
            pending.problem.problem_id if pending is not None else None
        ),
        candidate_id=(
            pending.candidate.candidate_id if pending is not None else None
        ),
        store_status=store_status,
        current_revision=current_revision,
        required_arc_keys=(
            pending.candidate.required_arc_keys if pending is not None else ()
        ),
        affected_day_ids=(
            pending.review.affected_day_ids if pending is not None else ()
        ),
        invalidated_day_ids=(
            pending.review.invalidated_day_ids if pending is not None else ()
        ),
        problems=(
            ScheduleStageProblem(code, message, details),
            *extra_problems,
        ),
    )


def _schedule_risks(
    *,
    change_count: int,
    max_auto_changes: int,
) -> tuple[str, ...]:
    return (
        ("LARGE_PERSISTENT_DIFF",)
        if change_count > max_auto_changes
        else ()
    )


def _required_arcs_for_days(
    required_arc_keys: Sequence[str],
    day_ids: Sequence[str],
) -> tuple[str, ...]:
    target_days = frozenset(day_ids)
    selected: list[str] = []
    for arc_key in required_arc_keys:
        try:
            value = json.loads(arc_key)
        except (TypeError, ValueError):
            continue
        if (
            isinstance(value, dict)
            and value.get("day_id") in target_days
        ):
            selected.append(arc_key)
    return tuple(selected)


def _changes(result: StoreResult) -> tuple[ChangeRecord, ...]:
    return result.draft.changes if result.draft is not None else ()


def _invalidated_day_ids(result: StoreResult) -> tuple[str, ...]:
    return (
        result.draft.invalidated_day_ids
        if result.draft is not None
        else ()
    )


def _affected_day_ids(result: StoreResult) -> tuple[str, ...]:
    return (
        result.draft.affected_day_ids
        if result.draft is not None
        else ()
    )


def _protected_changes(result: StoreResult) -> tuple[ChangeRecord, ...]:
    return (
        result.draft.protected_changes
        if result.draft is not None
        else ()
    )


def _is_store_approval_required(result: StoreResult) -> bool:
    return (
        result.required_approval_scope is not None
        and any(problem.code == "APPROVAL_REQUIRED" for problem in result.problems)
    )


def _is_exact_replay(result: StoreResult) -> bool:
    return (
        result.success
        and result.action == "patch"
        and result.status == "replayed"
        and result.replayed
        and not result.changed
    )


def _confirmed_apply(result: StoreResult) -> bool:
    return (
        result.action == "patch"
        and (
            (
                result.success
                and result.status == "applied"
                and result.changed
            )
            or _is_exact_replay(result)
        )
    )


def _store_problem(problem: StoreProblem) -> ScheduleStageProblem:
    return ScheduleStageProblem(
        problem.code,
        problem.message,
        problem.details,
    )


def _plan_revision(plan: Mapping[str, Any]) -> str | None:
    value = plan.get("revision")
    return value if isinstance(value, str) and value else None


def _plan_has_receipt(
    plan: Mapping[str, Any], idempotency_key: str
) -> bool:
    receipts = plan.get("receipts")
    return isinstance(receipts, Mapping) and idempotency_key in receipts


def _inspect_patch_receipt(
    plan: Mapping[str, Any],
    patch: PlanPatch,
) -> tuple[_PatchReceipt | None, ScheduleStageProblem | None]:
    receipts = plan.get("receipts")
    if not isinstance(receipts, Mapping):
        return None, ScheduleStageProblem(
            "MALFORMED_RECEIPTS",
            "canonical plan receipts must be an object",
        )
    raw = receipts.get(patch.idempotency_key)
    if raw is None:
        return None, None
    if not isinstance(raw, Mapping):
        return None, ScheduleStageProblem(
            "MALFORMED_SCHEDULE_RECEIPT",
            "schedule receipt must be an object",
        )
    expected_digest = patch_digest(patch)
    mismatches: list[str] = []
    for observed, expected, name in (
        (raw.get("kind"), "patch", "kind"),
        (raw.get("request_digest"), expected_digest, "request_digest"),
        (raw.get("base_revision"), patch.base_revision, "base_revision"),
    ):
        if observed != expected:
            mismatches.append(name)
    status = raw.get("status")
    if status not in {"applied", "rolled_back"}:
        mismatches.append("status")
    transaction_id = raw.get("transaction_id")
    applied_revision = raw.get("applied_revision")
    if not isinstance(transaction_id, str) or not transaction_id:
        mismatches.append("transaction_id")
    if not isinstance(applied_revision, str) or not applied_revision:
        mismatches.append("applied_revision")
    check_status = raw.get("check_status")
    if check_status is not None and (
        not isinstance(check_status, str) or not check_status
    ):
        mismatches.append("check_status")
    required_scope = raw.get("required_approval_scope")
    if required_scope is not None and (
        not isinstance(required_scope, str) or not required_scope
    ):
        mismatches.append("required_approval_scope")
    if mismatches:
        return None, ScheduleStageProblem(
            "SCHEDULE_RECEIPT_MISMATCH",
            "canonical receipt does not match the exact schedule patch",
            {
                "idempotency_key": patch.idempotency_key,
                "mismatched_fields": sorted(set(mismatches)),
            },
        )
    assert isinstance(status, str)
    assert isinstance(transaction_id, str)
    assert isinstance(applied_revision, str)
    assert check_status is None or isinstance(check_status, str)
    assert required_scope is None or isinstance(required_scope, str)
    return (
        _PatchReceipt(
            status=status,
            transaction_id=transaction_id,
            applied_revision=applied_revision,
            check_status=check_status,
            required_approval_scope=required_scope,
        ),
        None,
    )


def _canonical_plan_state(
    plan: Mapping[str, Any],
    *,
    expected_trip_id: str,
) -> tuple[TripState | None, ScheduleStageProblem | None]:
    try:
        observed_trip_id = plan.get("trip_id")
        if observed_trip_id != expected_trip_id:
            raise ValueError(
                "canonical plan trip_id does not match the schedule patch"
            )
        revision = _plan_revision(plan)
        if revision is None or compute_revision(plan) != revision:
            raise ValueError("canonical plan revision is invalid")
        return plan_to_trip_state(plan), None
    except (TypeError, ValueError) as exc:
        return None, ScheduleStageProblem(
            "CANONICAL_RECONCILIATION_FAILED",
            str(exc),
        )


def _safe_score_dict(
    score: ScheduleScore | None,
    *,
    evidence_bound: bool,
) -> dict[str, Any] | None:
    if score is None:
        return None
    if evidence_bound:
        return {
            "redacted": True,
            "digest": "sha256:"
            + hashlib.sha256(
                b"trip-planner.schedule-score/v1\0"
                + canonical_json_bytes(
                    {
                        item.name: getattr(score, item.name)
                        for item in fields(score)
                    }
                )
            ).hexdigest(),
        }
    return {
        item.name: getattr(score, item.name)
        for item in fields(score)
    }


def _is_sha256_digest(value: str) -> bool:
    payload = (
        value.removeprefix("sha256:")
        if value.startswith("sha256:")
        else value
    )
    return (
        len(payload) == 64
        and all(character in "0123456789abcdef" for character in payload)
    )


def _require_text(value: Any, name: str, *, maximum: int = 4096) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or any(
            unicodedata.category(character).startswith("C")
            for character in value
        )
    ):
        raise ValueError(
            f"{name} must be 1-{maximum} visible characters without "
            "leading/trailing whitespace or control characters"
        )


def _optional_text(value: Any, name: str) -> None:
    if value is not None:
        _require_text(value, name)


def _require_text_tuple(value: Any, name: str) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{name} must be a tuple")
    for item in value:
        _require_text(item, f"{name} item")


def _require_non_negative_int(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _safe_code(value: Any) -> str:
    text = str(value) if value is not None else ""
    normalized = "".join(
        character
        if (
            not unicodedata.category(character).startswith("C")
            and (character.isalnum() or character in "._:-")
        )
        else "_"
        for character in text.strip()
    )
    return normalized[:256] or "SCHEDULE_STAGE_ERROR"


def _safe_message(value: Any) -> str:
    text = str(value) if value is not None else ""
    without_controls = "".join(
        " "
        if unicodedata.category(character).startswith("C")
        else character
        for character in text
    )
    return (" ".join(without_controls.split()) or "Schedule staging failed.")[
        :4096
    ]


__all__ = [
    "ScheduleCommitResult",
    "ScheduleStageProblem",
    "ScheduleStageReview",
    "ScheduleStageState",
    "ScheduleStager",
]
