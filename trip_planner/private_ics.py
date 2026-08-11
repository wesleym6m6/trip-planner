"""Pure deterministic private ICS projection for Phase 6.1A.

The projector consumes an immutable :class:`~trip_planner.models.TripState`
that has already been loaded by the trusted codec/loader boundary.  It does no
caller-supplied or project/private-trip filesystem I/O and performs no
provider, environment, credential, or wall-clock access.  Python ``zoneinfo``
may read the host timezone database.  Returned bytes are private process-local
material, not render, write, import, share, or deployment authority.

This module implements a deliberately small RFC 5545 subset.  Event times are
always UTC, and the caller supplies the deterministic ``DTSTAMP`` input.  IANA
timezone conversion is reproducible only against the same timezone-data
snapshot.  Source revision, readiness, and stable-trip-identity binding remain
the responsibility of the later Phase 6 private-bundle gate.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import InitVar, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from .codec import SCHEMA_VERSION
from .models import Activity, CheckIssue, DaySpec, DecisionState, TripState


PRIVATE_ICS_VERSION = "trip-planner.private-ics/v1"
PRIVATE_ICS_UID_POLICY_VERSION = "trip-planner.private-ics.uid/v1"

MAX_PRIVATE_ICS_EVENTS = 4096
MAX_PRIVATE_ICS_IDENTITY_CHARS = 256
MAX_PRIVATE_ICS_TEXT_BYTES = 16 * 1024
MAX_PRIVATE_ICS_BYTES = 16 * 1024 * 1024
MAX_PRIVATE_ICS_DURATION_SECONDS = 366 * 24 * 60 * 60

_MAX_PRIVATE_ICS_DAYS = 366
_MAX_PRIVATE_ICS_INPUT_ITEMS = 4096
_MAX_PRIVATE_ICS_AGGREGATE_TEXT_BYTES = 4 * 1024 * 1024
_SUPPORTED_STATE_SCHEMAS = frozenset({SCHEMA_VERSION, "legacy-v1"})
_ACTIVE_DECISIONS = frozenset(
    {
        DecisionState.SELECTED,
        DecisionState.FIXED,
        DecisionState.BOOKED,
    }
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,95}")
_UTC = timezone.utc
_PROJECTION_TOKEN = object()


class PrivateIcsProjectionError(ValueError):
    """One bounded refusal that never contains private source values."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or _ERROR_CODE_RE.fullmatch(code) is None:
            raise ValueError("private ICS error code is invalid")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class PrivateIcsProjection:
    """Factory-only private bytes plus hidden exact-input bindings."""

    calendar_bytes: bytes = field(repr=False)
    input_sha256: str = field(repr=False)
    calendar_sha256: str = field(repr=False)
    event_count: int = field(repr=False)
    contract_version: str = PRIVATE_ICS_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _PROJECTION_TOKEN:
            raise ValueError("PrivateIcsProjection must come from the projector")
        if (
            type(self.calendar_bytes) is not bytes
            or not self.calendar_bytes
            or len(self.calendar_bytes) > MAX_PRIVATE_ICS_BYTES
        ):
            raise ValueError("calendar_bytes must be bounded non-empty bytes")
        if (
            type(self.input_sha256) is not str
            or _SHA256_RE.fullmatch(self.input_sha256) is None
        ):
            raise ValueError("input_sha256 must be a lowercase SHA-256 digest")
        if (
            type(self.calendar_sha256) is not str
            or _SHA256_RE.fullmatch(self.calendar_sha256) is None
            or hashlib.sha256(self.calendar_bytes).hexdigest()
            != self.calendar_sha256
        ):
            raise ValueError("calendar_sha256 must bind calendar_bytes")
        if (
            type(self.event_count) is not int
            or not 1 <= self.event_count <= MAX_PRIVATE_ICS_EVENTS
        ):
            raise ValueError("event_count must be a bounded positive integer")
        if self.contract_version != PRIVATE_ICS_VERSION:
            raise ValueError("unsupported private ICS contract version")

    def __repr__(self) -> str:
        return (
            "PrivateIcsProjection("
            f"contract_version={self.contract_version!r}, "
            "contains_private_data=True, writes_performed=False)"
        )

    def to_safe_dict(self) -> dict[str, object]:
        """Return a value-free status projection suitable for bounded logs."""

        return {
            "contract_version": self.contract_version,
            "contains_private_data": True,
            "writes_performed": False,
        }


@dataclass(frozen=True, slots=True, repr=False)
class _ProjectedEvent:
    activity_id: str
    uid: str
    starts_at_utc: datetime
    ends_at_utc: datetime
    summary: str


