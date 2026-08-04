"""Offline contracts for bounded Phase 5 read-only ``tripctl`` commands."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from trip_planner.legacy_evidence import LegacyEvidencePreviewError
import trip_planner.legacy_timeline as legacy_timeline_module
import trip_planner.tripctl as tripctl_module
from trip_planner.tripctl import (
    TripctlError,
    inspect_trip,
    inspection_failure,
    validate_trip,
    validation_failure,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "tripctl.py"
TRIPS_ROOT = REPO_ROOT / "trips"
PRIVATE = "private-address-place-id-token-price-37.5000-127.0000"


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _legacy_trip(root: Path) -> Path:
    trip_dir = root / "legacy-fixture"
    data = trip_dir / "data"
    data.mkdir(parents=True)
    _write_json(
        data / "trip.json",
        {
            "slug": "legacy-fixture",
            "title": PRIVATE,
            "date_range": "2026-10-01 ~ 2026-10-02",
            "cities": ["Fixture"],
            "icon": "🧭",
        },
    )
    _write_json(
        data / "itinerary.json",
        {
            "days": [
                {
                    "date": "2026-10-01",
                    "places": [
                        {
                            "title": PRIVATE,
                            "place_id": PRIVATE,
                            "lat": 35.123456,
                            "lng": 129.123456,
                        }
                    ],
                    "travel": [
                        {"source": "api", "duration_min": 47, "token": PRIVATE},
                        {"source": "manual", "duration_min": 15},
                    ],
                }
            ]
        },
    )
    _write_json(data / "places_cache.json", {PRIVATE: {"address": PRIVATE}})
    return trip_dir


def _timeline_legacy_trip(root: Path) -> Path:
    """Create only the kernel's two legacy sources, with private sentinels."""

    trip_dir = root / "timeline-fixture"
    data = trip_dir / "data"
    data.mkdir(parents=True)
    _write_json(
        data / "trip.json",
        {
            "slug": "timeline-fixture",
            "title": PRIVATE,
            "date_range": "2026-10-01 ~ 2026-10-01",
            "cities": [PRIVATE],
            "icon": "🧭",
            "timezone": "UTC",
        },
    )
    _write_json(
        data / "itinerary.json",
        {
            "days": [
                {
                    "date": "2026-10-01",
                    "available_start": "08:00",
                    "available_end": "20:00",
                    "start_location_id": PRIVATE,
                    "end_location_id": PRIVATE,
                    "places": [
                        {
                            "activity_id": PRIVATE,
                            "title": PRIVATE,
                            "time": "09:15",
                            "duration_min": 45,
                            "lat": 35.123456,
                            "lng": 129.123456,
                            "evidence_state": "unverified",
                        }
                    ],
                    "travel": [],
                }
            ]
        },
    )
    return trip_dir


