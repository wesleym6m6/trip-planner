"""Phase 5.32 must-not-exist canonical plan creation."""

from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from tests.test_phase53_guided_draft import _transport_boundary
from tests.test_phase56_guided_refinement import _brief
from tests.test_phase58_guided_itinerary import _accepted_context, _candidate
from tests.test_phase59_guided_itinerary_response import _capture
from trip_planner.codec import build_plan
from trip_planner.guided_itinerary import (
    GuidedItineraryResponseKind,
    capture_guided_itinerary_response,
)
from trip_planner.mutations import PlanPatch, UpdateActivity
from trip_planner.plan_creation import (
    PlanCreateRequest,
    prepare_guided_plan_create_request,
)
from trip_planner.store import StoreError, TripStore


UTC = timezone.utc
EVALUATION_AT = datetime(2026, 8, 8, 12, tzinfo=UTC)
SOURCE_BINDING = "a" * 64


def _plan(slug: str = "phase532-create") -> dict[str, object]:
    return build_plan(
        trip_id=slug,
        generation=1,
        state={
            "trip": {
                "slug": slug,
                "title": "Phase 5.32 create fixture",
                "timezone": "Asia/Taipei",
                "date_range": "2026-10-01 ~ 2026-10-01",
                "cities": ["Fixture City"],
            },
            "itinerary": {
                "available_modes": ["walking"],
                "days": [
                    {
                        "day_id": "day-1",
                        "day": 1,
                        "date": "2026-10-01",
                        "timezone": "Asia/Taipei",
                        "available_start": "08:00",
                        "available_end": "20:00",
                        "start_location_id": "loc-a",
                        "end_location_id": "loc-a",
                        "places": [
                            {
                                "activity_id": "activity-a",
                                "title": "Candidate Alpha",
                                "location_id": "loc-a",
                                "time": "10:00",
                                "duration_min": 60,
                                "decision_state": "candidate",
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


def _legacy_sources(slug: str) -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "title": "Legacy race fixture",
            "date_range": "2026-10-01 ~ 2026-10-01",
            "cities": ["Fixture City"],
            "slug": slug,
            "timezone": "Asia/Taipei",
        },
        {
            "available_modes": ["walking"],
            "days": [
                {
                    "day": 1,
                    "date": "2026-10-01",
                    "title": "Legacy day",
                    "available_start": "08:00",
                    "available_end": "20:00",
                    "places": [
                        {
                            "title": "Legacy Alpha",
                            "location_id": "legacy-loc-a",
                            "time": "10:00",
                            "duration_min": 60,
                        }
                    ],
                    "travel": [],
                }
            ],
        },
    )


class PlanCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.trips_root = self.root / "trips"
        self.slug = "phase532-create"
        self.data_dir = self.trips_root / self.slug / "data"
        self.data_dir.mkdir(parents=True)
        self.plan_path = self.data_dir / "plan.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def store(self, fault_stage: str | None = None) -> TripStore:
        def fault(stage: str) -> None:
            if stage == fault_stage:
                raise RuntimeError(f"forced {stage}")

        return TripStore(
            self.trips_root,
            self.slug,
            fault_hook=fault if fault_stage is not None else None,
        )

    def request(
        self,
        *,
        key: str = "create-phase532-v1",
        kind: GuidedItineraryResponseKind = (
            GuidedItineraryResponseKind.ACCEPT_ITINERARY_CANDIDATE
        ),
    ):
        context = _capture(kind)
        return prepare_guided_plan_create_request(
            *context,
            trip_id=self.slug,
            timezone_name="Asia/Taipei",
            idempotency_key=key,
            evaluation_at=EVALUATION_AT,
        )

    def assert_problem(self, result, code: str) -> None:
        self.assertIn(code, {item.code for item in result.problems})

    def test_preview_is_read_only_create_has_receipt_and_retry_replays(self) -> None:
        store = self.store()
        request = self.request()
        preview = store.preview_create(request)
        self.assertFalse(self.plan_path.exists())
        self.assertFalse(preview.to_safe_dict()["writes_to_trip"])
        self.assertFalse(preview.to_safe_dict()["candidate_plan_exposed"])

        created = store.commit_create(preview)
        self.assertTrue(created.success, created.to_dict())
        self.assertEqual("created", created.status)
        persisted = store.load_plan()
        receipt = persisted["receipts"][request.idempotency_key]
        self.assertEqual("create", receipt["kind"])
        self.assertTrue(receipt["expected_absent"])
        self.assertEqual(request.candidate_sha256, receipt["candidate_sha256"])
        self.assertNotIn("base_revision", receipt)

        replay = store.commit_create(preview)
        self.assertTrue(replay.success)
        self.assertTrue(replay.replayed)
        self.assertEqual(created.transaction_id, replay.transaction_id)

    def test_legacy_source_blocks_preview_and_appearing_legacy_blocks_commit(self) -> None:
        request = self.request()
        preview = self.store().preview_create(request)
        (self.data_dir / "trip.json").write_text("{}", encoding="utf-8")
        blocked = self.store().commit_create(preview)
        self.assertFalse(blocked.success)
        self.assert_problem(blocked, "LEGACY_SOURCE_PRESENT")
        self.assertFalse(self.plan_path.exists())
        with self.assertRaises(StoreError) as caught:
            self.store().preview_create(request)
        self.assertEqual("LEGACY_SOURCE_PRESENT", caught.exception.code)

    def test_migration_winning_after_create_preview_is_never_overwritten(self) -> None:
        request = self.request()
        preview = self.store().preview_create(request)
        trip, itinerary = _legacy_sources(self.slug)
        (self.data_dir / "trip.json").write_text(
            json.dumps(trip), encoding="utf-8"
        )
        (self.data_dir / "itinerary.json").write_text(
            json.dumps(itinerary), encoding="utf-8"
        )
        store = self.store()
        migrated = store.commit_migration(store.preview_migration())
        self.assertTrue(migrated.success, migrated.to_dict())
        before = self.plan_path.read_bytes()
        rejected = store.commit_create(preview)
        self.assertFalse(rejected.success)
        self.assert_problem(rejected, "PLAN_ALREADY_EXISTS")
        self.assertEqual(before, self.plan_path.read_bytes())

    def test_concurrent_same_request_is_one_create_and_one_receipt_replay(self) -> None:
        request = self.request()
        preview = self.store().preview_create(request)

        def commit(_index: int):
            return self.store().commit_create(preview)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(commit, range(2)))
        self.assertEqual(2, sum(item.success for item in results))
        self.assertEqual(1, sum(item.status == "created" for item in results))
        self.assertEqual(1, sum(item.replayed for item in results))

    def test_different_create_request_cannot_replace_the_winner(self) -> None:
        first = self.request(key="create-first")
        second = self.request(key="create-second")
        first_preview = self.store().preview_create(first)
        second_preview = self.store().preview_create(second)
        created = self.store().commit_create(first_preview)
        self.assertTrue(created.success)
        before = self.plan_path.read_bytes()
        rejected = self.store().commit_create(second_preview)
        self.assertFalse(rejected.success)
        self.assert_problem(rejected, "PLAN_ALREADY_EXISTS")
        self.assertEqual(before, self.plan_path.read_bytes())

    def test_fault_before_install_leaves_absent_and_lost_ack_reconciles(self) -> None:
        request = self.request()
        preview = self.store().preview_create(request)
        before = self.store("before_replace").commit_create(preview)
        self.assertFalse(before.success)
        self.assertEqual("write_failed", before.status)
        self.assertFalse(self.plan_path.exists())

        unknown = self.store("after_replace").commit_create(preview)
        self.assertFalse(unknown.success)
        self.assertEqual("commit_outcome_unknown", unknown.status)
        self.assertTrue(self.plan_path.exists())
        replay = self.store().commit_create(preview)
        self.assertTrue(replay.success)
        self.assertTrue(replay.replayed)
        self.assertEqual(unknown.transaction_id, replay.transaction_id)

    def test_tampered_preview_and_existing_symlink_fail_closed(self) -> None:
        request = self.request()
        preview = self.store().preview_create(request)
        original = preview.preview_digest
        object.__setattr__(preview, "preview_digest", "0" * 64)
        try:
            rejected = self.store().commit_create(preview)
        finally:
            object.__setattr__(preview, "preview_digest", original)
        self.assertFalse(rejected.success)
        self.assertFalse(self.plan_path.exists())

        target = self.data_dir / "outside.json"
        target.write_text("outside", encoding="utf-8")
        self.plan_path.symlink_to(target)
        with self.assertRaises(StoreError):
            self.store()
        self.assertEqual("outside", target.read_text(encoding="utf-8"))

    def test_preview_target_binding_rejects_other_roots_before_receipt_replay(self) -> None:
        request = self.request()
        preview = self.store().preview_create(request)
        for state in ("absent", "exact", "different"):
            with self.subTest(state=state):
                other_root = self.root / f"other-{state}"
                (other_root / self.slug / "data").mkdir(parents=True)
                other = TripStore(other_root, self.slug)
                if state != "absent":
                    other_request = (
                        request
                        if state == "exact"
                        else self.request(key="different-target-winner")
                    )
                    created = other.commit_create(
                        other.preview_create(other_request)
                    )
                    self.assertTrue(created.success, created.to_dict())
                rejected = other.commit_create(preview)
                self.assertFalse(rejected.success)
                self.assert_problem(rejected, "CREATE_TARGET_MISMATCH")

    def test_create_requires_accepted_source_and_projects_narrow_state(self) -> None:
        with self.assertRaises(ValueError):
            self.request(
                kind=GuidedItineraryResponseKind.REQUEST_ITINERARY_ADJUSTMENT
            )
        request = self.request()
        plan = request.mutable_candidate_plan()
        self.assertEqual({}, plan["receipts"])
        self.assertNotIn("lodgings", plan["state"]["trip"])
        places = [
            place
            for day in plan["state"]["itinerary"]["days"]
            for place in day["places"]
        ]
        self.assertTrue(places)
        self.assertTrue(
            all(
                item["decision_state"] == "candidate"
                and item["flexibility"] == "movable"
                and item["evidence_state"] == "unverified"
                and item["type"] == "activity"
                for item in places
            )
        )
        with self.assertRaises(ValueError):
            PlanCreateRequest(
                trip_id=self.slug,
                idempotency_key="untrusted-direct-create",
                source_binding_digest=SOURCE_BINDING,
                evaluation_at=EVALUATION_AT,
                candidate_plan=_plan(self.slug),
            )

    def test_nonempty_transport_boundary_fails_closed_instead_of_being_lost(self) -> None:
        boundary = _transport_boundary()
        brief = replace(_brief(), transport_boundaries=(boundary,))
        context = _accepted_context(brief=brief)
        itinerary = _candidate(
            (0, (0,)),
            (1, (1,)),
            boundary_ids=(boundary.boundary_id,),
        )
        response = capture_guided_itinerary_response(
            *context,
            itinerary,
            kind=GuidedItineraryResponseKind.ACCEPT_ITINERARY_CANDIDATE,
        )
        with self.assertRaisesRegex(ValueError, "transport boundaries"):
            prepare_guided_plan_create_request(
                *context,
                itinerary,
                response,
                trip_id=self.slug,
                timezone_name="Asia/Taipei",
                idempotency_key="boundary-must-not-disappear",
                evaluation_at=EVALUATION_AT,
            )

    def test_create_receipt_survives_patch_replay_and_cannot_be_rollback_target(self) -> None:
        request = self.request()
        preview = self.store().preview_create(request)
        created = self.store().commit_create(preview)
        rollback = self.store().rollback(
            created.transaction_id,
            created.current_revision,
            "rollback-create-forbidden",
            evaluation_at=EVALUATION_AT,
        )
        self.assertFalse(rollback.success)
        self.assert_problem(rollback, "TRANSACTION_NOT_ROLLBACKABLE")

        plan = self.store().load_plan()
        activity_id = plan["state"]["itinerary"]["days"][0]["places"][0][
            "activity_id"
        ]
        patch = PlanPatch(
            trip_id=self.slug,
            base_revision=plan["revision"],
            idempotency_key="post-create-note",
            operations=(
                UpdateActivity(
                    "update-note",
                    activity_id,
                    {"note": "reviewed later"},
                ),
            ),
            intent="phase532 post-create patch",
        )
        applied = self.store().apply_patch(patch, evaluation_at=EVALUATION_AT)
        self.assertTrue(applied.success, applied.to_dict())
        replay = self.store().commit_create(preview)
        self.assertTrue(replay.success)
        self.assertTrue(replay.replayed)
        self.assertEqual(applied.current_revision, replay.current_revision)
        self.assertEqual(created.applied_revision, replay.applied_revision)



if __name__ == "__main__":
    unittest.main()
