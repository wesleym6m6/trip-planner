"""Offline regressions for quarantined legacy opening-hours checks."""

from __future__ import annotations

import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.check_hours import check_place, check_visit_time, get_periods_for_day
from trip_planner.opening_hours import evaluate_opening_window


UTC = timezone.utc


class LegacyHoursTests(unittest.TestCase):
    def test_split_shift_requires_one_continuous_interval(self) -> None:
        arrival = datetime(2026, 7, 29, 11, 30, tzinfo=UTC)
        result = evaluate_opening_window(
            (
                (
                    datetime(2026, 7, 29, 9, tzinfo=UTC),
                    datetime(2026, 7, 29, 12, tzinfo=UTC),
                ),
                (
                    datetime(2026, 7, 29, 13, tzinfo=UTC),
                    datetime(2026, 7, 29, 17, tzinfo=UTC),
                ),
            ),
            arrival,
            timedelta(hours=1),
        )
        self.assertEqual("closed", result.status)

    def test_full_duration_and_half_open_boundaries(self) -> None:
        start = datetime(2026, 7, 29, 9, tzinfo=UTC)
        end = datetime(2026, 7, 29, 10, tzinfo=UTC)
        self.assertEqual(
            "open",
            evaluate_opening_window(
                ((start, end),), start, timedelta(hours=1)
            ).status,
        )
        self.assertEqual(
            "closed",
            evaluate_opening_window(
                ((start, end),), end, timedelta(minutes=1)
            ).status,
        )

    def test_overnight_and_unknown(self) -> None:
        self.assertEqual(
            "open",
            evaluate_opening_window(
                (
                    (
                        datetime(2026, 7, 29, 22, tzinfo=UTC),
                        datetime(2026, 7, 30, 2, tzinfo=UTC),
                    ),
                ),
                datetime(2026, 7, 30, 1, tzinfo=UTC),
                timedelta(minutes=30),
            ).status,
        )
        self.assertEqual(
            "unknown",
            evaluate_opening_window(
                None,
                datetime(2026, 7, 30, 1, tzinfo=UTC),
                timedelta(minutes=30),
            ).status,
        )

    def test_regular_schedule_never_returns_green_and_unknown_warns(self) -> None:
        cache = {
            "p": {
                "regular_opening_hours": {
                    "weekdayDescriptions": ["Monday: 09:00-17:00"] * 7,
                    "periods": [
                        {
                            "open": {"day": 1, "hour": 9},
                            "close": {"day": 1, "hour": 17},
                        }
                    ],
                },
                "time_zone": "UTC",
            }
        }
        regular = check_place(
            {
                "title": "Regular",
                "place_id": "p",
                "time": "10:00",
                "duration": 60,
            },
            cache,
            0,
            resolved_at=datetime(2026, 7, 27, 10, 0),
        )
        unknown = check_place(
            {"title": "Unknown", "place_id": "missing", "time": "10:00"},
            cache,
            0,
        )
        self.assertEqual("⚠️", regular["status"])
        self.assertIn("advisory", regular["note"])
        self.assertEqual("❓", unknown["status"])
        self.assertIn("需確認", unknown["note"])
        self.assertEqual(
            "in_range",
            check_visit_time(
                [(9 * 60, 10 * 60, "09:00", "10:00")], "09:30"
            )[0],
        )

    def test_full_mask_builder_is_quarantined_by_default(self) -> None:
        script = Path(__file__).parents[1] / "scripts" / "build_places_cache.py"
        completed = subprocess.run(
            [sys.executable, str(script)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("legacy-only", completed.stderr)

    def test_locale_text_never_closes_and_prior_overnight_is_included(self) -> None:
        hours = {
            "weekdayDescriptions": ["Sunday: Closed"] * 7,
            "periods": [
                {
                    "open": {"day": 1, "hour": 22},
                    "close": {"day": 2, "hour": 2},
                }
            ],
        }
        self.assertEqual(1, len(get_periods_for_day(hours, 2)))
        result = check_place(
            {"title": "Night", "place_id": "p", "time": "01:00"},
            {"p": {"regular_opening_hours": hours, "time_zone": "UTC"}},
            1,
            resolved_at=datetime(2026, 7, 28, 1, 0),
        )
        self.assertEqual("⚠️", result["status"])
        self.assertIn("advisory", result["note"])
        self.assertIsNone(result["hours"])

    def test_corrupt_locale_text_and_regular_closed_guess_remain_advisory(
        self,
    ) -> None:
        hours = {
            "weekdayDescriptions": [{"not": "text"}] * 7,
            "periods": [
                {
                    "open": {"day": 1, "hour": 9},
                    "close": {"day": 1, "hour": 17},
                }
            ],
        }
        result = check_place(
            {"title": "Late", "place_id": "p", "time": "20:00"},
            {"p": {"regular_opening_hours": hours, "time_zone": "UTC"}},
            0,
        )
        self.assertEqual("⚠️", result["status"])
        self.assertIsNone(result["hours"])
        self.assertIn("advisory", result["note"])
        self.assertIn("需", result["note"])

    def test_missing_periods_and_dst_ambiguity_are_not_open_or_closed(self) -> None:
        missing = check_place(
            {"title": "Missing", "place_id": "p", "time": "10:00"},
            {"p": {"regular_opening_hours": {"weekdayDescriptions": []}}},
            0,
        )
        self.assertEqual("❓", missing["status"])
        hours = {
            "periods": [
                {
                    "open": {"day": 0, "hour": 0},
                    "close": {"day": 0, "hour": 23},
                }
            ]
        }
        ambiguous = check_place(
            {"title": "DST", "place_id": "p", "time": "01:30"},
            {
                "p": {
                    "regular_opening_hours": hours,
                    "time_zone": "America/New_York",
                }
            },
            6,
            resolved_at=datetime(2026, 11, 1, 1, 30),
        )
        self.assertEqual("⚠️", ambiguous["status"])
        self.assertIn("DST", ambiguous["note"])


if __name__ == "__main__":
    unittest.main()
