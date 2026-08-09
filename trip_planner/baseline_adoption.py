"""One-time review, classification, and adoption of a migrated baseline.

The flow is deliberately process-local.  A private classification review binds
the exact canonical revision and protected activity order.  A complete typed
classification stages one sole-operation patch.  Canonical commit still needs
both the existing human checkpoint and a separate exact ``ApprovalGrant``.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Sequence

from .mutations import (
    AdoptMigratedBaseline,
    ApprovalGrant,
    MigratedActivityClassification,
    MigratedActivityClassificationKind,
    PlanPatch,
    patch_digest,
)
from .repair_loop import HumanCheckpointGrant
from .store import StoreResult, TripStore


MIGRATED_BASELINE_ADOPTION_VERSION = "migrated-baseline-adoption/v1"
_SUPPORTED_SOURCE_SCHEMA = "legacy-v1"
_REVIEW_LIFETIME = timedelta(minutes=30)
_CLASSIFICATION_REVIEW_TOKEN = object()
_ADOPTION_REVIEW_TOKEN = object()


class MigratedBaselineAdoptionError(ValueError):
    """Bounded preparation or execution rejection."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not code:
            raise ValueError("baseline adoption error code must be text")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _PrivateActivityReviewItem:
    activity_id: str
    day_id: str
    day_number: int
    day_date: str
    title: str
    scheduled_time: str | None
    current_decision_state: str | None
    current_flexibility: str | None

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "activity_id": self.activity_id,
            "day_id": self.day_id,
            "day_number": self.day_number,
            "day_date": self.day_date,
            "title": self.title,
            "scheduled_time": self.scheduled_time,
            "current_decision_state": self.current_decision_state,
            "current_flexibility": self.current_flexibility,
        }


