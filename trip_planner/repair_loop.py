"""Bounded, storage-agnostic controller for AI-authored itinerary repairs.

The controller owns budgets, snapshot freshness, semantic deduplication,
preview/commit separation, progress checks, and human checkpoints.  It never
calls a model or a provider itself, and model-authored data can never carry an
approval.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol

from .codec import (
    FrozenJsonValue,
    canonical_json_bytes,
    deep_copy_json,
    freeze_json,
)
from .facts import EvidenceSnapshot
from .models import CheckStatus, IssueSeverity
from .mutations import (
    AddActivity,
    AddConstraint,
    ApprovalGrant,
    ChangeRecord,
    PatchOperation,
    PlanPatch,
    PlaceActivity,
    RemoveActivity,
    RemoveConstraint,
    UpdateActivity,
    UpdateConstraint,
    UpdateDay,
)
from .repair import (
    BoundProposal,
    IssueOwner,
    OperationReason,
    PlannerSnapshot,
    ProposalIntent,
    RepairBudget,
    RepairContractError,
    RepairIssue,
    RepairOptionKind,
    bind_proposal,
    decode_proposal_intent,
    operation_matches_option,
    snapshot_from_plan,
)
from .store import StoreProblem, StoreResult


class PlanRepository(Protocol):
    """Minimal persistence boundary required by :class:`RepairController`."""

    def load_plan(self) -> dict[str, Any]:
        """Return one detached strict canonical plan."""

    def preview_patch(
        self,
        patch: PlanPatch,
        approvals: Sequence[ApprovalGrant] = (),
        *,
        evaluation_at: datetime | None = None,
    ) -> StoreResult:
        """Evaluate a patch without persistent writes."""

    def apply_patch(
        self,
        patch: PlanPatch,
        approvals: Sequence[ApprovalGrant] = (),
        *,
        dry_run: bool = False,
        evaluation_at: datetime | None = None,
    ) -> StoreResult:
        """Atomically apply or exactly replay a patch."""


class EvidenceLoadResult(Protocol):
    """One current evidence load that can issue an immutable snapshot."""

    def snapshot(self, *, evaluation_at: datetime) -> EvidenceSnapshot:
        """Bind the loaded evidence revision to one semantic instant."""


class EvidenceSource(Protocol):
    """Reloadable evidence boundary used to detect review-time drift."""

    def load(self) -> EvidenceLoadResult:
        """Return the source's current evidence result."""


class _EvidenceSourceFailure(RuntimeError):
    """Internal redacted wrapper; never expose provider exception text."""


class RepairState(str, Enum):
    READY = "ready"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_EXTERNAL = "waiting_external"
    COMPLETE = "complete"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class RepairProblem:
    code: str
    message: str
    details: Mapping[str, FrozenJsonValue] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "code",
            _safe_problem_code(self.code),
        )
        object.__setattr__(
            self,
            "message",
            _safe_problem_message(self.message),
        )
        frozen = freeze_json(self.details)
        if not isinstance(frozen, Mapping):
            raise TypeError("RepairProblem.details must be a mapping")
        object.__setattr__(self, "details", frozen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": deep_copy_json(self.details),
        }


@dataclass(frozen=True, slots=True)
class HumanCheckpointGrant:
    """Trusted approval for one exact controller review.

    This object is supplied out-of-band by the host application.  It is not
    part of the model-facing proposal schema.
    """

    review_id: str
    approved_by: str
    approved_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.review_id, "HumanCheckpointGrant.review_id")
        _require_text(self.approved_by, "HumanCheckpointGrant.approved_by")
        _require_aware_datetime(
            self.approved_at,
            "HumanCheckpointGrant.approved_at",
        )


@dataclass(frozen=True, slots=True)
class ProposalReview:
    """Read-only outcome of previewing one proposal."""

    state: RepairState
    snapshot: PlannerSnapshot
    review_id: str | None = None
    candidate_snapshot: PlannerSnapshot | None = None
    effect_digest: str | None = None
    attempt_digest: str | None = None
    change_count: int = 0
    changes: tuple[ChangeRecord, ...] = ()
    reasons: tuple[OperationReason, ...] = ()
    target_issue_ids: tuple[str, ...] = ()
    risk_codes: tuple[str, ...] = ()
    required_approval_scope: str | None = None
    problems: tuple[RepairProblem, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, RepairState):
            raise TypeError("ProposalReview.state must be RepairState")
        if not isinstance(self.snapshot, PlannerSnapshot):
            raise TypeError("ProposalReview.snapshot must be PlannerSnapshot")
        if self.candidate_snapshot is not None and not isinstance(
            self.candidate_snapshot, PlannerSnapshot
        ):
            raise TypeError(
                "ProposalReview.candidate_snapshot must be PlannerSnapshot"
            )
        _optional_text(self.review_id, "ProposalReview.review_id")
        _optional_text(self.effect_digest, "ProposalReview.effect_digest")
        _optional_text(self.attempt_digest, "ProposalReview.attempt_digest")
        _optional_text(
            self.required_approval_scope,
            "ProposalReview.required_approval_scope",
        )
        _require_non_negative_int(self.change_count, "change_count")
        if not isinstance(self.changes, tuple):
            raise TypeError("ProposalReview.changes must be a tuple")
        if any(not isinstance(item, ChangeRecord) for item in self.changes):
            raise TypeError(
                "ProposalReview.changes must contain ChangeRecord values"
            )
        if not isinstance(self.reasons, tuple):
            raise TypeError("ProposalReview.reasons must be a tuple")
        if any(not isinstance(item, OperationReason) for item in self.reasons):
            raise TypeError(
                "ProposalReview.reasons must contain OperationReason values"
            )
        if self.change_count != len(self.changes):
            raise ValueError(
                "ProposalReview.change_count must equal the diff length"
            )
        _require_text_tuple(
            self.target_issue_ids,
            "ProposalReview.target_issue_ids",
        )
        _require_text_tuple(self.risk_codes, "ProposalReview.risk_codes")
        if not isinstance(self.problems, tuple):
            raise TypeError("ProposalReview.problems must be a tuple")
        if any(not isinstance(item, RepairProblem) for item in self.problems):
            raise TypeError(
                "ProposalReview.problems must contain RepairProblem values"
            )

    @property
    def ready_to_commit(self) -> bool:
        return (
            self.review_id is not None
            and not self.problems
            and self.state in {
                RepairState.READY,
                RepairState.WAITING_APPROVAL,
            }
        )

    @property
    def requires_human_checkpoint(self) -> bool:
        return bool(self.risk_codes)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "state": self.state.value,
            "review_id": self.review_id,
            "snapshot_id": self.snapshot.snapshot_id,
            "candidate_snapshot_id": (
                self.candidate_snapshot.snapshot_id
                if self.candidate_snapshot is not None
                else None
            ),
            "effect_digest": self.effect_digest,
            "attempt_digest": self.attempt_digest,
            "change_count": self.change_count,
            "changes": [change.to_dict() for change in self.changes],
            "reasons": [reason.to_dict() for reason in self.reasons],
            "target_issue_ids": list(self.target_issue_ids),
            "risk_codes": list(self.risk_codes),
            "required_approval_scope": self.required_approval_scope,
            "problems": [problem.to_dict() for problem in self.problems],
        }
        return result


