"""Offline Phase 5.33 migrated-baseline adoption regressions."""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trip_planner.baseline_adoption import (
    MigratedBaselineAdoptionError,
    MigratedBaselineAdoptionStager,
    prepare_migrated_baseline_classification_review,
)
from trip_planner.codec import (
    build_plan,
    compute_revision,
    encode_plan,
)
from trip_planner.guided_canonical_apply import (
    GuidedCanonicalApplyResponseKind,
    capture_guided_canonical_apply_response,
    execute_guided_canonical_apply_response,
    prepare_guided_canonical_baseline_adoption_review,
)
from trip_planner.mutations import (
    ApprovalGrant,
    MigratedActivityClassification,
    MigratedActivityClassificationKind,
)
from trip_planner.store import TripStore


NOW = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
PRIVATE_TITLE = "private migrated activity title"


def _plan(*, missing_second_time: bool = False) -> dict[str, object]:
    second = {
        "activity_id": "activity-beta",
        "title": "Beta",
        "location_id": "location-beta",
        "duration_min": 30,
        "note": "preserve beta note",
    }
    if not missing_second_time:
        second["time"] = "11:00"
    return build_plan(
        trip_id="baseline-fixture",
        generation=1,
        state={
            "trip": {
                "slug": "baseline-fixture",
                "title": "Baseline fixture",
                "timezone": "Asia/Taipei",
                "date_range": "2026-10-01 ~ 2026-10-01",
                "cities": ["Fixture"],
                "constraints": [],
                "_trip_planner": {
                    "migration": {
                        "source_schema": "legacy-v1",
                        "source_revision": "d" * 64,
                        "protected_activity_ids": [
                            "activity-alpha",
                            "activity-beta",
                        ],
                        "ignored_travel_edges": [
                            {
                                "day_id": "day-1",
                                "index": 3,
                                "edge": {"from": 7, "to": 8},
                            }
                        ],
                    }
                },
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
                        "start_location_id": "location-alpha",
                        "end_location_id": "location-beta",
                        "allowed_modes": ["walking"],
                        "places": [
                            {
                                "activity_id": "activity-alpha",
                                "title": PRIVATE_TITLE,
                                "location_id": "location-alpha",
                                "time": "09:00",
                                "duration_min": 30,
                                "note": "preserve alpha note",
                                "evidence_state": "stale",
                            },
                            second,
                        ],
                        "travel": [
                            {
                                "from_activity_id": "activity-alpha",
                                "to_activity_id": "activity-beta",
                                "recommended_mode": "walking",
                                "modes": {
                                    "walking": {
                                        "duration_min": 15,
                                        "evidence_state": "unverified",
                                    }
                                },
                            }
                        ],
                    }
                ],
            },
        },
    )