@dataclass(frozen=True, slots=True, repr=False)
class MigratedBaselineClassificationReview:
    """Exact private inventory to classify before adoption is proposed."""

    trip_slug: str
    trip_id: str
    base_revision: str
    source_schema: str
    source_revision: str
    protected_activity_ids: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    store_target_digest: str = field(repr=False)
    _items: tuple[_PrivateActivityReviewItem, ...] = field(repr=False)
    review_id: str = field(init=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _CLASSIFICATION_REVIEW_TOKEN:
            raise ValueError(
                "classification reviews require the preparation gate"
            )
        created = _aware_utc(self.created_at, "created_at")
        expires = _aware_utc(self.expires_at, "expires_at")
        if not created < expires <= created + _REVIEW_LIFETIME:
            raise ValueError("classification review expiry is invalid")
        if not self.protected_activity_ids or len(
            set(self.protected_activity_ids)
        ) != len(self.protected_activity_ids):
            raise ValueError("protected activity inventory is invalid")
        if (
            any(
                not isinstance(activity_id, str) or not activity_id
                for activity_id in self.protected_activity_ids
            )
            or not isinstance(self.source_revision, str)
            or not self.source_revision
            or self.source_schema != _SUPPORTED_SOURCE_SCHEMA
        ):
            raise ValueError("migration classification context is invalid")
        if tuple(item.activity_id for item in self._items) != (
            self.protected_activity_ids
        ):
            raise ValueError("private review items differ from protection order")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(
            self,
            "review_id",
            _sha256(
                {
                    "contract_version": MIGRATED_BASELINE_ADOPTION_VERSION,
                    "domain": "classification-review",
                    "trip_slug": self.trip_slug,
                    "trip_id": self.trip_id,
                    "base_revision": self.base_revision,
                    "source_schema": self.source_schema,
                    "source_revision": self.source_revision,
                    "protected_activity_ids": self.protected_activity_ids,
                    "created_at": created.isoformat(),
                    "expires_at": expires.isoformat(),
                    "store_target_digest": self.store_target_digest,
                    "private_items": [item.to_dict() for item in self._items],
                }
            ),
        )

    def verify(self) -> None:
        rebuilt = MigratedBaselineClassificationReview(
            trip_slug=self.trip_slug,
            trip_id=self.trip_id,
            base_revision=self.base_revision,
            source_schema=self.source_schema,
            source_revision=self.source_revision,
            protected_activity_ids=self.protected_activity_ids,
            created_at=self.created_at,
            expires_at=self.expires_at,
            store_target_digest=self.store_target_digest,
            _items=self._items,
            _token=_CLASSIFICATION_REVIEW_TOKEN,
        )
        if rebuilt.review_id != self.review_id:
            raise ValueError("classification review no longer matches")

    def to_dict(self) -> dict[str, Any]:
        self.verify()
        return {
            "contract_version": MIGRATED_BASELINE_ADOPTION_VERSION,
            "status": "review_required",
            "review_id": self.review_id,
            "trip_slug": self.trip_slug,
            "base_revision": self.base_revision,
            "protected_activity_count": len(self.protected_activity_ids),
            "classification_kinds": [
                item.value for item in MigratedActivityClassificationKind
            ],
            "private_activity_data_exposed": False,
            "canonical_write_performed": False,
            "next_action": "classify_all_migrated_activities",
        }

    def to_ephemeral_private_review_payload(self) -> dict[str, Any]:
        """Return the exact ephemeral activity context for a private UI."""

        self.verify()
        return {
            "contract_version": MIGRATED_BASELINE_ADOPTION_VERSION,
            "payload_handling": (
                "private_ephemeral_direct_human_review_only"
            ),
            "review_id": self.review_id,
            "source_schema": self.source_schema,
            "activities": [item.to_dict() for item in self._items],
            "classification_kinds": [
                item.value for item in MigratedActivityClassificationKind
            ],
            "complete_classification_required": True,
            "partial_adoption_supported": False,
        }

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("classification reviews are process-local")


@dataclass(frozen=True, slots=True, repr=False)
class MigratedBaselineAdoptionReview:
    """One exact pending classification patch ready for canonical review."""

    classification_review: MigratedBaselineClassificationReview = field(
        repr=False
    )
    patch: PlanPatch = field(repr=False)
    run_id: str
    required_approval_scope: str
    check_status: str
    change_count: int
    classification_counts: tuple[tuple[str, int], ...]
    created_at: datetime
    expires_at: datetime
    store_target_digest: str = field(repr=False)
    review_id: str = field(init=False)
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _ADOPTION_REVIEW_TOKEN:
            raise ValueError("adoption reviews require the classification gate")
        if type(self.classification_review) is not (
            MigratedBaselineClassificationReview
        ):
            raise TypeError("classification review must be exact")
        self.classification_review.verify()
        if type(self.patch) is not PlanPatch or len(self.patch.operations) != 1:
            raise TypeError("adoption review requires one exact patch")
        if type(self.patch.operations[0]) is not AdoptMigratedBaseline:
            raise TypeError("adoption review patch has the wrong operation")
        created = _aware_utc(self.created_at, "created_at")
        expires = _aware_utc(self.expires_at, "expires_at")
        if not created < expires <= min(
            created + _REVIEW_LIFETIME,
            self.classification_review.expires_at,
        ):
            raise ValueError("adoption review expiry is invalid")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(
            self,
            "review_id",
            _sha256(
                {
                    "contract_version": MIGRATED_BASELINE_ADOPTION_VERSION,
                    "domain": "adoption-review",
                    "classification_review_id": (
                        self.classification_review.review_id
                    ),
                    "patch_digest": patch_digest(self.patch),
                    "run_id": self.run_id,
                    "required_approval_scope": (
                        self.required_approval_scope
                    ),
                    "check_status": self.check_status,
                    "change_count": self.change_count,
                    "classification_counts": self.classification_counts,
                    "created_at": created.isoformat(),
                    "expires_at": expires.isoformat(),
                    "store_target_digest": self.store_target_digest,
                }
            ),
        )

    @property
    def ready_to_commit(self) -> bool:
        return True

    @property
    def requires_human_checkpoint(self) -> bool:
        return True

    def verify(self) -> None:
        rebuilt = MigratedBaselineAdoptionReview(
            classification_review=self.classification_review,
            patch=self.patch,
            run_id=self.run_id,
            required_approval_scope=self.required_approval_scope,
            check_status=self.check_status,
            change_count=self.change_count,
            classification_counts=self.classification_counts,
            created_at=self.created_at,
            expires_at=self.expires_at,
            store_target_digest=self.store_target_digest,
            _token=_ADOPTION_REVIEW_TOKEN,
        )
        if rebuilt.review_id != self.review_id:
            raise ValueError("adoption review no longer matches")

    def to_dict(self) -> dict[str, Any]:
        self.verify()
        return {
            "contract_version": MIGRATED_BASELINE_ADOPTION_VERSION,
            "state": "review_required",
            "review_id": self.review_id,
            "classification_review_id": self.classification_review.review_id,
            "base_revision": self.patch.base_revision,
            "classification_count": sum(
                count for _kind, count in self.classification_counts
            ),
            "classification_counts": dict(self.classification_counts),
            "change_count": self.change_count,
            "check_status": self.check_status,
            "required_approval_scope": self.required_approval_scope,
            "requires_human_checkpoint": True,
            "partial_adoption_supported": False,
            "canonical_write_performed": False,
        }

    def to_ephemeral_private_review_payload(self) -> dict[str, Any]:
        """Return the exact classifications for the final private checkpoint."""

        self.verify()
        operation = self.patch.operations[0]
        assert isinstance(operation, AdoptMigratedBaseline)
        choices = {
            item.activity_id: item.kind
            for item in operation.classifications
        }
        items = []
        for item in self.classification_review._items:
            value = item.to_dict()
            choice = choices[item.activity_id]
            value["classification_kind"] = choice.value
            value["resulting_decision_state"] = choice.decision_state
            value["resulting_flexibility"] = choice.flexibility
            items.append(value)
        return {
            "contract_version": MIGRATED_BASELINE_ADOPTION_VERSION,
            "payload_handling": (
                "private_ephemeral_direct_human_review_only"
            ),
            "review_id": self.review_id,
            "classification_review_id": self.classification_review.review_id,
            "activities": items,
            "required_approval_scope": self.required_approval_scope,
            "complete_classification": True,
            "partial_adoption_supported": False,
            "canonical_write_performed": False,
        }

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("adoption reviews are process-local")


class MigratedBaselineAdoptionState(str, Enum):
    WAITING_CHECKPOINT = "waiting_checkpoint"
    WAITING_APPROVAL = "waiting_approval"
    APPLIED = "applied"
    REPLAY_CONFIRMED = "replay_confirmed"
    ROLLED_BACK = "rolled_back"
    OUTCOME_UNKNOWN = "outcome_unknown"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class MigratedBaselineAdoptionResult:
    state: MigratedBaselineAdoptionState
    applied: bool
    store_status: str | None
    replayed: bool
    canonical_write_performed: bool | None
    pending_review_retained: bool
    required_approval_scope: str
    problem_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": MIGRATED_BASELINE_ADOPTION_VERSION,
            "state": self.state.value,
            "applied": self.applied,
            "store_status": self.store_status,
            "replayed": self.replayed,
            "canonical_write_performed": self.canonical_write_performed,
            "pending_review_retained": self.pending_review_retained,
            "required_approval_scope": self.required_approval_scope,
            "problem_codes": list(self.problem_codes),
        }


