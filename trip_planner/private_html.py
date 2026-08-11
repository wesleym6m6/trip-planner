"""Pure deterministic private HTML projection for Phase 6.1B.

The projector consumes one caller-loaded immutable :class:`TripState` and
returns private process-local bytes.  It accepts no path, raw JSON, template,
environment, clock, readiness assertion, or provider material and performs no
filesystem or network operation.  The fixed renderer deliberately contains no
script, storage, map, link, form, or external resource seam.

This slice is a review projection, not a readiness gate.  Candidate and
incomplete activities remain visible with explicit fixed labels; authoritative
source, readiness, bundle provenance, and artifact writes belong to Phase 6.2.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
from dataclasses import InitVar, dataclass, field
from datetime import date, time
from decimal import Decimal

from .codec import SCHEMA_VERSION
from .models import (
    Activity,
    DaySpec,
    DecisionState,
    EvidenceState,
    Flexibility,
    TripState,
)


PRIVATE_HTML_VERSION = "trip-planner.private-html/v1"
PRIVATE_HTML_RENDERER_VERSION = "trip-planner.private-html.renderer/v1"
PRIVATE_HTML_TEMPLATE_VERSION = "trip-planner.private-html.template/v1"

MAX_PRIVATE_HTML_BYTES = 16 * 1024 * 1024
MAX_PRIVATE_HTML_TEXT_BYTES = 16 * 1024
MAX_PRIVATE_HTML_DAYS = 366
MAX_PRIVATE_HTML_ACTIVITIES = 4096

_MAX_PRIVATE_HTML_CITIES = 64
_MAX_PRIVATE_HTML_IDENTITY_CHARS = 256
_MAX_PRIVATE_HTML_AGGREGATE_TEXT_BYTES = 1024 * 1024
_MAX_PRIVATE_HTML_DURATION_MINUTES = 366 * 24 * 60
_SUPPORTED_STATE_SCHEMAS = frozenset({SCHEMA_VERSION, "legacy-v1"})
_VISIBLE_DECISIONS = frozenset(
    {
        DecisionState.CANDIDATE,
        DecisionState.SELECTED,
        DecisionState.FIXED,
        DecisionState.BOOKED,
    }
)
_DECISION_LABELS = {
    DecisionState.CANDIDATE: "候選",
    DecisionState.SELECTED: "已選",
    DecisionState.FIXED: "固定",
    DecisionState.BOOKED: "已預訂",
}
_EVIDENCE_LABELS = {
    EvidenceState.UNVERIFIED: "待驗證",
    EvidenceState.VERIFIED: "已驗證",
    EvidenceState.STALE: "需更新",
    EvidenceState.CONFLICTED: "有衝突",
}
_FLEXIBILITY_LABELS = {
    Flexibility.MOVABLE: "可調整",
    Flexibility.FIXED_DAY: "固定日期",
    Flexibility.FIXED_TIME: "固定時間",
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,95}")
_PROJECTION_TOKEN = object()


class PrivateHtmlProjectionError(ValueError):
    """One bounded refusal that never contains private source values."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or _ERROR_CODE_RE.fullmatch(code) is None:
            raise ValueError("private HTML error code is invalid")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class PrivateHtmlProjection:
    """Factory-only private bytes plus hidden content-equality bindings."""

    html_bytes: bytes = field(repr=False)
    input_sha256: str = field(repr=False)
    html_sha256: str = field(repr=False)
    contract_version: str = PRIVATE_HTML_VERSION
    renderer_version: str = PRIVATE_HTML_RENDERER_VERSION
    template_version: str = PRIVATE_HTML_TEMPLATE_VERSION
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _PROJECTION_TOKEN:
            raise ValueError("PrivateHtmlProjection must come from the projector")
        if (
            type(self.html_bytes) is not bytes
            or not self.html_bytes
            or len(self.html_bytes) > MAX_PRIVATE_HTML_BYTES
            or not self.html_bytes.startswith(b"<!doctype html>\n")
            or not self.html_bytes.endswith(b"</html>\n")
            or b"\r" in self.html_bytes
        ):
            raise ValueError("html_bytes must be bounded canonical HTML bytes")
        if (
            type(self.input_sha256) is not str
            or len(self.input_sha256) != 64
            or _SHA256_RE.fullmatch(self.input_sha256) is None
        ):
            raise ValueError("input_sha256 must be a lowercase SHA-256 digest")
        if (
            type(self.html_sha256) is not str
            or len(self.html_sha256) != 64
            or _SHA256_RE.fullmatch(self.html_sha256) is None
            or hashlib.sha256(self.html_bytes).hexdigest() != self.html_sha256
        ):
            raise ValueError("html_sha256 must bind html_bytes")
        if self.contract_version != PRIVATE_HTML_VERSION:
            raise ValueError("unsupported private HTML contract version")
        if self.renderer_version != PRIVATE_HTML_RENDERER_VERSION:
            raise ValueError("unsupported private HTML renderer version")
        if self.template_version != PRIVATE_HTML_TEMPLATE_VERSION:
            raise ValueError("unsupported private HTML template version")

    def __repr__(self) -> str:
        return (
            "PrivateHtmlProjection("
            f"contract_version={self.contract_version!r}, "
            f"renderer_version={self.renderer_version!r}, "
            f"template_version={self.template_version!r}, "
            "contains_private_data=True, writes_performed=False)"
        )

    def to_safe_dict(self) -> dict[str, object]:
        """Return a value-free process status, never publishable HTML."""

        return {
            "contract_version": self.contract_version,
            "renderer_version": self.renderer_version,
            "template_version": self.template_version,
            "contains_private_data": True,
            "source_authority_bound": False,
            "readiness_assessed": False,
            "writes_performed": False,
        }


