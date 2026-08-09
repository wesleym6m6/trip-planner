"""Bounded, read-only canonical inputs for the public ``tripctl`` facade.

This module is deliberately narrower than :class:`~trip_planner.store.TripStore`.
It pins and reads only ``plan.json``, never opens an EvidenceStore, and never
loads receipts or private state into a public result.  ``validate`` evaluates a
detached canonical snapshot with ``now=None`` so repeated offline calls remain
deterministic.  The exact source is re-read before a result can escape.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from .codec import PlanCodecError, decode_plan, plan_to_trip_state
from .loaders import LoadError
from .models import CheckStatus, IssueSeverity
from .timeline import evaluate_timeline


CANONICAL_TRIPCTL_VERSION = "canonical-tripctl/v1"
"""Version of the aggregate-only canonical projection."""

MAX_CANONICAL_PLAN_BYTES = 16 * 1024 * 1024
"""Maximum canonical input accepted by the public read-only facade."""

_PLAN_NAME = "plan.json"
_PROBLEM_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_TIMELINE_STATUSES = frozenset(item.value for item in CheckStatus)
_SEVERITIES = frozenset(item.value for item in IssueSeverity)
_SOURCE_DIGEST_DOMAIN = b"trip-planner.tripctl-canonical-source/v1\0"


class CanonicalTripctlError(ValueError):
    """One bounded internal failure with no source-derived message."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        if not isinstance(code, str) or _PROBLEM_CODE_RE.fullmatch(code) is None:
            raise ValueError("canonical tripctl error code must be a bounded token")
        if not isinstance(retryable, bool):
            raise TypeError("canonical tripctl retryable must be bool")
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CanonicalTimelineProblem:
    """One redacted aggregate of canonical kernel issues."""

    code: str
    severity: str
    affected_count: int

    def __post_init__(self) -> None:
        if _PROBLEM_CODE_RE.fullmatch(self.code) is None:
            raise ValueError("canonical timeline problem code must be bounded")
        if self.severity not in _SEVERITIES:
            raise ValueError("canonical timeline problem severity is unsupported")
        if type(self.affected_count) is not int or self.affected_count < 1:
            raise ValueError("canonical timeline affected_count must be positive")

    def to_dict(self) -> dict[str, str | int]:
        return {
            "code": self.code,
            "severity": self.severity,
            "affected_count": self.affected_count,
        }


@dataclass(frozen=True, slots=True, repr=False)
class CanonicalInspection:
    """Safe aggregate identity for one exact canonical source snapshot."""

    _snapshot: "_CanonicalSourceSnapshot" = field(repr=False)
    plan_revision: str
    generation: int
    day_count: int
    activity_count: int
    travel_estimate_count: int
    constraint_count: int
    receipt_count: int

    def __post_init__(self) -> None:
        if not isinstance(self._snapshot, _CanonicalSourceSnapshot):
            raise TypeError("canonical inspection requires an exact source snapshot")
        if not re.fullmatch(r"[0-9a-f]{64}", self.plan_revision):
            raise ValueError("canonical inspection revision must be a digest")
        for name in (
            "generation",
            "day_count",
            "activity_count",
            "travel_estimate_count",
            "constraint_count",
            "receipt_count",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    def source_is_current(self) -> bool:
        return _source_is_current(self._snapshot)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": CANONICAL_TRIPCTL_VERSION,
            "plan_revision": self.plan_revision,
            "source_digest": self._snapshot.source_digest,
            "generation": self.generation,
            "day_count": self.day_count,
            "activity_count": self.activity_count,
            "travel_estimate_count": self.travel_estimate_count,
            "constraint_count": self.constraint_count,
            "receipt_count": self.receipt_count,
        }


