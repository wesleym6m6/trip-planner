"""Bounded, offline timeline validation for one legacy trip.

This is intentionally narrower than the renderer's seven-file structural
validator.  It projects only the two legacy files consumed by the planning
kernel (``trip.json`` and ``itinerary.json``), through a bounded no-follow
snapshot, so ``tripctl`` can report deterministic timeline blockers without
reading cache contents, contacting providers, or changing a user trip.

The public result is aggregate-only.  In particular, it never serializes a
trip title, place, time, location, activity ID, source path, provider value, or
the raw message carried by a loader/kernel exception.
"""

from __future__ import annotations

import re
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from .codec import PlanCodecError, decode_json_bytes
from .legacy_evidence import (
    LegacyEvidencePreviewError,
    LegacyEvidenceSourceState,
    _SourceFingerprint,
    _capture_manifest_entry,
    _find_data_dir,
    _read_verified_source,
)
from .loaders import LoadError, load_legacy_trip
from .models import CheckStatus, IssueSeverity
from .timeline import evaluate_timeline


LEGACY_TIMELINE_VALIDATION_VERSION = "legacy-timeline-validation/v1"
"""Version of this bounded legacy timeline projection."""

_SOURCE_FILES = ("trip.json", "itinerary.json")
_PROBLEM_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_TIMELINE_STATUSES = frozenset(item.value for item in CheckStatus)
_SEVERITIES = frozenset(item.value for item in IssueSeverity)


class LegacyTimelineValidationError(ValueError):
    """One redacted failure while preparing a legacy timeline snapshot."""

    def __init__(self, code: str, *, affected_count: int = 1) -> None:
        if (
            not isinstance(code, str)
            or _PROBLEM_CODE_RE.fullmatch(code) is None
        ):
            raise ValueError("legacy timeline error code must be a bounded token")
        if type(affected_count) is not int or affected_count < 1:
            raise ValueError("affected_count must be a positive integer")
        self.code = code
        self.affected_count = affected_count
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LegacyTimelineProblem:
    """One safe aggregate of deterministic kernel issues."""

    code: str
    severity: str
    affected_count: int

    def __post_init__(self) -> None:
        if _PROBLEM_CODE_RE.fullmatch(self.code) is None:
            raise ValueError("timeline problem code must be a bounded token")
        if self.severity not in _SEVERITIES:
            raise ValueError("timeline problem severity is unsupported")
        if type(self.affected_count) is not int or self.affected_count < 1:
            raise ValueError("timeline problem affected_count must be positive")

    def to_dict(self) -> dict[str, str | int]:
        return {
            "code": self.code,
            "severity": self.severity,
            "affected_count": self.affected_count,
        }


