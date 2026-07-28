"""Compatibility boundary between canonical plans and legacy scripts.

Read-only legacy consumers can use :func:`load_trip_views` without knowing
whether a trip still uses ``trip.json``/``itinerary.json`` or has migrated to
``plan.json``.  Legacy writers must call :func:`refuse_canonical_write` before
performing provider calls or writes so they cannot bypass PlanPatch safety.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trip_planner.codec import (  # noqa: E402
    PlanCodecError,
    legacy_compatibility_views,
    load_plan,
)


class CanonicalWriteRefused(RuntimeError):
    """Raised when a legacy writer targets a migrated canonical trip."""


def resolve_ordered_local_datetimes(
    day_date: date | datetime | str | None,
    local_time_values: Iterable[object],
    *,
    available_start: object = None,
    available_end: object = None,
) -> tuple[datetime | None, ...]:
    """Resolve ordered local clocks with deterministic midnight rollover."""

    if isinstance(day_date, datetime):
        base_date = day_date.date()
    elif isinstance(day_date, date):
        base_date = day_date
    elif isinstance(day_date, str):
        try:
            base_date = date.fromisoformat(day_date)
        except ValueError:
            base_date = None
    else:
        base_date = None

    values = tuple(local_time_values)
    if base_date is None:
        return tuple(None for _ in values)

    start = _local_time_value(available_start)
    end = _local_time_value(available_end)
    crosses_midnight = (
        start is not None and end is not None and end <= start
    )
    rollover_days = 0
    previous: datetime | None = None
    resolved: list[datetime | None] = []
    for value in values:
        parsed = _local_time_value(value)
        if parsed is None:
            resolved.append(None)
            continue
        if (
            previous is None
            and crosses_midnight
            and start is not None
            and parsed < start
        ):
            rollover_days = 1
        candidate = datetime.combine(
            base_date + timedelta(days=rollover_days), parsed
        )
        while previous is not None and candidate < previous:
            rollover_days += 1
            candidate = datetime.combine(
                base_date + timedelta(days=rollover_days), parsed
            )
        resolved.append(candidate)
        previous = candidate
    return tuple(resolved)


def _local_time_value(value: object) -> time | None:
    if isinstance(value, time):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = time.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is None else None


def _as_data_dir(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.name == "plan.json" or candidate.suffix == ".json":
        return candidate.parent
    if candidate.name == "data":
        return candidate
    if (candidate / "data").is_dir() or (candidate / "data" / "plan.json").is_symlink():
        return candidate / "data"
    return candidate


def has_canonical_plan(path: str | Path) -> bool:
    """Return whether ``path`` identifies data containing a canonical plan.

    A broken symlink still counts as canonical presence.  Callers must fail
    closed instead of silently falling back to legacy files.
    """

    plan_path = _as_data_dir(path) / "plan.json"
    return plan_path.exists() or plan_path.is_symlink()


def load_trip_views(
    path: str | Path,
) -> tuple[dict[str, Any], dict[str, Any], str | None, str | None]:
    """Load detached legacy-compatible trip and itinerary views.

    Canonical ``plan.json`` always wins when present.  Its strict codec errors
    propagate and never trigger a legacy fallback.  Unmigrated trips preserve
    the historical JSON loading behavior and return ``None`` identity metadata.
    """

    data_dir = _as_data_dir(path)
    plan_path = data_dir / "plan.json"
    if has_canonical_plan(data_dir):
        plan = load_plan(plan_path)
        trip, itinerary = legacy_compatibility_views(plan)
        trip_id = plan["trip_id"]
        revision = plan["revision"]
        assert isinstance(trip_id, str)
        assert isinstance(revision, str)
        return trip, itinerary, trip_id, revision

    trip = json.loads((data_dir / "trip.json").read_text(encoding="utf-8"))
    itinerary = json.loads(
        (data_dir / "itinerary.json").read_text(encoding="utf-8")
    )
    return trip, itinerary, None, None


def refuse_canonical_write(
    path: str | Path, *, operation: str = "legacy writer"
) -> None:
    """Refuse a direct legacy write whenever ``plan.json`` is present."""

    data_dir = _as_data_dir(path)
    plan_path = data_dir / "plan.json"
    if has_canonical_plan(data_dir):
        raise CanonicalWriteRefused(
            f"{operation} cannot modify canonical trip {plan_path}; "
            "submit a validated PlanPatch instead"
        )


__all__ = [
    "CanonicalWriteRefused",
    "PlanCodecError",
    "has_canonical_plan",
    "load_trip_views",
    "refuse_canonical_write",
    "resolve_ordered_local_datetimes",
]