class MigratedBaselineAdoptionStager:
    """Hold one exact classified baseline pending canonical authority."""

    def __init__(self, repository: TripStore, *, run_id: str) -> None:
        if type(repository) is not TripStore:
            raise TypeError("baseline adoption requires an exact TripStore")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("baseline adoption run_id must be text")
        self._repository = repository
        self.run_id = run_id
        self._pending: MigratedBaselineAdoptionReview | None = None
        self._lock = threading.Lock()

    @property
    def pending_review(self) -> MigratedBaselineAdoptionReview | None:
        with self._lock:
            return self._pending

    @property
    def has_pending_review(self) -> bool:
        return self.pending_review is not None

    def classify(
        self,
        review: MigratedBaselineClassificationReview,
        classifications: tuple[MigratedActivityClassification, ...],
        *,
        classified_at: datetime,
        idempotency_key: str,
    ) -> MigratedBaselineAdoptionReview:
        if type(review) is not MigratedBaselineClassificationReview:
            raise MigratedBaselineAdoptionError(
                "INVALID_CLASSIFICATION_REVIEW"
            )
        review.verify()
        classified = _aware_utc(classified_at, "classified_at")
        values = tuple(classifications)
        if any(type(item) is not MigratedActivityClassification for item in values):
            raise MigratedBaselineAdoptionError("INVALID_CLASSIFICATIONS")
        if tuple(item.activity_id for item in values) != (
            review.protected_activity_ids
        ):
            raise MigratedBaselineAdoptionError(
                "CLASSIFICATION_CONTEXT_MISMATCH"
            )
        if not review.created_at <= classified < review.expires_at:
            raise MigratedBaselineAdoptionError("CLASSIFICATION_REVIEW_EXPIRED")
        _verify_store_context(self._repository, review)

        operation = AdoptMigratedBaseline(
            op_id="adopt-migrated-baseline",
            source_revision=review.source_revision,
            classifications=values,
        )
        patch = PlanPatch(
            trip_id=review.trip_id,
            base_revision=review.base_revision,
            idempotency_key=idempotency_key,
            operations=(operation,),
            intent="adopt fully classified migrated baseline",
        )
        unapproved = self._repository.preview_patch(
            patch,
            evaluation_at=classified,
        )
        problem_codes = {item.code for item in unapproved.problems}
        scope = unapproved.required_approval_scope
        if (
            scope is None
            or problem_codes != {"APPROVAL_REQUIRED"}
            or unapproved.draft is None
        ):
            raise MigratedBaselineAdoptionError(
                next(iter(sorted(problem_codes)), "ADOPTION_PREVIEW_REJECTED")
            )
        probe = ApprovalGrant(
            approval_id="baseline-adoption-preview-only",
            scope_digest=scope,
            approved_by="baseline-adoption-preview-gate",
            approved_at=classified.isoformat(),
        )
        approved_preview = self._repository.preview_patch(
            patch,
            approvals=(probe,),
            evaluation_at=classified,
        )
        if not approved_preview.success or approved_preview.draft is None:
            raise MigratedBaselineAdoptionError(
                next(
                    (
                        item.code
                        for item in approved_preview.problems
                    ),
                    "ADOPTION_PREVIEW_REJECTED",
                )
            )
        counts: dict[str, int] = {}
        for item in values:
            counts[item.kind.value] = counts.get(item.kind.value, 0) + 1
        adoption_review = MigratedBaselineAdoptionReview(
            classification_review=review,
            patch=patch,
            run_id=self.run_id,
            required_approval_scope=scope,
            check_status=approved_preview.check_status or "unknown",
            change_count=len(approved_preview.draft.changes),
            classification_counts=tuple(sorted(counts.items())),
            created_at=classified,
            expires_at=min(
                classified + _REVIEW_LIFETIME,
                review.expires_at,
            ),
            store_target_digest=self._repository.target_binding_digest,
            _token=_ADOPTION_REVIEW_TOKEN,
        )
        with self._lock:
            if self._pending is not None:
                raise MigratedBaselineAdoptionError(
                    "ADOPTION_REVIEW_ALREADY_PENDING"
                )
            self._pending = adoption_review
        return adoption_review

    def commit(
        self,
        review_id: str,
        *,
        human_grant: HumanCheckpointGrant | None,
        approvals: Sequence[ApprovalGrant] = (),
    ) -> MigratedBaselineAdoptionResult:
        with self._lock:
            review = self._pending
            if review is None or review.review_id != review_id:
                raise MigratedBaselineAdoptionError(
                    "STALE_BASELINE_ADOPTION_REVIEW"
                )
            review.verify()
            if human_grant is None:
                return _result(
                    review,
                    MigratedBaselineAdoptionState.WAITING_CHECKPOINT,
                    pending=True,
                )
            if (
                type(human_grant) is not HumanCheckpointGrant
                or human_grant.review_id != review.review_id
                or not review.created_at
                <= human_grant.approved_at
                <= review.expires_at
            ):
                raise MigratedBaselineAdoptionError(
                    "INVALID_BASELINE_ADOPTION_CHECKPOINT"
                )
            approval_values = tuple(approvals)
            if any(type(item) is not ApprovalGrant for item in approval_values):
                raise MigratedBaselineAdoptionError(
                    "INVALID_BASELINE_ADOPTION_APPROVAL"
                )
            store_result = self._repository.apply_patch(
                review.patch,
                approvals=approval_values,
                evaluation_at=human_grant.approved_at,
            )
            codes = tuple(item.code for item in store_result.problems)
            if store_result.status == "replayed_rolled_back":
                self._pending = None
                return _result(
                    review,
                    MigratedBaselineAdoptionState.ROLLED_BACK,
                    store_result=store_result,
                    applied=False,
                    pending=False,
                    problem_codes=("BASELINE_ADOPTION_ROLLED_BACK",),
                )
            if store_result.success:
                state = (
                    MigratedBaselineAdoptionState.REPLAY_CONFIRMED
                    if store_result.replayed
                    else MigratedBaselineAdoptionState.APPLIED
                )
                self._pending = None
                return _result(
                    review,
                    state,
                    store_result=store_result,
                    applied=True,
                    pending=False,
                )
            if store_result.status == "commit_outcome_unknown":
                return _result(
                    review,
                    MigratedBaselineAdoptionState.OUTCOME_UNKNOWN,
                    store_result=store_result,
                    applied=False,
                    pending=True,
                    problem_codes=codes,
                )
            if set(codes).intersection(
                {"APPROVAL_REQUIRED", "APPROVAL_SCOPE_MISMATCH"}
            ):
                return _result(
                    review,
                    MigratedBaselineAdoptionState.WAITING_APPROVAL,
                    store_result=store_result,
                    applied=False,
                    pending=True,
                    problem_codes=codes,
                )
            self._pending = None
            return _result(
                review,
                MigratedBaselineAdoptionState.REJECTED,
                store_result=store_result,
                applied=False,
                pending=False,
                problem_codes=codes,
            )

    def cancel_pending(self) -> None:
        with self._lock:
            self._pending = None


