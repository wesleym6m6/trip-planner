"""Crash-aware current-state persistence for policy-authorized evidence.

The evidence cache is deliberately separate from ``plan.json`` and has no
history, receipts, rollback snapshots, or provider I/O.  It stores only
observations whose current host policy explicitly permits disk persistence.
Every public operation obtains the per-trip lock, samples one trusted clock
instant, applies retention, and uses a same-directory atomic replace when the
durable record set changes.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
import time
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .facts import (
    FACT_OBSERVATION_VERSION,
    AuthorizedProviderResult,
    EvidenceLedger,
    EvidencePersistence,
    EvidencePrune,
    EvidenceSnapshot,
    FactContractError,
    FactKey,
    FactKind,
    FactObservation,
    FactValue,
    ProviderPolicyRegistry,
    ProviderProblem,
    ProviderProvenance,
    _restore_durable_evidence_ledger,
    merge_provider_result,
    prune_evidence,
)


EVIDENCE_STORE_VERSION = "evidence-store/v2"

_CACHE_FILENAME = ".trip-planner-evidence.json"
_LOCK_FILENAME = ".trip-planner.lock"
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_UTC_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T"
    r"\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{6})?Z"
)
_TEMP_NAME_RE = re.compile(
    r"\.trip-planner-evidence\.[A-Za-z0-9_-]+\.tmp"
)
_MAX_SLUG_LENGTH = 128
_MAX_TRIP_ID_LENGTH = 256
_MAX_CACHE_BYTES = 16 * 1024 * 1024
_MAX_RECORDS = 4096
_MAX_ORPHAN_TEMPS = 4096
_MAX_GENERATION = 2**63 - 1
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

Clock = Callable[[], datetime]
FaultHook = Callable[[str], None]
ResetNonceSource = Callable[[], bytes]

_RESET_NONCE_BYTES = 32
_CORRUPT_RESET_DOMAIN = (
    b"trip-planner.evidence-store.corrupt-reset/v3\0"
)
_OVERSIZED_RESET_DOMAIN = (
    b"trip-planner.evidence-store.oversized-reset/v3\0"
)


class EvidenceStoreError(FactContractError):
    """Unsafe filesystem or trusted-clock boundary failure."""


@dataclass(frozen=True, slots=True)
class EvidenceStoreProblem:
    """Redacted machine-readable store failure."""

    code: str
    message: str

    def __post_init__(self) -> None:
        _require_visible_text(self.code, "EvidenceStoreProblem.code", 128)
        _require_visible_text(
            self.message, "EvidenceStoreProblem.message", 1024
        )

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class EvidenceStoreResult:
    """One redacted evidence-cache operation result."""

    success: bool
    status: str
    action: str
    ledger: EvidenceLedger | None = field(default=None, repr=False)
    previous_revision: str | None = None
    current_revision: str | None = None
    expected_revision: str | None = None
    generation: int | None = None
    purge_checked_at: datetime | None = None
    changed: bool = False
    replayed: bool = False
    purged_observation_ids: tuple[str, ...] = ()
    promoted_observation_ids: tuple[str, ...] = ()
    ignored_observation_ids: tuple[str, ...] = ()
    provider_problems: tuple[ProviderProblem, ...] = field(
        default=(),
        repr=False,
    )
    problems: tuple[EvidenceStoreProblem, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.success, bool):
            raise TypeError("EvidenceStoreResult.success must be bool")
        _require_visible_text(self.status, "EvidenceStoreResult.status", 128)
        _require_visible_text(self.action, "EvidenceStoreResult.action", 128)
        if self.ledger is not None and type(self.ledger) is not EvidenceLedger:
            raise TypeError(
                "EvidenceStoreResult.ledger must be EvidenceLedger or None"
            )
        for value, name in (
            (self.previous_revision, "previous_revision"),
            (self.current_revision, "current_revision"),
            (self.expected_revision, "expected_revision"),
        ):
            if value is not None and not _DIGEST_RE.fullmatch(value):
                raise ValueError(
                    f"EvidenceStoreResult.{name} must be a digest or None"
                )
        if self.generation is not None and (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
            or self.generation > _MAX_GENERATION
        ):
            raise ValueError(
                "EvidenceStoreResult.generation must be non-negative or None"
            )
        if self.purge_checked_at is not None:
            object.__setattr__(
                self,
                "purge_checked_at",
                _aware_utc(
                    self.purge_checked_at,
                    "EvidenceStoreResult.purge_checked_at",
                ),
            )
        for value, name in (
            (self.changed, "changed"),
            (self.replayed, "replayed"),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"EvidenceStoreResult.{name} must be bool")
        for value, name in (
            (self.purged_observation_ids, "purged_observation_ids"),
            (self.promoted_observation_ids, "promoted_observation_ids"),
            (self.ignored_observation_ids, "ignored_observation_ids"),
        ):
            if not isinstance(value, tuple):
                raise TypeError(f"EvidenceStoreResult.{name} must be a tuple")
            for observation_id in value:
                if not isinstance(observation_id, str) or not (
                    _DIGEST_RE.fullmatch(observation_id)
                ):
                    raise ValueError(
                        f"EvidenceStoreResult.{name} contains an invalid ID"
                    )
            object.__setattr__(self, name, tuple(sorted(set(value))))
        if not isinstance(self.provider_problems, tuple) or any(
            type(item) is not ProviderProblem
            for item in self.provider_problems
        ):
            raise TypeError(
                "EvidenceStoreResult.provider_problems must be typed"
            )
        if not isinstance(self.problems, tuple) or any(
            type(item) is not EvidenceStoreProblem for item in self.problems
        ):
            raise TypeError("EvidenceStoreResult.problems must be typed")
        if self.success and self.ledger is None:
            raise ValueError("Successful evidence results require a ledger")
        if not self.success and not self.problems:
            raise ValueError("Failed evidence results require a problem")

    def to_dict(self) -> dict[str, Any]:
        """Return diagnostics without normalized provider values."""

        return {
            "success": self.success,
            "status": self.status,
            "action": self.action,
            "previous_revision": self.previous_revision,
            "current_revision": self.current_revision,
            "expected_revision": self.expected_revision,
            "generation": self.generation,
            "purge_checked_at": (
                _utc_iso(self.purge_checked_at)
                if self.purge_checked_at is not None
                else None
            ),
            "changed": self.changed,
            "replayed": self.replayed,
            "observation_count": (
                len(self.ledger.observations)
                if self.ledger is not None
                else None
            ),
            "purged_observation_ids": list(
                self.purged_observation_ids
            ),
            "promoted_observation_ids": list(
                self.promoted_observation_ids
            ),
            "ignored_observation_ids": list(
                self.ignored_observation_ids
            ),
            "provider_problems": [
                item.to_dict() for item in self.provider_problems
            ],
            "problems": [item.to_dict() for item in self.problems],
        }

    def snapshot(self, *, evaluation_at: datetime) -> EvidenceSnapshot:
        """Build an immutable view bound to this exact durable revision."""

        if (
            not self.success
            or self.ledger is None
            or self.current_revision is None
            or self.purge_checked_at is None
        ):
            raise EvidenceStoreError(
                "EVIDENCE_SNAPSHOT_UNAVAILABLE",
                "Only a successful current evidence result can be composed.",
            )
        return EvidenceSnapshot.from_ledger(
            self.ledger,
            evaluation_at=evaluation_at,
            purge_now=self.purge_checked_at,
            store_revision=self.current_revision,
        )


@dataclass(frozen=True, slots=True)
class _StoredEvidence:
    ledger: EvidenceLedger
    store_revision: str
    store_epoch: str
    existed: bool


@dataclass(frozen=True, slots=True)
class _WriteOutcome:
    replaced: bool
    problem_code: str | None = None


class _OversizedEvidenceCache(FactContractError):
    def __init__(self, raw_digest: bytes) -> None:
        super().__init__(
            "CACHE_CORRUPTED",
            "Evidence cache exceeds its size limit.",
        )
        if type(raw_digest) is not bytes or len(raw_digest) != 32:
            raise ValueError(
                "Oversized evidence digest must contain exactly 256 bits."
            )
        self.raw_digest = raw_digest


class _TrustedUtcClock:
    """UTC wall clock clamped by monotonic elapsed time and prior reads."""

    def __init__(self, source: Clock | None) -> None:
        if source is not None and not callable(source):
            raise TypeError("clock must be callable or None")
        self._source = source
        self._anchor_wall = datetime.now(timezone.utc)
        self._anchor_monotonic = time.monotonic()
        self._last: datetime | None = None

    def now(self) -> datetime:
        if self._source is None:
            wall = datetime.now(timezone.utc)
            elapsed = time.monotonic() - self._anchor_monotonic
            projected = self._anchor_wall + timedelta(
                seconds=max(0.0, elapsed)
            )
            candidate = max(wall, projected)
        else:
            candidate = self._source()
        normalized = _aware_utc(candidate, "trusted evidence clock")
        if self._last is not None and normalized < self._last:
            normalized = self._last
        self._last = normalized
        return normalized


class EvidenceStore:
    """One strict, current-state evidence cache for a canonical trip."""

    def __init__(
        self,
        trips_root: str | Path,
        slug: str,
        trip_id: str,
        policies: ProviderPolicyRegistry,
        *,
        clock: Clock | None = None,
        fault_hook: FaultHook | None = None,
        reset_nonce_source: ResetNonceSource | None = None,
    ) -> None:
        self.slug = _validate_slug(slug)
        _require_visible_text(
            trip_id, "EvidenceStore.trip_id", _MAX_TRIP_ID_LENGTH
        )
        if type(policies) is not ProviderPolicyRegistry:
            raise TypeError(
                "EvidenceStore.policies must be ProviderPolicyRegistry"
            )
        if fault_hook is not None and not callable(fault_hook):
            raise TypeError("fault_hook must be callable or None")
        if reset_nonce_source is not None and not callable(
            reset_nonce_source
        ):
            raise TypeError(
                "reset_nonce_source must be callable or None"
            )
        try:
            root = Path(trips_root).resolve(strict=True)
        except OSError as exc:
            raise EvidenceStoreError(
                "INVALID_TRIPS_ROOT",
                "Evidence trips root is unavailable.",
            ) from exc
        _require_directory(root, "trips root", allow_symlink_target=True)
        self.trips_root = root
        self.trip_dir = root / self.slug
        self.data_dir = self.trip_dir / "data"
        self.cache_path = self.data_dir / _CACHE_FILENAME
        self.lock_path = self.data_dir / _LOCK_FILENAME
        self.trip_id = trip_id
        self.policies = policies
        self._clock = _TrustedUtcClock(clock)
        self._fault_hook = fault_hook
        self._reset_nonce_source = (
            _secure_reset_nonce
            if reset_nonce_source is None
            else reset_nonce_source
        )
        self._validate_layout()

    def load(self) -> EvidenceStoreResult:
        """Load and durably purge expired records before returning a ledger."""

        return self._read_and_prune(action="load")

    def cleanup(self) -> EvidenceStoreResult:
        """Idempotently apply retention without accepting provider content."""

        return self._read_and_prune(action="cleanup")

    def merge(
        self,
        authorized_result: AuthorizedProviderResult,
    ) -> EvidenceStoreResult:
        """Merge one disk-authorized result under the current host policy."""

        if type(authorized_result) is not AuthorizedProviderResult:
            raise TypeError(
                "authorized_result must be AuthorizedProviderResult"
            )
        try:
            policy = self.policies.policy(
                authorized_result.request.policy_id
            )
        except FactContractError:
            return self._failed(
                action="merge",
                status="rejected",
                code="UNTRUSTED_PROVENANCE",
                message="Provider result policy is not currently authorized.",
            )
        if policy.persistence is EvidencePersistence.MEMORY_ONLY:
            return self._failed(
                action="merge",
                status="rejected",
                code="MEMORY_ONLY_RESULT",
                message=(
                    "Memory-only provider evidence must remain in the "
                    "run-scoped session."
                ),
            )

        with self._exclusive_lock():
            checked_at = self._clock.now()
            current_or_error = self._load_locked("merge", checked_at)
            if isinstance(current_or_error, EvidenceStoreResult):
                return current_or_error
            current = current_or_error
            previous_revision = current.store_revision
            store_epoch = current.store_epoch
            retained_current_ids = frozenset(
                observation.observation_id
                for observation in current.ledger.observations
                if observation.retained_at(checked_at)
            )
            try:
                merged = merge_provider_result(
                    current.ledger,
                    authorized_result,
                    purge_now=checked_at,
                )
            except FactContractError as exc:
                pruned = _prune_durable_evidence(
                    current.ledger,
                    purge_now=checked_at,
                )
                if pruned.changed:
                    candidate_revision = _store_revision(
                        self.trip_id,
                        self.policies,
                        pruned.ledger,
                        store_epoch,
                    )
                    outcome = self._replace_ledger(
                        pruned.ledger,
                        candidate_revision,
                        store_epoch,
                    )
                    if outcome.problem_code is not None:
                        return self._write_failure(
                            action="merge",
                            checked_at=checked_at,
                            previous_revision=previous_revision,
                            expected_revision=candidate_revision,
                            outcome=outcome,
                        )
                    current = _StoredEvidence(
                        ledger=pruned.ledger,
                        store_revision=candidate_revision,
                        store_epoch=store_epoch,
                        existed=True,
                    )
                return EvidenceStoreResult(
                    success=False,
                    status="rejected",
                    action="merge",
                    ledger=current.ledger,
                    previous_revision=previous_revision,
                    current_revision=current.store_revision,
                    generation=current.ledger.generation,
                    purge_checked_at=checked_at,
                    changed=pruned.changed,
                    purged_observation_ids=(
                        pruned.purged_observation_ids
                    ),
                    problems=(
                        EvidenceStoreProblem(
                            code=exc.code,
                            message=(
                                "Provider result failed the trusted evidence "
                                "merge boundary."
                            ),
                        ),
                    ),
                )

            candidate_revision = _store_revision(
                self.trip_id,
                self.policies,
                merged.ledger,
                store_epoch,
            )
            if not merged.changed:
                return EvidenceStoreResult(
                    success=True,
                    status="no_op",
                    action="merge",
                    ledger=current.ledger,
                    previous_revision=previous_revision,
                    current_revision=previous_revision,
                    generation=current.ledger.generation,
                    purge_checked_at=checked_at,
                    changed=False,
                    replayed=bool(
                        authorized_result.result.observations
                    )
                    and not merged.problems
                    and all(
                        observation.observation_id
                        in retained_current_ids
                        for observation
                        in authorized_result.result.observations
                    ),
                    purged_observation_ids=(
                        merged.purged_observation_ids
                    ),
                    promoted_observation_ids=(
                        merged.promoted_observation_ids
                    ),
                    ignored_observation_ids=(
                        merged.ignored_observation_ids
                    ),
                    provider_problems=merged.problems,
                )

            outcome = self._replace_ledger(
                merged.ledger,
                candidate_revision,
                store_epoch,
            )
            if outcome.problem_code is not None:
                if (
                    outcome.problem_code == "CACHE_WRITE_FAILED"
                    and merged.purged_observation_ids
                ):
                    pruned = _prune_durable_evidence(
                        current.ledger,
                        purge_now=checked_at,
                    )
                    if pruned.changed:
                        purge_revision = _store_revision(
                            self.trip_id,
                            self.policies,
                            pruned.ledger,
                            store_epoch,
                        )
                        purge_outcome = self._replace_ledger(
                            pruned.ledger,
                            purge_revision,
                            store_epoch,
                        )
                        if purge_outcome.problem_code is not None:
                            return self._write_failure(
                                action="merge",
                                checked_at=checked_at,
                                previous_revision=previous_revision,
                                expected_revision=purge_revision,
                                outcome=purge_outcome,
                            )
                        return EvidenceStoreResult(
                            success=False,
                            status="write_failed",
                            action="merge",
                            ledger=pruned.ledger,
                            previous_revision=previous_revision,
                            current_revision=purge_revision,
                            expected_revision=candidate_revision,
                            generation=pruned.ledger.generation,
                            purge_checked_at=checked_at,
                            changed=True,
                            purged_observation_ids=(
                                pruned.purged_observation_ids
                            ),
                            provider_problems=merged.problems,
                            problems=(
                                EvidenceStoreProblem(
                                    code="CACHE_WRITE_FAILED",
                                    message=(
                                        "Provider promotion was not persisted; "
                                        "required retention deletion completed."
                                    ),
                                ),
                            ),
                        )
                return self._write_failure(
                    action="merge",
                    checked_at=checked_at,
                    previous_revision=previous_revision,
                    expected_revision=candidate_revision,
                    outcome=outcome,
                )
            return EvidenceStoreResult(
                success=True,
                status="merged",
                action="merge",
                ledger=merged.ledger,
                previous_revision=previous_revision,
                current_revision=candidate_revision,
                generation=merged.ledger.generation,
                purge_checked_at=checked_at,
                changed=True,
                purged_observation_ids=merged.purged_observation_ids,
                promoted_observation_ids=(
                    merged.promoted_observation_ids
                ),
                ignored_observation_ids=merged.ignored_observation_ids,
                provider_problems=merged.problems,
            )

    def _read_and_prune(self, *, action: str) -> EvidenceStoreResult:
        with self._exclusive_lock():
            checked_at = self._clock.now()
            current_or_error = self._load_locked(action, checked_at)
            if isinstance(current_or_error, EvidenceStoreResult):
                return current_or_error
            current = current_or_error
            store_epoch = current.store_epoch
            pruned = _prune_durable_evidence(
                current.ledger,
                purge_now=checked_at,
            )
            if not pruned.changed:
                status = (
                    "empty"
                    if not current.existed
                    else ("no_op" if action == "cleanup" else "loaded")
                )
                return EvidenceStoreResult(
                    success=True,
                    status=status,
                    action=action,
                    ledger=current.ledger,
                    current_revision=current.store_revision,
                    generation=current.ledger.generation,
                    purge_checked_at=checked_at,
                )

            candidate_revision = _store_revision(
                self.trip_id,
                self.policies,
                pruned.ledger,
                store_epoch,
            )
            outcome = self._replace_ledger(
                pruned.ledger,
                candidate_revision,
                store_epoch,
            )
            if outcome.problem_code is not None:
                return self._write_failure(
                    action=action,
                    checked_at=checked_at,
                    previous_revision=current.store_revision,
                    expected_revision=candidate_revision,
                    outcome=outcome,
                )
            return EvidenceStoreResult(
                success=True,
                status="cleaned" if action == "cleanup" else "purged",
                action=action,
                ledger=pruned.ledger,
                previous_revision=current.store_revision,
                current_revision=candidate_revision,
                generation=pruned.ledger.generation,
                purge_checked_at=checked_at,
                changed=True,
                purged_observation_ids=pruned.purged_observation_ids,
            )

    def _load_locked(
        self,
        action: str,
        checked_at: datetime,
    ) -> _StoredEvidence | EvidenceStoreResult:
        data: bytes | None = None
        try:
            data = self._read_regular_bytes(
                self.cache_path,
                required=False,
            )
            if data is None:
                ledger = EvidenceLedger(policies=self.policies)
                store_epoch = _initial_store_epoch(
                    self.trip_id,
                    self.policies,
                )
                return _StoredEvidence(
                    ledger=ledger,
                    store_revision=_store_revision(
                        self.trip_id,
                        self.policies,
                        ledger,
                        store_epoch,
                    ),
                    store_epoch=store_epoch,
                    existed=False,
                )
            stored = _decode_document(
                data,
                expected_trip_id=self.trip_id,
                policies=self.policies,
            )
            future = tuple(
                observation
                for observation in stored.ledger.observations
                if observation.retrieved_at > checked_at
            )
            expired = tuple(
                observation
                for observation in stored.ledger.observations
                if not observation.retained_at(checked_at)
            )
            if future and expired:
                return self._reset_corrupt_cache_locked(
                    data,
                    action=action,
                    checked_at=checked_at,
                    previous_revision=stored.store_revision,
                    purged_observation_ids=tuple(
                        observation.observation_id
                        for observation in stored.ledger.observations
                    ),
                )
            if future:
                return self._failed(
                    action=action,
                    status="corrupted",
                    code="CACHE_CORRUPTED",
                    message=(
                        "Evidence cache is valid but contains observations "
                        "after the trusted clock; it was not used or overwritten."
                    ),
                    checked_at=checked_at,
                )
            return stored
        except _OversizedEvidenceCache as exc:
            return self._reset_corrupt_cache_locked(
                None,
                action=action,
                checked_at=checked_at,
                oversized_raw_digest=exc.raw_digest,
            )
        except FactContractError:
            if data is not None:
                document = _probe_corrupt_document(data)
                if (
                    document is not None
                    and isinstance(document.get("trip_id"), str)
                    and document["trip_id"] != self.trip_id
                ):
                    return self._failed(
                        action=action,
                        status="corrupted",
                        code="CACHE_CORRUPTED",
                        message=(
                            "Evidence cache belongs to another trip and was "
                            "not used or overwritten."
                        ),
                        checked_at=checked_at,
                    )
                return self._reset_corrupt_cache_locked(
                    data,
                    action=action,
                    checked_at=checked_at,
                    previous_revision=_probe_store_revision(document),
                )
            return self._failed(
                action=action,
                status="corrupted",
                code="CACHE_CORRUPTED",
                message=(
                    "Evidence cache failed strict validation and could not "
                    "be reset safely."
                ),
                checked_at=checked_at,
            )

    def _reset_corrupt_cache_locked(
        self,
        data: bytes | None,
        *,
        action: str,
        checked_at: datetime,
        previous_revision: str | None = None,
        purged_observation_ids: tuple[str, ...] = (),
        oversized_raw_digest: bytes | None = None,
    ) -> EvidenceStoreResult:
        """Replace current-trip corruption wholesale; never salvage its values."""

        if data is not None and oversized_raw_digest is not None:
            raise EvidenceStoreError(
                "CACHE_CORRUPTED",
                "Corrupt reset received ambiguous content identity.",
            )
        if data is not None:
            domain = _CORRUPT_RESET_DOMAIN
            raw_digest = hashlib.sha256(data).digest()
        elif oversized_raw_digest is not None:
            domain = _OVERSIZED_RESET_DOMAIN
            raw_digest = oversized_raw_digest
        else:
            raise EvidenceStoreError(
                "CACHE_CORRUPTED",
                "Corrupt reset requires a bound content digest.",
            )
        reset_epoch = _reset_store_epoch(
            domain=domain,
            raw_digest=raw_digest,
            nonce=self._next_reset_nonce(),
        )
        cleared = _restore_durable_evidence_ledger(
            policies=self.policies,
            observations=(),
            generation=0,
        )
        cleared_revision = _store_revision(
            self.trip_id,
            self.policies,
            cleared,
            reset_epoch,
        )
        outcome = self._replace_ledger(
            cleared,
            cleared_revision,
            reset_epoch,
        )
        if outcome.problem_code is not None:
            return self._write_failure(
                action=action,
                checked_at=checked_at,
                previous_revision=previous_revision,
                expected_revision=cleared_revision,
                outcome=outcome,
            )
        return EvidenceStoreResult(
            success=False,
            status="corrupted",
            action=action,
            previous_revision=previous_revision,
            current_revision=cleared_revision,
            generation=cleared.generation,
            purge_checked_at=checked_at,
            changed=True,
            purged_observation_ids=purged_observation_ids,
            problems=(
                EvidenceStoreProblem(
                    code="CACHE_CORRUPTED",
                    message=(
                        "Corrupted current-trip evidence was cleared in full "
                        "and was not used."
                    ),
                ),
            ),
        )

    def _next_reset_nonce(self) -> bytes:
        try:
            nonce = self._reset_nonce_source()
        except Exception as exc:
            raise EvidenceStoreError(
                "RESET_NONCE_UNAVAILABLE",
                "Corrupt-cache reset entropy is unavailable.",
            ) from exc
        if type(nonce) is not bytes or len(nonce) != _RESET_NONCE_BYTES:
            raise EvidenceStoreError(
                "RESET_NONCE_UNAVAILABLE",
                "Corrupt-cache reset entropy must contain exactly 256 bits.",
            )
        return nonce

    def _write_failure(
        self,
        *,
        action: str,
        checked_at: datetime,
        previous_revision: str | None,
        expected_revision: str,
        outcome: _WriteOutcome,
    ) -> EvidenceStoreResult:
        code = outcome.problem_code or "CACHE_WRITE_FAILED"
        return EvidenceStoreResult(
            success=False,
            status=(
                "outcome_unknown"
                if code == "CACHE_OUTCOME_UNKNOWN"
                else "write_failed"
            ),
            action=action,
            previous_revision=previous_revision,
            expected_revision=expected_revision,
            purge_checked_at=checked_at,
            changed=outcome.replaced,
            problems=(
                EvidenceStoreProblem(
                    code=code,
                    message=(
                        "Evidence cache replacement may have completed; "
                        "reload and compare the expected revision."
                        if code == "CACHE_OUTCOME_UNKNOWN"
                        else "Evidence cache replacement failed before commit."
                    ),
                ),
            ),
        )

    def _failed(
        self,
        *,
        action: str,
        status: str,
        code: str,
        message: str,
        checked_at: datetime | None = None,
    ) -> EvidenceStoreResult:
        return EvidenceStoreResult(
            success=False,
            status=status,
            action=action,
            purge_checked_at=checked_at,
            problems=(EvidenceStoreProblem(code=code, message=message),),
        )

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        self._validate_layout()
        flags = os.O_RDWR | os.O_CREAT | _O_CLOEXEC | _O_NOFOLLOW
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise EvidenceStoreError(
                "UNSAFE_LOCK_PATH",
                "Evidence lock could not be opened safely.",
            ) from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise EvidenceStoreError(
                    "UNSAFE_LOCK_PATH",
                    "Evidence lock path must be a regular file.",
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self._validate_layout()
            self._cleanup_orphan_temps_locked()
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _replace_ledger(
        self,
        ledger: EvidenceLedger,
        store_revision: str,
        store_epoch: str,
    ) -> _WriteOutcome:
        try:
            data = _encode_document(
                trip_id=self.trip_id,
                policies=self.policies,
                ledger=ledger,
                store_revision=store_revision,
                store_epoch=store_epoch,
            )
        except FactContractError:
            return _WriteOutcome(
                replaced=False,
                problem_code="CACHE_WRITE_FAILED",
            )
        return self._atomic_replace(data)

    def _atomic_replace(self, data: bytes) -> _WriteOutcome:
        temp_path: Path | None = None
        replaced = False
        replace_attempted = False
        try:
            self._fault("before_temp_write")
            descriptor, temp_name = tempfile.mkstemp(
                prefix=".trip-planner-evidence.",
                suffix=".tmp",
                dir=self.data_dir,
            )
            temp_path = Path(temp_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                output.write(data)
                output.flush()
                self._fault("after_temp_write")
                os.fsync(output.fileno())
                self._fault("after_temp_fsync")
            self._validate_cache_destination()
            self._fault("before_replace")
            replace_attempted = True
            os.replace(temp_path, self.cache_path)
            replaced = True
            temp_path = None
            self._fault("after_replace")
            self._fsync_directory(self.data_dir)
            self._fault("after_directory_fsync")
            return _WriteOutcome(replaced=True)
        except Exception:
            return _WriteOutcome(
                replaced=replaced or replace_attempted,
                problem_code=(
                    "CACHE_OUTCOME_UNKNOWN"
                    if replaced or replace_attempted
                    else "CACHE_WRITE_FAILED"
                ),
            )
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
                else:
                    try:
                        self._fsync_directory(self.data_dir)
                    except OSError:
                        # The operation already fails closed. A later lock
                        # owner also removes any temp entry that reappears.
                        pass

    def _cleanup_orphan_temps_locked(self) -> None:
        """Remove only reserved temp files after their writer lost the lock."""

        directory_descriptor = os.open(
            self.data_dir,
            os.O_RDONLY | _O_CLOEXEC | _O_DIRECTORY | _O_NOFOLLOW,
        )
        try:
            names = sorted(
                name
                for name in os.listdir(directory_descriptor)
                if _TEMP_NAME_RE.fullmatch(name)
            )
            if len(names) > _MAX_ORPHAN_TEMPS:
                raise EvidenceStoreError(
                    "ORPHAN_TEMP_LIMIT",
                    "Evidence temp cleanup exceeded its bounded namespace.",
                )
            for name in names:
                info = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(info.st_mode):
                    raise EvidenceStoreError(
                        "UNSAFE_EVIDENCE_PATH",
                        "Evidence temp path must be a regular file.",
                    )
            for name in names:
                os.unlink(name, dir_fd=directory_descriptor)
            if names:
                os.fsync(directory_descriptor)
        except EvidenceStoreError:
            raise
        except OSError as exc:
            raise EvidenceStoreError(
                "EVIDENCE_TEMP_CLEANUP_FAILED",
                "Evidence orphan temp cleanup failed closed.",
            ) from exc
        finally:
            os.close(directory_descriptor)

    def _read_regular_bytes(
        self,
        path: Path,
        *,
        required: bool,
    ) -> bytes | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            if required:
                raise EvidenceStoreError(
                    "MISSING_EVIDENCE_CACHE",
                    "Evidence cache does not exist.",
                )
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise EvidenceStoreError(
                "UNSAFE_EVIDENCE_PATH",
                "Evidence cache must be a non-symlink regular file.",
            )
        if info.st_size > _MAX_CACHE_BYTES:
            raise _OversizedEvidenceCache(
                self._oversized_corrupt_raw_digest(path, info.st_size)
            )
        descriptor = os.open(
            path,
            os.O_RDONLY | _O_CLOEXEC | _O_NOFOLLOW,
        )
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise EvidenceStoreError(
                    "UNSAFE_EVIDENCE_PATH",
                    "Opened evidence cache is not a regular file.",
                )
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_CACHE_BYTES:
                    raise _OversizedEvidenceCache(
                        _oversized_corrupt_raw_digest(
                            b"".join(chunks) + chunk,
                            opened.st_size,
                        )
                    )
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _oversized_corrupt_raw_digest(
        self,
        path: Path,
        expected_size: int,
    ) -> bytes:
        """Bind a bounded prefix and size without reading an unbounded file."""

        descriptor = os.open(
            path,
            os.O_RDONLY | _O_CLOEXEC | _O_NOFOLLOW,
        )
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise EvidenceStoreError(
                    "UNSAFE_EVIDENCE_PATH",
                    "Opened evidence cache is not a regular file.",
                )
            prefix = bytearray()
            remaining = _MAX_CACHE_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                prefix.extend(chunk)
                remaining -= len(chunk)
            return _oversized_corrupt_raw_digest(
                bytes(prefix),
                expected_size,
            )
        finally:
            os.close(descriptor)

    def _validate_layout(self) -> None:
        _require_directory(self.trip_dir, "trip directory")
        _require_directory(self.data_dir, "trip data directory")
        _require_optional_regular_file(
            self.cache_path, "evidence cache"
        )
        _require_optional_regular_file(self.lock_path, "trip lock")

    def _validate_cache_destination(self) -> None:
        _require_directory(self.data_dir, "trip data directory")
        _require_optional_regular_file(
            self.cache_path, "evidence cache"
        )

    def _fsync_directory(self, path: Path) -> None:
        descriptor = os.open(
            path,
            os.O_RDONLY | _O_CLOEXEC | _O_DIRECTORY | _O_NOFOLLOW,
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _fault(self, stage: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(stage)


def _prune_durable_evidence(
    ledger: EvidenceLedger,
    *,
    purge_now: datetime,
) -> EvidencePrune:
    """Apply retention even after the ABA generation counter saturates.

    At the signed 64-bit ceiling the store becomes promotion-terminal, but
    irreversible deletion may continue with the same generation.  The exact
    store revision still changes because it binds the remaining record set,
    and deletion alone cannot recreate an earlier state.
    """

    if ledger.generation < _MAX_GENERATION:
        return prune_evidence(ledger, purge_now=purge_now)
    checked_at = _aware_utc(purge_now, "purge_now")
    retained = tuple(
        observation
        for observation in ledger.observations
        if observation.retained_at(checked_at)
    )
    purged = tuple(
        observation.observation_id
        for observation in ledger.observations
        if not observation.retained_at(checked_at)
    )
    if not purged:
        return EvidencePrune(ledger=ledger, changed=False)
    return EvidencePrune(
        ledger=_restore_durable_evidence_ledger(
            policies=ledger.policies,
            observations=retained,
            generation=ledger.generation,
        ),
        changed=True,
        purged_observation_ids=purged,
    )


def _encode_document(
    *,
    trip_id: str,
    policies: ProviderPolicyRegistry,
    ledger: EvidenceLedger,
    store_revision: str,
    store_epoch: str,
) -> bytes:
    _require_digest_value(store_epoch, "store_epoch")
    if len(ledger.observations) > _MAX_RECORDS:
        raise FactContractError(
            "CACHE_WRITE_FAILED",
            "Evidence cache records exceed their bounded list contract.",
        )
    records: list[dict[str, Any]] = []
    for observation in ledger.observations:
        policy = policies.validate_observation(observation)
        if policy.persistence is EvidencePersistence.MEMORY_ONLY:
            raise FactContractError(
                "CACHE_CORRUPTED",
                "Memory-only evidence cannot enter the disk codec.",
            )
        records.append(
            {
                "policy_id": policy.policy_id,
                "policy_digest": policy.policy_digest,
                "observation": _encode_observation(observation),
            }
        )
    document = {
        "schema_version": EVIDENCE_STORE_VERSION,
        "trip_id": trip_id,
        "policy_registry_revision": policies.revision,
        "generation": ledger.generation,
        "records": records,
        "store_epoch": store_epoch,
        "store_revision": store_revision,
    }
    encoded = _canonical_json_bytes(document)
    if len(encoded) > _MAX_CACHE_BYTES:
        raise FactContractError(
            "CACHE_WRITE_FAILED",
            "Evidence cache exceeds its size limit.",
        )
    return encoded


def _decode_document_content(
    data: bytes,
    *,
    expected_trip_id: str,
    policies: ProviderPolicyRegistry,
) -> tuple[EvidenceLedger, str, str]:
    value = _decode_json_object(data)
    _exact_fields(
        value,
        {
            "schema_version",
            "trip_id",
            "policy_registry_revision",
            "generation",
            "records",
            "store_epoch",
            "store_revision",
        },
        "evidence cache",
    )
    if value["schema_version"] != EVIDENCE_STORE_VERSION:
        raise FactContractError(
            "CACHE_CORRUPTED",
            "Evidence cache schema version is unsupported.",
        )
    if value["trip_id"] != expected_trip_id:
        raise FactContractError(
            "CACHE_CORRUPTED",
            "Evidence cache belongs to a different trip.",
        )
    if value["policy_registry_revision"] != policies.revision:
        raise FactContractError(
            "CACHE_CORRUPTED",
            "Evidence cache policy registry is no longer current.",
        )
    generation = value["generation"]
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
        or generation > _MAX_GENERATION
    ):
        raise FactContractError(
            "CACHE_CORRUPTED",
            "Evidence cache generation must be non-negative.",
        )
    raw_records = value["records"]
    if (
        not isinstance(raw_records, list)
        or len(raw_records) > _MAX_RECORDS
    ):
        raise FactContractError(
            "CACHE_CORRUPTED",
            "Evidence cache records exceed their bounded list contract.",
        )
    observations: list[FactObservation] = []
    for index, raw_record in enumerate(raw_records):
        record = _require_object(raw_record, f"records[{index}]")
        _exact_fields(
            record,
            {"policy_id", "policy_digest", "observation"},
            f"records[{index}]",
        )
        policy_id = _require_text_value(
            record["policy_id"], f"records[{index}].policy_id"
        )
        policy_digest = _require_digest_value(
            record["policy_digest"],
            f"records[{index}].policy_digest",
        )
        policy = policies.policy(policy_id)
        if policy.policy_digest != policy_digest:
            raise FactContractError(
                "CACHE_CORRUPTED",
                "Evidence record policy digest is no longer current.",
            )
        if policy.persistence is EvidencePersistence.MEMORY_ONLY:
            raise FactContractError(
                "CACHE_CORRUPTED",
                "Memory-only provider content was found on disk.",
            )
        observation = _decode_observation(
            record["observation"],
            path=f"records[{index}].observation",
        )
        if observation.provenance.retention_policy_id != policy_id:
            raise FactContractError(
                "CACHE_CORRUPTED",
                "Evidence record wrapper and provenance policy disagree.",
            )
        policies.validate_observation(observation)
        observations.append(observation)
    ledger = _restore_durable_evidence_ledger(
        policies=policies,
        observations=tuple(observations),
        generation=generation,
    )
    stored_revision = _require_digest_value(
        value["store_revision"], "store_revision"
    )
    store_epoch = _require_digest_value(value["store_epoch"], "store_epoch")
    return ledger, stored_revision, store_epoch


def _decode_document(
    data: bytes,
    *,
    expected_trip_id: str,
    policies: ProviderPolicyRegistry,
) -> _StoredEvidence:
    ledger, stored_revision, store_epoch = _decode_document_content(
        data,
        expected_trip_id=expected_trip_id,
        policies=policies,
    )
    expected_revision = _store_revision(
        expected_trip_id,
        policies,
        ledger,
        store_epoch,
    )
    if stored_revision != expected_revision:
        raise FactContractError(
            "CACHE_CORRUPTED",
            "Evidence store revision does not match its records.",
        )
    if data != _encode_document(
        trip_id=expected_trip_id,
        policies=policies,
        ledger=ledger,
        store_revision=stored_revision,
        store_epoch=store_epoch,
    ):
        raise FactContractError(
            "CACHE_CORRUPTED",
            "Evidence cache is not in normalized canonical form.",
        )
    return _StoredEvidence(
        ledger=ledger,
        store_revision=stored_revision,
        store_epoch=store_epoch,
        existed=True,
    )


def _encode_observation(
    observation: FactObservation,
) -> dict[str, Any]:
    provenance = observation.provenance
    return {
        "contract_version": observation.contract_version,
        "observation_id": observation.observation_id,
        "key": observation.key.to_dict(),
        "value": {
            "kind": observation.value.kind.value,
            "schema_version": observation.value.schema_version,
            "payload": observation.value.payload,
            "value_digest": observation.value.value_digest,
        },
        "provenance": {
            "provider_id": provenance.provider_id,
            "adapter_id": provenance.adapter_id,
            "adapter_version": provenance.adapter_version,
            "request_fingerprint": provenance.request_fingerprint,
            "retention_policy_id": provenance.retention_policy_id,
            "provider_record_id": provenance.provider_record_id,
            "response_id": provenance.response_id,
            "source_uri": provenance.source_uri,
            "attributions": [
                {"label": label, "uri": uri}
                for label, uri in provenance.attributions
            ],
        },
        "retrieved_at": _utc_iso(observation.retrieved_at),
        "valid_until": _utc_iso(observation.valid_until),
        "purge_at": (
            _utc_iso(observation.purge_at)
            if observation.purge_at is not None
            else None
        ),
        "confidence": observation.confidence,
    }


def _decode_observation(value: Any, *, path: str) -> FactObservation:
    observation = _require_object(value, path)
    _exact_fields(
        observation,
        {
            "contract_version",
            "observation_id",
            "key",
            "value",
            "provenance",
            "retrieved_at",
            "valid_until",
            "purge_at",
            "confidence",
        },
        path,
    )
    if observation["contract_version"] != FACT_OBSERVATION_VERSION:
        raise FactContractError(
            "CACHE_CORRUPTED",
            "Stored observation version is unsupported.",
        )
    key_payload = _require_object(observation["key"], f"{path}.key")
    _exact_fields(
        key_payload,
        {
            "contract_version",
            "kind",
            "subject_ids",
            "qualifiers",
            "key_id",
        },
        f"{path}.key",
    )
    kind = _fact_kind(key_payload["kind"], f"{path}.key.kind")
    subject_ids = key_payload["subject_ids"]
    qualifiers = _require_object(
        key_payload["qualifiers"], f"{path}.key.qualifiers"
    )
    if not isinstance(subject_ids, list):
        raise FactContractError(
            "CACHE_CORRUPTED",
            "Stored fact key subjects must be a list.",
        )
    key = FactKey(
        kind=kind,
        subject_ids=tuple(subject_ids),
        qualifiers=tuple(qualifiers.items()),
        contract_version=_require_text_value(
            key_payload["contract_version"],
            f"{path}.key.contract_version",
        ),
        key_id=_require_digest_value(
            key_payload["key_id"], f"{path}.key.key_id"
        ),
    )

    value_payload = _require_object(
        observation["value"], f"{path}.value"
    )
    _exact_fields(
        value_payload,
        {"kind", "schema_version", "payload", "value_digest"},
        f"{path}.value",
    )
    value_kind = _fact_kind(
        value_payload["kind"], f"{path}.value.kind"
    )
    normalized_value = FactValue.from_payload(
        value_kind,
        _require_object(
            value_payload["payload"], f"{path}.value.payload"
        ),
    )
    if (
        value_payload["schema_version"]
        != normalized_value.schema_version
        or value_payload["value_digest"]
        != normalized_value.value_digest
    ):
        raise FactContractError(
            "CACHE_CORRUPTED",
            "Stored fact value digest or schema does not match.",
        )

    raw_provenance = _require_object(
        observation["provenance"], f"{path}.provenance"
    )
    _exact_fields(
        raw_provenance,
        {
            "provider_id",
            "adapter_id",
            "adapter_version",
            "request_fingerprint",
            "retention_policy_id",
            "provider_record_id",
            "response_id",
            "source_uri",
            "attributions",
        },
        f"{path}.provenance",
    )
    raw_attributions = raw_provenance["attributions"]
    if not isinstance(raw_attributions, list):
        raise FactContractError(
            "CACHE_CORRUPTED",
            "Stored attributions must be a list.",
        )
    attributions: list[tuple[str, str | None]] = []
    for index, raw_attribution in enumerate(raw_attributions):
        attribution = _require_object(
            raw_attribution,
            f"{path}.provenance.attributions[{index}]",
        )
        _exact_fields(
            attribution,
            {"label", "uri"},
            f"{path}.provenance.attributions[{index}]",
        )
        label = _require_text_value(
            attribution["label"],
            f"{path}.provenance.attributions[{index}].label",
        )
        uri = attribution["uri"]
        if uri is not None and not isinstance(uri, str):
            raise FactContractError(
                "CACHE_CORRUPTED",
                "Stored attribution URI must be text or null.",
            )
        attributions.append((label, uri))
    provenance = ProviderProvenance(
        provider_id=_require_text_value(
            raw_provenance["provider_id"],
            f"{path}.provenance.provider_id",
        ),
        adapter_id=_require_text_value(
            raw_provenance["adapter_id"],
            f"{path}.provenance.adapter_id",
        ),
        adapter_version=_require_text_value(
            raw_provenance["adapter_version"],
            f"{path}.provenance.adapter_version",
        ),
        request_fingerprint=_require_digest_value(
            raw_provenance["request_fingerprint"],
            f"{path}.provenance.request_fingerprint",
        ),
        retention_policy_id=_require_text_value(
            raw_provenance["retention_policy_id"],
            f"{path}.provenance.retention_policy_id",
        ),
        provider_record_id=_optional_text_value(
            raw_provenance["provider_record_id"],
            f"{path}.provenance.provider_record_id",
        ),
        response_id=_optional_text_value(
            raw_provenance["response_id"],
            f"{path}.provenance.response_id",
        ),
        source_uri=_optional_text_value(
            raw_provenance["source_uri"],
            f"{path}.provenance.source_uri",
        ),
        attributions=tuple(attributions),
    )
    purge_at_value = observation["purge_at"]
    purge_at = (
        None
        if purge_at_value is None
        else _parse_datetime_value(purge_at_value, f"{path}.purge_at")
    )
    return FactObservation(
        key=key,
        value=normalized_value,
        provenance=provenance,
        retrieved_at=_parse_datetime_value(
            observation["retrieved_at"], f"{path}.retrieved_at"
        ),
        valid_until=_parse_datetime_value(
            observation["valid_until"], f"{path}.valid_until"
        ),
        purge_at=purge_at,
        confidence=observation["confidence"],
        contract_version=observation["contract_version"],
        observation_id=_require_digest_value(
            observation["observation_id"], f"{path}.observation_id"
        ),
    )


def _initial_store_epoch(
    trip_id: str,
    policies: ProviderPolicyRegistry,
) -> str:
    return hashlib.sha256(
        b"trip-planner.evidence-store.initial-epoch/v2\0"
        + _canonical_json_bytes(
            {
                "trip_id": trip_id,
                "policy_registry_revision": policies.revision,
            }
        )
    ).hexdigest()


def _secure_reset_nonce() -> bytes:
    return secrets.token_bytes(_RESET_NONCE_BYTES)


def _reset_store_epoch(
    *,
    domain: bytes,
    raw_digest: bytes,
    nonce: bytes,
) -> str:
    if domain not in (_CORRUPT_RESET_DOMAIN, _OVERSIZED_RESET_DOMAIN):
        raise ValueError("Corrupt reset domain is not recognized.")
    if type(raw_digest) is not bytes or len(raw_digest) != 32:
        raise ValueError(
            "Corrupt reset raw digest must contain exactly 256 bits."
        )
    if type(nonce) is not bytes or len(nonce) != _RESET_NONCE_BYTES:
        raise ValueError(
            "Corrupt reset nonce must contain exactly 256 bits."
        )
    return hashlib.sha256(domain + nonce + raw_digest).hexdigest()


def _corrupt_store_epoch(data: bytes, nonce: bytes) -> str:
    return _reset_store_epoch(
        domain=_CORRUPT_RESET_DOMAIN,
        raw_digest=hashlib.sha256(data).digest(),
        nonce=nonce,
    )


def _oversized_corrupt_raw_digest(
    prefix: bytes,
    size: int,
) -> bytes:
    return hashlib.sha256(
        b"trip-planner.evidence-store.oversized-raw/v3\0"
        + str(size).encode("ascii")
        + b"\0"
        + prefix
    ).digest()


def _oversized_corrupt_store_epoch(
    prefix: bytes,
    size: int,
    nonce: bytes,
) -> str:
    return _reset_store_epoch(
        domain=_OVERSIZED_RESET_DOMAIN,
        raw_digest=_oversized_corrupt_raw_digest(prefix, size),
        nonce=nonce,
    )


def _probe_corrupt_document(data: bytes) -> dict[str, Any] | None:
    """Read only unambiguous envelope metadata; never salvage record values."""

    try:
        return _decode_json_object(data)
    except FactContractError:
        return None


def _probe_store_revision(
    document: Mapping[str, Any] | None,
) -> str | None:
    if document is None:
        return None
    value = document.get("store_revision")
    if isinstance(value, str) and _DIGEST_RE.fullmatch(value):
        return value
    return None


def _store_revision(
    trip_id: str,
    policies: ProviderPolicyRegistry,
    ledger: EvidenceLedger,
    store_epoch: str,
) -> str:
    _require_digest_value(store_epoch, "store_epoch")
    payload = {
        "schema_version": EVIDENCE_STORE_VERSION,
        "trip_id": trip_id,
        "policy_registry_revision": policies.revision,
        "store_epoch": store_epoch,
        "generation": ledger.generation,
        "records": [
            {
                "observation_id": observation.observation_id,
                "policy_id": (
                    observation.provenance.retention_policy_id
                ),
                "policy_digest": policies.policy(
                    observation.provenance.retention_policy_id
                ).policy_digest,
            }
            for observation in ledger.observations
        ],
    }
    return hashlib.sha256(
        b"trip-planner.evidence-store/v2\0"
        + _canonical_json_bytes(payload)
    ).hexdigest()


def _decode_json_object(data: bytes) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise FactContractError(
                    "CACHE_CORRUPTED",
                    "Evidence cache contains duplicate JSON keys.",
                )
            result[key] = value
        return result

    try:
        decoded = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=lambda _value: (_raise_cache_json_error()),
        )
    except FactContractError:
        raise
    except (
        UnicodeDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise FactContractError(
            "CACHE_CORRUPTED",
            "Evidence cache is not strict UTF-8 JSON.",
        ) from exc
    if not isinstance(decoded, dict):
        raise FactContractError(
            "CACHE_CORRUPTED",
            "Evidence cache root must be an object.",
        )
    return decoded


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise FactContractError(
            "CACHE_CORRUPTED",
            "Evidence cache value is not strict JSON.",
        ) from exc


def _raise_cache_json_error() -> None:
    raise FactContractError(
        "CACHE_CORRUPTED",
        "Evidence cache contains a non-finite JSON number.",
    )


def _exact_fields(
    value: Mapping[str, Any],
    expected: set[str],
    path: str,
) -> None:
    if set(value) != expected:
        raise FactContractError(
            "CACHE_CORRUPTED",
            f"{path} has an invalid field set.",
        )


def _require_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FactContractError(
            "CACHE_CORRUPTED",
            f"{path} must be an object.",
        )
    return value


def _require_text_value(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise FactContractError(
            "CACHE_CORRUPTED",
            f"{path} must be text.",
        )
    return value


def _optional_text_value(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _require_text_value(value, path)


def _require_digest_value(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise FactContractError(
            "CACHE_CORRUPTED",
            f"{path} must be a lowercase SHA-256 digest.",
        )
    return value


def _fact_kind(value: Any, path: str) -> FactKind:
    if not isinstance(value, str):
        raise FactContractError(
            "CACHE_CORRUPTED",
            f"{path} must be a fact kind.",
        )
    try:
        return FactKind(value)
    except ValueError as exc:
        raise FactContractError(
            "CACHE_CORRUPTED",
            f"{path} is unsupported.",
        ) from exc


def _parse_datetime_value(value: Any, path: str) -> datetime:
    if (
        not isinstance(value, str)
        or not _CANONICAL_UTC_TIMESTAMP_RE.fullmatch(value)
    ):
        raise FactContractError(
            "CACHE_CORRUPTED",
            f"{path} must be a canonical UTC timestamp.",
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except (OverflowError, ValueError) as exc:
        raise FactContractError(
            "CACHE_CORRUPTED",
            f"{path} must be a canonical UTC timestamp.",
        ) from exc
    normalized = parsed.astimezone(timezone.utc)
    if _utc_iso(normalized) != value:
        raise FactContractError(
            "CACHE_CORRUPTED",
            f"{path} must use the normalized UTC form.",
        )
    return normalized


def _aware_utc(value: Any, name: str) -> datetime:
    try:
        valid = (
            isinstance(value, datetime)
            and value.tzinfo is not None
            and value.utcoffset() is not None
        )
        normalized = value.astimezone(timezone.utc) if valid else None
    except (OverflowError, ValueError) as exc:
        raise EvidenceStoreError(
            "INVALID_TRUSTED_CLOCK",
            f"{name} is outside the supported UTC range.",
        ) from exc
    if not valid or normalized is None:
        raise EvidenceStoreError(
            "INVALID_TRUSTED_CLOCK",
            f"{name} must be a timezone-aware datetime.",
        )
    return normalized


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _validate_slug(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) > _MAX_SLUG_LENGTH
        or not _SLUG_RE.fullmatch(value)
    ):
        raise EvidenceStoreError(
            "INVALID_SLUG",
            "Evidence trip slug must be a lowercase path-safe slug.",
        )
    return value


def _require_visible_text(
    value: Any,
    name: str,
    maximum: int,
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
            f"{name} must be visible text of at most {maximum} characters"
        )


def _require_directory(
    path: Path,
    name: str,
    *,
    allow_symlink_target: bool = False,
) -> None:
    try:
        info = path.stat() if allow_symlink_target else path.lstat()
    except OSError as exc:
        raise EvidenceStoreError(
            "UNSAFE_EVIDENCE_PATH",
            f"{name} is unavailable.",
        ) from exc
    if (
        (not allow_symlink_target and stat.S_ISLNK(info.st_mode))
        or not stat.S_ISDIR(info.st_mode)
    ):
        raise EvidenceStoreError(
            "UNSAFE_EVIDENCE_PATH",
            f"{name} must be a non-symlink directory.",
        )


def _require_optional_regular_file(path: Path, name: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise EvidenceStoreError(
            "UNSAFE_EVIDENCE_PATH",
            f"{name} is unavailable.",
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise EvidenceStoreError(
            "UNSAFE_EVIDENCE_PATH",
            f"{name} must be a non-symlink regular file.",
        )


__all__ = [
    "EVIDENCE_STORE_VERSION",
    "EvidenceStore",
    "EvidenceStoreError",
    "EvidenceStoreProblem",
    "EvidenceStoreResult",
]
