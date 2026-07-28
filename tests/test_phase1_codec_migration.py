"""Adversarial offline tests for the Phase 1 codec and migration boundary.

Every migration preview operates on a temporary copy.  The local ``trips/``
tree is read-only test input and is hash-checked again after this suite.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from scripts.plan_compat import (
    CanonicalWriteRefused,
    load_trip_views,
    refuse_canonical_write,
)
from scripts.validate_trip import validate
from trip_planner.codec import (
    MIGRATION_META_KEY,
    PlanCodecError,
    build_plan,
    compute_revision,
    decode_plan,
    encode_plan,
    legacy_compatibility_views,
    load_plan,
    plan_to_trip_state,
)
from trip_planner.loaders import load_legacy_trip
from trip_planner.migrations import MigrationError, preview_legacy_migration
from trip_planner.models import (
    DecisionState,
    EvidenceState,
    Flexibility,
    TripState,
)
from trip_planner.mutations import PlanPatch, UpdateActivity
from trip_planner.store import TripStore


REPO_ROOT = Path(__file__).resolve().parents[1]
TRIPS_ROOT = REPO_ROOT / "trips"


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=True) + "\n"
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    """Capture file set, exact bytes, and mtimes without following directories."""

    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _legacy_documents(
    *,
    duplicate_activities: bool = False,
    include_unknowns: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    trip: dict[str, Any] = {
        "title": "Migration fixture",
        "subtitle": "Fixture subtitle",
        "date_range": "2026-10-01 ~ 2026-10-01",
        "cities": ["Busan"],
        "slug": "migration-fixture",
        "icon": "🧭",
    }
    if include_unknowns:
        trip["unknown_root"] = {
            "null_value": None,
            "zero_value": 0,
            "false_value": False,
            "nested": ["保留", {"depth": 2}],
        }

    first: dict[str, Any] = {
        "type": "spot",
        "title": "Mirror" if duplicate_activities else "Alpha",
        "note": None,
        "maps_query": "Mirror, Busan" if duplicate_activities else "Alpha, Busan",
        "place_id": None,
        "lat": 35.100001,
        "lng": 129.100001,
        "display_name": "Mirror" if duplicate_activities else "Alpha",
        "time": "10:00",
    }
    second = (
        dict(first)
        if duplicate_activities
        else {
            "type": "food",
            "title": "Beta",
            "note": "Lunch",
            "maps_query": "Beta, Busan",
            "place_id": "google-beta",
            "lat": 35.200002,
            "lng": 129.200002,
            "display_name": "Beta",
            "time": "12:00",
        }
    )
    if include_unknowns:
        first["unknown_place"] = {"null_value": None, "zero_value": 0}

    edge: dict[str, Any] = {
        "from": 0,
        "to": 1,
        "recommended_mode": "transit",
        "source": "fixture",
        "modes": {
            "transit": {
                "duration_min": 12,
                "distance_km": 0,
                "transit_steps": [
                    {
                        "stopDetails": {
                            "departureTime": None,
                            "arrivalTime": "2026-10-01T02:12:00Z",
                        },
                        "zero_value": 0,
                    }
                ],
            }
        },
    }
    if include_unknowns:
        edge["unknown_edge"] = {"null_value": None, "zero_value": 0}

    itinerary: dict[str, Any] = {
        "available_modes": ["walking", "transit"],
        "days": [
            {
                "day": 1,
                "date": "2026-10-01",
                "title": "One day",
                "subtitle": "Fixture day",
                "places": [first, second],
                "travel": [edge],
            }
        ],
    }
    if include_unknowns:
        itinerary["unknown_itinerary"] = {"null_value": None, "zero_value": 0}
        itinerary["days"][0]["unknown_day"] = {
            "null_value": None,
            "zero_value": 0,
        }
    return trip, itinerary


def _write_legacy_trip(
    root: Path,
    *,
    duplicate_activities: bool = False,
    include_unknowns: bool = True,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    trip, itinerary = _legacy_documents(
        duplicate_activities=duplicate_activities,
        include_unknowns=include_unknowns,
    )
    trip_dir = root / "migration-fixture"
    data_dir = trip_dir / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "trip.json").write_bytes(_json_bytes(trip))
    (data_dir / "itinerary.json").write_bytes(_json_bytes(itinerary))
    (data_dir / "info.json").write_text(
        json.dumps({"sections": []}), encoding="utf-8"
    )
    (data_dir / "reservations.json").write_text("[]\n", encoding="utf-8")
    (data_dir / "todo.json").write_text("[]\n", encoding="utf-8")
    (data_dir / "packing.json").write_text("[]\n", encoding="utf-8")
    (data_dir / "places_cache.json").write_text(
        '{"sentinel":{"zero":0,"null":null}}\n', encoding="utf-8"
    )
    return trip_dir, trip, itinerary


def _semantic_signature(state: TripState) -> tuple[Any, ...]:
    day_position = {day.day_id: index for index, day in enumerate(state.days)}
    activity_position = {
        activity.activity_id: (day_position[activity.day_id], activity.order)
        for activity in state.activities
    }
    return (
        (
            state.slug,
            state.title,
            state.subtitle,
            state.timezone,
            state.start_date,
            state.end_date,
            state.cities,
        ),
        tuple(
            (
                day.date,
                day.timezone,
                day.available_start,
                day.available_end,
                day.allowed_modes,
                day.title,
                day.subtitle,
                len(day.activity_ids),
            )
            for day in state.days
        ),
        tuple(
            (
                activity.order,
                activity.title,
                activity.scheduled_start,
                activity.duration_min,
                activity.priority,
                activity.decision_state,
                activity.flexibility,
                activity.evidence_state,
                activity.kind,
                activity.note,
                activity.lat,
                activity.lng,
                activity.maps_query,
            )
            for activity in state.activities
        ),
        tuple(
            (
                activity_position[edge.from_activity_id],
                activity_position[edge.to_activity_id],
                edge.mode,
                edge.duration_min,
                edge.buffer_min,
                edge.distance_km,
                edge.evidence_state,
                edge.recommended,
            )
            for edge in state.travel_estimates
        ),
    )


class Phase1CodecMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._real_trip_hashes = _tree_hashes(TRIPS_ROOT)

    @classmethod
    def tearDownClass(cls) -> None:
        if TRIPS_ROOT.exists():
            current = _tree_hashes(TRIPS_ROOT)
            if current != cls._real_trip_hashes:
                raise AssertionError("Phase 1 migration tests modified local trips/")

    def test_preview_is_deterministic_and_zero_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir, _trip, _itinerary = _write_legacy_trip(Path(temporary))
            before = _tree_snapshot(trip_dir)

            first = preview_legacy_migration(trip_dir)
            second = preview_legacy_migration(trip_dir)

            self.assertEqual(first.candidate_bytes, second.candidate_bytes)
            self.assertEqual(first.candidate_revision, second.candidate_revision)
            self.assertEqual(first.preview_digest, second.preview_digest)
            self.assertEqual(first.id_assignments, second.id_assignments)
            self.assertEqual(first.issues, second.issues)
            self.assertEqual(before, _tree_snapshot(trip_dir))
            self.assertFalse((trip_dir / "data" / "plan.json").exists())

    def test_preview_identity_and_digest_ignore_symlink_alias_spelling(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trip_dir, trip, _itinerary = _write_legacy_trip(root / "real")
            alias = root / "alias-name"
            alias.symlink_to(trip_dir, target_is_directory=True)

            direct = preview_legacy_migration(trip_dir)
            through_alias = preview_legacy_migration(alias)

            self.assertEqual(direct.candidate_bytes, through_alias.candidate_bytes)
            self.assertEqual(
                direct.candidate_revision,
                through_alias.candidate_revision,
            )
            self.assertEqual(direct.preview_digest, through_alias.preview_digest)
            self.assertNotEqual(
                direct.id_assignments[0].source_path,
                through_alias.id_assignments[0].source_path,
            )

            # A missing legacy slug uses physical directory identity, not the
            # spelling of the symlink through which the source was reached.
            trip.pop("slug")
            (trip_dir / "data" / "trip.json").write_bytes(_json_bytes(trip))
            direct_without_slug = preview_legacy_migration(trip_dir)
            alias_without_slug = preview_legacy_migration(alias)
            self.assertEqual(
                direct_without_slug.candidate_plan["trip_id"],
                alias_without_slug.candidate_plan["trip_id"],
            )
            self.assertEqual(
                direct_without_slug.candidate_bytes,
                alias_without_slug.candidate_bytes,
            )
            self.assertEqual(
                direct_without_slug.preview_digest,
                alias_without_slug.preview_digest,
            )
            store = TripStore(root / "real", "migration-fixture")
            committed = store.commit_migration(alias_without_slug)
            self.assertTrue(committed.success)
            self.assertEqual("migrated", committed.status)

    def test_preview_refuses_broken_canonical_plan_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir, _trip, _itinerary = _write_legacy_trip(Path(temporary))
            data_dir = trip_dir / "data"
            plan_path = data_dir / "plan.json"
            plan_path.symlink_to(data_dir / "missing-plan-target.json")

            with self.assertRaises(MigrationError) as raised:
                preview_legacy_migration(trip_dir)

            self.assertEqual("PLAN_ALREADY_EXISTS", raised.exception.code)
            self.assertTrue(plan_path.is_symlink())

    def test_duplicate_looking_activities_receive_unique_persisted_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir, _trip, _itinerary = _write_legacy_trip(
                Path(temporary), duplicate_activities=True
            )
            preview = preview_legacy_migration(trip_dir)
            plan_path = trip_dir / "data" / "plan.json"
            plan_path.write_bytes(preview.candidate_bytes)

            persisted = load_plan(plan_path)
            places = persisted["state"]["itinerary"]["days"][0]["places"]
            activity_ids = [place["activity_id"] for place in places]
            self.assertEqual(2, len(set(activity_ids)))
            self.assertEqual(
                set(activity_ids), set(preview.protected_activity_ids)
            )
            self.assertEqual(
                activity_ids,
                [
                    assignment.stable_id
                    for assignment in preview.id_assignments
                    if assignment.entity_kind == "activity"
                ],
            )

    def test_rename_and_reorder_preserve_identity_and_derive_travel_indices(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir, _trip, _itinerary = _write_legacy_trip(Path(temporary))
            original = preview_legacy_migration(trip_dir).mutable_candidate_plan()
            original_places = original["state"]["itinerary"]["days"][0]["places"]
            alpha_id = original_places[0]["activity_id"]
            beta_id = original_places[1]["activity_id"]
            edge = original["state"]["itinerary"]["days"][0]["travel"][0]
            self.assertEqual(alpha_id, edge["from_activity_id"])
            self.assertEqual(beta_id, edge["to_activity_id"])

            state = original["state"]
            places = state["itinerary"]["days"][0]["places"]
            places[0]["title"] = "Alpha renamed"
            places[:] = [places[1], places[0]]
            migrated_edge = state["itinerary"]["days"][0]["travel"][0]
            migrated_edge.pop("from", None)
            migrated_edge.pop("to", None)
            reordered = build_plan(
                trip_id=original["trip_id"],
                generation=original["generation"] + 1,
                state=state,
                receipts=original["receipts"],
            )

            _trip_view, itinerary_view = legacy_compatibility_views(reordered)
            reordered_places = itinerary_view["days"][0]["places"]
            self.assertEqual(beta_id, reordered_places[0]["activity_id"])
            self.assertEqual(alpha_id, reordered_places[1]["activity_id"])
            self.assertEqual("Alpha renamed", reordered_places[1]["title"])
            reordered_edge = itinerary_view["days"][0]["travel"][0]
            self.assertEqual(alpha_id, reordered_edge["from_activity_id"])
            self.assertEqual(beta_id, reordered_edge["to_activity_id"])
            self.assertEqual((1, 0), (reordered_edge["from"], reordered_edge["to"]))

    def test_positional_index_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir, _trip, _itinerary = _write_legacy_trip(Path(temporary))
            plan = preview_legacy_migration(trip_dir).mutable_candidate_plan()
            state = plan["state"]
            places = state["itinerary"]["days"][0]["places"]
            places[:] = [places[1], places[0]]

            with self.assertRaises(PlanCodecError) as raised:
                build_plan(
                    trip_id=plan["trip_id"],
                    generation=plan["generation"] + 1,
                    state=state,
                    receipts=plan["receipts"],
                )

            self.assertEqual("TRAVEL_REFERENCE_MISMATCH", raised.exception.code)

    def test_unknown_fields_null_zero_and_nested_transit_are_lossless(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir, trip, itinerary = _write_legacy_trip(Path(temporary))
            plan = preview_legacy_migration(trip_dir).mutable_candidate_plan()
            trip_view, itinerary_view = legacy_compatibility_views(plan)

            self.assertEqual(trip["unknown_root"], trip_view["unknown_root"])
            self.assertEqual(
                itinerary["unknown_itinerary"],
                itinerary_view["unknown_itinerary"],
            )
            self.assertEqual(
                itinerary["days"][0]["unknown_day"],
                itinerary_view["days"][0]["unknown_day"],
            )
            self.assertEqual(
                itinerary["days"][0]["places"][0]["unknown_place"],
                itinerary_view["days"][0]["places"][0]["unknown_place"],
            )
            original_edge = itinerary["days"][0]["travel"][0]
            viewed_edge = itinerary_view["days"][0]["travel"][0]
            self.assertEqual(
                original_edge["unknown_edge"], viewed_edge["unknown_edge"]
            )
            self.assertEqual(
                original_edge["modes"]["transit"]["transit_steps"],
                viewed_edge["modes"]["transit"]["transit_steps"],
            )
            self.assertIsNone(
                viewed_edge["modes"]["transit"]["transit_steps"][0][
                    "stopDetails"
                ]["departureTime"]
            )
            self.assertEqual(0, viewed_edge["modes"]["transit"]["distance_km"])
            self.assertEqual(0, viewed_edge["unknown_edge"]["zero_value"])

    def test_runtime_fallbacks_and_defaults_are_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir, _trip, _itinerary = _write_legacy_trip(Path(temporary))
            plan = preview_legacy_migration(trip_dir).mutable_candidate_plan()
            before = encode_plan(plan)
            canonical_trip = plan["state"]["trip"]
            canonical_activity = plan["state"]["itinerary"]["days"][0]["places"][0]

            self.assertNotIn("timezone", canonical_trip)
            for field in (
                "decision_state",
                "flexibility",
                "evidence_state",
                "duration_min",
            ):
                self.assertNotIn(field, canonical_activity)

            state = plan_to_trip_state(plan)

            self.assertEqual("UTC", state.timezone)
            self.assertEqual(DecisionState.SELECTED, state.activities[0].decision_state)
            self.assertEqual(Flexibility.MOVABLE, state.activities[0].flexibility)
            self.assertEqual(
                EvidenceState.UNVERIFIED, state.activities[0].evidence_state
            )
            self.assertIsNone(state.activities[0].duration_min)
            self.assertEqual(before, encode_plan(plan))

    def test_every_migrated_activity_is_protected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir, _trip, _itinerary = _write_legacy_trip(
                Path(temporary), duplicate_activities=True
            )
            preview = preview_legacy_migration(trip_dir)
            plan = preview.mutable_candidate_plan()
            activity_ids = [
                place["activity_id"]
                for day in plan["state"]["itinerary"]["days"]
                for place in day["places"]
            ]
            protected = plan["state"]["trip"][MIGRATION_META_KEY]["migration"][
                "protected_activity_ids"
            ]
            self.assertEqual(activity_ids, protected)
            self.assertEqual(activity_ids, list(preview.protected_activity_ids))
            self.assertEqual(len(protected), len(set(protected)))

    def test_strict_json_and_plan_failures_are_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trip_dir, trip, itinerary = _write_legacy_trip(root)
            data_dir = trip_dir / "data"

            cases = {
                "duplicate-key": (
                    b'{"title":"one","title":"two"}',
                    _json_bytes(itinerary),
                    "DUPLICATE_JSON_KEY",
                ),
                "nan": (
                    _json_bytes({**trip, "bad_number": float("nan")}),
                    _json_bytes(itinerary),
                    "NON_FINITE_NUMBER",
                ),
            }
            for name, (trip_bytes, itinerary_bytes, expected_code) in cases.items():
                with self.subTest(name=name):
                    (data_dir / "trip.json").write_bytes(trip_bytes)
                    (data_dir / "itinerary.json").write_bytes(itinerary_bytes)
                    with self.assertRaises(PlanCodecError) as raised:
                        preview_legacy_migration(trip_dir)
                    self.assertEqual(expected_code, raised.exception.code)

            (data_dir / "trip.json").write_bytes(_json_bytes(trip))
            (data_dir / "itinerary.json").write_bytes(_json_bytes(itinerary))
            valid = preview_legacy_migration(trip_dir).mutable_candidate_plan()

            future = json.loads(encode_plan(valid))
            future["schema_version"] = "trip-planner.plan/v999"
            future["revision"] = compute_revision(future)
            with self.assertRaises(PlanCodecError) as raised:
                decode_plan(json.dumps(future))
            self.assertEqual("UNSUPPORTED_SCHEMA", raised.exception.code)

            corrupt = json.loads(encode_plan(valid))
            corrupt["revision"] = "0" * 64
            with self.assertRaises(PlanCodecError) as raised:
                decode_plan(json.dumps(corrupt))
            self.assertEqual("REVISION_MISMATCH", raised.exception.code)

    def test_stable_ids_and_references_require_visible_bounded_text(
        self,
    ) -> None:
        base_state: dict[str, Any] = {
            "trip": {},
            "itinerary": {
                "days": [
                    {
                        "day_id": "day-1",
                        "places": [
                            {
                                "activity_id": "activity-a",
                                "location_id": "location-a",
                            },
                            {
                                "activity_id": "activity-b",
                                "location_id": "location-b",
                            },
                        ],
                        "travel": [
                            {
                                "from_activity_id": "activity-a",
                                "to_activity_id": "activity-b",
                            }
                        ],
                    }
                ]
            },
        }

        def detached_state() -> dict[str, Any]:
            return json.loads(json.dumps(base_state))

        cases: dict[str, tuple[str, dict[str, Any]]] = {}

        activity_state = detached_state()
        activity_state["itinerary"]["days"][0]["places"][0][
            "activity_id"
        ] = " activity-a "
        cases["activity identity"] = ("trip-1", activity_state)

        location_state = detached_state()
        location_state["itinerary"]["days"][0]["places"][0][
            "location_id"
        ] = " location-a "
        cases["location identity"] = ("trip-1", location_state)

        edge_state = detached_state()
        edge_state["itinerary"]["days"][0]["travel"][0][
            "from_activity_id"
        ] = " activity-a "
        cases["travel reference"] = ("trip-1", edge_state)

        control_state = detached_state()
        control_state["itinerary"]["days"][0]["places"][0][
            "activity_id"
        ] = "activity-\nA"
        control_state["itinerary"]["days"][0]["travel"][0][
            "from_activity_id"
        ] = "activity-\nA"
        cases["control character"] = ("trip-1", control_state)

        oversized_state = detached_state()
        oversized_id = "a" * 257
        oversized_state["itinerary"]["days"][0]["places"][0][
            "activity_id"
        ] = oversized_id
        oversized_state["itinerary"]["days"][0]["travel"][0][
            "from_activity_id"
        ] = oversized_id
        cases["oversized identity"] = ("trip-1", oversized_state)

        fragment_state = detached_state()
        fragment_state["trip"][MIGRATION_META_KEY] = {
            "migration": {
                "protected_activity_ids": ["activity-a", "activity-b"],
                "ignored_travel_edges": [
                    {
                        "day_id": " day-1 ",
                        "index": 0,
                        "edge": {"from": 0, "to": 1},
                    }
                ],
            }
        }
        cases["migration reference"] = ("trip-1", fragment_state)
        cases["trip identity"] = (" trip-1 ", detached_state())

        for name, (trip_id, state) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(PlanCodecError) as raised:
                    build_plan(
                        trip_id=trip_id,
                        generation=1,
                        state=state,
                    )
                self.assertEqual("MALFORMED_PLAN", raised.exception.code)

    def test_migration_rejects_unusable_preserved_stable_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir, _trip, itinerary = _write_legacy_trip(Path(temporary))
            itinerary["days"][0]["places"][0]["activity_id"] = "a" * 257
            (trip_dir / "data" / "itinerary.json").write_bytes(
                _json_bytes(itinerary)
            )

            with self.assertRaises(PlanCodecError) as raised:
                preview_legacy_migration(trip_dir)

            self.assertEqual("MALFORMED_PLAN", raised.exception.code)
            self.assertIn("activity_id", raised.exception.path)

    def test_receipts_are_strict_but_allow_extension_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir, _trip, _itinerary = _write_legacy_trip(Path(temporary))
            plan = preview_legacy_migration(trip_dir).mutable_candidate_plan()

            valid_receipt: dict[str, Any] = {
                "kind": "patch",
                "status": "applied",
                "request_digest": f"sha256:{'1' * 64}",
                "transaction_id": f"tx-{'2' * 32}",
                "base_revision": "3" * 64,
                "applied_revision": "4" * 64,
                "applied_generation": 1,
                "evaluation_at": "2026-10-01T04:30:00+00:00",
                "extension": {"future": True, "nullable": None},
            }
            plan["receipts"] = {"request-1": valid_receipt}
            decoded = decode_plan(encode_plan(plan))
            self.assertEqual(
                valid_receipt["extension"],
                decoded["receipts"]["request-1"]["extension"],
            )

            invalid_values: dict[str, tuple[str, Any]] = {
                "kind": ("kind", "future-kind"),
                "status": ("status", "corrupt"),
                "request digest": ("request_digest", "1" * 64),
                "transaction ID": ("transaction_id", "tx-short"),
                "base revision": ("base_revision", "bad"),
                "applied revision": ("applied_revision", "bad"),
                "generation bool": ("applied_generation", True),
                "generation zero": ("applied_generation", 0),
                "evaluation malformed": ("evaluation_at", "not-a-time"),
                "evaluation naive": (
                    "evaluation_at",
                    "2026-10-01T04:30:00",
                ),
                "evaluation non-UTC": (
                    "evaluation_at",
                    "2026-10-01T13:30:00+09:00",
                ),
            }
            for name, (field, bad_value) in invalid_values.items():
                with self.subTest(name=name):
                    invalid = json.loads(json.dumps(plan))
                    invalid["receipts"]["request-1"][field] = bad_value
                    with self.assertRaises(PlanCodecError) as raised:
                        encode_plan(invalid)
                    self.assertEqual(
                        "MALFORMED_RECEIPT", raised.exception.code
                    )

            for key in ("", " padded ", "control\nkey"):
                with self.subTest(receipt_key=repr(key)):
                    invalid = json.loads(json.dumps(plan))
                    invalid["receipts"] = {
                        key: json.loads(json.dumps(valid_receipt))
                    }
                    with self.assertRaises(PlanCodecError) as raised:
                        encode_plan(invalid)
                    self.assertEqual(
                        "MALFORMED_RECEIPT", raised.exception.code
                    )

            malformed_object = json.loads(json.dumps(plan))
            malformed_object["receipts"] = {"request-1": "not-an-object"}
            with self.assertRaises(PlanCodecError):
                encode_plan(malformed_object)

            missing_core = json.loads(json.dumps(plan))
            del missing_core["receipts"]["request-1"]["transaction_id"]
            with self.assertRaises(PlanCodecError) as raised:
                encode_plan(missing_core)
            self.assertEqual("MALFORMED_RECEIPT", raised.exception.code)

            duplicate_transaction = json.loads(json.dumps(plan))
            duplicate_transaction["receipts"]["request-2"] = json.loads(
                json.dumps(valid_receipt)
            )
            with self.assertRaises(PlanCodecError) as raised:
                encode_plan(duplicate_transaction)
            self.assertEqual("MALFORMED_RECEIPT", raised.exception.code)

    def test_store_patch_and_rollback_receipts_round_trip_strict_codec(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trips_root = Path(temporary) / "trips"
            _write_legacy_trip(trips_root)
            store = TripStore(trips_root, "migration-fixture")
            migrated = store.commit_migration(store.preview_migration())
            self.assertTrue(migrated.success)

            plan = store.load_plan()
            activity_id = plan["state"]["itinerary"]["days"][0]["places"][0][
                "activity_id"
            ]
            patch = PlanPatch(
                trip_id=plan["trip_id"],
                base_revision=plan["revision"],
                idempotency_key="receipt-round-trip-patch",
                operations=(
                    UpdateActivity(
                        op_id="receipt-update",
                        activity_id=activity_id,
                        fields={"note": "round-trip"},
                    ),
                ),
            )
            applied = store.apply_patch(patch)
            self.assertTrue(applied.success)
            self.assertEqual("applied", applied.status)
            replayed = store.apply_patch(patch)
            self.assertTrue(replayed.success)
            self.assertTrue(replayed.replayed)

            after_patch = store.load_plan()
            patch_receipt = after_patch["receipts"][
                "receipt-round-trip-patch"
            ]
            self.assertEqual("patch", patch_receipt["kind"])
            self.assertTrue(
                patch_receipt["request_digest"].startswith("sha256:")
            )
            decode_plan(encode_plan(after_patch))

            rolled_back = store.rollback(
                applied.transaction_id,
                applied.applied_revision,
                "receipt-round-trip-rollback",
            )
            self.assertTrue(rolled_back.success)
            self.assertEqual("rolled_back", rolled_back.status)
            rollback_replay = store.rollback(
                applied.transaction_id,
                applied.applied_revision,
                "receipt-round-trip-rollback",
            )
            self.assertTrue(rollback_replay.success)
            self.assertTrue(rollback_replay.replayed)

            after_rollback = store.load_plan()
            self.assertEqual(
                "rolled_back",
                after_rollback["receipts"]["receipt-round-trip-patch"][
                    "status"
                ],
            )
            rollback_receipt = after_rollback["receipts"][
                "receipt-round-trip-rollback"
            ]
            self.assertEqual("rollback", rollback_receipt["kind"])
            self.assertEqual("applied", rollback_receipt["status"])
            decode_plan(encode_plan(after_rollback))

    def test_plan_to_trip_state_is_semantically_equivalent_to_legacy_loader(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir, _trip, _itinerary = _write_legacy_trip(Path(temporary))
            legacy = load_legacy_trip(trip_dir)
            preview = preview_legacy_migration(trip_dir)
            canonical = plan_to_trip_state(preview.mutable_candidate_plan())

            self.assertEqual(_semantic_signature(legacy), _semantic_signature(canonical))
            self.assertEqual(preview.candidate_revision, canonical.revision)
            self.assertEqual("trip-planner.plan/v1", canonical.schema_version)

    def test_tainan_dangling_edge_is_compatibility_only(self) -> None:
        source = TRIPS_ROOT / "tainan-2026-04" / "data"
        self.assertTrue(source.is_dir(), f"missing real-trip fixture: {source}")
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "tainan-2026-04" / "data"
            shutil.copytree(source, copied)
            original_itinerary = json.loads(
                (copied / "itinerary.json").read_text(encoding="utf-8")
            )

            preview = preview_legacy_migration(copied)
            plan = preview.mutable_candidate_plan()
            migration = plan["state"]["trip"][MIGRATION_META_KEY]["migration"]
            fragments = migration["ignored_travel_edges"]

            self.assertIn(
                "INVALID_TRAVEL_REFERENCE",
                {issue.code for issue in preview.issues},
            )
            self.assertGreaterEqual(len(fragments), 1)
            canonical_days = {
                day["day_id"]: day for day in plan["state"]["itinerary"]["days"]
            }
            _trip_view, itinerary_view = legacy_compatibility_views(plan)
            view_days = {
                day["day_id"]: day for day in itinerary_view["days"]
            }

            for fragment in fragments:
                canonical_travel = canonical_days[fragment["day_id"]].get(
                    "travel", []
                )
                self.assertNotIn(fragment["edge"], canonical_travel)
                restored = view_days[fragment["day_id"]]["travel"][
                    fragment["index"]
                ]
                self.assertEqual(fragment["edge"], restored)

            original_edges = sum(
                len(day.get("travel", []))
                for day in original_itinerary["days"]
            )
            canonical_edges = sum(
                len(day.get("travel", []))
                for day in plan["state"]["itinerary"]["days"]
            )
            compatibility_edges = sum(
                len(day.get("travel", [])) for day in itinerary_view["days"]
            )
            self.assertEqual(original_edges, compatibility_edges)
            self.assertEqual(
                original_edges - len(fragments), canonical_edges
            )
            plan_to_trip_state(plan)

    def test_real_trip_previews_are_deterministic_read_only_and_preserve_aux(
        self,
    ) -> None:
        real_data_dirs = sorted(TRIPS_ROOT.glob("*/data"))
        self.assertGreaterEqual(len(real_data_dirs), 3)

        for source in real_data_dirs:
            with self.subTest(trip=source.parent.name):
                with tempfile.TemporaryDirectory() as temporary:
                    copied = Path(temporary) / source.parent.name / "data"
                    shutil.copytree(source, copied)
                    before = _tree_snapshot(copied)
                    aux_before = {
                        name: value
                        for name, value in before.items()
                        if name not in {"trip.json", "itinerary.json"}
                    }

                    first = preview_legacy_migration(copied)
                    second = preview_legacy_migration(copied)
                    state = plan_to_trip_state(first.mutable_candidate_plan())

                    self.assertEqual(first.candidate_bytes, second.candidate_bytes)
                    self.assertEqual(first.preview_digest, second.preview_digest)
                    self.assertEqual(first.id_mapping, second.id_mapping)
                    self.assertEqual(first.candidate_revision, state.revision)
                    after = _tree_snapshot(copied)
                    self.assertEqual(before, after)
                    self.assertEqual(
                        aux_before,
                        {
                            name: value
                            for name, value in after.items()
                            if name not in {"trip.json", "itinerary.json"}
                        },
                    )
                    self.assertFalse((copied / "plan.json").exists())

    def test_canonical_reader_wins_and_malformed_plan_never_falls_back(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir, legacy_trip, _itinerary = _write_legacy_trip(Path(temporary))
            data_dir = trip_dir / "data"
            preview = preview_legacy_migration(trip_dir)
            plan = preview.mutable_candidate_plan()
            state = plan["state"]
            state["trip"]["title"] = "Canonical wins"
            canonical = build_plan(
                trip_id=plan["trip_id"],
                generation=plan["generation"] + 1,
                state=state,
                receipts=plan["receipts"],
            )
            (data_dir / "plan.json").write_bytes(encode_plan(canonical))

            trip_view, _itinerary_view, trip_id, revision = load_trip_views(
                data_dir
            )
            self.assertEqual("Canonical wins", trip_view["title"])
            self.assertNotEqual(legacy_trip["title"], trip_view["title"])
            self.assertEqual(canonical["trip_id"], trip_id)
            self.assertEqual(canonical["revision"], revision)

            (data_dir / "plan.json").write_text(
                '{"schema_version":"broken"}', encoding="utf-8"
            )
            with self.assertRaises(PlanCodecError):
                load_trip_views(data_dir)
            validation_errors = validate(trip_dir)
            self.assertTrue(validation_errors)
            self.assertTrue(validation_errors[0].startswith("plan.json:"))
            with self.assertRaises(CanonicalWriteRefused):
                refuse_canonical_write(data_dir)

    def test_legacy_writers_refuse_canonical_before_external_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir, _trip, _itinerary = _write_legacy_trip(Path(temporary))
            data_dir = trip_dir / "data"
            preview = preview_legacy_migration(trip_dir)
            (data_dir / "plan.json").write_bytes(preview.candidate_bytes)
            before = _tree_snapshot(trip_dir)

            commands = (
                (
                    [sys.executable, "scripts/build_itinerary.py"],
                    json.dumps(
                        {
                            "cache_path": str(data_dir / "missing-cache.json"),
                            "output_path": str(data_dir / "itinerary.json"),
                            "days": [],
                        }
                    ),
                ),
                (
                    [
                        sys.executable,
                        "scripts/enrich_itinerary.py",
                        str(data_dir / "missing-itinerary.json"),
                    ],
                    None,
                ),
                (
                    [
                        sys.executable,
                        "scripts/import_gmaps_list.py",
                        "--merge",
                        str(trip_dir),
                        "not-a-url",
                    ],
                    None,
                ),
            )
            for command, stdin in commands:
                with self.subTest(script=command[1]):
                    result = subprocess.run(
                        command,
                        cwd=REPO_ROOT,
                        input=stdin,
                        text=True,
                        capture_output=True,
                        timeout=10,
                    )
                    self.assertEqual(2, result.returncode, result.stderr)
                    self.assertIn("PlanPatch", result.stderr)
                    self.assertEqual(before, _tree_snapshot(trip_dir))

    def test_legacy_reader_and_build_itinerary_behavior_remain_available(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trip_dir, trip, itinerary = _write_legacy_trip(root)
            data_dir = trip_dir / "data"

            loaded_trip, loaded_itinerary, trip_id, revision = load_trip_views(
                data_dir
            )
            self.assertEqual(trip, loaded_trip)
            self.assertEqual(itinerary, loaded_itinerary)
            self.assertIsNone(trip_id)
            self.assertIsNone(revision)
            self.assertEqual([], validate(trip_dir))

            output_dir = root / "new-legacy" / "data"
            output_dir.mkdir(parents=True)
            cache_path = output_dir / "places_cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "google-museum": {
                            "display_name": "Museum",
                            "maps_query": "Museum, Busan",
                            "lat": 35.1,
                            "lng": 129.1,
                        }
                    }
                ),
                encoding="utf-8",
            )
            output_path = output_dir / "itinerary.json"
            payload = {
                "cache_path": str(cache_path),
                "output_path": str(output_path),
                "days": [
                    {
                        "day": 1,
                        "date": "2026-10-01",
                        "places": [
                            {
                                "name": "Museum",
                                "type": "spot",
                                "time": "10:00",
                                "note": "",
                            }
                        ],
                    }
                ],
            }
            result = subprocess.run(
                [sys.executable, "scripts/build_itinerary.py"],
                cwd=REPO_ROOT,
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            built = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual("Museum", built["days"][0]["places"][0]["title"])

    def test_validator_accepts_overnight_legacy_times_and_requires_sidecars(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir, _trip, itinerary = _write_legacy_trip(Path(temporary))
            itinerary["days"][0]["available_start"] = "22:00"
            itinerary["days"][0]["available_end"] = "02:00"
            itinerary["days"][0]["places"][0]["time"] = "23:50"
            itinerary["days"][0]["places"][1]["time"] = "00:10"
            (trip_dir / "data" / "itinerary.json").write_bytes(
                _json_bytes(itinerary)
            )
            self.assertEqual([], validate(trip_dir))

            (trip_dir / "data" / "places_cache.json").unlink()
            errors = validate(trip_dir)
            self.assertIn(
                "Missing required file: places_cache.json (places cache)", errors
            )

    def test_validator_rejects_backwards_times_without_overnight_window(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir, _trip, itinerary = _write_legacy_trip(Path(temporary))
            itinerary["days"][0]["places"][0]["time"] = "10:00"
            itinerary["days"][0]["places"][1]["time"] = "09:00"
            (trip_dir / "data" / "itinerary.json").write_bytes(
                _json_bytes(itinerary)
            )
            errors = validate(trip_dir)
            self.assertTrue(
                any("time not ascending" in error for error in errors), errors
            )

    def test_validator_rejects_backwards_times_outside_overnight_window(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir, _trip, itinerary = _write_legacy_trip(Path(temporary))
            itinerary["days"][0]["available_start"] = "22:00"
            itinerary["days"][0]["available_end"] = "02:00"
            itinerary["days"][0]["places"][0]["time"] = "10:00"
            itinerary["days"][0]["places"][1]["time"] = "09:00"
            (trip_dir / "data" / "itinerary.json").write_bytes(
                _json_bytes(itinerary)
            )
            errors = validate(trip_dir)
            self.assertTrue(
                any("time not ascending" in error for error in errors), errors
            )

    def test_validator_requires_canonical_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trip_dir, _trip, _itinerary = _write_legacy_trip(Path(temporary))
            preview = preview_legacy_migration(trip_dir)
            (trip_dir / "data" / "plan.json").write_bytes(preview.candidate_bytes)
            (trip_dir / "data" / "todo.json").unlink()
            errors = validate(trip_dir)
            self.assertIn(
                "Missing required file: todo.json (pre-trip checklist)", errors
            )

    def test_user_facing_clis_report_usage_without_input(self) -> None:
        commands = (
            [sys.executable, "scripts/build_itinerary.py"],
            [sys.executable, "scripts/enrich_itinerary.py"],
            [sys.executable, "scripts/render_trip.py"],
        )
        for command in commands:
            with self.subTest(script=command[1]):
                result = subprocess.run(
                    command,
                    cwd=REPO_ROOT,
                    input="",
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertIn("Usage:", result.stderr)

    def test_build_itinerary_reports_missing_required_stdin_fields(self) -> None:
        payloads = (
            ({}, "cache_path, days"),
            ({"cache_path": "missing.json"}, "days"),
            ({"days": []}, "cache_path"),
        )
        for payload, missing in payloads:
            with self.subTest(payload=payload):
                result = subprocess.run(
                    [sys.executable, "scripts/build_itinerary.py"],
                    cwd=REPO_ROOT,
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertIn("missing required stdin field", result.stderr)
                self.assertIn(missing, result.stderr)
                self.assertIn("Usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