@dataclass(frozen=True, slots=True)
class RepairResult:
    """Outcome of committing a previously reviewed proposal."""

    state: RepairState
    snapshot: PlannerSnapshot
    applied: bool
    review_id: str | None = None
    store_status: str | None = None
    change_count: int = 0
    changes: tuple[ChangeRecord, ...] = ()
    reasons: tuple[OperationReason, ...] = ()
    problems: tuple[RepairProblem, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, RepairState):
            raise TypeError("RepairResult.state must be RepairState")
        if not isinstance(self.snapshot, PlannerSnapshot):
            raise TypeError("RepairResult.snapshot must be PlannerSnapshot")
        if not isinstance(self.applied, bool):
            raise TypeError("RepairResult.applied must be bool")
        _optional_text(self.review_id, "RepairResult.review_id")
        _optional_text(self.store_status, "RepairResult.store_status")
        _require_non_negative_int(self.change_count, "change_count")
        if not isinstance(self.changes, tuple):
            raise TypeError("RepairResult.changes must be a tuple")
        if any(not isinstance(item, ChangeRecord) for item in self.changes):
            raise TypeError(
                "RepairResult.changes must contain ChangeRecord values"
            )
        if not isinstance(self.reasons, tuple):
            raise TypeError("RepairResult.reasons must be a tuple")
        if any(not isinstance(item, OperationReason) for item in self.reasons):
            raise TypeError(
                "RepairResult.reasons must contain OperationReason values"
            )
        if self.applied and self.change_count != len(self.changes):
            raise ValueError(
                "applied RepairResult change_count must equal the diff length"
            )
        if not isinstance(self.problems, tuple):
            raise TypeError("RepairResult.problems must be a tuple")
        if any(not isinstance(item, RepairProblem) for item in self.problems):
            raise TypeError(
                "RepairResult.problems must contain RepairProblem values"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "snapshot_id": self.snapshot.snapshot_id,
            "applied": self.applied,
            "review_id": self.review_id,
            "store_status": self.store_status,
            "change_count": self.change_count,
            "changes": [change.to_dict() for change in self.changes],
            "reasons": [reason.to_dict() for reason in self.reasons],
            "problems": [problem.to_dict() for problem in self.problems],
        }


@dataclass(frozen=True, slots=True)
class ProviderCallReservation:
    allowed: bool
    cached: bool
    remaining_provider_calls: int
    problem: RepairProblem | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool) or not isinstance(
            self.cached, bool
        ):
            raise TypeError("provider reservation flags must be bool")
        _require_non_negative_int(
            self.remaining_provider_calls,
            "remaining_provider_calls",
        )
        if self.problem is not None and not isinstance(
            self.problem, RepairProblem
        ):
            raise TypeError("problem must be RepairProblem or None")


@dataclass(frozen=True, slots=True)
class _PendingReview:
    review: ProposalReview
    bound: BoundProposal
    base_snapshot: PlannerSnapshot
    evidence_snapshot: EvidenceSnapshot | None


