"""Always-explicit Phase 5.32 review/response gate for canonical writes.

This is a thin controller over existing TripStore primitives.  It neither
constructs patches nor mints ApprovalGrant or lodging-confirmation authority.
For repair and schedule reviews, the exact typed ``ACCEPT_APPLY`` response is
the user checkpoint and is translated into a same-review HumanCheckpointGrant.
It only permits the exact reviewed create, migration, repair, schedule, or
lodging action to be handed to its existing persistence boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import InitVar, dataclass, field, fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any

from .baseline_adoption import (
    MigratedBaselineAdoptionResult,
    MigratedBaselineAdoptionReview,
    MigratedBaselineAdoptionStager,
    MigratedBaselineAdoptionState,
)
from .migrations import MigrationPreview
from .lodging_confirmation import (
    LodgingConfirmationResult,
    LodgingConfirmationReview,
    LodgingConfirmationState,
    LodgingConfirmationStager,
)
from .mutations import (
    ApprovalGrant,
    LodgingConfirmationGrant,
)
from .plan_creation import PlanCreatePreview, PlanCreateRequest
from .repair_loop import (
    HumanCheckpointGrant,
    ProposalReview,
    RepairController,
    RepairResult,
    RepairState,
)
from .schedule_staging import (
    ScheduleCommitResult,
    ScheduleStageReview,
    ScheduleStageState,
    ScheduleStager,
)
from .store import StoreResult, TripStore


GUIDED_CANONICAL_APPLY_VERSION = "guided-canonical-apply/v1"
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_REVIEW_LIFETIME = timedelta(minutes=30)
_REVIEW_TOKEN = object()
_RESPONSE_TOKEN = object()
_OUTCOME_TOKEN = object()
_MAX_ACTIVE_REVIEW_DECISIONS = 4096
_REVIEW_DECISION_LOCK = threading.Lock()
_REVIEW_DECISIONS: dict[
    str,
    tuple[datetime, str, GuidedCanonicalApplyResponseKind],
] = {}


class GuidedCanonicalApplyActionKind(str, Enum):
    CREATE_PLAN = "create_plan"
    MIGRATE_LEGACY = "migrate_legacy"
    APPLY_REPAIR_PATCH = "apply_repair_patch"
    APPLY_SCHEDULE_PATCH = "apply_schedule_patch"
    APPLY_LODGING_PATCH = "apply_lodging_patch"
    ADOPT_MIGRATED_BASELINE = "adopt_migrated_baseline"


class GuidedCanonicalApplyResponseKind(str, Enum):
    ACCEPT_APPLY = "accept_apply"
    REQUEST_CHANGES = "request_changes"
    CANCEL = "cancel"


class _Sealed:
    __slots__ = ("_sealed",)

    def _seal(self) -> None:
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError(f"{type(self).__name__} is immutable")
        object.__setattr__(self, name, value)


class GuidedCanonicalApplyReview(_Sealed):
    """One expiring exact review; safe output never contains provider state."""

    __slots__ = (
        "action_kind",
        "trip_slug",
        "trip_id",
        "created_at",
        "expires_at",
        "review_id",
        "_subject",
        "_preview",
        "_evidence_binding_digest",
        "_approval_binding_digest",
        "_context_binding_digest",
        "_store_target_digest",
        "_preview_fingerprint",
        "_responded",
        "_captured_response_id",
        "_captured_response_kind",
        "_response_lock",
    )

    def __init__(
        self,
        *,
        action_kind: GuidedCanonicalApplyActionKind,
        trip_slug: str,
        trip_id: str,
        subject: object,
        preview: object,
        evidence_binding_digest: str,
        approval_binding_digest: str,
        store_target_digest: str,
        created_at: datetime,
        expires_at: datetime | None = None,
        context_binding_digest: str | None = None,
        _token: object | None = None,
    ) -> None:
        if _token is not _REVIEW_TOKEN:
            raise ValueError("Canonical apply reviews require the review gate")
        if (
            type(action_kind) is not GuidedCanonicalApplyActionKind
            or type(trip_slug) is not str
            or not trip_slug
            or type(trip_id) is not str
            or not trip_id
        ):
            raise ValueError("Canonical apply review identity is invalid")
        created = _aware_utc(created_at, "created_at")
        evidence_digest = _digest(
            evidence_binding_digest,
            "evidence_binding_digest",
        )
        approvals_digest = _digest(
            approval_binding_digest,
            "approval_binding_digest",
        )
        context_digest = (
            None
            if context_binding_digest is None
            else _digest(context_binding_digest, "context_binding_digest")
        )
        target_digest = _digest(store_target_digest, "store_target_digest")
        expires = (
            created + _REVIEW_LIFETIME
            if expires_at is None
            else _aware_utc(expires_at, "expires_at")
        )
        if not created < expires <= created + _REVIEW_LIFETIME:
            raise ValueError("Canonical apply review expiry is invalid")
        preview_fingerprint = _preview_fingerprint(
            action_kind,
            subject,
            preview,
            evidence_digest,
            approvals_digest,
            context_digest,
        )
        review_id = _sha256(
            {
                "contract_version": GUIDED_CANONICAL_APPLY_VERSION,
                "domain": "guided-canonical-apply-review",
                "action_kind": action_kind.value,
                "trip_slug": trip_slug,
                "trip_id": trip_id,
                "store_target_digest": target_digest,
                "preview_fingerprint": preview_fingerprint,
                "created_at": created.isoformat(),
                "expires_at": expires.isoformat(),
            }
        )
        self.action_kind = action_kind
        self.trip_slug = trip_slug
        self.trip_id = trip_id
        self.created_at = created
        self.expires_at = expires
        self.review_id = review_id
        self._subject = subject
        self._preview = preview
        self._evidence_binding_digest = evidence_digest
        self._approval_binding_digest = approvals_digest
        self._context_binding_digest = context_digest
        self._store_target_digest = target_digest
        self._preview_fingerprint = preview_fingerprint
        self._responded = False
        self._captured_response_id = None
        self._captured_response_kind = None
        self._response_lock = threading.Lock()
        self._seal()

    def __repr__(self) -> str:
        return (
            "GuidedCanonicalApplyReview("
            f"action_kind={self.action_kind.value!r}, "
            f"trip_slug={self.trip_slug!r}, review_id={self.review_id!r})"
        )

    def verify(self) -> None:
        if (
            _preview_fingerprint(
                self.action_kind,
                self._subject,
                self._preview,
                self._evidence_binding_digest,
                self._approval_binding_digest,
                self._context_binding_digest,
            )
            != self._preview_fingerprint
            or _sha256(
                {
                    "contract_version": GUIDED_CANONICAL_APPLY_VERSION,
                    "domain": "guided-canonical-apply-review",
                    "action_kind": self.action_kind.value,
                    "trip_slug": self.trip_slug,
                    "trip_id": self.trip_id,
                    "store_target_digest": self._store_target_digest,
                    "preview_fingerprint": self._preview_fingerprint,
                    "created_at": self.created_at.isoformat(),
                    "expires_at": self.expires_at.isoformat(),
                }
            )
            != self.review_id
            or not self.created_at
            < self.expires_at
            <= self.created_at + _REVIEW_LIFETIME
            or self._responded
            and (
                type(self._captured_response_kind)
                is not GuidedCanonicalApplyResponseKind
                or not isinstance(self._captured_response_id, str)
                or _DIGEST_RE.fullmatch(self._captured_response_id) is None
            )
            or not self._responded
            and (
                self._captured_response_kind is not None
                or self._captured_response_id is not None
            )
        ):
            raise ValueError("Canonical apply review no longer matches its preview")

    def patch_changes(self) -> tuple[dict[str, Any], ...]:
        """Return explicit canonical changes for a human review surface."""

        self.verify()
        if type(self._preview) in {ProposalReview, ScheduleStageReview}:
            return tuple(item.to_dict() for item in self._preview.changes)
        return ()

    def to_dict(self) -> dict[str, Any]:
        self.verify()
        result: dict[str, Any] = {
            "contract_version": GUIDED_CANONICAL_APPLY_VERSION,
            "status": "review_required",
            "action_kind": self.action_kind.value,
            "trip_slug": self.trip_slug,
            "trip_id": self.trip_id,
            "review_id": self.review_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "requires_user_review": True,
            "response_kinds": [item.value for item in GuidedCanonicalApplyResponseKind],
            "candidate_plan_exposed": (
                self.action_kind is GuidedCanonicalApplyActionKind.CREATE_PLAN
            ),
            "provider_runtime_state_exposed": False,
            "evidence_binding_digest_exposed": False,
            "canonical_write_performed": False,
        }
        if self._context_binding_digest is not None:
            result["product_context_bound"] = True
            result["product_context_digest_exposed"] = False
        if type(self._preview) is PlanCreatePreview:
            result["preview"] = self._preview.to_safe_dict()
            result["proposal"] = self._subject.to_review_payload()
        elif type(self._preview) is MigrationPreview:
            result["preview"] = self._preview.to_dict()
        elif type(self._preview) in {
            ProposalReview,
            ScheduleStageReview,
            LodgingConfirmationReview,
            MigratedBaselineAdoptionReview,
        }:
            result["preview"] = _safe_domain_review(self._preview)
            result["changes"] = list(self.patch_changes())
        return result

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("Canonical apply reviews are non-serializable")


@dataclass(frozen=True, slots=True, repr=False)
class GuidedCanonicalApplyResponse:
    review_id: str
    kind: GuidedCanonicalApplyResponseKind
    captured_at: datetime
    response_id: str = ""
    contract_version: str = GUIDED_CANONICAL_APPLY_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _RESPONSE_TOKEN:
            raise ValueError("Canonical apply responses require the capture gate")
        _digest(self.review_id, "review_id")
        if type(self.kind) is not GuidedCanonicalApplyResponseKind:
            raise TypeError("Canonical apply response kind must be exact")
        captured = _aware_utc(self.captured_at, "captured_at")
        expected = _sha256(
            {
                "contract_version": self.contract_version,
                "review_id": self.review_id,
                "kind": self.kind.value,
                "captured_at": captured.isoformat(),
            }
        )
        if self.response_id and self.response_id != expected:
            raise ValueError("Canonical apply response ID is invalid")
        object.__setattr__(self, "captured_at", captured)
        object.__setattr__(self, "response_id", expected)

    def to_dict(self) -> dict[str, Any]:
        self.verify()
        return {
            "contract_version": self.contract_version,
            "review_id": self.review_id,
            "kind": self.kind.value,
            "captured_at": self.captured_at.isoformat(),
            "response_id": self.response_id,
            "canonical_write_performed": False,
        }

    def verify(self) -> None:
        captured = _aware_utc(self.captured_at, "captured_at")
        if type(self.kind) is not GuidedCanonicalApplyResponseKind:
            raise ValueError(
                "Canonical apply response no longer matches its capture"
            )
        expected = _sha256(
            {
                "contract_version": GUIDED_CANONICAL_APPLY_VERSION,
                "review_id": self.review_id,
                "kind": self.kind.value,
                "captured_at": captured.isoformat(),
            }
        )
        if (
            self.contract_version != GUIDED_CANONICAL_APPLY_VERSION
            or self.response_id != expected
        ):
            raise ValueError(
                "Canonical apply response no longer matches its capture"
            )


class GuidedCanonicalApplyOutcome(_Sealed):
    __slots__ = ("status", "next_action", "_result")

    def __init__(
        self,
        *,
        status: str,
        next_action: str,
        result: (
            StoreResult
            | RepairResult
            | ScheduleCommitResult
            | LodgingConfirmationResult
            | MigratedBaselineAdoptionResult
            | None
        ),
        _token: object | None = None,
    ) -> None:
        if _token is not _OUTCOME_TOKEN:
            raise ValueError("Canonical apply outcomes require the apply gate")
        if type(status) is not str or not status or type(next_action) is not str:
            raise ValueError("Canonical apply outcome is invalid")
        if result is not None and type(result) not in {
            StoreResult,
            RepairResult,
            ScheduleCommitResult,
            LodgingConfirmationResult,
            MigratedBaselineAdoptionResult,
        }:
            raise TypeError("Canonical apply result must be an exact domain result")
        self.status = status
        self.next_action = next_action
        self._result = result
        self._seal()

    @property
    def store_result(self) -> StoreResult | None:
        return self._result if type(self._result) is StoreResult else None

    @property
    def domain_result(
        self,
    ) -> (
        RepairResult
        | ScheduleCommitResult
        | LodgingConfirmationResult
        | MigratedBaselineAdoptionResult
        | None
    ):
        if type(self._result) in {
            RepairResult,
            ScheduleCommitResult,
            LodgingConfirmationResult,
            MigratedBaselineAdoptionResult,
        }:
            return self._result
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": GUIDED_CANONICAL_APPLY_VERSION,
            "status": self.status,
            "next_action": self.next_action,
            "result": (
                self._result.to_dict(include_candidate=False)
                if type(self._result) is StoreResult
                else self._result.to_dict()
                if self._result is not None
                else None
            ),
            "canonical_write_performed": _result_changed(self._result),
        }


def prepare_guided_canonical_create_review(
    store: TripStore,
    request: PlanCreateRequest,
    *,
    evaluation_at: datetime,
) -> GuidedCanonicalApplyReview:
    if type(store) is not TripStore or type(request) is not PlanCreateRequest:
        raise TypeError("Create review sources must be exact")
    created = _aware_utc(evaluation_at, "evaluation_at")
    if created < request.evaluation_at:
        raise ValueError("Canonical review clock rolled back")
    preview = store.preview_create(request)
    return GuidedCanonicalApplyReview(
        action_kind=GuidedCanonicalApplyActionKind.CREATE_PLAN,
        trip_slug=store.slug,
        trip_id=request.trip_id,
        subject=request,
        preview=preview,
        evidence_binding_digest=request.source_binding_digest,
        approval_binding_digest="0" * 64,
        store_target_digest=store.target_binding_digest,
        created_at=created,
        _token=_REVIEW_TOKEN,
    )


def prepare_guided_canonical_migration_review(
    store: TripStore,
    *,
    evaluation_at: datetime,
) -> GuidedCanonicalApplyReview:
    if type(store) is not TripStore:
        raise TypeError("Migration review requires an exact TripStore")
    created = _aware_utc(evaluation_at, "evaluation_at")
    preview = store.preview_migration()
    return GuidedCanonicalApplyReview(
        action_kind=GuidedCanonicalApplyActionKind.MIGRATE_LEGACY,
        trip_slug=store.slug,
        trip_id=str(preview.candidate_plan["trip_id"]),
        subject=preview,
        preview=preview,
        evidence_binding_digest=preview.source_revision,
        approval_binding_digest="0" * 64,
        store_target_digest=store.target_binding_digest,
        created_at=created,
        _token=_REVIEW_TOKEN,
    )


def prepare_guided_canonical_repair_review(
    store: TripStore,
    controller: RepairController,
    domain_review: ProposalReview,
    *,
    evaluation_at: datetime,
) -> GuidedCanonicalApplyReview:
    """Wrap one exact pending repair review without bypassing its controller."""

    if (
        type(store) is not TripStore
        or type(controller) is not RepairController
        or type(domain_review) is not ProposalReview
    ):
        raise TypeError("Repair review sources must be exact")
    created = _aware_utc(evaluation_at, "evaluation_at")
    if (
        controller._repository is not store
        or controller.pending_review is not domain_review
        or not domain_review.ready_to_commit
        or domain_review.review_id is None
        or created < controller.evaluation_at
    ):
        raise ValueError("Repair controller has no matching ready review")
    evidence = (
        domain_review.snapshot.evidence_binding.binding_digest
        if domain_review.snapshot.evidence_binding is not None
        else "0" * 64
    )
    return GuidedCanonicalApplyReview(
        action_kind=GuidedCanonicalApplyActionKind.APPLY_REPAIR_PATCH,
        trip_slug=store.slug,
        trip_id=domain_review.snapshot.trip_id,
        subject=controller,
        preview=domain_review,
        evidence_binding_digest=evidence,
        approval_binding_digest="0" * 64,
        store_target_digest=store.target_binding_digest,
        created_at=created,
        _token=_REVIEW_TOKEN,
    )


def prepare_guided_canonical_schedule_review(
    store: TripStore,
    stager: ScheduleStager,
    domain_review: ScheduleStageReview,
    *,
    evaluation_at: datetime,
    context_binding_digest: str | None = None,
) -> GuidedCanonicalApplyReview:
    """Wrap one exact pending schedule review without accepting a raw patch."""

    if (
        type(store) is not TripStore
        or type(stager) is not ScheduleStager
        or type(domain_review) is not ScheduleStageReview
    ):
        raise TypeError("Schedule review sources must be exact")
    created = _aware_utc(evaluation_at, "evaluation_at")
    pending = stager._pending
    if (
        stager._repository is not store
        or stager.pending_review is not domain_review
        or pending is None
        or not domain_review.ready_to_commit
        or domain_review.review_id is None
        or created < pending.problem.evaluation_at
    ):
        raise ValueError("Schedule stager has no matching ready review")
    return GuidedCanonicalApplyReview(
        action_kind=GuidedCanonicalApplyActionKind.APPLY_SCHEDULE_PATCH,
        trip_slug=store.slug,
        trip_id=pending.patch.trip_id,
        subject=stager,
        preview=domain_review,
        evidence_binding_digest=(
            domain_review.evidence_binding_digest or "0" * 64
        ),
        approval_binding_digest="0" * 64,
        store_target_digest=store.target_binding_digest,
        created_at=created,
        context_binding_digest=context_binding_digest,
        _token=_REVIEW_TOKEN,
    )


def prepare_guided_canonical_lodging_review(
    store: TripStore,
    stager: LodgingConfirmationStager,
    domain_review: LodgingConfirmationReview,
    *,
    evaluation_at: datetime,
) -> GuidedCanonicalApplyReview:
    """Wrap one exact pending lodging review; never mint its signed grant."""

    if (
        type(store) is not TripStore
        or type(stager) is not LodgingConfirmationStager
        or type(domain_review) is not LodgingConfirmationReview
    ):
        raise TypeError("Lodging review sources must be exact")
    created = _aware_utc(evaluation_at, "evaluation_at")
    if (
        stager._repository is not store
        or stager.pending_review is not domain_review
        or not domain_review.ready_for_confirmation
        or domain_review.review_id is None
        or not domain_review.created_at <= created < domain_review.expires_at
    ):
        raise ValueError("Lodging stager has no matching live review")
    return GuidedCanonicalApplyReview(
        action_kind=GuidedCanonicalApplyActionKind.APPLY_LODGING_PATCH,
        trip_slug=store.slug,
        trip_id=domain_review.trip_id,
        subject=stager,
        preview=domain_review,
        evidence_binding_digest="0" * 64,
        approval_binding_digest="0" * 64,
        store_target_digest=store.target_binding_digest,
        created_at=created,
        expires_at=min(created + _REVIEW_LIFETIME, domain_review.expires_at),
        _token=_REVIEW_TOKEN,
    )


def prepare_guided_canonical_baseline_adoption_review(
    store: TripStore,
    stager: MigratedBaselineAdoptionStager,
    domain_review: MigratedBaselineAdoptionReview,
    *,
    evaluation_at: datetime,
) -> GuidedCanonicalApplyReview:
    """Wrap one complete migrated-baseline classification review."""

    if (
        type(store) is not TripStore
        or type(stager) is not MigratedBaselineAdoptionStager
        or type(domain_review) is not MigratedBaselineAdoptionReview
    ):
        raise TypeError("Baseline adoption review sources must be exact")
    created = _aware_utc(evaluation_at, "evaluation_at")
    if (
        stager._repository is not store
        or stager.pending_review is not domain_review
        or not domain_review.ready_to_commit
        or not domain_review.created_at <= created < domain_review.expires_at
    ):
        raise ValueError("Baseline stager has no matching live review")
    return GuidedCanonicalApplyReview(
        action_kind=(
            GuidedCanonicalApplyActionKind.ADOPT_MIGRATED_BASELINE
        ),
        trip_slug=store.slug,
        trip_id=domain_review.patch.trip_id,
        subject=stager,
        preview=domain_review,
        evidence_binding_digest="0" * 64,
        approval_binding_digest=(
            domain_review.required_approval_scope.removeprefix("sha256:")
        ),
        store_target_digest=store.target_binding_digest,
        created_at=created,
        expires_at=min(
            created + _REVIEW_LIFETIME,
            domain_review.expires_at,
        ),
        _token=_REVIEW_TOKEN,
    )


def capture_guided_canonical_apply_response(
    review: GuidedCanonicalApplyReview,
    kind: GuidedCanonicalApplyResponseKind,
    *,
    evaluation_at: datetime,
) -> GuidedCanonicalApplyResponse:
    if type(review) is not GuidedCanonicalApplyReview:
        raise TypeError("response capture requires an exact review")
    if type(kind) is not GuidedCanonicalApplyResponseKind:
        raise TypeError("response kind must be exact")
    review.verify()
    captured = _aware_utc(evaluation_at, "evaluation_at")
    if not review.created_at <= captured <= review.expires_at:
        raise ValueError("Canonical apply review is outside its response window")
    response = GuidedCanonicalApplyResponse(
        review_id=review.review_id,
        kind=kind,
        captured_at=captured,
        _token=_RESPONSE_TOKEN,
    )
    with _REVIEW_DECISION_LOCK:
        expired = tuple(
            review_id
            for review_id, (expires_at, _, _) in _REVIEW_DECISIONS.items()
            if expires_at < captured
        )
        for review_id in expired:
            del _REVIEW_DECISIONS[review_id]
        if review.review_id in _REVIEW_DECISIONS:
            raise ValueError("Canonical apply review response was already captured")
        if len(_REVIEW_DECISIONS) >= _MAX_ACTIVE_REVIEW_DECISIONS:
            raise ValueError("Canonical apply response registry is full")
        with review._response_lock:
            review.verify()
            if review._responded:
                raise ValueError(
                    "Canonical apply review response was already captured"
                )
            _REVIEW_DECISIONS[review.review_id] = (
                review.expires_at,
                response.response_id,
                response.kind,
            )
            object.__setattr__(review, "_responded", True)
            object.__setattr__(review, "_captured_response_id", response.response_id)
            object.__setattr__(review, "_captured_response_kind", response.kind)
    return response


def execute_guided_canonical_apply_response(
    review: GuidedCanonicalApplyReview,
    response: GuidedCanonicalApplyResponse,
    store: TripStore,
    *,
    evaluation_at: datetime,
    approvals: Sequence[ApprovalGrant] = (),
    lodging_confirmation: LodgingConfirmationGrant | None = None,
) -> GuidedCanonicalApplyOutcome:
    if (
        type(review) is not GuidedCanonicalApplyReview
        or type(response) is not GuidedCanonicalApplyResponse
        or type(store) is not TripStore
    ):
        raise TypeError("Canonical apply execution sources must be exact")
    review.verify()
    response.verify()
    evaluated = _aware_utc(evaluation_at, "evaluation_at")
    approval_values = tuple(approvals)
    if any(type(item) is not ApprovalGrant for item in approval_values):
        raise TypeError("approvals must contain exact ApprovalGrant values")
    if (
        lodging_confirmation is not None
        and type(lodging_confirmation) is not LodgingConfirmationGrant
    ):
        raise TypeError("lodging_confirmation must be an exact grant or None")
    with _REVIEW_DECISION_LOCK:
        registered_response = _REVIEW_DECISIONS.get(review.review_id)
    if (
        response.review_id != review.review_id
        or not review._responded
        or response.response_id != review._captured_response_id
        or response.kind is not review._captured_response_kind
        or not response.captured_at <= evaluated <= review.expires_at
        or store.slug != review.trip_slug
        or store.target_binding_digest != review._store_target_digest
        or registered_response
        != (review.expires_at, response.response_id, response.kind)
    ):
        raise ValueError("Canonical apply response no longer matches its review")
    if response.kind is GuidedCanonicalApplyResponseKind.REQUEST_CHANGES:
        if (
            review.action_kind
            is GuidedCanonicalApplyActionKind.ADOPT_MIGRATED_BASELINE
            and type(review._subject) is MigratedBaselineAdoptionStager
        ):
            review._subject.cancel_pending()
        return GuidedCanonicalApplyOutcome(
            status="changes_requested",
            next_action="prepare_revised_canonical_proposal",
            result=None,
            _token=_OUTCOME_TOKEN,
        )
    if response.kind is GuidedCanonicalApplyResponseKind.CANCEL:
        if (
            review.action_kind
            is GuidedCanonicalApplyActionKind.ADOPT_MIGRATED_BASELINE
            and type(review._subject) is MigratedBaselineAdoptionStager
        ):
            review._subject.cancel_pending()
        return GuidedCanonicalApplyOutcome(
            status="cancelled",
            next_action="stop_canonical_apply",
            result=None,
            _token=_OUTCOME_TOKEN,
        )

    if review.action_kind is GuidedCanonicalApplyActionKind.CREATE_PLAN:
        if (
            type(review._subject) is not PlanCreateRequest
            or type(review._preview) is not PlanCreatePreview
            or review._preview.request is not review._subject
        ):
            raise ValueError("Create review subject drifted")
        # Receipt reconciliation belongs inside commit_create.  Re-previewing
        # here would prevent an exact retry after an acknowledged-unknown
        # create whose atomic install actually landed.
        result: object = store.commit_create(review._preview)
    elif review.action_kind is GuidedCanonicalApplyActionKind.MIGRATE_LEGACY:
        if (
            type(review._subject) is not MigrationPreview
            or review._subject is not review._preview
        ):
            raise ValueError("Migration review subject drifted")
        result = store.commit_migration(review._preview)
    elif review.action_kind is GuidedCanonicalApplyActionKind.APPLY_REPAIR_PATCH:
        controller = review._subject
        domain_review = review._preview
        if (
            type(controller) is not RepairController
            or type(domain_review) is not ProposalReview
            or controller._repository is not store
            or controller.pending_review is not domain_review
            or domain_review.review_id is None
        ):
            raise ValueError("Repair review no longer matches its controller")
        human_grant = (
            HumanCheckpointGrant(
                review_id=domain_review.review_id,
                approved_by="guided-canonical-apply",
                approved_at=response.captured_at,
            )
            if domain_review.requires_human_checkpoint
            else None
        )
        result = controller.commit(
            domain_review.review_id,
            human_grant=human_grant,
            approvals=approval_values,
        )
    elif review.action_kind is GuidedCanonicalApplyActionKind.APPLY_SCHEDULE_PATCH:
        stager = review._subject
        domain_review = review._preview
        if (
            type(stager) is not ScheduleStager
            or type(domain_review) is not ScheduleStageReview
            or stager._repository is not store
            or stager.pending_review is not domain_review
            or domain_review.review_id is None
        ):
            raise ValueError("Schedule review no longer matches its stager")
        human_grant = (
            HumanCheckpointGrant(
                review_id=domain_review.review_id,
                approved_by="guided-canonical-apply",
                approved_at=response.captured_at,
            )
            if domain_review.requires_human_checkpoint
            else None
        )
        result = stager.commit(
            domain_review.review_id,
            human_grant=human_grant,
            approvals=approval_values,
        )
    elif review.action_kind is GuidedCanonicalApplyActionKind.APPLY_LODGING_PATCH:
        stager = review._subject
        domain_review = review._preview
        if (
            type(stager) is not LodgingConfirmationStager
            or type(domain_review) is not LodgingConfirmationReview
            or stager._repository is not store
            or stager.pending_review is not domain_review
            or domain_review.review_id is None
        ):
            raise ValueError("Lodging review no longer matches its stager")
        result = stager.commit(
            domain_review.review_id,
            confirmation=lodging_confirmation,
            approvals=approval_values,
        )
    elif (
        review.action_kind
        is GuidedCanonicalApplyActionKind.ADOPT_MIGRATED_BASELINE
    ):
        stager = review._subject
        domain_review = review._preview
        if (
            type(stager) is not MigratedBaselineAdoptionStager
            or type(domain_review) is not MigratedBaselineAdoptionReview
            or stager._repository is not store
            or stager.pending_review is not domain_review
        ):
            raise ValueError(
                "Baseline adoption review no longer matches its stager"
            )
        human_grant = HumanCheckpointGrant(
            review_id=domain_review.review_id,
            approved_by="guided-canonical-apply",
            approved_at=response.captured_at,
        )
        result = stager.commit(
            domain_review.review_id,
            human_grant=human_grant,
            approvals=approval_values,
        )
    else:  # pragma: no cover - exact enum exhaustiveness guard
        raise ValueError("Canonical apply action is unsupported")
    return _outcome_from_result(result)


def _preview_fingerprint(
    action_kind: GuidedCanonicalApplyActionKind,
    subject: object,
    preview: object,
    evidence_binding_digest: str,
    approval_binding_digest: str,
    context_binding_digest: str | None,
) -> str:
    if type(preview) is PlanCreatePreview and type(subject) is PlanCreateRequest:
        binding: object = {
            "request_digest": subject.request_digest,
            "preview_digest": preview.preview_digest,
        }
    elif type(preview) is MigrationPreview and type(subject) is MigrationPreview:
        binding = {
            "preview_digest": preview.preview_digest,
            "source_revision": preview.source_revision,
            "candidate_sha256": hashlib.sha256(preview.candidate_bytes).hexdigest(),
        }
    elif type(preview) is ProposalReview and type(subject) is RepairController:
        binding = {
            "controller_instance": id(subject),
            "run_id": subject.run_id,
            "evaluation_at": subject.evaluation_at,
            "review": preview.to_dict(),
        }
    elif type(preview) is ScheduleStageReview and type(subject) is ScheduleStager:
        binding = {
            "stager_instance": id(subject),
            "run_id": subject.run_id,
            "max_changes": subject.max_changes,
            "max_auto_changes": subject.max_auto_changes,
            "expected_solver": subject.expected_solver,
            "review": preview.to_dict(),
            **(
                {"availability_keys": subject._availability_keys}
                if subject._availability_keys
                else {}
            ),
        }
    elif (
        type(preview) is LodgingConfirmationReview
        and type(subject) is LodgingConfirmationStager
    ):
        binding = {
            "stager_instance": id(subject),
            "review": preview.to_dict(),
        }
    elif (
        type(preview) is MigratedBaselineAdoptionReview
        and type(subject) is MigratedBaselineAdoptionStager
    ):
        binding = {
            "stager_instance": id(subject),
            "run_id": subject.run_id,
            "review": preview.to_dict(),
        }
    else:
        raise TypeError("Canonical apply preview/subject pair is unsupported")
    payload = {
        "contract_version": GUIDED_CANONICAL_APPLY_VERSION,
        "action_kind": action_kind.value,
        "binding": binding,
        "evidence_binding_digest": evidence_binding_digest,
        "approval_binding_digest": approval_binding_digest,
    }
    if context_binding_digest is not None:
        payload["context_binding_digest"] = context_binding_digest
    return _sha256(payload)


def _outcome_from_result(result: object) -> GuidedCanonicalApplyOutcome:
    status: str
    next_action: str
    if type(result) is StoreResult:
        if result.success:
            status = "replay_confirmed" if result.replayed else "applied"
            next_action = "continue_planning"
        elif result.status == "commit_outcome_unknown":
            status = "outcome_unknown"
            next_action = "retry_exact_apply"
        else:
            status = "apply_failed"
            next_action = "inspect_apply_failure"
    elif type(result) is RepairResult:
        if result.applied and result.store_status == "replayed":
            status = "replay_confirmed"
            if result.state is RepairState.WAITING_EXTERNAL:
                next_action = "refresh_external_evidence"
            elif result.state is RepairState.STOPPED or result.problems:
                next_action = "inspect_post_apply_state"
            else:
                next_action = "continue_planning"
        elif result.state is RepairState.WAITING_EXTERNAL:
            status = "waiting_external"
            next_action = "refresh_external_evidence"
        elif result.applied:
            status = "applied"
            next_action = (
                "inspect_post_apply_state"
                if result.state is RepairState.STOPPED or result.problems
                else "continue_planning"
            )
        elif result.store_status == "commit_outcome_unknown" or any(
            item.code == "COMMIT_OUTCOME_UNKNOWN" for item in result.problems
        ):
            status = "outcome_unknown"
            next_action = "reconcile_exact_apply"
        elif result.state is RepairState.WAITING_APPROVAL:
            status = "waiting_approval"
            next_action = "obtain_exact_authority"
        else:
            status = "apply_failed"
            next_action = "inspect_apply_failure"
    elif type(result) is ScheduleCommitResult:
        if result.state is ScheduleStageState.REPLAY_CONFIRMED:
            status = "replay_confirmed"
            next_action = "continue_planning"
        elif result.state is ScheduleStageState.OUTCOME_UNKNOWN:
            status = "outcome_unknown"
            next_action = "retry_exact_apply"
        elif result.state is ScheduleStageState.WAITING_APPROVAL:
            status = "waiting_approval"
            next_action = "obtain_exact_authority"
        elif result.state is ScheduleStageState.WAITING_EXTERNAL:
            status = "waiting_external"
            next_action = "refresh_external_evidence"
        elif result.applied:
            status = "applied"
            next_action = "continue_planning"
        else:
            status = "apply_failed"
            next_action = "inspect_apply_failure"
    elif type(result) is LodgingConfirmationResult:
        if result.state is LodgingConfirmationState.REPLAY_CONFIRMED:
            status = "replay_confirmed"
            next_action = "continue_planning"
        elif result.state is LodgingConfirmationState.OUTCOME_UNKNOWN:
            status = "outcome_unknown"
            next_action = "retry_exact_apply"
        elif result.state in {
            LodgingConfirmationState.WAITING_APPROVAL,
            LodgingConfirmationState.WAITING_CONFIRMATION,
        }:
            status = result.state.value
            next_action = "obtain_exact_authority"
        elif result.applied:
            status = "applied"
            next_action = "continue_planning"
        else:
            status = "apply_failed"
            next_action = "inspect_apply_failure"
    elif type(result) is MigratedBaselineAdoptionResult:
        if result.state is MigratedBaselineAdoptionState.REPLAY_CONFIRMED:
            status = "replay_confirmed"
            next_action = "continue_planning"
        elif result.state is MigratedBaselineAdoptionState.ROLLED_BACK:
            status = "rolled_back"
            next_action = "prepare_fresh_review"
        elif result.state is MigratedBaselineAdoptionState.OUTCOME_UNKNOWN:
            status = "outcome_unknown"
            next_action = "retry_exact_apply"
        elif result.state in {
            MigratedBaselineAdoptionState.WAITING_CHECKPOINT,
            MigratedBaselineAdoptionState.WAITING_APPROVAL,
        }:
            status = result.state.value
            next_action = "obtain_exact_authority"
        elif result.state is MigratedBaselineAdoptionState.APPLIED:
            status = "applied"
            next_action = "continue_planning"
        else:
            status = "apply_failed"
            next_action = "inspect_apply_failure"
    else:
        raise TypeError("Canonical apply result must be exact")
    return GuidedCanonicalApplyOutcome(
        status=status,
        next_action=next_action,
        result=result,
        _token=_OUTCOME_TOKEN,
    )


def _result_changed(result: object) -> bool | None:
    if type(result) is StoreResult:
        return result.changed
    if type(result) is RepairResult:
        return result.applied and result.store_status != "replayed"
    if type(result) is ScheduleCommitResult:
        return (
            result.applied
            and result.state is not ScheduleStageState.REPLAY_CONFIRMED
        )
    if type(result) is LodgingConfirmationResult:
        return (
            result.applied
            and result.state is not LodgingConfirmationState.REPLAY_CONFIRMED
        )
    if type(result) is MigratedBaselineAdoptionResult:
        return result.canonical_write_performed
    if result is None:
        return False
    raise TypeError("Canonical apply result must be exact")


def _safe_domain_review(
    review: (
        ProposalReview
        | ScheduleStageReview
        | LodgingConfirmationReview
        | MigratedBaselineAdoptionReview
    ),
) -> dict[str, Any]:
    value = review.to_dict()
    # The digest remains privately bound into this outer review fingerprint;
    # it is not useful on the user response surface.
    value.pop("evidence_binding_digest", None)
    return value


def _aware_utc(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _digest(value: object, name: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _sha256(value: object) -> str:
    encoded = json.dumps(
        _private_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _private_value(value: object) -> object:
    if value is None or type(value) in {str, int, float, bool}:
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _aware_utc(value, "private datetime").isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _private_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_private_value(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _private_value(getattr(value, item.name))
            for item in fields(value)
        }
    raise TypeError("Canonical apply binding contains an unsupported value")


__all__ = [
    "GUIDED_CANONICAL_APPLY_VERSION",
    "GuidedCanonicalApplyActionKind",
    "GuidedCanonicalApplyOutcome",
    "GuidedCanonicalApplyResponse",
    "GuidedCanonicalApplyResponseKind",
    "GuidedCanonicalApplyReview",
    "capture_guided_canonical_apply_response",
    "execute_guided_canonical_apply_response",
    "prepare_guided_canonical_baseline_adoption_review",
    "prepare_guided_canonical_create_review",
    "prepare_guided_canonical_lodging_review",
    "prepare_guided_canonical_migration_review",
    "prepare_guided_canonical_repair_review",
    "prepare_guided_canonical_schedule_review",
]
