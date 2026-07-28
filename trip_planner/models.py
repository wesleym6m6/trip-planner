"""Immutable domain models for the deterministic planning kernel.

The models deliberately contain no provider, persistence, or optimization
behavior.  They are the small contract shared by loaders, validators, timeline
simulation, and any AI-facing orchestration layer.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from enum import Enum
from types import MappingProxyType
from typing import Mapping, TypeAlias


Scalar: TypeAlias = str | int | float | bool | None
Params: TypeAlias = tuple[tuple[str, Scalar], ...]
_MACHINE_NAME_RE = re.compile(r"[a-z][a-z0-9._/-]{0,127}")


class DecisionState(str, Enum):
    """Where an activity sits in the user's decision process."""

    CANDIDATE = "candidate"
    SELECTED = "selected"
    FIXED = "fixed"
    BOOKED = "booked"
    CANCELLED = "cancelled"
    EXCLUDED = "excluded"


class Flexibility(str, Enum):
    """How freely a planner may move an activity."""

    MOVABLE = "movable"
    FIXED_DAY = "fixed_day"
    FIXED_TIME = "fixed_time"


class EvidenceState(str, Enum):
    """Whether a mutable travel fact is safe to rely on."""

    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    STALE = "stale"
    CONFLICTED = "conflicted"


class ConstraintStrength(str, Enum):
    """Whether a constraint is mandatory or an optimization preference."""

    HARD = "hard"
    SOFT = "soft"


class ConstraintKind(str, Enum):
    """Small, intentionally non-general constraint vocabulary."""

    MUST_INCLUDE = "must_include"
    EXACTLY_ONCE = "exactly_once"
    AT_MOST_ONCE = "at_most_once"
    FIXED_TIME = "fixed_time"
    ALLOWED_WINDOW = "allowed_window"
    ALLOWED_DAY = "allowed_day"
    BEFORE = "before"
    REQUIRES = "requires"
    CHOOSE_N = "choose_n"
    ALLOWED_MODE = "allowed_mode"
    DAILY_LIMIT = "daily_limit"
    LOCATION_CONTINUITY = "location_continuity"


class CheckStatus(str, Enum):
    """Outcome of checking a proposed plan against known evidence."""

    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    NEEDS_VERIFICATION = "needs_verification"


