"""Host-only product bridge for one migrated-baseline adoption.

The bridge keeps classification, the exact apply response, and retry state in
one process.  Safe projections contain only aggregate product state; private
activity context is exposed only through an explicitly ephemeral projection.
No object in this module is a CLI token or persisted mutation authority.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Sequence
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .baseline_adoption import (
    MigratedBaselineAdoptionError,
    MigratedBaselineAdoptionResult,
    MigratedBaselineAdoptionReview,
    MigratedBaselineAdoptionStager,
    MigratedBaselineAdoptionState,
    MigratedBaselineClassificationReview,
    prepare_migrated_baseline_classification_review,
)
from .guided_canonical_apply import (
    GuidedCanonicalApplyActionKind,
    GuidedCanonicalApplyOutcome,
    GuidedCanonicalApplyResponse,
    GuidedCanonicalApplyResponseKind,
    GuidedCanonicalApplyReview,
    capture_guided_canonical_apply_response,
    execute_guided_canonical_apply_response,
    prepare_guided_canonical_baseline_adoption_review,
)
from .mutations import ApprovalGrant, MigratedActivityClassification
from .store import TripStore
from .tripctl_apply import (
    TripctlApplyResponseError,
    TripctlApplyReviewError,
)


TRIPCTL_BASELINE_APPLY_VERSION = "tripctl-baseline-apply/v1"
"""Version of the host-only migrated-baseline product facade."""

_TRIPCTL_ENVELOPE_VERSION = "tripctl/v1"
_PLAIN_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_CLASSIFICATION_TOKEN = object()
_APPLY_REVIEW_TOKEN = object()
_RESPONSE_TOKEN = object()
_OUTCOME_TOKEN = object()


@dataclass(frozen=True, slots=True, repr=False)
class TripctlBaselineClassificationReview:
    """One exact inventory and process-local classifier."""

    run_id: str
    _review: MigratedBaselineClassificationReview = field(repr=False)
    _stager: MigratedBaselineAdoptionStager = field(repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
        compare=False,
    )
    _last_evaluated_at: datetime = field(init=False, repr=False)
    _classified: bool = field(default=False, init=False, repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _CLASSIFICATION_TOKEN:
            raise ValueError(
                "tripctl baseline reviews require the preparation gate"
            )
        if type(self.run_id) is not str or not self.run_id:
            raise ValueError("baseline review run_id must be text")
        if type(self._review) is not MigratedBaselineClassificationReview:
            raise TypeError("baseline classification review must be exact")
        if type(self._stager) is not MigratedBaselineAdoptionStager:
            raise TypeError("baseline classification stager must be exact")
        object.__setattr__(self, "_last_evaluated_at", self.created_at)
        self.verify()

    @property
    def review_id(self) -> str:
        return self._review.review_id

    @property
    def created_at(self) -> datetime:
        return self._review.created_at

    @property
    def expires_at(self) -> datetime:
        return self._review.expires_at

    @property
    def classified(self) -> bool:
        with self._lock:
            return self._classified

    def verify(self) -> None:
        self._review.verify()
        if (
            self._stager.run_id != self.run_id
            or self._stager._repository.slug != self._review.trip_slug
            or self._stager._repository.target_binding_digest
            != self._review.store_target_digest
            or type(self._last_evaluated_at) is not datetime
            or self._last_evaluated_at.tzinfo is None
            or self._last_evaluated_at.utcoffset() is None
            or self._last_evaluated_at < self.created_at
        ):
            raise ValueError(
                "tripctl baseline classification context no longer matches"
            )

    def to_dict(self) -> dict[str, Any]:
        self.verify()
        return {
            "contract_version": _TRIPCTL_ENVELOPE_VERSION,
            "command": "apply",
            "ok": True,
            "status": "review_required",
            "storage_mode": "canonical",
            "result": {
                "contract_version": TRIPCTL_BASELINE_APPLY_VERSION,
                "action_kind": "adopt_migrated_baseline",
                "classification_review": self._review.to_dict(),
                "apply_authority": False,
                "canonical_write_performed": False,
            },
            "problems": [],
            "retryable": False,
            "pending_review_retained": True,
            "next_action": "classify_migrated_baseline",
            "requires_user_review": True,
        }

    def to_ephemeral_private_review_payload(self) -> dict[str, Any]:
        """Return exact private activity context for direct human review."""

        self.verify()
        return {
            "contract_version": TRIPCTL_BASELINE_APPLY_VERSION,
            "payload_handling": (
                "private_ephemeral_direct_human_review_only"
            ),
            "action_kind": "adopt_migrated_baseline",
            "classification_review": (
                self._review.to_ephemeral_private_review_payload()
            ),
            "apply_authority": False,
            "canonical_write_performed": False,
        }

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("tripctl baseline reviews are non-serializable")


@dataclass(frozen=True, slots=True, repr=False)
class TripctlBaselineApplyReview:
    """Final exact classification wrapped in the canonical apply gate."""

    classification_review: TripctlBaselineClassificationReview = field(
        repr=False
    )
    _domain_review: MigratedBaselineAdoptionReview = field(repr=False)
    _review: GuidedCanonicalApplyReview = field(repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _APPLY_REVIEW_TOKEN:
            raise ValueError(
                "tripctl baseline apply reviews require classification"
            )
        if type(self.classification_review) is not (
            TripctlBaselineClassificationReview
        ):
            raise TypeError("baseline classification handle must be exact")
        if type(self._domain_review) is not MigratedBaselineAdoptionReview:
            raise TypeError("baseline domain review must be exact")
        if type(self._review) is not GuidedCanonicalApplyReview:
            raise TypeError("baseline guided review must be exact")
        self.verify()

    @property
    def review_id(self) -> str:
        return self._review.review_id

    @property
    def created_at(self) -> datetime:
        return self._review.created_at

    @property
    def expires_at(self) -> datetime:
        return self._review.expires_at

    @property
    def classification_count(self) -> int:
        return sum(
            count for _kind, count in self._domain_review.classification_counts
        )

    @property
    def required_approval_scope(self) -> str:
        return self._domain_review.required_approval_scope

    def verify(self) -> None:
        self.classification_review.verify()
        self._domain_review.verify()
        self._review.verify()
        stager = self.classification_review._stager
        if (
            self._review.action_kind
            is not GuidedCanonicalApplyActionKind.ADOPT_MIGRATED_BASELINE
            or self._review._subject is not stager
            or self._review._preview is not self._domain_review
            or stager.pending_review is not self._domain_review
            or self._domain_review.classification_review
            is not self.classification_review._review
        ):
            raise ValueError("tripctl baseline apply review is stale")

    def to_dict(self) -> dict[str, Any]:
        self.verify()
        return {
            "contract_version": _TRIPCTL_ENVELOPE_VERSION,
            "command": "apply",
            "ok": True,
            "status": "review_required",
            "storage_mode": "canonical",
            "result": {
                "contract_version": TRIPCTL_BASELINE_APPLY_VERSION,
                "action_kind": "adopt_migrated_baseline",
                "classification_review_id": (
                    self.classification_review.review_id
                ),
                "classification_count": self.classification_count,
                "required_approval_scope": self.required_approval_scope,
                "apply_review": self._review.to_dict(),
                "apply_authority": False,
                "canonical_write_performed": False,
            },
            "problems": [],
            "retryable": False,
            "pending_review_retained": True,
            "next_action": "capture_apply_response",
            "requires_user_review": True,
        }

    def to_ephemeral_private_review_payload(self) -> dict[str, Any]:
        """Return exact choices for the final private human checkpoint."""

        self.verify()
        return {
            "contract_version": TRIPCTL_BASELINE_APPLY_VERSION,
            "payload_handling": (
                "private_ephemeral_direct_human_review_only"
            ),
            "action_kind": "adopt_migrated_baseline",
            "apply_review": (
                self._domain_review.to_ephemeral_private_review_payload()
            ),
            "apply_authority": False,
            "canonical_write_performed": False,
        }

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("tripctl baseline apply reviews are non-serializable")


@dataclass(frozen=True, slots=True, repr=False)
class TripctlBaselineApplyResponse:
    """One process-local exact response with monotonic retry state."""

    kind: GuidedCanonicalApplyResponseKind
    captured_at: datetime
    _review: TripctlBaselineApplyReview = field(repr=False)
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
            raise ValueError("tripctl baseline responses require capture")
        if type(self._review) is not TripctlBaselineApplyReview:
            raise TypeError("baseline response review must be exact")
        if type(self._response) is not GuidedCanonicalApplyResponse:
            raise TypeError("baseline guided response must be exact")
        if type(self.kind) is not GuidedCanonicalApplyResponseKind:
            raise TypeError("baseline response kind must be exact")
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
        guided_review = self._review._review
        if (
            self.kind is not self._response.kind
            or self.captured_at != self._response.captured_at
            or self._response.review_id != self.review_id
            or guided_review._captured_response_id
            != self._response.response_id
            or guided_review._captured_response_kind is not self.kind
            or type(self._last_evaluated_at) is not datetime
            or self._last_evaluated_at.tzinfo is None
            or self._last_evaluated_at.utcoffset() is None
            or self._last_evaluated_at < self.captured_at
        ):
            raise ValueError("tripctl baseline response differs from review")

    def to_dict(self) -> dict[str, Any]:
        self.verify()
        return {
            "contract_version": _TRIPCTL_ENVELOPE_VERSION,
            "command": "apply",
            "ok": True,
            "status": "response_captured",
            "storage_mode": "canonical",
            "result": {
                "contract_version": TRIPCTL_BASELINE_APPLY_VERSION,
                "action_kind": "adopt_migrated_baseline",
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
        raise TypeError("tripctl baseline responses are non-serializable")


@dataclass(frozen=True, slots=True, repr=False)
class TripctlBaselineApplyOutcome:
    """Safe aggregate after one baseline response execution."""

    review_id: str
    response_kind: GuidedCanonicalApplyResponseKind
    classification_count: int
    pending_review_retained: bool
    _outcome: GuidedCanonicalApplyOutcome = field(repr=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _OUTCOME_TOKEN:
            raise ValueError("tripctl baseline outcomes require execution")
        if _PLAIN_DIGEST_RE.fullmatch(self.review_id) is None:
            raise ValueError("baseline outcome review_id must be a digest")
        if type(self.response_kind) is not GuidedCanonicalApplyResponseKind:
            raise TypeError("baseline outcome response kind must be exact")
        if (
            isinstance(self.classification_count, bool)
            or not isinstance(self.classification_count, int)
            or self.classification_count <= 0
        ):
            raise ValueError("baseline outcome classification count is invalid")
        if type(self.pending_review_retained) is not bool:
            raise TypeError("pending baseline state must be exact bool")
        if type(self._outcome) is not GuidedCanonicalApplyOutcome:
            raise TypeError("baseline guided outcome must be exact")
        domain = self._outcome.domain_result
        if domain is not None and type(domain) is not MigratedBaselineAdoptionResult:
            raise TypeError("baseline outcome has the wrong domain result")

    @property
    def status(self) -> str:
        return self._outcome.status

    @property
    def next_action(self) -> str:
        if self.status in {"changes_requested", "rolled_back"}:
            return "prepare_fresh_baseline_classification_review"
        return self._outcome.next_action

    @property
    def domain_result(self) -> MigratedBaselineAdoptionResult | None:
        value = self._outcome.domain_result
        return (
            value if type(value) is MigratedBaselineAdoptionResult else None
        )

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
        return {
            "contract_version": _TRIPCTL_ENVELOPE_VERSION,
            "command": "apply",
            "ok": self.status not in {
                "apply_failed",
                "outcome_unknown",
                "rolled_back",
            },
            "status": self.status,
            "storage_mode": "canonical",
            "result": {
                "contract_version": TRIPCTL_BASELINE_APPLY_VERSION,
                "action_kind": "adopt_migrated_baseline",
                "review_id": self.review_id,
                "response_kind": self.response_kind.value,
                "domain_state": (
                    domain.state.value if domain is not None else None
                ),
                "classification_count": self.classification_count,
                "applied": domain.applied if domain is not None else False,
                "replay_confirmed": (
                    domain is not None
                    and domain.state
                    is MigratedBaselineAdoptionState.REPLAY_CONFIRMED
                ),
                "problem_count": (
                    len(domain.problem_codes) if domain is not None else 0
                ),
                "canonical_write_outcome": write_outcome,
                "canonical_write_performed": write_performed,
                "evidence_state_changed": False,
                "travel_ready": False,
                "apply_authority": False,
                "private_activity_data_exposed": False,
            },
            "problems": _safe_problem_counts(domain),
            "retryable": (
                self.status == "outcome_unknown"
                and self.pending_review_retained
            ),
            "pending_review_retained": self.pending_review_retained,
            "next_action": self.next_action,
            "requires_user_review": self.status == "waiting_approval",
        }

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("tripctl baseline outcomes are non-serializable")


def prepare_trip_baseline_classification_review(
    store: TripStore,
    *,
    reviewed_at: datetime,
    run_id: str,
) -> TripctlBaselineClassificationReview:
    """Prepare one exact private inventory without writing canonical state."""

    if type(store) is not TripStore:
        raise TripctlApplyReviewError("INVALID_BASELINE_STORE")
    if type(run_id) is not str or not run_id:
        raise TripctlApplyReviewError("INVALID_BASELINE_RUN_ID")
    reviewed = _review_aware_utc(reviewed_at)
    try:
        domain_review = prepare_migrated_baseline_classification_review(
            store,
            reviewed_at=reviewed,
        )
        stager = MigratedBaselineAdoptionStager(store, run_id=run_id)
        return TripctlBaselineClassificationReview(
            run_id=run_id,
            _review=domain_review,
            _stager=stager,
            _token=_CLASSIFICATION_TOKEN,
        )
    except MigratedBaselineAdoptionError as exc:
        raise TripctlApplyReviewError(exc.code) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise TripctlApplyReviewError(
            "BASELINE_CLASSIFICATION_REVIEW_UNAVAILABLE"
        ) from exc


def classify_trip_migrated_baseline(
    review: TripctlBaselineClassificationReview,
    classifications: tuple[MigratedActivityClassification, ...],
    *,
    classified_at: datetime,
    idempotency_key: str,
) -> TripctlBaselineApplyReview:
    """Classify the full inventory and prepare its final apply review."""

    if type(review) is not TripctlBaselineClassificationReview:
        raise TripctlApplyReviewError("INVALID_BASELINE_CLASSIFICATION_REVIEW")
    if type(classifications) is not tuple:
        raise TripctlApplyReviewError("INVALID_BASELINE_CLASSIFICATIONS")
    values = tuple(classifications)
    if any(type(item) is not MigratedActivityClassification for item in values):
        raise TripctlApplyReviewError("INVALID_BASELINE_CLASSIFICATIONS")
    classified = _review_aware_utc(classified_at)
    with review._lock:
        try:
            review.verify()
        except (TypeError, ValueError) as exc:
            raise TripctlApplyReviewError(
                "STALE_BASELINE_CLASSIFICATION_REVIEW"
            ) from exc
        if review._classified or review._stager.has_pending_review:
            raise TripctlApplyReviewError(
                "BASELINE_CLASSIFICATION_ALREADY_COMPLETED"
            )
        if classified < review._last_evaluated_at:
            raise TripctlApplyReviewError(
                "BASELINE_CLASSIFICATION_CLOCK_ROLLBACK"
            )
        object.__setattr__(review, "_last_evaluated_at", classified)
        try:
            domain_review = review._stager.classify(
                review._review,
                values,
                classified_at=classified,
                idempotency_key=idempotency_key,
            )
            guided_review = (
                prepare_guided_canonical_baseline_adoption_review(
                    review._stager._repository,
                    review._stager,
                    domain_review,
                    evaluation_at=classified,
                )
            )
            product_review = TripctlBaselineApplyReview(
                classification_review=review,
                _domain_review=domain_review,
                _review=guided_review,
                _token=_APPLY_REVIEW_TOKEN,
            )
        except MigratedBaselineAdoptionError as exc:
            raise TripctlApplyReviewError(exc.code) from exc
        except (OSError, TypeError, ValueError) as exc:
            review._stager.cancel_pending()
            raise TripctlApplyReviewError(
                "BASELINE_APPLY_REVIEW_UNAVAILABLE"
            ) from exc
        object.__setattr__(review, "_classified", True)
        return product_review


def capture_trip_baseline_apply_response(
    review: TripctlBaselineApplyReview,
    kind: GuidedCanonicalApplyResponseKind,
    *,
    evaluation_at: datetime,
) -> TripctlBaselineApplyResponse:
    """Capture one exact response without creating external approval."""

    if type(review) is not TripctlBaselineApplyReview:
        raise TripctlApplyResponseError("INVALID_BASELINE_APPLY_REVIEW")
    if type(kind) is not GuidedCanonicalApplyResponseKind:
        raise TripctlApplyResponseError("INVALID_APPLY_RESPONSE_KIND")
    captured = _response_aware_utc(evaluation_at)
    try:
        review.verify()
    except (TypeError, ValueError) as exc:
        raise TripctlApplyResponseError("STALE_BASELINE_APPLY_REVIEW") from exc
    stager = review.classification_review._stager
    if not review.created_at <= captured <= review.expires_at:
        stager.cancel_pending()
        raise TripctlApplyResponseError("APPLY_REVIEW_EXPIRED")
    try:
        guided_response = capture_guided_canonical_apply_response(
            review._review,
            kind,
            evaluation_at=captured,
        )
        return TripctlBaselineApplyResponse(
            kind=kind,
            captured_at=guided_response.captured_at,
            _review=review,
            _response=guided_response,
            _token=_RESPONSE_TOKEN,
        )
    except (TypeError, ValueError) as exc:
        raise TripctlApplyResponseError(
            "APPLY_RESPONSE_CAPTURE_REJECTED"
        ) from exc


def execute_trip_baseline_apply_response(
    review: TripctlBaselineApplyReview,
    response: TripctlBaselineApplyResponse,
    store: TripStore,
    *,
    evaluation_at: datetime,
    approvals: Sequence[ApprovalGrant] = (),
) -> TripctlBaselineApplyOutcome:
    """Execute or exact-retry one process-local baseline response."""

    if (
        type(review) is not TripctlBaselineApplyReview
        or type(response) is not TripctlBaselineApplyResponse
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
        or store.target_binding_digest != review._review._store_target_digest
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
        stager = review.classification_review._stager
        if evaluated > review.expires_at:
            stager.cancel_pending()
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
        pending = stager.has_pending_review
        retryable_in_process = (
            pending
            and guided_outcome.status
            in {
                "outcome_unknown",
                "waiting_approval",
                "waiting_checkpoint",
            }
        )
        if not retryable_in_process:
            object.__setattr__(response, "_terminal", True)
        return TripctlBaselineApplyOutcome(
            review_id=review.review_id,
            response_kind=response.kind,
            classification_count=review.classification_count,
            pending_review_retained=pending,
            _outcome=guided_outcome,
            _token=_OUTCOME_TOKEN,
        )


def _review_aware_utc(value: object) -> datetime:
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


def _safe_problem_counts(
    result: MigratedBaselineAdoptionResult | None,
) -> list[dict[str, Any]]:
    if result is None:
        return []
    counts: dict[str, int] = {}
    for code in result.problem_codes:
        counts[code] = counts.get(code, 0) + 1
    return [
        {"code": code, "severity": "error", "affected_count": count}
        for code, count in sorted(counts.items())
    ]


__all__ = [
    "TRIPCTL_BASELINE_APPLY_VERSION",
    "TripctlBaselineApplyOutcome",
    "TripctlBaselineApplyResponse",
    "TripctlBaselineApplyReview",
    "TripctlBaselineClassificationReview",
    "capture_trip_baseline_apply_response",
    "classify_trip_migrated_baseline",
    "execute_trip_baseline_apply_response",
    "prepare_trip_baseline_classification_review",
]
