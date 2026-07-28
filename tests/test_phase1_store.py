"""Offline adversarial tests for the Phase 1 canonical trip store."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trip_planner.codec import (
    canonical_json_bytes,
    compute_revision,
    encode_plan,
)
from trip_planner.mutations import (
    AddActivity,
    AddConstraint,
    ApprovalGrant,
    PlanPatch,
    RemoveActivity,
    UpdateActivity,
    UpdateDay,
)
from trip_planner.store import (
    StoreError,
    StoreResult,
    TripStore,
    _rollback_protected_changes,
)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _days(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return plan["state"]["itinerary"]["days"]


def _activities(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        activity
        for day in _days(plan)
        for activity in day["places"]
    ]


def _activity(plan: dict[str, Any], activity_id: str) -> dict[str, Any]:
    return next(
        activity
        for activity in _activities(plan)
        if activity["activity_id"] == activity_id
    )


class _InjectedFault(RuntimeError):
    pass


class Phase1StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.trips_root = self.root / "trips"
        self.slug = "store-fixture"
        self.data_dir = self.trips_root / self.slug / "data"
        self.data_dir.mkdir(parents=True)
        self.plan_path = self.data_dir / "plan.json"
        self.history_dir = self.data_dir / ".trip-planner-history"

        self.trip_source = {
            "title": "Store fixture",
            "subtitle": "offline",
            "date_range": "2026-10-01 ~ 2026-10-01",
            "cities": ["Fixture City"],
            "slug": self.slug,
            "timezone": "Asia/Taipei",
        }
        self.itinerary_source = {
            "available_modes": ["walking"],
            "days": [
                {
                    "day": 1,
                    "date": "2026-10-01",
                    "title": "Fixture day",
                    "available_start": "08:00",
                    "available_end": "20:00",
                    "places": [
                        {
                            "title": "Alpha",
                            "location_id": "location-alpha",
                            "time": "09:00",
                            "duration_min": 45,
                            "note": "alpha original",
                        },
                        {
                            "title": "Beta",
                            "location_id": "location-beta",
                            "time": "11:00",
                            "duration_min": 60,
                            "note": "beta original",
                        },
                    ],
                    "travel": [
                        {
                            "from": 0,
                            "to": 1,
                            "modes": {
                                "walking": {
                                    "duration_min": 12,
                                    "evidence_state": "unverified",
                                }
                            },
                            "recommended_mode": "walking",
                        }
                    ],
                }
            ],
        }
        _write_json(self.data_dir / "trip.json", self.trip_source)
        _write_json(
            self.data_dir / "itinerary.json", self.itinerary_source
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _store(self, fault_stage: str | None = None) -> TripStore:
        def fault_hook(stage: str) -> None:
            if stage == fault_stage:
                raise _InjectedFault(f"injected failure at {stage}")

        return TripStore(
            self.trips_root,
            self.slug,
            fault_hook=fault_hook if fault_stage is not None else None,
        )

    def _migrate(self) -> tuple[TripStore, dict[str, Any]]:
        store = self._store()
        result = store.commit_migration(store.preview_migration())
        self.assertTrue(result.success, result.to_dict())
        self.assertEqual("migrated", result.status)
        plan = store.load_plan()
        by_title = {
            activity["title"]: activity["activity_id"]
            for activity in _activities(plan)
        }
        self.alpha = by_title["Alpha"]
        self.beta = by_title["Beta"]
        self.day_id = _days(plan)[0]["day_id"]
        return store, plan

    def _patch(
        self,
        plan: dict[str, Any],
        *operations: Any,
        key: str,
    ) -> PlanPatch:
        return PlanPatch(
            trip_id=plan["trip_id"],
            base_revision=plan["revision"],
            idempotency_key=key,
            operations=tuple(operations),
            intent="offline store fixture",
        )

    def _note_patch(
        self,
        plan: dict[str, Any],
        note: str,
        *,
        key: str,
        activity_id: str | None = None,
    ) -> PlanPatch:
        return self._patch(
            plan,
            UpdateActivity(
                f"note-{key}",
                activity_id or self.alpha,
                {"note": note},
            ),
            key=key,
        )

    def assert_problem(self, result: StoreResult, code: str) -> None:
        self.assertIn(code, {problem.code for problem in result.problems})

    def _grant(self, scope: str, *, approval_id: str) -> ApprovalGrant:
        return ApprovalGrant(
            approval_id=approval_id,
            scope_digest=scope,
            approved_by="fixture-human",
            approved_at="2026-07-27T00:00:00Z",
        )

    def test_migration_commits_exact_preview_and_replays(self) -> None:
        store = self._store()
        legacy_paths = (
            self.data_dir / "trip.json",
            self.data_dir / "itinerary.json",
        )
        before_preview = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in legacy_paths
        }
        preview = store.preview_migration()
        self.assertFalse(self.plan_path.exists())
        self.assertEqual(
            before_preview,
            {
                path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in legacy_paths
            },
        )

        first = store.commit_migration(preview)

        self.assertTrue(first.success, first.to_dict())
        self.assertEqual("migrated", first.status)
        self.assertTrue(first.changed)
        self.assertEqual(preview.candidate_bytes, self.plan_path.read_bytes())

        before_replay = self.plan_path.read_bytes()
        replay = store.commit_migration(preview)
        self.assertTrue(replay.success, replay.to_dict())
        self.assertEqual("replayed", replay.status)
        self.assertTrue(replay.replayed)
        self.assertFalse(replay.changed)
        self.assertEqual(before_replay, self.plan_path.read_bytes())

    def test_stale_migration_preview_is_rejected_without_plan(self) -> None:
        store = self._store()
        preview = store.preview_migration()
        changed = deepcopy(self.itinerary_source)
        changed["days"][0]["title"] = "Changed after preview"
        _write_json(self.data_dir / "itinerary.json", changed)

        result = store.commit_migration(preview)

        self.assertFalse(result.success)
        self.assert_problem(result, "STALE_MIGRATION_PREVIEW")
        self.assertFalse(self.plan_path.exists())

    def test_migration_fault_before_replace_keeps_target_absent(self) -> None:
        preview = self._store().preview_migration()

        failed = self._store("before_replace").commit_migration(preview)

        self.assertFalse(failed.success)
        self.assertEqual("write_failed", failed.status)
        self.assert_problem(failed, "STORE_WRITE_FAILED")
        self.assertFalse(failed.changed)
        self.assertFalse(self.plan_path.exists())
        self.assertFalse(any(self.data_dir.glob(".plan.*.tmp")))

    def test_migration_fault_after_replace_replays_exact_candidate(self) -> None:
        preview = self._store().preview_migration()

        uncertain = self._store("after_replace").commit_migration(preview)

        self.assertFalse(uncertain.success)
        self.assertEqual("commit_outcome_unknown", uncertain.status)
        self.assert_problem(uncertain, "COMMIT_OUTCOME_UNKNOWN")
        self.assertTrue(uncertain.changed)
        self.assertEqual(preview.candidate_bytes, self.plan_path.read_bytes())

        (self.data_dir / "trip.json").unlink()
        (self.data_dir / "itinerary.json").unlink()
        replay = self._store().commit_migration(preview)
        self.assertTrue(replay.success, replay.to_dict())
        self.assertEqual("replayed", replay.status)
        self.assertTrue(replay.replayed)
        self.assertEqual(preview.candidate_bytes, self.plan_path.read_bytes())

    def test_canonical_load_and_apply_survive_legacy_source_removal(self) -> None:
        store, plan = self._migrate()
        (self.data_dir / "trip.json").unlink()
        (self.data_dir / "itinerary.json").unlink()

        reopened = self._store()
        self.assertEqual(plan, reopened.load_plan())
        patch = self._note_patch(
            plan,
            "canonical remains writable",
            key="canonical-without-legacy",
        )
        applied = reopened.apply_patch(patch)
        self.assertTrue(applied.success, applied.to_dict())
        self.assertEqual("applied", applied.status)
        self.assertEqual(
            "canonical remains writable",
            _activity(reopened.load_plan(), self.alpha)["note"],
        )

    def test_stale_and_failed_multi_operation_patch_are_all_or_nothing(
        self,
    ) -> None:
        store, plan = self._migrate()
        original_bytes = self.plan_path.read_bytes()
        stale = PlanPatch(
            trip_id=plan["trip_id"],
            base_revision="0" * 64,
            idempotency_key="stale-patch",
            operations=(
                UpdateActivity("stale-note", self.alpha, {"note": "stale"}),
            ),
        )

        stale_result = store.apply_patch(stale)
        self.assertFalse(stale_result.success)
        self.assert_problem(stale_result, "STALE_REVISION")
        self.assertEqual(original_bytes, self.plan_path.read_bytes())

        failed = self._patch(
            plan,
            UpdateActivity(
                "first-would-succeed",
                self.alpha,
                {"note": "must not persist"},
            ),
            RemoveActivity("then-fails", "missing-activity"),
            key="failed-multi-operation",
        )
        failed_result = store.apply_patch(failed)
        self.assertFalse(failed_result.success)
        self.assert_problem(failed_result, "UNKNOWN_ENTITY")
        self.assertEqual(original_bytes, self.plan_path.read_bytes())
        self.assertEqual(plan, failed_result.mutable_candidate_plan())
        self.assertFalse(self.history_dir.exists())

    def test_dry_run_and_semantic_noop_are_write_free(self) -> None:
        store, plan = self._migrate()
        before = self.plan_path.read_bytes()
        dry_patch = self._note_patch(
            plan, "dry-run candidate", key="dry-run"
        )

        dry = store.apply_patch(dry_patch, dry_run=True)

        self.assertTrue(dry.success, dry.to_dict())
        self.assertEqual("preview_ready", dry.status)
        self.assertTrue(dry.dry_run)
        self.assertTrue(dry.changed)
        self.assertEqual(before, self.plan_path.read_bytes())
        self.assertFalse(self.history_dir.exists())
        self.assertNotIn("dry-run", store.load_plan()["receipts"])

        no_op_patch = self._note_patch(
            plan,
            _activity(plan, self.alpha)["note"],
            key="semantic-no-op",
        )
        no_op = store.apply_patch(no_op_patch)
        self.assertTrue(no_op.success, no_op.to_dict())
        self.assertEqual("no_op", no_op.status)
        self.assertFalse(no_op.changed)
        self.assertEqual(before, self.plan_path.read_bytes())
        self.assertNotIn("semantic-no-op", store.load_plan()["receipts"])

    def test_net_round_trip_patch_is_store_noop_without_invalidation(self) -> None:
        store, plan = self._migrate()
        before = self.plan_path.read_bytes()
        patch = self._patch(
            plan,
            UpdateActivity(
                "temporary-location",
                self.alpha,
                {"location_id": "location-temporary"},
            ),
            UpdateActivity(
                "restore-location",
                self.alpha,
                {"location_id": "location-alpha"},
            ),
            AddActivity(
                "temporary-add",
                "activity-temporary",
                self.day_id,
                {
                    "title": "Temporary",
                    "location_id": "location-temporary",
                },
            ),
            RemoveActivity("temporary-remove", "activity-temporary"),
            key="store-net-noop",
        )

        result = store.apply_patch(patch)

        self.assertTrue(result.success, result.to_dict())
        self.assertEqual("no_op", result.status)
        self.assertFalse(result.changed)
        self.assertEqual((), result.draft.invalidated_day_ids)
        self.assertEqual(before, self.plan_path.read_bytes())
        self.assertNotIn("store-net-noop", store.load_plan()["receipts"])
        self.assertFalse(self.history_dir.exists())

    def test_idempotency_replays_exact_request_and_rejects_key_reuse(
        self,
    ) -> None:
        store, plan = self._migrate()
        patch = self._note_patch(plan, "applied once", key="idempotent")

        first = store.apply_patch(patch)
        self.assertTrue(first.success, first.to_dict())
        self.assertEqual("applied", first.status)
        after_first = self.plan_path.read_bytes()

        replay = store.apply_patch(patch)
        self.assertTrue(replay.success, replay.to_dict())
        self.assertEqual("replayed", replay.status)
        self.assertTrue(replay.replayed)
        self.assertFalse(replay.changed)
        self.assertEqual(after_first, self.plan_path.read_bytes())

        reused = self._note_patch(
            plan,
            "different request",
            key="idempotent",
            activity_id=self.beta,
        )
        conflict = store.apply_patch(reused)
        self.assertFalse(conflict.success)
        self.assert_problem(conflict, "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(after_first, self.plan_path.read_bytes())

    def test_evaluation_at_is_shared_by_preview_apply_and_receipt(
        self,
    ) -> None:
        store, plan = self._migrate()
        patch = self._note_patch(
            plan,
            "evaluated with one deterministic instant",
            key="evaluation-context",
        )
        evaluation_at = datetime(
            2026,
            10,
            1,
            13,
            30,
            tzinfo=timezone(timedelta(hours=9)),
        )

        preview = store.preview_patch(
            patch,
            evaluation_at=evaluation_at,
        )
        applied = store.apply_patch(
            patch,
            evaluation_at=evaluation_at,
        )

        self.assertTrue(preview.success, preview.to_dict())
        self.assertTrue(applied.success, applied.to_dict())
        self.assertEqual(preview.applied_revision, applied.applied_revision)
        self.assertEqual(preview.check_report, applied.check_report)
        self.assertEqual(
            "2026-10-01T04:30:00+00:00",
            applied.receipt["evaluation_at"],
        )
        persisted = store.load_plan()["receipts"]["evaluation-context"]
        self.assertEqual(
            "2026-10-01T04:30:00+00:00",
            persisted["evaluation_at"],
        )

    def test_naive_evaluation_at_is_structurally_rejected_without_write(
        self,
    ) -> None:
        store, plan = self._migrate()
        patch = self._note_patch(
            plan,
            "must not be written",
            key="naive-evaluation-context",
        )
        before = self.plan_path.read_bytes()
        naive = datetime(2026, 10, 1, 4, 30)

        preview = store.preview_patch(patch, evaluation_at=naive)
        applied = store.apply_patch(patch, evaluation_at=naive)

        for result in (preview, applied):
            self.assertFalse(result.success)
            self.assert_problem(result, "INVALID_EVALUATION_AT")
            problem = next(
                item
                for item in result.problems
                if item.code == "INVALID_EVALUATION_AT"
            )
            self.assertEqual("evaluation_at", problem.details["path"])
        self.assertEqual(before, self.plan_path.read_bytes())

    def test_evaluation_at_controls_freshness_during_preview_and_apply(
        self,
    ) -> None:
        store, plan = self._migrate()
        day = _days(plan)[0]
        day["start_location_id"] = "location-alpha"
        day["end_location_id"] = "location-beta"
        for activity in day["places"]:
            activity["evidence_state"] = "verified"
        _activity(plan, self.beta)["time"] = "09:30"
        walking = day["travel"][0]["modes"]["walking"]
        walking["evidence_state"] = "verified"
        walking["fresh_until"] = "2026-12-01T00:00:00+00:00"
        plan["revision"] = compute_revision(plan)
        self.plan_path.write_bytes(encode_plan(plan))
        plan = store.load_plan()
        patch = self._note_patch(
            plan,
            "context-sensitive candidate",
            key="freshness-context",
        )

        without_context = store.preview_patch(patch)
        evaluation_at = datetime(
            2026,
            10,
            1,
            tzinfo=timezone.utc,
        )
        with_context = store.preview_patch(
            patch,
            evaluation_at=evaluation_at,
        )
        before_apply = self.plan_path.read_bytes()
        rejected_apply = store.apply_patch(
            patch,
            evaluation_at=evaluation_at,
        )

        self.assertTrue(without_context.success, without_context.to_dict())
        self.assertIn(
            "FRESHNESS_NOT_EVALUATED",
            {issue.code for issue in without_context.check_report.issues},
        )
        for result in (with_context, rejected_apply):
            self.assertFalse(result.success)
            self.assert_problem(result, "PLAN_INFEASIBLE")
            self.assertIn(
                "SCHEDULED_START_CONFLICT",
                {issue.code for issue in result.check_report.issues},
            )
        self.assertEqual(before_apply, self.plan_path.read_bytes())

    def test_needs_verification_commits_but_infeasible_candidate_does_not(
        self,
    ) -> None:
        store, plan = self._migrate()
        needs_verification = self._patch(
            plan,
            AddActivity(
                "add-unknown-duration",
                "activity-unknown-duration",
                self.day_id,
                {
                    "title": "Unknown duration",
                    "location_id": "location-unknown-duration",
                    "decision_state": "selected",
                    "flexibility": "movable",
                    "evidence_state": "unverified",
                },
            ),
            key="needs-verification",
        )
        accepted = store.apply_patch(needs_verification)
        self.assertTrue(accepted.success, accepted.to_dict())
        self.assertEqual("applied", accepted.status)
        self.assertEqual("needs_verification", accepted.check_status)

        current = store.load_plan()
        before_infeasible = self.plan_path.read_bytes()
        infeasible = self._patch(
            current,
            AddConstraint(
                "add-contradictory-fixed-time",
                "constraint-contradictory-fixed-time",
                {
                    "kind": "fixed_time",
                    "strength": "hard",
                    "subject_ids": [self.alpha],
                    "params": {"time": "10:00"},
                },
            ),
            key="infeasible",
        )
        rejected = store.apply_patch(infeasible)
        self.assertFalse(rejected.success)
        self.assert_problem(rejected, "PLAN_INFEASIBLE")
        self.assertEqual("infeasible", rejected.check_status)
        self.assertEqual(before_infeasible, self.plan_path.read_bytes())

    def test_concurrent_compare_and_swap_allows_only_one_base_revision(
        self,
    ) -> None:
        store, plan = self._migrate()
        first = self._note_patch(plan, "thread one", key="thread-one")
        second = self._note_patch(
            plan,
            "thread two",
            key="thread-two",
            activity_id=self.beta,
        )
        barrier = threading.Barrier(2)

        def apply(patch: PlanPatch) -> StoreResult:
            barrier.wait(timeout=5)
            return store.apply_patch(patch)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(apply, first),
                executor.submit(apply, second),
            ]
            results = [future.result(timeout=15) for future in futures]

        self.assertEqual(1, sum(result.success for result in results))
        self.assertEqual(
            1,
            sum(
                "STALE_REVISION"
                in {problem.code for problem in result.problems}
                for result in results
            ),
        )
        current = store.load_plan()
        self.assertEqual(plan["generation"] + 1, current["generation"])
        self.assertEqual(
            1,
            len(
                {
                    "thread-one",
                    "thread-two",
                }
                & set(current["receipts"])
            ),
        )

    def test_fault_before_replace_keeps_old_plan_and_no_receipt(self) -> None:
        _, plan = self._migrate()
        before = self.plan_path.read_bytes()
        patch = self._note_patch(plan, "never visible", key="before-replace")

        failed = self._store("before_replace").apply_patch(patch)

        self.assertFalse(failed.success)
        self.assertEqual("write_failed", failed.status)
        self.assert_problem(failed, "STORE_WRITE_FAILED")
        self.assertFalse(failed.changed)
        self.assertEqual(before, self.plan_path.read_bytes())
        self.assertNotIn(
            "before-replace", self._store().load_plan()["receipts"]
        )
        self.assertFalse(any(self.data_dir.glob(".plan.*.tmp")))

    def test_fault_after_replace_is_unknown_but_exact_replay_recovers(
        self,
    ) -> None:
        _, plan = self._migrate()
        patch = self._note_patch(
            plan, "replace happened", key="after-replace"
        )

        uncertain = self._store("after_replace").apply_patch(patch)

        self.assertFalse(uncertain.success)
        self.assertEqual("commit_outcome_unknown", uncertain.status)
        self.assert_problem(uncertain, "COMMIT_OUTCOME_UNKNOWN")
        self.assertTrue(uncertain.changed)
        committed = self._store().load_plan()
        self.assertEqual(
            "replace happened", _activity(committed, self.alpha)["note"]
        )
        self.assertIn("after-replace", committed["receipts"])

        replay = self._store().apply_patch(patch)
        self.assertTrue(replay.success, replay.to_dict())
        self.assertTrue(replay.replayed)
        self.assertEqual("replayed", replay.status)

    def test_rollback_restores_state_without_aba_and_replays_old_receipts(
        self,
    ) -> None:
        store, initial = self._migrate()
        patch = self._note_patch(
            initial, "state to roll back", key="patch-to-rollback"
        )
        applied = store.apply_patch(patch)
        self.assertTrue(applied.success, applied.to_dict())
        self.assertIsNotNone(applied.transaction_id)
        current = store.load_plan()
        rollback_evaluation_at = datetime(
            2026,
            10,
            2,
            9,
            0,
            tzinfo=timezone(timedelta(hours=9)),
        )

        rollback = store.rollback(
            applied.transaction_id,
            current["revision"],
            "rollback-once",
            evaluation_at=rollback_evaluation_at,
        )

        self.assertTrue(rollback.success, rollback.to_dict())
        self.assertEqual("rolled_back", rollback.status)
        restored = store.load_plan()
        self.assertEqual(initial["state"], restored["state"])
        self.assertEqual(initial["generation"] + 2, restored["generation"])
        self.assertNotEqual(initial["revision"], restored["revision"])
        self.assertNotEqual(current["revision"], restored["revision"])
        self.assertEqual(
            "rolled_back",
            restored["receipts"]["patch-to-rollback"]["status"],
        )
        self.assertEqual(
            rollback.transaction_id,
            restored["receipts"]["patch-to-rollback"]["rolled_back_by"],
        )
        self.assertEqual(
            "2026-10-02T00:00:00+00:00",
            restored["receipts"]["rollback-once"]["evaluation_at"],
        )

        rollback_replay = store.rollback(
            applied.transaction_id,
            current["revision"],
            "rollback-once",
        )
        self.assertTrue(rollback_replay.success, rollback_replay.to_dict())
        self.assertTrue(rollback_replay.replayed)
        self.assertEqual("replayed", rollback_replay.status)

        old_patch_replay = store.apply_patch(patch)
        self.assertTrue(old_patch_replay.success, old_patch_replay.to_dict())
        self.assertTrue(old_patch_replay.replayed)
        self.assertEqual("replayed_rolled_back", old_patch_replay.status)
        self.assertEqual(restored, store.load_plan())

        stale_distinct = PlanPatch(
            trip_id=initial["trip_id"],
            base_revision=initial["revision"],
            idempotency_key="aba-distinct",
            operations=(
                UpdateActivity(
                    "aba-distinct-note",
                    self.alpha,
                    {"note": "must be stale despite equal state"},
                ),
            ),
        )
        stale = store.apply_patch(stale_distinct)
        self.assertFalse(stale.success)
        self.assert_problem(stale, "STALE_REVISION")

    def test_protected_apply_and_rollback_require_distinct_exact_approvals(
        self,
    ) -> None:
        store, initial = self._migrate()
        protected_patch = self._patch(
            initial,
            UpdateActivity(
                "move-protected-time",
                self.alpha,
                {"time": "09:30"},
            ),
            key="protected-apply",
        )
        preview = store.preview_patch(protected_patch)
        self.assertFalse(preview.success)
        self.assert_problem(preview, "APPROVAL_REQUIRED")
        self.assertIsNotNone(preview.required_approval_scope)
        apply_grant = self._grant(
            preview.required_approval_scope,
            approval_id="approve-protected-apply",
        )

        applied = store.apply_patch(
            protected_patch,
            approvals=(apply_grant,),
        )
        self.assertTrue(applied.success, applied.to_dict())
        self.assertEqual(
            "09:30", _activity(store.load_plan(), self.alpha)["time"]
        )
        current = store.load_plan()

        rollback_preview = store.rollback(
            applied.transaction_id,
            current["revision"],
            "protected-rollback",
        )
        self.assertFalse(rollback_preview.success)
        self.assert_problem(rollback_preview, "APPROVAL_REQUIRED")
        self.assertIsNotNone(rollback_preview.required_approval_scope)
        self.assertEqual(current, store.load_plan())

        wrong_scope = store.rollback(
            applied.transaction_id,
            current["revision"],
            "protected-rollback",
            approvals=(apply_grant,),
        )
        self.assertFalse(wrong_scope.success)
        self.assert_problem(wrong_scope, "APPROVAL_SCOPE_MISMATCH")
        self.assertEqual(current, store.load_plan())

        rollback_grant = self._grant(
            rollback_preview.required_approval_scope,
            approval_id="approve-protected-rollback",
        )
        rolled_back = store.rollback(
            applied.transaction_id,
            current["revision"],
            "protected-rollback",
            approvals=(rollback_grant,),
        )
        self.assertTrue(rolled_back.success, rolled_back.to_dict())
        self.assertEqual("rolled_back", rolled_back.status)
        self.assertNotEqual(
            apply_grant.scope_digest,
            rollback_grant.scope_digest,
        )
        self.assertEqual(
            "09:00", _activity(store.load_plan(), self.alpha)["time"]
        )

    def test_rollback_scope_covers_windows_and_full_day_context(self) -> None:
        store, initial = self._migrate()
        context_patch = self._patch(
            initial,
            UpdateActivity(
                "change-protected-windows",
                self.alpha,
                {
                    "allowed_windows": [
                        {"start": "08:30", "end": "12:00"}
                    ]
                },
            ),
            UpdateDay(
                "change-protected-day",
                self.day_id,
                {
                    "allowed_modes": ["walking", "transit"],
                    "available_end": "21:00",
                    "available_start": "07:30",
                    "date": "2026-10-02",
                    "day": 2,
                    "end_location_id": "location-end",
                    "start_location_id": "location-start",
                    "timezone": "Asia/Tokyo",
                },
            ),
            key="protected-context-apply",
        )
        preview = store.preview_patch(context_patch)
        self.assertFalse(preview.success)
        self.assert_problem(preview, "APPROVAL_REQUIRED")
        applied = store.apply_patch(
            context_patch,
            approvals=(
                self._grant(
                    preview.required_approval_scope,
                    approval_id="approve-protected-context",
                ),
            ),
        )
        self.assertTrue(applied.success, applied.to_dict())

        current = store.load_plan()
        rollback_candidate = deepcopy(current)
        rollback_candidate["state"] = deepcopy(initial["state"])
        protected_fields = {
            change.field
            for change in _rollback_protected_changes(
                current, rollback_candidate
            )
        }
        self.assertTrue(
            {
                "allowed_windows",
                "day.allowed_modes",
                "day.available_end",
                "day.available_start",
                "day.date",
                "day.day",
                "day.end_location_id",
                "day.start_location_id",
                "day.timezone",
            }.issubset(protected_fields),
            protected_fields,
        )

        denied = store.rollback(
            applied.transaction_id,
            current["revision"],
            "protected-context-rollback",
        )
        self.assertFalse(denied.success)
        self.assert_problem(denied, "APPROVAL_REQUIRED")
        rolled = store.rollback(
            applied.transaction_id,
            current["revision"],
            "protected-context-rollback",
            approvals=(
                self._grant(
                    denied.required_approval_scope,
                    approval_id="approve-protected-context-rollback",
                ),
            ),
        )
        self.assertTrue(rolled.success, rolled.to_dict())
        self.assertEqual(initial["state"], store.load_plan()["state"])

    def test_rollback_cannot_remove_a_hard_constraint_without_approval(
        self,
    ) -> None:
        store, initial = self._migrate()
        add_hard = self._patch(
            initial,
            AddConstraint(
                "add-hard-fixed-time",
                "constraint-hard-fixed-time",
                {
                    "kind": "fixed_time",
                    "strength": "hard",
                    "subject_ids": [self.alpha],
                    "params": {"time": "09:00"},
                    "origin": "user-confirmed",
                },
            ),
            key="add-hard-constraint",
        )
        applied = store.apply_patch(add_hard)
        self.assertTrue(applied.success, applied.to_dict())
        current = store.load_plan()

        denied = store.rollback(
            applied.transaction_id,
            current["revision"],
            "rollback-hard-constraint",
        )
        self.assertFalse(denied.success)
        self.assert_problem(denied, "APPROVAL_REQUIRED")
        self.assertEqual(current, store.load_plan())

        approved = store.rollback(
            applied.transaction_id,
            current["revision"],
            "rollback-hard-constraint",
            approvals=(
                self._grant(
                    denied.required_approval_scope,
                    approval_id="approve-hard-constraint-rollback",
                ),
            ),
        )
        self.assertTrue(approved.success, approved.to_dict())
        self.assertEqual(initial["state"], store.load_plan()["state"])

    def test_only_latest_patch_transaction_is_rollbackable(self) -> None:
        store, initial = self._migrate()
        first = store.apply_patch(
            self._note_patch(initial, "first", key="first-transaction")
        )
        self.assertTrue(first.success, first.to_dict())
        after_first = store.load_plan()
        second = store.apply_patch(
            self._note_patch(
                after_first, "second", key="second-transaction"
            )
        )
        self.assertTrue(second.success, second.to_dict())
        current = store.load_plan()

        old = store.rollback(
            first.transaction_id,
            current["revision"],
            "rollback-old",
        )
        self.assertFalse(old.success)
        self.assert_problem(old, "TRANSACTION_NOT_LATEST")

        latest = store.rollback(
            second.transaction_id,
            current["revision"],
            "rollback-latest",
        )
        self.assertTrue(latest.success, latest.to_dict())
        self.assertEqual(after_first["state"], store.load_plan()["state"])

        still_old = store.rollback(
            first.transaction_id,
            store.load_plan()["revision"],
            "rollback-old-after-latest",
        )
        self.assertFalse(still_old.success)
        self.assert_problem(still_old, "TRANSACTION_NOT_LATEST")

    def test_history_tampering_fails_closed(self) -> None:
        store, initial = self._migrate()
        applied = store.apply_patch(
            self._note_patch(initial, "history target", key="history-target")
        )
        self.assertTrue(applied.success, applied.to_dict())
        before = self.plan_path.read_bytes()
        history_path = (
            self.history_dir
            / f"{applied.transaction_id}.plan.json"
        )
        history_path.write_bytes(b"tampered history")

        result = store.rollback(
            applied.transaction_id,
            store.load_plan()["revision"],
            "rollback-tampered-history",
        )

        self.assertFalse(result.success)
        self.assert_problem(result, "HISTORY_INTEGRITY_FAILED")
        self.assertEqual(before, self.plan_path.read_bytes())

    def test_symlink_and_path_guards_reject_unsafe_targets(self) -> None:
        with self.assertRaises(StoreError):
            TripStore(self.trips_root, "../escape")

        linked_trip_slug = "linked-trip"
        outside_trip = self.root / "outside-trip"
        (outside_trip / "data").mkdir(parents=True)
        (self.trips_root / linked_trip_slug).symlink_to(
            outside_trip, target_is_directory=True
        )
        with self.assertRaises(StoreError):
            TripStore(self.trips_root, linked_trip_slug)

        linked_slug = "linked-data"
        linked_trip = self.trips_root / linked_slug
        linked_trip.mkdir()
        outside_data = self.root / "outside-data"
        outside_data.mkdir()
        (linked_trip / "data").symlink_to(outside_data, target_is_directory=True)
        with self.assertRaises(StoreError):
            TripStore(self.trips_root, linked_slug)

        legacy_path = self.data_dir / "trip.json"
        outside_legacy = self.root / "outside-trip.json"
        outside_legacy.write_bytes(legacy_path.read_bytes())
        legacy_path.unlink()
        legacy_path.symlink_to(outside_legacy)
        with self.assertRaises(StoreError):
            TripStore(self.trips_root, self.slug)
        legacy_path.unlink()
        _write_json(legacy_path, self.trip_source)

        store, plan = self._migrate()
        outside_plan = self.root / "outside-plan.json"
        outside_plan.write_bytes(self.plan_path.read_bytes())
        self.plan_path.unlink()
        self.plan_path.symlink_to(outside_plan)
        patch = self._note_patch(plan, "unsafe", key="unsafe-plan-link")

        result = store.apply_patch(patch)

        self.assertFalse(result.success)
        self.assert_problem(result, "UNSAFE_PATH")
        self.assertEqual(encode_plan(plan), outside_plan.read_bytes())

    def test_malformed_plan_domain_value_and_receipt_fail_closed(self) -> None:
        store, plan = self._migrate()
        invalid_domain = self._patch(
            plan,
            AddActivity(
                "invalid-duration",
                "activity-invalid-duration",
                self.day_id,
                {
                    "title": "Invalid duration",
                    "location_id": "location-invalid-duration",
                    "duration_min": "not-a-number",
                },
            ),
            key="invalid-domain",
        )
        before = self.plan_path.read_bytes()
        domain_result = store.apply_patch(invalid_domain)
        self.assertFalse(domain_result.success)
        self.assert_problem(domain_result, "KERNEL_VALIDATION_FAILED")
        self.assertEqual(before, self.plan_path.read_bytes())

        malformed_receipt_plan = store.load_plan()
        malformed_receipt_plan["receipts"]["malformed-receipt"] = "bad"
        self.plan_path.write_bytes(
            canonical_json_bytes(malformed_receipt_plan)
        )
        malformed_receipt_bytes = self.plan_path.read_bytes()
        receipt_patch = self._note_patch(
            malformed_receipt_plan,
            "must not apply",
            key="malformed-receipt",
        )
        receipt_result = store.apply_patch(receipt_patch)
        self.assertFalse(receipt_result.success)
        self.assert_problem(receipt_result, "MALFORMED_RECEIPT")
        self.assertEqual(malformed_receipt_bytes, self.plan_path.read_bytes())

        self.plan_path.write_bytes(b'{"state":')
        malformed_bytes = self.plan_path.read_bytes()
        malformed_result = store.apply_patch(receipt_patch)
        self.assertFalse(malformed_result.success)
        self.assert_problem(malformed_result, "INVALID_JSON")
        self.assertEqual(malformed_bytes, self.plan_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