class RepairController:
    """One bounded in-memory repair run over an abstract plan repository."""

    def __init__(
        self,
        repository: PlanRepository,
        *,
        run_id: str,
        evaluation_at: datetime,
        budget: RepairBudget | None = None,
        evidence_source: EvidenceSource | None = None,
    ) -> None:
        _require_text(run_id, "run_id")
        _require_aware_datetime(evaluation_at, "evaluation_at")
        self._repository = repository
        self.run_id = run_id
        self.evaluation_at = evaluation_at.astimezone(timezone.utc)
        self.budget = budget or RepairBudget()
        if not isinstance(self.budget, RepairBudget):
            raise TypeError("budget must be RepairBudget")
        if evidence_source is not None and not callable(
            getattr(evidence_source, "load", None)
        ):
            raise TypeError("evidence_source must provide load() or be None")
        self._evidence_source = evidence_source
        self._iterations_used = 0
        self._provider_calls_used = 0
        self._changes_used = 0
        self._seen_attempts: set[str] = set()
        self._visited_states: set[str] = set()
        self._provider_fingerprints: dict[str, bool] = {}
        self._snapshot: PlannerSnapshot | None = None
        self._pending: _PendingReview | None = None
        self._terminal_problem: RepairProblem | None = None
        self._state = RepairState.READY

    @property
    def state(self) -> RepairState:
        return self._state

    @property
    def remaining_iterations(self) -> int:
        return max(0, self.budget.max_iterations - self._iterations_used)

    @property
    def remaining_provider_calls(self) -> int:
        return max(
            0,
            self.budget.max_provider_calls - self._provider_calls_used,
        )

    @property
    def remaining_changes(self) -> int:
        return max(0, self.budget.max_changes - self._changes_used)

    def inspect(self) -> PlannerSnapshot:
        """Read the canonical state and issue a fresh model-facing snapshot."""

        evidence_snapshot = self._load_evidence_snapshot()
        snapshot = self._load_snapshot(
            evidence_snapshot=evidence_snapshot,
        )
        self._adopt_snapshot(snapshot, clear_pending_on_change=True)
        return snapshot

    def submit(
        self,
        snapshot_id: str,
        proposal: ProposalIntent | bytes | bytearray | memoryview | str,
    ) -> ProposalReview:
        """Consume one model iteration, strictly decode, bind, and preview."""

        if self._snapshot is None:
            self.inspect()
        assert self._snapshot is not None
        model_snapshot = self._snapshot

        if self._terminal_problem is not None:
            self._state = RepairState.STOPPED
            return self._problem_review(
                self._terminal_problem.code,
                self._terminal_problem.message,
                details=self._terminal_problem.details,
            )
        if self._pending is not None:
            return self._problem_review(
                "PENDING_REVIEW",
                "commit or cancel the current review before submitting another",
            )
        if self.remaining_iterations == 0:
            self._state = RepairState.STOPPED
            return self._problem_review(
                "ITERATION_BUDGET_EXHAUSTED",
                "the repair iteration budget is exhausted",
            )

        # A malformed or stale model response still consumes an iteration.
        self._iterations_used += 1
        try:
            evidence_snapshot = self._load_evidence_snapshot()
            current = self._load_snapshot(
                evidence_snapshot=evidence_snapshot,
            )
        except _EvidenceSourceFailure:
            self._state = RepairState.WAITING_EXTERNAL
            return self._problem_review(
                "EVIDENCE_SOURCE_FAILED",
                "provider evidence snapshot is unavailable",
                snapshot=model_snapshot,
            )
        except Exception as exc:
            self._state = RepairState.STOPPED
            return self._problem_review(
                "REPOSITORY_READ_FAILED",
                str(exc),
                snapshot=model_snapshot,
            )
        _, repeated_external_state = self._adopt_snapshot(
            current,
            previous=model_snapshot,
            clear_pending_on_change=True,
        )

        evidence_changed = _evidence_binding_changed(
            current,
            model_snapshot,
        )
        if evidence_changed:
            if not repeated_external_state:
                self._state = self._state_for_snapshot(current)
            return self._problem_review(
                "EVIDENCE_REVISION_CHANGED",
                "provider evidence changed after the proposal snapshot",
                details=_evidence_change_details(
                    expected=model_snapshot,
                    current=current,
                ),
            )
        if (
            snapshot_id != model_snapshot.snapshot_id
            or current.revision != model_snapshot.revision
            or current.state_digest != model_snapshot.state_digest
        ):
            if not repeated_external_state:
                self._state = self._state_for_snapshot(current)
            return self._problem_review(
                "STALE_SNAPSHOT",
                "proposal snapshot no longer matches canonical state",
                details={
                    "submitted_snapshot_id": (
                        snapshot_id if isinstance(snapshot_id, str) else None
                    ),
                    "expected_snapshot_id": model_snapshot.snapshot_id,
                    "current_snapshot_id": current.snapshot_id,
                },
            )

        try:
            intent = (
                proposal
                if isinstance(proposal, ProposalIntent)
                else decode_proposal_intent(proposal)
            )
            bound = bind_proposal(model_snapshot, intent, run_id=self.run_id)
        except (RepairContractError, TypeError, ValueError) as exc:
            self._state = self._state_for_snapshot(current)
            return self._problem_review(
                getattr(exc, "code", "MALFORMED_PROPOSAL"),
                str(exc),
                details={
                    "path": getattr(exc, "path", None),
                },
            )

        if bound.attempt_digest in self._seen_attempts:
            self._state = self._state_for_snapshot(current)
            return self._problem_review(
                "REPEATED_ATTEMPT",
                "the same semantic effect was already attempted on this state",
                details={"attempt_digest": bound.attempt_digest},
            )
        try:
            preview = self._repository.preview_patch(
                bound.patch,
                (),
                evaluation_at=self.evaluation_at,
            )
        except Exception as exc:
            self._state = RepairState.STOPPED
            return self._problem_review(
                "REPOSITORY_PREVIEW_FAILED",
                str(exc),
            )
        if preview.draft is not None or preview.status in {
            "preview_ready",
            "no_op",
            "replayed",
            "replayed_rolled_back",
        }:
            self._seen_attempts.add(bound.attempt_digest)

        target_issue_ids = tuple(
            sorted(
                {
                    issue_id
                    for reason in bound.reasons
                    for issue_id in reason.issue_ids
                }
            )
        )
        change_count = _change_count(preview)
        changes = _changes(preview)
        if change_count > self.remaining_changes:
            self._state = RepairState.STOPPED
            return self._problem_review(
                "CHANGE_BUDGET_EXCEEDED",
                "proposal exceeds the remaining persistent change budget",
                details={
                    "change_count": change_count,
                    "remaining_changes": self.remaining_changes,
                },
            )
        risk_codes = _proposal_risks(
            current,
            bound,
            change_count=change_count,
            max_auto_changes=self.budget.max_auto_changes_per_patch,
        )

        if _is_approval_required(preview):
            if preview.draft is None or preview.required_approval_scope is None:
                self._state = RepairState.STOPPED
                return self._problem_review(
                    "MALFORMED_APPROVAL_CHECKPOINT",
                    "repository requested approval without an exact draft scope",
                )
            review = self._stage_review(
                current=current,
                bound=bound,
                candidate=None,
                evidence_snapshot=evidence_snapshot,
                change_count=change_count,
                changes=changes,
                target_issue_ids=target_issue_ids,
                risk_codes=risk_codes,
                required_approval_scope=preview.required_approval_scope,
            )
            return review

        candidate, assessment_problem = self._assess_preview(
            current,
            bound,
            preview,
            evidence_snapshot=evidence_snapshot,
            target_issue_ids=target_issue_ids,
        )
        if assessment_problem is not None:
            if assessment_problem.code == "OSCILLATION_DETECTED":
                self._latch_terminal(
                    assessment_problem.code,
                    assessment_problem.message,
                    assessment_problem.details,
                )
            else:
                self._state = self._state_for_snapshot(current)
            return self._problem_review(
                assessment_problem.code,
                assessment_problem.message,
                details=assessment_problem.details,
                candidate=candidate,
                bound=bound,
                change_count=change_count,
                changes=changes,
                target_issue_ids=target_issue_ids,
                risk_codes=risk_codes,
            )
        assert candidate is not None
        return self._stage_review(
            current=current,
            bound=bound,
            candidate=candidate,
            evidence_snapshot=evidence_snapshot,
            change_count=change_count,
            changes=changes,
            target_issue_ids=target_issue_ids,
            risk_codes=risk_codes,
            required_approval_scope=None,
        )

    def commit(
        self,
        review_id: str,
        *,
        human_grant: HumanCheckpointGrant | None = None,
        approvals: Sequence[ApprovalGrant] = (),
    ) -> RepairResult:
        """Re-preview and commit one exact pending review."""

        if self._snapshot is None:
            self.inspect()
        assert self._snapshot is not None
        if self._terminal_problem is not None:
            self._state = RepairState.STOPPED
            return self._problem_result(
                self._terminal_problem.code,
                self._terminal_problem.message,
                details=self._terminal_problem.details,
                review_id=review_id,
            )
        pending = self._pending
        if pending is None or review_id != pending.review.review_id:
            return self._problem_result(
                "UNKNOWN_REVIEW",
                "no pending review matches the supplied review ID",
            )

        if pending.review.risk_codes and (
            human_grant is None
            or not isinstance(human_grant, HumanCheckpointGrant)
            or human_grant.review_id != review_id
        ):
            self._state = RepairState.WAITING_APPROVAL
            return self._problem_result(
                "HUMAN_CHECKPOINT_REQUIRED",
                "this review contains effects outside the automatic policy",
                review_id=review_id,
            )
        required_scope = pending.review.required_approval_scope
        if required_scope is not None and not any(
            isinstance(grant, ApprovalGrant)
            and grant.scope_digest == required_scope
            for grant in approvals
        ):
            self._state = RepairState.WAITING_APPROVAL
            return self._problem_result(
                "STORE_APPROVAL_REQUIRED",
                "the exact protected-change approval grant is missing",
                review_id=review_id,
            )

        try:
            observed_evidence = self._load_evidence_snapshot()
            current = self._load_snapshot(
                evidence_snapshot=observed_evidence,
            )
        except _EvidenceSourceFailure:
            self._state = RepairState.WAITING_EXTERNAL
            return self._problem_result(
                "EVIDENCE_SOURCE_FAILED",
                "provider evidence snapshot is unavailable",
                review_id=review_id,
            )
        except Exception as exc:
            self._state = RepairState.STOPPED
            return self._problem_result(
                "REPOSITORY_READ_FAILED",
                str(exc),
                review_id=review_id,
            )
        _, repeated_external_state = self._adopt_snapshot(
            current,
            previous=pending.base_snapshot,
            clear_pending_on_change=True,
        )
        if _evidence_binding_changed(
            current,
            pending.base_snapshot,
        ):
            self._pending = None
            if not repeated_external_state:
                self._state = self._state_for_snapshot(current)
            return self._problem_result(
                "EVIDENCE_REVISION_CHANGED",
                "provider evidence changed after proposal review",
                details=_evidence_change_details(
                    expected=pending.base_snapshot,
                    current=current,
                ),
                review_id=review_id,
            )
        if (
            current.revision != pending.base_snapshot.revision
            or current.state_digest != pending.base_snapshot.state_digest
        ):
            self._pending = None
            if not repeated_external_state:
                self._state = self._state_for_snapshot(current)
            return self._problem_result(
                "STALE_REVIEW",
                "canonical state changed after proposal review",
                review_id=review_id,
            )

        try:
            preview = self._repository.preview_patch(
                pending.bound.patch,
                approvals,
                evaluation_at=self.evaluation_at,
            )
        except Exception as exc:
            self._state = RepairState.STOPPED
            return self._problem_result(
                "REPOSITORY_PREVIEW_FAILED",
                str(exc),
                review_id=review_id,
            )
        candidate, assessment_problem = self._assess_preview(
            current,
            pending.bound,
            preview,
            evidence_snapshot=pending.evidence_snapshot,
            target_issue_ids=pending.review.target_issue_ids,
        )
        if assessment_problem is not None:
            approval_still_required = _is_approval_required(preview)
            if assessment_problem.code == "OSCILLATION_DETECTED":
                self._pending = None
                self._latch_terminal(
                    assessment_problem.code,
                    assessment_problem.message,
                    assessment_problem.details,
                )
            elif approval_still_required:
                self._state = RepairState.WAITING_APPROVAL
            else:
                # The exact reviewed effect is no longer committable. A fresh
                # proposal must be produced instead of retrying it forever.
                self._pending = None
                self._state = self._state_for_snapshot(current)
            return self._problem_result(
                assessment_problem.code,
                assessment_problem.message,
                details=assessment_problem.details,
                review_id=review_id,
                store_status=preview.status,
            )
        assert candidate is not None

        change_count = _change_count(preview)
        risk_codes = _proposal_risks(
            current,
            pending.bound,
            change_count=change_count,
            max_auto_changes=self.budget.max_auto_changes_per_patch,
        )
        if (
            change_count != pending.review.change_count
            or _changes(preview) != pending.review.changes
            or risk_codes != pending.review.risk_codes
            or (
                pending.review.candidate_snapshot is not None
                and candidate.decision_context_digest
                != pending.review.candidate_snapshot.decision_context_digest
            )
        ):
            self._pending = None
            self._latch_terminal(
                "NONDETERMINISTIC_PREVIEW",
                "re-preview did not reproduce the exact reviewed effect",
            )
            return self._problem_result(
                "NONDETERMINISTIC_PREVIEW",
                "re-preview did not reproduce the exact reviewed effect",
                review_id=review_id,
            )
        if change_count > self.remaining_changes:
            self._pending = None
            self._state = RepairState.STOPPED
            return self._problem_result(
                "CHANGE_BUDGET_EXCEEDED",
                "review exceeds the remaining persistent change budget",
                review_id=review_id,
            )

        if self._evidence_source is not None:
            try:
                latest_evidence = self._load_evidence_snapshot()
            except _EvidenceSourceFailure:
                self._state = RepairState.WAITING_EXTERNAL
                return self._problem_result(
                    "EVIDENCE_SOURCE_FAILED",
                    "provider evidence snapshot is unavailable",
                    review_id=review_id,
                )
            if not _evidence_matches_binding(
                latest_evidence,
                pending.base_snapshot,
            ):
                try:
                    refreshed = self._load_snapshot(
                        evidence_snapshot=latest_evidence,
                    )
                except _EvidenceSourceFailure:
                    self._state = RepairState.WAITING_EXTERNAL
                    return self._problem_result(
                        "EVIDENCE_SOURCE_FAILED",
                        "provider evidence snapshot is unavailable",
                        review_id=review_id,
                    )
                except Exception as exc:
                    self._state = RepairState.STOPPED
                    return self._problem_result(
                        "REPOSITORY_READ_FAILED",
                        str(exc),
                        review_id=review_id,
                    )
                self._pending = None
                self._adopt_snapshot(
                    refreshed,
                    previous=current,
                    clear_pending_on_change=True,
                )
                self._state = self._state_for_snapshot(refreshed)
                return self._problem_result(
                    "EVIDENCE_REVISION_CHANGED",
                    "provider evidence changed before canonical commit",
                    details=_evidence_change_details(
                        expected=pending.base_snapshot,
                        current=refreshed,
                    ),
                    review_id=review_id,
                )

        try:
            result = self._repository.apply_patch(
                pending.bound.patch,
                approvals,
                evaluation_at=self.evaluation_at,
            )
            if result.status == "commit_outcome_unknown":
                result = self._repository.apply_patch(
                    pending.bound.patch,
                    approvals,
                    evaluation_at=self.evaluation_at,
                )
        except Exception as exc:
            self._state = RepairState.STOPPED
            return self._problem_result(
                "REPOSITORY_COMMIT_FAILED",
                str(exc),
                review_id=review_id,
            )

        if not _confirmed_apply(result):
            if result.status in {
                "replayed_rolled_back",
                "no_op",
                "rejected",
            }:
                self._pending = None
            try:
                observed_evidence = self._load_evidence_snapshot()
                observed = self._load_snapshot(
                    evidence_snapshot=observed_evidence,
                )
                self._adopt_snapshot(
                    observed,
                    previous=current,
                    clear_pending_on_change=True,
                )
            except Exception:
                pass
            if result.status == "commit_outcome_unknown":
                self._latch_terminal(
                    "COMMIT_OUTCOME_UNKNOWN",
                    "exact retry could not determine the durable outcome",
                    {"store_status": result.status},
                )
            elif self._pending is not None:
                self._state = self._pending.review.state
            else:
                self._state = self._state_for_snapshot(self._snapshot)
            return self._problem_result(
                "COMMIT_NOT_CONFIRMED",
                (
                    "repository did not confirm an applied or exactly replayed "
                    "patch"
                ),
                details={"store_status": result.status},
                review_id=review_id,
                store_status=result.status,
                extra_problems=_store_problems(result),
            )

        self._changes_used += change_count
        self._pending = None
        post_commit_problems: tuple[RepairProblem, ...] = ()
        post_commit_snapshot_uncertain = False
        post_commit_evidence_changed = False
        post_commit_evidence_failed = False
        try:
            committed_evidence = self._load_evidence_snapshot()
            committed = self._load_snapshot(
                evidence_snapshot=committed_evidence,
            )
            post_commit_evidence_changed = _evidence_binding_changed(
                committed,
                pending.base_snapshot,
            )
        except _EvidenceSourceFailure:
            fallback = _snapshot_from_store_result(
                result,
                evaluation_at=self.evaluation_at,
                remaining_iterations=self.remaining_iterations,
                remaining_provider_calls=self.remaining_provider_calls,
                remaining_changes=self.remaining_changes,
                evidence_snapshot=pending.evidence_snapshot,
            )
            committed = fallback or current
            post_commit_snapshot_uncertain = fallback is None
            post_commit_evidence_failed = True
            post_commit_problems = (
                RepairProblem(
                    "EVIDENCE_SOURCE_FAILED",
                    "provider evidence snapshot is unavailable after commit",
                    {"used_store_candidate": fallback is not None},
                ),
            )
        except Exception as exc:
            fallback = _snapshot_from_store_result(
                result,
                evaluation_at=self.evaluation_at,
                remaining_iterations=self.remaining_iterations,
                remaining_provider_calls=self.remaining_provider_calls,
                remaining_changes=self.remaining_changes,
                evidence_snapshot=pending.evidence_snapshot,
            )
            committed = fallback or current
            post_commit_snapshot_uncertain = fallback is None
            post_commit_problems = (
                RepairProblem(
                    "POST_COMMIT_READ_FAILED",
                    str(exc),
                    {"used_store_candidate": fallback is not None},
                ),
            )
        _, repeated_state = self._adopt_snapshot(
            committed,
            previous=current,
        )
        if post_commit_evidence_failed:
            self._state = RepairState.WAITING_EXTERNAL
            problems = post_commit_problems
        elif post_commit_evidence_changed:
            self._state = RepairState.WAITING_EXTERNAL
            problems = (
                RepairProblem(
                    "EVIDENCE_REVISION_CHANGED",
                    "provider evidence changed after canonical commit",
                    _evidence_change_details(
                        expected=pending.base_snapshot,
                        current=committed,
                    ),
                ),
            ) + post_commit_problems
        elif repeated_state:
            problems = (
                RepairProblem(
                    "OSCILLATION_DETECTED",
                    "committed state repeats an earlier semantic state",
                    {"state_digest": committed.state_digest},
                ),
            ) + post_commit_problems
        elif post_commit_snapshot_uncertain:
            self._state = RepairState.STOPPED
            problems = post_commit_problems
        else:
            self._state = self._state_for_snapshot(committed)
            problems = post_commit_problems
        return RepairResult(
            state=self._state,
            snapshot=committed,
            applied=True,
            review_id=review_id,
            store_status=result.status,
            change_count=change_count,
            changes=pending.review.changes,
            reasons=pending.review.reasons,
            problems=problems,
        )

    def cancel_pending(self) -> None:
        """Discard a pending review without changing canonical state."""

        self._pending = None
        if self._snapshot is not None:
            self._state = (
                RepairState.STOPPED
                if self._terminal_problem is not None
                else self._state_for_snapshot(self._snapshot)
            )

    def reserve_provider_call(
        self,
        fingerprint: str,
    ) -> ProviderCallReservation:
        """Reserve one unique provider call; cache hits consume no budget."""

        _require_text(fingerprint, "fingerprint")
        if self._terminal_problem is not None:
            return ProviderCallReservation(
                allowed=False,
                cached=False,
                remaining_provider_calls=self.remaining_provider_calls,
                problem=self._terminal_problem,
            )
        if fingerprint in self._provider_fingerprints:
            completed = self._provider_fingerprints[fingerprint]
            return ProviderCallReservation(
                allowed=completed,
                cached=completed,
                remaining_provider_calls=self.remaining_provider_calls,
                problem=(
                    None
                    if completed
                    else RepairProblem(
                        "PROVIDER_CALL_RESERVED",
                        "this provider request is already reserved but has no "
                        "completed cached result",
                    )
                ),
            )
        if self._pending is not None:
            return ProviderCallReservation(
                allowed=False,
                cached=False,
                remaining_provider_calls=self.remaining_provider_calls,
                problem=RepairProblem(
                    "PENDING_REVIEW",
                    "provider work cannot start while a proposal is pending",
                ),
            )
        if self.remaining_provider_calls == 0:
            return ProviderCallReservation(
                allowed=False,
                cached=False,
                remaining_provider_calls=0,
                problem=RepairProblem(
                    "PROVIDER_BUDGET_EXHAUSTED",
                    "the unique provider-call budget is exhausted",
                ),
            )
        self._provider_fingerprints[fingerprint] = False
        self._provider_calls_used += 1
        if self._snapshot is not None:
            self.inspect()
        return ProviderCallReservation(
            allowed=True,
            cached=False,
            remaining_provider_calls=self.remaining_provider_calls,
        )

    def complete_provider_call(self, fingerprint: str) -> None:
        """Mark a reserved provider fingerprint as a reusable cache hit."""

        _require_text(fingerprint, "fingerprint")
        if fingerprint not in self._provider_fingerprints:
            raise ValueError("provider fingerprint was not reserved")
        self._provider_fingerprints[fingerprint] = True

    def _load_evidence_snapshot(self) -> EvidenceSnapshot | None:
        if self._evidence_source is None:
            return None
        try:
            result = self._evidence_source.load()
            snapshot = result.snapshot(evaluation_at=self.evaluation_at)
            if type(snapshot) is not EvidenceSnapshot:
                raise TypeError(
                    "evidence source must return an exact EvidenceSnapshot"
                )
            return snapshot
        except Exception as exc:
            raise _EvidenceSourceFailure(
                "provider evidence snapshot is unavailable"
            ) from exc

    def _load_snapshot(
        self,
        *,
        evidence_snapshot: EvidenceSnapshot | None,
    ) -> PlannerSnapshot:
        plan = self._repository.load_plan()
        try:
            return snapshot_from_plan(
                plan,
                evaluation_at=self.evaluation_at,
                remaining_iterations=self.remaining_iterations,
                remaining_provider_calls=self.remaining_provider_calls,
                remaining_changes=self.remaining_changes,
                evidence_snapshot=evidence_snapshot,
            )
        except Exception as exc:
            if evidence_snapshot is not None:
                raise _EvidenceSourceFailure(
                    "provider evidence composition is unavailable"
                ) from exc
            raise

    def _adopt_snapshot(
        self,
        snapshot: PlannerSnapshot,
        *,
        previous: PlannerSnapshot | None = None,
        clear_pending_on_change: bool = False,
    ) -> tuple[bool, bool]:
        """Record one observed canonical state and enforce oscillation policy."""

        prior = self._snapshot if previous is None else previous
        canonical_changed = prior is not None and (
            snapshot.revision != prior.revision
            or snapshot.state_digest != prior.state_digest
        )
        evidence_changed = (
            prior is not None
            and _evidence_binding_changed(snapshot, prior)
        )
        changed = canonical_changed or evidence_changed
        repeated = (
            canonical_changed
            and snapshot.state_digest in self._visited_states
        )
        if changed and clear_pending_on_change:
            self._pending = None
        self._snapshot = snapshot
        self._visited_states.add(snapshot.state_digest)
        if repeated:
            self._latch_terminal(
                "OSCILLATION_DETECTED",
                "canonical state returned to an earlier semantic state",
                {"state_digest": snapshot.state_digest},
            )
        elif self._terminal_problem is not None:
            self._state = RepairState.STOPPED
        elif self._pending is None:
            self._state = self._state_for_snapshot(snapshot)
        return changed, repeated

    def _state_for_snapshot(
        self,
        snapshot: PlannerSnapshot | None,
    ) -> RepairState:
        if snapshot is None:
            return RepairState.STOPPED
        if self._terminal_problem is not None:
            return RepairState.STOPPED
        if not snapshot.issues:
            return RepairState.COMPLETE
        planner_work = any(
            issue.auto_repairable
            and any(
                option.kind is RepairOptionKind.PLAN_PATCH
                and option.owner is IssueOwner.PLANNER
                and option.auto_allowed
                and not option.blocking
                for option in issue.options
            )
            for issue in snapshot.issues
        )
        if planner_work:
            if (
                snapshot.remaining_iterations == 0
                or snapshot.remaining_changes == 0
            ):
                return RepairState.STOPPED
            return RepairState.READY
        return RepairState.WAITING_EXTERNAL

    def _latch_terminal(
        self,
        code: str,
        message: str,
        details: Mapping[str, Any] = MappingProxyType({}),
    ) -> None:
        if self._terminal_problem is None:
            self._terminal_problem = RepairProblem(code, message, details)
        self._state = RepairState.STOPPED

    def _stage_review(
        self,
        *,
        current: PlannerSnapshot,
        bound: BoundProposal,
        candidate: PlannerSnapshot | None,
        evidence_snapshot: EvidenceSnapshot | None,
        change_count: int,
        changes: tuple[ChangeRecord, ...],
        target_issue_ids: tuple[str, ...],
        risk_codes: tuple[str, ...],
        required_approval_scope: str | None,
    ) -> ProposalReview:
        review_id = _review_id(
            self.run_id,
            bound,
            candidate,
            change_count=change_count,
            changes=changes,
            reasons=bound.reasons,
            risk_codes=risk_codes,
            required_approval_scope=required_approval_scope,
        )
        state = (
            RepairState.WAITING_APPROVAL
            if risk_codes or required_approval_scope is not None
            else RepairState.READY
        )
        review = ProposalReview(
            state=state,
            snapshot=current,
            review_id=review_id,
            candidate_snapshot=candidate,
            effect_digest=bound.effect_digest,
            attempt_digest=bound.attempt_digest,
            change_count=change_count,
            changes=changes,
            reasons=bound.reasons,
            target_issue_ids=target_issue_ids,
            risk_codes=risk_codes,
            required_approval_scope=required_approval_scope,
        )
        self._pending = _PendingReview(
            review=review,
            bound=bound,
            base_snapshot=current,
            evidence_snapshot=evidence_snapshot,
        )
        self._state = state
        return review

    def _assess_preview(
        self,
        current: PlannerSnapshot,
        bound: BoundProposal,
        preview: StoreResult,
        *,
        evidence_snapshot: EvidenceSnapshot | None,
        target_issue_ids: tuple[str, ...],
    ) -> tuple[PlannerSnapshot | None, RepairProblem | None]:
        candidate = _snapshot_from_store_result(
            preview,
            evaluation_at=self.evaluation_at,
            remaining_iterations=self.remaining_iterations,
            remaining_provider_calls=self.remaining_provider_calls,
            remaining_changes=self.remaining_changes,
            evidence_snapshot=evidence_snapshot,
        )
        if (
            not preview.success
            or preview.status != "preview_ready"
            or not preview.changed
        ):
            problems = _store_problems(preview)
            if _is_approval_required(preview):
                return candidate, RepairProblem(
                    "APPROVAL_REQUIRED",
                    "repository requires an exact protected-change approval",
                    {
                        "required_approval_scope": (
                            preview.required_approval_scope
                        )
                    },
                )
            if problems:
                return candidate, RepairProblem(
                    problems[0].code,
                    problems[0].message,
                    problems[0].details,
                )
            return candidate, RepairProblem(
                "PREVIEW_NOT_APPLICABLE",
                "repository preview did not produce an applicable candidate",
                {
                    "store_status": preview.status,
                    "changed": preview.changed,
                },
            )
        if candidate is None:
            return None, RepairProblem(
                "MALFORMED_PREVIEW",
                "successful preview omitted its canonical candidate",
            )
        if candidate.state_digest == current.state_digest:
            return candidate, RepairProblem(
                "NO_SEMANTIC_PROGRESS",
                "candidate has the same semantic state as its base",
            )
        if candidate.state_digest in self._visited_states:
            return candidate, RepairProblem(
                "OSCILLATION_DETECTED",
                "candidate repeats an earlier semantic state",
                {"state_digest": candidate.state_digest},
            )

        current_by_id = current.issue_by_id
        candidate_ids = set(candidate.issue_by_id)
        persistent = [
            issue_id
            for issue_id in target_issue_ids
            if issue_id in candidate_ids
            and issue_id in current_by_id
        ]
        if persistent:
            return candidate, RepairProblem(
                "TARGET_ISSUE_PERSISTS",
                "every issue cited by the proposal must disappear",
                {"issue_ids": persistent},
            )
        before = _progress_vector(current)
        after = _progress_vector(candidate)
        if not after < before:
            return candidate, RepairProblem(
                "NO_STRICT_PROGRESS",
                "candidate does not strictly improve the repair progress vector",
                {
                    "before": list(before),
                    "after": list(after),
                },
            )
        return candidate, None

    def _problem_review(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] = MappingProxyType({}),
        snapshot: PlannerSnapshot | None = None,
        candidate: PlannerSnapshot | None = None,
        bound: BoundProposal | None = None,
        change_count: int = 0,
        changes: tuple[ChangeRecord, ...] = (),
        target_issue_ids: tuple[str, ...] = (),
        risk_codes: tuple[str, ...] = (),
    ) -> ProposalReview:
        current = snapshot or self._snapshot
        assert current is not None
        return ProposalReview(
            state=self._state,
            snapshot=current,
            candidate_snapshot=candidate,
            effect_digest=bound.effect_digest if bound else None,
            attempt_digest=bound.attempt_digest if bound else None,
            change_count=change_count,
            changes=changes,
            reasons=bound.reasons if bound else (),
            target_issue_ids=target_issue_ids,
            risk_codes=risk_codes,
            problems=(RepairProblem(code, message, details),),
        )

    def _problem_result(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] = MappingProxyType({}),
        review_id: str | None = None,
        store_status: str | None = None,
        extra_problems: tuple[RepairProblem, ...] = (),
    ) -> RepairResult:
        assert self._snapshot is not None
        return RepairResult(
            state=self._state,
            snapshot=self._snapshot,
            applied=False,
            review_id=review_id,
            store_status=store_status,
            problems=(RepairProblem(code, message, details),) + extra_problems,
        )


