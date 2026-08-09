"""Product bridge from an evidence-bound score to canonical schedule apply.

The bridge prepares the existing schedule-domain preview, captures one exact
expiring response, and executes only the matching process-local response
against the exact bound store.  Opaque proposal refs and serialized safe views
never carry mutation authority.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Sequence
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .facts import EvidenceSnapshot, FactKey
from .guided_canonical_apply import (
    GuidedCanonicalApplyActionKind,
    GuidedCanonicalApplyOutcome,
    GuidedCanonicalApplyResponse,
    GuidedCanonicalApplyResponseKind,
    GuidedCanonicalApplyReview,
    capture_guided_canonical_apply_response,
    execute_guided_canonical_apply_response,
    prepare_guided_canonical_schedule_review,
)
from .lodging import LodgingIntakeAssessment
from .lodging_confirmation import LodgingConfirmationReview
from .schedule_staging import (
    EvidenceSource,
    ScheduleCommitResult,
    ScheduleStageState,
    ScheduleStager,
)
from .mutations import ApprovalGrant
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

TRIPCTL_APPLY_RESPONSE_VERSION = "tripctl-apply-response/v1"
"""Version of the process-local product response bridge."""

TRIPCTL_APPLY_OUTCOME_VERSION = "tripctl-apply-outcome/v1"
"""Version of the safe schedule-apply outcome projection."""

_TRIPCTL_ENVELOPE_VERSION = "tripctl/v1"
_PROBLEM_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_PLAIN_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_CONTEXT_BINDING_DOMAIN = b"trip-planner.tripctl-apply-context/v1\0"
_REVIEW_TOKEN = object()
_RESPONSE_TOKEN = object()
_OUTCOME_TOKEN = object()


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


class TripctlApplyResponseError(ValueError):
    """One bounded capture/execution rejection without private payloads."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        if not isinstance(code, str) or _PROBLEM_CODE_RE.fullmatch(code) is None:
            raise ValueError("tripctl apply response code must be bounded")
        if not isinstance(retryable, bool):
            raise TypeError("tripctl apply response retryable must be bool")
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