@dataclass(frozen=True, slots=True, repr=False)
class CanonicalTimelineValidation:
    """One deterministic canonical timeline result bound to exact source bytes."""

    _snapshot: "_CanonicalSourceSnapshot" = field(repr=False)
    plan_revision: str
    timeline_status: str
    problems: tuple[CanonicalTimelineProblem, ...]
    day_count: int
    activity_count: int
    timeline_entry_count: int
    day_summary_count: int

    def __post_init__(self) -> None:
        if not isinstance(self._snapshot, _CanonicalSourceSnapshot):
            raise TypeError("canonical validation requires an exact source snapshot")
        if not re.fullmatch(r"[0-9a-f]{64}", self.plan_revision):
            raise ValueError("canonical validation revision must be a digest")
        if self.timeline_status not in _TIMELINE_STATUSES:
            raise ValueError("canonical timeline status is unsupported")
        if tuple(
            sorted((item.code, item.severity) for item in self.problems)
        ) != tuple((item.code, item.severity) for item in self.problems):
            raise ValueError("canonical timeline problems must be deterministic")
        for name in (
            "day_count",
            "activity_count",
            "timeline_entry_count",
            "day_summary_count",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    def source_is_current(self) -> bool:
        return _source_is_current(self._snapshot)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": CANONICAL_TRIPCTL_VERSION,
            "plan_revision": self.plan_revision,
            "source_digest": self._snapshot.source_digest,
            "timeline_status": self.timeline_status,
            "day_count": self.day_count,
            "activity_count": self.activity_count,
            "timeline_entry_count": self.timeline_entry_count,
            "day_summary_count": self.day_summary_count,
            "problems": [item.to_dict() for item in self.problems],
        }


@dataclass(frozen=True, slots=True, repr=False)
class _CanonicalSourceSnapshot:
    data_dir: Path = field(repr=False)
    directory_identity: tuple[int, int] = field(repr=False)
    source_identity: tuple[int, int, int, int, int] = field(repr=False)
    raw: bytes = field(repr=False)
    source_digest: str