def _snapshot_from_store_result(
    result: StoreResult,
    *,
    evaluation_at: datetime,
    remaining_iterations: int,
    remaining_provider_calls: int,
    remaining_changes: int,
    evidence_snapshot: EvidenceSnapshot | None,
) -> PlannerSnapshot | None:
    candidate = result.mutable_candidate_plan()
    if candidate is None:
        return None
    try:
        return snapshot_from_plan(
            candidate,
            evaluation_at=evaluation_at,
            remaining_iterations=remaining_iterations,
            remaining_provider_calls=remaining_provider_calls,
            remaining_changes=remaining_changes,
            evidence_snapshot=evidence_snapshot,
        )
    except (TypeError, ValueError):
        return None


def _evidence_binding_changed(
    current: PlannerSnapshot,
    expected: PlannerSnapshot,
) -> bool:
    current_digest = (
        current.evidence_binding.binding_digest
        if current.evidence_binding is not None
        else None
    )
    expected_digest = (
        expected.evidence_binding.binding_digest
        if expected.evidence_binding is not None
        else None
    )
    return current_digest != expected_digest


def _evidence_matches_binding(
    evidence_snapshot: EvidenceSnapshot | None,
    expected: PlannerSnapshot,
) -> bool:
    binding = expected.evidence_binding
    if evidence_snapshot is None:
        return binding is None
    if binding is None:
        return False
    return (
        evidence_snapshot.policies.revision
        == binding.policy_registry_revision
        and evidence_snapshot.store_revision == binding.store_revision
        and evidence_snapshot.evidence_revision
        == binding.evidence_revision
        and evidence_snapshot.evaluation_at == binding.evaluation_at
    )