class IssueSeverity(str, Enum):
    """Machine-readable severity for a check or loading issue."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """A local-time window.

    ``end <= start`` is intentionally allowed and means that the window crosses
    midnight.  The owning day and its IANA timezone provide date semantics.
    """

    start: time
    end: time

    def __post_init__(self) -> None:
        if not isinstance(self.start, time) or not isinstance(self.end, time):
            raise TypeError("TimeWindow start and end must be datetime.time values")
        if self.start.tzinfo is not None or self.end.tzinfo is not None:
            raise ValueError("TimeWindow values must be timezone-naive local times")
        if self.start.fold or self.end.fold:
            raise ValueError(
                "TimeWindow cannot encode PEP 495 fold in persisted local ISO time"
            )

    @property
    def spans_midnight(self) -> bool:
        return self.end <= self.start


@dataclass(frozen=True, slots=True)
class DaySpec:
    """Planning boundaries and ordered activity membership for one local day."""

    day_id: str
    date: date
    timezone: str | None = None
    available_start: time | None = None
    available_end: time | None = None
    start_location_id: str | None = None
    end_location_id: str | None = None
    allowed_modes: tuple[str, ...] = ()
    activity_ids: tuple[str, ...] = ()
    title: str = ""
    subtitle: str = ""

    def __post_init__(self) -> None:
        _require_text(self.day_id, "DaySpec.day_id")
        if not isinstance(self.date, date) or isinstance(self.date, datetime):
            raise TypeError("DaySpec.date must be datetime.date")
        if self.timezone is not None:
            _require_text(self.timezone, "DaySpec.timezone")
        _optional_local_time(self.available_start, "DaySpec.available_start")
        _optional_local_time(self.available_end, "DaySpec.available_end")
        if (
            self.available_start is not None
            and self.available_end is not None
            and self.available_start == self.available_end
        ):
            raise ValueError(
                "DaySpec availability start and end cannot be equal"
            )
        if self.start_location_id is not None:
            _require_text(self.start_location_id, "DaySpec.start_location_id")
        if self.end_location_id is not None:
            _require_text(self.end_location_id, "DaySpec.end_location_id")
        _require_tuple(self.allowed_modes, "DaySpec.allowed_modes")
        _require_tuple(self.activity_ids, "DaySpec.activity_ids")
        if len(set(self.activity_ids)) != len(self.activity_ids):
            raise ValueError(f"DaySpec {self.day_id!r} contains duplicate activity IDs")
        for activity_id in self.activity_ids:
            _require_text(activity_id, "DaySpec.activity_ids item")
        for mode in self.allowed_modes:
            _require_text(mode, "DaySpec.allowed_modes item")


@dataclass(frozen=True, slots=True)
class Activity:
    """A proposed visit or fixed event in the canonical plan."""

    activity_id: str
    day_id: str
    order: int
    title: str
    location_id: str
    scheduled_start: time | None = None
    duration_min: float | None = None
    priority: int = 0
    decision_state: DecisionState = DecisionState.SELECTED
    flexibility: Flexibility = Flexibility.MOVABLE
    evidence_state: EvidenceState = EvidenceState.UNVERIFIED
    allowed_windows: tuple[TimeWindow, ...] = ()
    kind: str = "activity"
    note: str = ""
    lat: float | None = None
    lng: float | None = None
    maps_query: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.activity_id, "Activity.activity_id")
        _require_text(self.day_id, "Activity.day_id")
        _require_text(self.title, "Activity.title")
        _require_text(self.location_id, "Activity.location_id")
        if isinstance(self.order, bool) or not isinstance(self.order, int):
            raise TypeError("Activity.order must be an integer")
        if self.order < 0:
            raise ValueError("Activity.order cannot be negative")
        _optional_local_time(self.scheduled_start, "Activity.scheduled_start")
        if self.duration_min is not None:
            _non_negative_number(self.duration_min, "Activity.duration_min", positive=True)
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise TypeError("Activity.priority must be an integer")
        if not isinstance(self.decision_state, DecisionState):
            raise TypeError("Activity.decision_state must be DecisionState")
        if not isinstance(self.flexibility, Flexibility):
            raise TypeError("Activity.flexibility must be Flexibility")
        if not isinstance(self.evidence_state, EvidenceState):
            raise TypeError("Activity.evidence_state must be EvidenceState")
        if (
            self.flexibility is Flexibility.FIXED_TIME
            and self.scheduled_start is None
        ):
            raise ValueError("FIXED_TIME activity requires scheduled_start")
        _require_tuple(self.allowed_windows, "Activity.allowed_windows")
        for window in self.allowed_windows:
            if not isinstance(window, TimeWindow):
                raise TypeError("Activity.allowed_windows items must be TimeWindow")
        _require_text(self.kind, "Activity.kind")
        if not isinstance(self.note, str):
            raise TypeError("Activity.note must be a string")
        _optional_coordinate(self.lat, -90.0, 90.0, "Activity.lat")
        _optional_coordinate(self.lng, -180.0, 180.0, "Activity.lng")
        if self.maps_query is not None and not isinstance(self.maps_query, str):
            raise TypeError("Activity.maps_query must be a string or None")

    @property
    def id(self) -> str:
        """Concise alias useful to generic planner code."""

        return self.activity_id


@dataclass(frozen=True, slots=True)
class TravelEstimate:
    """One mode-specific estimate between stable locations."""

    from_location_id: str
    to_location_id: str
    mode: str
    duration_min: float
    day_id: str | None = None
    buffer_min: float = 0.0
    distance_km: float | None = None
    evidence_state: EvidenceState = EvidenceState.UNVERIFIED
    fresh_until: datetime | None = None
    evidence_ref: str | None = None
    source: str | None = None
    recommended: bool = False
    from_activity_id: str | None = None
    to_activity_id: str | None = None
    query_departure_at: datetime | None = None
    query_arrival_at: datetime | None = None
    static_duration_min: float | None = None
    fallback_from_mode: str | None = None
    warning_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.from_location_id, "TravelEstimate.from_location_id")
        _require_text(self.to_location_id, "TravelEstimate.to_location_id")
        _require_text(self.mode, "TravelEstimate.mode")
        if self.day_id is not None:
            _require_text(self.day_id, "TravelEstimate.day_id")
        _non_negative_number(
            self.duration_min, "TravelEstimate.duration_min", positive=False
        )
        _non_negative_number(self.buffer_min, "TravelEstimate.buffer_min", positive=False)
        if self.distance_km is not None:
            _non_negative_number(
                self.distance_km, "TravelEstimate.distance_km", positive=False
            )
        if self.static_duration_min is not None:
            _non_negative_number(
                self.static_duration_min,
                "TravelEstimate.static_duration_min",
                positive=False,
            )
        if self.fallback_from_mode is not None:
            _require_text(
                self.fallback_from_mode,
                "TravelEstimate.fallback_from_mode",
            )
            if (
                self.fallback_from_mode != "transit"
                or self.mode != "driving"
            ):
                raise ValueError(
                    "TravelEstimate fallback only supports transit to driving"
                )
        _require_tuple(self.warning_codes, "TravelEstimate.warning_codes")
        seen_warning_codes: set[str] = set()
        for warning_code in self.warning_codes:
            if not isinstance(warning_code, str):
                raise TypeError(
                    "TravelEstimate.warning_codes items must be strings"
                )
            if _MACHINE_NAME_RE.fullmatch(warning_code) is None:
                raise ValueError(
                    "TravelEstimate.warning_codes items must be lowercase "
                    "machine names"
                )
            if warning_code in seen_warning_codes:
                raise ValueError(
                    "TravelEstimate.warning_codes cannot contain duplicates"
                )
            seen_warning_codes.add(warning_code)
        object.__setattr__(
            self,
            "warning_codes",
            tuple(sorted(self.warning_codes)),
        )
        if self.fresh_until is not None and not isinstance(self.fresh_until, datetime):
            raise TypeError("TravelEstimate.fresh_until must be datetime or None")
        if not isinstance(self.evidence_state, EvidenceState):
            raise TypeError("TravelEstimate.evidence_state must be EvidenceState")
        if self.evidence_ref is not None:
            _require_text(self.evidence_ref, "TravelEstimate.evidence_ref")
        if self.source is not None:
            _require_text(self.source, "TravelEstimate.source")
        if not isinstance(self.recommended, bool):
            raise TypeError("TravelEstimate.recommended must be bool")
        if self.from_activity_id is not None:
            _require_text(
                self.from_activity_id, "TravelEstimate.from_activity_id"
            )
        if self.to_activity_id is not None:
            _require_text(self.to_activity_id, "TravelEstimate.to_activity_id")
        for name, value in (
            ("query_departure_at", self.query_departure_at),
            ("query_arrival_at", self.query_arrival_at),
        ):
            if value is None:
                continue
            if not isinstance(value, datetime):
                raise TypeError(f"TravelEstimate.{name} must be datetime or None")
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(
                    f"TravelEstimate.{name} must be timezone-aware"
                )
        if (
            self.query_departure_at is not None
            and self.query_arrival_at is not None
        ):
            raise ValueError(
                "TravelEstimate can bind at most one route query timestamp"
            )


@dataclass(frozen=True, slots=True)
class Constraint:
    """A typed rule or preference supplied to a planner."""

    constraint_id: str
    kind: ConstraintKind
    strength: ConstraintStrength
    subject_ids: tuple[str, ...] = ()
    params: Params = ()
    origin: str = "user"
    confidence: float = 1.0
    source_text: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.constraint_id, "Constraint.constraint_id")
        if not isinstance(self.kind, ConstraintKind):
            raise TypeError("Constraint.kind must be ConstraintKind")
        if not isinstance(self.strength, ConstraintStrength):
            raise TypeError("Constraint.strength must be ConstraintStrength")
        _require_tuple(self.subject_ids, "Constraint.subject_ids")
        _require_tuple(self.params, "Constraint.params")
        for subject_id in self.subject_ids:
            _require_text(subject_id, "Constraint.subject_ids item")
        if len(set(self.subject_ids)) != len(self.subject_ids):
            raise ValueError("Constraint.subject_ids must be unique")
        _validate_params(self.params, "Constraint.params")
        object.__setattr__(
            self,
            "params",
            tuple(sorted(self.params, key=lambda item: item[0])),
        )
        if isinstance(self.confidence, bool) or not isinstance(
            self.confidence, (int, float)
        ):
            raise TypeError("Constraint.confidence must be numeric")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("Constraint.confidence must be between 0 and 1")

    def param(self, name: str, default: Scalar = None) -> Scalar:
        """Return an immutable parameter value by name."""

        for key, value in self.params:
            if key == name:
                return value
        return default


@dataclass(frozen=True, slots=True)
class CheckIssue:
    """A structured, stable explanation that an AI can repair against."""

    code: str
    severity: IssueSeverity
    message: str
    activity_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    details: Params = ()
    suggested_fixes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.code, "CheckIssue.code")
        _require_text(self.message, "CheckIssue.message")
        if not isinstance(self.severity, IssueSeverity):
            raise TypeError("CheckIssue.severity must be IssueSeverity")
        _require_tuple(self.activity_ids, "CheckIssue.activity_ids")
        _require_tuple(self.evidence_refs, "CheckIssue.evidence_refs")
        _require_tuple(self.details, "CheckIssue.details")
        _require_tuple(self.suggested_fixes, "CheckIssue.suggested_fixes")
        for activity_id in self.activity_ids:
            _require_text(activity_id, "CheckIssue.activity_ids item")
        for evidence_ref in self.evidence_refs:
            _require_text(evidence_ref, "CheckIssue.evidence_refs item")
        _validate_params(self.details, "CheckIssue.details")
        object.__setattr__(
            self,
            "details",
            tuple(sorted(self.details, key=lambda item: item[0])),
        )
        for suggested_fix in self.suggested_fixes:
            _require_text(suggested_fix, "CheckIssue.suggested_fixes item")


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """One fully simulated activity interval."""

    activity_id: str
    day_id: str
    location_id: str
    arrival_at: datetime
    start_at: datetime
    end_at: datetime
    travel_duration_min: float = 0.0
    wait_duration_min: float = 0.0
    slack_min: float | None = None

    def __post_init__(self) -> None:
        _require_text(self.activity_id, "TimelineEntry.activity_id")
        _require_text(self.day_id, "TimelineEntry.day_id")
        _require_text(self.location_id, "TimelineEntry.location_id")
        for value, name in (
            (self.arrival_at, "arrival_at"),
            (self.start_at, "start_at"),
            (self.end_at, "end_at"),
        ):
            if not isinstance(value, datetime):
                raise TypeError(f"TimelineEntry.{name} must be datetime")
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"TimelineEntry.{name} must be timezone-aware")
        if (
            _instant(self.arrival_at) > _instant(self.start_at)
            or _instant(self.start_at) > _instant(self.end_at)
        ):
            raise ValueError("TimelineEntry timestamps must be arrival <= start <= end")
        _non_negative_number(
            self.travel_duration_min,
            "TimelineEntry.travel_duration_min",
            positive=False,
        )
        _non_negative_number(
            self.wait_duration_min, "TimelineEntry.wait_duration_min", positive=False
        )
        if self.slack_min is not None:
            _finite_number(self.slack_min, "TimelineEntry.slack_min")


@dataclass(frozen=True, slots=True)
class DayTimelineSummary:
    """One day's duty interval, including the final return-to-base leg."""

    day_id: str
    starts_at: datetime | None = None
    completes_at: datetime | None = None
    available_end_at: datetime | None = None
    end_slack_min: float | None = None
    activity_count: int = 0
    service_min: float = 0.0
    travel_min: float = 0.0
    buffer_min: float = 0.0
    wait_min: float = 0.0
    timing_verified: bool = False

    def __post_init__(self) -> None:
        _require_text(self.day_id, "DayTimelineSummary.day_id")
        for value, name in (
            (self.starts_at, "starts_at"),
            (self.completes_at, "completes_at"),
            (self.available_end_at, "available_end_at"),
        ):
            if value is None:
                continue
            if not isinstance(value, datetime):
                raise TypeError(
                    f"DayTimelineSummary.{name} must be datetime or None"
                )
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(
                    f"DayTimelineSummary.{name} must be timezone-aware"
                )
        if (
            self.starts_at is not None
            and self.completes_at is not None
            and _instant(self.starts_at) > _instant(self.completes_at)
        ):
            raise ValueError(
                "DayTimelineSummary starts_at cannot be after completes_at"
            )
        if self.end_slack_min is not None:
            _finite_number(
                self.end_slack_min, "DayTimelineSummary.end_slack_min"
            )
        if (
            isinstance(self.activity_count, bool)
            or not isinstance(self.activity_count, int)
            or self.activity_count < 0
        ):
            raise ValueError(
                "DayTimelineSummary.activity_count must be a non-negative integer"
            )
        for value, name in (
            (self.service_min, "service_min"),
            (self.travel_min, "travel_min"),
            (self.buffer_min, "buffer_min"),
            (self.wait_min, "wait_min"),
        ):
            _non_negative_number(
                value,
                f"DayTimelineSummary.{name}",
                positive=False,
            )
        if not isinstance(self.timing_verified, bool):
            raise TypeError("DayTimelineSummary.timing_verified must be bool")