def inspect_canonical_plan(path: str | Path) -> CanonicalInspection:
    """Decode one canonical plan and expose only stable aggregate metadata."""

    snapshot = _read_source_snapshot(Path(path))
    try:
        plan = decode_plan(snapshot.raw)
        state = plan["state"]
        assert isinstance(state, dict)
        trip = state["trip"]
        itinerary = state["itinerary"]
        receipts = plan["receipts"]
        assert isinstance(trip, dict)
        assert isinstance(itinerary, dict)
        assert isinstance(receipts, dict)
        days = itinerary["days"]
        assert isinstance(days, list)
        inspection = CanonicalInspection(
            _snapshot=snapshot,
            plan_revision=str(plan["revision"]),
            generation=int(plan["generation"]),
            day_count=len(days),
            activity_count=sum(_list_length(day, "places") for day in days),
            travel_estimate_count=sum(_list_length(day, "travel") for day in days),
            constraint_count=_optional_list_length(trip, "constraints"),
            receipt_count=len(receipts),
        )
    except (
        AssertionError,
        KeyError,
        MemoryError,
        OverflowError,
        PlanCodecError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        _raise_after_source_check(snapshot, "CANONICAL_PLAN_MALFORMED")
    verify_canonical_source(inspection)
    return inspection


def validate_canonical_timeline(path: str | Path) -> CanonicalTimelineValidation:
    """Evaluate an exact canonical plan without runtime evidence or wall time."""

    snapshot = _read_source_snapshot(Path(path))
    try:
        plan = decode_plan(snapshot.raw)
        state = plan_to_trip_state(plan)
        report = evaluate_timeline(state, now=None)
        validation = CanonicalTimelineValidation(
            _snapshot=snapshot,
            plan_revision=str(plan["revision"]),
            timeline_status=report.status.value,
            problems=_summarize_issues(report.issues),
            day_count=len(state.days),
            activity_count=len(state.activities),
            timeline_entry_count=len(report.timeline),
            day_summary_count=len(report.day_summaries),
        )
    except PlanCodecError:
        _raise_after_source_check(snapshot, "CANONICAL_PLAN_MALFORMED")
    except (
        AssertionError,
        KeyError,
        LoadError,
        MemoryError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        _raise_after_source_check(
            snapshot,
            "CANONICAL_TIMELINE_VALIDATION_UNAVAILABLE",
        )
    except OSError:
        _raise_after_source_check(
            snapshot,
            "CANONICAL_TIMELINE_VALIDATION_UNAVAILABLE",
        )
    verify_canonical_source(validation)
    return validation


def verify_canonical_source(
    value: CanonicalInspection | CanonicalTimelineValidation,
) -> None:
    """Reject an aggregate whose exact canonical source has drifted."""

    if not isinstance(value, (CanonicalInspection, CanonicalTimelineValidation)):
        raise TypeError("value must be a canonical tripctl result")
    if not value.source_is_current():
        raise CanonicalTripctlError("STALE_CANONICAL_PLAN", retryable=True)


def _read_source_snapshot(data_dir: Path) -> _CanonicalSourceSnapshot:
    if not all(
        hasattr(os, name)
        for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    ):
        raise CanonicalTripctlError("CANONICAL_PLATFORM_UNSAFE")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    try:
        directory_fd = os.open(data_dir, directory_flags)
    except OSError as exc:
        raise CanonicalTripctlError("CANONICAL_PLAN_UNAVAILABLE") from exc
    try:
        directory_stat = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise CanonicalTripctlError("CANONICAL_PLAN_UNAVAILABLE")
        source_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if hasattr(os, "O_CLOEXEC"):
            source_flags |= os.O_CLOEXEC
        try:
            source_fd = os.open(_PLAN_NAME, source_flags, dir_fd=directory_fd)
        except OSError as exc:
            raise CanonicalTripctlError("CANONICAL_PLAN_UNAVAILABLE") from exc
        try:
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode):
                raise CanonicalTripctlError("CANONICAL_PLAN_UNAVAILABLE")
            try:
                raw = _read_bounded(source_fd)
            except OSError as exc:
                raise CanonicalTripctlError(
                    "CANONICAL_PLAN_UNAVAILABLE"
                ) from exc
            after = os.fstat(source_fd)
        finally:
            os.close(source_fd)
    except CanonicalTripctlError:
        raise
    except OSError as exc:
        raise CanonicalTripctlError("CANONICAL_PLAN_UNAVAILABLE") from exc
    finally:
        os.close(directory_fd)
    if _source_identity(before) != _source_identity(after):
        raise CanonicalTripctlError("STALE_CANONICAL_PLAN", retryable=True)
    return _CanonicalSourceSnapshot(
        data_dir=data_dir,
        directory_identity=(directory_stat.st_dev, directory_stat.st_ino),
        source_identity=_source_identity(before),
        raw=raw,
        source_digest="sha256:"
        + hashlib.sha256(_SOURCE_DIGEST_DOMAIN + raw).hexdigest(),
    )


def _read_bounded(descriptor: int) -> bytes:
    remaining = MAX_CANONICAL_PLAN_BYTES + 1
    chunks: list[bytes] = []
    while remaining:
        chunk = os.read(descriptor, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) > MAX_CANONICAL_PLAN_BYTES:
        raise CanonicalTripctlError("CANONICAL_PLAN_OVERSIZED")
    return raw


def _source_is_current(snapshot: _CanonicalSourceSnapshot) -> bool:
    try:
        current = _read_source_snapshot(snapshot.data_dir)
    except CanonicalTripctlError:
        return False
    return (
        current.directory_identity == snapshot.directory_identity
        and current.source_identity == snapshot.source_identity
        and current.source_digest == snapshot.source_digest
    )


def _raise_after_source_check(
    snapshot: _CanonicalSourceSnapshot,
    code: str,
) -> NoReturn:
    if not _source_is_current(snapshot):
        raise CanonicalTripctlError("STALE_CANONICAL_PLAN", retryable=True)
    raise CanonicalTripctlError(code)


def _source_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _list_length(value: Any, key: str) -> int:
    if not isinstance(value, dict):
        raise TypeError("canonical collection owner must be an object")
    items = value.get(key, [])
    if not isinstance(items, list):
        raise TypeError("canonical aggregate collection must be a list")
    return len(items)


def _optional_list_length(value: dict[str, Any], key: str) -> int:
    items = value.get(key, [])
    if not isinstance(items, list):
        raise TypeError("canonical aggregate collection must be a list")
    return len(items)


def _summarize_issues(
    issues: tuple[Any, ...],
) -> tuple[CanonicalTimelineProblem, ...]:
    grouped: Counter[tuple[str, str]] = Counter()
    for issue in issues:
        code = getattr(issue, "code", None)
        severity = getattr(getattr(issue, "severity", None), "value", None)
        safe_code = (
            code
            if isinstance(code, str) and _PROBLEM_CODE_RE.fullmatch(code) is not None
            else "TIMELINE_ISSUE_REDACTED"
        )
        safe_severity = (
            severity if severity in _SEVERITIES else IssueSeverity.WARNING.value
        )
        grouped[(safe_code, safe_severity)] += 1
    return tuple(
        CanonicalTimelineProblem(code, severity, affected_count)
        for (code, severity), affected_count in sorted(grouped.items())
    )


__all__ = [
    "CANONICAL_TRIPCTL_VERSION",
    "MAX_CANONICAL_PLAN_BYTES",
    "CanonicalInspection",
    "CanonicalTimelineProblem",
    "CanonicalTimelineValidation",
    "CanonicalTripctlError",
    "inspect_canonical_plan",
    "validate_canonical_timeline",
    "verify_canonical_source",
]