def _evidence_change_details(
    *,
    expected: PlannerSnapshot,
    current: PlannerSnapshot,
) -> dict[str, str | None]:
    return {
        "expected_evidence_binding_digest": (
            expected.evidence_binding.binding_digest
            if expected.evidence_binding is not None
            else None
        ),
        "current_evidence_binding_digest": (
            current.evidence_binding.binding_digest
            if current.evidence_binding is not None
            else None
        ),
    }


def _progress_vector(snapshot: PlannerSnapshot) -> tuple[int, int, int, int, int]:
    status_rank = {
        CheckStatus.FEASIBLE: 0,
        CheckStatus.NEEDS_VERIFICATION: 1,
        CheckStatus.INFEASIBLE: 2,
    }[snapshot.report.status]
    errors = sum(
        issue.check.severity is IssueSeverity.ERROR
        for issue in snapshot.issues
    )
    verification_warnings = sum(
        issue.check.severity is IssueSeverity.WARNING
        and any(
            option.requires_evidence
            or option.owner is IssueOwner.PROVIDER
            for option in issue.options
        )
        for issue in snapshot.issues
    )
    planner_owned = sum(
        any(
            option.kind is RepairOptionKind.PLAN_PATCH
            and option.owner is IssueOwner.PLANNER
            and option.auto_allowed
            and not option.blocking
            for option in issue.options
        )
        for issue in snapshot.issues
    )
    return (
        status_rank,
        errors,
        verification_warnings,
        planner_owned,
        len(snapshot.issues),
    )