def project_private_ics(
    state: TripState,
    *,
    uid_namespace: str,
    generated_at: datetime,
) -> PrivateIcsProjection:
    """Project one exact typed-value snapshot into private in-memory ICS bytes.

    ``uid_namespace`` must be a caller-owned stable namespace.  Phase 6.1A
    validates its shape and deterministic use but deliberately does not claim
    that it matches a persisted trip identity; Phase 6.2 will bind that identity
    together with source revision and readiness.  ``generated_at`` is an
    explicit deterministic ``DTSTAMP`` input; the wall clock is never read.
    """

    if type(state) is not TripState:
        raise PrivateIcsProjectionError("PRIVATE_ICS_TYPED_STATE_REQUIRED")
    _check_input_bounds(state)
    stable_uid_namespace = _identity(
        uid_namespace, "PRIVATE_ICS_UID_NAMESPACE_INVALID"
    )
    deterministic_instant = _generated_at(generated_at)
    if (
        type(state.schema_version) is not str
        or len(state.schema_version) > 64
        or state.schema_version not in _SUPPORTED_STATE_SCHEMAS
    ):
        raise PrivateIcsProjectionError("PRIVATE_ICS_STATE_SCHEMA_UNSUPPORTED")
    if (
        type(state.revision) is not str
        or len(state.revision) != 64
        or _SHA256_RE.fullmatch(state.revision) is None
    ):
        raise PrivateIcsProjectionError("PRIVATE_ICS_SOURCE_REVISION_INVALID")

    calendar_title = _text(state.title, "PRIVATE_ICS_TEXT_INVALID")
    projected_text_bytes = len(calendar_title.encode("utf-8"))
    active: list[Activity] = []
    for activity in state.activities:
        if type(activity) is not Activity:
            raise PrivateIcsProjectionError("PRIVATE_ICS_TYPED_STATE_INVALID")
        if activity.decision_state in _ACTIVE_DECISIONS:
            active.append(activity)
    if not active:
        raise PrivateIcsProjectionError("PRIVATE_ICS_NO_EVENTS")
    if len(active) > MAX_PRIVATE_ICS_EVENTS:
        raise PrivateIcsProjectionError("PRIVATE_ICS_EVENT_LIMIT_EXCEEDED")

    membership: dict[str, tuple[int, str]] = {}
    day_by_id: dict[str, DaySpec] = {}
    for day in state.days:
        stable_day_id = _identity(day.day_id, "PRIVATE_ICS_DAY_ID_INVALID")
        day_by_id[stable_day_id] = day
        for activity_id in day.activity_ids:
            stable_member_id = _identity(
                activity_id, "PRIVATE_ICS_ACTIVITY_ID_INVALID"
            )
            count, _ = membership.get(stable_member_id, (0, stable_day_id))
            membership[stable_member_id] = (count + 1, stable_day_id)
    synthetic_ids: set[str] = set()
    for issue in state.load_issues:
        if type(issue) is not CheckIssue or type(issue.code) is not str:
            raise PrivateIcsProjectionError("PRIVATE_ICS_TYPED_STATE_INVALID")
        if issue.code != "SYNTHETIC_ACTIVITY_IDS":
            continue
        for activity_id in issue.activity_ids:
            synthetic_ids.add(
                _identity(activity_id, "PRIVATE_ICS_ACTIVITY_ID_INVALID")
            )

    projected: list[_ProjectedEvent] = []
    seen_uids: set[str] = set()
    for activity in active:
        stable_activity_id = _identity(
            activity.activity_id,
            "PRIVATE_ICS_ACTIVITY_ID_INVALID",
        )
        if stable_activity_id in synthetic_ids:
            raise PrivateIcsProjectionError("PRIVATE_ICS_UNSTABLE_ACTIVITY_ID")
        stable_day_id = _identity(
            activity.day_id, "PRIVATE_ICS_DAY_ID_INVALID"
        )
        membership_count, membership_day_id = membership.get(
            stable_activity_id, (0, "")
        )
        if membership_count != 1 or membership_day_id != stable_day_id:
            raise PrivateIcsProjectionError("PRIVATE_ICS_ACTIVITY_MEMBERSHIP_INVALID")
        day = day_by_id.get(stable_day_id)
        if day is None:
            raise PrivateIcsProjectionError("PRIVATE_ICS_ACTIVITY_MEMBERSHIP_INVALID")
        summary = _text(activity.title, "PRIVATE_ICS_TEXT_INVALID")
        projected_text_bytes += len(summary.encode("utf-8"))
        if projected_text_bytes > _MAX_PRIVATE_ICS_AGGREGATE_TEXT_BYTES:
            raise PrivateIcsProjectionError(
                "PRIVATE_ICS_AGGREGATE_TEXT_LIMIT_EXCEEDED"
            )
        starts_at, ends_at = _event_interval(day, activity)
        uid = _uid_for(stable_uid_namespace, stable_activity_id)
        if uid in seen_uids:
            raise PrivateIcsProjectionError("PRIVATE_ICS_UID_COLLISION")
        seen_uids.add(uid)
        projected.append(
            _ProjectedEvent(
                activity_id=stable_activity_id,
                uid=uid,
                starts_at_utc=starts_at,
                ends_at_utc=ends_at,
                summary=summary,
            )
        )

    projected.sort(key=lambda item: (item.starts_at_utc, item.uid))
    input_sha256 = _input_digest(
        state=state,
        uid_namespace=stable_uid_namespace,
        generated_at=deterministic_instant,
        calendar_title=calendar_title,
        events=tuple(projected),
    )
    dtstamp = _format_utc(deterministic_instant)
    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//Trip Planner//Private ICS v1//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_escape_text(calendar_title)}",
        f"X-TRIPPLANNER-CONTRACT:{PRIVATE_ICS_VERSION}",
        f"X-TRIPPLANNER-UID-POLICY:{PRIVATE_ICS_UID_POLICY_VERSION}",
        f"X-TRIPPLANNER-SOURCE-SCHEMA:{state.schema_version}",
    ]
    for event in projected:
        lines.extend(
            (
                "BEGIN:VEVENT",
                f"UID:{event.uid}",
                f"DTSTAMP:{dtstamp}",
                f"DTSTART:{_format_utc(event.starts_at_utc)}",
                f"DTEND:{_format_utc(event.ends_at_utc)}",
                f"SUMMARY:{_escape_text(event.summary)}",
                "END:VEVENT",
            )
        )
    lines.append("END:VCALENDAR")
    calendar_bytes = _encode_lines(tuple(lines))
    return PrivateIcsProjection(
        calendar_bytes=calendar_bytes,
        input_sha256=input_sha256,
        calendar_sha256=hashlib.sha256(calendar_bytes).hexdigest(),
        event_count=len(projected),
        _token=_PROJECTION_TOKEN,
    )