@dataclass(frozen=True, slots=True, repr=False)
class _HtmlActivity:
    title: str
    time_label: str
    duration_label: str
    decision_label: str
    evidence_label: str
    flexibility_label: str


@dataclass(frozen=True, slots=True, repr=False)
class _HtmlDay:
    date_label: str
    title: str | None
    subtitle: str | None
    timezone_label: str
    availability_label: str
    activities: tuple[_HtmlActivity, ...]


@dataclass(frozen=True, slots=True, repr=False)
class _HtmlView:
    schema_version: str
    title: str
    subtitle: str | None
    timezone_label: str
    cities: tuple[str, ...]
    days: tuple[_HtmlDay, ...]


def project_private_html(state: TripState) -> PrivateHtmlProjection:
    """Project one exact typed review view into private in-memory HTML bytes.

    The hidden input digest binds only the canonicalized allowlisted view and
    the versioned renderer/template contract.  It is not source provenance,
    readiness evidence, a resumable handle, or write authority.
    """

    if type(state) is not TripState:
        raise PrivateHtmlProjectionError("PRIVATE_HTML_TYPED_STATE_REQUIRED")
    _check_input_bounds(state)
    if (
        type(state.schema_version) is not str
        or len(state.schema_version) > 64
        or state.schema_version not in _SUPPORTED_STATE_SCHEMAS
    ):
        raise PrivateHtmlProjectionError("PRIVATE_HTML_STATE_SCHEMA_UNSUPPORTED")
    aggregate_text_bytes = 0

    def bounded_text(value: object, code: str, *, optional: bool = False) -> str | None:
        nonlocal aggregate_text_bytes
        projected = _text(value, code, optional=optional)
        if projected is None:
            return None
        aggregate_text_bytes += len(projected.encode("utf-8"))
        if aggregate_text_bytes > _MAX_PRIVATE_HTML_AGGREGATE_TEXT_BYTES:
            raise PrivateHtmlProjectionError(
                "PRIVATE_HTML_AGGREGATE_TEXT_LIMIT_EXCEEDED"
            )
        return projected

    title = bounded_text(state.title, "PRIVATE_HTML_TEXT_INVALID")
    assert title is not None
    subtitle = bounded_text(
        state.subtitle, "PRIVATE_HTML_TEXT_INVALID", optional=True
    )
    timezone_label = _identity(
        state.timezone, "PRIVATE_HTML_TIMEZONE_INVALID"
    )
    aggregate_text_bytes += len(timezone_label.encode("utf-8"))
    if aggregate_text_bytes > _MAX_PRIVATE_HTML_AGGREGATE_TEXT_BYTES:
        raise PrivateHtmlProjectionError(
            "PRIVATE_HTML_AGGREGATE_TEXT_LIMIT_EXCEEDED"
        )
    cities: list[str] = []
    for city in state.cities:
        projected_city = bounded_text(city, "PRIVATE_HTML_TEXT_INVALID")
        assert projected_city is not None
        cities.append(projected_city)

    activity_by_id: dict[str, Activity] = {}
    for activity in state.activities:
        if type(activity) is not Activity:
            raise PrivateHtmlProjectionError("PRIVATE_HTML_TYPED_STATE_INVALID")
        _validate_activity_enums(activity)
        activity_id = _identity(
            activity.activity_id, "PRIVATE_HTML_ACTIVITY_ID_INVALID"
        )
        if activity_id in activity_by_id:
            raise PrivateHtmlProjectionError("PRIVATE_HTML_ACTIVITY_ID_INVALID")
        activity_by_id[activity_id] = activity

    membership: dict[str, tuple[int, str]] = {}
    day_ids: set[str] = set()
    projected_days: list[_HtmlDay] = []
    for day in state.days:
        if type(day) is not DaySpec:
            raise PrivateHtmlProjectionError("PRIVATE_HTML_TYPED_STATE_INVALID")
        day_id = _identity(day.day_id, "PRIVATE_HTML_DAY_ID_INVALID")
        if day_id in day_ids:
            raise PrivateHtmlProjectionError("PRIVATE_HTML_DAY_ID_INVALID")
        day_ids.add(day_id)
        if type(day.date) is not date:
            raise PrivateHtmlProjectionError("PRIVATE_HTML_DATE_INVALID")
        day_title = bounded_text(
            day.title, "PRIVATE_HTML_TEXT_INVALID", optional=True
        )
        day_subtitle = bounded_text(
            day.subtitle, "PRIVATE_HTML_TEXT_INVALID", optional=True
        )
        if day.timezone is None:
            day_timezone = "時區待確認"
        else:
            day_timezone = _identity(
                day.timezone, "PRIVATE_HTML_TIMEZONE_INVALID"
            )
            aggregate_text_bytes += len(day_timezone.encode("utf-8"))
            if aggregate_text_bytes > _MAX_PRIVATE_HTML_AGGREGATE_TEXT_BYTES:
                raise PrivateHtmlProjectionError(
                    "PRIVATE_HTML_AGGREGATE_TEXT_LIMIT_EXCEEDED"
                )
        availability = _availability_label(
            day.available_start, day.available_end
        )
        day_activities: list[_HtmlActivity] = []
        for member_id_value in day.activity_ids:
            member_id = _identity(
                member_id_value, "PRIVATE_HTML_ACTIVITY_ID_INVALID"
            )
            count, _ = membership.get(member_id, (0, day_id))
            membership[member_id] = (count + 1, day_id)
            activity = activity_by_id.get(member_id)
            if activity is None:
                raise PrivateHtmlProjectionError(
                    "PRIVATE_HTML_ACTIVITY_MEMBERSHIP_INVALID"
                )
            activity_day_id = _identity(
                activity.day_id, "PRIVATE_HTML_DAY_ID_INVALID"
            )
            if activity_day_id != day_id:
                raise PrivateHtmlProjectionError(
                    "PRIVATE_HTML_ACTIVITY_MEMBERSHIP_INVALID"
                )
            if activity.decision_state not in _VISIBLE_DECISIONS:
                continue
            activity_title = bounded_text(
                activity.title, "PRIVATE_HTML_TEXT_INVALID"
            )
            assert activity_title is not None
            day_activities.append(
                _HtmlActivity(
                    title=activity_title,
                    time_label=_activity_time_label(activity.scheduled_start),
                    duration_label=_duration_label(activity.duration_min),
                    decision_label=_enum_label(
                        activity.decision_state,
                        _DECISION_LABELS,
                        "PRIVATE_HTML_DECISION_STATE_INVALID",
                    ),
                    evidence_label=_enum_label(
                        activity.evidence_state,
                        _EVIDENCE_LABELS,
                        "PRIVATE_HTML_EVIDENCE_STATE_INVALID",
                    ),
                    flexibility_label=_enum_label(
                        activity.flexibility,
                        _FLEXIBILITY_LABELS,
                        "PRIVATE_HTML_FLEXIBILITY_INVALID",
                    ),
                )
            )
        projected_days.append(
            _HtmlDay(
                date_label=_format_date(day.date),
                title=day_title,
                subtitle=day_subtitle,
                timezone_label=day_timezone,
                availability_label=availability,
                activities=tuple(day_activities),
            )
        )

    for activity_id, activity in activity_by_id.items():
        if activity.decision_state not in _VISIBLE_DECISIONS:
            continue
        activity_day_id = _identity(
            activity.day_id, "PRIVATE_HTML_DAY_ID_INVALID"
        )
        member_count, member_day_id = membership.get(activity_id, (0, ""))
        if member_count != 1 or member_day_id != activity_day_id:
            raise PrivateHtmlProjectionError(
                "PRIVATE_HTML_ACTIVITY_MEMBERSHIP_INVALID"
            )

    view = _HtmlView(
        schema_version=state.schema_version,
        title=title,
        subtitle=subtitle,
        timezone_label=timezone_label,
        cities=tuple(cities),
        days=tuple(projected_days),
    )
    input_sha256 = _input_digest(view)
    html_bytes = _render_html(view)
    return PrivateHtmlProjection(
        html_bytes=html_bytes,
        input_sha256=input_sha256,
        html_sha256=hashlib.sha256(html_bytes).hexdigest(),
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
            state.cities,
        )
    ):
        raise PrivateHtmlProjectionError("PRIVATE_HTML_TYPED_STATE_INVALID")
    if (
        len(state.days) > MAX_PRIVATE_HTML_DAYS
        or len(state.activities) > MAX_PRIVATE_HTML_ACTIVITIES
        or len(state.travel_estimates) > MAX_PRIVATE_HTML_ACTIVITIES
        or len(state.constraints) > MAX_PRIVATE_HTML_ACTIVITIES
        or len(state.load_issues) > MAX_PRIVATE_HTML_ACTIVITIES
        or len(state.cities) > _MAX_PRIVATE_HTML_CITIES
    ):
        raise PrivateHtmlProjectionError("PRIVATE_HTML_INPUT_LIMIT_EXCEEDED")
    membership_count = 0
    for day in state.days:
        if type(day) is not DaySpec or type(day.activity_ids) is not tuple:
            raise PrivateHtmlProjectionError("PRIVATE_HTML_TYPED_STATE_INVALID")
        membership_count += len(day.activity_ids)
        if membership_count > MAX_PRIVATE_HTML_ACTIVITIES:
            raise PrivateHtmlProjectionError("PRIVATE_HTML_INPUT_LIMIT_EXCEEDED")


