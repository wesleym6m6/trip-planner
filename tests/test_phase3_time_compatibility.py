"""Compatibility regressions for lossless scheduler local-time text."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, time
from io import StringIO
from pathlib import Path

from scripts.check_hours import check_visit_time
from scripts.enrich_itinerary import format_departure_time
from scripts.generate_ics import generate_ics
from scripts.plan_compat import resolve_ordered_local_datetimes


class Phase3TimeCompatibilityTests(unittest.TestCase):
    def test_opening_hours_accepts_second_and_fraction_precision(self) -> None:
        status, details = check_visit_time(
            [(9 * 60, 10 * 60, "09:00", "10:00")],
            "09:30:15.250000",
        )

        self.assertEqual("in_range", status)
        self.assertEqual("09:00-10:00", details)

    def test_ics_parser_accepts_lossless_local_time(self) -> None:
        resolved = resolve_ordered_local_datetimes(
            date(2027, 1, 10), ("09:10:30.123456",)
        )
        self.assertEqual(
            time(9, 10, 30, 123456),
            resolved[0].time(),
        )

    def test_routes_departure_time_does_not_append_duplicate_seconds(
        self,
    ) -> None:
        self.assertEqual(
            "2027-01-10T09:10:30.123456+09:00",
            format_departure_time(
                "2027-01-10",
                "09:10:30.123456",
                "+09:00",
            ),
        )

    def test_ordered_time_resolver_rolls_midnight_to_next_date(self) -> None:
        resolved = resolve_ordered_local_datetimes(
            date(2026, 10, 1),
            ("23:50", "00:10", "01:30"),
            available_start="22:00",
            available_end="02:00",
        )

        self.assertEqual("2026-10-01T23:50:00", resolved[0].isoformat())
        self.assertEqual("2026-10-02T00:10:00", resolved[1].isoformat())
        self.assertEqual("2026-10-02T01:30:00", resolved[2].isoformat())
        self.assertEqual(4, resolved[1].weekday())

    def test_ics_overnight_events_keep_monotonic_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = Path(temporary) / "overnight-trip"
            data_dir = trip_dir / "data"
            data_dir.mkdir(parents=True)
            (data_dir / "trip.json").write_text(
                json.dumps(
                    {
                        "slug": "overnight-trip",
                        "title": "Overnight",
                        "date_range": "2026-10-01 ~ 2026-10-02",
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "itinerary.json").write_text(
                json.dumps(
                    {
                        "days": [
                            {
                                "day": 1,
                                "title": "Overnight day",
                                "available_start": "22:00",
                                "available_end": "02:00",
                                "places": [
                                    {"title": "Late", "time": "23:50"},
                                    {"title": "After", "time": "00:10"},
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "places_cache.json").write_text(
                json.dumps({"hotel": {"utc_offset_minutes": 540}}),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()):
                output_path = generate_ics(trip_dir)
            output = output_path.read_text(encoding="utf-8")

        self.assertIn("DTSTART:20261001T235000+0900", output)
        self.assertIn("DTEND:20261002T001000+0900", output)
        self.assertIn("DTSTART:20261002T001000+0900", output)
        self.assertEqual(
            "2027-01-10T09:10:00+09:00",
            format_departure_time(
                "2027-01-10",
                "09:10",
                "+09:00",
            ),
        )


if __name__ == "__main__":
    unittest.main()
