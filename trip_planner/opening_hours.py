"""Pure opening-hours window evaluation.

This module deliberately knows nothing about providers or persistence.  A
caller supplies date-specific, timezone-aware instants; regular weekly
schedules are not evidence that a future visit is open.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable


@dataclass(frozen=True, slots=True)
class OpeningHoursEvaluation:
    """Result for one proposed visit window."""

    status: str  # ``open``, ``closed``, or ``unknown``
    visit_end: datetime | None
    matching_interval: tuple[datetime, datetime] | None = None


def evaluate_opening_window(
    intervals: Iterable[tuple[datetime, datetime]] | None,
    arrival_at: datetime,
    duration: timedelta,
) -> OpeningHoursEvaluation:
    """Evaluate ``[arrival_at, arrival_at + duration)`` against open windows.

    Both visit and opening windows are half-open.  A visit ending exactly at a
    closing instant is valid, but a visit beginning at that instant is not.
    One continuous opening interval must cover the complete visit; adjacent or
    split shifts are never silently stitched together.
    """

    _require_aware(arrival_at, "arrival_at")
    if not isinstance(duration, timedelta) or duration <= timedelta(0):
        raise ValueError("duration must be a positive timedelta")
    visit_end = arrival_at + duration
    if intervals is None:
        return OpeningHoursEvaluation("unknown", visit_end)

    normalized: list[tuple[datetime, datetime]] = []
    for item in intervals:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError("opening interval must be a (start, end) tuple")
        start, end = item
        _require_aware(start, "opening interval start")
        _require_aware(end, "opening interval end")
        if end <= start:
            raise ValueError("opening interval must have positive duration")
        normalized.append((start, end))

    for start, end in normalized:
        if start <= arrival_at and visit_end <= end:
            return OpeningHoursEvaluation("open", visit_end, (start, end))
    return OpeningHoursEvaluation("closed", visit_end)


def _require_aware(value: datetime, name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be timezone-aware")


__all__ = ["OpeningHoursEvaluation", "evaluate_opening_window"]
