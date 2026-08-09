"""Phase 5.33 read-only canonical dispatch for the unified ``tripctl``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import trip_planner.canonical_tripctl as canonical_tripctl_module
from trip_planner.codec import build_plan, encode_plan
from trip_planner.tripctl import (
    TripctlError,
    inspect_trip,
    inspection_failure,
    validate_trip,
    validation_failure,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "tripctl.py"
PRIVATE = "private-address-place-id-token-price-37.5000-127.0000"


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _canonical_plan(*, generation: int = 1) -> dict[str, Any]:
    return build_plan(
        trip_id="phase533-canonical",
        generation=generation,
        state={
            "trip": {
                "slug": "phase533-canonical",
                "title": PRIVATE,
                "timezone": "UTC",
                "date_range": "2026-10-01 ~ 2026-10-01",
                "cities": [PRIVATE],
            },
            "itinerary": {
                "available_modes": ["walking"],
                "days": [
                    {
                        "day_id": "day-1",
                        "day": 1,
                        "date": "2026-10-01",
                        "timezone": "UTC",
                        "available_start": "08:00",
                        "available_end": "20:00",
                        "start_location_id": "location-a",
                        "end_location_id": "location-a",
                        "places": [
                            {
                                "activity_id": "activity-a",
                                "title": PRIVATE,
                                "location_id": "location-a",
                                "time": "09:15",
                                "duration_min": 60,
                                "decision_state": "selected",
                                "flexibility": "movable",
                                "evidence_state": "unverified",
                            }
                        ],
                        "travel": [],
                    }
                ],
            },
        },
    )


def _canonical_trip(root: Path) -> Path:
    trip_dir = root / "canonical-fixture"
    data_dir = trip_dir / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "plan.json").write_bytes(encode_plan(_canonical_plan()))
    # Adjacent legacy bytes must never win once a canonical marker exists.
    (data_dir / "trip.json").write_text(PRIVATE, encoding="utf-8")
    (data_dir / "itinerary.json").write_text(PRIVATE, encoding="utf-8")
    return trip_dir


class Phase533TripctlCanonicalTests(unittest.TestCase):
    def test_canonical_dispatch_is_deterministic_redacted_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _canonical_trip(Path(temporary))
            before = _tree_bytes(trip_dir)

            first_inspection = inspect_trip(trip_dir)
            second_inspection = inspect_trip(trip_dir / "data")
            first_validation = validate_trip(trip_dir)
            second_validation = validate_trip(trip_dir / "data")

            self.assertEqual(first_inspection, second_inspection)
            self.assertEqual(first_validation, second_validation)
            self.assertTrue(first_inspection["ok"])
            self.assertEqual("canonical", first_inspection["storage_mode"])
            self.assertEqual("waiting_external", first_inspection["status"])
            self.assertEqual("refresh_evidence", first_inspection["next_action"])
            self.assertFalse(first_inspection["requires_user_review"])
            self.assertFalse(
                first_inspection["result"]["runtime_evidence_loaded"]
            )
            self.assertEqual(1, first_inspection["result"]["day_count"])
            self.assertEqual(1, first_inspection["result"]["activity_count"])
            self.assertEqual(
                "CANONICAL_RUNTIME_EVIDENCE_NOT_LOADED",
                first_inspection["problems"][0]["code"],
            )

            self.assertTrue(first_validation["ok"])
            self.assertEqual("canonical", first_validation["storage_mode"])
            self.assertEqual("waiting_external", first_validation["status"])
            self.assertEqual("refresh_evidence", first_validation["next_action"])
            self.assertEqual(
                "needs_verification",
                first_validation["result"]["timeline_status"],
            )
            self.assertFalse(
                first_validation["result"]["runtime_evidence_loaded"]
            )

            rendered = json.dumps(
                {"inspect": first_inspection, "validate": first_validation},
                ensure_ascii=False,
                sort_keys=True,
            )
            self.assertNotIn(PRIVATE, rendered)
            self.assertNotIn(str(trip_dir), rendered)
            self.assertNotIn("09:15", rendered)
            self.assertNotIn('"trip_id"', rendered)
            self.assertNotIn('"title"', rendered)
            self.assertNotIn("travel_ready", rendered)
            self.assertEqual(before, _tree_bytes(trip_dir))

    def test_canonical_source_drift_is_retryable_without_partial_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _canonical_trip(Path(temporary))
            plan_path = trip_dir / "data" / "plan.json"
            original_decode = canonical_tripctl_module.decode_plan
            original_evaluate = canonical_tripctl_module.evaluate_timeline

            def decode_then_drift(raw: object) -> object:
                plan = original_decode(raw)
                plan_path.write_bytes(plan_path.read_bytes() + b" ")
                return plan

            with patch(
                "trip_planner.canonical_tripctl.decode_plan",
                side_effect=decode_then_drift,
            ):
                with self.assertRaises(TripctlError) as inspection_raised:
                    inspect_trip(trip_dir)

            self.assertEqual(
                "STALE_CANONICAL_PLAN",
                inspection_raised.exception.code,
            )
            inspection_rejection = inspection_failure(
                inspection_raised.exception
            )
            self.assertTrue(inspection_rejection["retryable"])
            self.assertIsNone(inspection_rejection["result"])
            self.assertEqual(
                "retry_inspection",
                inspection_rejection["next_action"],
            )

            plan_path.write_bytes(encode_plan(_canonical_plan()))

            def evaluate_then_drift(state: object, *, now: object = None) -> object:
                plan_path.write_bytes(plan_path.read_bytes() + b" ")
                return original_evaluate(state, now=now)

            with patch(
                "trip_planner.canonical_tripctl.evaluate_timeline",
                side_effect=evaluate_then_drift,
            ):
                with self.assertRaises(TripctlError) as raised:
                    validate_trip(trip_dir)

            self.assertEqual("STALE_CANONICAL_PLAN", raised.exception.code)
            self.assertTrue(raised.exception.retryable)
            failure = validation_failure(raised.exception)
            self.assertFalse(failure["ok"])
            self.assertEqual("canonical", failure["storage_mode"])
            self.assertIsNone(failure["result"])
            self.assertTrue(failure["retryable"])
            self.assertEqual("retry_validation", failure["next_action"])
            self.assertNotIn(PRIVATE, json.dumps(failure))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO unavailable")
    def test_unsafe_canonical_marker_fails_closed_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = Path(temporary) / "unsafe-canonical"
            data_dir = trip_dir / "data"
            data_dir.mkdir(parents=True)
            (data_dir / "trip.json").write_text(PRIVATE, encoding="utf-8")
            (data_dir / "itinerary.json").write_text(PRIVATE, encoding="utf-8")
            os.mkfifo(data_dir / "plan.json")

            for command, failure_builder, expected_code in (
                (inspect_trip, inspection_failure, "CANONICAL_INSPECT_UNAVAILABLE"),
                (validate_trip, validation_failure, "CANONICAL_VALIDATE_UNAVAILABLE"),
            ):
                with self.subTest(command=command.__name__):
                    with self.assertRaises(TripctlError) as raised:
                        command(trip_dir)
                    self.assertEqual(expected_code, raised.exception.code)
                    failure = failure_builder(raised.exception)
                    self.assertEqual("canonical", failure["storage_mode"])
                    self.assertNotIn(PRIVATE, json.dumps(failure))

    def test_malformed_canonical_marker_never_falls_back_to_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _canonical_trip(Path(temporary))
            plan_path = trip_dir / "data" / "plan.json"
            plan_path.write_text(
                '{"schema_version":"first","schema_version":"second",'
                f'"private":"{PRIVATE}"}}',
                encoding="utf-8",
            )

            for command, failure_builder, expected_code in (
                (inspect_trip, inspection_failure, "CANONICAL_INSPECT_UNAVAILABLE"),
                (validate_trip, validation_failure, "CANONICAL_VALIDATE_UNAVAILABLE"),
            ):
                with self.subTest(command=command.__name__):
                    with self.assertRaises(TripctlError) as raised:
                        command(trip_dir)
                    self.assertEqual(expected_code, raised.exception.code)
                    failure = failure_builder(raised.exception)
                    self.assertEqual("canonical", failure["storage_mode"])
                    self.assertNotIn(PRIVATE, json.dumps(failure))

    def test_low_level_read_failure_stays_inside_the_json_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _canonical_trip(Path(temporary))
            with patch(
                "trip_planner.canonical_tripctl.os.read",
                side_effect=OSError(PRIVATE),
            ):
                with self.assertRaises(TripctlError) as raised:
                    inspect_trip(trip_dir)

            self.assertEqual("CANONICAL_INSPECT_UNAVAILABLE", raised.exception.code)
            failure = inspection_failure(raised.exception)
            self.assertFalse(failure["ok"])
            self.assertEqual("canonical", failure["storage_mode"])
            self.assertNotIn(PRIVATE, json.dumps(failure))

    def test_cli_dispatches_canonical_and_emits_only_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _canonical_trip(Path(temporary))
            for command in ("inspect", "validate"):
                with self.subTest(command=command):
                    completed = subprocess.run(
                        [sys.executable, str(SCRIPT), command, str(trip_dir)],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(0, completed.returncode, completed.stderr)
                    self.assertEqual("", completed.stderr)
                    payload = json.loads(completed.stdout)
                    self.assertTrue(payload["ok"])
                    self.assertEqual("canonical", payload["storage_mode"])
                    self.assertNotIn(PRIVATE, completed.stdout)
                    self.assertNotIn("travel_ready", completed.stdout)


if __name__ == "__main__":
    unittest.main()