@dataclass(frozen=True, slots=True)
class CheckReport:
    """Result of deterministic validation and timeline simulation."""

    status: CheckStatus
    issues: tuple[CheckIssue, ...] = ()
    timeline: tuple[TimelineEntry, ...] = ()
    metrics: tuple[tuple[str, float], ...] = ()
    day_summaries: tuple[DayTimelineSummary, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, CheckStatus):
            raise TypeError("CheckReport.status must be CheckStatus")
        _require_tuple(self.issues, "CheckReport.issues")
        _require_tuple(self.timeline, "CheckReport.timeline")
        _require_tuple(self.metrics, "CheckReport.metrics")
        _require_tuple(self.day_summaries, "CheckReport.day_summaries")
        for issue in self.issues:
            if not isinstance(issue, CheckIssue):
                raise TypeError("CheckReport.issues items must be CheckIssue")
        for entry in self.timeline:
            if not isinstance(entry, TimelineEntry):
                raise TypeError("CheckReport.timeline items must be TimelineEntry")
        summary_day_ids: set[str] = set()
        for summary in self.day_summaries:
            if not isinstance(summary, DayTimelineSummary):
                raise TypeError(
                    "CheckReport.day_summaries items must be DayTimelineSummary"
                )
            if summary.day_id in summary_day_ids:
                raise ValueError(
                    "CheckReport.day_summaries contains duplicate day IDs"
                )
            summary_day_ids.add(summary.day_id)
        seen: set[str] = set()
        for key, value in self.metrics:
            _require_text(key, "CheckReport.metrics key")
            if key in seen:
                raise ValueError(f"CheckReport.metrics contains duplicate key {key!r}")
            seen.add(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError("CheckReport.metrics values must be numeric")
            _finite_number(value, f"CheckReport.metrics[{key!r}]")

    @property
    def errors(self) -> tuple[CheckIssue, ...]:
        return tuple(i for i in self.issues if i.severity is IssueSeverity.ERROR)

    @property
    def warnings(self) -> tuple[CheckIssue, ...]:
        return tuple(i for i in self.issues if i.severity is IssueSeverity.WARNING)


@dataclass(frozen=True, slots=True)
class TripState:
    """Immutable aggregate consumed and returned by planning operations."""

    slug: str
    title: str
    timezone: str
    days: tuple[DaySpec, ...]
    activities: tuple[Activity, ...]
    travel_estimates: tuple[TravelEstimate, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    load_issues: tuple[CheckIssue, ...] = ()
    schema_version: str = "legacy-v1"
    revision: str = ""
    start_date: date | None = None
    end_date: date | None = None
    subtitle: str = ""
    cities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.slug, "TripState.slug")
        _require_text(self.title, "TripState.title")
        _require_text(self.timezone, "TripState.timezone")
        _require_text(self.schema_version, "TripState.schema_version")
        if not isinstance(self.revision, str):
            raise TypeError("TripState.revision must be a string")
        if not isinstance(self.subtitle, str):
            raise TypeError("TripState.subtitle must be a string")
        for value, name in (
            (self.days, "TripState.days"),
            (self.activities, "TripState.activities"),
            (self.travel_estimates, "TripState.travel_estimates"),
            (self.constraints, "TripState.constraints"),
            (self.load_issues, "TripState.load_issues"),
            (self.cities, "TripState.cities"),
        ):
            _require_tuple(value, name)
        for city in self.cities:
            _require_text(city, "TripState.cities item")
        for day in self.days:
            if not isinstance(day, DaySpec):
                raise TypeError("TripState.days items must be DaySpec")
        for activity in self.activities:
            if not isinstance(activity, Activity):
                raise TypeError("TripState.activities items must be Activity")
        for estimate in self.travel_estimates:
            if not isinstance(estimate, TravelEstimate):
                raise TypeError(
                    "TripState.travel_estimates items must be TravelEstimate"
                )
        for constraint in self.constraints:
            if not isinstance(constraint, Constraint):
                raise TypeError("TripState.constraints items must be Constraint")
        for issue in self.load_issues:
            if not isinstance(issue, CheckIssue):
                raise TypeError("TripState.load_issues items must be CheckIssue")

        day_ids = [day.day_id for day in self.days]
        if len(set(day_ids)) != len(day_ids):
            raise ValueError("TripState contains duplicate day IDs")
        activity_ids = [activity.activity_id for activity in self.activities]
        if len(set(activity_ids)) != len(activity_ids):
            raise ValueError("TripState contains duplicate activity IDs")
        constraint_ids = [
            constraint.constraint_id for constraint in self.constraints
        ]
        if len(set(constraint_ids)) != len(constraint_ids):
            raise ValueError("TripState contains duplicate constraint IDs")

        day_id_set = set(day_ids)
        activity_by_id = {activity.activity_id: activity for activity in self.activities}
        for activity in self.activities:
            if activity.day_id not in day_id_set:
                raise ValueError(
                    f"Activity {activity.activity_id!r} refers to unknown day "
                    f"{activity.day_id!r}"
                )
        for day in self.days:
            for activity_id in day.activity_ids:
                activity = activity_by_id.get(activity_id)
                if activity is None:
                    raise ValueError(
                        f"Day {day.day_id!r} refers to unknown activity {activity_id!r}"
                    )
                if activity.day_id != day.day_id:
                    raise ValueError(
                        f"Day {day.day_id!r} contains activity {activity_id!r} "
                        f"owned by {activity.day_id!r}"
                    )

        for value, name in (
            (self.start_date, "TripState.start_date"),
            (self.end_date, "TripState.end_date"),
        ):
            if value is not None and (
                not isinstance(value, date) or isinstance(value, datetime)
            ):
                raise TypeError(f"{name} must be datetime.date or None")
        if self.start_date is not None and self.end_date is not None:
            if self.end_date < self.start_date:
                raise ValueError("TripState.end_date cannot be before start_date")

    @property
    def activity_by_id(self) -> Mapping[str, Activity]:
        return MappingProxyType(
            {activity.activity_id: activity for activity in self.activities}
        )

    @property
    def day_by_id(self) -> Mapping[str, DaySpec]:
        return MappingProxyType({day.day_id: day for day in self.days})

    @property
    def travel(self) -> tuple[TravelEstimate, ...]:
        """Short compatibility alias for planner code."""

        return self.travel_estimates


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_tuple(value: object, name: str) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{name} must be a tuple")


def _optional_local_time(value: time | None, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, time):
        raise TypeError(f"{name} must be datetime.time or None")
    if value.tzinfo is not None:
        raise ValueError(f"{name} must be a timezone-naive local time")
    if value.fold:
        raise ValueError(
            f"{name} cannot encode PEP 495 fold in persisted local ISO time"
        )


def _non_negative_number(value: object, name: str, *, positive: bool) -> None:
    _finite_number(value, name)
    if positive and float(value) <= 0:
        raise ValueError(f"{name} must be greater than zero")
    if not positive and float(value) < 0:
        raise ValueError(f"{name} cannot be negative")


def _optional_coordinate(
    value: float | None, minimum: float, maximum: float, name: str
) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric or None")
    if not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite")
    if not minimum <= float(value) <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


def _finite_number(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    if not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite")


def _validate_params(params: Params, name: str) -> None:
    seen: set[str] = set()
    for key, value in params:
        _require_text(key, f"{name} key")
        if key in seen:
            raise ValueError(f"{name} contains duplicate key {key!r}")
        seen.add(key)
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise TypeError(f"{name} values must be scalar")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{name} numeric values must be finite")


def _instant(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)
