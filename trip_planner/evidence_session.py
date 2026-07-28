"""Run-scoped evidence boundary for memory-only provider facts.

The durable :class:`EvidenceStore` owns only policy-authorized disk records.
This session reloads that exact durable source to detect drift, then merges
restricted provider values in memory.  It intentionally has no filesystem
path and no write-through behavior.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from .evidence_store import EvidenceStoreResult
from .facts import (
    AuthorizedProviderResult,
    EvidenceLedger,
    EvidencePersistence,
    EvidenceSnapshot,
    FactContractError,
    ProviderProblem,
    merge_provider_result,
    prune_evidence,
)


Clock = Callable[[], datetime]


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
        self._seed_store_problems(base.provider_problems)

    def load(self) -> EvidenceSessionLoad:
        """Return current evidence after trusted-clock retention pruning."""

        with self._lock:
            checked_at = self._refresh_durable()
            pruned = prune_evidence(self._ledger, purge_now=checked_at)
            self._ledger = pruned.ledger
            return self._load_at(checked_at)

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
            self._validate_current_route_basis(authorized_result)
            checked_at = self._clock.now()
            merged = merge_provider_result(
                self._ledger,
                authorized_result,
                purge_now=checked_at,
            )
            self._ledger = merged.ledger
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
        self._seed_store_problems(current.provider_problems)
        return max(checked_at, self._clock.now())

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
        active_ids = {
            item.observation_id for item in self._ledger.observations
        }
        if (
            None in endpoint_observations
            or not endpoint_observations.issubset(active_ids)
        ):
            raise FactContractError(
                "EVIDENCE_REVISION_CHANGED",
                (
                    "Google route endpoint identity changed before the "
                    "memory-only result was merged."
                ),
            )

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


__all__ = [
    "EvidenceSession",
    "EvidenceSessionLoad",
    "EvidenceSessionMerge",
    "DurableEvidenceSource",
]