@dataclass(frozen=True, slots=True, repr=False)
class TripctlScheduleApplyResponse:
    """Process-local authority for one exact captured schedule response."""

    proposal_ref: str
    runtime_context_ref: str
    kind: GuidedCanonicalApplyResponseKind
    captured_at: datetime
    _review: TripctlScheduleApplyReview = field(repr=False)
    _response: GuidedCanonicalApplyResponse = field(repr=False)
    _execution_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
        compare=False,
    )
    _terminal: bool = field(default=False, init=False, repr=False)
    _last_evaluated_at: datetime = field(init=False, repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _RESPONSE_TOKEN:
            raise ValueError("tripctl apply responses require the capture gate")
        if type(self._review) is not TripctlScheduleApplyReview:
            raise TypeError("response review must be exact")
        if type(self._response) is not GuidedCanonicalApplyResponse:
            raise TypeError("guided response must be exact")
        if type(self.kind) is not GuidedCanonicalApplyResponseKind:
            raise TypeError("response kind must be exact")
        object.__setattr__(self, "_last_evaluated_at", self.captured_at)
        self.verify()

    @property
    def review_id(self) -> str:
        return self._review.review_id

    @property
    def expires_at(self) -> datetime:
        return self._review.expires_at

    @property
    def terminal(self) -> bool:
        with self._execution_lock:
            return self._terminal

    def verify(self) -> None:
        self._review.verify()
        self._response.verify()
        if (
            self.proposal_ref != self._review.proposal_ref
            or self.runtime_context_ref != self._review.runtime_context_ref
            or self.kind is not self._response.kind
            or self.captured_at != self._response.captured_at
            or self._response.review_id != self._review.review_id
            or self._review._review._captured_response_id
            != self._response.response_id
            or self._review._review._captured_response_kind
            is not self.kind
            or type(self._last_evaluated_at) is not datetime
            or self._last_evaluated_at.tzinfo is None
            or self._last_evaluated_at.utcoffset() is None
            or self._last_evaluated_at < self.captured_at
        ):
            raise ValueError("tripctl apply response differs from its review")

    def to_dict(self) -> dict[str, Any]:
        self.verify()
        return {
            "contract_version": _TRIPCTL_ENVELOPE_VERSION,
            "command": "apply",
            "ok": True,
            "status": "response_captured",
            "storage_mode": "canonical",
            "result": {
                "contract_version": TRIPCTL_APPLY_RESPONSE_VERSION,
                "storage_mode": "canonical",
                "proposal_ref": self.proposal_ref,
                "runtime_context_ref": self.runtime_context_ref,
                "review_id": self.review_id,
                "response_kind": self.kind.value,
                "captured_at": self.captured_at.isoformat(),
                "expires_at": self.expires_at.isoformat(),
                "acceptance_captured": (
                    self.kind
                    is GuidedCanonicalApplyResponseKind.ACCEPT_APPLY
                ),
                "apply_authority_exposed": False,
                "canonical_write_performed": False,
            },
            "problems": [],
            "retryable": False,
            "pending_review_retained": True,
            "next_action": "execute_apply_response",
            "requires_user_review": False,
        }

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("tripctl apply responses are non-serializable")


@dataclass(frozen=True, slots=True, repr=False)
class TripctlScheduleApplyOutcome:
    """Safe aggregate after one product-level schedule response execution."""

    proposal_ref: str
    review_id: str
    response_kind: GuidedCanonicalApplyResponseKind
    pending_review_retained: bool
    _outcome: GuidedCanonicalApplyOutcome = field(repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _OUTCOME_TOKEN:
            raise ValueError("tripctl apply outcomes require the execution gate")
        if _DIGEST_RE.fullmatch(self.proposal_ref) is None:
            raise ValueError("outcome proposal_ref must be an opaque ref")
        if _PLAIN_DIGEST_RE.fullmatch(self.review_id) is None:
            raise ValueError("outcome review_id must be a plain digest")
        if type(self.response_kind) is not GuidedCanonicalApplyResponseKind:
            raise TypeError("outcome response kind must be exact")
        if type(self.pending_review_retained) is not bool:
            raise TypeError("pending_review_retained must be exact bool")
        if type(self._outcome) is not GuidedCanonicalApplyOutcome:
            raise TypeError("guided outcome must be exact")
        domain_result = self._outcome.domain_result
        if (
            domain_result is not None
            and type(domain_result) is not ScheduleCommitResult
        ):
            raise TypeError("product apply outcome must contain a schedule result")

    @property
    def status(self) -> str:
        return self._outcome.status

    @property
    def next_action(self) -> str:
        if (
            self.status == "outcome_unknown"
            and not self.pending_review_retained
        ):
            return "inspect_apply_outcome"
        return self._outcome.next_action

    @property
    def domain_result(self) -> ScheduleCommitResult | None:
        value = self._outcome.domain_result
        return value if type(value) is ScheduleCommitResult else None

    def to_dict(self) -> dict[str, Any]:
        domain = self.domain_result
        guided = self._outcome.to_dict()
        if self.status == "outcome_unknown":
            write_performed: bool | None = None
            write_outcome = "unknown"
        else:
            write_performed = bool(guided["canonical_write_performed"])
            write_outcome = (
                "performed" if write_performed else "not_performed"
            )
        problems = _safe_schedule_problems(domain, status=self.status)
        retryable = (
            self.status == "outcome_unknown"
            and self.pending_review_retained
        )
        return {
            "contract_version": _TRIPCTL_ENVELOPE_VERSION,
            "command": "apply",
            "ok": self.status not in {"apply_failed", "outcome_unknown"},
            "status": self.status,
            "storage_mode": "canonical",
            "result": {
                "contract_version": TRIPCTL_APPLY_OUTCOME_VERSION,
                "storage_mode": "canonical",
                "proposal_ref": self.proposal_ref,
                "review_id": self.review_id,
                "response_kind": self.response_kind.value,
                "domain_state": (
                    domain.state.value if domain is not None else None
                ),
                "applied": domain.applied if domain is not None else False,
                "replay_confirmed": (
                    domain.replayed if domain is not None else False
                ),
                "candidate_is_current": (
                    domain.candidate_is_current
                    if domain is not None
                    else False
                ),
                "change_count": (
                    domain.change_count if domain is not None else 0
                ),
                "affected_day_count": (
                    len(domain.affected_day_ids) if domain is not None else 0
                ),
                "invalidated_day_count": (
                    len(domain.invalidated_day_ids)
                    if domain is not None
                    else 0
                ),
                "problem_count": (
                    len(domain.problems) if domain is not None else 0
                ),
                "canonical_write_outcome": write_outcome,
                "canonical_write_performed": write_performed,
                "apply_authority": False,
                "provider_runtime_state_exposed": False,
                "evidence_binding_digest_exposed": False,
            },
            "problems": problems,
            "retryable": retryable,
            "pending_review_retained": self.pending_review_retained,
            "next_action": self.next_action,
            "requires_user_review": self.status == "waiting_approval",
        }

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("tripctl apply outcomes are non-serializable")


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


def capture_trip_schedule_apply_response(
    review: TripctlScheduleApplyReview,
    kind: GuidedCanonicalApplyResponseKind,
    *,
    evaluation_at: datetime,
) -> TripctlScheduleApplyResponse:
    """Capture one exact enum response without writing canonical state."""

    if type(review) is not TripctlScheduleApplyReview:
        raise TripctlApplyResponseError("INVALID_APPLY_REVIEW")
    if type(kind) is not GuidedCanonicalApplyResponseKind:
        raise TripctlApplyResponseError("INVALID_APPLY_RESPONSE_KIND")
    captured = _response_aware_utc(evaluation_at)
    try:
        review.verify()
    except (TypeError, ValueError) as exc:
        raise TripctlApplyResponseError("STALE_APPLY_REVIEW") from exc
    stager = _schedule_stager(review)
    if stager.pending_review is not review._review._preview:
        raise TripctlApplyResponseError("STALE_APPLY_REVIEW")
    if not review.created_at <= captured <= review.expires_at:
        stager.cancel_pending()
        raise TripctlApplyResponseError("APPLY_REVIEW_EXPIRED")
    if review._review._responded:
        raise TripctlApplyResponseError("APPLY_RESPONSE_ALREADY_CAPTURED")
    try:
        guided_response = capture_guided_canonical_apply_response(
            review._review,
            kind,
            evaluation_at=captured,
        )
    except (TypeError, ValueError) as exc:
        raise TripctlApplyResponseError(
            "APPLY_RESPONSE_CAPTURE_REJECTED"
        ) from exc
    return TripctlScheduleApplyResponse(
        proposal_ref=review.proposal_ref,
        runtime_context_ref=review.runtime_context_ref,
        kind=kind,
        captured_at=guided_response.captured_at,
        _review=review,
        _response=guided_response,
        _token=_RESPONSE_TOKEN,
    )


def execute_trip_schedule_apply_response(
    review: TripctlScheduleApplyReview,
    response: TripctlScheduleApplyResponse,
    store: TripStore,
    *,
    evaluation_at: datetime,
    approvals: Sequence[ApprovalGrant] = (),
) -> TripctlScheduleApplyOutcome:
    """Execute one captured response through the existing schedule gate."""

    if (
        type(review) is not TripctlScheduleApplyReview
        or type(response) is not TripctlScheduleApplyResponse
        or type(store) is not TripStore
    ):
        raise TripctlApplyResponseError("INVALID_APPLY_EXECUTION_INPUT")
    evaluated = _response_aware_utc(evaluation_at)
    approval_values = tuple(approvals)
    if any(type(item) is not ApprovalGrant for item in approval_values):
        raise TripctlApplyResponseError("INVALID_APPLY_APPROVALS")
    try:
        review.verify()
        response.verify()
    except (TypeError, ValueError) as exc:
        raise TripctlApplyResponseError("STALE_APPLY_RESPONSE") from exc
    if response._review is not review:
        raise TripctlApplyResponseError("APPLY_RESPONSE_REVIEW_MISMATCH")
    if (
        store.slug != review._review.trip_slug
        or store.target_binding_digest
        != review._review._store_target_digest
    ):
        raise TripctlApplyResponseError("APPLY_STORE_MISMATCH")

    with response._execution_lock:
        if response._terminal:
            raise TripctlApplyResponseError(
                "APPLY_RESPONSE_ALREADY_EXECUTED"
            )
        if evaluated < response._last_evaluated_at:
            raise TripctlApplyResponseError(
                "APPLY_EXECUTION_CLOCK_ROLLBACK"
            )
        object.__setattr__(response, "_last_evaluated_at", evaluated)
        if evaluated > review.expires_at:
            _schedule_stager(review).cancel_pending()
            object.__setattr__(response, "_terminal", True)
            raise TripctlApplyResponseError("APPLY_REVIEW_EXPIRED")
        try:
            guided_outcome = execute_guided_canonical_apply_response(
                review._review,
                response._response,
                store,
                evaluation_at=evaluated,
                approvals=approval_values,
            )
        except (TypeError, ValueError) as exc:
            raise TripctlApplyResponseError(
                "APPLY_RESPONSE_EXECUTION_REJECTED"
            ) from exc
        stager = _schedule_stager(review)
        if response.kind in {
            GuidedCanonicalApplyResponseKind.REQUEST_CHANGES,
            GuidedCanonicalApplyResponseKind.CANCEL,
        }:
            stager.cancel_pending()
        pending = stager.has_pending_review
        retryable_in_process = (
            pending
            and guided_outcome.status
            in {"outcome_unknown", "waiting_approval"}
        )
        if not retryable_in_process:
            object.__setattr__(response, "_terminal", True)
        return TripctlScheduleApplyOutcome(
            proposal_ref=review.proposal_ref,
            review_id=review.review_id,
            response_kind=response.kind,
            pending_review_retained=pending,
            _outcome=guided_outcome,
            _token=_OUTCOME_TOKEN,
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


def _response_aware_utc(value: object) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise TripctlApplyResponseError("INVALID_APPLY_RESPONSE_TIME")
    return value.astimezone(timezone.utc)


def _schedule_stager(review: TripctlScheduleApplyReview) -> ScheduleStager:
    subject = review._review._subject
    if type(subject) is not ScheduleStager:
        raise TripctlApplyResponseError("STALE_APPLY_REVIEW")
    return subject


def _safe_schedule_problems(
    result: ScheduleCommitResult | None,
    *,
    status: str,
) -> list[dict[str, Any]]:
    if result is None:
        return []
    counts: dict[str, int] = {}
    for problem in result.problems:
        counts[problem.code] = counts.get(problem.code, 0) + 1
    severity = "warning" if status == "waiting_external" else "error"
    return [
        {
            "code": code,
            "severity": severity,
            "affected_count": count,
        }
        for code, count in sorted(counts.items())
    ]


def _context_binding_digest(runtime_context_ref: str) -> str:
    if _PLAIN_DIGEST_RE.fullmatch(runtime_context_ref) is None:
        raise ValueError("runtime_context_ref must be a plain digest")
    return hashlib.sha256(
        _CONTEXT_BINDING_DOMAIN + runtime_context_ref.encode("ascii")
    ).hexdigest()


__all__ = [
    "TRIPCTL_APPLY_OUTCOME_VERSION",
    "TRIPCTL_APPLY_REVIEW_VERSION",
    "TRIPCTL_APPLY_RESPONSE_VERSION",
    "TripctlApplyReviewError",
    "TripctlApplyResponseError",
    "TripctlScheduleApplyOutcome",
    "TripctlScheduleApplyReview",
    "TripctlScheduleApplyResponse",
    "capture_trip_schedule_apply_response",
    "execute_trip_schedule_apply_response",
    "prepare_trip_schedule_apply_review",
]
