"""Phase 5.33 host-only tripctl migrated-baseline product facade."""

from __future__ import annotations

import json
import pickle
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import trip_planner
from tests.test_phase533_migrated_baseline import NOW, PRIVATE_TITLE, _plan
from trip_planner.codec import encode_plan
from trip_planner.mutations import (
    ApprovalGrant,
    MigratedActivityClassification,
    MigratedActivityClassificationKind,
)
from trip_planner.store import TripStore
from trip_planner.tripctl import (
    GuidedCanonicalApplyResponseKind,
    TripctlApplyResponseError,
    TripctlApplyReviewError,
    TripctlBaselineApplyOutcome,
    TripctlBaselineApplyResponse,
    TripctlBaselineApplyReview,
    TripctlBaselineClassificationReview,
    capture_trip_baseline_apply_response,
    classify_trip_migrated_baseline,
    execute_trip_baseline_apply_response,
    prepare_trip_baseline_classification_review,
)


class Phase533TripctlBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "trips"
        self.store = self._store(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _store(root: Path, *, fault_hook=None) -> TripStore:
        data_dir = root / "baseline-fixture" / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "plan.json").write_bytes(encode_plan(_plan()))
        return TripStore(
            root,
            "baseline-fixture",
            fault_hook=fault_hook,
        )

    @staticmethod
    def _classifications():
        return (
            MigratedActivityClassification(
                "activity-alpha",
                MigratedActivityClassificationKind.MOVABLE,
            ),
            MigratedActivityClassification(
                "activity-beta",
                MigratedActivityClassificationKind.BOOKED,
            ),
        )

    def _prepare(self, store: TripStore | None = None, *, run_id="facade"):
        target = store or self.store
        classification_review = prepare_trip_baseline_classification_review(
            target,
            reviewed_at=NOW,
            run_id=run_id,
        )
        apply_review = classify_trip_migrated_baseline(
            classification_review,
            self._classifications(),
            classified_at=NOW + timedelta(seconds=1),
            idempotency_key=f"adopt-{run_id}",
        )
        return classification_review, apply_review

    @staticmethod
    def _approval(scope: str, *, approval_id="baseline-approval"):
        return ApprovalGrant(
            approval_id=approval_id,
            scope_digest=scope,
            approved_by="fixture-human",
            approved_at=(NOW + timedelta(seconds=3)).isoformat(),
        )

    def test_prepare_and_classify_are_safe_private_and_zero_write(self) -> None:
        before = self.store.plan_path.read_bytes()
        classification_review = prepare_trip_baseline_classification_review(
            self.store,
            reviewed_at=NOW,
            run_id="safe-private",
        )

        self.assertIs(
            trip_planner.prepare_trip_baseline_classification_review,
            prepare_trip_baseline_classification_review,
        )
        self.assertIsInstance(
            classification_review,
            TripctlBaselineClassificationReview,
        )
        safe = classification_review.to_dict()
        private = (
            classification_review.to_ephemeral_private_review_payload()
        )
        safe_text = json.dumps(safe, ensure_ascii=False, sort_keys=True)
        private_text = json.dumps(private, ensure_ascii=False, sort_keys=True)
        self.assertEqual("apply", safe["command"])
        self.assertEqual("review_required", safe["status"])
        self.assertEqual("adopt_migrated_baseline", safe["result"]["action_kind"])
        self.assertNotIn(PRIVATE_TITLE, safe_text)
        self.assertNotIn("activity-alpha", safe_text)
        self.assertIn(PRIVATE_TITLE, private_text)
        self.assertIn("2026-10-01", private_text)
        self.assertEqual(
            "private_ephemeral_direct_human_review_only",
            private["payload_handling"],
        )
        with self.assertRaises(TypeError):
            pickle.dumps(classification_review)
        self.assertEqual(before, self.store.plan_path.read_bytes())

        apply_review = classify_trip_migrated_baseline(
            classification_review,
            self._classifications(),
            classified_at=NOW + timedelta(seconds=1),
            idempotency_key="safe-private-adoption",
        )
        self.assertIs(
            trip_planner.classify_trip_migrated_baseline,
            classify_trip_migrated_baseline,
        )
        self.assertIsInstance(apply_review, TripctlBaselineApplyReview)
        apply_safe = apply_review.to_dict()
        apply_private = apply_review.to_ephemeral_private_review_payload()
        apply_safe_text = json.dumps(
            apply_safe,
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertEqual("review_required", apply_safe["status"])
        self.assertEqual(2, apply_safe["result"]["classification_count"])
        self.assertNotIn(PRIVATE_TITLE, apply_safe_text)
        self.assertNotIn("activity-alpha", apply_safe_text)
        choices = apply_private["apply_review"]["activities"]
        self.assertEqual(
            [("selected", "movable"), ("booked", "fixed_time")],
            [
                (
                    item["resulting_decision_state"],
                    item["resulting_flexibility"],
                )
                for item in choices
            ],
        )
        with self.assertRaises(TypeError):
            pickle.dumps(apply_review)
        self.assertEqual(before, self.store.plan_path.read_bytes())

    def test_classification_order_expiry_and_clock_fail_closed(self) -> None:
        review = prepare_trip_baseline_classification_review(
            self.store,
            reviewed_at=NOW,
            run_id="classification-failures",
        )
        partial = self._classifications()[:1]
        with self.assertRaises(TripctlApplyReviewError) as raised:
            classify_trip_migrated_baseline(
                review,
                partial,
                classified_at=NOW + timedelta(seconds=2),
                idempotency_key="partial",
            )
        self.assertEqual("CLASSIFICATION_CONTEXT_MISMATCH", raised.exception.code)
        self.assertEqual({}, self.store.load_plan()["receipts"])

        with self.assertRaises(TripctlApplyReviewError) as rollback:
            classify_trip_migrated_baseline(
                review,
                partial,
                classified_at=NOW + timedelta(seconds=1),
                idempotency_key="clock-rollback",
            )
        self.assertEqual(
            "BASELINE_CLASSIFICATION_CLOCK_ROLLBACK",
            rollback.exception.code,
        )

        reversed_values = tuple(reversed(self._classifications()))
        with self.assertRaises(TripctlApplyReviewError) as reversed_error:
            classify_trip_migrated_baseline(
                review,
                reversed_values,
                classified_at=NOW + timedelta(seconds=3),
                idempotency_key="wrong-order",
            )
        self.assertEqual(
            "CLASSIFICATION_CONTEXT_MISMATCH",
            reversed_error.exception.code,
        )

        extra = (
            *self._classifications(),
            MigratedActivityClassification(
                "unexpected-activity",
                MigratedActivityClassificationKind.MOVABLE,
            ),
        )
        with self.assertRaises(TripctlApplyReviewError) as extra_error:
            classify_trip_migrated_baseline(
                review,
                extra,
                classified_at=NOW + timedelta(seconds=4),
                idempotency_key="extra-classification",
            )
        self.assertEqual(
            "CLASSIFICATION_CONTEXT_MISMATCH",
            extra_error.exception.code,
        )

        expiry_root = self.root / "expiry"
        expiry_store = self._store(expiry_root)
        expiry_review = prepare_trip_baseline_classification_review(
            expiry_store,
            reviewed_at=NOW,
            run_id="exact-expiry",
        )
        with self.assertRaises(TripctlApplyReviewError) as expired:
            classify_trip_migrated_baseline(
                expiry_review,
                self._classifications(),
                classified_at=expiry_review.expires_at,
                idempotency_key="exact-expiry",
            )
        self.assertEqual("CLASSIFICATION_REVIEW_EXPIRED", expired.exception.code)
        self.assertEqual({}, expiry_store.load_plan()["receipts"])

    def test_exact_response_retains_approval_then_applies(self) -> None:
        _classification_review, apply_review = self._prepare(
            run_id="approval-resume"
        )
        before = self.store.plan_path.read_bytes()
        with self.assertRaises(TripctlApplyResponseError) as generic:
            capture_trip_baseline_apply_response(
                apply_review,
                "accept_apply",  # type: ignore[arg-type]
                evaluation_at=NOW + timedelta(seconds=2),
            )
        self.assertEqual("INVALID_APPLY_RESPONSE_KIND", generic.exception.code)

        response = capture_trip_baseline_apply_response(
            apply_review,
            GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
            evaluation_at=NOW + timedelta(seconds=2),
        )
        self.assertIs(
            trip_planner.capture_trip_baseline_apply_response,
            capture_trip_baseline_apply_response,
        )
        self.assertIsInstance(response, TripctlBaselineApplyResponse)
        self.assertFalse(response.terminal)
        self.assertEqual("response_captured", response.to_dict()["status"])
        with self.assertRaises(TypeError):
            pickle.dumps(response)
        self.assertEqual(before, self.store.plan_path.read_bytes())

        waiting = execute_trip_baseline_apply_response(
            apply_review,
            response,
            self.store,
            evaluation_at=NOW + timedelta(seconds=3),
        )
        waiting_safe = waiting.to_dict()
        self.assertEqual("waiting_approval", waiting_safe["status"])
        self.assertEqual("obtain_exact_authority", waiting_safe["next_action"])
        self.assertTrue(waiting_safe["requires_user_review"])
        self.assertFalse(waiting_safe["retryable"])
        self.assertTrue(waiting_safe["pending_review_retained"])
        self.assertFalse(response.terminal)
        self.assertEqual(before, self.store.plan_path.read_bytes())

        wrong = self._approval(
            "sha256:" + "0" * 64,
            approval_id="wrong-baseline-approval",
        )
        mismatch = execute_trip_baseline_apply_response(
            apply_review,
            response,
            self.store,
            evaluation_at=NOW + timedelta(seconds=4),
            approvals=(wrong,),
        ).to_dict()
        self.assertEqual("waiting_approval", mismatch["status"])
        self.assertTrue(mismatch["pending_review_retained"])
        self.assertFalse(response.terminal)

        outcome = execute_trip_baseline_apply_response(
            apply_review,
            response,
            self.store,
            evaluation_at=NOW + timedelta(seconds=5),
            approvals=(self._approval(apply_review.required_approval_scope),),
        )
        self.assertIs(
            trip_planner.execute_trip_baseline_apply_response,
            execute_trip_baseline_apply_response,
        )
        self.assertIsInstance(outcome, TripctlBaselineApplyOutcome)
        safe = outcome.to_dict()
        self.assertEqual("applied", safe["status"])
        self.assertEqual("continue_planning", safe["next_action"])
        self.assertTrue(safe["result"]["applied"])
        self.assertTrue(safe["result"]["canonical_write_performed"])
        self.assertFalse(safe["result"]["evidence_state_changed"])
        self.assertFalse(safe["result"]["travel_ready"])
        self.assertFalse(safe["pending_review_retained"])
        self.assertTrue(response.terminal)
        with self.assertRaises(TypeError):
            pickle.dumps(outcome)
        rendered = json.dumps(safe, ensure_ascii=False, sort_keys=True)
        for private_value in (
            PRIVATE_TITLE,
            "activity-alpha",
            "day-1",
            "2026-10-01",
            "09:00",
        ):
            self.assertNotIn(private_value, rendered)
        self.assertEqual(1, len(self.store.load_plan()["receipts"]))

    def test_request_changes_and_cancel_are_terminal_zero_write(self) -> None:
        for index, (kind, status) in enumerate(
            (
                (
                    GuidedCanonicalApplyResponseKind.REQUEST_CHANGES,
                    "changes_requested",
                ),
                (GuidedCanonicalApplyResponseKind.CANCEL, "cancelled"),
            )
        ):
            with self.subTest(kind=kind.value):
                root = self.root / f"decision-{index}"
                store = self._store(root)
                _classification, apply_review = self._prepare(
                    store,
                    run_id=f"decision-{index}",
                )
                before = store.plan_path.read_bytes()
                response = capture_trip_baseline_apply_response(
                    apply_review,
                    kind,
                    evaluation_at=NOW + timedelta(seconds=2),
                )
                outcome = execute_trip_baseline_apply_response(
                    apply_review,
                    response,
                    store,
                    evaluation_at=NOW + timedelta(seconds=3),
                ).to_dict()
                self.assertEqual(status, outcome["status"])
                self.assertEqual(
                    (
                        "prepare_fresh_baseline_classification_review"
                        if status == "changes_requested"
                        else "stop_canonical_apply"
                    ),
                    outcome["next_action"],
                )
                self.assertFalse(
                    outcome["result"]["canonical_write_performed"]
                )
                self.assertFalse(outcome["pending_review_retained"])
                self.assertTrue(response.terminal)
                self.assertEqual(before, store.plan_path.read_bytes())

    def test_execution_identity_clock_and_expiry_fail_closed(self) -> None:
        _classification, review = self._prepare(run_id="binding-a")
        response = capture_trip_baseline_apply_response(
            review,
            GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
            evaluation_at=NOW + timedelta(seconds=2),
        )
        other_root = self.root / "binding-other-review"
        other_store = self._store(other_root)
        _other_classification, other_review = self._prepare(
            other_store,
            run_id="binding-b",
        )
        before = self.store.plan_path.read_bytes()

        with self.assertRaises(TripctlApplyResponseError) as mismatch:
            execute_trip_baseline_apply_response(
                other_review,
                response,
                other_store,
                evaluation_at=NOW + timedelta(seconds=3),
            )
        self.assertEqual(
            "APPLY_RESPONSE_REVIEW_MISMATCH",
            mismatch.exception.code,
        )
        with self.assertRaises(TripctlApplyResponseError) as clock:
            execute_trip_baseline_apply_response(
                review,
                response,
                self.store,
                evaluation_at=response.captured_at - timedelta(microseconds=1),
            )
        self.assertEqual("APPLY_EXECUTION_CLOCK_ROLLBACK", clock.exception.code)
        with self.assertRaises(TripctlApplyResponseError) as wrong_store:
            execute_trip_baseline_apply_response(
                review,
                response,
                other_store,
                evaluation_at=NOW + timedelta(seconds=3),
            )
        self.assertEqual("APPLY_STORE_MISMATCH", wrong_store.exception.code)
        self.assertFalse(response.terminal)
        self.assertEqual(before, self.store.plan_path.read_bytes())

        expiry_root = self.root / "execution-expiry"
        expiry_store = self._store(expiry_root)
        _expiry_classification, expiry_review = self._prepare(
            expiry_store,
            run_id="execution-expiry",
        )
        expiry_response = capture_trip_baseline_apply_response(
            expiry_review,
            GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
            evaluation_at=NOW + timedelta(seconds=2),
        )
        with self.assertRaises(TripctlApplyResponseError) as expired:
            execute_trip_baseline_apply_response(
                expiry_review,
                expiry_response,
                expiry_store,
                evaluation_at=expiry_review.expires_at
                + timedelta(microseconds=1),
            )
        self.assertEqual("APPLY_REVIEW_EXPIRED", expired.exception.code)
        self.assertTrue(expiry_response.terminal)
        self.assertEqual({}, expiry_store.load_plan()["receipts"])

    def test_lost_ack_exact_retry_and_rolled_back_receipt_are_truthful(
        self,
    ) -> None:
        fault_count = 0

        def fault(stage: str) -> None:
            nonlocal fault_count
            if stage == "after_replace" and fault_count == 0:
                fault_count += 1
                raise RuntimeError("canned lost acknowledgement")

        root = self.root / "lost-ack"
        store = self._store(root, fault_hook=fault)
        _classification, review = self._prepare(store, run_id="lost-ack")
        response = capture_trip_baseline_apply_response(
            review,
            GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
            evaluation_at=NOW + timedelta(seconds=2),
        )
        approval = self._approval(review.required_approval_scope)
        unknown = execute_trip_baseline_apply_response(
            review,
            response,
            store,
            evaluation_at=NOW + timedelta(seconds=3),
            approvals=(approval,),
        ).to_dict()
        self.assertEqual("outcome_unknown", unknown["status"])
        self.assertEqual("retry_exact_apply", unknown["next_action"])
        self.assertTrue(unknown["retryable"])
        self.assertIsNone(unknown["result"]["canonical_write_performed"])
        self.assertFalse(response.terminal)

        replay = execute_trip_baseline_apply_response(
            review,
            response,
            store,
            evaluation_at=NOW + timedelta(seconds=4),
            approvals=(approval,),
        ).to_dict()
        self.assertEqual("replay_confirmed", replay["status"])
        self.assertEqual("continue_planning", replay["next_action"])
        self.assertTrue(replay["result"]["replay_confirmed"])
        self.assertFalse(replay["result"]["canonical_write_performed"])
        self.assertTrue(response.terminal)
        self.assertEqual(1, fault_count)
        self.assertEqual(1, len(store.load_plan()["receipts"]))

        rollback_root = self.root / "lost-ack-rollback"
        rollback_fault_count = 0

        def rollback_fault(stage: str) -> None:
            nonlocal rollback_fault_count
            if stage == "after_replace" and rollback_fault_count == 0:
                rollback_fault_count += 1
                raise RuntimeError("canned lost acknowledgement")

        rollback_store = self._store(
            rollback_root,
            fault_hook=rollback_fault,
        )
        _rollback_classification, rollback_review = self._prepare(
            rollback_store,
            run_id="lost-ack-rollback",
        )
        rollback_response = capture_trip_baseline_apply_response(
            rollback_review,
            GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
            evaluation_at=NOW + timedelta(seconds=2),
        )
        rollback_approval = self._approval(
            rollback_review.required_approval_scope,
            approval_id="rollback-flow-apply",
        )
        first = execute_trip_baseline_apply_response(
            rollback_review,
            rollback_response,
            rollback_store,
            evaluation_at=NOW + timedelta(seconds=3),
            approvals=(rollback_approval,),
        ).to_dict()
        self.assertEqual("outcome_unknown", first["status"])

        current = rollback_store.load_plan()
        receipt = current["receipts"]["adopt-lost-ack-rollback"]
        rollback_preview = rollback_store.rollback(
            receipt["transaction_id"],
            current["revision"],
            "rollback-before-retry",
            evaluation_at=NOW + timedelta(seconds=4),
        )
        self.assertIsNotNone(rollback_preview.required_approval_scope)
        rollback_grant = ApprovalGrant(
            approval_id="rollback-flow-grant",
            scope_digest=rollback_preview.required_approval_scope or "",
            approved_by="fixture-human",
            approved_at=(NOW + timedelta(seconds=5)).isoformat(),
        )
        rolled = rollback_store.rollback(
            receipt["transaction_id"],
            current["revision"],
            "rollback-before-retry",
            approvals=(rollback_grant,),
            evaluation_at=NOW + timedelta(seconds=6),
        )
        self.assertEqual("rolled_back", rolled.status)

        outcome = execute_trip_baseline_apply_response(
            rollback_review,
            rollback_response,
            rollback_store,
            evaluation_at=NOW + timedelta(seconds=7),
            approvals=(rollback_approval,),
        ).to_dict()
        self.assertEqual("rolled_back", outcome["status"])
        self.assertEqual(
            "prepare_fresh_baseline_classification_review",
            outcome["next_action"],
        )
        self.assertFalse(outcome["result"]["applied"])
        self.assertFalse(outcome["result"]["replay_confirmed"])
        self.assertFalse(outcome["retryable"])
        self.assertFalse(outcome["pending_review_retained"])
        self.assertTrue(rollback_response.terminal)


if __name__ == "__main__":
    unittest.main()
