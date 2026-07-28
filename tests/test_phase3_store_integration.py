"""Offline integration of Phase 3 candidates with the canonical TripStore."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

from trip_planner.mutations import ApprovalGrant
from trip_planner.scheduling import (
    ScheduleAssignment,
    build_schedule_candidate,
    candidate_to_plan_patch,
    schedule_problem_from_plan,
)
from trip_planner.store import TripStore


EVALUATION_AT = datetime(2026, 7, 28, tzinfo=timezone.utc)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class Phase3StoreIntegrationTests(unittest.TestCase):
    def test_migrated_canonical_trip_id_projects_previews_and_applies(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trips_root = Path(temporary) / "trips"
            slug = "phase3-store-fixture"
            data_dir = trips_root / slug / "data"
            data_dir.mkdir(parents=True)
            _write_json(
                data_dir / "trip.json",
                {
                    "title": "Phase 3 store fixture",
                    "date_range": "2026-10-01 ~ 2026-10-01",
                    "cities": ["Fixture City"],
                    "slug": slug,
                    "timezone": "Asia/Seoul",
                },
            )
            _write_json(
                data_dir / "itinerary.json",
                {
                    "available_modes": ["walking"],
                    "days": [
                        {
                            "day": 1,
                            "date": "2026-10-01",
                            "title": "Fixture day",
                            "available_start": "08:00",
                            "available_end": "18:00",
                            "start_location_id": "location-alpha",
                            "end_location_id": "location-beta",
                            "places": [
                                {
                                    "title": "Alpha",
                                    "location_id": "location-alpha",
                                    "time": "09:00",
                                    "duration_min": 30,
                                    "decision_state": "selected",
                                    "flexibility": "movable",
                                    "evidence_state": "verified",
                                },
                                {
                                    "title": "Beta",
                                    "location_id": "location-beta",
                                    "time": "10:00",
                                    "duration_min": 30,
                                    "decision_state": "selected",
                                    "flexibility": "movable",
                                    "evidence_state": "verified",
                                },
                            ],
                            "travel": [
                                {
                                    "from": 0,
                                    "to": 1,
                                    "recommended_mode": "walking",
                                    "modes": {
                                        "walking": {
                                            "duration_min": 10,
                                            "evidence_state": "verified",
                                        }
                                    },
                                },
                            ],
                        }
                    ],
                },
            )

            store = TripStore(trips_root, slug)
            migrated = store.commit_migration(store.preview_migration())
            self.assertTrue(migrated.success, migrated.to_dict())
            plan = store.load_plan()
            self.assertNotEqual(slug, plan["trip_id"])

            schedule_problem = schedule_problem_from_plan(
                plan,
                evaluation_at=EVALUATION_AT,
            )
            by_title = {
                activity.title: activity
                for activity in schedule_problem.state.activities
            }
            alpha = by_title["Alpha"]
            beta = by_title["Beta"]
            day_id = schedule_problem.state.days[0].day_id
            candidate = build_schedule_candidate(
                schedule_problem,
                (
                    ScheduleAssignment(
                        alpha.activity_id,
                        day_id,
                        0,
                        time(9, 30),
                    ),
                    ScheduleAssignment(
                        beta.activity_id,
                        day_id,
                        1,
                        time(10, 30),
                    ),
                ),
                solver="store-integration",
            )
            patch = candidate_to_plan_patch(schedule_problem, candidate)

            self.assertEqual(plan["trip_id"], patch.trip_id)
            self.assertNotEqual(slug, patch.trip_id)
            denied = store.preview_patch(
                patch,
                evaluation_at=EVALUATION_AT,
            )
            denied_codes = {problem.code for problem in denied.problems}
            self.assertNotIn("TRIP_ID_MISMATCH", denied_codes)
            self.assertIn("APPROVAL_REQUIRED", denied_codes)
            self.assertIsNotNone(denied.required_approval_scope)

            grant = ApprovalGrant(
                approval_id="approve-migrated-schedule",
                scope_digest=denied.required_approval_scope,
                approved_by="phase3-integration",
                approved_at=EVALUATION_AT.isoformat(),
            )
            preview = store.preview_patch(
                patch,
                approvals=(grant,),
                evaluation_at=EVALUATION_AT,
            )
            self.assertTrue(preview.success, preview.to_dict())
            self.assertEqual("preview_ready", preview.status)
            self.assertEqual("needs_verification", preview.check_status)
            self.assertEqual((day_id,), preview.draft.invalidated_day_ids)
            self.assertTrue(
                any(
                    change.kind == "invalidate"
                    and change.field == "travel"
                    for change in preview.draft.changes
                )
            )

            applied = store.apply_patch(
                patch,
                approvals=(grant,),
                evaluation_at=EVALUATION_AT,
            )
            self.assertTrue(applied.success, applied.to_dict())
            self.assertEqual("applied", applied.status)
            self.assertEqual("needs_verification", applied.check_status)
            self.assertNotEqual(plan["revision"], applied.current_revision)

            current = store.load_plan()
            day = current["state"]["itinerary"]["days"][0]
            self.assertEqual(
                ["Alpha", "Beta"],
                [activity["title"] for activity in day["places"]],
            )
            self.assertEqual(
                ["09:30:00", "10:30:00"],
                [activity["time"] for activity in day["places"]],
            )
            self.assertEqual([], day["travel"])
            self.assertIn(
                f"schedule:{candidate.candidate_id}",
                current["receipts"],
            )


if __name__ == "__main__":
    unittest.main()