def _proposal_risks(
    current: PlannerSnapshot,
    bound: BoundProposal,
    *,
    change_count: int,
    max_auto_changes: int,
) -> tuple[str, ...]:
    risks: set[str] = set()
    reason_by_id = {reason.op_id: reason for reason in bound.reasons}
    state = deep_copy_json(current.state)
    assert isinstance(state, dict)
    activity_by_id = _activity_index(state)

    for operation in bound.patch.operations:
        reason = reason_by_id[operation.op_id]
        if not operation_matches_option(reason.option_key, operation):
            risks.add("OPTION_OPERATION_MISMATCH")
        if isinstance(operation, RemoveActivity):
            risks.add("REMOVE_ACTIVITY")
        if isinstance(operation, (AddConstraint,)):
            if operation.fields.get("strength", "hard") in {None, "hard"}:
                risks.add("ADD_HARD_CONSTRAINT")
        if isinstance(operation, (UpdateConstraint, RemoveConstraint)):
            risks.add("MUTATE_CONSTRAINT")
        if isinstance(operation, UpdateDay) and set(operation.fields).intersection(
            {"date", "timezone", "available_start", "available_end"}
        ):
            risks.add("CHANGE_DAY_BOUNDARY")
        if isinstance(operation, (AddActivity, UpdateActivity)):
            fields = operation.fields
            if fields.get("decision_state") in {"fixed", "booked"}:
                risks.add("CREATE_COMMITTED_DECISION")
            if fields.get("flexibility") in {"fixed_day", "fixed_time"}:
                risks.add("CREATE_FIXED_FLEXIBILITY")
        if isinstance(operation, UpdateActivity):
            original = activity_by_id.get(operation.activity_id)
            if original is not None and "decision_state" in operation.fields:
                before = _decision_rank(original.get("decision_state"))
                after = _decision_rank(operation.fields.get("decision_state"))
                if before is not None and after is not None and after < before:
                    risks.add("DOWNGRADE_DECISION")
    if change_count > max_auto_changes:
        risks.add("LARGE_PATCH")
    return tuple(sorted(risks))


