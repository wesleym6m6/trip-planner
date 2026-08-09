"""Offline adversarial coverage for the Phase 4.6B legacy evidence preview."""

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

import trip_planner.legacy_evidence as legacy_evidence_module
from trip_planner.legacy_evidence import (
    MAX_LEGACY_EVIDENCE_FILE_BYTES,
    LegacyEvidenceArtifactState,
    LegacyEvidencePreviewError,
    preview_legacy_evidence,
    verify_legacy_evidence_source,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
TRIPS_ROOT = REPO_ROOT / "trips"
SCRIPT = REPO_ROOT / "scripts" / "preview_legacy_evidence.py"
PRIVATE = "private-address-place-token-price-37.5000-127.0000"


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


def _make_trip(root: Path, *, include_optional: bool = True) -> Path:
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
                        },
                        {"title": "No provider identity"},
                    ],
                    "travel": [
                        {"source": "api", "token": PRIVATE},
                        {"source": "manual", "duration_min": 40},
                        {"source": "unknown-provider"},
                    ],
                }
            ]
        },
    )
    _write_json(
        data / "places_cache.json",
        {
            PRIVATE: {
                "address": PRIVATE,
                "reviews": [PRIVATE],
                "photos": [PRIVATE],
                "opening_hours": {"periods": []},
            }
        },
    )
    if include_optional:
        _write_json(
            data / "flights_cache.json",
            {"flights": [{"token": PRIVATE, "price": PRIVATE}]},
        )
        _write_json(
            data / "hotels_cache.json",
            {"hotels": [{"address": PRIVATE, "price": PRIVATE}]},
        )
    return trip_dir