def prepare_migrated_baseline_classification_review(
    store: TripStore,
    *,
    reviewed_at: datetime,
) -> MigratedBaselineClassificationReview:
    """Read one exact canonical inventory without writing or classifying it."""

    if type(store) is not TripStore:
        raise MigratedBaselineAdoptionError("INVALID_BASELINE_STORE")
    created = _aware_utc(reviewed_at, "reviewed_at")
    plan = store.load_plan()
    try:
        migration = plan["state"]["trip"]["_trip_planner"]["migration"]
        protected = tuple(migration["protected_activity_ids"])
        source_schema = migration["source_schema"]
        source_revision = migration["source_revision"]
    except (KeyError, TypeError) as exc:
        raise MigratedBaselineAdoptionError(
            "MIGRATED_BASELINE_UNAVAILABLE"
        ) from exc
    if not protected:
        raise MigratedBaselineAdoptionError(
            "MIGRATED_BASELINE_ALREADY_ADOPTED"
        )
    if source_schema != _SUPPORTED_SOURCE_SCHEMA:
        raise MigratedBaselineAdoptionError(
            "MIGRATED_BASELINE_SOURCE_SCHEMA_UNSUPPORTED"
        )
    if not isinstance(source_revision, str) or not source_revision:
        raise MigratedBaselineAdoptionError(
            "MIGRATED_BASELINE_UNAVAILABLE"
        )
    by_id: dict[str, _PrivateActivityReviewItem] = {}
    for day in plan["state"]["itinerary"]["days"]:
        for activity in day["places"]:
            activity_id = activity["activity_id"]
            by_id[activity_id] = _PrivateActivityReviewItem(
                activity_id=activity_id,
                day_id=day["day_id"],
                day_number=day["day"],
                day_date=day["date"],
                title=activity.get("title", ""),
                scheduled_time=activity.get("time"),
                current_decision_state=activity.get("decision_state"),
                current_flexibility=activity.get("flexibility"),
            )
    try:
        items = tuple(by_id[activity_id] for activity_id in protected)
    except KeyError as exc:
        raise MigratedBaselineAdoptionError(
            "MIGRATED_BASELINE_UNAVAILABLE"
        ) from exc
    return MigratedBaselineClassificationReview(
        trip_slug=store.slug,
        trip_id=plan["trip_id"],
        base_revision=plan["revision"],
        source_schema=source_schema,
        source_revision=source_revision,
        protected_activity_ids=protected,
        created_at=created,
        expires_at=created + _REVIEW_LIFETIME,
        store_target_digest=store.target_binding_digest,
        _items=items,
        _token=_CLASSIFICATION_REVIEW_TOKEN,
    )