def _identity(value: object, code: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_PRIVATE_HTML_IDENTITY_CHARS
        or value != value.strip()
        or _contains_forbidden_control(value, allow_newlines=False)
    ):
        raise PrivateHtmlProjectionError(code)
    return value


def _text(value: object, code: str, *, optional: bool) -> str | None:
    if type(value) is not str:
        raise PrivateHtmlProjectionError(code)
    if optional and value == "":
        return None
    if len(value) > 2 * MAX_PRIVATE_HTML_TEXT_BYTES:
        raise PrivateHtmlProjectionError("PRIVATE_HTML_TEXT_LIMIT_EXCEEDED")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized or _contains_forbidden_control(
        normalized, allow_newlines=True
    ):
        raise PrivateHtmlProjectionError(code)
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError:
        raise PrivateHtmlProjectionError(code) from None
    if len(encoded) > MAX_PRIVATE_HTML_TEXT_BYTES:
        raise PrivateHtmlProjectionError("PRIVATE_HTML_TEXT_LIMIT_EXCEEDED")
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


def _format_date(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}-{value.day:02d}"


def _format_clock(value: object, code: str) -> str:
    if type(value) is not time or value.tzinfo is not None or value.fold:
        raise PrivateHtmlProjectionError(code)
    if value.microsecond:
        return (
            f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}."
            f"{value.microsecond:06d}"
        )
    if value.second:
        return f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}"
    return f"{value.hour:02d}:{value.minute:02d}"