class Phase46LegacyEvidenceTests(unittest.TestCase):
    def test_classifies_known_legacy_evidence_without_writes_or_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _make_trip(Path(temporary))
            before = _tree_bytes(trip_dir)

            preview = preview_legacy_evidence(trip_dir)
            payload = preview.to_dict()
            repeated = preview_legacy_evidence(trip_dir)

            self.assertEqual(
                {
                    "api_edge_count": 1,
                    "manual_edge_count": 1,
                    "unclassified_edge_count": 1,
                    "place_id_count": 1,
                    "coordinate_pair_count": 1,
                },
                preview.route_summary.to_dict(),
            )
            self.assertEqual(0, preview.import_count)
            self.assertEqual((), preview.cleanup_targets)
            self.assertTrue(preview.source_is_current())
            self.assertFalse(hasattr(preview, "data_dir"))
            self.assertFalse(hasattr(preview, "source_revision"))
            self.assertEqual(payload, repeated.to_dict())
            self.assertEqual(preview.preview_digest, repeated.preview_digest)
            self.assertTrue(payload["source_verifiable"])
            self.assertTrue(payload["requires_user_review"])
            self.assertEqual([], payload["cleanup_targets"])
            states = {item["kind"]: item["state"] for item in payload["artifacts"]}
            self.assertEqual("compatibility_blocked", states["places_cache"])
            self.assertEqual("quarantined", states["flights_cache"])
            self.assertEqual("quarantined", states["hotels_cache"])
            codes = {item["code"] for item in payload["problems"]}
            self.assertTrue(
                {
                    "LEGACY_PROVIDER_EVIDENCE_REFRESH_REQUIRED",
                    "LEGACY_MANUAL_TRAVEL_CLASSIFICATION_REQUIRED",
                    "LEGACY_TRAVEL_PROVENANCE_UNKNOWN",
                    "LEGACY_PLACE_IDENTITY_REFRESH_REQUIRED",
                    "LEGACY_COORDINATE_PROVENANCE_REQUIRED",
                    "LEGACY_PLACE_CACHE_QUARANTINED",
                    "LEGACY_FLIGHT_CACHE_QUARANTINED",
                    "LEGACY_HOTEL_CACHE_QUARANTINED",
                }.issubset(codes)
            )
            rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True) + repr(preview)
            self.assertNotIn(PRIVATE, rendered)
            self.assertNotIn(str(trip_dir), rendered)
            self.assertEqual(before, _tree_bytes(trip_dir))

    def test_manifest_drift_is_rejected_for_changed_missing_and_new_optional_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _make_trip(Path(temporary))
            data = trip_dir / "data"
            mutations = (
                lambda: (data / "flights_cache.json").write_text("{}\n", encoding="utf-8"),
                lambda: (data / "flights_cache.json").unlink(),
                lambda: (data / "flights_cache.json").write_text("{}\n", encoding="utf-8"),
            )
            for mutation in mutations:
                with self.subTest(mutation=mutations.index(mutation)):
                    preview = preview_legacy_evidence(trip_dir)
                    mutation()
                    self.assertFalse(preview.source_is_current())
                    with self.assertRaises(LegacyEvidencePreviewError) as raised:
                        verify_legacy_evidence_source(preview)
                    self.assertEqual("STALE_LEGACY_EVIDENCE_PREVIEW", raised.exception.code)

    def test_malformed_symlink_and_oversized_sources_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trip_dir = _make_trip(root, include_optional=False)
            data = trip_dir / "data"

            (data / "itinerary.json").write_text("{bad", encoding="utf-8")
            malformed = preview_legacy_evidence(trip_dir)
            self.assertIn(
                "LEGACY_EVIDENCE_JSON_MALFORMED",
                {item.code for item in malformed.problems},
            )

            cache_target = root / "private-cache.json"
            cache_target.write_text(json.dumps({PRIVATE: PRIVATE}), encoding="utf-8")
            (data / "places_cache.json").unlink()
            (data / "places_cache.json").symlink_to(cache_target)
            unsafe = preview_legacy_evidence(trip_dir)
            self.assertFalse(unsafe.source_verifiable)
            places = next(
                artifact
                for artifact in unsafe.artifacts
                if artifact.relative_path == "places_cache.json"
            )
            self.assertIs(places.state, LegacyEvidenceArtifactState.UNSAFE_SOURCE)
            self.assertNotIn(PRIVATE, json.dumps(unsafe.to_dict(), ensure_ascii=False))

        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _make_trip(Path(temporary), include_optional=False)
            cache = trip_dir / "data" / "places_cache.json"
            with cache.open("wb") as handle:
                handle.truncate(MAX_LEGACY_EVIDENCE_FILE_BYTES + 1)
            oversized = preview_legacy_evidence(trip_dir)
            places = next(
                artifact
                for artifact in oversized.artifacts
                if artifact.relative_path == "places_cache.json"
            )
            self.assertIs(places.state, LegacyEvidenceArtifactState.OVERSIZED)
            self.assertFalse(oversized.source_verifiable)
            self.assertFalse(oversized.source_is_current())

    def test_missing_required_places_cache_is_not_a_reusable_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _make_trip(Path(temporary), include_optional=False)
            (trip_dir / "data" / "places_cache.json").unlink()

            preview = preview_legacy_evidence(trip_dir)

            self.assertFalse(preview.source_verifiable)
            self.assertFalse(preview.source_is_current())
            self.assertIn(
                "LEGACY_PLACE_CACHE_MISSING",
                {item.code for item in preview.problems},
            )

    def test_hostile_json_is_bounded_and_cli_never_traces_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _make_trip(Path(temporary), include_optional=False)
            data = trip_dir / "data"
            (data / "places_cache.json").write_bytes(
                b'{"nested":' * 1_200 + b"0" + b"}" * 1_200
            )
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(trip_dir)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertNotIn("Traceback", completed.stderr + completed.stdout)
            payload = json.loads(completed.stdout)
            places = next(
                item
                for item in payload["artifacts"]
                if item["relative_path"] == "places_cache.json"
            )
            self.assertEqual("malformed", places["state"])

        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _make_trip(Path(temporary), include_optional=False)
            itinerary_path = trip_dir / "data" / "itinerary.json"
            itinerary = json.loads(itinerary_path.read_text(encoding="utf-8"))
            itinerary["days"][0]["places"][0]["lat"] = int("9" * 401)
            _write_json(itinerary_path, itinerary)
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(trip_dir)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertNotIn("Traceback", completed.stderr + completed.stdout)
            self.assertEqual(
                0,
                json.loads(completed.stdout)["route_summary"]["coordinate_pair_count"],
            )

    def test_out_of_scope_paths_are_presence_only_and_do_not_open_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _make_trip(Path(temporary), include_optional=False)
            data = trip_dir / "data"
            for name in ("plan.json", ".trip-planner-evidence.json"):
                with (data / name).open("wb") as handle:
                    handle.truncate(MAX_LEGACY_EVIDENCE_FILE_BYTES + 1)

            opened: list[str] = []
            real_open = legacy_evidence_module.os.open

            def recording_open(path: object, flags: int, *args: object) -> int:
                opened.append(Path(path).name)
                return real_open(path, flags, *args)

            with patch.object(
                legacy_evidence_module.os,
                "open",
                side_effect=recording_open,
            ):
                preview = preview_legacy_evidence(trip_dir)

            self.assertTrue(preview.source_verifiable)
            self.assertTrue(preview.source_is_current())
            self.assertNotIn("plan.json", opened)
            self.assertNotIn(".trip-planner-evidence.json", opened)
            codes = {item.code for item in preview.problems}
            self.assertIn(
                "LEGACY_EVIDENCE_CLEANUP_BLOCKED_BY_CANONICAL_COMPATIBILITY",
                codes,
            )
            self.assertIn("CURRENT_EVIDENCE_STORE_OUT_OF_SCOPE", codes)

    def test_non_utf8_unknown_cache_name_is_redacted_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _make_trip(Path(temporary), include_optional=False)
            data = trip_dir / "data"
            raw_path = os.fsencode(data) + b"/\xff_cache.json"
            descriptor = os.open(
                raw_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                os.write(descriptor, b"{}\n")
            finally:
                os.close(descriptor)

            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(trip_dir)],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertNotIn("Traceback", completed.stderr + completed.stdout)
            self.assertNotIn("\\udcff", completed.stdout)
            self.assertIn(
                "UNKNOWN_LEGACY_CACHE_REVIEW_REQUIRED",
                {item["code"] for item in json.loads(completed.stdout)["problems"]},
            )

    def test_malformed_trip_json_is_a_typed_repair_problem(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _make_trip(Path(temporary), include_optional=False)
            (trip_dir / "data" / "trip.json").write_text("{bad", encoding="utf-8")

            preview = preview_legacy_evidence(trip_dir)

            self.assertIn(
                "LEGACY_EVIDENCE_JSON_MALFORMED",
                {item.code for item in preview.problems},
            )

    def test_canonical_presence_and_unknown_cache_remain_cleanup_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _make_trip(Path(temporary), include_optional=False)
            data = trip_dir / "data"
            (data / "plan.json").write_text("{}\n", encoding="utf-8")
            (data / "other_cache.json").write_text("{}\n", encoding="utf-8")

            preview = preview_legacy_evidence(trip_dir)

            self.assertEqual((), preview.cleanup_targets)
            codes = {item.code for item in preview.problems}
            self.assertIn(
                "LEGACY_EVIDENCE_CLEANUP_BLOCKED_BY_CANONICAL_COMPATIBILITY",
                codes,
            )
            self.assertIn("UNKNOWN_LEGACY_CACHE_REVIEW_REQUIRED", codes)
            self.assertTrue(preview.source_is_current())

    def test_cli_is_redacted_and_input_failure_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir = _make_trip(Path(temporary))
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(trip_dir)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("", completed.stderr)
            self.assertNotIn(PRIVATE, completed.stdout)
            self.assertEqual(
                "legacy-evidence-preview/v1",
                json.loads(completed.stdout)["contract_version"],
            )
            missing = subprocess.run(
                [sys.executable, str(SCRIPT), str(Path(temporary) / "missing")],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(2, missing.returncode)
            self.assertEqual("DATA_DIRECTORY_MISSING", json.loads(missing.stderr)["error"]["code"])
            self.assertNotIn("missing", missing.stderr)

    def test_local_private_trip_previews_are_read_only_without_changes(self) -> None:
        before = _tree_bytes(TRIPS_ROOT)
        previews = [
            preview_legacy_evidence(path)
            for path in sorted(TRIPS_ROOT.iterdir())
            if (path / "data").is_dir()
        ]
        self.assertTrue(previews)
        self.assertTrue(all(item.source_is_current() for item in previews))
        self.assertTrue(all(item.import_count == 0 for item in previews))
        self.assertTrue(all(item.cleanup_targets == () for item in previews))
        self.assertEqual(before, _tree_bytes(TRIPS_ROOT))


if __name__ == "__main__":
    unittest.main()