def _check_input_bounds(state: TripState) -> None:
    if any(
        type(value) is not tuple
        for value in (
            state.days,
            state.activities,
            state.travel_estimates,
            state.constraints,
            state.load_issues,
        )
    ):
        raise PrivateIcsProjectionError("PRIVATE_ICS_TYPED_STATE_INVALID")
    if (
        len(state.days) > _MAX_PRIVATE_ICS_DAYS
        or len(state.activities) > _MAX_PRIVATE_ICS_INPUT_ITEMS
        or len(state.travel_estimates) > _MAX_PRIVATE_ICS_INPUT_ITEMS
        or len(state.constraints) > _MAX_PRIVATE_ICS_INPUT_ITEMS
        or len(state.load_issues) > _MAX_PRIVATE_ICS_INPUT_ITEMS
    ):
        raise PrivateIcsProjectionError("PRIVATE_ICS_INPUT_LIMIT_EXCEEDED")
    membership_count = 0
    for day in state.days:
        if type(day) is not DaySpec or type(day.activity_ids) is not tuple:
            raise PrivateIcsProjectionError("PRIVATE_ICS_TYPED_STATE_INVALID")
        membership_count += len(day.activity_ids)
        if membership_count > _MAX_PRIVATE_ICS_INPUT_ITEMS:
            raise PrivateIcsProjectionError("PRIVATE_ICS_INPUT_LIMIT_EXCEEDED")
    issue_activity_count = 0
    for issue in state.load_issues:
        if type(issue) is not CheckIssue or type(issue.activity_ids) is not tuple:
            raise PrivateIcsProjectionError("PRIVATE_ICS_TYPED_STATE_INVALID")
        issue_activity_count += len(issue.activity_ids)
        if issue_activity_count > _MAX_PRIVATE_ICS_INPUT_ITEMS:
            raise PrivateIcsProjectionError("PRIVATE_ICS_INPUT_LIMIT_EXCEEDED")


