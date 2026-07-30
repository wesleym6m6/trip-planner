"""Runtime-only activity availability projected from provider evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class AvailabilityDisposition(str, Enum):
    """Whether availability may constrain scheduling or only warn."""

    HARD_CURRENT = "hard_current"
    NEEDS_VERIFICATION = "needs_verification"


def _utc(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class AvailabilityInterval:
    """One half-open provider availability interval in UTC."""

    start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        start = _utc(
            self.start_at,
            "AvailabilityInterval.start_at",
        )
        end = _utc(
            self.end_at,
            "AvailabilityInterval.end_at",
        )
        if end <= start:
            raise ValueError(
                "AvailabilityInterval must have positive duration"
            )
        object.__setattr__(self, "start_at", start)
        object.__setattr__(self, "end_at", end)


@dataclass(frozen=True, slots=True)
class ActivityAvailability:
    """A process-local scheduling sidecar for one canonical activity."""

    activity_id: str
    disposition: AvailabilityDisposition
    intervals: tuple[AvailabilityInterval, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    reason: str | None = None
    fresh_until: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.activity_id, str) or not self.activity_id:
            raise ValueError(
                "ActivityAvailability.activity_id must be non-empty text"
            )
        if type(self.disposition) is not AvailabilityDisposition:
            raise TypeError(
                "ActivityAvailability.disposition must be exact"
            )
        if (
            not isinstance(self.intervals, tuple)
            or any(
                type(item) is not AvailabilityInterval
                for item in self.intervals
            )
        ):
            raise TypeError(
                "ActivityAvailability.intervals must contain exact intervals"
            )
        if (
            not isinstance(self.evidence_refs, tuple)
            or any(
                not isinstance(item, str) or not item
                for item in self.evidence_refs
            )
        ):
            raise TypeError(
                "ActivityAvailability.evidence_refs must contain text"
            )
        refs = tuple(sorted(set(self.evidence_refs)))
        object.__setattr__(self, "evidence_refs", refs)
        if self.reason is not None and (
            not isinstance(self.reason, str) or not self.reason
        ):
            raise TypeError(
                "ActivityAvailability.reason must be non-empty text or None"
            )

        fresh_until = self.fresh_until
        if self.disposition is AvailabilityDisposition.HARD_CURRENT:
            if fresh_until is None:
                raise ValueError(
                    "HARD_CURRENT availability requires fresh_until"
                )
            if not refs:
                raise ValueError(
                    "HARD_CURRENT availability requires evidence_refs"
                )
            if any(
                not ref.startswith("fact:")
                or len(ref) != 69
                or any(
                    character not in "0123456789abcdef"
                    for character in ref[5:]
                )
                for ref in refs
            ):
                raise ValueError(
                    "HARD_CURRENT availability requires fact digest references"
                )
            if self.reason is not None:
                raise ValueError(
                    "HARD_CURRENT availability cannot declare a warning reason"
                )
            fresh_until = _utc(
                fresh_until,
                "ActivityAvailability.fresh_until",
            )
        else:
            if self.intervals or fresh_until is not None:
                raise ValueError(
                    "NEEDS_VERIFICATION cannot carry hard intervals or freshness"
                )
            if self.reason is None:
                raise ValueError(
                    "NEEDS_VERIFICATION requires an explicit reason"
                )
        object.__setattr__(self, "fresh_until", fresh_until)

        ordered = sorted(
            self.intervals,
            key=lambda item: (item.start_at, item.end_at),
        )
        merged: list[AvailabilityInterval] = []
        for interval in ordered:
            if merged and interval.start_at <= merged[-1].end_at:
                merged[-1] = AvailabilityInterval(
                    merged[-1].start_at,
                    max(merged[-1].end_at, interval.end_at),
                )
            else:
                merged.append(interval)
        object.__setattr__(self, "intervals", tuple(merged))


__all__ = [
    "ActivityAvailability",
    "AvailabilityDisposition",
    "AvailabilityInterval",
]