def _availability_label(start: object, end: object) -> str:
    if start is None and end is None:
        return "可用時段待確認"
    start_label = (
        "開始待確認"
        if start is None
        else _format_clock(start, "PRIVATE_HTML_DAY_BOUNDS_INVALID")
    )
    end_label = (
        "結束待確認"
        if end is None
        else _format_clock(end, "PRIVATE_HTML_DAY_BOUNDS_INVALID")
    )
    suffix = ""
    if type(start) is time and type(end) is time:
        if end == start:
            raise PrivateHtmlProjectionError("PRIVATE_HTML_DAY_BOUNDS_INVALID")
        if end < start:
            suffix = "（跨日 planning window）"
    return f"{start_label}–{end_label}{suffix}"


def _activity_time_label(value: object) -> str:
    if value is None:
        return "時間待確認"
    return _format_clock(value, "PRIVATE_HTML_ACTIVITY_TIME_INVALID")


def _duration_label(value: object) -> str:
    if value is None:
        return "時長待確認"
    if type(value) not in (int, float):
        raise PrivateHtmlProjectionError("PRIVATE_HTML_DURATION_INVALID")
    if (
        (type(value) is float and not math.isfinite(value))
        or value <= 0
        or value > _MAX_PRIVATE_HTML_DURATION_MINUTES
    ):
        raise PrivateHtmlProjectionError("PRIVATE_HTML_DURATION_INVALID")
    try:
        duration = Decimal(str(value))
    except Exception:
        raise PrivateHtmlProjectionError("PRIVATE_HTML_DURATION_INVALID") from None
    normalized = _canonical_decimal(duration)
    return f"{normalized} 分鐘"