def _activity_index(state: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    itinerary = state.get("itinerary")
    if not isinstance(itinerary, Mapping):
        return result
    days = itinerary.get("days")
    if not isinstance(days, Sequence) or isinstance(days, (str, bytes)):
        return result
    for day in days:
        if not isinstance(day, Mapping):
            continue
        places = day.get("places")
        if not isinstance(places, Sequence) or isinstance(
            places, (str, bytes)
        ):
            continue
        for activity in places:
            if not isinstance(activity, Mapping):
                continue
            activity_id = activity.get("activity_id")
            if isinstance(activity_id, str):
                result[activity_id] = activity
    return result


def _decision_rank(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    return {
        "cancelled": -2,
        "excluded": -2,
        "candidate": 0,
        "selected": 1,
        "fixed": 2,
        "booked": 3,
    }.get(value)


def _review_id(
    run_id: str,
    bound: BoundProposal,
    candidate: PlannerSnapshot | None,
    *,
    change_count: int,
    changes: tuple[ChangeRecord, ...],
    reasons: tuple[OperationReason, ...],
    risk_codes: tuple[str, ...],
    required_approval_scope: str | None,
) -> str:
    digest = hashlib.sha256(
        b"trip-planner.repair-review/v1\0"
        + canonical_json_bytes(
            {
                "run_id": run_id,
                "snapshot_id": bound.snapshot_id,
                "attempt_digest": bound.attempt_digest,
                "candidate_decision_context_digest": (
                    candidate.decision_context_digest
                    if candidate is not None
                    else None
                ),
                "change_count": change_count,
                "changes": [change.to_dict() for change in changes],
                "reasons": [reason.to_dict() for reason in reasons],
                "risk_codes": list(risk_codes),
                "required_approval_scope": required_approval_scope,
            }
        )
    ).hexdigest()
    return f"review-{digest}"


def _change_count(result: StoreResult) -> int:
    if result.draft is None:
        return 0
    return len(result.draft.changes)


def _changes(result: StoreResult) -> tuple[ChangeRecord, ...]:
    if result.draft is None:
        return ()
    return result.draft.changes


def _is_approval_required(result: StoreResult) -> bool:
    return (
        result.required_approval_scope is not None
        and any(problem.code == "APPROVAL_REQUIRED" for problem in result.problems)
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
            or (
                result.success
                and result.status == "replayed"
                and result.replayed
                and not result.changed
            )
        )
    )


def _store_problems(result: StoreResult) -> tuple[RepairProblem, ...]:
    return tuple(_store_problem(problem) for problem in result.problems)


def _store_problem(problem: StoreProblem) -> RepairProblem:
    return RepairProblem(
        code=problem.code,
        message=problem.message,
        details=problem.details,
    )


def _require_text(
    value: Any,
    name: str,
    *,
    maximum: int = 256,
) -> None:
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


def _safe_problem_code(value: Any) -> str:
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
    normalized = normalized[:256]
    return normalized or "REPAIR_ERROR"


def _safe_problem_message(value: Any) -> str:
    text = str(value) if value is not None else ""
    without_controls = "".join(
        " "
        if unicodedata.category(character).startswith("C")
        else character
        for character in text
    )
    normalized = " ".join(without_controls.split())
    return (normalized or "Repair operation failed.")[:4096]


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


def _require_aware_datetime(value: Any, name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")


__all__ = [
    "EvidenceSource",
    "HumanCheckpointGrant",
    "PlanRepository",
    "ProposalReview",
    "ProviderCallReservation",
    "RepairController",
    "RepairProblem",
    "RepairResult",
    "RepairState",
]