def _event_interval(day: DaySpec, activity: Activity) -> tuple[datetime, datetime]:
    if type(day.date) is not date:
        raise PrivateIcsProjectionError("PRIVATE_ICS_DATETIME_INVALID")
    if day.timezone is None:
        raise PrivateIcsProjectionError("PRIVATE_ICS_DAY_TIMEZONE_MISSING")
    timezone_name = _identity(day.timezone, "PRIVATE_ICS_TIMEZONE_INVALID")
    if day.available_start is None or day.available_end is None:
        raise PrivateIcsProjectionError("PRIVATE_ICS_DAY_BOUNDS_MISSING")
    if type(day.available_start) is not time or type(day.available_end) is not time:
        raise PrivateIcsProjectionError("PRIVATE_ICS_DAY_BOUNDS_INVALID")
    if day.available_start.microsecond or day.available_end.microsecond:
        raise PrivateIcsProjectionError("PRIVATE_ICS_SUBSECOND_UNSUPPORTED")
    if activity.scheduled_start is None:
        raise PrivateIcsProjectionError("PRIVATE_ICS_START_MISSING")
    if type(activity.scheduled_start) is not time:
        raise PrivateIcsProjectionError("PRIVATE_ICS_START_INVALID")
    if activity.scheduled_start.microsecond:
        raise PrivateIcsProjectionError("PRIVATE_ICS_SUBSECOND_UNSUPPORTED")
    if activity.duration_min is None:
        raise PrivateIcsProjectionError("PRIVATE_ICS_DURATION_MISSING")
    duration_seconds = _duration_seconds(activity.duration_min)
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        raise PrivateIcsProjectionError("PRIVATE_ICS_TIMEZONE_INVALID") from None

    local_date = day.date
    if _day_crosses_midnight(day) and activity.scheduled_start < day.available_start:
        try:
            local_date += timedelta(days=1)
        except OverflowError:
            raise PrivateIcsProjectionError("PRIVATE_ICS_DATETIME_INVALID") from None
    starts_at = _localize_strict(local_date, activity.scheduled_start, zone)
    try:
        ends_at = starts_at + timedelta(seconds=duration_seconds)
    except OverflowError:
        raise PrivateIcsProjectionError("PRIVATE_ICS_DATETIME_INVALID") from None
    if ends_at <= starts_at:
        raise PrivateIcsProjectionError("PRIVATE_ICS_INTERVAL_INVALID")
    return starts_at, ends_at


def _day_crosses_midnight(day: DaySpec) -> bool:
    return (
        day.available_start is not None
        and day.available_end is not None
        and day.available_end <= day.available_start
    )


def _localize_strict(local_date: date, clock: time, zone: ZoneInfo) -> datetime:
    naive = datetime.combine(local_date, clock)
    candidates: set[datetime] = set()
    try:
        for fold in (0, 1):
            aware = naive.replace(tzinfo=zone, fold=fold)
            candidate = aware.astimezone(_UTC)
            roundtrip = candidate.astimezone(zone)
            if (
                roundtrip.replace(tzinfo=None) == naive
                and roundtrip.fold == fold
            ):
                candidates.add(candidate)
    except (OverflowError, ValueError):
        raise PrivateIcsProjectionError("PRIVATE_ICS_DATETIME_INVALID") from None
    if not candidates:
        raise PrivateIcsProjectionError("PRIVATE_ICS_LOCAL_TIME_NONEXISTENT")
    if len(candidates) != 1:
        raise PrivateIcsProjectionError("PRIVATE_ICS_LOCAL_TIME_AMBIGUOUS")
    return next(iter(candidates))


def _identity(value: object, code: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_PRIVATE_ICS_IDENTITY_CHARS
        or value != value.strip()
        or _contains_forbidden_control(value, allow_newlines=False)
    ):
        raise PrivateIcsProjectionError(code)
    return value


def _generated_at(value: object) -> datetime:
    if type(value) is not datetime:
        raise PrivateIcsProjectionError("PRIVATE_ICS_DTSTAMP_INVALID")
    try:
        if value.tzinfo is None:
            raise ValueError
        offset = value.utcoffset()
        if offset is None:
            raise ValueError
        normalized = value.astimezone(_UTC)
    except Exception:
        raise PrivateIcsProjectionError("PRIVATE_ICS_DTSTAMP_INVALID") from None
    if normalized.microsecond:
        raise PrivateIcsProjectionError("PRIVATE_ICS_SUBSECOND_UNSUPPORTED")
    return normalized


def _duration_seconds(value: object) -> int:
    if type(value) not in (int, float):
        raise PrivateIcsProjectionError("PRIVATE_ICS_DURATION_INVALID")
    try:
        seconds = Decimal(str(value)) * Decimal(60)
    except Exception:
        raise PrivateIcsProjectionError("PRIVATE_ICS_DURATION_INVALID") from None
    integral = seconds.to_integral_value()
    if (
        not seconds.is_finite()
        or seconds != integral
        or not 1 <= integral <= MAX_PRIVATE_ICS_DURATION_SECONDS
    ):
        raise PrivateIcsProjectionError("PRIVATE_ICS_DURATION_INVALID")
    return int(integral)