@dataclass(frozen=True, slots=True, repr=False)
class LegacyTimelineValidation:
    """One exact, redacted evaluation whose source can be rechecked."""

    _data_dir: Path = field(repr=False)
    _resolved_data_dir: Path = field(repr=False)
    _sources: tuple[_SourceFingerprint, ...] = field(repr=False)
    timeline_status: str
    problems: tuple[LegacyTimelineProblem, ...]
    day_count: int
    activity_count: int
    timeline_entry_count: int
    day_summary_count: int

    def __post_init__(self) -> None:
        if self.timeline_status not in _TIMELINE_STATUSES:
            raise ValueError("timeline_status is unsupported")
        if tuple(source.name for source in self._sources) != _SOURCE_FILES:
            raise ValueError("timeline validation must bind the fixed source pair")
        if tuple(
            sorted((item.code, item.severity) for item in self.problems)
        ) != tuple((item.code, item.severity) for item in self.problems):
            raise ValueError("timeline problems must use deterministic ordering")
        for field_name in (
            "day_count",
            "activity_count",
            "timeline_entry_count",
            "day_summary_count",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")

    def source_is_current(self) -> bool:
        """Return whether the exact input pair still backs this result."""

        try:
            current_data_dir = _find_data_dir(self._data_dir)
        except LegacyEvidencePreviewError:
            return False
        if not _data_dir_is_current(current_data_dir, self._resolved_data_dir):
            return False
        return _sources_are_current(current_data_dir, self._sources)

    def to_dict(self) -> dict[str, Any]:
        """Return only safe counts and issue tokens for ``tripctl``."""

        return {
            "contract_version": LEGACY_TIMELINE_VALIDATION_VERSION,
            "timeline_status": self.timeline_status,
            "day_count": self.day_count,
            "activity_count": self.activity_count,
            "timeline_entry_count": self.timeline_entry_count,
            "day_summary_count": self.day_summary_count,
            "problems": [item.to_dict() for item in self.problems],
        }

    def __repr__(self) -> str:
        return (
            "LegacyTimelineValidation("
            f"timeline_status={self.timeline_status!r}, "
            f"problems={len(self.problems)!r}, day_count={self.day_count!r}, "
            f"activity_count={self.activity_count!r})"
        )


def validate_legacy_timeline(path: str | Path) -> LegacyTimelineValidation:
    """Evaluate a legacy trip through an ephemeral, bounded source snapshot.

    The caller is responsible for the product-level canonical marker policy.
    This function deliberately reads no renderer sidecars: they remain covered
    by the existing full legacy validator rather than being reimplemented here.
    """

    try:
        data_dir = _find_data_dir(Path(path))
    except (LegacyEvidencePreviewError, TypeError, ValueError) as exc:
        raise LegacyTimelineValidationError("DATA_DIRECTORY_MISSING") from exc
    try:
        resolved_data_dir = data_dir.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LegacyTimelineValidationError(
            "LEGACY_TIMELINE_SOURCE_UNAVAILABLE"
        ) from exc

    sources = tuple(_capture_manifest_entry(data_dir, name) for name in _SOURCE_FILES)
    unavailable_count = sum(
        source.state is not LegacyEvidenceSourceState.REGULAR_FILE
        for source in sources
    )
    if unavailable_count:
        raise LegacyTimelineValidationError(
            "LEGACY_TIMELINE_SOURCE_UNAVAILABLE",
            affected_count=unavailable_count,
        )

    source_bytes: dict[str, bytes] = {}
    for source in sources:
        content = _read_verified_source(data_dir, source)
        if content is None:
            raise LegacyTimelineValidationError("STALE_LEGACY_TIMELINE_SOURCE")
        try:
            decode_json_bytes(content)
        except (
            MemoryError,
            OverflowError,
            PlanCodecError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            _raise_after_source_check(
                data_dir,
                sources,
                LegacyTimelineValidationError("LEGACY_TIMELINE_SOURCE_MALFORMED"),
            )
        source_bytes[source.name] = content

    failure: LegacyTimelineValidationError | None = None
    validation: LegacyTimelineValidation | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="trip-planner-legacy-timeline-") as root:
            snapshot_data = Path(root) / "data"
            snapshot_data.mkdir()
            for name in _SOURCE_FILES:
                (snapshot_data / name).write_bytes(source_bytes[name])
            state = load_legacy_trip(snapshot_data)
            report = evaluate_timeline(state, now=None)
            validation = LegacyTimelineValidation(
                _data_dir=data_dir,
                _resolved_data_dir=resolved_data_dir,
                _sources=sources,
                timeline_status=_timeline_status(report.status),
                problems=_summarize_issues(report.issues),
                day_count=len(state.days),
                activity_count=len(state.activities),
                timeline_entry_count=len(report.timeline),
                day_summary_count=len(report.day_summaries),
            )
    except (
        LoadError,
        MemoryError,
        OverflowError,
        PlanCodecError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        failure = LegacyTimelineValidationError("LEGACY_TIMELINE_LOAD_FAILED")
    except OSError:
        failure = LegacyTimelineValidationError("LEGACY_TIMELINE_VALIDATION_UNAVAILABLE")

    if not _data_dir_is_current(data_dir, resolved_data_dir) or not _sources_are_current(
        data_dir,
        sources,
    ):
        raise LegacyTimelineValidationError("STALE_LEGACY_TIMELINE_SOURCE")
    if failure is not None:
        raise failure
    if validation is None:
        raise LegacyTimelineValidationError("LEGACY_TIMELINE_VALIDATION_UNAVAILABLE")
    return validation


def verify_legacy_timeline_source(validation: LegacyTimelineValidation) -> None:
    """Fail closed before emitting a result whose source has drifted."""

    if not isinstance(validation, LegacyTimelineValidation):
        raise TypeError("validation must be a LegacyTimelineValidation")
    if not validation.source_is_current():
        raise LegacyTimelineValidationError("STALE_LEGACY_TIMELINE_SOURCE")


def _raise_after_source_check(
    data_dir: Path,
    sources: tuple[_SourceFingerprint, ...],
    failure: LegacyTimelineValidationError,
) -> NoReturn:
    if not _sources_are_current(data_dir, sources):
        raise LegacyTimelineValidationError("STALE_LEGACY_TIMELINE_SOURCE")
    raise failure


def _sources_are_current(
    data_dir: Path,
    sources: tuple[_SourceFingerprint, ...],
) -> bool:
    return tuple(
        _capture_manifest_entry(data_dir, name) for name in _SOURCE_FILES
    ) == sources


def _data_dir_is_current(data_dir: Path, expected: Path) -> bool:
    try:
        return data_dir.resolve(strict=True) == expected
    except (OSError, RuntimeError):
        return False


def _timeline_status(status: CheckStatus) -> str:
    if not isinstance(status, CheckStatus) or status.value not in _TIMELINE_STATUSES:
        raise ValueError("timeline report has an unsupported status")
    return status.value


def _summarize_issues(issues: tuple[Any, ...]) -> tuple[LegacyTimelineProblem, ...]:
    grouped: Counter[tuple[str, str]] = Counter()
    for issue in issues:
        code = getattr(issue, "code", None)
        severity = getattr(getattr(issue, "severity", None), "value", None)
        safe_code = (
            code
            if isinstance(code, str) and _PROBLEM_CODE_RE.fullmatch(code) is not None
            else "TIMELINE_ISSUE_REDACTED"
        )
        safe_severity = severity if severity in _SEVERITIES else IssueSeverity.WARNING.value
        grouped[(safe_code, safe_severity)] += 1
    return tuple(
        LegacyTimelineProblem(code, severity, affected_count)
        for (code, severity), affected_count in sorted(grouped.items())
    )


__all__ = [
    "LEGACY_TIMELINE_VALIDATION_VERSION",
    "LegacyTimelineProblem",
    "LegacyTimelineValidation",
    "LegacyTimelineValidationError",
    "validate_legacy_timeline",
    "verify_legacy_timeline_source",
]
