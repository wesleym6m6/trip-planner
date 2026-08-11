"""Read-only adapters from the existing trip JSON files to kernel models."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    Activity,
    CheckIssue,
    Constraint,
    ConstraintKind,
    ConstraintStrength,
    DaySpec,
    DecisionState,
    EvidenceState,
    Flexibility,
    IssueSeverity,
    Params,
    TimeWindow,
    TravelEstimate,
    TripState,
)


_DATE_RANGE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})\s*(?:~|–|—|to)\s*(\d{4}-\d{2}-\d{2})\s*$"
)
_ID_NAMESPACE = uuid.UUID("fbfb5d73-f626-4742-9840-ca22fab25846")
_EnumT = TypeVar("_EnumT")


class LoadError(ValueError):
    """Typed failure for malformed canonical or legacy source data."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: Path | None = None,
        json_path: str | None = None,
    ) -> None:
        self.code = code
        self.path = path
        self.json_path = json_path
        location = str(path) if path is not None else "<input>"
        if json_path:
            location = f"{location}:{json_path}"
        super().__init__(f"{code}: {message} ({location})")


def load_trip(path: str | Path) -> TripState:
    """Load the authoritative trip format, preferring canonical ``plan.json``.

    A present canonical file always wins.  If it is malformed or unsupported,
    its codec error is allowed to propagate instead of silently falling back to
    stale legacy files.
    """

    candidate = Path(path)
    if candidate.name == "plan.json":
        plan_path = candidate
    elif candidate.is_dir() and (
        (candidate / "plan.json").exists()
        or (candidate / "plan.json").is_symlink()
    ):
        plan_path = candidate / "plan.json"
    else:
        plan_path = candidate / "data" / "plan.json"

    if plan_path.exists() or plan_path.is_symlink():
        from .codec import load_plan, plan_to_trip_state

        return plan_to_trip_state(load_plan(plan_path))
    return load_legacy_trip(candidate)


def load_legacy_trip(path: str | Path) -> TripState:
    """Load one existing trip without modifying its files.

    ``path`` may point to a trip directory (containing ``data/``) or directly
    to its ``data`` directory.  Legacy activity IDs are synthesized
    deterministically.  Unsupported or stale *derived* travel edges are ignored
    with structured load issues; malformed core day/activity references raise
    :class:`LoadError`.
    """

    data_dir = _find_data_dir(Path(path))
    trip_path = data_dir / "trip.json"
    itinerary_path = data_dir / "itinerary.json"
    trip_raw, trip_bytes = _read_json_object(trip_path)
    itinerary_raw, itinerary_bytes = _read_json_object(itinerary_path)

    fallback_slug = (
        data_dir.parent.name if data_dir.name == "data" else data_dir.name
    )
    return _load_legacy_objects(
        trip_raw,
        itinerary_raw,
        trip_bytes=trip_bytes,
        itinerary_bytes=itinerary_bytes,
        trip_path=trip_path,
        itinerary_path=itinerary_path,
        fallback_slug=fallback_slug,
    )


def _load_legacy_values(
    trip_raw: dict[str, Any],
    itinerary_raw: dict[str, Any],
    *,
    trip_bytes: bytes,
    itinerary_bytes: bytes,
    fallback_slug: str,
) -> TripState:
    """Parse detached legacy-compatible values without filesystem access."""

    if (
        type(trip_raw) is not dict
        or type(itinerary_raw) is not dict
        or type(trip_bytes) is not bytes
        or type(itinerary_bytes) is not bytes
        or type(fallback_slug) is not str
        or not fallback_slug
    ):
        raise LoadError("MALFORMED_DATA", "in-memory legacy input is invalid")
    return _load_legacy_objects(
        trip_raw,
        itinerary_raw,
        trip_bytes=trip_bytes,
        itinerary_bytes=itinerary_bytes,
        trip_path=Path("<canonical-plan>/trip.json"),
        itinerary_path=Path("<canonical-plan>/itinerary.json"),
        fallback_slug=fallback_slug,
    )