def _text(value: object, code: str) -> str:
    if type(value) is not str:
        raise PrivateIcsProjectionError(code)
    if len(value) > 2 * MAX_PRIVATE_ICS_TEXT_BYTES:
        raise PrivateIcsProjectionError("PRIVATE_ICS_TEXT_LIMIT_EXCEEDED")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized or _contains_forbidden_control(
        normalized, allow_newlines=True
    ):
        raise PrivateIcsProjectionError(code)
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError:
        raise PrivateIcsProjectionError(code) from None
    if len(encoded) > MAX_PRIVATE_ICS_TEXT_BYTES:
        raise PrivateIcsProjectionError("PRIVATE_ICS_TEXT_LIMIT_EXCEEDED")
    return normalized


def _contains_forbidden_control(value: str, *, allow_newlines: bool) -> bool:
    for character in value:
        codepoint = ord(character)
        if allow_newlines and character == "\n":
            continue
        if (
            codepoint < 0x20
            or 0x7F <= codepoint <= 0x9F
            or 0xD800 <= codepoint <= 0xDFFF
        ):
            return True
    return False


def _escape_text(value: str) -> str:
    normalized = _text(value, "PRIVATE_ICS_TEXT_INVALID")
    return (
        normalized.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _uid_for(uid_namespace: str, activity_id: str) -> str:
    namespace_bytes = uid_namespace.encode("utf-8")
    activity_bytes = activity_id.encode("utf-8")
    payload = b"\0".join(
        (
            PRIVATE_ICS_UID_POLICY_VERSION.encode("ascii"),
            len(namespace_bytes).to_bytes(4, "big"),
            namespace_bytes,
            len(activity_bytes).to_bytes(4, "big"),
            activity_bytes,
        )
    )
    return f"{hashlib.sha256(payload).hexdigest()}@private.trip-planner"


def _input_digest(
    *,
    state: TripState,
    uid_namespace: str,
    generated_at: datetime,
    calendar_title: str,
    events: tuple[_ProjectedEvent, ...],
) -> str:
    payload = {
        "contract_version": PRIVATE_ICS_VERSION,
        "uid_policy_version": PRIVATE_ICS_UID_POLICY_VERSION,
        "state_schema_version": state.schema_version,
        "uid_namespace": uid_namespace,
        "generated_at": generated_at.isoformat(),
        "calendar_title": calendar_title,
        "events": [
            {
                "activity_id": event.activity_id,
                "uid": event.uid,
                "starts_at": event.starts_at_utc.isoformat(),
                "ends_at": event.ends_at_utc.isoformat(),
                "summary": event.summary,
            }
            for event in events
        ],
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(
        PRIVATE_ICS_VERSION.encode("ascii") + b"\n" + encoded
    ).hexdigest()


def _format_utc(value: datetime) -> str:
    return (
        f"{value.year:04d}{value.month:02d}{value.day:02d}T"
        f"{value.hour:02d}{value.minute:02d}{value.second:02d}Z"
    )


def _encode_lines(lines: tuple[str, ...]) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for line in lines:
        if "\r" in line or "\n" in line:
            raise PrivateIcsProjectionError("PRIVATE_ICS_LINE_INVALID")
        folded = _fold_line(line)
        chunk = (folded + "\r\n").encode("utf-8")
        total += len(chunk)
        if total > MAX_PRIVATE_ICS_BYTES:
            raise PrivateIcsProjectionError("PRIVATE_ICS_ARTIFACT_LIMIT_EXCEEDED")
        chunks.append(chunk)
    return b"".join(chunks)


def _fold_line(line: str) -> str:
    segments: list[str] = []
    current: list[str] = []
    current_octets = 0
    limit = 75
    for character in line:
        octets = len(character.encode("utf-8"))
        if current and current_octets + octets > limit:
            segments.append("".join(current))
            current = [character]
            current_octets = octets
            limit = 74
        else:
            current.append(character)
            current_octets += octets
    segments.append("".join(current))
    return "\r\n ".join(segments)


__all__ = [
    "MAX_PRIVATE_ICS_BYTES",
    "MAX_PRIVATE_ICS_EVENTS",
    "MAX_PRIVATE_ICS_TEXT_BYTES",
    "PRIVATE_ICS_UID_POLICY_VERSION",
    "PRIVATE_ICS_VERSION",
    "PrivateIcsProjection",
    "PrivateIcsProjectionError",
    "project_private_ics",
]