def _verify_store_context(
    store: TripStore,
    review: MigratedBaselineClassificationReview,
) -> None:
    if (
        store.slug != review.trip_slug
        or store.target_binding_digest != review.store_target_digest
    ):
        raise MigratedBaselineAdoptionError("BASELINE_STORE_MISMATCH")
    current = store.load_plan()
    try:
        migration = current["state"]["trip"]["_trip_planner"]["migration"]
        protected = tuple(migration["protected_activity_ids"])
        source_schema = migration["source_schema"]
        source_revision = migration["source_revision"]
    except (KeyError, TypeError) as exc:
        raise MigratedBaselineAdoptionError(
            "MIGRATED_BASELINE_UNAVAILABLE"
        ) from exc
    if (
        current["trip_id"] != review.trip_id
        or current["revision"] != review.base_revision
        or source_schema != review.source_schema
        or source_schema != _SUPPORTED_SOURCE_SCHEMA
        or source_revision != review.source_revision
        or protected != review.protected_activity_ids
    ):
        raise MigratedBaselineAdoptionError("BASELINE_CONTEXT_CHANGED")


def _result(
    review: MigratedBaselineAdoptionReview,
    state: MigratedBaselineAdoptionState,
    *,
    store_result: StoreResult | None = None,
    applied: bool = False,
    pending: bool,
    problem_codes: tuple[str, ...] = (),
) -> MigratedBaselineAdoptionResult:
    write_performed = (
        None
        if state is MigratedBaselineAdoptionState.OUTCOME_UNKNOWN
        else bool(
            store_result is not None
            and store_result.changed
            and not store_result.replayed
        )
    )
    return MigratedBaselineAdoptionResult(
        state=state,
        applied=applied,
        store_status=(store_result.status if store_result is not None else None),
        replayed=(store_result.replayed if store_result is not None else False),
        canonical_write_performed=write_performed,
        pending_review_retained=pending,
        required_approval_scope=review.required_approval_scope,
        problem_codes=problem_codes,
    )


def _aware_utc(value: object, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise MigratedBaselineAdoptionError(f"INVALID_{name.upper()}")
    return value.astimezone(timezone.utc)


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "MIGRATED_BASELINE_ADOPTION_VERSION",
    "MigratedBaselineAdoptionError",
    "MigratedBaselineAdoptionResult",
    "MigratedBaselineAdoptionReview",
    "MigratedBaselineAdoptionStager",
    "MigratedBaselineAdoptionState",
    "MigratedBaselineClassificationReview",
    "prepare_migrated_baseline_classification_review",
]
