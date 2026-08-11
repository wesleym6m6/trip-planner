"""Run-scoped evidence boundary for memory-only provider facts.

The durable :class:`EvidenceStore` owns only policy-authorized disk records.
This session reloads that exact durable source to detect drift, then merges
restricted provider values in memory.  It intentionally has no filesystem
path and no write-through behavior.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .evidence_store import EvidenceStore, EvidenceStoreResult
from .facts import (
    AuthorizedProviderResult,
    EvidenceLedger,
    EvidencePersistence,
    EvidenceSnapshot,
    EVIDENCE_SNAPSHOT_VERSION,
    FactContractError,
    FactKey,
    FactKind,
    FactObservation,
    FactValue,
    ProviderPolicy,
    ProviderPolicyRegistry,
    ProviderProblem,
    ProviderProblemCode,
    ProviderProvenance,
    _digest as _fact_digest,
    merge_provider_result,
    prune_evidence,
)
from .places_identity import _digest as _place_identity_digest


Clock = Callable[[], datetime]

EVIDENCE_SESSION_DELIVERY_SOURCE_VERSION = (
    "evidence-session-delivery-source/v1"
)

_DELIVERY_SOURCE_TOKEN = object()
_PATH_TYPE = type(Path("."))
_RLOCK_TYPE = type(threading.RLock())
_MAX_DELIVERY_OBSERVATIONS = 4096
_MAX_DELIVERY_EVIDENCE_BYTES = 16 * 1024 * 1024
_MAX_DELIVERY_POLICIES = 256
_MAX_DELIVERY_PROBLEMS = 4096
_MAX_DELIVERY_TEXT_CHARS = 4096
_MAX_DELIVERY_SESSION_FIELDS = 32
_MAX_DELIVERY_SOURCE_FIELDS = 64
_MAX_DELIVERY_CLOCK_FIELDS = 16
_EVIDENCE_STORE_READ_SNAPSHOT = EvidenceStore.read_snapshot


class DurableEvidenceSource(Protocol):
    """Reloadable durable source used to detect review-time drift."""

    def load(self) -> EvidenceStoreResult:
        """Return one exact current EvidenceStore result."""


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Any, *, prefix: str) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(prefix.encode("utf-8") + b"\n" + encoded).hexdigest()


class _SessionClock:
    """Trusted UTC clock that cannot move behind its durable seed."""

    def __init__(self, source: Clock | None, *, floor: datetime) -> None:
        if source is not None and not callable(source):
            raise TypeError("clock must be callable or None")
        self._source = source
        self._anchor_wall = datetime.now(timezone.utc)
        self._anchor_monotonic = time.monotonic()
        self._last = _utc(floor, "clock floor")

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
        return self.observe(candidate)

    def observe(self, value: datetime) -> datetime:
        checked = _utc(value, "trusted evidence session clock")
        if checked < self._last:
            checked = self._last
        self._last = checked
        return checked

    def high_water(self) -> datetime:
        """Return the current floor without sampling or advancing the clock."""

        return self._last


@dataclass(frozen=True, slots=True)
class EvidenceSessionLoad:
    """One immutable, redacted load from a run-scoped evidence session."""

    ledger: EvidenceLedger = field(repr=False)
    store_revision: str
    purge_checked_at: datetime
    provider_problems: tuple[ProviderProblem, ...] = field(
        default=(),
        repr=False,
    )
    outcome_revision: str = ""

    def __post_init__(self) -> None:
        if type(self.ledger) is not EvidenceLedger:
            raise TypeError("EvidenceSessionLoad.ledger must be EvidenceLedger")
        if (
            not isinstance(self.store_revision, str)
            or len(self.store_revision) != 64
            or any(char not in "0123456789abcdef" for char in self.store_revision)
        ):
            raise ValueError("EvidenceSessionLoad.store_revision must be a digest")
        checked_at = _utc(
            self.purge_checked_at,
            "EvidenceSessionLoad.purge_checked_at",
        )
        if not isinstance(self.provider_problems, tuple) or any(
            type(item) is not ProviderProblem for item in self.provider_problems
        ):
            raise TypeError("provider_problems must contain exact values")
        ordered = tuple(
            sorted(
                self.provider_problems,
                key=lambda item: (
                    item.fact_key_ids,
                    item.code.value,
                    item.next_action,
                    item.message,
                ),
            )
        )
        expected_outcome = _digest(
            [item.to_dict() for item in ordered],
            prefix="evidence-session-outcomes",
        )
        if self.outcome_revision and self.outcome_revision != expected_outcome:
            raise ValueError(
                "EvidenceSessionLoad.outcome_revision does not match outcomes"
            )
        object.__setattr__(self, "purge_checked_at", checked_at)
        object.__setattr__(self, "provider_problems", ordered)
        object.__setattr__(self, "outcome_revision", expected_outcome)

    def snapshot(self, *, evaluation_at: datetime) -> EvidenceSnapshot:
        """Pin current durable and memory evidence to one evaluation time."""

        return EvidenceSnapshot.from_ledger(
            self.ledger,
            evaluation_at=evaluation_at,
            purge_now=self.purge_checked_at,
            store_revision=self.store_revision,
            outcome_revision=self.outcome_revision,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "store_revision": self.store_revision,
            "ledger_revision": self.ledger.revision,
            "purge_checked_at": _utc_iso(self.purge_checked_at),
            "observation_count": len(self.ledger.observations),
            "provider_problems": [
                item.to_dict() for item in self.provider_problems
            ],
            "outcome_revision": self.outcome_revision,
        }


@dataclass(frozen=True, slots=True)
class EvidenceSessionMerge:
    """One memory-only merge result and its current immutable load."""

    current: EvidenceSessionLoad
    changed: bool
    purged_observation_ids: tuple[str, ...] = ()
    promoted_observation_ids: tuple[str, ...] = ()
    ignored_observation_ids: tuple[str, ...] = ()
    problems: tuple[ProviderProblem, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if type(self.current) is not EvidenceSessionLoad:
            raise TypeError(
                "EvidenceSessionMerge.current must be EvidenceSessionLoad"
            )
        if not isinstance(self.changed, bool):
            raise TypeError("EvidenceSessionMerge.changed must be bool")
        for value, name in (
            (self.purged_observation_ids, "purged_observation_ids"),
            (self.promoted_observation_ids, "promoted_observation_ids"),
            (self.ignored_observation_ids, "ignored_observation_ids"),
        ):
            if not isinstance(value, tuple):
                raise TypeError(f"EvidenceSessionMerge.{name} must be a tuple")
            for observation_id in value:
                if (
                    not isinstance(observation_id, str)
                    or len(observation_id) != 64
                    or any(
                        char not in "0123456789abcdef"
                        for char in observation_id
                    )
                ):
                    raise ValueError(
                        f"EvidenceSessionMerge.{name} contains an invalid ID"
                    )
            object.__setattr__(self, name, tuple(sorted(set(value))))
        if not isinstance(self.problems, tuple) or any(
            type(item) is not ProviderProblem for item in self.problems
        ):
            raise TypeError(
                "EvidenceSessionMerge.problems must contain exact values"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current.to_dict(),
            "changed": self.changed,
            "purged_observation_ids": list(self.purged_observation_ids),
            "promoted_observation_ids": list(self.promoted_observation_ids),
            "ignored_observation_ids": list(self.ignored_observation_ids),
            "problems": [item.to_dict() for item in self.problems],
        }


class EvidenceSession:
    """Thread-safe memory-only evidence source for one planning run."""

    def __init__(
        self,
        source: DurableEvidenceSource,
        *,
        clock: Clock | None = None,
    ) -> None:
        loader = getattr(source, "load", None)
        if not callable(loader):
            raise TypeError(
                "EvidenceSession requires a reloadable durable evidence source"
            )
        base = loader()
        self._validate_store_result(base)
        if (
            base.ledger is None
            or base.current_revision is None
            or base.purge_checked_at is None
        ):
            raise ValueError(
                "EvidenceSession requires one successful current store load"
            )
        self._validate_durable_ledger(base.ledger)
        self._lock = threading.RLock()
        self._source = source
        self._ledger = base.ledger
        self._store_revision = base.current_revision
        self._clock = _SessionClock(clock, floor=base.purge_checked_at)
        self._problems: dict[tuple[Any, ...], ProviderProblem] = {}
        self._problem_ids_by_key: dict[
            str, set[tuple[Any, ...]]
        ] = {}
        self._problem_scope_by_id: dict[
            tuple[Any, ...], tuple[str, ...]
        ] = {}
        self._global_problem_ids: set[tuple[Any, ...]] = set()
        self._accepted_place_details_bases: set[
            tuple[str, str, str]
        ] = set()
        self._seed_store_problems(base.provider_problems)

    def load(self) -> EvidenceSessionLoad:
        """Return current evidence after trusted-clock retention pruning."""

        with self._lock:
            checked_at = self._refresh_durable()
            pruned = prune_evidence(self._ledger, purge_now=checked_at)
            self._ledger = pruned.ledger
            return self._load_at(checked_at)

    def private_delivery_source(self) -> "EvidenceSessionDeliverySource":
        """Return a sealed, non-mutating private-delivery snapshot adapter.

        Only an exact :class:`EvidenceStore` backing can establish the path and
        durable-source identity required by the private-delivery writer.  The
        adapter never calls :meth:`EvidenceStore.load`, refreshes a provider,
        rebases this session, or persists retention changes.
        """

        if type(self) is not EvidenceSession:
            raise FactContractError(
                "EVIDENCE_DELIVERY_SOURCE_UNAVAILABLE",
                "The delivery source host session is unavailable.",
            )
        lock = _validated_delivery_session_lock(self)
        with lock:
            try:
                source = object.__getattribute__(self, "_source")
            except Exception:
                raise FactContractError(
                    "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
                    "The delivery source host session is not intact.",
                ) from None
            if type(source) is not EvidenceStore:
                raise FactContractError(
                    "EVIDENCE_DELIVERY_SOURCE_UNAVAILABLE",
                    (
                        "Private delivery requires an exact read-only "
                        "EvidenceStore-backed session."
                    ),
                )
            return EvidenceSessionDeliverySource(
                session=self,
                source=source,
                _token=_DELIVERY_SOURCE_TOKEN,
            )

    def merge(
        self,
        authorized_result: AuthorizedProviderResult,
    ) -> EvidenceSessionMerge:
        """Merge one authorized memory-only result without disk writes."""

        if type(authorized_result) is not AuthorizedProviderResult:
            raise TypeError(
                "authorized_result must be AuthorizedProviderResult"
            )
        with self._lock:
            checked_at = self._refresh_durable()
            policy = self._ledger.policies.policy(
                authorized_result.request.policy_id
            )
            if policy.persistence is not EvidencePersistence.MEMORY_ONLY:
                raise FactContractError(
                    "UNTRUSTED_PROVENANCE",
                    (
                        "EvidenceSession accepts only memory-only provider "
                        "results; durable evidence belongs to EvidenceStore."
                    ),
                )
            place_details_basis = (
                self._validate_current_google_runtime_basis(
                    authorized_result,
                    checked_at=checked_at,
                )
            )
            checked_at = self._clock.now()
            merged = merge_provider_result(
                self._ledger,
                authorized_result,
                purge_now=checked_at,
            )
            self._ledger = merged.ledger
            if place_details_basis is not None:
                self._accepted_place_details_bases.add(
                    place_details_basis
                )
            self._record_outcomes(
                authorized_result,
                merged.problems,
            )
            return EvidenceSessionMerge(
                current=self._load_at(checked_at),
                changed=merged.changed,
                purged_observation_ids=merged.purged_observation_ids,
                promoted_observation_ids=merged.promoted_observation_ids,
                ignored_observation_ids=merged.ignored_observation_ids,
                problems=merged.problems,
            )

    def _load_at(self, checked_at: datetime) -> EvidenceSessionLoad:
        return EvidenceSessionLoad(
            ledger=self._ledger,
            store_revision=self._store_revision,
            purge_checked_at=checked_at,
            provider_problems=self._current_problems(),
        )

    def _read_private_delivery_snapshot(
        self,
        adapter: "EvidenceSessionDeliverySource",
        *,
        evaluation_at: datetime,
    ) -> EvidenceSnapshot:
        """Reproduce the current run-scoped view without mutating it."""

        checked_at = _delivery_utc(evaluation_at)
        lock = _validated_delivery_session_lock(self, adapter=adapter)
        with lock:
            try:
                _verify_delivery_source(adapter, session=self)
                if type(self._clock) is not _SessionClock:
                    raise FactContractError(
                        "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
                        "The delivery source clock is not intact.",
                    )
                clock_floor = _delivery_utc(
                    _SessionClock.high_water(self._clock)
                )
                if checked_at < clock_floor:
                    raise FactContractError(
                        "EVIDENCE_DELIVERY_SOURCE_CLOCK_ROLLBACK",
                        "The delivery snapshot time predates the session state.",
                    )
                source = adapter._source
                ledger_before = _validate_delivery_ledger(self._ledger)
                store_revision_before = self._store_revision
                if not _delivery_digest(store_revision_before):
                    raise FactContractError(
                        "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
                        "The run-scoped evidence source is not intact.",
                    )

                # Call the exact class implementation so an instance-level
                # monkeypatch cannot turn this read boundary into an arbitrary
                # callback.  EvidenceStore.read_snapshot itself is bounded and
                # no-follow and performs no application-level durable writes.
                durable = _EVIDENCE_STORE_READ_SNAPSHOT(
                    source,
                    evaluation_at=checked_at,
                )
                durable = _validate_delivery_snapshot(
                    durable,
                    expected_policies=ledger_before.policies,
                )
                retained = prune_evidence(
                    ledger_before,
                    purge_now=checked_at,
                ).ledger
                durable_observations = tuple(
                    observation
                    for observation in retained.observations
                    if retained.policies.persistence_for(observation)
                    is not EvidencePersistence.MEMORY_ONLY
                )
                if (
                    durable.policies is not retained.policies
                    or durable.store_revision != store_revision_before
                    or durable.evaluation_at != checked_at
                    or durable.purge_checked_at != checked_at
                    or durable.outcome_revision is not None
                    or durable.observations != durable_observations
                ):
                    raise FactContractError(
                        "EVIDENCE_DELIVERY_SOURCE_STALE",
                        "The durable evidence source changed.",
                    )
                problems = _validated_delivery_problems(self)
                outcome_revision = _digest(
                    [item.to_dict() for item in problems],
                    prefix="evidence-session-outcomes",
                )
                snapshot = EvidenceSnapshot.from_ledger(
                    retained,
                    evaluation_at=checked_at,
                    purge_now=checked_at,
                    store_revision=store_revision_before,
                    outcome_revision=outcome_revision,
                )
                if (
                    self._ledger is not ledger_before
                    or self._store_revision != store_revision_before
                ):
                    raise FactContractError(
                        "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
                        "The run-scoped evidence source changed during read.",
                    )
                _verify_delivery_source(adapter, session=self)
                return snapshot
            except FactContractError as exc:
                if exc.code.startswith("EVIDENCE_DELIVERY_SOURCE_"):
                    raise
                raise FactContractError(
                    "EVIDENCE_DELIVERY_SOURCE_UNAVAILABLE",
                    "The run-scoped evidence snapshot is unavailable.",
                ) from None
            except Exception:
                raise FactContractError(
                    "EVIDENCE_DELIVERY_SOURCE_UNAVAILABLE",
                    "The run-scoped evidence snapshot is unavailable.",
                ) from None

    def _record_outcomes(
        self,
        authorized_result: AuthorizedProviderResult,
        merged_problems: tuple[ProviderProblem, ...],
    ) -> None:
        requested = authorized_result.request.requested_key_ids
        superseded: set[tuple[Any, ...]] = set()
        for key_id in requested:
            for problem_id in self._problem_ids_by_key.get(
                key_id, set()
            ):
                scope = self._problem_scope_by_id[problem_id]
                if set(scope).issubset(requested):
                    superseded.add(problem_id)
        for problem_id in superseded:
            self._remove_problem(problem_id)
        for problem in merged_problems:
            self._add_problem(
                problem,
                supersession_scope=(
                    problem.fact_key_ids or requested
                ),
            )

    def _refresh_durable(self) -> datetime:
        current = self._source.load()
        self._validate_store_result(current)
        if (
            current.ledger is None
            or current.current_revision is None
            or current.purge_checked_at is None
        ):
            raise FactContractError(
                "EVIDENCE_REVISION_CHANGED",
                "The durable evidence source is unavailable.",
            )
        self._validate_durable_ledger(current.ledger)
        checked_at = self._clock.observe(current.purge_checked_at)
        if (
            current.ledger.policies.revision
            != self._ledger.policies.revision
        ):
            raise FactContractError(
                "EVIDENCE_REVISION_CHANGED",
                "The durable evidence policy registry changed.",
            )
        if current.current_revision == self._store_revision:
            current_durable_ids = {
                item.observation_id
                for item in self._ledger.observations
                if self._ledger.policies.persistence_for(item)
                is not EvidencePersistence.MEMORY_ONLY
            }
            loaded_ids = {
                item.observation_id for item in current.ledger.observations
            }
            if current_durable_ids != loaded_ids:
                raise FactContractError(
                    "CACHE_CORRUPTED",
                    (
                        "One durable store revision returned different "
                        "evidence records."
                    ),
                )
            return max(checked_at, self._clock.now())

        # Any durable revision drift invalidates all endpoint-bound run facts.
        # This conservative rebase prevents an old Place ID from retaining a
        # route after another process refreshes durable identity.
        self._ledger = current.ledger
        self._store_revision = current.current_revision
        self._problems.clear()
        self._problem_ids_by_key.clear()
        self._problem_scope_by_id.clear()
        self._global_problem_ids.clear()
        self._accepted_place_details_bases.clear()
        self._seed_store_problems(current.provider_problems)
        return max(checked_at, self._clock.now())

    def _validate_current_google_runtime_basis(
        self,
        authorized_result: AuthorizedProviderResult,
        *,
        checked_at: datetime,
    ) -> tuple[str, str, str] | None:
        request = authorized_result.request
        if request.provider_id == "google-routes" and (
            request.policy_id == "google-route-runtime-v1"
        ):
            self._validate_current_route_basis(authorized_result)
            return None
        if request.provider_id == "google-places" and request.policy_id in {
            "google-place-profile-runtime-v1",
            "google-place-hours-runtime-v1",
        }:
            return self._validate_current_place_details_basis(
                authorized_result,
                checked_at=checked_at,
            )
        return None

    def _validate_current_route_basis(
        self,
        authorized_result: AuthorizedProviderResult,
    ) -> None:
        request = authorized_result.request
        if (
            request.provider_id != "google-routes"
            or request.policy_id != "google-route-runtime-v1"
        ):
            return
        scope = dict(request.query_scope)
        if scope.get("basis_store_revision") != self._store_revision:
            raise FactContractError(
                "EVIDENCE_REVISION_CHANGED",
                (
                    "Google route durable evidence changed before the "
                    "memory-only result was merged."
                ),
            )
        endpoint_observations = {
            scope.get("origin_observation_id"),
            scope.get("destination_observation_id"),
        }
        active_by_id = {
            item.observation_id: item
            for item in self._ledger.observations
        }
        if (
            None in endpoint_observations
            or not endpoint_observations.issubset(active_by_id)
        ):
            raise FactContractError(
                "EVIDENCE_REVISION_CHANGED",
                (
                    "Google route endpoint identity changed before the "
                    "memory-only result was merged."
                ),
            )
        key = request.fact_keys[0]
        for index, prefix in enumerate(("origin", "destination")):
            observation = active_by_id[scope[f"{prefix}_observation_id"]]
            if (
                observation.key.kind is not FactKind.PLACE_IDENTITY
                or observation.key.subject_ids != (key.subject_ids[index],)
                or observation.provenance.provider_id != "google-places"
                or observation.value.value_digest
                != scope[f"{prefix}_value_digest"]
            ):
                raise FactContractError(
                    "EVIDENCE_REVISION_CHANGED",
                    (
                        "Google route endpoint identity binding changed before "
                        "the memory-only result was merged."
                    ),
                )

    def _validate_current_place_details_basis(
        self,
        authorized_result: AuthorizedProviderResult,
        *,
        checked_at: datetime,
    ) -> tuple[str, str, str]:
        request = authorized_result.request
        scope = dict(request.query_scope)
        if scope.get("basis_store_revision") != self._store_revision:
            raise FactContractError(
                "EVIDENCE_REVISION_CHANGED",
                (
                    "Google Place Details durable evidence changed before the "
                    "memory-only result was merged."
                ),
            )
        basis = (
            self._store_revision,
            str(scope.get("basis_evidence_revision")),
            str(scope.get("basis_snapshot_id")),
        )
        active_observation_ids = [
            item.observation_id
            for item in self._ledger.observations
            if item.retained_at(checked_at)
        ]
        current_evidence_revision = _fact_digest(
            {"observation_ids": active_observation_ids},
            prefix="active-evidence",
        )
        if (
            basis[1] != current_evidence_revision
            and basis not in self._accepted_place_details_bases
        ):
            raise FactContractError(
                "EVIDENCE_REVISION_CHANGED",
                (
                    "Google Place Details evidence changed before the "
                    "memory-only result was merged."
                ),
            )
        observation_id = scope.get("identity_observation_id")
        identity = next(
            (
                item
                for item in self._ledger.observations
                if item.observation_id == observation_id
            ),
            None,
        )
        key = request.fact_keys[0]
        expected_endpoint_id = None
        if identity is not None:
            expected_endpoint_id = _place_identity_digest(
                {
                    "location_id": key.subject_ids[0],
                    "provider_id": "google-places",
                    "provider_place_id": identity.value.payload.get(
                        "provider_place_id"
                    ),
                    "observation_id": identity.observation_id,
                    "value_digest": identity.value.value_digest,
                    "valid_until": _utc_iso(identity.valid_until),
                    "snapshot_id": scope.get("basis_snapshot_id"),
                },
                prefix="place-endpoint-identity",
            )
        if (
            identity is None
            or identity.key.kind is not FactKind.PLACE_IDENTITY
            or identity.key.subject_ids != key.subject_ids
            or identity.provenance.provider_id != "google-places"
            or identity.value.value_digest
            != scope.get("identity_value_digest")
            or identity.value.payload.get("provider_place_id")
            != key.qualifier_map.get("provider_place_id")
            or not identity.fresh_at(checked_at)
            or expected_endpoint_id != scope.get("identity_endpoint_id")
        ):
            raise FactContractError(
                "EVIDENCE_REVISION_CHANGED",
                (
                    "Google Place Details identity changed before the "
                    "memory-only result was merged."
                ),
            )
        return basis

    def _current_problems(self) -> tuple[ProviderProblem, ...]:
        return tuple(
            sorted(
                self._problems.values(),
                key=lambda item: (
                    item.fact_key_ids,
                    item.code.value,
                    item.next_action,
                    item.message,
                ),
            )
        )

    def _seed_store_problems(
        self,
        problems: tuple[ProviderProblem, ...],
    ) -> None:
        for problem in problems:
            self._add_problem(
                problem,
                supersession_scope=problem.fact_key_ids,
            )

    def _add_problem(
        self,
        problem: ProviderProblem,
        *,
        supersession_scope: tuple[str, ...],
    ) -> None:
        normalized_scope = tuple(sorted(set(supersession_scope)))
        problem_id = (
            *self._problem_identity(problem),
            "supersession_scope",
            normalized_scope,
        )
        self._problems[problem_id] = problem
        self._problem_scope_by_id[problem_id] = normalized_scope
        if normalized_scope:
            for key_id in normalized_scope:
                self._problem_ids_by_key.setdefault(
                    key_id, set()
                ).add(problem_id)
        else:
            self._global_problem_ids.add(problem_id)

    def _remove_problem(self, problem_id: tuple[Any, ...]) -> None:
        self._problems.pop(problem_id, None)
        self._problem_scope_by_id.pop(problem_id, None)
        self._global_problem_ids.discard(problem_id)
        empty_keys: list[str] = []
        for key_id, problem_ids in self._problem_ids_by_key.items():
            problem_ids.discard(problem_id)
            if not problem_ids:
                empty_keys.append(key_id)
        for key_id in empty_keys:
            self._problem_ids_by_key.pop(key_id, None)

    @staticmethod
    def _problem_identity(problem: ProviderProblem) -> tuple[Any, ...]:
        return (
            problem.code.value,
            problem.message,
            problem.retryable,
            problem.next_action,
            problem.fact_key_ids,
        )

    @staticmethod
    def _validate_store_result(result: object) -> None:
        if type(result) is not EvidenceStoreResult:
            raise TypeError(
                "durable evidence source must return EvidenceStoreResult"
            )
        if (
            not result.success
            or result.ledger is None
            or result.current_revision is None
            or result.purge_checked_at is None
        ):
            raise ValueError(
                "durable evidence source returned no current evidence"
            )

    @staticmethod
    def _validate_durable_ledger(ledger: EvidenceLedger) -> None:
        if any(
            ledger.policies.persistence_for(observation)
            is EvidencePersistence.MEMORY_ONLY
            for observation in ledger.observations
        ):
            raise FactContractError(
                "CACHE_CORRUPTED",
                (
                    "The durable evidence source returned a memory-only "
                    "observation."
                ),
            )


@dataclass(frozen=True, slots=True, init=False, repr=False)
class EvidenceSessionDeliverySource:
    """Sealed process-local adapter for private-delivery evidence reads.

    The public path attributes exist only because the delivery writer must bind
    the evidence source to the exact ``TripStore``.  They are private material:
    ``repr`` and the safe projection never expose them.
    """

    trip_id: str
    slug: str
    trips_root: Path
    data_dir: Path
    contract_version: str
    _session: EvidenceSession = field(repr=False, compare=False)
    _source: EvidenceStore = field(repr=False, compare=False)
    _clock: _SessionClock = field(repr=False, compare=False)
    _lock: object = field(repr=False, compare=False)
    _trip_dir: Path = field(repr=False, compare=False)
    _cache_path: Path = field(repr=False, compare=False)
    _lock_path: Path = field(repr=False, compare=False)
    _seal: str = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        session: EvidenceSession,
        source: EvidenceStore,
        _token: object | None = None,
    ) -> None:
        if _token is not _DELIVERY_SOURCE_TOKEN:
            raise FactContractError(
                "EVIDENCE_DELIVERY_SOURCE_UNAVAILABLE",
                "The delivery source must be created by EvidenceSession.",
            )
        metadata = _delivery_source_metadata(session=session, source=source)
        object.__setattr__(self, "trip_id", metadata[0])
        object.__setattr__(self, "slug", metadata[1])
        object.__setattr__(self, "trips_root", metadata[2])
        object.__setattr__(self, "data_dir", metadata[3])
        object.__setattr__(
            self,
            "contract_version",
            EVIDENCE_SESSION_DELIVERY_SOURCE_VERSION,
        )
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "_source", source)
        object.__setattr__(self, "_clock", session._clock)
        object.__setattr__(self, "_lock", session._lock)
        object.__setattr__(self, "_trip_dir", metadata[4])
        object.__setattr__(self, "_cache_path", metadata[5])
        object.__setattr__(self, "_lock_path", metadata[6])
        object.__setattr__(
            self,
            "_seal",
            _delivery_source_seal(
                session=session,
                source=source,
                metadata=metadata,
            ),
        )

    def read_snapshot(self, *, evaluation_at: datetime) -> EvidenceSnapshot:
        """Return one exact combined durable + memory-only snapshot."""

        session = self._session
        lock = _validated_delivery_session_lock(session, adapter=self)
        with lock:
            _verify_delivery_source(self, session=session)
            return EvidenceSession._read_private_delivery_snapshot(
                session,
                self,
                evaluation_at=evaluation_at,
            )

    def to_safe_dict(self) -> dict[str, Any]:
        """Return a value-free capability description, never source state."""

        session = self._session
        lock = _validated_delivery_session_lock(session, adapter=self)
        with lock:
            _verify_delivery_source(self, session=session)
            return {
                "contract_version": EVIDENCE_SESSION_DELIVERY_SOURCE_VERSION,
                "contains_private_data": True,
                "read_only_snapshot_source": True,
                "read_performs_provider_call": False,
                "read_performs_application_level_durable_write": False,
                "read_performs_artifact_write": False,
                "ordinary_read_may_update_host_atime": True,
                "write_authorized": False,
            }

    def __repr__(self) -> str:
        session = self._session
        lock = _validated_delivery_session_lock(session, adapter=self)
        with lock:
            _verify_delivery_source(self, session=session)
            return (
                "EvidenceSessionDeliverySource("
                f"contract_version={EVIDENCE_SESSION_DELIVERY_SOURCE_VERSION!r}, "
                "contains_private_data=True, write_authorized=False)"
            )

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("EvidenceSessionDeliverySource is process-local")


def _delivery_utc(value: datetime) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is not timezone.utc
        or value.fold != 0
    ):
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TIME_INVALID",
            "The delivery snapshot time must be exact UTC.",
        )
    return value


def _validated_delivery_instance_state(
    value: object,
    *,
    maximum: int,
) -> dict[str, object]:
    try:
        state = object.__getattribute__(value, "__dict__")
    except Exception:
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            "The delivery source host state is not intact.",
        ) from None
    if (
        type(state) is not dict
        or len(state) > maximum
        or any(type(name) is not str for name in state)
    ):
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            "The delivery source host state is not intact.",
        )
    return state


def _validated_delivery_session_lock(
    session: object,
    *,
    adapter: object | None = None,
) -> object:
    if type(session) is not EvidenceSession:
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            "The delivery source host session is not intact.",
        )
    _validated_delivery_instance_state(
        session,
        maximum=_MAX_DELIVERY_SESSION_FIELDS,
    )
    try:
        lock = object.__getattribute__(session, "_lock")
    except Exception:
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            "The delivery source session lock is not intact.",
        ) from None
    if type(lock) is not _RLOCK_TYPE or (
        adapter is not None
        and (
            type(adapter) is not EvidenceSessionDeliverySource
            or adapter._lock is not lock
        )
    ):
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            "The delivery source session lock is not intact.",
        )
    return lock


def _delivery_digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _delivery_text(value: object, *, maximum: int = _MAX_DELIVERY_TEXT_CHARS) -> bool:
    return type(value) is str and len(value) <= maximum


def _delivery_identity(
    value: object,
    *,
    path_component: bool = False,
) -> bool:
    if (
        type(value) is not str
        or not value
        or len(value) > 256
        or len(value.encode("utf-8")) > 1024
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return False
    if path_component and (
        value in {".", ".."}
        or "/" in value
        or "\\" in value
    ):
        return False
    return True


def _delivery_text_tuple(
    value: object,
    *,
    allow_empty: bool = True,
) -> bool:
    return (
        type(value) is tuple
        and len(value) <= 256
        and (allow_empty or bool(value))
        and all(_delivery_text(item) for item in value)
    )


def _validate_delivery_policy(policy: object) -> ProviderPolicy:
    if type(policy) is not ProviderPolicy:
        raise ValueError("delivery policy is invalid")
    if any(
        not _delivery_text(value)
        for value in (
            policy.policy_id,
            policy.provider_id,
            policy.adapter_id,
            policy.adapter_version,
            policy.contract_region,
        )
    ) or not _delivery_digest(policy.policy_digest):
        raise ValueError("delivery policy text is invalid")
    if (
        type(policy.allowed_fact_kinds) is not tuple
        or not policy.allowed_fact_kinds
        or len(policy.allowed_fact_kinds) > len(FactKind)
        or any(type(item) is not FactKind for item in policy.allowed_fact_kinds)
        or not _delivery_text_tuple(
            policy.allowed_value_fields,
            allow_empty=False,
        )
        or not _delivery_text_tuple(
            policy.allowed_operations,
            allow_empty=False,
        )
        or not _delivery_text_tuple(policy.allowed_query_fields)
        or not _delivery_text_tuple(policy.required_attribution_labels)
        or type(policy.persistence) is not EvidencePersistence
        or type(policy.max_validity_seconds) is not int
        or not 0 < policy.max_validity_seconds <= 2**63 - 1
        or (
            policy.max_retention_seconds is not None
            and (
                type(policy.max_retention_seconds) is not int
                or not 0 < policy.max_retention_seconds <= 2**63 - 1
            )
        )
    ):
        raise ValueError("delivery policy shape is invalid")
    rebuilt = replace(policy)
    if rebuilt != policy:
        raise ValueError("delivery policy identity drifted")
    return rebuilt


def _validate_delivery_key(key: object) -> FactKey:
    if (
        type(key) is not FactKey
        or type(key.kind) is not FactKind
        or not _delivery_text(key.contract_version)
        or not _delivery_digest(key.key_id)
        or type(key.subject_ids) is not tuple
        or not key.subject_ids
        or len(key.subject_ids) > 16
        or any(not _delivery_text(item) for item in key.subject_ids)
        or type(key.qualifiers) is not tuple
        or len(key.qualifiers) > 32
    ):
        raise ValueError("delivery fact key is invalid")
    for item in key.qualifiers:
        if (
            type(item) is not tuple
            or len(item) != 2
            or not _delivery_text(item[0])
            or type(item[1]) not in {str, int, float, bool, type(None)}
            or (type(item[1]) is str and not _delivery_text(item[1]))
            or (
                type(item[1]) is int
                and not -(2**63) <= item[1] <= 2**63 - 1
            )
            or (
                type(item[1]) is float
                and not math.isfinite(item[1])
            )
        ):
            raise ValueError("delivery fact qualifier is invalid")
    rebuilt = replace(key)
    if rebuilt != key:
        raise ValueError("delivery fact key identity drifted")
    return rebuilt


def _validate_delivery_value(value: object) -> FactValue:
    if (
        type(value) is not FactValue
        or type(value.kind) is not FactKind
        or not _delivery_text(value.schema_version)
        or type(value.canonical_json) is not bytes
        or len(value.canonical_json) > 32_768
        or not _delivery_digest(value.value_digest)
    ):
        raise ValueError("delivery fact value is invalid")
    rebuilt = replace(value)
    if rebuilt != value:
        raise ValueError("delivery fact value identity drifted")
    return rebuilt


def _validate_delivery_provenance(value: object) -> ProviderProvenance:
    if type(value) is not ProviderProvenance:
        raise ValueError("delivery provenance is invalid")
    if any(
        not _delivery_text(item)
        for item in (
            value.provider_id,
            value.adapter_id,
            value.adapter_version,
            value.retention_policy_id,
        )
    ) or not _delivery_digest(value.request_fingerprint):
        raise ValueError("delivery provenance text is invalid")
    if any(
        item is not None and not _delivery_text(item)
        for item in (
            value.provider_record_id,
            value.response_id,
            value.source_uri,
        )
    ) or type(value.attributions) is not tuple or len(value.attributions) > 32:
        raise ValueError("delivery provenance shape is invalid")
    for item in value.attributions:
        if (
            type(item) is not tuple
            or len(item) != 2
            or not _delivery_text(item[0])
            or (item[1] is not None and not _delivery_text(item[1]))
        ):
            raise ValueError("delivery attribution is invalid")
    rebuilt = replace(value)
    if rebuilt != value:
        raise ValueError("delivery provenance identity drifted")
    return rebuilt


def _validate_delivery_observation(
    value: object,
    *,
    registry: ProviderPolicyRegistry,
) -> FactObservation:
    if (
        type(value) is not FactObservation
        or not _delivery_text(value.contract_version)
        or not _delivery_digest(value.observation_id)
        or type(value.confidence) is not float
        or not math.isfinite(value.confidence)
        or not 0.0 <= value.confidence <= 1.0
        or type(value.retrieved_at) is not datetime
        or value.retrieved_at.tzinfo is not timezone.utc
        or value.retrieved_at.fold != 0
        or type(value.valid_until) is not datetime
        or value.valid_until.tzinfo is not timezone.utc
        or value.valid_until.fold != 0
        or (
            value.purge_at is not None
            and (
                type(value.purge_at) is not datetime
                or value.purge_at.tzinfo is not timezone.utc
                or value.purge_at.fold != 0
            )
        )
    ):
        raise ValueError("delivery observation shape is invalid")
    key = _validate_delivery_key(value.key)
    fact_value = _validate_delivery_value(value.value)
    provenance = _validate_delivery_provenance(value.provenance)
    rebuilt = replace(
        value,
        key=key,
        value=fact_value,
        provenance=provenance,
    )
    if rebuilt != value:
        raise ValueError("delivery observation identity drifted")
    registry.validate_observation(rebuilt)
    return rebuilt


def _validate_delivery_ledger(ledger: object) -> EvidenceLedger:
    if (
        type(ledger) is not EvidenceLedger
        or type(ledger.policies) is not ProviderPolicyRegistry
        or type(ledger.policies.policies) is not tuple
        or not ledger.policies.policies
        or len(ledger.policies.policies) > _MAX_DELIVERY_POLICIES
        or not _delivery_digest(ledger.policies.revision)
        or type(ledger.observations) is not tuple
        or len(ledger.observations) > _MAX_DELIVERY_OBSERVATIONS
        or type(ledger.generation) is not int
        or not 0 <= ledger.generation <= 2**63 - 1
        or not _delivery_digest(ledger.revision)
    ):
        raise ValueError("delivery ledger shape is invalid")
    policies = tuple(
        _validate_delivery_policy(item)
        for item in ledger.policies.policies
    )
    registry = ProviderPolicyRegistry(
        policies=policies,
        revision=ledger.policies.revision,
    )
    if registry != ledger.policies:
        raise ValueError("delivery policy registry drifted")
    observations: list[FactObservation] = []
    aggregate_bytes = 0
    for item in ledger.observations:
        rebuilt = _validate_delivery_observation(item, registry=registry)
        aggregate_bytes += len(rebuilt.value.canonical_json)
        if aggregate_bytes > _MAX_DELIVERY_EVIDENCE_BYTES:
            raise ValueError("delivery evidence exceeds its bound")
        observations.append(rebuilt)
    normalized = tuple(
        sorted(
            observations,
            key=lambda item: (
                item.key.key_id,
                item.provenance.provider_id,
                item.observation_id,
            ),
        )
    )
    if normalized != ledger.observations:
        raise ValueError("delivery evidence order drifted")
    expected_revision = _fact_digest(
        {
            "policy_registry_revision": registry.revision,
            "generation": ledger.generation,
            "observation_ids": [item.observation_id for item in normalized],
        },
        prefix="evidence-ledger",
    )
    if ledger.revision != expected_revision:
        raise ValueError("delivery ledger identity drifted")
    return ledger


def _validate_delivery_snapshot(
    value: object,
    *,
    expected_policies: ProviderPolicyRegistry,
) -> EvidenceSnapshot:
    """Preflight an exact snapshot before any comparison can dispatch."""

    if type(value) is not EvidenceSnapshot:
        raise ValueError("delivery snapshot is invalid")
    if (
        type(expected_policies) is not ProviderPolicyRegistry
        or type(value.policies) is not ProviderPolicyRegistry
        or value.policies is not expected_policies
        or type(value.policies.policies) is not tuple
        or not value.policies.policies
        or len(value.policies.policies) > _MAX_DELIVERY_POLICIES
        or not _delivery_digest(value.policies.revision)
        or type(value.observations) is not tuple
        or len(value.observations) > _MAX_DELIVERY_OBSERVATIONS
        or any(type(item) is not FactObservation for item in value.observations)
        or type(value.evaluation_at) is not datetime
        or value.evaluation_at.tzinfo is not timezone.utc
        or value.evaluation_at.fold != 0
        or type(value.purge_checked_at) is not datetime
        or value.purge_checked_at.tzinfo is not timezone.utc
        or value.purge_checked_at.fold != 0
        or type(value.contract_version) is not str
        or value.contract_version != EVIDENCE_SNAPSHOT_VERSION
        or not _delivery_digest(value.store_revision)
        or not _delivery_digest(value.evidence_revision)
        or (
            value.outcome_revision is not None
            and not _delivery_digest(value.outcome_revision)
        )
        or not _delivery_digest(value.snapshot_id)
    ):
        raise ValueError("delivery snapshot shape is invalid")

    policies = tuple(
        _validate_delivery_policy(item)
        for item in value.policies.policies
    )
    rebuilt_registry = ProviderPolicyRegistry(
        policies=policies,
        revision=value.policies.revision,
    )
    if rebuilt_registry != value.policies:
        raise ValueError("delivery snapshot policies drifted")

    observations: list[FactObservation] = []
    aggregate_bytes = 0
    source_slots: set[tuple[str, str]] = set()
    for item in value.observations:
        rebuilt = _validate_delivery_observation(
            item,
            registry=rebuilt_registry,
        )
        aggregate_bytes += len(rebuilt.value.canonical_json)
        if aggregate_bytes > _MAX_DELIVERY_EVIDENCE_BYTES:
            raise ValueError("delivery snapshot evidence exceeds its bound")
        if (
            rebuilt.retrieved_at > value.purge_checked_at
            or (
                rebuilt.purge_at is not None
                and value.purge_checked_at >= rebuilt.purge_at
            )
            or rebuilt.source_slot in source_slots
        ):
            raise ValueError("delivery snapshot observation is invalid")
        source_slots.add(rebuilt.source_slot)
        observations.append(rebuilt)
    normalized = tuple(
        sorted(
            observations,
            key=lambda item: (
                item.key.key_id,
                item.provenance.provider_id,
                item.observation_id,
            ),
        )
    )
    if normalized != value.observations:
        raise ValueError("delivery snapshot evidence order drifted")
    expected_evidence_revision = _fact_digest(
        {
            "observation_ids": [
                item.observation_id for item in normalized
            ]
        },
        prefix="active-evidence",
    )
    snapshot_payload = {
        "contract_version": value.contract_version,
        "policy_registry_revision": value.policies.revision,
        "store_revision": value.store_revision,
        "evidence_revision": expected_evidence_revision,
        "evaluation_at": _utc_iso(value.evaluation_at),
        "purge_checked_at": _utc_iso(value.purge_checked_at),
    }
    if value.outcome_revision is not None:
        snapshot_payload["outcome_revision"] = value.outcome_revision
    expected_snapshot_id = _fact_digest(
        snapshot_payload,
        prefix="evidence-snapshot",
    )
    if (
        value.evidence_revision != expected_evidence_revision
        or value.snapshot_id != expected_snapshot_id
    ):
        raise ValueError("delivery snapshot identity drifted")
    return value


def _validated_delivery_problems(
    session: EvidenceSession,
) -> tuple[ProviderProblem, ...]:
    values = session._problems
    if type(values) is not dict or len(values) > _MAX_DELIVERY_PROBLEMS:
        raise ValueError("delivery problems exceed their bound")
    rebuilt: list[ProviderProblem] = []
    for item in values.values():
        if (
            type(item) is not ProviderProblem
            or type(item.code) is not ProviderProblemCode
            or not _delivery_text(item.message, maximum=1024)
            or type(item.retryable) is not bool
            or not _delivery_text(item.next_action, maximum=256)
            or type(item.fact_key_ids) is not tuple
            or len(item.fact_key_ids) > 256
            or any(not _delivery_digest(key_id) for key_id in item.fact_key_ids)
        ):
            raise ValueError("delivery problem shape is invalid")
        checked = replace(item)
        if checked != item:
            raise ValueError("delivery problem identity drifted")
        rebuilt.append(checked)
    return tuple(
        sorted(
            rebuilt,
            key=lambda item: (
                item.fact_key_ids,
                item.code.value,
                item.next_action,
                item.message,
            ),
        )
    )


def _delivery_source_metadata(
    *,
    session: EvidenceSession,
    source: EvidenceStore,
) -> tuple[str, str, Path, Path, Path, Path, Path]:
    try:
        return _delivery_source_metadata_unchecked(
            session=session,
            source=source,
        )
    except FactContractError as exc:
        if exc.code.startswith("EVIDENCE_DELIVERY_SOURCE_"):
            raise
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            "The delivery source host binding is not intact.",
        ) from None
    except Exception:
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            "The delivery source host binding is not intact.",
        ) from None


def _delivery_source_metadata_unchecked(
    *,
    session: EvidenceSession,
    source: EvidenceStore,
) -> tuple[str, str, Path, Path, Path, Path, Path]:
    if type(session) is not EvidenceSession or type(source) is not EvidenceStore:
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_UNAVAILABLE",
            "The delivery source host binding is unavailable.",
        )
    _validated_delivery_instance_state(
        session,
        maximum=_MAX_DELIVERY_SESSION_FIELDS,
    )
    source_state = _validated_delivery_instance_state(
        source,
        maximum=_MAX_DELIVERY_SOURCE_FIELDS,
    )
    if any(
        name in source_state
        for name, class_value in EvidenceStore.__dict__.items()
        if callable(class_value)
    ):
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            "The delivery source host binding is not intact.",
        )
    lock = _validated_delivery_session_lock(session)
    try:
        clock = object.__getattribute__(session, "_clock")
    except Exception:
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            "The delivery source clock is not intact.",
        ) from None
    if (
        type(clock) is not _SessionClock
        or any(
            name in _validated_delivery_instance_state(
                clock,
                maximum=_MAX_DELIVERY_CLOCK_FIELDS,
            )
            for name, class_value in _SessionClock.__dict__.items()
            if callable(class_value)
        )
    ):
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            "The delivery source clock is not intact.",
        )
    _delivery_utc(_SessionClock.high_water(clock))
    try:
        ledger = object.__getattribute__(session, "_ledger")
        session_source = object.__getattribute__(session, "_source")
    except Exception:
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            "The delivery source host binding is not intact.",
        ) from None
    _validate_delivery_ledger(ledger)
    _validated_delivery_problems(session)
    try:
        trip_id = source.trip_id
        slug = source.slug
        trips_root = source.trips_root
        trip_dir = source.trip_dir
        data_dir = source.data_dir
        cache_path = source.cache_path
        lock_path = source.lock_path
        policies = source.policies
    except Exception:
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            "The delivery source host binding is not intact.",
        ) from None
    if (
        not _delivery_identity(trip_id)
        or not _delivery_identity(slug, path_component=True)
        or type(trips_root) is not _PATH_TYPE
        or type(trip_dir) is not _PATH_TYPE
        or type(data_dir) is not _PATH_TYPE
        or type(cache_path) is not _PATH_TYPE
        or type(lock_path) is not _PATH_TYPE
        or trip_dir != trips_root / slug
        or data_dir != trip_dir / "data"
        or cache_path != data_dir / ".trip-planner-evidence.json"
        or lock_path != data_dir / ".trip-planner.lock"
        or session_source is not source
        or type(ledger) is not EvidenceLedger
        or policies is not ledger.policies
    ):
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            "The delivery source host binding is not intact.",
        )
    return (
        trip_id,
        slug,
        trips_root,
        data_dir,
        trip_dir,
        cache_path,
        lock_path,
    )


def _delivery_source_seal(
    *,
    session: EvidenceSession,
    source: EvidenceStore,
    metadata: tuple[str, str, Path, Path, Path, Path, Path],
) -> str:
    return _digest(
        {
            "contract_version": EVIDENCE_SESSION_DELIVERY_SOURCE_VERSION,
            "session_id": id(session),
            "source_id": id(source),
            "clock_id": id(session._clock),
            "lock_id": id(session._lock),
            "trip_id": metadata[0],
            "slug": metadata[1],
            "trips_root": str(metadata[2]),
            "data_dir": str(metadata[3]),
            "trip_dir": str(metadata[4]),
            "cache_path": str(metadata[5]),
            "lock_path": str(metadata[6]),
            "policy_revision": session._ledger.policies.revision,
        },
        prefix=EVIDENCE_SESSION_DELIVERY_SOURCE_VERSION,
    )


def _verify_delivery_source(
    value: EvidenceSessionDeliverySource,
    *,
    session: EvidenceSession,
) -> None:
    try:
        _verify_delivery_source_unchecked(value, session=session)
    except FactContractError as exc:
        if exc.code.startswith("EVIDENCE_DELIVERY_SOURCE_"):
            raise
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            "The delivery source is not intact.",
        ) from None
    except Exception:
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            "The delivery source is not intact.",
        ) from None


def _verify_delivery_source_unchecked(
    value: EvidenceSessionDeliverySource,
    *,
    session: EvidenceSession,
) -> None:
    if type(value) is not EvidenceSessionDeliverySource:
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            "The delivery source is not intact.",
        )
    if (
        type(value.contract_version) is not str
        or value.contract_version
        != EVIDENCE_SESSION_DELIVERY_SOURCE_VERSION
        or value._session is not session
        or type(value._source) is not EvidenceStore
        or type(value._clock) is not _SessionClock
        or type(value._lock) is not _RLOCK_TYPE
        or type(value.trip_id) is not str
        or type(value.slug) is not str
        or type(value.trips_root) is not _PATH_TYPE
        or type(value.data_dir) is not _PATH_TYPE
        or type(value._trip_dir) is not _PATH_TYPE
        or type(value._cache_path) is not _PATH_TYPE
        or type(value._lock_path) is not _PATH_TYPE
        or type(value._seal) is not str
    ):
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            "The delivery source is not intact.",
        )
    metadata = _delivery_source_metadata(
        session=session,
        source=value._source,
    )
    try:
        session_clock = object.__getattribute__(session, "_clock")
        session_lock = object.__getattribute__(session, "_lock")
    except Exception:
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            "The delivery source is not intact.",
        ) from None
    if (
        value._clock is not session_clock
        or value._lock is not session_lock
        or metadata
        != (
            value.trip_id,
            value.slug,
            value.trips_root,
            value.data_dir,
            value._trip_dir,
            value._cache_path,
            value._lock_path,
        )
        or value._seal
        != _delivery_source_seal(
            session=session,
            source=value._source,
            metadata=metadata,
        )
    ):
        raise FactContractError(
            "EVIDENCE_DELIVERY_SOURCE_TAMPERED",
            "The delivery source is not intact.",
        )


__all__ = [
    "EVIDENCE_SESSION_DELIVERY_SOURCE_VERSION",
    "EvidenceSession",
    "EvidenceSessionDeliverySource",
    "EvidenceSessionLoad",
    "EvidenceSessionMerge",
    "DurableEvidenceSource",
]
