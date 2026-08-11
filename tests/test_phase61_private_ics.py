"""Offline adversarial tests for Phase 6.1A private ICS projection."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from unittest import mock

from trip_planner.codec import build_plan, plan_to_trip_state
from trip_planner.loaders import load_legacy_trip
from trip_planner.models import (
    CheckIssue,
    EvidenceState,
    Flexibility,
    IssueSeverity,
)
from trip_planner.private_ics import (
    PRIVATE_ICS_VERSION,
    PrivateIcsProjection,
    PrivateIcsProjectionError,
    project_private_ics,
)


UTC = timezone.utc
GENERATED_AT = datetime(2026, 8, 9, 12, tzinfo=UTC)
REVISION_RE = re.compile(r"[0-9a-f]{64}")


def _place(
    activity_id: str,
    title: str,
    clock: str,
    *,
    duration_min: int | float = 60,
    decision_state: str = "fixed",
    evidence_state: str = "verified",
    location_id: str = "location-example",
) -> dict[str, object]:
    return {
        "activity_id": activity_id,
        "title": title,
        "location_id": location_id,
        "time": clock,
        "duration_min": duration_min,
        "decision_state": decision_state,
        "flexibility": "fixed_time",
        "evidence_state": evidence_state,
    }


def _plan(
    *,
    trip_id: str = "synthetic-calendar-trip",
    title: str = "Synthetic Calendar",
    timezone_name: str = "Asia/Tokyo",
    day_date: str = "2026-10-01",
    available_start: str = "08:00",
    available_end: str = "20:00",
    places: tuple[dict[str, object], ...] | None = None,
) -> dict[str, object]:
    if places is None:
        places = (_place("activity-alpha", "Example Museum", "10:00"),)
    return build_plan(
        trip_id=trip_id,
        generation=1,
        state={
            "trip": {
                "slug": trip_id,
                "title": title,
                "timezone": timezone_name,
                "date_range": f"{day_date} ~ {day_date}",
                "cities": ["Example City"],
            },
            "itinerary": {
                "available_modes": ["walking"],
                "days": [
                    {
                        "day_id": "day-1",
                        "day": 1,
                        "date": day_date,
                        "timezone": timezone_name,
                        "available_start": available_start,
                        "available_end": available_end,
                        "start_location_id": "location-example",
                        "end_location_id": "location-example",
                        "places": [deepcopy(item) for item in places],
                        "travel": [],
                    }
                ],
            },
        },
    )


def _project(
    plan: dict[str, object],
    *,
    generated_at: datetime = GENERATED_AT,
):
    trip_id = plan["trip_id"]
    assert isinstance(trip_id, str)
    return project_private_ics(
        plan_to_trip_state(plan),
        uid_namespace=trip_id,
        generated_at=generated_at,
    )


def _logical_lines(calendar_bytes: bytes) -> tuple[str, ...]:
    text = calendar_bytes.decode("utf-8")
    physical = text.split("\r\n")
    if physical[-1] != "":
        raise AssertionError("calendar must end in exactly one CRLF")
    logical: list[str] = []
    for line in physical[:-1]:
        if line.startswith(" "):
            if not logical:
                raise AssertionError("orphan folded continuation")
            logical[-1] += line[1:]
        else:
            logical.append(line)
    return tuple(logical)


def _events(calendar_bytes: bytes) -> tuple[dict[str, str], ...]:
    events: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in _logical_lines(calendar_bytes):
        if line == "BEGIN:VEVENT":
            if current is not None:
                raise AssertionError("nested VEVENT")
            current = {}
        elif line == "END:VEVENT":
            if current is None:
                raise AssertionError("VEVENT ended without beginning")
            events.append(current)
            current = None
        elif current is not None:
            name, value = line.split(":", 1)
            current[name] = value
    if current is not None:
        raise AssertionError("unterminated VEVENT")
    return tuple(events)


def _error_code(callable_object) -> str:
    with unittest.TestCase().assertRaises(PrivateIcsProjectionError) as caught:
        callable_object()
    return caught.exception.code


class Phase61PrivateIcsTests(unittest.TestCase):
    def test_exact_input_is_byte_deterministic_and_result_is_safe(self) -> None:
        plan = _plan()
        first = _project(plan)
        second = _project(plan)

        self.assertEqual(first.calendar_bytes, second.calendar_bytes)
        self.assertEqual(
            hashlib.sha256(first.calendar_bytes).hexdigest(),
            first.calendar_sha256,
        )
        self.assertRegex(first.input_sha256, REVISION_RE)
        self.assertEqual(
            {
                "contract_version": PRIVATE_ICS_VERSION,
                "contains_private_data": True,
                "writes_performed": False,
            },
            first.to_safe_dict(),
        )
        rendered_repr = repr(first)
        self.assertNotIn(first.input_sha256, rendered_repr)
        self.assertNotIn(first.calendar_sha256, rendered_repr)
        self.assertNotIn("Example Museum", rendered_repr)
        self.assertNotIn(str(plan["revision"]).encode(), first.calendar_bytes)

    def test_supported_rfc_subset_uses_utc_and_crlf(self) -> None:
        result = _project(_plan())
        logical = _logical_lines(result.calendar_bytes)
        events = _events(result.calendar_bytes)

        self.assertEqual("BEGIN:VCALENDAR", logical[0])
        self.assertEqual("END:VCALENDAR", logical[-1])
        self.assertIn("VERSION:2.0", logical)
        self.assertNotIn("METHOD:PUBLISH", logical)
        self.assertEqual(1, len(events))
        self.assertEqual("20260809T120000Z", events[0]["DTSTAMP"])
        self.assertEqual("20261001T010000Z", events[0]["DTSTART"])
        self.assertEqual("20261001T020000Z", events[0]["DTEND"])
        for name in ("DTSTAMP", "DTSTART", "DTEND"):
            self.assertRegex(events[0][name], r"^[0-9]{8}T[0-9]{6}Z$")
        self.assertNotRegex(result.calendar_bytes.decode(), r"[+-][0-9]{4}")
        self.assertNotIn(b"\n", result.calendar_bytes.replace(b"\r\n", b""))
        self.assertNotIn(b"\r", result.calendar_bytes.replace(b"\r\n", b""))
        for physical in result.calendar_bytes.split(b"\r\n")[:-1]:
            self.assertLessEqual(len(physical), 75)

    def test_hidden_input_digest_binds_typed_values_not_stale_revision(self) -> None:
        state = plan_to_trip_state(_plan())
        changed = replace(state, title="Changed In-Memory Calendar")

        first = project_private_ics(
            state,
            uid_namespace="synthetic-calendar-trip",
            generated_at=GENERATED_AT,
        )
        second = project_private_ics(
            changed,
            uid_namespace="synthetic-calendar-trip",
            generated_at=GENERATED_AT,
        )

        self.assertEqual(state.revision, changed.revision)
        self.assertNotEqual(first.input_sha256, second.input_sha256)
        self.assertNotEqual(first.calendar_bytes, second.calendar_bytes)
        self.assertNotIn(state.revision.encode(), first.calendar_bytes)

    def test_projection_result_is_factory_only_and_self_consistent(self) -> None:
        with self.assertRaisesRegex(ValueError, "must come from the projector"):
            PrivateIcsProjection(
                calendar_bytes=b"not-an-ics",
                input_sha256="a" * 64,
                calendar_sha256=hashlib.sha256(b"not-an-ics").hexdigest(),
                event_count=1,
            )

    def test_uid_survives_rename_retime_revision_and_trip_changes(self) -> None:
        base = _plan()
        renamed = _plan(
            places=(_place("activity-alpha", "Renamed Example", "10:00"),)
        )
        retimed = _plan(
            places=(_place("activity-alpha", "Example Museum", "11:00"),)
        )
        other_trip = _plan(trip_id="other-synthetic-calendar")

        base_uid = _events(_project(base).calendar_bytes)[0]["UID"]
        self.assertEqual(
            base_uid, _events(_project(renamed).calendar_bytes)[0]["UID"]
        )
        self.assertEqual(
            base_uid, _events(_project(retimed).calendar_bytes)[0]["UID"]
        )
        self.assertNotEqual(base["revision"], renamed["revision"])
        self.assertNotEqual(
            _project(base).calendar_bytes,
            _project(renamed).calendar_bytes,
        )
        self.assertNotEqual(
            base_uid, _events(_project(other_trip).calendar_bytes)[0]["UID"]
        )

    def test_uid_set_survives_activity_reordering(self) -> None:
        first = _plan(
            places=(
                _place("activity-alpha", "Alpha", "09:00"),
                _place("activity-beta", "Beta", "11:00"),
            )
        )
        reordered = _plan(
            places=(
                _place("activity-beta", "Beta", "09:00"),
                _place("activity-alpha", "Alpha", "11:00"),
            )
        )

        self.assertEqual(
            {event["UID"] for event in _events(_project(first).calendar_bytes)},
            {
                event["UID"]
                for event in _events(_project(reordered).calendar_bytes)
            },
        )

    def test_quarter_hour_timezone_and_overnight_rollover(self) -> None:
        quarter_hour = _plan(
            timezone_name="Asia/Kathmandu",
            places=(_place("activity-alpha", "Quarter Hour", "10:00"),),
        )
        overnight = _plan(
            available_start="22:00",
            available_end="02:00",
            places=(
                _place("activity-late", "Late Example", "23:30", duration_min=30),
                _place("activity-after", "After Midnight", "00:30", duration_min=30),
            ),
        )

        self.assertEqual(
            "20261001T041500Z",
            _events(_project(quarter_hour).calendar_bytes)[0]["DTSTART"],
        )
        overnight_events = _events(_project(overnight).calendar_bytes)
        self.assertEqual("20261001T143000Z", overnight_events[0]["DTSTART"])
        self.assertEqual("20261001T153000Z", overnight_events[1]["DTSTART"])

    def test_elapsed_duration_is_preserved_across_dst_transition(self) -> None:
        plan = _plan(
            timezone_name="America/New_York",
            day_date="2026-03-08",
            available_start="00:00",
            available_end="06:00",
            places=(
                _place(
                    "activity-alpha",
                    "DST Elapsed Example",
                    "01:30",
                    duration_min=120,
                ),
            ),
        )
        event = _events(_project(plan).calendar_bytes)[0]

        self.assertEqual("20260308T063000Z", event["DTSTART"])
        self.assertEqual("20260308T083000Z", event["DTEND"])

    def test_dst_gap_and_fold_fail_closed(self) -> None:
        gap = _plan(
            timezone_name="America/New_York",
            day_date="2026-03-08",
            available_start="00:00",
            available_end="05:00",
            places=(_place("activity-gap", "Gap", "02:30"),),
        )
        fold = _plan(
            timezone_name="America/New_York",
            day_date="2026-11-01",
            available_start="00:00",
            available_end="04:00",
            places=(_place("activity-fold", "Fold", "01:30"),),
        )

        self.assertEqual(
            "PRIVATE_ICS_LOCAL_TIME_NONEXISTENT",
            _error_code(lambda: _project(gap)),
        )
        self.assertEqual(
            "PRIVATE_ICS_LOCAL_TIME_AMBIGUOUS",
            _error_code(lambda: _project(fold)),
        )

    def test_missing_schedule_fails_without_partial_output(self) -> None:
        state = plan_to_trip_state(_plan())
        activity = state.activities[0]
        cases = (
            (
                "PRIVATE_ICS_START_MISSING",
                replace(
                    state,
                    activities=(
                        replace(
                            activity,
                            scheduled_start=None,
                            flexibility=Flexibility.MOVABLE,
                        ),
                    ),
                ),
            ),
            (
                "PRIVATE_ICS_DURATION_MISSING",
                replace(
                    state,
                    activities=(replace(activity, duration_min=None),),
                ),
            ),
            (
                "PRIVATE_ICS_DAY_TIMEZONE_MISSING",
                replace(
                    state,
                    days=(replace(state.days[0], timezone=None),),
                ),
            ),
            (
                "PRIVATE_ICS_DAY_BOUNDS_MISSING",
                replace(
                    state,
                    days=(replace(state.days[0], available_start=None),),
                ),
            ),
            (
                "PRIVATE_ICS_DAY_BOUNDS_MISSING",
                replace(
                    state,
                    days=(replace(state.days[0], available_end=None),),
                ),
            ),
            (
                "PRIVATE_ICS_SUBSECOND_UNSUPPORTED",
                replace(
                    state,
                    days=(
                        replace(
                            state.days[0],
                            available_start=time(8, microsecond=1),
                        ),
                    ),
                ),
            ),
        )
        for expected, candidate in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    expected,
                    _error_code(
                        lambda candidate=candidate: project_private_ics(
                            candidate,
                            uid_namespace="synthetic-calendar-trip",
                            generated_at=GENERATED_AT,
                        )
                    ),
                )

    def test_overnight_schedule_without_day_bounds_fails_closed(self) -> None:
        bounded_state = plan_to_trip_state(
            _plan(
                available_start="22:00",
                available_end="02:00",
                places=(
                    _place("activity-late", "Late", "23:30", duration_min=30),
                    _place("activity-after", "After", "00:30", duration_min=30),
                ),
            )
        )
        subsecond_bounds = replace(
            bounded_state,
            days=(
                replace(
                    bounded_state.days[0],
                    available_start=time(22, microsecond=1),
                ),
            ),
        )
        state = replace(
            bounded_state,
            days=(
                replace(
                    bounded_state.days[0],
                    available_start=None,
                    available_end=None,
                ),
            ),
        )

        self.assertEqual(
            "PRIVATE_ICS_SUBSECOND_UNSUPPORTED",
            _error_code(
                lambda: project_private_ics(
                    subsecond_bounds,
                    uid_namespace="synthetic-calendar-trip",
                    generated_at=GENERATED_AT,
                )
            ),
        )
        self.assertEqual(
            "PRIVATE_ICS_DAY_BOUNDS_MISSING",
            _error_code(
                lambda: project_private_ics(
                    state,
                    uid_namespace="synthetic-calendar-trip",
                    generated_at=GENERATED_AT,
                )
            ),
        )

    def test_projection_does_not_decide_readiness_or_emit_evidence_state(self) -> None:
        state = plan_to_trip_state(_plan())
        state = replace(
            state,
            activities=(
                replace(
                    state.activities[0],
                    evidence_state=EvidenceState.UNVERIFIED,
                ),
            ),
        )

        result = project_private_ics(
            state,
            uid_namespace="synthetic-calendar-trip",
            generated_at=GENERATED_AT,
        )

        self.assertEqual(1, result.event_count)
        self.assertNotIn(b"unverified", result.calendar_bytes)

    def test_synthetic_active_identity_is_rejected_but_inactive_is_ignored(self) -> None:
        plan = _plan(
            places=(
                _place("stable-active", "Stable", "10:00"),
                _place(
                    "synthetic-candidate",
                    "Candidate",
                    "12:00",
                    decision_state="candidate",
                    evidence_state="unverified",
                ),
            )
        )
        state = plan_to_trip_state(plan)
        issue = CheckIssue(
            code="SYNTHETIC_ACTIVITY_IDS",
            severity=IssueSeverity.INFO,
            message="Synthetic fixture issue.",
            activity_ids=("synthetic-candidate",),
        )
        inactive_only = replace(state, load_issues=(issue,))
        active_issue = replace(
            state,
            load_issues=(replace(issue, activity_ids=("stable-active",)),),
        )

        self.assertEqual(
            1,
            project_private_ics(
                inactive_only,
                uid_namespace="synthetic-calendar-trip",
                generated_at=GENERATED_AT,
            ).event_count,
        )
        self.assertEqual(
            "PRIVATE_ICS_UNSTABLE_ACTIVITY_ID",
            _error_code(
                lambda: project_private_ics(
                    active_issue,
                    uid_namespace="synthetic-calendar-trip",
                    generated_at=GENERATED_AT,
                )
            ),
        )

    def test_synthetic_legacy_loader_provenance_is_enforced(self) -> None:
        def write_fixture(root: Path, *, explicit_id: str | None) -> None:
            (root / "trip.json").write_text(
                json.dumps(
                    {
                        "slug": "synthetic-legacy-calendar",
                        "title": "Synthetic Legacy Calendar",
                        "timezone": "Asia/Tokyo",
                        "date_range": "2026-10-01 ~ 2026-10-01",
                    }
                ),
                encoding="utf-8",
            )
            place: dict[str, object] = {
                "title": "Synthetic Legacy Event",
                "location_id": "synthetic-location",
                "time": "10:00",
                "duration_min": 60,
                "decision_state": "fixed",
                "flexibility": "fixed_time",
                "evidence_state": "verified",
            }
            if explicit_id is not None:
                place["activity_id"] = explicit_id
            (root / "itinerary.json").write_text(
                json.dumps(
                    {
                        "days": [
                            {
                                "day_id": "synthetic-day",
                                "day": 1,
                                "date": "2026-10-01",
                                "timezone": "Asia/Tokyo",
                                "available_start": "08:00",
                                "available_end": "20:00",
                                "start_location_id": "synthetic-location",
                                "end_location_id": "synthetic-location",
                                "places": [place],
                                "travel": [],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

        with tempfile.TemporaryDirectory() as temporary:
            missing_id_dir = Path(temporary) / "missing-id"
            explicit_id_dir = Path(temporary) / "explicit-id"
            missing_id_dir.mkdir()
            explicit_id_dir.mkdir()
            write_fixture(missing_id_dir, explicit_id=None)
            explicit = "activity-" + "a" * 32
            write_fixture(explicit_id_dir, explicit_id=explicit)
            missing_id = load_legacy_trip(missing_id_dir)
            explicit_id = load_legacy_trip(explicit_id_dir)

        self.assertEqual(
            "PRIVATE_ICS_UNSTABLE_ACTIVITY_ID",
            _error_code(
                lambda: project_private_ics(
                    missing_id,
                    uid_namespace="synthetic-legacy-namespace",
                    generated_at=GENERATED_AT,
                )
            ),
        )
        result = project_private_ics(
            explicit_id,
            uid_namespace="synthetic-legacy-namespace",
            generated_at=GENERATED_AT,
        )
        self.assertEqual(1, result.event_count)

    def test_active_activity_must_have_exactly_one_day_membership(self) -> None:
        state = plan_to_trip_state(_plan())
        detached = replace(
            state,
            days=(replace(state.days[0], activity_ids=()),),
        )

        self.assertEqual(
            "PRIVATE_ICS_ACTIVITY_MEMBERSHIP_INVALID",
            _error_code(
                lambda: project_private_ics(
                    detached,
                    uid_namespace="synthetic-calendar-trip",
                    generated_at=GENERATED_AT,
                )
            ),
        )

    def test_candidate_and_private_only_fields_are_not_projected(self) -> None:
        sentinel = "PRIVATE-SENTINEL-DO-NOT-PROJECT"
        plan = _plan(
            places=(
                _place("activity-alpha", "Visible Summary", "10:00"),
                _place(
                    "activity-candidate",
                    sentinel,
                    "12:00",
                    decision_state="candidate",
                    evidence_state="unverified",
                ),
            )
        )
        state = plan_to_trip_state(plan)
        active = replace(
            state.activities[0],
            note=sentinel,
            maps_query=sentinel,
            lat=1.001,
            lng=1.002,
        )
        state = replace(state, activities=(active, state.activities[1]))
        result = project_private_ics(
            state,
            uid_namespace="synthetic-calendar-trip",
            generated_at=GENERATED_AT,
        )

        self.assertNotIn(sentinel.encode(), result.calendar_bytes)
        self.assertEqual(1, result.event_count)
        self.assertEqual("Visible Summary", _events(result.calendar_bytes)[0]["SUMMARY"])

    def test_text_escaping_blocks_component_injection_and_folds_unicode(self) -> None:
        title = "例" * 30 + "\\comma,semi;\r\nBEGIN:VEVENT"
        result = _project(
            _plan(places=(_place("activity-alpha", title, "10:00"),))
        )
        logical = _logical_lines(result.calendar_bytes)
        event = _events(result.calendar_bytes)[0]

        self.assertEqual(1, logical.count("BEGIN:VEVENT"))
        self.assertIn("\\\\comma\\,semi\\;\\nBEGIN:VEVENT", event["SUMMARY"])
        self.assertTrue(
            any(line.startswith(b" ") for line in result.calendar_bytes.split(b"\r\n"))
        )
        for physical in result.calendar_bytes.split(b"\r\n")[:-1]:
            self.assertLessEqual(len(physical), 75)

    def test_folding_boundaries_are_octet_safe(self) -> None:
        for summary_length, expected_summary_lines in ((66, 1), (67, 1), (68, 2)):
            with self.subTest(summary_length=summary_length):
                result = _project(
                    _plan(
                        places=(
                            _place(
                                "activity-alpha",
                                "a" * summary_length,
                                "10:00",
                            ),
                        )
                    )
                )
                physical_lines = result.calendar_bytes.split(b"\r\n")
                summary_index = next(
                    index
                    for index, line in enumerate(physical_lines)
                    if line.startswith(b"SUMMARY:")
                )
                summary_lines = [physical_lines[summary_index]]
                for line in physical_lines[summary_index + 1 :]:
                    if not line.startswith(b" "):
                        break
                    summary_lines.append(line)
                self.assertEqual(expected_summary_lines, len(summary_lines))
                self.assertTrue(all(len(line) <= 75 for line in summary_lines))

        unicode_summary = "a" * 65 + "🙂"
        unicode_result = _project(
            _plan(
                places=(
                    _place("activity-alpha", unicode_summary, "10:00"),
                )
            )
        )
        self.assertEqual(
            unicode_summary,
            _events(unicode_result.calendar_bytes)[0]["SUMMARY"],
        )
        unicode_physical = unicode_result.calendar_bytes.split(b"\r\n")
        unicode_summary_index = next(
            index
            for index, line in enumerate(unicode_physical)
            if line.startswith(b"SUMMARY:")
        )
        self.assertEqual(
            [73, 5],
            [
                len(unicode_physical[unicode_summary_index]),
                len(unicode_physical[unicode_summary_index + 1]),
            ],
        )
        self.assertTrue(
            all(
                len(line) <= 75
                for line in unicode_result.calendar_bytes.split(b"\r\n")[:-1]
            )
        )
        self.assertTrue(unicode_result.calendar_bytes.endswith(b"END:VCALENDAR\r\n"))
        self.assertFalse(unicode_result.calendar_bytes.endswith(b"\r\n\r\n"))

    def test_control_subsecond_and_fractional_second_failures_are_value_free(self) -> None:
        sentinel = "PRIVATE-SENTINEL"
        state = plan_to_trip_state(_plan())
        activity = state.activities[0]
        bad_title = replace(
            state,
            activities=(replace(activity, title=sentinel + "\x00"),),
        )
        cases = (
            (
                "PRIVATE_ICS_TEXT_INVALID",
                lambda: project_private_ics(
                    bad_title,
                    uid_namespace="synthetic-calendar-trip",
                    generated_at=GENERATED_AT,
                ),
            ),
            (
                "PRIVATE_ICS_SUBSECOND_UNSUPPORTED",
                lambda: project_private_ics(
                    state,
                    uid_namespace="synthetic-calendar-trip",
                    generated_at=GENERATED_AT.replace(microsecond=1),
                ),
            ),
            (
                "PRIVATE_ICS_DURATION_INVALID",
                lambda: project_private_ics(
                    replace(
                        state,
                        activities=(replace(activity, duration_min=0.001),),
                    ),
                    uid_namespace="synthetic-calendar-trip",
                    generated_at=GENERATED_AT,
                ),
            ),
        )
        for expected, call in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(PrivateIcsProjectionError) as caught:
                    call()
                self.assertEqual(expected, caught.exception.code)
                self.assertNotIn(sentinel, str(caught.exception))
                self.assertNotIn(sentinel, repr(caught.exception))

    def test_hostile_dtstamp_timezone_failure_is_value_free(self) -> None:
        sentinel = "PRIVATE-SENTINEL-TZ"

        class HostileTimezone(tzinfo):
            def utcoffset(self, value):
                raise TypeError(sentinel)

            def dst(self, value):
                return timedelta(0)

        generated_at = datetime(2026, 8, 9, 12, tzinfo=HostileTimezone())
        with self.assertRaises(PrivateIcsProjectionError) as caught:
            _project(_plan(), generated_at=generated_at)

        self.assertEqual("PRIVATE_ICS_DTSTAMP_INVALID", caught.exception.code)
        self.assertNotIn(sentinel, str(caught.exception))
        self.assertNotIn(sentinel, repr(caught.exception))

    def test_timezone_database_failure_is_value_free(self) -> None:
        sentinel = "PRIVATE-SENTINEL-TZDB"
        with mock.patch(
            "trip_planner.private_ics.ZoneInfo",
            side_effect=OSError(sentinel),
        ):
            with self.assertRaises(PrivateIcsProjectionError) as caught:
                _project(_plan())

        self.assertEqual("PRIVATE_ICS_TIMEZONE_INVALID", caught.exception.code)
        self.assertNotIn(sentinel, str(caught.exception))
        self.assertNotIn(sentinel, repr(caught.exception))

    def test_hostile_primitive_subclasses_fail_with_fixed_codes(self) -> None:
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

        state = plan_to_trip_state(_plan())
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
            ("PRIVATE_ICS_DURATION_INVALID", duration_state),
            ("PRIVATE_ICS_STATE_SCHEMA_UNSUPPORTED", schema_state),
        )
        for expected, candidate in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(PrivateIcsProjectionError) as caught:
                    project_private_ics(
                        candidate,
                        uid_namespace="synthetic-calendar-trip",
                        generated_at=GENERATED_AT,
                    )
                self.assertEqual(expected, caught.exception.code)
                self.assertNotIn(sentinel, str(caught.exception))
                self.assertNotIn(sentinel, repr(caught.exception))

    def test_identity_revision_schema_empty_and_bounds_fail_closed(self) -> None:
        state = plan_to_trip_state(_plan())
        candidate_only = plan_to_trip_state(
            _plan(
                places=(
                    _place(
                        "activity-candidate",
                        "Candidate",
                        "10:00",
                        decision_state="candidate",
                        evidence_state="unverified",
                    ),
                )
            )
        )
        cases = (
            (
                "PRIVATE_ICS_UID_NAMESPACE_INVALID",
                lambda: project_private_ics(
                    state, uid_namespace=" bad ", generated_at=GENERATED_AT
                ),
            ),
            (
                "PRIVATE_ICS_UID_NAMESPACE_INVALID",
                lambda: project_private_ics(
                    state,
                    uid_namespace="x" * 257,
                    generated_at=GENERATED_AT,
                ),
            ),
            (
                "PRIVATE_ICS_SOURCE_REVISION_INVALID",
                lambda: project_private_ics(
                    replace(state, revision="bad"),
                    uid_namespace="synthetic-calendar-trip",
                    generated_at=GENERATED_AT,
                ),
            ),
            (
                "PRIVATE_ICS_STATE_SCHEMA_UNSUPPORTED",
                lambda: project_private_ics(
                    replace(state, schema_version="unknown/v1"),
                    uid_namespace="synthetic-calendar-trip",
                    generated_at=GENERATED_AT,
                ),
            ),
            (
                "PRIVATE_ICS_NO_EVENTS",
                lambda: project_private_ics(
                    candidate_only,
                    uid_namespace="synthetic-calendar-trip",
                    generated_at=GENERATED_AT,
                ),
            ),
            (
                "PRIVATE_ICS_TIMEZONE_INVALID",
                lambda: project_private_ics(
                    replace(
                        state,
                        days=(replace(state.days[0], timezone="x" * 257),),
                    ),
                    uid_namespace="synthetic-calendar-trip",
                    generated_at=GENERATED_AT,
                ),
            ),
        )
        for expected, call in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, _error_code(call))

        with mock.patch("trip_planner.private_ics.MAX_PRIVATE_ICS_TEXT_BYTES", 4):
            self.assertEqual(
                "PRIVATE_ICS_TEXT_LIMIT_EXCEEDED",
                _error_code(
                    lambda: project_private_ics(
                        state,
                        uid_namespace="synthetic-calendar-trip",
                        generated_at=GENERATED_AT,
                    )
                ),
            )
        with mock.patch("trip_planner.private_ics.MAX_PRIVATE_ICS_BYTES", 100):
            self.assertEqual(
                "PRIVATE_ICS_ARTIFACT_LIMIT_EXCEEDED",
                _error_code(
                    lambda: project_private_ics(
                        state,
                        uid_namespace="synthetic-calendar-trip",
                        generated_at=GENERATED_AT,
                    )
                ),
            )
        with mock.patch(
            "trip_planner.private_ics._MAX_PRIVATE_ICS_AGGREGATE_TEXT_BYTES",
            20,
        ):
            self.assertEqual(
                "PRIVATE_ICS_AGGREGATE_TEXT_LIMIT_EXCEEDED",
                _error_code(
                    lambda: project_private_ics(
                        state,
                        uid_namespace="synthetic-calendar-trip",
                        generated_at=GENERATED_AT,
                    )
                ),
            )
        two_activity_state = plan_to_trip_state(
            _plan(
                places=(
                    _place("activity-alpha", "Alpha", "09:00"),
                    _place("activity-beta", "Beta", "11:00"),
                )
            )
        )
        with mock.patch("trip_planner.private_ics._MAX_PRIVATE_ICS_INPUT_ITEMS", 1):
            self.assertEqual(
                "PRIVATE_ICS_INPUT_LIMIT_EXCEEDED",
                _error_code(
                    lambda: project_private_ics(
                        two_activity_state,
                        uid_namespace="synthetic-calendar-trip",
                        generated_at=GENERATED_AT,
                    )
                ),
            )

    def test_event_limit_and_uid_collision_are_checked(self) -> None:
        plan = _plan(
            places=(
                _place("activity-alpha", "Alpha", "09:00"),
                _place("activity-beta", "Beta", "11:00"),
            )
        )
        state = plan_to_trip_state(plan)
        with mock.patch("trip_planner.private_ics.MAX_PRIVATE_ICS_EVENTS", 1):
            self.assertEqual(
                "PRIVATE_ICS_EVENT_LIMIT_EXCEEDED",
                _error_code(
                    lambda: project_private_ics(
                        state,
                        uid_namespace="synthetic-calendar-trip",
                        generated_at=GENERATED_AT,
                    )
                ),
            )
        with mock.patch(
            "trip_planner.private_ics._uid_for",
            return_value="collision@private.trip-planner",
        ):
            self.assertEqual(
                "PRIVATE_ICS_UID_COLLISION",
                _error_code(
                    lambda: project_private_ics(
                        state,
                        uid_namespace="synthetic-calendar-trip",
                        generated_at=GENERATED_AT,
                    )
                ),
            )

    def test_subprocess_output_ignores_cwd_process_timezone_and_hash_seed(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        program = r'''
import sys
from datetime import date, datetime, time, timezone
from trip_planner.models import Activity, DaySpec, DecisionState, EvidenceState, Flexibility, TripState
from trip_planner.private_ics import project_private_ics
day = DaySpec(day_id="day", date=date(2026, 10, 1), timezone="Asia/Tokyo", available_start=time(8), available_end=time(20), start_location_id="loc", end_location_id="loc", activity_ids=("activity",))
activity = Activity(activity_id="activity", day_id="day", order=0, title="Synthetic", location_id="loc", scheduled_start=time(10), duration_min=60, decision_state=DecisionState.FIXED, flexibility=Flexibility.FIXED_TIME, evidence_state=EvidenceState.VERIFIED)
state = TripState(slug="synthetic", title="Synthetic", timezone="Asia/Tokyo", days=(day,), activities=(activity,), schema_version="legacy-v1", revision="a" * 64, start_date=date(2026, 10, 1), end_date=date(2026, 10, 1))
result = project_private_ics(state, uid_namespace="stable-trip", generated_at=datetime(2026, 8, 9, 12, tzinfo=timezone.utc))
sys.stdout.buffer.write(result.calendar_bytes)
'''
        outputs: list[bytes] = []
        settings = (
            ("UTC", "1", "C"),
            ("Pacific/Honolulu", "987", "C.UTF-8"),
        )
        for process_timezone, hash_seed, locale_name in settings:
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


if __name__ == "__main__":
    unittest.main()