class Phase533MigratedBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = self._store(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _store(
        self,
        root: Path,
        *,
        fault_hook=None,
    ) -> TripStore:
        data_dir = root / "baseline-fixture" / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "plan.json").write_bytes(encode_plan(_plan()))
        return TripStore(
            root,
            "baseline-fixture",
            fault_hook=fault_hook,
        )

    def _prepare(self, store: TripStore | None = None):
        target = store or self.store
        classification_review = (
            prepare_migrated_baseline_classification_review(
                target,
                reviewed_at=NOW,
            )
        )
        stager = MigratedBaselineAdoptionStager(
            target,
            run_id="baseline-adoption-test",
        )
        classifications = (
            MigratedActivityClassification(
                "activity-alpha",
                MigratedActivityClassificationKind.MOVABLE,
            ),
            MigratedActivityClassification(
                "activity-beta",
                MigratedActivityClassificationKind.BOOKED,
            ),
        )
        adoption_review = stager.classify(
            classification_review,
            classifications,
            classified_at=NOW + timedelta(seconds=1),
            idempotency_key="adopt-baseline-once",
        )
        guided_review = prepare_guided_canonical_baseline_adoption_review(
            target,
            stager,
            adoption_review,
            evaluation_at=NOW + timedelta(seconds=2),
        )
        return (
            classification_review,
            stager,
            adoption_review,
            guided_review,
        )

    @staticmethod
    def _approval(scope: str) -> ApprovalGrant:
        return ApprovalGrant(
            approval_id="external-baseline-approval",
            scope_digest=scope,
            approved_by="fixture-human",
            approved_at=(NOW + timedelta(seconds=3)).isoformat(),
        )

    def test_exact_review_classify_accept_and_separate_approval(self) -> None:
        before = self.store.plan_path.read_bytes()
        original = deepcopy(self.store.load_plan())
        classification, stager, adoption, guided = self._prepare()

        safe = json.dumps(classification.to_dict(), sort_keys=True)
        private = json.dumps(
            classification.to_ephemeral_private_review_payload(),
            sort_keys=True,
        )
        self.assertNotIn(PRIVATE_TITLE, safe)
        self.assertIn(PRIVATE_TITLE, private)
        self.assertNotIn(PRIVATE_TITLE, json.dumps(guided.to_dict()))
        classification_private = (
            classification.to_ephemeral_private_review_payload()
        )
        self.assertEqual(
            "private_ephemeral_direct_human_review_only",
            classification_private["payload_handling"],
        )
        self.assertEqual(1, classification_private["activities"][0]["day_number"])
        self.assertEqual(
            "2026-10-01",
            classification_private["activities"][0]["day_date"],
        )
        final_private = adoption.to_ephemeral_private_review_payload()
        self.assertEqual(
            "private_ephemeral_direct_human_review_only",
            final_private["payload_handling"],
        )
        self.assertEqual(
            ["movable", "booked"],
            [
                item["classification_kind"]
                for item in final_private["activities"]
            ],
        )
        self.assertEqual(
            [("selected", "movable"), ("booked", "fixed_time")],
            [
                (
                    item["resulting_decision_state"],
                    item["resulting_flexibility"],
                )
                for item in final_private["activities"]
            ],
        )
        direct = self.store.apply_patch(adoption.patch)
        self.assertFalse(direct.success)
        self.assertEqual(before, self.store.plan_path.read_bytes())

        with self.assertRaises(TypeError):
            capture_guided_canonical_apply_response(
                guided,
                "accept_apply",  # type: ignore[arg-type]
                evaluation_at=NOW + timedelta(seconds=3),
            )
        response = capture_guided_canonical_apply_response(
            guided,
            GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
            evaluation_at=NOW + timedelta(seconds=3),
        )
        self.assertEqual(before, self.store.plan_path.read_bytes())

        waiting = execute_guided_canonical_apply_response(
            guided,
            response,
            self.store,
            evaluation_at=NOW + timedelta(seconds=4),
        ).to_dict()
        self.assertEqual("waiting_approval", waiting["status"])
        self.assertTrue(stager.has_pending_review)
        self.assertEqual(before, self.store.plan_path.read_bytes())

        wrong_approval = ApprovalGrant(
            approval_id="wrong-baseline-approval",
            scope_digest="sha256:" + "0" * 64,
            approved_by="fixture-human",
            approved_at=(NOW + timedelta(seconds=4)).isoformat(),
        )
        mismatch = execute_guided_canonical_apply_response(
            guided,
            response,
            self.store,
            evaluation_at=NOW + timedelta(seconds=5),
            approvals=(wrong_approval,),
        ).to_dict()
        self.assertEqual("waiting_approval", mismatch["status"])
        self.assertTrue(stager.has_pending_review)
        self.assertEqual(before, self.store.plan_path.read_bytes())

        outcome = execute_guided_canonical_apply_response(
            guided,
            response,
            self.store,
            evaluation_at=NOW + timedelta(seconds=6),
            approvals=(self._approval(adoption.required_approval_scope),),
        ).to_dict()
        self.assertEqual("applied", outcome["status"])
        self.assertEqual("continue_planning", outcome["next_action"])
        self.assertTrue(outcome["canonical_write_performed"])
        current = self.store.load_plan()
        migration = current["state"]["trip"]["_trip_planner"]["migration"]
        original_migration = original["state"]["trip"]["_trip_planner"][
            "migration"
        ]
        self.assertEqual([], migration["protected_activity_ids"])
        for field in (
            "source_schema",
            "source_revision",
            "ignored_travel_edges",
        ):
            self.assertEqual(original_migration[field], migration[field])
        activities = current["state"]["itinerary"]["days"][0]["places"]
        self.assertEqual(("selected", "movable"), (
            activities[0]["decision_state"],
            activities[0]["flexibility"],
        ))
        self.assertEqual(("booked", "fixed_time"), (
            activities[1]["decision_state"],
            activities[1]["flexibility"],
        ))
        for index, activity in enumerate(activities):
            original_activity = original["state"]["itinerary"]["days"][0][
                "places"
            ][index]
            for field in (
                "time",
                "duration_min",
                "location_id",
                "note",
                "evidence_state",
            ):
                self.assertEqual(
                    original_activity.get(field),
                    activity.get(field),
                )
        self.assertEqual(
            original["state"]["itinerary"]["days"][0]["travel"],
            current["state"]["itinerary"]["days"][0]["travel"],
        )
        self.assertEqual(1, len(current["receipts"]))
        with self.assertRaises(MigratedBaselineAdoptionError) as raised:
            prepare_migrated_baseline_classification_review(
                self.store,
                reviewed_at=NOW + timedelta(seconds=7),
            )
        self.assertEqual(
            "MIGRATED_BASELINE_ALREADY_ADOPTED",
            raised.exception.code,
        )

    def test_classification_context_store_and_clock_fail_closed(self) -> None:
        review = prepare_migrated_baseline_classification_review(
            self.store,
            reviewed_at=NOW,
        )
        stager = MigratedBaselineAdoptionStager(
            self.store,
            run_id="classification-failures",
        )
        partial = (
            MigratedActivityClassification(
                "activity-alpha",
                MigratedActivityClassificationKind.MOVABLE,
            ),
        )
        with self.assertRaises(MigratedBaselineAdoptionError) as raised:
            stager.classify(
                review,
                partial,
                classified_at=NOW + timedelta(seconds=1),
                idempotency_key="partial",
            )
        self.assertEqual("CLASSIFICATION_CONTEXT_MISMATCH", raised.exception.code)

        extra_values = (
            MigratedActivityClassification(
                "activity-alpha",
                MigratedActivityClassificationKind.MOVABLE,
            ),
            MigratedActivityClassification(
                "activity-beta",
                MigratedActivityClassificationKind.BOOKED,
            ),
            MigratedActivityClassification(
                "unexpected-activity",
                MigratedActivityClassificationKind.MOVABLE,
            ),
        )
        with self.assertRaises(MigratedBaselineAdoptionError) as raised:
            stager.classify(
                review,
                extra_values,
                classified_at=NOW + timedelta(seconds=1),
                idempotency_key="extra-classification",
            )
        self.assertEqual("CLASSIFICATION_CONTEXT_MISMATCH", raised.exception.code)

        reversed_values = (
            MigratedActivityClassification(
                "activity-beta",
                MigratedActivityClassificationKind.BOOKED,
            ),
            MigratedActivityClassification(
                "activity-alpha",
                MigratedActivityClassificationKind.MOVABLE,
            ),
        )
        with self.assertRaises(MigratedBaselineAdoptionError) as raised:
            stager.classify(
                review,
                reversed_values,
                classified_at=NOW + timedelta(seconds=1),
                idempotency_key="wrong-order",
            )
        self.assertEqual("CLASSIFICATION_CONTEXT_MISMATCH", raised.exception.code)

        other_root = self.root / "other-root"
        other_store = self._store(other_root)
        wrong_stager = MigratedBaselineAdoptionStager(
            other_store,
            run_id="wrong-root",
        )
        with self.assertRaises(MigratedBaselineAdoptionError) as raised:
            wrong_stager.classify(
                review,
                (
                    MigratedActivityClassification(
                        "activity-alpha",
                        MigratedActivityClassificationKind.MOVABLE,
                    ),
                    MigratedActivityClassification(
                        "activity-beta",
                        MigratedActivityClassificationKind.BOOKED,
                    ),
                ),
                classified_at=NOW + timedelta(seconds=1),
                idempotency_key="wrong-root",
            )
        self.assertEqual("BASELINE_STORE_MISMATCH", raised.exception.code)

        with self.assertRaises(MigratedBaselineAdoptionError) as raised:
            stager.classify(
                review,
                (
                    MigratedActivityClassification(
                        "activity-alpha",
                        MigratedActivityClassificationKind.MOVABLE,
                    ),
                    MigratedActivityClassification(
                        "activity-beta",
                        MigratedActivityClassificationKind.BOOKED,
                    ),
                ),
                classified_at=review.expires_at + timedelta(seconds=1),
                idempotency_key="expired",
            )
        self.assertEqual("CLASSIFICATION_REVIEW_EXPIRED", raised.exception.code)
        with self.assertRaises(MigratedBaselineAdoptionError) as raised:
            stager.classify(
                review,
                (
                    MigratedActivityClassification(
                        "activity-alpha",
                        MigratedActivityClassificationKind.MOVABLE,
                    ),
                    MigratedActivityClassification(
                        "activity-beta",
                        MigratedActivityClassificationKind.BOOKED,
                    ),
                ),
                classified_at=review.expires_at,
                idempotency_key="exact-expiry",
            )
        self.assertEqual("CLASSIFICATION_REVIEW_EXPIRED", raised.exception.code)

        changed = self.store.load_plan()
        changed["state"]["trip"]["subtitle"] = "external revision"
        changed["generation"] += 1
        changed["revision"] = compute_revision(changed)
        self.store.plan_path.write_bytes(encode_plan(changed))
        with self.assertRaises(MigratedBaselineAdoptionError) as raised:
            stager.classify(
                review,
                (
                    MigratedActivityClassification(
                        "activity-alpha",
                        MigratedActivityClassificationKind.MOVABLE,
                    ),
                    MigratedActivityClassification(
                        "activity-beta",
                        MigratedActivityClassificationKind.BOOKED,
                    ),
                ),
                classified_at=NOW + timedelta(seconds=2),
                idempotency_key="stale",
            )
        self.assertEqual("BASELINE_CONTEXT_CHANGED", raised.exception.code)

        expiry_root = self.root / "guided-expiry"
        expiry_store = self._store(expiry_root)
        _classification, _stager, _adoption, guided = self._prepare(
            expiry_store
        )
        before = expiry_store.plan_path.read_bytes()
        with self.assertRaises(ValueError):
            capture_guided_canonical_apply_response(
                guided,
                GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
                evaluation_at=guided.expires_at + timedelta(seconds=1),
            )
        self.assertEqual(before, expiry_store.plan_path.read_bytes())

    def test_foreign_schema_and_inactive_activity_fail_closed(self) -> None:
        foreign_root = self.root / "foreign-schema"
        foreign_store = self._store(foreign_root)
        foreign = foreign_store.load_plan()
        foreign["state"]["trip"]["_trip_planner"]["migration"][
            "source_schema"
        ] = "future-v2"
        foreign["generation"] += 1
        foreign["revision"] = compute_revision(foreign)
        foreign_store.plan_path.write_bytes(encode_plan(foreign))
        with self.assertRaises(MigratedBaselineAdoptionError) as raised:
            prepare_migrated_baseline_classification_review(
                foreign_store,
                reviewed_at=NOW,
            )
        self.assertEqual(
            "MIGRATED_BASELINE_SOURCE_SCHEMA_UNSUPPORTED",
            raised.exception.code,
        )

        inactive_root = self.root / "inactive-activity"
        inactive_store = self._store(inactive_root)
        inactive = inactive_store.load_plan()
        inactive["state"]["itinerary"]["days"][0]["places"][0][
            "decision_state"
        ] = "cancelled"
        inactive["state"]["itinerary"]["days"][0]["places"][0][
            "flexibility"
        ] = "movable"
        inactive["generation"] += 1
        inactive["revision"] = compute_revision(inactive)
        inactive_store.plan_path.write_bytes(encode_plan(inactive))
        classification = prepare_migrated_baseline_classification_review(
            inactive_store,
            reviewed_at=NOW,
        )
        stager = MigratedBaselineAdoptionStager(
            inactive_store,
            run_id="inactive-baseline",
        )
        with self.assertRaises(MigratedBaselineAdoptionError) as raised:
            stager.classify(
                classification,
                (
                    MigratedActivityClassification(
                        "activity-alpha",
                        MigratedActivityClassificationKind.MOVABLE,
                    ),
                    MigratedActivityClassification(
                        "activity-beta",
                        MigratedActivityClassificationKind.BOOKED,
                    ),
                ),
                classified_at=NOW + timedelta(seconds=1),
                idempotency_key="inactive-baseline",
            )
        self.assertEqual(
            "MIGRATED_BASELINE_REACTIVATION_FORBIDDEN",
            raised.exception.code,
        )

    def test_request_changes_and_cancel_perform_zero_writes(self) -> None:
        for index, kind in enumerate(
            (
                GuidedCanonicalApplyResponseKind.REQUEST_CHANGES,
                GuidedCanonicalApplyResponseKind.CANCEL,
            )
        ):
            with self.subTest(kind=kind.value):
                root = self.root / f"decision-{index}"
                store = self._store(root)
                before = store.plan_path.read_bytes()
                _classification, stager, _adoption, guided = self._prepare(store)
                response = capture_guided_canonical_apply_response(
                    guided,
                    kind,
                    evaluation_at=NOW + timedelta(seconds=3),
                )
                outcome = execute_guided_canonical_apply_response(
                    guided,
                    response,
                    store,
                    evaluation_at=NOW + timedelta(seconds=4),
                ).to_dict()
                self.assertIn(outcome["status"], {"changes_requested", "cancelled"})
                self.assertFalse(outcome["canonical_write_performed"])
                self.assertFalse(stager.has_pending_review)
                self.assertEqual(before, store.plan_path.read_bytes())

    def test_lost_ack_reconciles_one_exact_receipt(self) -> None:
        fault_count = 0

        def fault(stage: str) -> None:
            nonlocal fault_count
            if stage == "after_replace" and fault_count == 0:
                fault_count += 1
                raise RuntimeError("canned lost acknowledgement")

        root = self.root / "lost-ack"
        store = self._store(root, fault_hook=fault)
        _classification, stager, adoption, guided = self._prepare(store)
        response = capture_guided_canonical_apply_response(
            guided,
            GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
            evaluation_at=NOW + timedelta(seconds=3),
        )
        approval = self._approval(adoption.required_approval_scope)
        unknown = execute_guided_canonical_apply_response(
            guided,
            response,
            store,
            evaluation_at=NOW + timedelta(seconds=4),
            approvals=(approval,),
        ).to_dict()
        self.assertEqual("outcome_unknown", unknown["status"])
        self.assertEqual("retry_exact_apply", unknown["next_action"])
        self.assertIsNone(unknown["canonical_write_performed"])
        self.assertTrue(stager.has_pending_review)

        replay = execute_guided_canonical_apply_response(
            guided,
            response,
            store,
            evaluation_at=NOW + timedelta(seconds=5),
            approvals=(approval,),
        ).to_dict()
        self.assertEqual("replay_confirmed", replay["status"])
        self.assertFalse(replay["canonical_write_performed"])
        self.assertFalse(stager.has_pending_review)
        self.assertEqual(1, fault_count)
        self.assertEqual(1, len(store.load_plan()["receipts"]))

    def test_lost_ack_then_rollback_is_not_reported_as_applied(self) -> None:
        fault_count = 0

        def fault(stage: str) -> None:
            nonlocal fault_count
            if stage == "after_replace" and fault_count == 0:
                fault_count += 1
                raise RuntimeError("canned lost acknowledgement")

        root = self.root / "lost-ack-rollback"
        store = self._store(root, fault_hook=fault)
        _classification, stager, adoption, guided = self._prepare(store)
        response = capture_guided_canonical_apply_response(
            guided,
            GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
            evaluation_at=NOW + timedelta(seconds=3),
        )
        approval = self._approval(adoption.required_approval_scope)
        unknown = execute_guided_canonical_apply_response(
            guided,
            response,
            store,
            evaluation_at=NOW + timedelta(seconds=4),
            approvals=(approval,),
        ).to_dict()
        self.assertEqual("outcome_unknown", unknown["status"])
        self.assertTrue(stager.has_pending_review)

        current = store.load_plan()
        receipt = current["receipts"]["adopt-baseline-once"]
        rollback_preview = store.rollback(
            receipt["transaction_id"],
            current["revision"],
            "rollback-baseline-adoption",
            evaluation_at=NOW + timedelta(seconds=5),
        )
        self.assertFalse(rollback_preview.success)
        self.assertIsNotNone(rollback_preview.required_approval_scope)
        rollback_approval = ApprovalGrant(
            approval_id="external-baseline-rollback-approval",
            scope_digest=rollback_preview.required_approval_scope or "",
            approved_by="fixture-human",
            approved_at=(NOW + timedelta(seconds=6)).isoformat(),
        )
        rolled_back = store.rollback(
            receipt["transaction_id"],
            current["revision"],
            "rollback-baseline-adoption",
            approvals=(rollback_approval,),
            evaluation_at=NOW + timedelta(seconds=7),
        )
        self.assertTrue(rolled_back.success, rolled_back.to_dict())
        self.assertEqual("rolled_back", rolled_back.status)

        outcome = execute_guided_canonical_apply_response(
            guided,
            response,
            store,
            evaluation_at=NOW + timedelta(seconds=8),
            approvals=(approval,),
        ).to_dict()
        self.assertEqual("rolled_back", outcome["status"])
        self.assertEqual("prepare_fresh_review", outcome["next_action"])
        self.assertFalse(outcome["result"]["applied"])
        self.assertFalse(outcome["canonical_write_performed"])
        self.assertIn(
            "BASELINE_ADOPTION_ROLLED_BACK",
            outcome["result"]["problem_codes"],
        )
        self.assertFalse(stager.has_pending_review)


if __name__ == "__main__":
    unittest.main()
