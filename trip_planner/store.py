"""Crash-aware single-document persistence for canonical trip plans.

``TripStore`` is the only Phase 1 component allowed to mutate ``plan.json``.
The mutation engine remains pure, the codec remains strict, and migration
preview remains read-only.  Commits are serialized with a per-trip ``flock``,
recheck compare-and-swap state inside that lock, preserve an exact before
snapshot, and replace one same-directory temporary file atomically.

This is intentionally not a database, journal, or event store.  The hidden
history directory contains only exact before snapshots needed for guarded
rollback.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import tempfile
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator

from .codec import (
    FrozenJsonValue,
    PlanCodecError,
    canonical_json_bytes,
    compute_revision,
    decode_plan,
    deep_copy_json,
    encode_plan,
    freeze_json,
    plan_to_trip_state,
)
from .migrations import (
    MigrationPreview,
    legacy_source_revision,
    preview_legacy_migration,
)
from .models import CheckReport, CheckStatus
from .mutations import (
    ApprovalGrant,
    ChangeRecord,
    LodgingConfirmationGrant,
    MutationProblem,
    PatchDraft,
    PlanPatch,
    apply_patch_to_plan,
    approval_scope_digest,
    hard_constraint_protected_changes,
    lodging_confirmation_scope_digest,
    patch_digest,
)
from .timeline import evaluate_timeline


_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TRANSACTION_RE = re.compile(r"^tx-[0-9a-f]{32}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_SLUG_LENGTH = 128
_MAX_IDEMPOTENCY_KEY_LENGTH = 256
_LOCK_FILENAME = ".trip-planner.lock"
_HISTORY_DIRNAME = ".trip-planner-history"
_PLAN_FILENAME = "plan.json"
_HISTORY_SUFFIX = ".plan.json"
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

FaultHook = Callable[[str], None]
LodgingConfirmationVerifier = Callable[[LodgingConfirmationGrant], bool]


class StoreError(PlanCodecError):
    """A hard store boundary or filesystem safety error."""


@dataclass(frozen=True, slots=True)
class StoreProblem:
    """Machine-readable reason an operation was not safely completed."""

    code: str
    message: str
    details: Mapping[str, Any] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("StoreProblem.code must be non-empty text")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("StoreProblem.message must be non-empty text")
        frozen = freeze_json(self.details)
        if not isinstance(frozen, Mapping):
            raise TypeError("StoreProblem.details must be a mapping")
        object.__setattr__(self, "details", frozen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": deep_copy_json(self.details),
        }


@dataclass(frozen=True, slots=True)
class StoreResult:
    """Frozen result shared by migration, patch, replay, and rollback."""

    success: bool
    status: str
    action: str
    transaction_id: str | None = None
    trip_id: str | None = None
    base_revision: str | None = None
    applied_revision: str | None = None
    current_revision: str | None = None
    generation: int | None = None
    replayed: bool = False
    dry_run: bool = False
    changed: bool = False
    check_status: str | None = None
    required_approval_scope: str | None = None
    required_lodging_confirmation_scope: str | None = None
    lodging_confirmation_granted: bool = False
    problems: tuple[StoreProblem, ...] = ()
    draft: PatchDraft | None = None
    candidate_plan: FrozenJsonValue | None = None
    receipt: FrozenJsonValue | None = None
    check_report: CheckReport | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.success, bool):
            raise TypeError("StoreResult.success must be bool")
        if not isinstance(self.status, str) or not self.status.strip():
            raise ValueError("StoreResult.status must be non-empty text")
        if not isinstance(self.action, str) or not self.action.strip():
            raise ValueError("StoreResult.action must be non-empty text")
        if not isinstance(self.problems, tuple):
            object.__setattr__(self, "problems", tuple(self.problems))
        if any(not isinstance(item, StoreProblem) for item in self.problems):
            raise TypeError("StoreResult.problems must contain StoreProblem values")
        if self.candidate_plan is not None:
            object.__setattr__(
                self, "candidate_plan", freeze_json(self.candidate_plan)
            )
        if self.receipt is not None:
            object.__setattr__(self, "receipt", freeze_json(self.receipt))

    @property
    def ok(self) -> bool:
        return self.success

    @property
    def revision(self) -> str | None:
        """Compatibility alias for the currently stored/observed revision."""

        return self.current_revision

    @property
    def plan(self) -> FrozenJsonValue | None:
        return self.candidate_plan

    def mutable_candidate_plan(self) -> dict[str, Any] | None:
        if self.candidate_plan is None:
            return None
        value = deep_copy_json(self.candidate_plan)
        assert isinstance(value, dict)
        return value

    def to_dict(self, *, include_candidate: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "success": self.success,
            "status": self.status,
            "action": self.action,
            "transaction_id": self.transaction_id,
            "trip_id": self.trip_id,
            "base_revision": self.base_revision,
            "applied_revision": self.applied_revision,
            "current_revision": self.current_revision,
            "generation": self.generation,
            "replayed": self.replayed,
            "dry_run": self.dry_run,
            "changed": self.changed,
            "check_status": self.check_status,
            "required_approval_scope": self.required_approval_scope,
            "required_lodging_confirmation_scope": self.required_lodging_confirmation_scope,
            "lodging_confirmation_granted": self.lodging_confirmation_granted,
            "problems": [problem.to_dict() for problem in self.problems],
        }
        if self.receipt is not None:
            result["receipt"] = deep_copy_json(self.receipt)
        if include_candidate and self.candidate_plan is not None:
            result["candidate_plan"] = deep_copy_json(self.candidate_plan)
        return result


@dataclass(frozen=True, slots=True)
class _WriteOutcome:
    replaced: bool
    problem: StoreProblem | None = None


class TripStore:
    """Persistence boundary for one trusted-root trip slug."""

    def __init__(
        self,
        trips_root: str | Path,
        slug: str,
        fault_hook: FaultHook | None = None,
        *,
        lodging_confirmation_verifier: (
            LodgingConfirmationVerifier | None
        ) = None,
    ) -> None:
        self.slug = _validate_slug(slug)
        if fault_hook is not None and not callable(fault_hook):
            raise TypeError("fault_hook must be callable or None")
        if (
            lodging_confirmation_verifier is not None
            and not callable(lodging_confirmation_verifier)
        ):
            raise TypeError(
                "lodging_confirmation_verifier must be callable or None"
            )
        self._fault_hook = fault_hook
        self._lodging_confirmation_verifier = (
            lodging_confirmation_verifier
        )

        try:
            root = Path(trips_root).resolve(strict=True)
        except OSError as exc:
            raise StoreError(
                "INVALID_TRIPS_ROOT",
                str(exc),
                path=str(trips_root),
            ) from exc
        _require_directory(root, "trips root", allow_symlink_target=True)
        self.trips_root = root
        self.trip_dir = root / self.slug
        self.data_dir = self.trip_dir / "data"
        self.plan_path = self.data_dir / _PLAN_FILENAME
        self.lock_path = self.data_dir / _LOCK_FILENAME
        self.history_dir = self.data_dir / _HISTORY_DIRNAME
        self.trip_json_path = self.data_dir / "trip.json"
        self.itinerary_json_path = self.data_dir / "itinerary.json"
        # Canonical reads and mutations must remain available after migration
        # even when the compatibility source files are later archived.
        self._validate_layout(require_legacy=False)

    def load_plan(self) -> dict[str, Any]:
        """Load a detached canonical plan through a no-follow regular file."""

        self._validate_layout(require_legacy=False)
        data = self._read_regular_bytes(self.plan_path, required=True)
        assert data is not None
        return decode_plan(data)

    def preview_migration(self) -> MigrationPreview:
        """Return a deterministic migration preview without store writes."""

        self._validate_layout(require_legacy=True)
        preview = preview_legacy_migration(self.data_dir)
        self._validate_layout(require_legacy=True)
        return preview

    def commit_migration(self, preview: MigrationPreview) -> StoreResult:
        """CAS-check and atomically persist the exact preview candidate."""

        if not isinstance(preview, MigrationPreview):
            raise TypeError("preview must be MigrationPreview")
        if (
            preview.data_dir.resolve(strict=False) != self.data_dir
            or preview.target_path.resolve(strict=False) != self.plan_path
        ):
            return self._rejected(
                "migration",
                "PREVIEW_TARGET_MISMATCH",
                "Migration preview belongs to a different canonical target.",
                details={
                    "preview_data_dir": str(preview.data_dir),
                    "preview_target_path": str(preview.target_path),
                    "store_data_dir": str(self.data_dir),
                    "store_target_path": str(self.plan_path),
                },
            )
        try:
            preview.verify_candidate_bytes()
            candidate = decode_plan(preview.candidate_bytes)
        except PlanCodecError as exc:
            return self._exception_result("migration", exc)

        transaction_id = f"migration-{preview.preview_digest[:32]}"
        try:
            with self._exclusive_lock():
                self._validate_layout(require_legacy=False)
                existing_bytes = self._read_regular_bytes(
                    self.plan_path, required=False
                )
                if existing_bytes is not None:
                    if existing_bytes == preview.candidate_bytes:
                        existing = decode_plan(existing_bytes)
                        return StoreResult(
                            success=True,
                            status="replayed",
                            action="migration",
                            transaction_id=transaction_id,
                            trip_id=str(existing["trip_id"]),
                            applied_revision=str(existing["revision"]),
                            current_revision=str(existing["revision"]),
                            generation=int(existing["generation"]),
                            replayed=True,
                            changed=False,
                            candidate_plan=existing,
                        )
                    return self._rejected(
                        "migration",
                        "PLAN_ALREADY_EXISTS",
                        "A different canonical plan already exists.",
                    )

                # Legacy sources are required only while producing the first
                # canonical snapshot. Exact replay must remain recoverable
                # after those compatibility files are archived.
                self._validate_layout(require_legacy=True)
                current_source_revision = legacy_source_revision(self.data_dir)
                if current_source_revision != preview.source_revision:
                    return self._rejected(
                        "migration",
                        "STALE_MIGRATION_PREVIEW",
                        "Legacy source changed after the migration preview.",
                        details={
                            "preview_source_revision": preview.source_revision,
                            "current_source_revision": current_source_revision,
                        },
                    )

                # Recreate the preview under the lock to prove its digest and
                # exact candidate bytes still follow current migration logic.
                current_preview = preview_legacy_migration(self.data_dir)
                if (
                    current_preview.preview_digest != preview.preview_digest
                    or current_preview.candidate_bytes != preview.candidate_bytes
                ):
                    return self._rejected(
                        "migration",
                        "MIGRATION_PREVIEW_MISMATCH",
                        "Migration preview no longer matches deterministic output.",
                        details={
                            "preview_digest": preview.preview_digest,
                            "current_digest": current_preview.preview_digest,
                        },
                    )

                report_or_problem = self._kernel_check(candidate)
                if isinstance(report_or_problem, StoreProblem):
                    return self._rejected_problem("migration", report_or_problem)
                report = report_or_problem
                if report.status is CheckStatus.INFEASIBLE:
                    return self._infeasible_result(
                        action="migration",
                        plan=candidate,
                        report=report,
                    )

                outcome = self._atomic_replace_plan(
                    preview.candidate_bytes,
                    transaction_id=None,
                    before_bytes=None,
                )
                if outcome.problem is not None:
                    return self._write_failure_result(
                        action="migration",
                        transaction_id=transaction_id,
                        plan=candidate,
                        outcome=outcome,
                        report=report,
                    )
                return StoreResult(
                    success=True,
                    status="migrated",
                    action="migration",
                    transaction_id=transaction_id,
                    trip_id=str(candidate["trip_id"]),
                    applied_revision=str(candidate["revision"]),
                    current_revision=str(candidate["revision"]),
                    generation=int(candidate["generation"]),
                    changed=True,
                    check_status=report.status.value,
                    candidate_plan=candidate,
                    check_report=report,
                )
        except (OSError, PlanCodecError, StoreError) as exc:
            return self._exception_result("migration", exc)

    def preview_patch(
        self,
        patch: PlanPatch,
        approvals: Sequence[ApprovalGrant] = (),
        lodging_confirmations: Sequence[LodgingConfirmationGrant] = (),
        *,
        evaluation_at: datetime | None = None,
    ) -> StoreResult:
        """Evaluate a patch without locks, receipts, history, or writes."""

        if not isinstance(patch, PlanPatch):
            raise TypeError("patch must be PlanPatch")
        try:
            normalized_evaluation_at = _normalize_evaluation_at(evaluation_at)
            _validate_idempotency_key(patch.idempotency_key)
            request_digest = patch_digest(patch)
            plan = self.load_plan()
        except (OSError, PlanCodecError, StoreError, TypeError, ValueError) as exc:
            return self._exception_result("patch", exc, dry_run=True)
        return self._evaluate_patch(
            plan,
            patch,
            approvals=approvals,
            lodging_confirmations=lodging_confirmations,
            request_digest=request_digest,
            dry_run=True,
            evaluation_at=normalized_evaluation_at,
        )

    def apply_patch(
        self,
        patch: PlanPatch,
        approvals: Sequence[ApprovalGrant] = (),
        lodging_confirmations: Sequence[LodgingConfirmationGrant] = (),
        *,
        dry_run: bool = False,
        evaluation_at: datetime | None = None,
    ) -> StoreResult:
        """Dry-run or atomically apply one semantic patch."""

        if not isinstance(patch, PlanPatch):
            raise TypeError("patch must be PlanPatch")
        if dry_run:
            return self.preview_patch(
                patch,
                approvals,
                lodging_confirmations,
                evaluation_at=evaluation_at,
            )

        try:
            normalized_evaluation_at = _normalize_evaluation_at(evaluation_at)
            _validate_idempotency_key(patch.idempotency_key)
            request_digest = patch_digest(patch)
        except (TypeError, ValueError, StoreError) as exc:
            return self._exception_result("patch", exc)

        try:
            with self._exclusive_lock():
                self._validate_layout(require_legacy=False)
                current_bytes = self._read_regular_bytes(
                    self.plan_path, required=True
                )
                assert current_bytes is not None
                current = decode_plan(current_bytes)

                replay = self._receipt_result(
                    current,
                    idempotency_key=patch.idempotency_key,
                    request_digest=request_digest,
                    action="patch",
                )
                if replay is not None:
                    return replay
                verified_lodging = self._verify_lodging_confirmations(
                    lodging_confirmations
                )
                if verified_lodging is None:
                    return self._rejected(
                        "patch",
                        "UNTRUSTED_LODGING_CONFIRMATION",
                        (
                            "The lodging confirmation was not verified by "
                            "the trusted persistence host."
                        ),
                    )

                evaluated = self._evaluate_patch(
                    current,
                    patch,
                    approvals=approvals,
                    lodging_confirmations=verified_lodging,
                    request_digest=request_digest,
                    dry_run=False,
                    skip_receipt_lookup=True,
                    evaluation_at=normalized_evaluation_at,
                )
                if not evaluated.success or not evaluated.changed:
                    return evaluated
                candidate = evaluated.mutable_candidate_plan()
                assert candidate is not None

                transaction_id = _new_transaction_id()
                receipt = {
                    "kind": "patch",
                    "status": "applied",
                    "request_digest": request_digest,
                    "transaction_id": transaction_id,
                    "base_revision": patch.base_revision,
                    "applied_revision": candidate["revision"],
                    "applied_generation": candidate["generation"],
                    "check_status": evaluated.check_status,
                    "required_approval_scope": evaluated.required_approval_scope,
                    "required_lodging_confirmation_scope": (
                        evaluated.required_lodging_confirmation_scope
                    ),
                    "lodging_confirmation_grant_ids": [
                        grant.grant_id
                        for grant in verified_lodging
                        if (
                            grant.scope_digest
                            == evaluated.required_lodging_confirmation_scope
                        )
                    ],
                    "approval_ids": [
                        grant.approval_id
                        for grant in approvals
                        if isinstance(grant, ApprovalGrant)
                        and evaluated.required_approval_scope is not None
                        and grant.scope_digest
                        == evaluated.required_approval_scope
                    ],
                    "change_count": len(
                        evaluated.draft.changes
                        if evaluated.draft is not None
                        else ()
                    ),
                    "before_snapshot_sha256": hashlib.sha256(
                        current_bytes
                    ).hexdigest(),
                }
                if normalized_evaluation_at is not None:
                    receipt["evaluation_at"] = (
                        normalized_evaluation_at.isoformat()
                    )
                receipts = candidate["receipts"]
                assert isinstance(receipts, dict)
                receipts[patch.idempotency_key] = receipt
                candidate_bytes = encode_plan(candidate)

                outcome = self._atomic_replace_plan(
                    candidate_bytes,
                    transaction_id=transaction_id,
                    before_bytes=current_bytes,
                )
                if outcome.problem is not None:
                    return self._write_failure_result(
                        action="patch",
                        transaction_id=transaction_id,
                        plan=candidate,
                        outcome=outcome,
                        report=evaluated.check_report,
                        base_revision=patch.base_revision,
                        applied_revision=str(candidate["revision"]),
                        receipt=receipt,
                        required_approval_scope=(
                            evaluated.required_approval_scope
                        ),
                        required_lodging_confirmation_scope=(
                            evaluated.required_lodging_confirmation_scope
                        ),
                        lodging_confirmation_granted=(
                            evaluated.lodging_confirmation_granted
                        ),
                    )

                return StoreResult(
                    success=True,
                    status="applied",
                    action="patch",
                    transaction_id=transaction_id,
                    trip_id=str(candidate["trip_id"]),
                    base_revision=patch.base_revision,
                    applied_revision=str(candidate["revision"]),
                    current_revision=str(candidate["revision"]),
                    generation=int(candidate["generation"]),
                    changed=True,
                    check_status=evaluated.check_status,
                    required_approval_scope=evaluated.required_approval_scope,
                    required_lodging_confirmation_scope=(
                        evaluated.required_lodging_confirmation_scope
                    ),
                    lodging_confirmation_granted=evaluated.lodging_confirmation_granted,
                    draft=evaluated.draft,
                    candidate_plan=candidate,
                    receipt=receipt,
                    check_report=evaluated.check_report,
                )
        except (OSError, PlanCodecError, StoreError) as exc:
            return self._exception_result("patch", exc)

    def rollback(
        self,
        transaction_id: str,
        base_revision: str,
        idempotency_key: str,
        approvals: Sequence[ApprovalGrant] = (),
        lodging_confirmations: Sequence[LodgingConfirmationGrant] = (),
        *,
        evaluation_at: datetime | None = None,
    ) -> StoreResult:
        """Rollback the latest successful semantic transaction.

        Implemented below the initial migration/apply slice so all rollback
        commits reuse the same lock, receipt, history, validation, and atomic
        replace primitives.
        """

        return self._rollback_latest(
            transaction_id,
            base_revision,
            idempotency_key,
            approvals,
            lodging_confirmations,
            evaluation_at=evaluation_at,
        )

    def _evaluate_patch(
        self,
        plan: dict[str, Any],
        patch: PlanPatch,
        *,
        approvals: Sequence[ApprovalGrant],
        lodging_confirmations: Sequence[LodgingConfirmationGrant] = (),
        request_digest: str,
        dry_run: bool,
        skip_receipt_lookup: bool = False,
        evaluation_at: datetime | None = None,
    ) -> StoreResult:
        if not skip_receipt_lookup:
            replay = self._receipt_result(
                plan,
                idempotency_key=patch.idempotency_key,
                request_digest=request_digest,
                action="patch",
                dry_run=dry_run,
            )
            if replay is not None:
                return replay

        draft = apply_patch_to_plan(
            plan,
            patch,
            approvals=approvals,
            lodging_confirmations=lodging_confirmations,
        )
        if draft.problems:
            return StoreResult(
                success=False,
                status="rejected",
                action="patch",
                trip_id=str(plan.get("trip_id", "")) or None,
                base_revision=patch.base_revision,
                current_revision=_optional_string(plan.get("revision")),
                generation=_optional_int(plan.get("generation")),
                dry_run=dry_run,
                changed=False,
                required_approval_scope=draft.required_approval_scope,
                required_lodging_confirmation_scope=draft.required_lodging_confirmation_scope,
                lodging_confirmation_granted=draft.lodging_confirmation_granted,
                problems=tuple(
                    _mutation_problem(problem) for problem in draft.problems
                ),
                draft=draft,
                candidate_plan=plan,
            )

        candidate = draft.to_plan_dict()
        current_state = plan.get("state")
        candidate_state = candidate.get("state")
        if canonical_json_bytes(current_state) == canonical_json_bytes(
            candidate_state
        ):
            return StoreResult(
                success=True,
                status="no_op",
                action="patch",
                trip_id=str(plan["trip_id"]),
                base_revision=patch.base_revision,
                applied_revision=str(plan["revision"]),
                current_revision=str(plan["revision"]),
                generation=int(plan["generation"]),
                dry_run=dry_run,
                changed=False,
                required_approval_scope=draft.required_approval_scope,
                required_lodging_confirmation_scope=draft.required_lodging_confirmation_scope,
                lodging_confirmation_granted=draft.lodging_confirmation_granted,
                draft=draft,
                candidate_plan=plan,
            )

        candidate["generation"] = int(plan["generation"]) + 1
        candidate["revision"] = compute_revision(candidate)
        candidate["receipts"] = deep_copy_json(plan["receipts"])
        try:
            candidate_bytes = encode_plan(candidate)
            candidate = decode_plan(candidate_bytes)
        except PlanCodecError as exc:
            return self._exception_result(
                "patch",
                exc,
                dry_run=dry_run,
                draft=draft,
                base_revision=patch.base_revision,
                current_revision=str(plan["revision"]),
            )

        report_or_problem = self._kernel_check(
            candidate,
            evaluation_at=evaluation_at,
        )
        if isinstance(report_or_problem, StoreProblem):
            return StoreResult(
                success=False,
                status="rejected",
                action="patch",
                trip_id=str(plan["trip_id"]),
                base_revision=patch.base_revision,
                current_revision=str(plan["revision"]),
                generation=int(plan["generation"]),
                dry_run=dry_run,
                changed=False,
                required_approval_scope=draft.required_approval_scope,
                required_lodging_confirmation_scope=draft.required_lodging_confirmation_scope,
                lodging_confirmation_granted=draft.lodging_confirmation_granted,
                problems=(report_or_problem,),
                draft=draft,
                candidate_plan=candidate,
            )
        report = report_or_problem
        if report.status is CheckStatus.INFEASIBLE:
            return self._infeasible_result(
                action="patch",
                plan=candidate,
                report=report,
                base_revision=patch.base_revision,
                current_revision=str(plan["revision"]),
                dry_run=dry_run,
                draft=draft,
                required_approval_scope=draft.required_approval_scope,
                required_lodging_confirmation_scope=draft.required_lodging_confirmation_scope,
                lodging_confirmation_granted=draft.lodging_confirmation_granted,
            )

        return StoreResult(
            success=True,
            status="preview_ready" if dry_run else "ready",
            action="patch",
            trip_id=str(candidate["trip_id"]),
            base_revision=patch.base_revision,
            applied_revision=str(candidate["revision"]),
            current_revision=str(plan["revision"]),
            generation=int(candidate["generation"]),
            dry_run=dry_run,
            changed=True,
            check_status=report.status.value,
            required_approval_scope=draft.required_approval_scope,
            required_lodging_confirmation_scope=draft.required_lodging_confirmation_scope,
            lodging_confirmation_granted=draft.lodging_confirmation_granted,
            draft=draft,
            candidate_plan=candidate,
            check_report=report,
        )

    def _kernel_check(
        self,
        plan: Mapping[str, Any],
        *,
        evaluation_at: datetime | None = None,
    ) -> CheckReport | StoreProblem:
        try:
            state = plan_to_trip_state(plan)
            return evaluate_timeline(state, now=evaluation_at)
        except (PlanCodecError, ValueError, TypeError) as exc:
            return StoreProblem(
                code="KERNEL_VALIDATION_FAILED",
                message=str(exc),
            )

    def _receipt_result(
        self,
        plan: Mapping[str, Any],
        *,
        idempotency_key: str,
        request_digest: str,
        action: str,
        dry_run: bool = False,
    ) -> StoreResult | None:
        receipts = plan.get("receipts")
        if not isinstance(receipts, Mapping):
            return self._rejected(
                action,
                "MALFORMED_RECEIPTS",
                "Canonical plan receipts must be an object.",
            )
        receipt_value = receipts.get(idempotency_key)
        if receipt_value is None:
            return None
        if not isinstance(receipt_value, Mapping):
            return self._rejected(
                action,
                "MALFORMED_RECEIPT",
                "Stored idempotency receipt is not an object.",
            )
        stored_digest = receipt_value.get("request_digest")
        if stored_digest != request_digest:
            return self._rejected(
                action,
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency key was already used for a different request.",
                details={
                    "idempotency_key": idempotency_key,
                    "stored_digest": stored_digest,
                    "request_digest": request_digest,
                },
            )
        stored_status = receipt_value.get("status")
        status = (
            "replayed_rolled_back"
            if stored_status == "rolled_back"
            else "replayed"
        )
        return StoreResult(
            success=True,
            status=status,
            action=action,
            transaction_id=_optional_string(
                receipt_value.get("transaction_id")
            ),
            trip_id=_optional_string(plan.get("trip_id")),
            base_revision=_optional_string(
                receipt_value.get("base_revision")
            ),
            applied_revision=_optional_string(
                receipt_value.get("applied_revision")
            ),
            current_revision=_optional_string(plan.get("revision")),
            generation=_optional_int(plan.get("generation")),
            replayed=True,
            dry_run=dry_run,
            changed=False,
            check_status=_optional_string(
                receipt_value.get("check_status")
            ),
            required_approval_scope=_optional_string(
                receipt_value.get("required_approval_scope")
            ),
            required_lodging_confirmation_scope=_optional_string(
                receipt_value.get(
                    "required_lodging_confirmation_scope"
                )
            ),
            lodging_confirmation_granted=bool(
                receipt_value.get(
                    "required_lodging_confirmation_scope"
                )
            ),
            candidate_plan=plan,
            receipt=receipt_value,
        )

    @contextmanager
    def _exclusive_lock(self, *, require_legacy: bool = False) -> Iterator[None]:
        self._validate_layout(require_legacy=require_legacy)
        flags = os.O_RDWR | os.O_CREAT | _O_CLOEXEC | _O_NOFOLLOW
        try:
            fd = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise StoreError(
                "UNSAFE_LOCK_PATH",
                str(exc),
                path=str(self.lock_path),
            ) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise StoreError(
                    "UNSAFE_LOCK_PATH",
                    "lock path is not a regular file",
                    path=str(self.lock_path),
                )
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._validate_layout(require_legacy=require_legacy)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _atomic_replace_plan(
        self,
        data: bytes,
        *,
        transaction_id: str | None,
        before_bytes: bytes | None,
    ) -> _WriteOutcome:
        temp_path: Path | None = None
        replaced = False
        try:
            self._fault("before_temp_write")
            fd, temp_name = tempfile.mkstemp(
                prefix=".plan.",
                suffix=".tmp",
                dir=self.data_dir,
            )
            temp_path = Path(temp_name)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb", closefd=True) as output:
                    output.write(data)
                    output.flush()
                    self._fault("after_temp_write")
                    os.fsync(output.fileno())
                    self._fault("after_temp_fsync")
            except BaseException:
                # fdopen owns and closes fd after construction.
                raise

            if transaction_id is not None and before_bytes is not None:
                self._fault("before_history")
                self._write_history_snapshot(transaction_id, before_bytes)
                self._fault("after_history")

            self._validate_plan_destination()
            self._fault("before_replace")
            os.replace(temp_path, self.plan_path)
            replaced = True
            temp_path = None
            self._fault("after_replace")
            self._fsync_directory(self.data_dir)
            self._fault("after_directory_fsync")
            return _WriteOutcome(replaced=True)
        except Exception as exc:
            code = "COMMIT_OUTCOME_UNKNOWN" if replaced else "STORE_WRITE_FAILED"
            return _WriteOutcome(
                replaced=replaced,
                problem=StoreProblem(
                    code=code,
                    message=str(exc) or type(exc).__name__,
                ),
            )
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass

    def _write_history_snapshot(
        self, transaction_id: str, before_bytes: bytes
    ) -> None:
        if not _TRANSACTION_RE.fullmatch(transaction_id):
            raise StoreError(
                "INVALID_TRANSACTION_ID",
                "store-generated transaction ID has an invalid form",
            )
        self._ensure_history_dir()
        snapshot_path = self._history_path(transaction_id)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | _O_CLOEXEC
            | _O_NOFOLLOW
        )
        fd = os.open(snapshot_path, flags, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=True) as output:
                output.write(before_bytes)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            try:
                snapshot_path.unlink()
            except FileNotFoundError:
                pass
            raise
        self._fsync_directory(self.history_dir)

    def _ensure_history_dir(self) -> None:
        try:
            os.mkdir(self.history_dir, 0o700)
        except FileExistsError:
            pass
        _require_directory(self.history_dir, "history directory")

    def _history_path(self, transaction_id: str) -> Path:
        if not _TRANSACTION_RE.fullmatch(transaction_id):
            raise StoreError(
                "INVALID_TRANSACTION_ID",
                "transaction ID is not a store-generated identifier",
            )
        return self.history_dir / f"{transaction_id}{_HISTORY_SUFFIX}"

    def _read_regular_bytes(
        self, path: Path, *, required: bool
    ) -> bytes | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            if required:
                raise StoreError(
                    "MISSING_PLAN",
                    f"{path.name} does not exist",
                    path=str(path),
                )
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise StoreError(
                "UNSAFE_PATH",
                "path must be a non-symlink regular file",
                path=str(path),
            )
        flags = os.O_RDONLY | _O_CLOEXEC | _O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise StoreError(
                    "UNSAFE_PATH",
                    "opened path is not a regular file",
                    path=str(path),
                )
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def _validate_layout(self, *, require_legacy: bool) -> None:
        _require_directory(self.trip_dir, "trip directory")
        _require_directory(self.data_dir, "trip data directory")
        if require_legacy:
            _require_regular_file(self.trip_json_path, "legacy trip.json")
            _require_regular_file(
                self.itinerary_json_path, "legacy itinerary.json"
            )
        else:
            _require_optional_regular_file(
                self.trip_json_path, "legacy trip.json"
            )
            _require_optional_regular_file(
                self.itinerary_json_path, "legacy itinerary.json"
            )
        _require_optional_regular_file(self.plan_path, "canonical plan.json")
        _require_optional_regular_file(self.lock_path, "trip lock")
        if self.history_dir.exists() or self.history_dir.is_symlink():
            _require_directory(self.history_dir, "history directory")

    def _validate_plan_destination(self) -> None:
        _require_directory(self.data_dir, "trip data directory")
        _require_optional_regular_file(self.plan_path, "canonical plan.json")

    def _fsync_directory(self, path: Path) -> None:
        fd = os.open(
            path,
            os.O_RDONLY | _O_CLOEXEC | _O_DIRECTORY | _O_NOFOLLOW,
        )
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _fault(self, stage: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(stage)

    def _verify_lodging_confirmations(
        self,
        grants: Sequence[LodgingConfirmationGrant],
    ) -> tuple[LodgingConfirmationGrant, ...] | None:
        """Fail closed unless every supplied grant passes the host verifier."""

        values = tuple(grants)
        if not values:
            return ()
        verifier = self._lodging_confirmation_verifier
        if verifier is None:
            return None
        for grant in values:
            if type(grant) is not LodgingConfirmationGrant:
                return None
            try:
                verified = verifier(grant)
            except Exception:
                return None
            if verified is not True:
                return None
        return values

    def _rollback_latest(
        self,
        transaction_id: str,
        base_revision: str,
        idempotency_key: str,
        approvals: Sequence[ApprovalGrant],
        lodging_confirmations: Sequence[LodgingConfirmationGrant],
        *,
        evaluation_at: datetime | None,
    ) -> StoreResult:
        try:
            normalized_evaluation_at = _normalize_evaluation_at(evaluation_at)
            _validate_transaction_id(transaction_id)
            _validate_revision(base_revision)
            _validate_idempotency_key(idempotency_key)
            request_digest = _rollback_request_digest(
                transaction_id=transaction_id,
                base_revision=base_revision,
                idempotency_key=idempotency_key,
            )
        except (TypeError, ValueError, StoreError) as exc:
            return self._exception_result("rollback", exc)

        try:
            with self._exclusive_lock():
                current_bytes = self._read_regular_bytes(
                    self.plan_path, required=True
                )
                assert current_bytes is not None
                current = decode_plan(current_bytes)

                # Receipt lookup deliberately precedes every CAS and target
                # check. A completed request remains replayable with its
                # original outcome after the plan revision has advanced.
                replay = self._receipt_result(
                    current,
                    idempotency_key=idempotency_key,
                    request_digest=request_digest,
                    action="rollback",
                )
                if replay is not None:
                    return replay
                verified_lodging = self._verify_lodging_confirmations(
                    lodging_confirmations
                )
                if verified_lodging is None:
                    return self._rejected(
                        "rollback",
                        "UNTRUSTED_LODGING_CONFIRMATION",
                        (
                            "The lodging confirmation was not verified by "
                            "the trusted persistence host."
                        ),
                    )

                if current["revision"] != base_revision:
                    return self._rejected(
                        "rollback",
                        "STALE_BASE_REVISION",
                        "Rollback base revision is not the current revision.",
                        details={
                            "base_revision": base_revision,
                            "current_revision": current["revision"],
                        },
                    )

                target_key, target_receipt = _find_transaction_receipt(
                    current["receipts"], transaction_id
                )
                if target_receipt is None or target_key is None:
                    return self._rejected(
                        "rollback",
                        "UNKNOWN_TRANSACTION",
                        "Rollback target transaction does not exist.",
                        details={"transaction_id": transaction_id},
                    )
                if (
                    target_receipt.get("kind") != "patch"
                    or target_receipt.get("status") != "applied"
                ):
                    return self._rejected(
                        "rollback",
                        "TRANSACTION_NOT_ROLLBACKABLE",
                        "Only an applied patch transaction can be rolled back.",
                        details={
                            "transaction_id": transaction_id,
                            "kind": target_receipt.get("kind"),
                            "status": target_receipt.get("status"),
                        },
                    )
                if target_receipt.get("applied_revision") != current["revision"]:
                    return self._rejected(
                        "rollback",
                        "TRANSACTION_NOT_LATEST",
                        "Only the latest successful patch can be rolled back.",
                        details={
                            "transaction_id": transaction_id,
                            "transaction_revision": target_receipt.get(
                                "applied_revision"
                            ),
                            "current_revision": current["revision"],
                        },
                    )

                snapshot_bytes = self._read_history_snapshot(transaction_id)
                expected_hash = target_receipt.get(
                    "before_snapshot_sha256"
                )
                actual_hash = hashlib.sha256(snapshot_bytes).hexdigest()
                if not isinstance(expected_hash, str) or (
                    expected_hash != actual_hash
                ):
                    return self._rejected(
                        "rollback",
                        "HISTORY_INTEGRITY_FAILED",
                        "Rollback history snapshot hash does not match receipt.",
                        details={
                            "transaction_id": transaction_id,
                            "expected_sha256": expected_hash,
                            "actual_sha256": actual_hash,
                        },
                    )
                snapshot = decode_plan(snapshot_bytes)
                if snapshot["trip_id"] != current["trip_id"]:
                    return self._rejected(
                        "rollback",
                        "HISTORY_TRIP_MISMATCH",
                        "Rollback history belongs to a different trip.",
                    )

                candidate = deep_copy_json(current)
                assert isinstance(candidate, dict)
                candidate["state"] = deep_copy_json(snapshot["state"])
                candidate["generation"] = int(current["generation"]) + 1
                candidate["revision"] = compute_revision(candidate)

                protected_changes = _rollback_protected_changes(
                    current, candidate
                )
                required_scope: str | None = None
                lodging_changes = _rollback_lodging_changes(current, candidate)
                lodging_scope: str | None = None
                matching_lodging: list[LodgingConfirmationGrant] = []
                if lodging_changes:
                    lodging_scope = lodging_confirmation_scope_digest(
                        trip_id=str(current["trip_id"]),
                        base_revision=base_revision,
                        request_digest=request_digest,
                        lodging_changes=lodging_changes,
                    )
                    matching_lodging = [
                        grant
                        for grant in verified_lodging
                        if (
                            grant.trip_id == current["trip_id"]
                            and grant.base_revision == base_revision
                            and grant.request_digest == request_digest
                            and grant.scope_digest == lodging_scope
                        )
                    ]
                    if not matching_lodging:
                        return self._rejected(
                            "rollback",
                            (
                                "LODGING_CONFIRMATION_MISMATCH"
                                if lodging_confirmations
                                else "LODGING_CONFIRMATION_REQUIRED"
                            ),
                            (
                                "Rollback changes canonical lodging and "
                                "requires an exact lodging confirmation."
                            ),
                            details={
                                "required_scope_digest": lodging_scope
                            },
                        )
                matching_approvals: list[ApprovalGrant] = []
                if protected_changes:
                    required_scope = approval_scope_digest(
                        trip_id=str(current["trip_id"]),
                        base_revision=base_revision,
                        request_digest=request_digest,
                        protected_changes=protected_changes,
                    )
                    matching_approvals = [
                        grant
                        for grant in approvals
                        if isinstance(grant, ApprovalGrant)
                        and grant.scope_digest == required_scope
                    ]
                    if not matching_approvals:
                        return StoreResult(
                            success=False,
                            status="rejected",
                            action="rollback",
                            transaction_id=transaction_id,
                            trip_id=str(current["trip_id"]),
                            base_revision=base_revision,
                            current_revision=str(current["revision"]),
                            generation=int(current["generation"]),
                            required_approval_scope=required_scope,
                            problems=(
                                StoreProblem(
                                    code=(
                                        "APPROVAL_SCOPE_MISMATCH"
                                        if approvals
                                        else "APPROVAL_REQUIRED"
                                    ),
                                    message=(
                                        "Provided approval does not match the "
                                        "exact protected rollback scope."
                                        if approvals
                                        else "Protected rollback changes "
                                        "require human approval."
                                    ),
                                    details={
                                        "required_scope_digest": required_scope,
                                        "protected_change_count": len(
                                            protected_changes
                                        ),
                                    },
                                ),
                            ),
                            candidate_plan=candidate,
                        )

                report_or_problem = self._kernel_check(
                    candidate,
                    evaluation_at=normalized_evaluation_at,
                )
                if isinstance(report_or_problem, StoreProblem):
                    return self._rejected_problem(
                        "rollback", report_or_problem
                    )
                report = report_or_problem
                if report.status is CheckStatus.INFEASIBLE:
                    return self._infeasible_result(
                        action="rollback",
                        plan=candidate,
                        report=report,
                        base_revision=base_revision,
                        current_revision=str(current["revision"]),
                        required_approval_scope=required_scope,
                        required_lodging_confirmation_scope=lodging_scope,
                        lodging_confirmation_granted=bool(matching_lodging),
                    )

                rollback_transaction_id = _new_transaction_id()
                receipts = candidate["receipts"]
                assert isinstance(receipts, dict)
                preserved_target = deep_copy_json(target_receipt)
                assert isinstance(preserved_target, dict)
                preserved_target["status"] = "rolled_back"
                preserved_target["rolled_back_by"] = rollback_transaction_id
                preserved_target["rolled_back_revision"] = candidate[
                    "revision"
                ]
                receipts[target_key] = preserved_target
                rollback_receipt = {
                    "kind": "rollback",
                    "status": "applied",
                    "request_digest": request_digest,
                    "transaction_id": rollback_transaction_id,
                    "target_transaction_id": transaction_id,
                    "base_revision": base_revision,
                    "applied_revision": candidate["revision"],
                    "applied_generation": candidate["generation"],
                    "check_status": report.status.value,
                    "required_approval_scope": required_scope,
                    "required_lodging_confirmation_scope": lodging_scope,
                    "lodging_confirmation_grant_ids": [
                        grant.grant_id for grant in matching_lodging
                    ],
                    "approval_ids": [
                        grant.approval_id for grant in matching_approvals
                    ],
                    "before_snapshot_sha256": hashlib.sha256(
                        current_bytes
                    ).hexdigest(),
                }
                if normalized_evaluation_at is not None:
                    rollback_receipt["evaluation_at"] = (
                        normalized_evaluation_at.isoformat()
                    )
                receipts[idempotency_key] = rollback_receipt
                candidate_bytes = encode_plan(candidate)
                candidate = decode_plan(candidate_bytes)

                outcome = self._atomic_replace_plan(
                    candidate_bytes,
                    transaction_id=rollback_transaction_id,
                    before_bytes=current_bytes,
                )
                if outcome.problem is not None:
                    return self._write_failure_result(
                        action="rollback",
                        transaction_id=rollback_transaction_id,
                        plan=candidate,
                        outcome=outcome,
                        report=report,
                        base_revision=base_revision,
                        applied_revision=str(candidate["revision"]),
                        receipt=rollback_receipt,
                        required_approval_scope=required_scope,
                        required_lodging_confirmation_scope=lodging_scope,
                        lodging_confirmation_granted=bool(matching_lodging),
                    )
                return StoreResult(
                    success=True,
                    status="rolled_back",
                    action="rollback",
                    transaction_id=rollback_transaction_id,
                    trip_id=str(candidate["trip_id"]),
                    base_revision=base_revision,
                    applied_revision=str(candidate["revision"]),
                    current_revision=str(candidate["revision"]),
                    generation=int(candidate["generation"]),
                    changed=True,
                    check_status=report.status.value,
                    required_approval_scope=required_scope,
                    required_lodging_confirmation_scope=lodging_scope,
                    lodging_confirmation_granted=bool(matching_lodging),
                    candidate_plan=candidate,
                    receipt=rollback_receipt,
                    check_report=report,
                )
        except (OSError, PlanCodecError, StoreError) as exc:
            return self._exception_result("rollback", exc)

    def _read_history_snapshot(self, transaction_id: str) -> bytes:
        path = self._history_path(transaction_id)
        try:
            value = self._read_regular_bytes(path, required=True)
        except StoreError as exc:
            raise StoreError(
                "MISSING_HISTORY",
                "rollback history snapshot is unavailable",
                path=str(path),
            ) from exc
        assert value is not None
        return value

    def _rejected(
        self,
        action: str,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] = MappingProxyType({}),
        dry_run: bool = False,
    ) -> StoreResult:
        return StoreResult(
            success=False,
            status="rejected",
            action=action,
            dry_run=dry_run,
            problems=(StoreProblem(code=code, message=message, details=details),),
        )

    def _rejected_problem(
        self, action: str, problem: StoreProblem
    ) -> StoreResult:
        return StoreResult(
            success=False,
            status="rejected",
            action=action,
            problems=(problem,),
        )

    def _exception_result(
        self,
        action: str,
        exc: Exception,
        *,
        dry_run: bool = False,
        draft: PatchDraft | None = None,
        base_revision: str | None = None,
        current_revision: str | None = None,
    ) -> StoreResult:
        code = getattr(exc, "code", "STORE_ERROR")
        details: dict[str, Any] = {}
        path = getattr(exc, "path", None)
        if path is not None:
            details["path"] = str(path)
        return StoreResult(
            success=False,
            status="rejected",
            action=action,
            base_revision=base_revision,
            current_revision=current_revision,
            dry_run=dry_run,
            problems=(
                StoreProblem(
                    code=str(code),
                    message=str(exc),
                    details=details,
                ),
            ),
            draft=draft,
        )

    def _infeasible_result(
        self,
        *,
        action: str,
        plan: Mapping[str, Any],
        report: CheckReport,
        base_revision: str | None = None,
        current_revision: str | None = None,
        dry_run: bool = False,
        draft: PatchDraft | None = None,
        required_approval_scope: str | None = None,
        required_lodging_confirmation_scope: str | None = None,
        lodging_confirmation_granted: bool = False,
    ) -> StoreResult:
        return StoreResult(
            success=False,
            status="rejected",
            action=action,
            trip_id=_optional_string(plan.get("trip_id")),
            base_revision=base_revision,
            current_revision=current_revision,
            generation=_optional_int(plan.get("generation")),
            dry_run=dry_run,
            changed=False,
            check_status=report.status.value,
            required_approval_scope=required_approval_scope,
            required_lodging_confirmation_scope=required_lodging_confirmation_scope,
            lodging_confirmation_granted=lodging_confirmation_granted,
            problems=(
                StoreProblem(
                    code="PLAN_INFEASIBLE",
                    message="Candidate plan violates one or more hard constraints.",
                    details={
                        "issue_codes": [issue.code for issue in report.errors],
                        "error_count": len(report.errors),
                    },
                ),
            ),
            draft=draft,
            candidate_plan=plan,
            check_report=report,
        )

    def _write_failure_result(
        self,
        *,
        action: str,
        transaction_id: str,
        plan: Mapping[str, Any],
        outcome: _WriteOutcome,
        report: CheckReport | None,
        base_revision: str | None = None,
        applied_revision: str | None = None,
        receipt: Mapping[str, Any] | None = None,
        required_approval_scope: str | None = None,
        required_lodging_confirmation_scope: str | None = None,
        lodging_confirmation_granted: bool = False,
    ) -> StoreResult:
        assert outcome.problem is not None
        return StoreResult(
            success=False,
            status=(
                "commit_outcome_unknown"
                if outcome.replaced
                else "write_failed"
            ),
            action=action,
            transaction_id=transaction_id,
            trip_id=_optional_string(plan.get("trip_id")),
            base_revision=base_revision,
            applied_revision=applied_revision
            or _optional_string(plan.get("revision")),
            current_revision=(
                None
                if outcome.replaced
                else base_revision
            ),
            generation=_optional_int(plan.get("generation")),
            changed=outcome.replaced,
            check_status=report.status.value if report is not None else None,
            required_approval_scope=required_approval_scope,
            required_lodging_confirmation_scope=(
                required_lodging_confirmation_scope
            ),
            lodging_confirmation_granted=lodging_confirmation_granted,
            problems=(outcome.problem,),
            candidate_plan=plan,
            receipt=receipt,
            check_report=report,
        )


def _validate_slug(slug: str) -> str:
    if not isinstance(slug, str):
        raise TypeError("slug must be a string")
    if (
        not slug
        or len(slug) > _MAX_SLUG_LENGTH
        or not _SLUG_RE.fullmatch(slug)
    ):
        raise StoreError(
            "INVALID_SLUG",
            (
                "slug must be lowercase ASCII alphanumeric segments separated "
                "by single hyphens"
            ),
            path="slug",
        )
    return slug


def _validate_idempotency_key(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("idempotency key must be a string")
    if (
        not value
        or len(value) > _MAX_IDEMPOTENCY_KEY_LENGTH
        or value != value.strip()
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise StoreError(
            "INVALID_IDEMPOTENCY_KEY",
            (
                "idempotency key must be 1-256 visible characters without "
                "leading/trailing whitespace or control characters"
            ),
            path="idempotency_key",
        )
    return value


def _normalize_evaluation_at(
    value: datetime | None,
) -> datetime | None:
    """Validate and normalize one deterministic kernel evaluation instant."""

    if value is None:
        return None
    if not isinstance(value, datetime):
        raise StoreError(
            "INVALID_EVALUATION_AT",
            "evaluation_at must be a timezone-aware datetime or None",
            path="evaluation_at",
        )
    if value.tzinfo is None or value.utcoffset() is None:
        raise StoreError(
            "INVALID_EVALUATION_AT",
            "evaluation_at must include a UTC offset",
            path="evaluation_at",
        )
    return value.astimezone(timezone.utc)


def _validate_transaction_id(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("transaction_id must be a string")
    if not _TRANSACTION_RE.fullmatch(value):
        raise StoreError(
            "INVALID_TRANSACTION_ID",
            "transaction ID is not a store-generated identifier",
            path="transaction_id",
        )
    return value


def _validate_revision(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("base_revision must be a string")
    if not _REVISION_RE.fullmatch(value):
        raise StoreError(
            "INVALID_BASE_REVISION",
            "base revision must be a lowercase 64-character SHA-256 digest",
            path="base_revision",
        )
    return value


def _rollback_request_digest(
    *,
    transaction_id: str,
    base_revision: str,
    idempotency_key: str,
) -> str:
    payload = {
        "kind": "rollback",
        "transaction_id": transaction_id,
        "base_revision": base_revision,
        "idempotency_key": idempotency_key,
    }
    return f"sha256:{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}"


def _find_transaction_receipt(
    receipts: Any, transaction_id: str
) -> tuple[str | None, Mapping[str, Any] | None]:
    if not isinstance(receipts, Mapping):
        return None, None
    for key in sorted(receipts):
        receipt = receipts[key]
        if (
            isinstance(key, str)
            and isinstance(receipt, Mapping)
            and receipt.get("transaction_id") == transaction_id
        ):
            return key, receipt
    return None, None


def _rollback_protected_changes(
    current: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> tuple[ChangeRecord, ...]:
    """Describe rollback changes that require an exact human approval.

    Rollback restores a whole earlier state rather than replaying patch
    operations, so this mirrors the mutation policy at the resulting net-diff
    boundary. Display-only edits intentionally stay outside approval scope.
    """

    current_days, current_activities = _rollback_entity_index(current)
    target_days, target_activities = _rollback_entity_index(candidate)
    protected_ids = (
        _metadata_protected_ids(current)
        | _metadata_protected_ids(candidate)
        | {
            activity_id
            for activity_id, (_, _, activity) in current_activities.items()
            if _activity_is_protected(activity)
        }
        | {
            activity_id
            for activity_id, (_, _, activity) in target_activities.items()
            if _activity_is_protected(activity)
        }
    )
    changes: list[ChangeRecord] = []
    for activity_id in sorted(protected_ids):
        before_found = current_activities.get(activity_id)
        after_found = target_activities.get(activity_id)
        if before_found is None and after_found is None:
            continue
        if before_found is None or after_found is None:
            changes.append(
                ChangeRecord(
                    op_id="rollback-policy",
                    entity_type="activity",
                    entity_id=activity_id,
                    field="$entity",
                    before=(
                        None if before_found is None else before_found[2]
                    ),
                    after=None if after_found is None else after_found[2],
                    kind="protected_rollback",
                )
            )
            continue

        before_day_id, before_index, before_activity = before_found
        after_day_id, after_index, after_activity = after_found
        comparisons: list[tuple[str, Any, Any]] = [
            ("day_id", before_day_id, after_day_id),
            (
                "position",
                {"day_id": before_day_id, "index": before_index},
                {"day_id": after_day_id, "index": after_index},
            ),
            ("time", before_activity.get("time"), after_activity.get("time")),
            (
                "duration_min",
                before_activity.get("duration_min"),
                after_activity.get("duration_min"),
            ),
            (
                "allowed_windows",
                before_activity.get("allowed_windows"),
                after_activity.get("allowed_windows"),
            ),
            (
                "location",
                _rollback_location_identity(before_activity),
                _rollback_location_identity(after_activity),
            ),
            (
                "decision_state",
                _rollback_field_marker(before_activity, "decision_state"),
                _rollback_field_marker(after_activity, "decision_state"),
            ),
            (
                "flexibility",
                _rollback_field_marker(before_activity, "flexibility"),
                _rollback_field_marker(after_activity, "flexibility"),
            ),
        ]
        if before_day_id == after_day_id:
            before_day = current_days.get(before_day_id, {})
            after_day = target_days.get(after_day_id, {})
            comparisons.extend(
                (
                    (
                        f"day.{field}",
                        before_day.get(field),
                        after_day.get(field),
                    )
                    for field in (
                        "allowed_modes",
                        "available_end",
                        "available_start",
                        "date",
                        "day",
                        "end_location_id",
                        "start_location_id",
                        "timezone",
                    )
                )
            )
        for field, before, after in comparisons:
            if canonical_json_bytes(before) == canonical_json_bytes(after):
                continue
            changes.append(
                ChangeRecord(
                    op_id="rollback-policy",
                    entity_type="activity",
                    entity_id=activity_id,
                    field=field,
                    before=before,
                    after=after,
                    kind="protected_rollback",
                )
            )
    changes.extend(
        hard_constraint_protected_changes(
            current,
            candidate,
            protect_target_hard=True,
        )
    )
    return tuple(
        sorted(
            changes,
            key=lambda change: (
                change.entity_type,
                change.entity_id,
                change.field,
                canonical_json_bytes(change.before),
                canonical_json_bytes(change.after),
            ),
        )
    )


def _rollback_lodging_changes(
    current: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> tuple[ChangeRecord, ...]:
    """Represent the full lodging binding as one exact rollback scope item."""

    def binding(plan: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "lodgings": plan["state"]["trip"].get("lodgings", []),
            "days": [
                {
                    key: day.get(key)
                    for key in (
                        "day_id",
                        "start_lodging_id",
                        "end_lodging_id",
                        "start_location_id",
                        "end_location_id",
                    )
                }
                for day in plan["state"]["itinerary"]["days"]
            ],
        }

    before = binding(current)
    after = binding(candidate)
    if canonical_json_bytes(before) == canonical_json_bytes(after):
        return ()
    return (
        ChangeRecord(
            "rollback-policy",
            "lodging",
            "lodgings",
            "selection",
            before,
            after,
            "lodging_rollback",
        ),
    )


def _rollback_entity_index(
    plan: Mapping[str, Any],
) -> tuple[
    dict[str, Mapping[str, Any]],
    dict[str, tuple[str, int, Mapping[str, Any]]],
]:
    days_by_id: dict[str, Mapping[str, Any]] = {}
    activities: dict[str, tuple[str, int, Mapping[str, Any]]] = {}
    try:
        days = plan["state"]["itinerary"]["days"]
    except (KeyError, TypeError):
        return days_by_id, activities
    if not isinstance(days, Sequence) or isinstance(days, (str, bytes)):
        return days_by_id, activities
    for day in days:
        if not isinstance(day, Mapping):
            continue
        day_id = day.get("day_id")
        if not isinstance(day_id, str) or not day_id:
            continue
        days_by_id[day_id] = day
        places = day.get("places")
        if not isinstance(places, Sequence) or isinstance(
            places, (str, bytes)
        ):
            continue
        for index, activity in enumerate(places):
            if not isinstance(activity, Mapping):
                continue
            activity_id = activity.get("activity_id")
            if isinstance(activity_id, str) and activity_id:
                activities[activity_id] = (day_id, index, activity)
    return days_by_id, activities


def _metadata_protected_ids(plan: Mapping[str, Any]) -> set[str]:
    try:
        raw = plan["state"]["trip"]["_trip_planner"]["migration"][
            "protected_activity_ids"
        ]
    except (KeyError, TypeError):
        return set()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return set()
    return {
        value for value in raw if isinstance(value, str) and bool(value)
    }


def _activity_is_protected(activity: Mapping[str, Any]) -> bool:
    if "decision_state" not in activity or "flexibility" not in activity:
        return True
    decision = activity.get("decision_state")
    flexibility = activity.get("flexibility")
    if decision not in {
        "cancelled",
        "excluded",
        "candidate",
        "selected",
        "fixed",
        "booked",
    }:
        return True
    if flexibility not in {"movable", "fixed_day", "fixed_time"}:
        return True
    return decision in {"fixed", "booked"} or flexibility in {
        "fixed_day",
        "fixed_time",
    }


def _rollback_location_identity(
    activity: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        field: deep_copy_json(activity.get(field))
        for field in ("location_id", "place_id", "lat", "lng", "maps_query")
    }


def _rollback_field_marker(
    activity: Mapping[str, Any], field: str
) -> Any:
    if field not in activity:
        return {"unclassified": True}
    return deep_copy_json(activity[field])


def _require_directory(
    path: Path,
    label: str,
    *,
    allow_symlink_target: bool = False,
) -> None:
    try:
        info = path.stat() if allow_symlink_target else path.lstat()
    except FileNotFoundError as exc:
        raise StoreError(
            "UNSAFE_PATH",
            f"{label} does not exist",
            path=str(path),
        ) from exc
    if not allow_symlink_target and stat.S_ISLNK(info.st_mode):
        raise StoreError(
            "UNSAFE_PATH",
            f"{label} must not be a symlink",
            path=str(path),
        )
    if not stat.S_ISDIR(info.st_mode):
        raise StoreError(
            "UNSAFE_PATH",
            f"{label} must be a directory",
            path=str(path),
        )


def _require_regular_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise StoreError(
            "UNSAFE_PATH",
            f"{label} does not exist",
            path=str(path),
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise StoreError(
            "UNSAFE_PATH",
            f"{label} must be a non-symlink regular file",
            path=str(path),
        )


def _require_optional_regular_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise StoreError(
            "UNSAFE_PATH",
            f"{label} must be a non-symlink regular file",
            path=str(path),
        )


def _new_transaction_id() -> str:
    return f"tx-{uuid.uuid4().hex}"


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _mutation_problem(problem: MutationProblem) -> StoreProblem:
    details = deep_copy_json(problem.details)
    assert isinstance(details, dict)
    if problem.op_id is not None:
        details["op_id"] = problem.op_id
    if problem.entity_type is not None:
        details["entity_type"] = problem.entity_type
    if problem.entity_id is not None:
        details["entity_id"] = problem.entity_id
    return StoreProblem(
        code=problem.code,
        message=problem.message,
        details=details,
    )


__all__ = [
    "FaultHook",
    "LodgingConfirmationVerifier",
    "StoreError",
    "StoreProblem",
    "StoreResult",
    "TripStore",
]