class Phase5TripctlTests(unittest.TestCase):
    def test_legacy_inspection_is_deterministic_redacted_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _legacy_trip(Path(temporary))
            before = _tree_bytes(trip_dir)

            first = inspect_trip(trip_dir)
            second = inspect_trip(trip_dir / "data")

            self.assertEqual(first, second)
            self.assertTrue(first["ok"])
            self.assertEqual("review_required", first["status"])
            self.assertEqual("legacy", first["result"]["storage_mode"])
            self.assertFalse(first["retryable"])
            self.assertFalse(first["pending_review_retained"])
            self.assertTrue(first["requires_user_review"])
            self.assertEqual([], first["result"]["cleanup_targets"])
            self.assertEqual(0, first["result"]["imports"])
            self.assertEqual(
                "review_legacy_preview",
                first["next_action"],
            )
            rendered = json.dumps(first, ensure_ascii=False, sort_keys=True)
            self.assertNotIn(PRIVATE, rendered)
            self.assertNotIn(str(trip_dir), rendered)
            self.assertNotIn("duration_min", rendered)
            self.assertEqual(before, _tree_bytes(trip_dir))

    def test_preview_drift_becomes_retryable_error_without_partial_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _legacy_trip(Path(temporary))
            with patch(
                "trip_planner.tripctl.verify_legacy_evidence_source",
                side_effect=LegacyEvidencePreviewError(
                    "STALE_LEGACY_EVIDENCE_PREVIEW",
                    "private-path-and-provider-value",
                ),
            ):
                with self.assertRaises(TripctlError) as raised:
                    inspect_trip(trip_dir)

            self.assertEqual("STALE_LEGACY_EVIDENCE_PREVIEW", raised.exception.code)
            self.assertTrue(raised.exception.retryable)
            failure = inspection_failure(raised.exception)
            self.assertFalse(failure["ok"])
            self.assertIsNone(failure["result"])
            self.assertTrue(failure["retryable"])
            self.assertEqual("retry_inspection", failure["next_action"])
            self.assertNotIn("private-path-and-provider-value", json.dumps(failure))

    def test_unverifiable_legacy_source_keeps_its_repair_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _legacy_trip(Path(temporary))
            (trip_dir / "data" / "places_cache.json").unlink()

            payload = inspect_trip(trip_dir)

            self.assertTrue(payload["ok"])
            self.assertEqual("repair_required", payload["status"])
            self.assertFalse(payload["result"]["source_verifiable"])
            self.assertFalse(payload["retryable"])
            self.assertEqual("repair_source", payload["next_action"])
            self.assertIn(
                "LEGACY_PLACE_CACHE_MISSING",
                {item["code"] for item in payload["problems"]},
            )

    def test_canonical_marker_is_refused_without_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _legacy_trip(Path(temporary))
            data = trip_dir / "data"
            (data / "plan.json").write_text(PRIVATE, encoding="utf-8")

            with self.assertRaises(TripctlError) as raised:
                inspect_trip(trip_dir)

            self.assertEqual("CANONICAL_INSPECT_UNAVAILABLE", raised.exception.code)
            failure = inspection_failure(raised.exception)
            self.assertEqual("canonical", failure["storage_mode"])
            self.assertEqual("use_developer_runtime", failure["next_action"])
            self.assertNotIn(PRIVATE, json.dumps(failure))

    def test_canonical_marker_created_after_preview_is_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _legacy_trip(Path(temporary))
            data = trip_dir / "data"
            original_verify = tripctl_module.verify_legacy_evidence_source

            def verify_then_create(preview: object) -> None:
                original_verify(preview)
                (data / "plan.json").write_text(PRIVATE, encoding="utf-8")

            with patch(
                "trip_planner.tripctl.verify_legacy_evidence_source",
                side_effect=verify_then_create,
            ):
                with self.assertRaises(TripctlError) as raised:
                    inspect_trip(trip_dir)

            self.assertEqual("CANONICAL_INSPECT_UNAVAILABLE", raised.exception.code)

    def test_legacy_timeline_validation_is_deterministic_redacted_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _timeline_legacy_trip(Path(temporary))
            before = _tree_bytes(trip_dir)

            first = validate_trip(trip_dir)
            second = validate_trip(trip_dir / "data")

            self.assertEqual(first, second)
            self.assertTrue(first["ok"])
            self.assertEqual("review_required", first["status"])
            self.assertEqual("legacy", first["storage_mode"])
            self.assertEqual("needs_verification", first["result"]["timeline_status"])
            self.assertEqual(1, first["result"]["day_count"])
            self.assertEqual(1, first["result"]["activity_count"])
            self.assertEqual("review_timeline", first["next_action"])
            self.assertTrue(first["requires_user_review"])
            rendered = json.dumps(first, ensure_ascii=False, sort_keys=True)
            self.assertNotIn(PRIVATE, rendered)
            self.assertNotIn(str(trip_dir), rendered)
            self.assertNotIn("09:15", rendered)
            self.assertNotIn("35.123456", rendered)
            self.assertNotIn("activity_id", rendered)
            self.assertEqual(before, _tree_bytes(trip_dir))

    def test_timeline_source_drift_is_retryable_without_partial_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _timeline_legacy_trip(Path(temporary))
            data = trip_dir / "data"
            original_evaluate = legacy_timeline_module.evaluate_timeline

            def evaluate_then_drift(state: object, *, now: object = None) -> object:
                (data / "itinerary.json").write_text(
                    (data / "itinerary.json").read_text(encoding="utf-8") + " ",
                    encoding="utf-8",
                )
                return original_evaluate(state, now=now)

            with patch(
                "trip_planner.legacy_timeline.evaluate_timeline",
                side_effect=evaluate_then_drift,
            ):
                with self.assertRaises(TripctlError) as raised:
                    validate_trip(trip_dir)

            self.assertEqual("STALE_LEGACY_TIMELINE_SOURCE", raised.exception.code)
            self.assertTrue(raised.exception.retryable)
            failure = validation_failure(raised.exception)
            self.assertFalse(failure["ok"])
            self.assertIsNone(failure["result"])
            self.assertTrue(failure["retryable"])
            self.assertEqual("retry_validation", failure["next_action"])
            self.assertNotIn(PRIVATE, json.dumps(failure))

    def test_timeline_source_repair_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _timeline_legacy_trip(Path(temporary))
            data = trip_dir / "data"
            (data / "itinerary.json").write_text(
                '{"days": ["' + PRIVATE,
                encoding="utf-8",
            )

            payload = validate_trip(trip_dir)

            self.assertTrue(payload["ok"])
            self.assertEqual("repair_required", payload["status"])
            self.assertIsNone(payload["result"])
            self.assertEqual("repair_source", payload["next_action"])
            self.assertEqual(
                "LEGACY_TIMELINE_SOURCE_MALFORMED",
                payload["problems"][0]["code"],
            )
            self.assertNotIn(PRIVATE, json.dumps(payload))

    def test_canonical_marker_created_after_timeline_snapshot_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _timeline_legacy_trip(Path(temporary))
            data = trip_dir / "data"
            original_evaluate = legacy_timeline_module.evaluate_timeline

            def evaluate_then_mark_canonical(
                state: object,
                *,
                now: object = None,
            ) -> object:
                (data / "plan.json").write_text(PRIVATE, encoding="utf-8")
                return original_evaluate(state, now=now)

            with patch(
                "trip_planner.legacy_timeline.evaluate_timeline",
                side_effect=evaluate_then_mark_canonical,
            ):
                with self.assertRaises(TripctlError) as raised:
                    validate_trip(trip_dir)

            self.assertEqual("CANONICAL_VALIDATE_UNAVAILABLE", raised.exception.code)
            failure = validation_failure(raised.exception)
            self.assertEqual("canonical", failure["storage_mode"])
            self.assertEqual("use_developer_runtime", failure["next_action"])
            self.assertNotIn(PRIVATE, json.dumps(failure))

    def test_timeline_canonical_marker_is_refused_without_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _timeline_legacy_trip(Path(temporary))
            data = trip_dir / "data"
            (data / "plan.json").symlink_to(data / "missing-plan.json")

            with self.assertRaises(TripctlError) as raised:
                validate_trip(trip_dir)

            self.assertEqual("CANONICAL_VALIDATE_UNAVAILABLE", raised.exception.code)
            failure = validation_failure(raised.exception)
            self.assertEqual("canonical", failure["storage_mode"])
            self.assertNotIn(PRIVATE, json.dumps(failure))

    def test_timeline_unsafe_source_is_a_redacted_repair_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trip_dir = _timeline_legacy_trip(root)
            data = trip_dir / "data"
            (data / "trip.json").unlink()
            (data / "trip.json").symlink_to(root / PRIVATE)

            payload = validate_trip(trip_dir)

            self.assertTrue(payload["ok"])
            self.assertEqual("repair_required", payload["status"])
            self.assertEqual(
                "LEGACY_TIMELINE_SOURCE_UNAVAILABLE",
                payload["problems"][0]["code"],
            )
            self.assertNotIn(PRIVATE, json.dumps(payload))

    def test_timeline_loader_failure_is_a_redacted_repair_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _timeline_legacy_trip(Path(temporary))
            data = trip_dir / "data"
            _write_json(data / "itinerary.json", {"days": PRIVATE})

            payload = validate_trip(trip_dir)

            self.assertTrue(payload["ok"])
            self.assertEqual("repair_required", payload["status"])
            self.assertEqual(
                "LEGACY_TIMELINE_LOAD_FAILED",
                payload["problems"][0]["code"],
            )
            self.assertNotIn(PRIVATE, json.dumps(payload))

    def test_cli_emits_only_json_and_redacts_input_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trip_dir = _legacy_trip(root)
            success = subprocess.run(
                [sys.executable, str(SCRIPT), "inspect", str(trip_dir)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, success.returncode, success.stderr)
            self.assertEqual("", success.stderr)
            payload = json.loads(success.stdout)
            self.assertTrue(payload["ok"])
            self.assertNotIn(PRIVATE, success.stdout)

            canonical = trip_dir / "data" / "plan.json"
            canonical.symlink_to(root / "missing-plan.json")
            rejected = subprocess.run(
                [sys.executable, str(SCRIPT), "inspect", str(trip_dir)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(2, rejected.returncode)
            self.assertEqual("", rejected.stdout)
            rejection = json.loads(rejected.stderr)
            self.assertFalse(rejection["ok"])
            self.assertEqual(
                "CANONICAL_INSPECT_UNAVAILABLE",
                rejection["problems"][0]["code"],
            )
            self.assertNotIn(str(trip_dir), rejected.stderr)
            self.assertNotIn(PRIVATE, rejected.stderr)

            invalid_argument = subprocess.run(
                [sys.executable, str(SCRIPT), "inspect", str(trip_dir), "--live"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(2, invalid_argument.returncode)
            self.assertEqual("", invalid_argument.stdout)
            self.assertEqual(
                "INVALID_ARGUMENT",
                json.loads(invalid_argument.stderr)["problems"][0]["code"],
            )

            missing = subprocess.run(
                [sys.executable, str(SCRIPT), "inspect", str(root / PRIVATE)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(2, missing.returncode)
            self.assertEqual("", missing.stdout)
            self.assertEqual(
                "DATA_DIRECTORY_MISSING",
                json.loads(missing.stderr)["problems"][0]["code"],
            )
            self.assertNotIn(PRIVATE, missing.stderr)

            timeline_trip = _timeline_legacy_trip(root)
            timeline = subprocess.run(
                [sys.executable, str(SCRIPT), "validate", str(timeline_trip)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, timeline.returncode, timeline.stderr)
            self.assertEqual("", timeline.stderr)
            self.assertTrue(json.loads(timeline.stdout)["ok"])
            self.assertNotIn(PRIVATE, timeline.stdout)

            invalid_timeline_argument = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "validate",
                    str(timeline_trip),
                    "--live",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(2, invalid_timeline_argument.returncode)
            self.assertEqual("", invalid_timeline_argument.stdout)
            self.assertEqual(
                "INVALID_ARGUMENT",
                json.loads(invalid_timeline_argument.stderr)["problems"][0]["code"],
            )
            self.assertEqual(
                "validate",
                json.loads(invalid_timeline_argument.stderr)["command"],
            )
            self.assertNotIn(PRIVATE, invalid_timeline_argument.stderr)

    def test_help_and_version_also_use_json_envelopes(self) -> None:
        for arguments, command in (
            (["--help"], "help"),
            (["--version"], "version"),
            (["inspect", "--help"], "inspect"),
            (["validate", "--help"], "validate"),
        ):
            with self.subTest(arguments=arguments):
                completed = subprocess.run(
                    [sys.executable, str(SCRIPT), *arguments],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertEqual("", completed.stderr)
                payload = json.loads(completed.stdout)
                self.assertTrue(payload["ok"])
                self.assertEqual("information", payload["status"])
                self.assertEqual(command, payload["command"])

    def test_real_legacy_trips_are_inspectable_without_changes(self) -> None:
        before = _tree_bytes(TRIPS_ROOT)
        trip_dirs = sorted(
            path for path in TRIPS_ROOT.iterdir() if (path / "data").is_dir()
        )

        payloads = [inspect_trip(path) for path in trip_dirs]

        self.assertEqual(3, len(payloads))
        self.assertTrue(all(item["ok"] for item in payloads))
        self.assertTrue(
            all(item["result"]["storage_mode"] == "legacy" for item in payloads)
        )
        self.assertTrue(
            all(item["status"] == "review_required" for item in payloads)
        )
        self.assertEqual(before, _tree_bytes(TRIPS_ROOT))

    def test_real_legacy_trips_have_safe_timeline_reviews_without_changes(self) -> None:
        before = _tree_bytes(TRIPS_ROOT)
        trip_dirs = sorted(
            path for path in TRIPS_ROOT.iterdir() if (path / "data").is_dir()
        )

        payloads = [validate_trip(path) for path in trip_dirs]

        self.assertEqual(3, len(payloads))
        self.assertTrue(all(item["ok"] for item in payloads))
        self.assertTrue(all(item["storage_mode"] == "legacy" for item in payloads))
        self.assertTrue(all(item["status"] == "review_required" for item in payloads))
        self.assertTrue(
            all(item["result"]["timeline_status"] == "needs_verification" for item in payloads)
        )
        tainan = next(
            item
            for path, item in zip(trip_dirs, payloads)
            if path.name == "tainan-2026-04"
        )
        self.assertIn(
            "INVALID_TRAVEL_REFERENCE",
            {problem["code"] for problem in tainan["problems"]},
        )
        self.assertIn(
            "POSSIBLE_SCHEDULED_START_CONFLICT",
            {problem["code"] for problem in tainan["problems"]},
        )
        self.assertEqual(before, _tree_bytes(TRIPS_ROOT))


if __name__ == "__main__":
    unittest.main()
