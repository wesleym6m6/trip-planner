"""Review-only bridge from an evidence-bound score to canonical apply.

This module prepares the existing schedule-domain preview and wraps it in the
existing expiring ``accept_apply`` gate.  It deliberately stops before response
capture or commit: an opaque proposal ref is not mutation authority, and the
host must keep the returned review process-local until an exact typed response
is captured.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .facts import EvidenceSnapshot, FactKey
from .guided_canonical_apply import (
    GuidedCanonicalApplyActionKind,
    GuidedCanonicalApplyReview,
    prepare_guided_canonical_schedule_review,
)
from .lodging import LodgingIntakeAssessment
from .lodging_confirmation import LodgingConfirmationReview
from .schedule_staging import (
    EvidenceSource,
    ScheduleStageState,
    ScheduleStager,
)
from .store import TripStore
from .tripctl_schedule import (
    TripctlScheduleError,
    _runtime_proposal_ref,
    _solve_exact_with_evidence,
    _verify_evidence_source,
)
from .tripctl_runtime import _CanonicalRuntimeInputs


TRIPCTL_APPLY_REVIEW_VERSION = "tripctl-apply-review/v1"
"""Version of the review-only product bridge."""

_TRIPCTL_ENVELOPE_VERSION = "tripctl/v1"
_PROBLEM_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_PLAIN_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_CONTEXT_BINDING_DOMAIN = b"trip-planner.tripctl-apply-context/v1\0"
_REVIEW_TOKEN = object()


class TripctlApplyReviewError(ValueError):
    """One redacted preparation failure with no candidate or provider data."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        if not isinstance(code, str) or _PROBLEM_CODE_RE.fullmatch(code) is None:
            raise ValueError("tripctl apply review code must be bounded")
        if not isinstance(retryable, bool):
            raise TypeError("tripctl apply review retryable must be bool")
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class TripctlScheduleApplyReview:
    """Non-serializable product handle over one exact guided apply review."""

    proposal_ref: str
    runtime_context_ref: str
    _review: GuidedCanonicalApplyReview = field(repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError("tripctl apply reviews require the preparation gate")
        if _DIGEST_RE.fullmatch(self.proposal_ref) is None:
            raise ValueError("proposal_ref must be an opaque SHA-256 ref")
        if _PLAIN_DIGEST_RE.fullmatch(self.runtime_context_ref) is None:
            raise ValueError("runtime_context_ref must be a plain digest")
        if type(self._review) is not GuidedCanonicalApplyReview:
            raise TypeError("review must be exact GuidedCanonicalApplyReview")
        self._review.verify()
        if (
            self._review.action_kind
            is not GuidedCanonicalApplyActionKind.APPLY_SCHEDULE_PATCH
            or self._review._context_binding_digest
            != _context_binding_digest(self.runtime_context_ref)
        ):
            raise ValueError("guided review differs from the schedule context")

    @property
    def review_id(self) -> str:
        return self._review.review_id

    @property
    def created_at(self) -> datetime:
        return self._review.created_at

    @property
    def expires_at(self) -> datetime:
        return self._review.expires_at

    def verify(self) -> None:
        self._review.verify()
        if (
            self._review.action_kind
            is not GuidedCanonicalApplyActionKind.APPLY_SCHEDULE_PATCH
            or self._review._context_binding_digest
            != _context_binding_digest(self.runtime_context_ref)
        ):
            raise ValueError("tripctl apply review no longer matches its context")

    def to_dict(self) -> dict[str, Any]:
        self.verify()
        guided = self._review.to_dict()
        return {
            "contract_version": _TRIPCTL_ENVELOPE_VERSION,
            "command": "apply",
            "ok": True,
            "status": "review_required",
            "storage_mode": "canonical",
            "result": {
                "contract_version": TRIPCTL_APPLY_REVIEW_VERSION,
                "storage_mode": "canonical",
                "proposal_ref": self.proposal_ref,
                "runtime_context_ref": self.runtime_context_ref,
                "apply_review": guided,
                "apply_authority": False,
                "canonical_write_performed": False,
            },
            "problems": [],
            "retryable": False,
            "pending_review_retained": True,
            "next_action": "capture_apply_response",
            "requires_user_review": True,
        }

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("tripctl apply reviews are non-serializable")


def prepare_trip_schedule_apply_review(
    store: TripStore,
    *,
    proposal_ref: str,
    evidence_snapshot: EvidenceSnapshot,
    evidence_source: EvidenceSource,
    reviewed_at: datetime,
    run_id: str,
    availability_keys: tuple[FactKey, ...] = (),
    lodging_intake: LodgingIntakeAssessment | None = None,
    pending_lodging_review: LodgingConfirmationReview | None = None,
    max_changes: int = 40,
    max_auto_changes: int = 12,
) -> TripctlScheduleApplyReview:
    """Prepare, but never execute, one exact schedule apply review."""

    if type(store) is not TripStore:
        raise TripctlApplyReviewError("INVALID_APPLY_STORE")
    if not isinstance(proposal_ref, str) or _DIGEST_RE.fullmatch(proposal_ref) is None:
        raise TripctlApplyReviewError("INVALID_PROPOSAL_REF")
    if not callable(getattr(evidence_source, "load", None)):
        raise TripctlApplyReviewError("INVALID_RUNTIME_EVIDENCE_SOURCE")
    reviewed = _aware_utc(reviewed_at)

    try:
        inputs, problem, result = _solve_exact_with_evidence(
            store.data_dir,
            evidence_snapshot=evidence_snapshot,
            availability_keys=availability_keys,
            lodging_intake=lodging_intake,
            pending_lodging_review=pending_lodging_review,
        )
    except TripctlScheduleError as exc:
        raise TripctlApplyReviewError(
            exc.code,
            retryable=exc.retryable,
        ) from exc
    candidate = result.candidate
    expected_ref = (
        _runtime_proposal_ref(
            candidate.candidate_id,
            inputs.assessment.runtime_context_ref,
        )
        if candidate is not None
        else None
    )
    if candidate is None or expected_ref != proposal_ref:
        _verify_or_raise(inputs)
        raise TripctlApplyReviewError("STALE_PROPOSAL_REF")
    if reviewed < problem.evaluation_at:
        raise TripctlApplyReviewError("APPLY_REVIEW_CLOCK_ROLLBACK")
    if not (
        candidate.score.changed_activity_ids
        or candidate.promoted_activity_ids
    ):
        _verify_or_raise(inputs)
        raise TripctlApplyReviewError("EMPTY_SCHEDULE_PATCH")

    try:
        stager = ScheduleStager(
            store,
            run_id=run_id,
            max_changes=max_changes,
            max_auto_changes=max_auto_changes,
            evidence_source=evidence_source,
            availability_keys=availability_keys,
        )
        domain_review = stager.stage_schedule_candidate(problem, candidate)
    except (TypeError, ValueError) as exc:
        raise TripctlApplyReviewError(
            "SCHEDULE_APPLY_REVIEW_UNAVAILABLE"
        ) from exc
    if domain_review.state is ScheduleStageState.REPLAY_CONFIRMED:
        raise TripctlApplyReviewError("SCHEDULE_ALREADY_APPLIED")
    if not domain_review.ready_to_commit:
        code = (
            domain_review.problems[0].code
            if domain_review.problems
            else "SCHEDULE_APPLY_REVIEW_UNAVAILABLE"
        )
        raise TripctlApplyReviewError(
            code,
            retryable=code
            in {
                "EVIDENCE_REVISION_CHANGED",
                "STALE_SCHEDULE_PROBLEM",
            },
        )
    try:
        guided_review = prepare_guided_canonical_schedule_review(
            store,
            stager,
            domain_review,
            evaluation_at=reviewed,
            context_binding_digest=_context_binding_digest(
                inputs.assessment.runtime_context_ref
            ),
        )
    except (TypeError, ValueError) as exc:
        raise TripctlApplyReviewError(
            "SCHEDULE_APPLY_REVIEW_UNAVAILABLE"
        ) from exc
    _verify_or_raise(inputs)
    return TripctlScheduleApplyReview(
        proposal_ref=proposal_ref,
        runtime_context_ref=inputs.assessment.runtime_context_ref,
        _review=guided_review,
        _token=_REVIEW_TOKEN,
    )


def _verify_or_raise(inputs: _CanonicalRuntimeInputs) -> None:
    try:
        _verify_evidence_source(inputs)
    except TripctlScheduleError as exc:
        raise TripctlApplyReviewError(
            exc.code,
            retryable=exc.retryable,
        ) from exc


def _aware_utc(value: object) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise TripctlApplyReviewError("INVALID_APPLY_REVIEW_TIME")
    return value.astimezone(timezone.utc)


def _context_binding_digest(runtime_context_ref: str) -> str:
    if _PLAIN_DIGEST_RE.fullmatch(runtime_context_ref) is None:
        raise ValueError("runtime_context_ref must be a plain digest")
    return hashlib.sha256(
        _CONTEXT_BINDING_DOMAIN + runtime_context_ref.encode("ascii")
    ).hexdigest()


__all__ = [
    "TRIPCTL_APPLY_REVIEW_VERSION",
    "TripctlApplyReviewError",
    "TripctlScheduleApplyReview",
    "prepare_trip_schedule_apply_review",
]