def _canonical_decimal(value: Decimal) -> str:
    """Format one positive finite Decimal without consulting decimal context."""

    _, digits, exponent = value.as_tuple()
    digit_text = "".join(str(digit) for digit in digits) or "0"
    if exponent >= 0:
        return digit_text + ("0" * exponent)
    decimal_index = len(digit_text) + exponent
    if decimal_index > 0:
        rendered = digit_text[:decimal_index] + "." + digit_text[decimal_index:]
    else:
        rendered = "0." + ("0" * -decimal_index) + digit_text
    return rendered.rstrip("0").rstrip(".")


def _validate_activity_enums(activity: Activity) -> None:
    for value, enum_type, code in (
        (
            activity.decision_state,
            DecisionState,
            "PRIVATE_HTML_DECISION_STATE_INVALID",
        ),
        (
            activity.evidence_state,
            EvidenceState,
            "PRIVATE_HTML_EVIDENCE_STATE_INVALID",
        ),
        (
            activity.flexibility,
            Flexibility,
            "PRIVATE_HTML_FLEXIBILITY_INVALID",
        ),
    ):
        if type(value) is not enum_type:
            raise PrivateHtmlProjectionError(code)


def _enum_label(value: object, labels: dict[object, str], code: str) -> str:
    try:
        return labels[value]
    except (KeyError, TypeError):
        raise PrivateHtmlProjectionError(code) from None


