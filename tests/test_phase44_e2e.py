"""Canned Busan and Hokkaido end-to-end Phase 4.4 acceptance fixtures."""

from __future__ import annotations

import copy
import unittest
from datetime import date

from tests.test_phase44_place_details import (
    NOW,
    _Fixture,
    _Transport,
    _current_body,
    _regular_body,
    _response,
)
from trip_planner.availability import AvailabilityDisposition
from trip_planner.codec import build_plan
from trip_planner.composition import compose_trip_state
from trip_planner.place_details import (
    PlaceDetailsAttemptBudget,
    PlaceDetailsKind,
    execute_google_place_details_batch,
)
from trip_planner.timeline import evaluate_composed_timeline


def _plan(
    *,
    trip_id: str,
    city: str,
    timezone_name: str,
) -> dict[str, object]:
    return build_plan(
        trip_id=trip_id,
        generation=1,
        state={
            "trip": {
                "slug": trip_id,
                "title": f"{city} acceptance fixture",
                "timezone": timezone_name,
                "date_range": "2026-07-29 ~ 2026-07-29",
                "cities": [city],
            },
            "itinerary": {
                "available_modes": ["walking"],
                "days": [
                    {
                        "day_id": "day-1",
                        "day": 1,
                        "date": "2026-07-29",
                        "timezone": timezone_name,
                        "available_start": "08:00",
                        "available_end": "20:00",
                        "start_location_id": "venue",
                        "end_location_id": "venue",
                        "places": [
                            {
                                "activity_id": "visit",
                                "title": f"{city} venue",
                                "location_id": "venue",
                                "time": "10:00",
                                "duration_min": 120,
                                "decision_state": "selected",
                                "flexibility": "fixed_time",
                                "evidence_state": "verified",
                            }
                        ],
                        "travel": [],
                    }
                ],
            },
        },
    )


class Phase44EndToEndTests(unittest.TestCase):
    def test_busan_current_hours_become_full_duration_hard_constraint(
        self,
    ) -> None:
        fixture = _Fixture(place_id="ChIJ-busan-canned")
        request = fixture.request(
            PlaceDetailsKind.CURRENT_HOURS,
            target_start=date(2026, 7, 29),
            target_end=date(2026, 7, 29),
        )
        batch = execute_google_place_details_batch(
            (request,),
            _Transport(
                _response(
                    _current_body(place_id=fixture.place_id)
                )
            ),
            session=fixture.session,
            attempt_budget=PlaceDetailsAttemptBudget(1),
            clock=lambda: NOW,
        )
        canonical = _plan(
            trip_id="busan-phase44",
            city="Busan",
            timezone_name="Asia/Seoul",
        )
        before = copy.deepcopy(canonical)
        composed = compose_trip_state(
            canonical,
            batch.current.snapshot(evaluation_at=NOW),
            availability_keys=(request.provider_request.fact_keys[0],),
        )
        report = evaluate_composed_timeline(composed, now=NOW)

        self.assertEqual(before, canonical)
        self.assertEqual(
            AvailabilityDisposition.HARD_CURRENT,
            composed.activity_availability[0].disposition,
        )
        self.assertFalse(
            {
                "OPENING_HOURS_VIOLATION",
                "OPENING_HOURS_NEEDS_VERIFICATION",
            }.intersection(item.code for item in report.issues)
        )
        self.assertTrue(report.day_summaries[0].timing_verified)
        self.assertEqual(
            fixture.snapshot.store_revision,
            batch.current.store_revision,
        )
        self.assertNotIn(
            "activity_availability",
            composed.to_dict(),
        )

    def test_hokkaido_regular_hours_never_become_green(self) -> None:
        fixture = _Fixture(place_id="ChIJ-hokkaido-canned")
        request = fixture.request(
            PlaceDetailsKind.REGULAR_HOURS,
            target_start=date(2026, 7, 29),
            target_end=date(2026, 7, 29),
        )
        batch = execute_google_place_details_batch(
            (request,),
            _Transport(
                _response(
                    _regular_body(
                        [
                            {
                                "open": {"day": 3, "hour": 9},
                                "close": {"day": 3, "hour": 17},
                            }
                        ],
                        place_id=fixture.place_id,
                        timezone_name="Asia/Tokyo",
                    )
                )
            ),
            session=fixture.session,
            attempt_budget=PlaceDetailsAttemptBudget(1),
            clock=lambda: NOW,
        )
        composed = compose_trip_state(
            _plan(
                trip_id="hokkaido-phase44",
                city="Hokkaido",
                timezone_name="Asia/Tokyo",
            ),
            batch.current.snapshot(evaluation_at=NOW),
            availability_keys=(request.provider_request.fact_keys[0],),
        )
        report = evaluate_composed_timeline(composed, now=NOW)

        availability = composed.activity_availability[0]
        self.assertEqual(
            AvailabilityDisposition.NEEDS_VERIFICATION,
            availability.disposition,
        )
        self.assertEqual("regular_opening_hours", availability.reason)
        codes = {item.code for item in report.issues}
        self.assertIn("OPENING_HOURS_NEEDS_VERIFICATION", codes)
        self.assertNotIn("OPENING_HOURS_VIOLATION", codes)
        self.assertFalse(report.day_summaries[0].timing_verified)


if __name__ == "__main__":
    unittest.main()
