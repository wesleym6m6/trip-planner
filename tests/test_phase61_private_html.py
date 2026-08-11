"""Synthetic adversarial tests for Phase 6.1B private HTML projection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, time, timezone
from decimal import ROUND_UP, localcontext
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from trip_planner.codec import build_plan, plan_to_trip_state
from trip_planner.loaders import load_legacy_trip
from trip_planner.models import (
    CheckIssue,
    Constraint,
    ConstraintKind,
    ConstraintStrength,
    EvidenceState,
    IssueSeverity,
    TimeWindow,
    TravelEstimate,
    TripState,
)
from trip_planner.private_html import (
    PRIVATE_HTML_RENDERER_VERSION,
    PRIVATE_HTML_TEMPLATE_VERSION,
    PRIVATE_HTML_VERSION,
    PrivateHtmlProjection,
    PrivateHtmlProjectionError,
    project_private_html,
)


REVISION_RE = re.compile(r"[0-9a-f]{64}")
_ALLOWED_TAG_ATTRIBUTES = {
    "html": frozenset({"lang"}),
    "head": frozenset(),
    "meta": frozenset({"charset", "name", "content", "http-equiv"}),
    "title": frozenset(),
    "style": frozenset(),
    "body": frozenset(),
    "main": frozenset(),
    "header": frozenset(),
    "h1": frozenset(),
    "p": frozenset({"class"}),
    "div": frozenset({"class"}),
    "section": frozenset(),
    "h2": frozenset(),
    "ul": frozenset(),
    "li": frozenset(),
    "span": frozenset({"class"}),
    "footer": frozenset(),
}
_ALLOWED_CLASS_VALUES = {
    "activity-meta",
    "activity-title",
    "badge",
    "day-copy",
    "day-meta",
    "empty",
    "meta",
    "notice",
    "subtitle",
}


def _place(
    activity_id: str,
    title: str,
    *,
    clock: str | None = None,
    duration_min: int | float | None = None,
    decision_state: str = "candidate",
    evidence_state: str = "unverified",
    flexibility: str | None = None,
    location_id: str = "location-example",
) -> dict[str, object]:
    if flexibility is None:
        flexibility = "fixed_time" if clock is not None else "movable"
    value: dict[str, object] = {
        "activity_id": activity_id,
        "title": title,
        "location_id": location_id,
        "decision_state": decision_state,
        "evidence_state": evidence_state,
        "flexibility": flexibility,
    }
    if clock is not None:
        value["time"] = clock
    if duration_min is not None:
        value["duration_min"] = duration_min
    return value


def _plan(
    *,
    trip_id: str = "synthetic-html-trip",
    title: str = "Synthetic Private Preview",
    subtitle: str = "Review copy",
    timezone_name: str = "Asia/Tokyo",
    cities: tuple[str, ...] = ("Example City",),
    day_id: str = "day-example",
    day_date: str = "2026-10-01",
    day_title: str = "Review Day",
    day_subtitle: str = "Synthetic fixture",
    day_timezone: str | None = "Asia/Tokyo",
    available_start: str | None = "08:00",
    available_end: str | None = "20:00",
    places: tuple[dict[str, object], ...] | None = None,
) -> dict[str, object]:
    if places is None:
        places = (_place("activity-example", "Candidate Example"),)
    day: dict[str, object] = {
        "day_id": day_id,
        "day": 1,
        "date": day_date,
        "title": day_title,
        "subtitle": day_subtitle,
        "start_location_id": "location-example",
        "end_location_id": "location-example",
        "places": [deepcopy(item) for item in places],
        "travel": [],
    }
    if day_timezone is not None:
        day["timezone"] = day_timezone
    if available_start is not None:
        day["available_start"] = available_start
    if available_end is not None:
        day["available_end"] = available_end
    return build_plan(
        trip_id=trip_id,
        generation=1,
        state={
            "trip": {
                "slug": trip_id,
                "title": title,
                "subtitle": subtitle,
                "timezone": timezone_name,
                "date_range": f"{day_date} ~ {day_date}",
                "cities": list(cities),
            },
            "itinerary": {
                "available_modes": ["walking"],
                "days": [day],
            },
        },
    )


def _project(plan: dict[str, object]) -> PrivateHtmlProjection:
    return project_private_html(plan_to_trip_state(plan))


def _error_code(callable_object) -> str:
    with unittest.TestCase().assertRaises(PrivateHtmlProjectionError) as caught:
        callable_object()
    return caught.exception.code


class _SurfaceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attributes: list[tuple[str, str, str | None]] = []
        self.elements: list[tuple[str, tuple[tuple[str, str | None], ...]]] = []
        self.text: list[str] = []
        self.style_text: list[str] = []
        self.comments: list[str] = []
        self._in_style = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.tags.append(tag)
        self.attributes.extend((tag, name, value) for name, value in attrs)
        self.elements.append((tag, tuple(attrs)))
        if tag == "style":
            self._in_style = True

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._in_style:
            self.style_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False

    def handle_comment(self, data: str) -> None:
        self.comments.append(data)


class Phase61BPrivateHtmlTests(unittest.TestCase):
    def test_versioned_renderer_matches_synthetic_golden(self) -> None:
        result = _project(_plan())
        golden_by_contract = {
            (
                "trip-planner.private-html/v1",
                "trip-planner.private-html.renderer/v1",
                "trip-planner.private-html.template/v1",
            ): "904d2423920948067c974c3aa2b5b71605391dd279396d66b7f55bc20133bb7c",
        }
        version_key = (
            result.contract_version,
            result.renderer_version,
            result.template_version,
        )
        self.assertIn(version_key, golden_by_contract)
        self.assertEqual(golden_by_contract[version_key], result.html_sha256)

    def test_draft_candidate_unknown_is_deterministic_and_result_is_safe(self) -> None:
        plan = _plan()
        first = _project(plan)
        second = _project(plan)
        rendered = first.html_bytes.decode("utf-8")

        self.assertEqual(first.html_bytes, second.html_bytes)
        self.assertEqual(first.input_sha256, second.input_sha256)
        self.assertEqual(
            hashlib.sha256(first.html_bytes).hexdigest(), first.html_sha256
        )
        self.assertRegex(first.input_sha256, REVISION_RE)
        self.assertEqual(
            {
                "contract_version": PRIVATE_HTML_VERSION,
                "renderer_version": PRIVATE_HTML_RENDERER_VERSION,
                "template_version": PRIVATE_HTML_TEMPLATE_VERSION,
                "contains_private_data": True,
                "source_authority_bound": False,
                "readiness_assessed": False,
                "writes_performed": False,
            },
            first.to_safe_dict(),
        )
        self.assertIn("Candidate Example", rendered)
        self.assertIn("時間待確認", rendered)
        self.assertIn("時長待確認", rendered)
        self.assertIn("決策：候選", rendered)
        self.assertIn("證據：待驗證", rendered)
        self.assertIn("彈性：可調整", rendered)
        self.assertIn("不代表已達 travel_ready", rendered)
        self.assertIn("標籤只呈現輸入狀態，未重驗來源或 readiness", rendered)
        self.assertNotIn(str(plan["revision"]), rendered)
        safe_repr = repr(first)
        for private_value in (
            first.input_sha256,
            first.html_sha256,
            "Candidate Example",
            "2026-10-01",
        ):
            self.assertNotIn(private_value, safe_repr)
            self.assertNotIn(private_value, json.dumps(first.to_safe_dict()))
        self.assertTrue(first.html_bytes.startswith(b"<!doctype html>\n"))
        self.assertTrue(first.html_bytes.endswith(b"</html>\n"))
        self.assertFalse(first.html_bytes.endswith(b"\n\n"))
        self.assertNotIn(b"\r", first.html_bytes)

    def test_review_states_overnight_and_partial_bounds_are_honest(self) -> None:
        plan = _plan(
            available_start="22:00",
            available_end="02:00",
            places=(
                _place("candidate", "Candidate", decision_state="candidate"),
                _place(
                    "selected",
                    "Selected",
                    clock="23:30",
                    duration_min=30,
                    decision_state="selected",
                    evidence_state="verified",
                ),
                _place(
                    "fixed",
                    "Fixed",
                    clock="00:30",
                    duration_min=45.5,
                    decision_state="fixed",
                    evidence_state="stale",
                    flexibility="fixed_day",
                ),
                _place(
                    "booked",
                    "Booked",
                    clock="01:30",
                    duration_min=60,
                    decision_state="booked",
                    evidence_state="conflicted",
                ),
                _place("cancelled", "HIDDEN-CANCELLED", decision_state="cancelled"),
                _place("excluded", "HIDDEN-EXCLUDED", decision_state="excluded"),
            ),
        )
        rendered = _project(plan).html_bytes.decode("utf-8")

        self.assertIn("22:00–02:00（跨日 planning window）", rendered)
        self.assertIn("23:30", rendered)
        self.assertIn("00:30", rendered)
        self.assertIn("45.5 分鐘", rendered)
        for label in (
            "決策：候選",
            "決策：已選",
            "決策：固定",
            "決策：已預訂",
            "證據：已驗證",
            "證據：需更新",
            "證據：有衝突",
            "彈性：固定日期",
        ):
            self.assertIn(label, rendered)
        self.assertNotIn("HIDDEN-CANCELLED", rendered)
        self.assertNotIn("HIDDEN-EXCLUDED", rendered)
        self.assertNotIn("DTSTART", rendered)

        partial = _project(
            _plan(available_start="09:00", available_end=None)
        ).html_bytes.decode("utf-8")
        self.assertIn("09:00–結束待確認", partial)
        unknown_state = plan_to_trip_state(
            _plan(available_start=None, available_end=None)
        )
        unknown_state = replace(
            unknown_state,
            days=(replace(unknown_state.days[0], timezone=None),),
        )
        unknown = project_private_html(unknown_state).html_bytes.decode("utf-8")
        self.assertIn("時區待確認", unknown)
        self.assertIn("可用時段待確認", unknown)

        fractional_state = plan_to_trip_state(
            _plan(
                available_start="22:00:00.123456",
                available_end="02:00:00.654321",
                places=(
                    _place(
                        "fractional",
                        "Fractional Review",
                        clock="23:30:00.234567",
                        duration_min=30,
                        decision_state="selected",
                        evidence_state="verified",
                    ),
                ),
            )
        )
        fractional_state = replace(
            fractional_state,
            revision="PRIVATE-NONAUTHORITATIVE-REVISION",
        )
        fractional = project_private_html(fractional_state).html_bytes.decode(
            "utf-8"
        )
        self.assertIn(
            "22:00:00.123456–02:00:00.654321（跨日 planning window）",
            fractional,
        )
        self.assertIn("23:30:00.234567", fractional)
        self.assertIn("證據：已驗證", fractional)
        self.assertIn("未重驗來源或 readiness", fractional)
        self.assertNotIn("PRIVATE-NONAUTHORITATIVE-REVISION", fractional)

    def test_excluded_fields_identifiers_order_and_revision_do_not_bind_view(self) -> None:
        base_state = plan_to_trip_state(_plan())
        sentinel = "PRIVATE-EXCLUDED-SENTINEL"
        issue = CheckIssue(
            code="SYNTHETIC_EXCLUDED_ISSUE",
            severity=IssueSeverity.WARNING,
            message=sentinel,
            activity_ids=(base_state.activities[0].activity_id,),
            evidence_refs=(sentinel,),
            details=(("private_value", sentinel),),
            suggested_fixes=(sentinel,),
        )
        changed_day = replace(
            base_state.days[0],
            start_location_id=sentinel,
            end_location_id=sentinel,
            allowed_modes=(sentinel,),
        )
        changed_activity = replace(
            base_state.activities[0],
            order=999,
            location_id=sentinel,
            priority=999,
            allowed_windows=(TimeWindow(time(1), time(2)),),
            kind=sentinel,
            note=sentinel,
            maps_query=sentinel,
            lat=1.001,
            lng=1.002,
        )
        estimate = TravelEstimate(
            from_location_id=sentinel,
            to_location_id=sentinel,
            mode="walking",
            duration_min=17,
            day_id=base_state.days[0].day_id,
            evidence_state=EvidenceState.VERIFIED,
            fresh_until=datetime(2099, 1, 1, tzinfo=timezone.utc),
            evidence_ref=sentinel,
            source=sentinel,
            query_departure_at=datetime(2098, 1, 1, tzinfo=timezone.utc),
            warning_codes=("synthetic.private.warning",),
        )
        constraint = Constraint(
            constraint_id=sentinel,
            kind=ConstraintKind.MUST_INCLUDE,
            strength=ConstraintStrength.HARD,
            subject_ids=(base_state.activities[0].activity_id,),
            params=(("private_value", sentinel),),
            origin=sentinel,
            source_text=sentinel,
        )
        excluded_state = replace(
            base_state,
            slug=sentinel,
            revision=sentinel,
            days=(changed_day,),
            activities=(changed_activity,),
            travel_estimates=(estimate,),
            constraints=(constraint,),
            load_issues=(issue,),
        )
        base = project_private_html(base_state)
        excluded = project_private_html(excluded_state)

        self.assertEqual(base.html_bytes, excluded.html_bytes)
        self.assertEqual(base.input_sha256, excluded.input_sha256)
        self.assertNotIn(sentinel.encode(), excluded.html_bytes)

        renamed_ids = _project(
            _plan(
                trip_id="different-private-slug",
                day_id="different-day-id",
                places=(
                    _place(
                        "different-activity-id",
                        "Candidate Example",
                        location_id="different-location-id",
                    ),
                ),
            )
        )
        self.assertEqual(base.html_bytes, renamed_ids.html_bytes)
        self.assertEqual(base.input_sha256, renamed_ids.input_sha256)

    def test_membership_order_controls_output_but_activity_tuple_order_does_not(self) -> None:
        state = plan_to_trip_state(
            _plan(
                places=(
                    _place("alpha", "Alpha"),
                    _place("beta", "Beta"),
                )
            )
        )
        base = project_private_html(state)
        tuple_reordered = project_private_html(
            replace(state, activities=tuple(reversed(state.activities)))
        )
        membership_reordered = project_private_html(
            replace(
                state,
                days=(
                    replace(
                        state.days[0],
                        activity_ids=tuple(reversed(state.days[0].activity_ids)),
                    ),
                ),
            )
        )

        self.assertEqual(base.html_bytes, tuple_reordered.html_bytes)
        self.assertEqual(base.input_sha256, tuple_reordered.input_sha256)
        self.assertNotEqual(base.html_bytes, membership_reordered.html_bytes)

    def test_html_injection_stays_text_and_markup_has_no_network_surface(self) -> None:
        title = (
            'https://private.invalid/ url(https://private.invalid/x) '
            '<script>alert("x")</script> & title'
        )
        day_title = "</style><img src=x onerror=alert(1)>"
        activity_title = "' onclick='alert(1) </textarea> javascript:private"
        result = _project(
            _plan(
                title=title,
                cities=("javascript:private",),
                day_title=day_title,
                places=(_place("activity-example", activity_title),),
            )
        )
        rendered = result.html_bytes.decode("utf-8")
        parser = _SurfaceParser()
        parser.feed(rendered)
        parser.close()

        self.assertIn("&lt;script&gt;alert(&quot;", rendered)
        self.assertIn("&lt;/style&gt;&lt;img", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<img ", rendered)
        for tag, attributes in parser.elements:
            self.assertIn(tag, _ALLOWED_TAG_ATTRIBUTES)
            for name, _ in attributes:
                self.assertIn(name, _ALLOWED_TAG_ATTRIBUTES[tag])
                self.assertFalse(name.lower().startswith("on"))
        for tag, name, value in parser.attributes:
            if name == "class":
                self.assertIn(value, _ALLOWED_CLASS_VALUES)
            elif name == "lang":
                self.assertEqual("zh-Hant", value)
            elif name == "charset":
                self.assertEqual("utf-8", value)
            elif name == "http-equiv":
                self.assertEqual("Content-Security-Policy", value)
            elif name == "name":
                self.assertIn(value, {"viewport", "referrer"})
        style_text = "".join(parser.style_text)
        self.assertNotIn("@import", style_text.lower())
        self.assertNotIn("url(", style_text.lower())
        self.assertEqual([], parser.comments)
        self.assertNotIn("localstorage", rendered.lower())
        self.assertNotIn("calendar.ics", rendered.lower())
        self.assertIn("https://private.invalid/", "".join(parser.text))
        private_fragments = (title, day_title, activity_title, "javascript:private")
        for _, _, value in parser.attributes:
            if value is None:
                continue
            for fragment in private_fragments:
                self.assertNotIn(fragment, value)
        for fragment in private_fragments:
            self.assertNotIn(fragment, style_text)
        csp_values = [
            value
            for tag, name, value in parser.attributes
            if tag == "meta" and name == "content" and value is not None
        ]
        self.assertTrue(
            any("default-src 'none'" in value for value in csp_values)
        )
        self.assertTrue(any("connect-src 'none'" in value for value in csp_values))

    def test_duration_rendering_ignores_decimal_context(self) -> None:
        state = plan_to_trip_state(
            _plan(
                places=(
                    _place(
                        "duration",
                        "Duration Review",
                        clock="10:00",
                        duration_min=45.55,
                        decision_state="selected",
                    ),
                )
            )
        )
        baseline = project_private_html(state)
        with localcontext() as context:
            context.prec = 2
            context.rounding = ROUND_UP
            constrained = project_private_html(state)
        self.assertEqual(baseline.html_bytes, constrained.html_bytes)
        self.assertEqual(baseline.input_sha256, constrained.input_sha256)
        self.assertIn("45.55 分鐘", baseline.html_bytes.decode("utf-8"))

    def test_local_clock_invariants_fail_closed_with_fixed_codes(self) -> None:
        base = plan_to_trip_state(
            _plan(
                places=(
                    _place(
                        "clock",
                        "Clock Review",
                        clock="10:00",
                        duration_min=30,
                        decision_state="selected",
                    ),
                )
            )
        )

        day_cases = (
            (time(8, tzinfo=timezone.utc), time(20), "aware bound"),
            (time(8, fold=1), time(20), "folded bound"),
            (time(8), time(8), "equal bounds"),
        )
        for start, end, label in day_cases:
            with self.subTest(label=label):
                day = replace(base.days[0])
                object.__setattr__(day, "available_start", start)
                object.__setattr__(day, "available_end", end)
                candidate = replace(base, days=(day,))
                self.assertEqual(
                    "PRIVATE_HTML_DAY_BOUNDS_INVALID",
                    _error_code(lambda: project_private_html(candidate)),
                )

        for clock, label in (
            (time(10, tzinfo=timezone.utc), "aware activity"),
            (time(10, fold=1), "folded activity"),
        ):
            with self.subTest(label=label):
                activity = replace(base.activities[0])
                object.__setattr__(activity, "scheduled_start", clock)
                candidate = replace(base, activities=(activity,))
                self.assertEqual(
                    "PRIVATE_HTML_ACTIVITY_TIME_INVALID",
                    _error_code(lambda: project_private_html(candidate)),
                )

    def test_control_and_surrogate_failures_are_value_free(self) -> None:
        sentinel = "PRIVATE-SENTINEL-CONTROL"
        state = plan_to_trip_state(_plan())
        cases = (
            replace(state, title=sentinel + "\x00"),
            replace(state, title=sentinel + "\ud800"),
            replace(
                state,
                activities=(
                    replace(state.activities[0], title=sentinel + "\x7f"),
                ),
            ),
        )
        for candidate in cases:
            with self.subTest(candidate_type=type(candidate.title).__name__):
                with self.assertRaises(PrivateHtmlProjectionError) as caught:
                    project_private_html(candidate)
                self.assertEqual("PRIVATE_HTML_TEXT_INVALID", caught.exception.code)
                self.assertNotIn(sentinel, str(caught.exception))
                self.assertNotIn(sentinel, repr(caught.exception))

    def test_membership_failure_is_bounded(self) -> None:
        state = plan_to_trip_state(_plan())
        detached = replace(
            state,
            days=(replace(state.days[0], activity_ids=()),),
        )
        self.assertEqual(
            "PRIVATE_HTML_ACTIVITY_MEMBERSHIP_INVALID",
            _error_code(lambda: project_private_html(detached)),
        )

    def test_empty_trip_state_has_fixed_review_copy(self) -> None:
        state = TripState(
            slug="synthetic-empty",
            title="Empty Synthetic Preview",
            subtitle="",
            timezone="Etc/UTC",
            cities=(),
            days=(),
            activities=(),
            schema_version="legacy-v1",
            revision="",
        )
        rendered = project_private_html(state).html_bytes.decode("utf-8")
        self.assertIn("目前尚無可顯示的日期與活動", rendered)
        self.assertIn("城市：待確認", rendered)

    def test_loader_proven_synthetic_ids_can_render_but_never_appear(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "trip.json").write_text(
                json.dumps(
                    {
                        "slug": "synthetic-legacy-preview",
                        "title": "Synthetic Legacy Preview",
                        "timezone": "Asia/Tokyo",
                        "date_range": "2026-10-01 ~ 2026-10-01",
                    }
                ),
                encoding="utf-8",
            )
            (root / "itinerary.json").write_text(
                json.dumps(
                    {
                        "days": [
                            {
                                "day": 1,
                                "date": "2026-10-01",
                                "places": [
                                    {
                                        "title": "Synthetic Legacy Candidate",
                                        "location_id": "synthetic-location",
                                        "decision_state": "candidate",
                                        "evidence_state": "unverified",
                                    }
                                ],
                                "travel": [],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            state = load_legacy_trip(root)

        result = project_private_html(state)
        rendered = result.html_bytes.decode("utf-8")
        self.assertIn("Synthetic Legacy Candidate", rendered)
        for issue in state.load_issues:
            for activity_id in issue.activity_ids:
                self.assertNotIn(activity_id, rendered)
                self.assertNotIn(activity_id, repr(result))

    def test_projection_result_is_factory_only_and_self_consistent(self) -> None:
        with self.assertRaisesRegex(ValueError, "must come from the projector"):
            PrivateHtmlProjection(
                html_bytes=b"<!doctype html>\n</html>\n",
                input_sha256="a" * 64,
                html_sha256=hashlib.sha256(
                    b"<!doctype html>\n</html>\n"
                ).hexdigest(),
            )

    def test_schema_primitive_text_aggregate_input_and_output_bounds(self) -> None:
        state = plan_to_trip_state(_plan())
        cases = (
            (
                "PRIVATE_HTML_STATE_SCHEMA_UNSUPPORTED",
                lambda: project_private_html(
                    replace(state, schema_version="unknown/v1")
                ),
            ),
            (
                "PRIVATE_HTML_TIMEZONE_INVALID",
                lambda: project_private_html(
                    replace(state, timezone="x" * 257)
                ),
            ),
        )
        for expected, call in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, _error_code(call))

        with mock.patch("trip_planner.private_html.MAX_PRIVATE_HTML_TEXT_BYTES", 4):
            self.assertEqual(
                "PRIVATE_HTML_TEXT_LIMIT_EXCEEDED",
                _error_code(lambda: project_private_html(state)),
            )
        with mock.patch(
            "trip_planner.private_html._MAX_PRIVATE_HTML_AGGREGATE_TEXT_BYTES",
            20,
        ):
            self.assertEqual(
                "PRIVATE_HTML_AGGREGATE_TEXT_LIMIT_EXCEEDED",
                _error_code(lambda: project_private_html(state)),
            )
        with mock.patch("trip_planner.private_html.MAX_PRIVATE_HTML_BYTES", 100):
            self.assertEqual(
                "PRIVATE_HTML_OUTPUT_LIMIT_EXCEEDED",
                _error_code(lambda: project_private_html(state)),
            )
        with mock.patch("trip_planner.private_html.MAX_PRIVATE_HTML_ACTIVITIES", 0):
            self.assertEqual(
                "PRIVATE_HTML_INPUT_LIMIT_EXCEEDED",
                _error_code(lambda: project_private_html(state)),
            )

    def test_hostile_numeric_and_string_subclasses_fail_with_fixed_codes(self) -> None:
        sentinel = "PRIVATE-SENTINEL-PRIMITIVE"

        class HostileFloat(float):
            armed = False

            def __str__(self):
                if self.armed:
                    raise RuntimeError(sentinel)
                return super().__str__()

        class HostileStr(str):
            armed = False

            def __hash__(self):
                if self.armed:
                    raise RuntimeError(sentinel)
                return super().__hash__()

        state = plan_to_trip_state(
            _plan(
                places=(
                    _place(
                        "activity-example",
                        "Duration Example",
                        clock="10:00",
                        duration_min=60,
                        decision_state="selected",
                    ),
                )
            )
        )
        hostile_duration = HostileFloat(60)
        duration_state = replace(
            state,
            activities=(
                replace(state.activities[0], duration_min=hostile_duration),
            ),
        )
        hostile_duration.armed = True
        hostile_schema = HostileStr(state.schema_version)
        schema_state = replace(state, schema_version=hostile_schema)
        hostile_schema.armed = True

        cases = (
            ("PRIVATE_HTML_DURATION_INVALID", duration_state),
            ("PRIVATE_HTML_STATE_SCHEMA_UNSUPPORTED", schema_state),
        )
        for expected, candidate in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(PrivateHtmlProjectionError) as caught:
                    project_private_html(candidate)
                self.assertEqual(expected, caught.exception.code)
                self.assertNotIn(sentinel, str(caught.exception))
                self.assertNotIn(sentinel, repr(caught.exception))

        oversized_activity = replace(state.activities[0])
        object.__setattr__(oversized_activity, "duration_min", 10**10_000)
        oversized_state = replace(state, activities=(oversized_activity,))
        with mock.patch(
            "trip_planner.private_html.Decimal",
            side_effect=AssertionError("oversized duration was stringified"),
        ):
            self.assertEqual(
                "PRIVATE_HTML_DURATION_INVALID",
                _error_code(lambda: project_private_html(oversized_state)),
            )

    def test_activity_enum_boundaries_reject_hostile_values_with_fixed_codes(self) -> None:
        sentinel = "PRIVATE-SENTINEL-ENUM"

        class HostileHash:
            def __hash__(self):
                raise RuntimeError(sentinel)

        base = plan_to_trip_state(_plan())
        cases = (
            ("decision_state", [], "PRIVATE_HTML_DECISION_STATE_INVALID"),
            (
                "decision_state",
                "candidate",
                "PRIVATE_HTML_DECISION_STATE_INVALID",
            ),
            (
                "evidence_state",
                HostileHash(),
                "PRIVATE_HTML_EVIDENCE_STATE_INVALID",
            ),
            (
                "flexibility",
                HostileHash(),
                "PRIVATE_HTML_FLEXIBILITY_INVALID",
            ),
        )
        for field_name, value, expected in cases:
            with self.subTest(field_name=field_name, expected=expected):
                activity = replace(base.activities[0])
                object.__setattr__(activity, field_name, value)
                candidate = replace(base, activities=(activity,))
                with self.assertRaises(PrivateHtmlProjectionError) as caught:
                    project_private_html(candidate)
                self.assertEqual(expected, caught.exception.code)
                self.assertNotIn(sentinel, str(caught.exception))
                self.assertNotIn(sentinel, repr(caught.exception))

    def test_projection_performs_no_path_or_network_operation(self) -> None:
        state = plan_to_trip_state(_plan())
        with (
            mock.patch("builtins.open", side_effect=AssertionError("open called")),
            mock.patch(
                "pathlib.Path.read_text",
                side_effect=AssertionError("read_text called"),
            ),
            mock.patch(
                "pathlib.Path.write_text",
                side_effect=AssertionError("write_text called"),
            ),
            mock.patch("socket.socket", side_effect=AssertionError("socket called")),
        ):
            result = project_private_html(state)
        self.assertIn(b"Candidate Example", result.html_bytes)

    def test_subprocess_output_ignores_cwd_timezone_locale_and_hash_seed(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        program = r'''
import sys
from datetime import date
from trip_planner.models import Activity, DaySpec, DecisionState, EvidenceState, Flexibility, TripState
from trip_planner.private_html import project_private_html
day = DaySpec(day_id="day", date=date(2026, 10, 1), timezone=None, available_start=None, available_end=None, activity_ids=("activity",))
activity = Activity(activity_id="activity", day_id="day", order=0, title="Synthetic Candidate", location_id="loc", scheduled_start=None, duration_min=None, decision_state=DecisionState.CANDIDATE, flexibility=Flexibility.MOVABLE, evidence_state=EvidenceState.UNVERIFIED)
state = TripState(slug="synthetic", title="Synthetic", subtitle="Review", timezone="Etc/UTC", cities=("Example",), days=(day,), activities=(activity,), schema_version="legacy-v1", revision="")
sys.stdout.buffer.write(project_private_html(state).html_bytes)
'''
        outputs: list[bytes] = []
        for process_timezone, hash_seed, locale_name in (
            ("UTC", "1", "C"),
            ("Pacific/Honolulu", "987", "C.UTF-8"),
        ):
            with tempfile.TemporaryDirectory() as temporary:
                environment = {
                    "PYTHONPATH": str(repo_root),
                    "PYTHONHASHSEED": hash_seed,
                    "TZ": process_timezone,
                    "LC_ALL": locale_name,
                }
                completed = subprocess.run(
                    [sys.executable, "-c", program],
                    cwd=temporary,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=20,
                )
                self.assertEqual(0, completed.returncode, completed.stderr.decode())
                outputs.append(completed.stdout)

        self.assertEqual(outputs[0], outputs[1])

    def test_ambient_environment_does_not_change_bytes(self) -> None:
        state = plan_to_trip_state(_plan())
        before = project_private_html(state)
        with mock.patch.dict(
            os.environ,
            {
                "PRIVATE_SENTINEL": "different",
                "GOOGLE_MAPS_API_KEY": "not-read",
                "SERPAPI_API_KEY": "not-read",
            },
            clear=False,
        ):
            after = project_private_html(state)
        self.assertEqual(before.html_bytes, after.html_bytes)
        self.assertEqual(before.input_sha256, after.input_sha256)


if __name__ == "__main__":
    unittest.main()