def _input_digest(view: _HtmlView) -> str:
    payload = {
        "contract_version": PRIVATE_HTML_VERSION,
        "renderer_version": PRIVATE_HTML_RENDERER_VERSION,
        "template_version": PRIVATE_HTML_TEMPLATE_VERSION,
        "state_schema_version": view.schema_version,
        "trip": {
            "title": view.title,
            "subtitle": view.subtitle,
            "timezone": view.timezone_label,
            "cities": list(view.cities),
        },
        "days": [
            {
                "date": day.date_label,
                "title": day.title,
                "subtitle": day.subtitle,
                "timezone": day.timezone_label,
                "availability": day.availability_label,
                "activities": [
                    {
                        "title": activity.title,
                        "time": activity.time_label,
                        "duration": activity.duration_label,
                        "decision": activity.decision_label,
                        "evidence": activity.evidence_label,
                        "flexibility": activity.flexibility_label,
                    }
                    for activity in day.activities
                ],
            }
            for day in view.days
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
        PRIVATE_HTML_VERSION.encode("ascii") + b"\n" + encoded
    ).hexdigest()


def _escaped(value: str) -> str:
    return html.escape(value, quote=True)


def _render_html(view: _HtmlView) -> bytes:
    parts = [
        "<!doctype html>\n",
        '<html lang="zh-Hant">\n',
        "<head>\n",
        '  <meta charset="utf-8">\n',
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n',
        '  <meta name="referrer" content="no-referrer">\n',
        "  <meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
        "style-src 'unsafe-inline'; img-src 'none'; font-src 'none'; media-src 'none'; "
        "connect-src 'none'; script-src 'none'; frame-src 'none'; object-src 'none'; "
        "base-uri 'none'; form-action 'none'\">\n",
        "  <title>",
        _escaped(view.title),
        " · 私有行程預覽</title>\n",
        "  <style>\n",
        "    *{box-sizing:border-box}\n",
        "    body{margin:0;background:#f4f7fa;color:#20303d;font-family:system-ui,sans-serif}\n",
        "    main{max-width:760px;margin:0 auto;padding:28px 18px 64px}\n",
        "    header{background:#173f5f;color:#fff;border-radius:14px;padding:24px 20px}\n",
        "    h1{font-size:26px;line-height:1.3;margin:0;white-space:pre-line}\n",
        "    .subtitle{margin:9px 0 0;opacity:.9;white-space:pre-line}\n",
        "    .meta{font-size:13px;line-height:1.6;margin-top:10px}\n",
        "    .notice{margin:16px 0 0;padding:12px 14px;border-radius:10px;background:#fff4d6;color:#654d00}\n",
        "    section{background:#fff;border:1px solid #d9e3eb;border-radius:12px;margin-top:16px;overflow:hidden}\n",
        "    h2{font-size:17px;margin:0;padding:15px 16px;border-bottom:1px solid #e7edf2}\n",
        "    .day-meta,.day-copy{padding:0 16px;color:#5b6975;font-size:13px;white-space:pre-line}\n",
        "    .day-copy{color:#273843}\n",
        "    ul{list-style:none;margin:12px 0 0;padding:0}\n",
        "    li{padding:14px 16px;border-top:1px solid #edf1f4}\n",
        "    .activity-title{font-weight:650;line-height:1.5;white-space:pre-line}\n",
        "    .activity-meta{font-size:12px;color:#5f6d78;margin-top:6px;line-height:1.7}\n",
        "    .badge{display:inline-block;margin:4px 6px 0 0;padding:2px 8px;border:1px solid #cfd9e1;border-radius:999px}\n",
        "    .empty{padding:14px 16px;color:#687681}\n",
        "    footer{font-size:12px;color:#687681;line-height:1.7;margin-top:24px}\n",
        "  </style>\n",
        "</head>\n",
        "<body>\n",
        "  <main>\n",
        "    <header>\n",
        "      <h1>",
        _escaped(view.title),
        "</h1>\n",
    ]
    if view.subtitle is not None:
        parts.extend(
            ("      <p class=\"subtitle\">", _escaped(view.subtitle), "</p>\n")
        )
    parts.extend(
        (
            '      <div class="meta">時區：',
            _escaped(view.timezone_label),
            "</div>\n",
            '      <div class="meta">城市：',
            _escaped(" · ".join(view.cities) if view.cities else "待確認"),
            "</div>\n",
            '      <p class="notice">本頁是私有 review preview；決策與證據分開顯示，'
            "標籤只呈現輸入狀態，未重驗來源或 readiness，亦不代表已達 "
            "travel_ready。</p>\n",
            "    </header>\n",
        )
    )
    for index, day in enumerate(view.days, start=1):
        parts.extend(
            (
                "    <section>\n",
                "      <h2>第 ",
                str(index),
                " 天 · ",
                _escaped(day.date_label),
                "</h2>\n",
            )
        )
        if day.title is not None:
            parts.extend(
                ("      <p class=\"day-copy\">", _escaped(day.title), "</p>\n")
            )
        if day.subtitle is not None:
            parts.extend(
                ("      <p class=\"day-copy\">", _escaped(day.subtitle), "</p>\n")
            )
        parts.extend(
            (
                '      <div class="day-meta">時區：',
                _escaped(day.timezone_label),
                " · ",
                _escaped(day.availability_label),
                "</div>\n",
            )
        )
        if day.activities:
            parts.append("      <ul>\n")
            for activity in day.activities:
                parts.extend(
                    (
                        "        <li>\n",
                        '          <div class="activity-title">',
                        _escaped(activity.title),
                        "</div>\n",
                        '          <div class="activity-meta">',
                        _escaped(activity.time_label),
                        " · ",
                        _escaped(activity.duration_label),
                        "</div>\n",
                        '          <div class="activity-meta">',
                        '<span class="badge">決策：',
                        _escaped(activity.decision_label),
                        "</span>",
                        '<span class="badge">證據：',
                        _escaped(activity.evidence_label),
                        "</span>",
                        '<span class="badge">彈性：',
                        _escaped(activity.flexibility_label),
                        "</span></div>\n",
                        "        </li>\n",
                    )
                )
            parts.append("      </ul>\n")
        else:
            parts.append('      <p class="empty">目前沒有可顯示的活動。</p>\n')
        parts.append("    </section>\n")
    if not view.days:
        parts.extend(
            (
                '    <section><p class="empty">目前尚無可顯示的日期與活動。</p>',
                "</section>\n",
            )
        )
    parts.extend(
        (
            "    <footer>此projection不包含地圖、連結、備註、provider資料、sidecar或ICS；"
            "取消與排除項目亦不顯示。</footer>\n",
            "  </main>\n",
            "</body>\n",
            "</html>\n",
        )
    )
    try:
        rendered = "".join(parts).encode("utf-8")
    except (UnicodeEncodeError, MemoryError):
        raise PrivateHtmlProjectionError("PRIVATE_HTML_OUTPUT_INVALID") from None
    if len(rendered) > MAX_PRIVATE_HTML_BYTES:
        raise PrivateHtmlProjectionError("PRIVATE_HTML_OUTPUT_LIMIT_EXCEEDED")
    return rendered


__all__ = [
    "MAX_PRIVATE_HTML_ACTIVITIES",
    "MAX_PRIVATE_HTML_BYTES",
    "MAX_PRIVATE_HTML_DAYS",
    "MAX_PRIVATE_HTML_TEXT_BYTES",
    "PRIVATE_HTML_RENDERER_VERSION",
    "PRIVATE_HTML_TEMPLATE_VERSION",
    "PRIVATE_HTML_VERSION",
    "PrivateHtmlProjection",
    "PrivateHtmlProjectionError",
    "project_private_html",
]