def _load_legacy_objects(
    trip_raw: dict[str, Any],
    itinerary_raw: dict[str, Any],
    *,
    trip_bytes: bytes,
    itinerary_bytes: bytes,
    trip_path: Path,
    itinerary_path: Path,
    fallback_slug: str,
) -> TripState:
    """Shared parser for file-backed and detached compatibility values."""

    slug = _optional_text(trip_raw.get("slug"), trip_path, "$.slug")
    if slug is None:
        slug = fallback_slug
    if not slug:
        raise LoadError("MISSING_FIELD", "trip slug is required", path=trip_path)
    title = _optional_text(trip_raw.get("title"), trip_path, "$.title") or slug
    subtitle = _optional_text(trip_raw.get("subtitle"), trip_path, "$.subtitle") or ""
    cities = _text_tuple(trip_raw.get("cities", ()), trip_path, "$.cities")

    load_issues: list[CheckIssue] = []
    timezone = _load_timezone(trip_raw, trip_path, load_issues)
    itinerary_allowed_modes = _text_tuple(
        itinerary_raw.get(
            "available_modes", trip_raw.get("available_modes", ())
        ),
        itinerary_path,
        "$.available_modes",
    )

    raw_days = itinerary_raw.get("days")
    if not isinstance(raw_days, list):
        raise LoadError(
            "MALFORMED_DATA",
            "itinerary.days must be an array",
            path=itinerary_path,
            json_path="$.days",
        )
    if not raw_days:
        raise LoadError(
            "MALFORMED_DATA",
            "itinerary.days cannot be empty",
            path=itinerary_path,
            json_path="$.days",
        )

    days: list[DaySpec] = []
    activities: list[Activity] = []
    travel_estimates: list[TravelEstimate] = []
    seen_day_ids: set[str] = set()
    seen_activity_ids: set[str] = set()
    synthetic_activity_ids: list[str] = []
    missing_duration_ids: list[str] = []
    unverified_travel_refs: list[str] = []
    all_days_have_explicit_timezones = True

    for day_index, raw_day_value in enumerate(raw_days):
        day_path = f"$.days[{day_index}]"
        raw_day = _object(raw_day_value, itinerary_path, day_path)
        day_date = _parse_date(
            raw_day.get("date"), itinerary_path, f"{day_path}.date", required=True
        )
        assert day_date is not None
        day_number = raw_day.get("day", day_index)
        if isinstance(day_number, bool) or not isinstance(day_number, (int, str)):
            raise LoadError(
                "MALFORMED_DATA",
                "day must be an integer or string",
                path=itinerary_path,
                json_path=f"{day_path}.day",
            )
        explicit_day_id = raw_day.get("day_id", raw_day.get("id"))
        if explicit_day_id is None:
            day_id = f"{day_date.isoformat()}#day-{day_number}"
        else:
            day_id = _required_text(
                explicit_day_id, itinerary_path, f"{day_path}.day_id"
            )
        if day_id in seen_day_ids:
            raise LoadError(
                "DUPLICATE_ID",
                f"duplicate day ID {day_id!r}",
                path=itinerary_path,
                json_path=day_path,
            )
        seen_day_ids.add(day_id)

        raw_places = raw_day.get("places")
        if not isinstance(raw_places, list):
            raise LoadError(
                "MALFORMED_DATA",
                "day.places must be an array",
                path=itinerary_path,
                json_path=f"{day_path}.places",
            )

        day_activities: list[Activity] = []
        for order, raw_place_value in enumerate(raw_places):
            place_path = f"{day_path}.places[{order}]"
            raw_place = _object(raw_place_value, itinerary_path, place_path)
            title_value = raw_place.get("title", raw_place.get("display_name"))
            activity_title = _required_text(
                title_value, itinerary_path, f"{place_path}.title"
            )
            location_id = _location_id(slug, raw_place, activity_title)

            explicit_activity_id = raw_place.get(
                "activity_id", raw_place.get("id")
            )
            if explicit_activity_id is None:
                activity_id = _synthetic_id(
                    "activity",
                    slug,
                    day_id,
                    str(order),
                    activity_title,
                    location_id,
                )
                synthetic_activity_ids.append(activity_id)
            else:
                activity_id = _required_text(
                    explicit_activity_id,
                    itinerary_path,
                    f"{place_path}.activity_id",
                )
            if activity_id in seen_activity_ids:
                raise LoadError(
                    "DUPLICATE_ID",
                    f"duplicate activity ID {activity_id!r}",
                    path=itinerary_path,
                    json_path=place_path,
                )
            seen_activity_ids.add(activity_id)

            scheduled_start = _parse_time(
                raw_place.get("time"),
                itinerary_path,
                f"{place_path}.time",
                required=False,
            )
            duration_min = _optional_number(
                raw_place.get("duration_min"),
                itinerary_path,
                f"{place_path}.duration_min",
                positive=True,
            )
            if duration_min is None:
                missing_duration_ids.append(activity_id)

            allowed_windows = _parse_windows(
                raw_place.get("allowed_windows"),
                itinerary_path,
                f"{place_path}.allowed_windows",
            )
            decision_state = _enum_or_default(
                DecisionState,
                raw_place.get("decision_state"),
                DecisionState.SELECTED,
                itinerary_path,
                f"{place_path}.decision_state",
            )
            flexibility = _enum_or_default(
                Flexibility,
                raw_place.get("flexibility"),
                Flexibility.MOVABLE,
                itinerary_path,
                f"{place_path}.flexibility",
            )
            evidence_state = _enum_or_default(
                EvidenceState,
                raw_place.get("evidence_state"),
                EvidenceState.UNVERIFIED,
                itinerary_path,
                f"{place_path}.evidence_state",
            )
            priority_value = raw_place.get("priority", 0)
            if isinstance(priority_value, bool) or not isinstance(priority_value, int):
                raise LoadError(
                    "MALFORMED_DATA",
                    "activity priority must be an integer",
                    path=itinerary_path,
                    json_path=f"{place_path}.priority",
                )

            try:
                activity = Activity(
                    activity_id=activity_id,
                    day_id=day_id,
                    order=order,
                    title=activity_title,
                    location_id=location_id,
                    scheduled_start=scheduled_start,
                    duration_min=duration_min,
                    priority=priority_value,
                    decision_state=decision_state,
                    flexibility=flexibility,
                    evidence_state=evidence_state,
                    allowed_windows=allowed_windows,
                    kind=_optional_text(
                        raw_place.get("type"), itinerary_path, f"{place_path}.type"
                    )
                    or "activity",
                    note=_optional_text(
                        raw_place.get("note"), itinerary_path, f"{place_path}.note"
                    )
                    or "",
                    lat=_optional_number(
                        raw_place.get("lat"),
                        itinerary_path,
                        f"{place_path}.lat",
                        allow_negative=True,
                    ),
                    lng=_optional_number(
                        raw_place.get("lng"),
                        itinerary_path,
                        f"{place_path}.lng",
                        allow_negative=True,
                    ),
                    maps_query=_optional_text(
                        raw_place.get("maps_query"),
                        itinerary_path,
                        f"{place_path}.maps_query",
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise LoadError(
                    "MALFORMED_DATA",
                    str(exc),
                    path=itinerary_path,
                    json_path=place_path,
                ) from exc
            day_activities.append(activity)
            activities.append(activity)

        day_timezone = _optional_text(
            raw_day.get("timezone"), itinerary_path, f"{day_path}.timezone"
        )
        if day_timezone is None:
            all_days_have_explicit_timezones = False
        if day_timezone is not None and not _is_iana_timezone(day_timezone):
            all_days_have_explicit_timezones = False
            load_issues.append(
                CheckIssue(
                    code="DAY_TIMEZONE_INVALID",
                    severity=IssueSeverity.WARNING,
                    message=(
                        f"Day {day_id} timezone {day_timezone!r} is invalid; "
                        f"using trip timezone {timezone!r}."
                    ),
                    details=(
                        ("day_id", day_id),
                        ("fallback", timezone),
                    ),
                    suggested_fixes=("set_day_iana_timezone",),
                )
            )
            day_timezone = timezone
        day_timezone = day_timezone or timezone

        try:
            day = DaySpec(
                day_id=day_id,
                date=day_date,
                timezone=day_timezone,
                available_start=_parse_time(
                    raw_day.get("available_start"),
                    itinerary_path,
                    f"{day_path}.available_start",
                    required=False,
                ),
                available_end=_parse_time(
                    raw_day.get("available_end"),
                    itinerary_path,
                    f"{day_path}.available_end",
                    required=False,
                ),
                start_location_id=_optional_text(
                    raw_day.get("start_location_id"),
                    itinerary_path,
                    f"{day_path}.start_location_id",
                ),
                end_location_id=_optional_text(
                    raw_day.get("end_location_id"),
                    itinerary_path,
                    f"{day_path}.end_location_id",
                ),
                allowed_modes=_text_tuple(
                    raw_day.get("allowed_modes", itinerary_allowed_modes),
                    itinerary_path,
                    f"{day_path}.allowed_modes",
                ),
                activity_ids=tuple(a.activity_id for a in day_activities),
                title=_optional_text(
                    raw_day.get("title"), itinerary_path, f"{day_path}.title"
                )
                or "",
                subtitle=_optional_text(
                    raw_day.get("subtitle"), itinerary_path, f"{day_path}.subtitle"
                )
                or "",
            )
        except (TypeError, ValueError) as exc:
            raise LoadError(
                "MALFORMED_DATA",
                str(exc),
                path=itinerary_path,
                json_path=day_path,
            ) from exc
        days.append(day)

        raw_travel = raw_day.get("travel", [])
        if not isinstance(raw_travel, list):
            raise LoadError(
                "MALFORMED_DATA",
                "day.travel must be an array",
                path=itinerary_path,
                json_path=f"{day_path}.travel",
            )
        for edge_index, raw_edge_value in enumerate(raw_travel):
            edge_path = f"{day_path}.travel[{edge_index}]"
            raw_edge = _object(raw_edge_value, itinerary_path, edge_path)
            from_index = _index(raw_edge.get("from"), itinerary_path, f"{edge_path}.from")
            to_index = _index(raw_edge.get("to"), itinerary_path, f"{edge_path}.to")
            if not (
                0 <= from_index < len(day_activities)
                and 0 <= to_index < len(day_activities)
            ):
                load_issues.append(
                    CheckIssue(
                        code="INVALID_TRAVEL_REFERENCE",
                        severity=IssueSeverity.WARNING,
                        message=(
                            f"Ignored travel edge {from_index}->{to_index} on "
                            f"{day_id}: the day has {len(day_activities)} activities."
                        ),
                        evidence_refs=(f"{itinerary_path.name}#{edge_path}",),
                        details=(
                            ("from_index", from_index),
                            ("to_index", to_index),
                            ("activity_count", len(day_activities)),
                        ),
                        suggested_fixes=("rebuild_day_travel_edges",),
                    )
                )
                continue

            from_activity = day_activities[from_index]
            to_activity = day_activities[to_index]
            raw_modes = raw_edge.get("modes")
            if not isinstance(raw_modes, dict):
                raise LoadError(
                    "MALFORMED_DATA",
                    "travel.modes must be an object",
                    path=itinerary_path,
                    json_path=f"{edge_path}.modes",
                )
            if not raw_modes:
                load_issues.append(
                    CheckIssue(
                        code="MISSING_TRAVEL_ESTIMATE",
                        severity=IssueSeverity.WARNING,
                        message=f"No travel modes are available for edge {edge_path}.",
                        activity_ids=(
                            from_activity.activity_id,
                            to_activity.activity_id,
                        ),
                        evidence_refs=(f"{itinerary_path.name}#{edge_path}",),
                        suggested_fixes=("refresh_travel_estimate",),
                    )
                )
                continue

            source = _optional_text(
                raw_edge.get("source"), itinerary_path, f"{edge_path}.source"
            )
            recommended_mode = _optional_text(
                raw_edge.get("recommended_mode"),
                itinerary_path,
                f"{edge_path}.recommended_mode",
            )
            if recommended_mode is not None and recommended_mode not in raw_modes:
                load_issues.append(
                    CheckIssue(
                        code="RECOMMENDED_MODE_UNAVAILABLE",
                        severity=IssueSeverity.WARNING,
                        message=(
                            f"Ignored recommended mode {recommended_mode!r} for "
                            f"{edge_path}: it has no estimate."
                        ),
                        activity_ids=(
                            from_activity.activity_id,
                            to_activity.activity_id,
                        ),
                        evidence_refs=(f"{itinerary_path.name}#{edge_path}",),
                        details=(("recommended_mode", recommended_mode),),
                        suggested_fixes=("refresh_travel_estimate",),
                    )
                )
            for mode, raw_mode_value in raw_modes.items():
                if not isinstance(mode, str) or not mode.strip():
                    raise LoadError(
                        "MALFORMED_DATA",
                        "travel mode names must be non-empty strings",
                        path=itinerary_path,
                        json_path=f"{edge_path}.modes",
                    )
                mode_path = f"{edge_path}.modes.{mode}"
                raw_mode = _object(raw_mode_value, itinerary_path, mode_path)
                duration_min = _optional_number(
                    raw_mode.get("duration_min"),
                    itinerary_path,
                    f"{mode_path}.duration_min",
                    positive=False,
                )
                if duration_min is None:
                    load_issues.append(
                        CheckIssue(
                            code="MISSING_TRAVEL_ESTIMATE",
                            severity=IssueSeverity.WARNING,
                            message=f"Travel mode {mode!r} has no duration.",
                            activity_ids=(
                                from_activity.activity_id,
                                to_activity.activity_id,
                            ),
                            evidence_refs=(f"{itinerary_path.name}#{mode_path}",),
                            suggested_fixes=("refresh_travel_estimate",),
                        )
                    )
                    continue
                fresh_until = _parse_datetime(
                    raw_mode.get("fresh_until", raw_edge.get("fresh_until")),
                    itinerary_path,
                    f"{mode_path}.fresh_until",
                )
                evidence_state = _enum_or_default(
                    EvidenceState,
                    raw_mode.get(
                        "evidence_state", raw_edge.get("evidence_state")
                    ),
                    EvidenceState.UNVERIFIED,
                    itinerary_path,
                    f"{mode_path}.evidence_state",
                )
                evidence_ref = f"{itinerary_path.name}#{mode_path}"
                if evidence_state is not EvidenceState.VERIFIED:
                    unverified_travel_refs.append(evidence_ref)
                try:
                    travel_estimates.append(
                        TravelEstimate(
                            from_location_id=from_activity.location_id,
                            to_location_id=to_activity.location_id,
                            mode=mode,
                            duration_min=duration_min,
                            day_id=day_id,
                            buffer_min=_optional_number(
                                raw_mode.get(
                                    "buffer_min", raw_edge.get("buffer_min", 0)
                                ),
                                itinerary_path,
                                f"{mode_path}.buffer_min",
                                positive=False,
                            )
                            or 0.0,
                            distance_km=_optional_number(
                                raw_mode.get("distance_km"),
                                itinerary_path,
                                f"{mode_path}.distance_km",
                                positive=False,
                            ),
                            evidence_state=evidence_state,
                            fresh_until=fresh_until,
                            evidence_ref=evidence_ref,
                            source=source,
                            recommended=mode == recommended_mode,
                            from_activity_id=from_activity.activity_id,
                            to_activity_id=to_activity.activity_id,
                        )
                    )
                except (TypeError, ValueError) as exc:
                    raise LoadError(
                        "MALFORMED_DATA",
                        str(exc),
                        path=itinerary_path,
                        json_path=mode_path,
                    ) from exc

    if all_days_have_explicit_timezones:
        load_issues = [
            (
                replace(
                    issue,
                    severity=IssueSeverity.INFO,
                    message=(
                        f"{issue.message} Every day has an explicit valid "
                        "timezone, so the trip-level fallback is not used for "
                        "timeline evaluation."
                    ),
                )
                if issue.code == "TIMEZONE_FALLBACK"
                else issue
            )
            for issue in load_issues
        ]

    if synthetic_activity_ids:
        load_issues.append(
            CheckIssue(
                code="SYNTHETIC_ACTIVITY_IDS",
                severity=IssueSeverity.INFO,
                message=(
                    f"Synthesized deterministic IDs for "
                    f"{len(synthetic_activity_ids)} legacy activities."
                ),
                activity_ids=tuple(synthetic_activity_ids),
                details=(("count", len(synthetic_activity_ids)),),
                suggested_fixes=("persist_stable_activity_ids",),
            )
        )
    if missing_duration_ids:
        load_issues.append(
            CheckIssue(
                code="ACTIVITY_DURATION_UNVERIFIED",
                severity=IssueSeverity.WARNING,
                message=(
                    f"{len(missing_duration_ids)} activities have no service "
                    "duration; the timeline cannot yet prove feasibility."
                ),
                activity_ids=tuple(missing_duration_ids),
                details=(("count", len(missing_duration_ids)),),
                suggested_fixes=("estimate_or_verify_activity_durations",),
            )
        )
    if unverified_travel_refs:
        load_issues.append(
            CheckIssue(
                code="TRAVEL_EVIDENCE_UNVERIFIED",
                severity=IssueSeverity.WARNING,
                message=(
                    f"{len(unverified_travel_refs)} travel estimates lack an "
                    "explicit verified evidence state."
                ),
                evidence_refs=tuple(unverified_travel_refs),
                details=(("count", len(unverified_travel_refs)),),
                suggested_fixes=("refresh_travel_estimates",),
            )
        )

    start_date, end_date = _trip_dates(trip_raw, trip_path, days, load_issues)
    constraints = _load_constraints(
        trip_raw.get("constraints", ()), trip_path, seen_activity_ids, seen_day_ids
    )
    revision = hashlib.sha256(
        trip_bytes + b"\0itinerary\0" + itinerary_bytes
    ).hexdigest()

    try:
        return TripState(
            slug=slug,
            title=title,
            timezone=timezone,
            days=tuple(days),
            activities=tuple(activities),
            travel_estimates=tuple(travel_estimates),
            constraints=constraints,
            load_issues=tuple(load_issues),
            revision=revision,
            start_date=start_date,
            end_date=end_date,
            subtitle=subtitle,
            cities=cities,
        )
    except (TypeError, ValueError) as exc:
        raise LoadError(
            "MALFORMED_REFERENCE",
            str(exc),
            path=itinerary_path,
        ) from exc


def _find_data_dir(path: Path) -> Path:
    if path.is_dir() and (path / "trip.json").is_file():
        data_dir = path
    elif path.is_dir() and (path / "data" / "trip.json").is_file():
        data_dir = path / "data"
    else:
        raise LoadError(
            "MISSING_DATA",
            "expected a data directory containing trip.json and itinerary.json",
            path=path,
        )
    if not (data_dir / "itinerary.json").is_file():
        raise LoadError(
            "MISSING_DATA", "itinerary.json is missing", path=data_dir / "itinerary.json"
        )
    return data_dir


def _read_json_object(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise LoadError("READ_ERROR", str(exc), path=path) from exc
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LoadError("INVALID_JSON", str(exc), path=path) from exc
    if not isinstance(value, dict):
        raise LoadError("MALFORMED_DATA", "root must be an object", path=path)
    return value, data


def _load_timezone(
    trip_raw: dict[str, Any], path: Path, issues: list[CheckIssue]
) -> str:
    value = _optional_text(trip_raw.get("timezone"), path, "$.timezone")
    if value and _is_iana_timezone(value):
        return value
    if value:
        reason = "invalid"
        message = f"Trip timezone {value!r} is not a valid IANA timezone; using UTC."
    else:
        reason = "missing"
        message = "Trip has no IANA timezone; using UTC until it is supplied."
    issues.append(
        CheckIssue(
            code="TIMEZONE_FALLBACK",
            severity=IssueSeverity.WARNING,
            message=message,
            details=(("reason", reason), ("fallback", "UTC")),
            suggested_fixes=("set_trip_iana_timezone",),
        )
    )
    return "UTC"


def _is_iana_timezone(value: str) -> bool:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _trip_dates(
    trip_raw: dict[str, Any],
    path: Path,
    days: list[DaySpec],
    issues: list[CheckIssue],
) -> tuple[date, date]:
    day_start = min(day.date for day in days)
    day_end = max(day.date for day in days)
    raw_range = trip_raw.get("date_range")
    if raw_range is None:
        issues.append(
            CheckIssue(
                code="DATE_RANGE_MISSING",
                severity=IssueSeverity.WARNING,
                message="trip.date_range is missing; derived it from explicit day dates.",
                details=(
                    ("derived_start", day_start.isoformat()),
                    ("derived_end", day_end.isoformat()),
                ),
                suggested_fixes=("set_trip_date_range",),
            )
        )
        return day_start, day_end
    text = _required_text(raw_range, path, "$.date_range")
    match = _DATE_RANGE.fullmatch(text)
    if not match:
        raise LoadError(
            "MALFORMED_DATA",
            "date_range must be 'YYYY-MM-DD ~ YYYY-MM-DD'",
            path=path,
            json_path="$.date_range",
        )
    try:
        start = date.fromisoformat(match.group(1))
        end = date.fromisoformat(match.group(2))
    except ValueError as exc:
        raise LoadError(
            "MALFORMED_DATA", str(exc), path=path, json_path="$.date_range"
        ) from exc
    if end < start:
        raise LoadError(
            "MALFORMED_DATA",
            "date_range end precedes start",
            path=path,
            json_path="$.date_range",
        )
    if day_start < start or day_end > end:
        issues.append(
            CheckIssue(
                code="DAY_OUTSIDE_TRIP_RANGE",
                severity=IssueSeverity.WARNING,
                message="One or more explicit day dates fall outside trip.date_range.",
                details=(
                    ("trip_start", start.isoformat()),
                    ("trip_end", end.isoformat()),
                    ("day_start", day_start.isoformat()),
                    ("day_end", day_end.isoformat()),
                ),
                suggested_fixes=("reconcile_trip_and_day_dates",),
            )
        )
    return start, end


def _load_constraints(
    raw_value: Any,
    path: Path,
    activity_ids: set[str],
    day_ids: set[str],
) -> tuple[Constraint, ...]:
    if raw_value in (None, ()):
        return ()
    if not isinstance(raw_value, list):
        raise LoadError(
            "MALFORMED_DATA",
            "constraints must be an array",
            path=path,
            json_path="$.constraints",
        )
    constraints: list[Constraint] = []
    known_subjects = activity_ids | day_ids
    seen_ids: set[str] = set()
    for index, raw_constraint_value in enumerate(raw_value):
        json_path = f"$.constraints[{index}]"
        raw_constraint = _object(raw_constraint_value, path, json_path)
        constraint_id = _optional_text(
            raw_constraint.get("constraint_id", raw_constraint.get("id")),
            path,
            f"{json_path}.constraint_id",
        ) or _synthetic_id("constraint", str(index), repr(raw_constraint))
        if constraint_id in seen_ids:
            raise LoadError(
                "DUPLICATE_ID",
                f"duplicate constraint ID {constraint_id!r}",
                path=path,
                json_path=json_path,
            )
        seen_ids.add(constraint_id)
        kind = _enum_or_default(
            ConstraintKind,
            raw_constraint.get("kind"),
            None,
            path,
            f"{json_path}.kind",
        )
        strength = _enum_or_default(
            ConstraintStrength,
            raw_constraint.get("strength"),
            ConstraintStrength.HARD,
            path,
            f"{json_path}.strength",
        )
        subject_ids = _text_tuple(
            raw_constraint.get("subject_ids", ()),
            path,
            f"{json_path}.subject_ids",
        )
        dangling = tuple(subject for subject in subject_ids if subject not in known_subjects)
        if dangling:
            raise LoadError(
                "MALFORMED_REFERENCE",
                f"constraint refers to unknown subjects: {', '.join(dangling)}",
                path=path,
                json_path=f"{json_path}.subject_ids",
            )
        params = _params(
            raw_constraint.get("params", {}), path, f"{json_path}.params"
        )
        try:
            constraints.append(
                Constraint(
                    constraint_id=constraint_id,
                    kind=kind,
                    strength=strength,
                    subject_ids=subject_ids,
                    params=params,
                    origin=_optional_text(
                        raw_constraint.get("origin"), path, f"{json_path}.origin"
                    )
                    or "user",
                    confidence=_optional_number(
                        raw_constraint.get("confidence", 1.0),
                        path,
                        f"{json_path}.confidence",
                    )
                    or 0.0,
                    source_text=_optional_text(
                        raw_constraint.get("source_text"),
                        path,
                        f"{json_path}.source_text",
                    ),
                )
            )
        except (TypeError, ValueError) as exc:
            raise LoadError(
                "MALFORMED_DATA", str(exc), path=path, json_path=json_path
            ) from exc
    return tuple(constraints)


def _location_id(slug: str, raw_place: dict[str, Any], title: str) -> str:
    explicit = raw_place.get("location_id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    place_id = raw_place.get("place_id")
    if isinstance(place_id, str) and place_id.strip():
        return f"google-place:{place_id.strip()}"
    lat = raw_place.get("lat")
    lng = raw_place.get("lng")
    if (
        not isinstance(lat, bool)
        and isinstance(lat, (int, float))
        and not isinstance(lng, bool)
        and isinstance(lng, (int, float))
    ):
        identity = f"{float(lat):.6f},{float(lng):.6f}"
    else:
        query = raw_place.get("maps_query")
        identity = query if isinstance(query, str) and query.strip() else title
        identity = " ".join(identity.casefold().split())
    return _synthetic_id("location", slug, identity)


def _synthetic_id(prefix: str, *parts: str) -> str:
    token = "\0".join(parts)
    return f"{prefix}-{uuid.uuid5(_ID_NAMESPACE, token).hex}"


def _parse_windows(value: Any, path: Path, json_path: str) -> tuple[TimeWindow, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise LoadError(
            "MALFORMED_DATA",
            "allowed_windows must be an array",
            path=path,
            json_path=json_path,
        )
    windows: list[TimeWindow] = []
    for index, raw_window_value in enumerate(value):
        window_path = f"{json_path}[{index}]"
        raw_window = _object(raw_window_value, path, window_path)
        start = _parse_time(
            raw_window.get("start"), path, f"{window_path}.start", required=True
        )
        end = _parse_time(
            raw_window.get("end"), path, f"{window_path}.end", required=True
        )
        assert start is not None and end is not None
        windows.append(TimeWindow(start=start, end=end))
    return tuple(windows)


def _parse_date(
    value: Any, path: Path, json_path: str, *, required: bool
) -> date | None:
    if value is None and not required:
        return None
    text = _required_text(value, path, json_path)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise LoadError(
            "MALFORMED_DATA", str(exc), path=path, json_path=json_path
        ) from exc


def _parse_time(
    value: Any, path: Path, json_path: str, *, required: bool
) -> time | None:
    if value is None and not required:
        return None
    text = _required_text(value, path, json_path)
    try:
        result = time.fromisoformat(text)
    except ValueError as exc:
        raise LoadError(
            "MALFORMED_DATA", str(exc), path=path, json_path=json_path
        ) from exc
    if result.tzinfo is not None:
        raise LoadError(
            "MALFORMED_DATA",
            "time values must be timezone-naive local times",
            path=path,
            json_path=json_path,
        )
    return result


def _parse_datetime(value: Any, path: Path, json_path: str) -> datetime | None:
    if value is None:
        return None
    text = _required_text(value, path, json_path)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise LoadError(
            "MALFORMED_DATA", str(exc), path=path, json_path=json_path
        ) from exc


def _enum_or_default(
    enum_type: type[_EnumT],
    value: Any,
    default: _EnumT | None,
    path: Path,
    json_path: str,
) -> _EnumT:
    if value is None:
        if default is None:
            raise LoadError(
                "MISSING_FIELD",
                f"{json_path.rsplit('.', 1)[-1]} is required",
                path=path,
                json_path=json_path,
            )
        return default
    if not isinstance(value, str):
        raise LoadError(
            "MALFORMED_DATA",
            "enum value must be a string",
            path=path,
            json_path=json_path,
        )
    try:
        return enum_type(value)  # type: ignore[call-arg]
    except ValueError as exc:
        valid = ", ".join(member.value for member in enum_type)  # type: ignore[attr-defined]
        raise LoadError(
            "MALFORMED_DATA",
            f"invalid value {value!r}; expected one of: {valid}",
            path=path,
            json_path=json_path,
        ) from exc


def _object(value: Any, path: Path, json_path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LoadError(
            "MALFORMED_DATA",
            "expected an object",
            path=path,
            json_path=json_path,
        )
    return value


def _index(value: Any, path: Path, json_path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LoadError(
            "MALFORMED_DATA",
            "travel endpoint must be an integer index",
            path=path,
            json_path=json_path,
        )
    return value


def _required_text(value: Any, path: Path, json_path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LoadError(
            "MISSING_FIELD",
            "expected a non-empty string",
            path=path,
            json_path=json_path,
        )
    return value.strip()


def _optional_text(value: Any, path: Path, json_path: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, path, json_path)


def _text_tuple(value: Any, path: Path, json_path: str) -> tuple[str, ...]:
    if value in (None, ()):
        return ()
    if not isinstance(value, (list, tuple)):
        raise LoadError(
            "MALFORMED_DATA", "expected an array", path=path, json_path=json_path
        )
    return tuple(
        _required_text(item, path, f"{json_path}[{index}]")
        for index, item in enumerate(value)
    )


def _optional_number(
    value: Any,
    path: Path,
    json_path: str,
    *,
    positive: bool = False,
    allow_negative: bool = False,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LoadError(
            "MALFORMED_DATA",
            "expected a number",
            path=path,
            json_path=json_path,
        )
    number = float(value)
    if not math.isfinite(number):
        raise LoadError(
            "MALFORMED_DATA",
            "expected a finite number",
            path=path,
            json_path=json_path,
        )
    if positive and number <= 0:
        raise LoadError(
            "MALFORMED_DATA",
            "expected a number greater than zero",
            path=path,
            json_path=json_path,
        )
    if not positive and not allow_negative and number < 0:
        raise LoadError(
            "MALFORMED_DATA",
            "expected a non-negative number",
            path=path,
            json_path=json_path,
        )
    return number


def _params(value: Any, path: Path, json_path: str) -> Params:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise LoadError(
            "MALFORMED_DATA",
            "constraint params must be an object",
            path=path,
            json_path=json_path,
        )
    params: list[tuple[str, str | int | float | bool | None]] = []
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise LoadError(
                "MALFORMED_DATA",
                "constraint param keys must be non-empty strings",
                path=path,
                json_path=json_path,
            )
        if item is not None and not isinstance(item, (str, int, float, bool)):
            raise LoadError(
                "MALFORMED_DATA",
                "constraint param values must be scalar",
                path=path,
                json_path=f"{json_path}.{key}",
            )
        params.append((key, item))
    return tuple(params)
