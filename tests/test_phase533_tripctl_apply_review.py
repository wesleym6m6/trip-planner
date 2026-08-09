"""Phase 5.33 evidence-bound score to expiring apply-review bridge."""

from __future__ import annotations

import json
import pickle
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import trip_planner
from tests.test_phase4_composition import _policies
from tests.test_phase533_tripctl_canonical import _tree_bytes
from tests.test_phase533_tripctl_schedule import (
    EVALUATION_AT,
    _verified_canonical_trip,
)
from trip_planner.codec import build_plan, encode_plan
from trip_planner.facts import EvidenceLedger, EvidenceSnapshot
from trip_planner.guided_canonical_apply import (
    GuidedCanonicalApplyResponseKind,
)
from trip_planner.lodging import (
    LodgingRequirement,
    assess_lodging_intake,
)
from trip_planner.store import TripStore
from trip_planner.tripctl import (
    TripctlApplyReviewError,
    TripctlApplyResponseError,
    TripctlScheduleApplyOutcome,
    TripctlScheduleApplyResponse,
    capture_trip_schedule_apply_response,
    execute_trip_schedule_apply_response,
    prepare_trip_schedule_apply_review,
    propose_trip_with_evidence,
    score_trip_with_evidence,
)
from trip_planner.tripctl_apply import TripctlScheduleApplyReview


PRIVATE_PROVIDER = "private-provider-runtime-state"


class _SnapshotLoad:
    def __init__(self, snapshot: EvidenceSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self, *, evaluation_at: datetime) -> EvidenceSnapshot:
        if evaluation_at != self._snapshot.evaluation_at:
            raise AssertionError("unexpected evidence evaluation clock")
        return self._snapshot


class _EvidenceSource:
    def __init__(self, *snapshots: EvidenceSnapshot) -> None:
        if not snapshots:
            raise ValueError("at least one snapshot is required")
        self._snapshots = snapshots
        self.load_count = 0

    def load(self) -> _SnapshotLoad:
        index = min(self.load_count, len(self._snapshots) - 1)
        self.load_count += 1
        return _SnapshotLoad(self._snapshots[index])


def _snapshot(*, store_revision: str = "9" * 64) -> EvidenceSnapshot:
    ledger = EvidenceLedger(_policies(PRIVATE_PROVIDER))
    return EvidenceSnapshot.from_ledger(
        ledger,
        evaluation_at=EVALUATION_AT,
        purge_now=EVALUATION_AT,
        store_revision=store_revision,
    )


def _lodging_intake():
    return assess_lodging_intake(
        stay_start=date(2026, 10, 1),
        stay_end=date(2026, 10, 2),
        requirement=LodgingRequirement.NOT_REQUIRED,
    )


def _schedule_plan(slug: str) -> dict[str, object]:
    def activity(
        activity_id: str,
        start: str,
        location_id: str,
        *,
        window: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "activity_id": activity_id,
            "title": activity_id,
            "location_id": location_id,
            "time": start,
            "duration_min": 30,
            "decision_state": "selected",
            "flexibility": "movable",
            "evidence_state": "verified",
            "type": "activity",
        }
        if window is not None:
            value["allowed_windows"] = [
                {"start": window[0], "end": window[1]}
            ]
        return value

    return build_plan(
        trip_id=slug,
        generation=1,
        state={
            "trip": {
                "slug": slug,
                "title": "Apply review fixture",
                "timezone": "Asia/Seoul",
                "date_range": "2026-10-01 ~ 2026-10-01",
                "cities": ["Fixture City"],
                "constraints": [],
            },
            "itinerary": {
                "available_modes": ["walking"],
                "days": [
                    {
                        "day_id": "day-1",
                        "day": 1,
                        "date": "2026-10-01",
                        "timezone": "Asia/Seoul",
                        "available_start": "08:00",
                        "available_end": "18:00",
                        "start_location_id": "location-alpha",
                        "end_location_id": "location-beta",
                        "allowed_modes": ["walking"],
                        "places": [
                            activity(
                                "activity-alpha",
                                "11:00",
                                "location-alpha",
                                window=("09:00", "10:00"),
                            ),
                            activity(
                                "activity-beta",
                                "12:00",
                                "location-beta",
                            ),
                        ],
                        "travel": [
                            {
                                "from_activity_id": "activity-alpha",
                                "to_activity_id": "activity-beta",
                                "recommended_mode": "walking",
                                "modes": {
                                    "walking": {
                                        "duration_min": 10,
                                        "evidence_state": "verified",
                                    }
                                },
                            }
                        ],
                    }
                ],
            },
        },
    )


class Phase533TripctlApplyReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.trips_root = Path(self.temporary.name) / "trips"
        self.slug = "phase533-apply-review"
        self.data_dir = self.trips_root / self.slug / "data"
        self.data_dir.mkdir(parents=True)
        self.plan_path = self.data_dir / "plan.json"
        self.plan_path.write_bytes(encode_plan(_schedule_plan(self.slug)))
        self.store = TripStore(self.trips_root, self.slug)
        self.snapshot = _snapshot()
        self.lodging_intake = _lodging_intake()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _proposal_and_score(self):
        proposal = propose_trip_with_evidence(
            self.data_dir,
            evidence_snapshot=self.snapshot,
            lodging_intake=self.lodging_intake,
        )
        proposal_ref = proposal["result"]["proposal_ref"]
        self.assertIsInstance(proposal_ref, str)
        score = score_trip_with_evidence(
            self.data_dir,
            proposal_ref=proposal_ref,
            evidence_snapshot=self.snapshot,
            lodging_intake=self.lodging_intake,
        )
        return proposal_ref, score

    def _prepare_review(
        self,
        *,
        store: TripStore | None = None,
        source: _EvidenceSource | None = None,
        run_id: str = "phase533-product-response",
        max_auto_changes: int = 12,
    ):
        proposal_ref, score = self._proposal_and_score()
        self.assertTrue(score["result"]["apply_review_available"])
        selected_store = store or self.store
        selected_source = source or _EvidenceSource(self.snapshot)
        review = prepare_trip_schedule_apply_review(
            selected_store,
            proposal_ref=proposal_ref,
            evidence_snapshot=self.snapshot,
            evidence_source=selected_source,
            lodging_intake=self.lodging_intake,
            reviewed_at=EVALUATION_AT + timedelta(seconds=1),
            run_id=run_id,
            max_auto_changes=max_auto_changes,
        )
        return proposal_ref, selected_source, review

    def test_changed_score_prepares_expiring_typed_review_without_write(
        self,
    ) -> None:
        proposal_ref, score = self._proposal_and_score()
        before = _tree_bytes(self.store.trip_dir)

        self.assertEqual("review_required", score["status"])
        self.assertEqual("review_proposal", score["next_action"])
        self.assertTrue(score["result"]["apply_review_available"])

        source = _EvidenceSource(self.snapshot)
        with (
            patch(
                "trip_planner.guided_canonical_apply."
                "capture_guided_canonical_apply_response",
                side_effect=AssertionError("response must not be captured"),
            ),
            patch(
                "trip_planner.guided_canonical_apply."
                "execute_guided_canonical_apply_response",
                side_effect=AssertionError("review must not execute"),
            ),
        ):
            review = prepare_trip_schedule_apply_review(
                self.store,
                proposal_ref=proposal_ref,
                evidence_snapshot=self.snapshot,
                evidence_source=source,
                lodging_intake=self.lodging_intake,
                reviewed_at=EVALUATION_AT + timedelta(seconds=1),
                run_id="phase533-review-only",
            )

        self.assertIs(
            trip_planner.prepare_trip_schedule_apply_review,
            prepare_trip_schedule_apply_review,
        )
        self.assertIsInstance(review, TripctlScheduleApplyReview)
        self.assertEqual(timedelta(minutes=30), review.expires_at - review.created_at)
        safe = review.to_dict()
        guided = safe["result"]["apply_review"]
        self.assertEqual("review_required", safe["status"])
        self.assertEqual("capture_apply_response", safe["next_action"])
        self.assertTrue(safe["pending_review_retained"])
        self.assertFalse(safe["result"]["apply_authority"])
        self.assertFalse(safe["result"]["canonical_write_performed"])
        self.assertEqual(
            ["accept_apply", "request_changes", "cancel"],
            guided["response_kinds"],
        )
        self.assertTrue(guided["product_context_bound"])
        self.assertFalse(guided["product_context_digest_exposed"])
        self.assertNotEqual(
            review.runtime_context_ref,
            review._review._context_binding_digest,
        )
        self.assertFalse(guided["provider_runtime_state_exposed"])
        self.assertGreater(guided["preview"]["change_count"], 0)
        self.assertTrue(guided["changes"])
        rendered = json.dumps(safe, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(PRIVATE_PROVIDER, rendered)
        self.assertNotIn('"evidence_binding_digest":', rendered)
        with self.assertRaises(TypeError):
            pickle.dumps(review)
        self.assertEqual(before, _tree_bytes(self.store.trip_dir))
        self.assertEqual({}, self.store.load_plan()["receipts"])
        self.assertGreaterEqual(source.load_count, 1)

    def test_noop_score_offers_no_review_and_prepare_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trip_dir = _verified_canonical_trip(root)
            store = TripStore(root, trip_dir.name)
            before = _tree_bytes(trip_dir)
            proposal = propose_trip_with_evidence(
                trip_dir,
                evidence_snapshot=self.snapshot,
                lodging_intake=self.lodging_intake,
            )
            proposal_ref = proposal["result"]["proposal_ref"]
            score = score_trip_with_evidence(
                trip_dir,
                proposal_ref=proposal_ref,
                evidence_snapshot=self.snapshot,
                lodging_intake=self.lodging_intake,
            )

            self.assertEqual("ready", score["status"])
            self.assertEqual("none", score["next_action"])
            self.assertFalse(score["requires_user_review"])
            self.assertFalse(score["result"]["apply_review_available"])
            with self.assertRaises(TripctlApplyReviewError) as raised:
                prepare_trip_schedule_apply_review(
                    store,
                    proposal_ref=proposal_ref,
                    evidence_snapshot=self.snapshot,
                    evidence_source=_EvidenceSource(self.snapshot),
                    lodging_intake=self.lodging_intake,
                    reviewed_at=EVALUATION_AT,
                    run_id="phase533-noop",
                )
            self.assertEqual("EMPTY_SCHEDULE_PATCH", raised.exception.code)
            self.assertEqual(before, _tree_bytes(trip_dir))

    def test_wrong_ref_or_lodging_context_never_reaches_preview(self) -> None:
        proposal_ref, _score = self._proposal_and_score()
        before = _tree_bytes(self.store.trip_dir)
        for kwargs in (
            {"proposal_ref": "sha256:" + "0" * 64},
            {
                "proposal_ref": proposal_ref,
                "lodging_intake": None,
            },
        ):
            with self.subTest(kwargs=kwargs):
                inputs = {
                    "proposal_ref": proposal_ref,
                    "lodging_intake": self.lodging_intake,
                }
                inputs.update(kwargs)
                with (
                    patch.object(
                        self.store,
                        "preview_patch",
                        side_effect=AssertionError("preview must not run"),
                    ),
                    self.assertRaises(TripctlApplyReviewError) as raised,
                ):
                    prepare_trip_schedule_apply_review(
                        self.store,
                        evidence_snapshot=self.snapshot,
                        evidence_source=_EvidenceSource(self.snapshot),
                        reviewed_at=EVALUATION_AT,
                        run_id="phase533-stale-context",
                        **inputs,
                    )
                self.assertEqual("STALE_PROPOSAL_REF", raised.exception.code)
        self.assertEqual(before, _tree_bytes(self.store.trip_dir))

    def test_review_clock_rollback_fails_before_staging(self) -> None:
        proposal_ref, _score = self._proposal_and_score()
        source = _EvidenceSource(self.snapshot)
        before = _tree_bytes(self.store.trip_dir)

        with self.assertRaises(TripctlApplyReviewError) as raised:
            prepare_trip_schedule_apply_review(
                self.store,
                proposal_ref=proposal_ref,
                evidence_snapshot=self.snapshot,
                evidence_source=source,
                lodging_intake=self.lodging_intake,
                reviewed_at=EVALUATION_AT - timedelta(microseconds=1),
                run_id="phase533-clock-rollback",
            )

        self.assertEqual("APPLY_REVIEW_CLOCK_ROLLBACK", raised.exception.code)
        self.assertEqual(0, source.load_count)
        self.assertEqual(before, _tree_bytes(self.store.trip_dir))

    def test_evidence_drift_rejects_before_canonical_preview(self) -> None:
        proposal_ref, _score = self._proposal_and_score()
        changed = _snapshot(store_revision="8" * 64)
        before = _tree_bytes(self.store.trip_dir)

        with (
            patch.object(
                self.store,
                "preview_patch",
                side_effect=AssertionError("preview must not run"),
            ),
            self.assertRaises(TripctlApplyReviewError) as raised,
        ):
            prepare_trip_schedule_apply_review(
                self.store,
                proposal_ref=proposal_ref,
                evidence_snapshot=self.snapshot,
                evidence_source=_EvidenceSource(changed),
                lodging_intake=self.lodging_intake,
                reviewed_at=EVALUATION_AT,
                run_id="phase533-evidence-drift",
            )

        self.assertEqual("EVIDENCE_REVISION_CHANGED", raised.exception.code)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(before, _tree_bytes(self.store.trip_dir))

    def test_exact_accept_executes_only_after_product_response_capture(
        self,
    ) -> None:
        proposal_ref, _source, review = self._prepare_review()
        before_capture = _tree_bytes(self.store.trip_dir)

        response = capture_trip_schedule_apply_response(
            review,
            GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
            evaluation_at=EVALUATION_AT + timedelta(seconds=2),
        )

        self.assertIs(
            trip_planner.capture_trip_schedule_apply_response,
            capture_trip_schedule_apply_response,
        )
        self.assertIsInstance(response, TripctlScheduleApplyResponse)
        captured = response.to_dict()
        self.assertEqual("response_captured", captured["status"])
        self.assertTrue(captured["result"]["acceptance_captured"])
        self.assertFalse(captured["result"]["apply_authority_exposed"])
        self.assertFalse(captured["result"]["canonical_write_performed"])
        self.assertEqual(before_capture, _tree_bytes(self.store.trip_dir))
        with self.assertRaises(TypeError):
            pickle.dumps(response)

        outcome = execute_trip_schedule_apply_response(
            review,
            response,
            self.store,
            evaluation_at=EVALUATION_AT + timedelta(seconds=3),
        )

        self.assertIs(
            trip_planner.execute_trip_schedule_apply_response,
            execute_trip_schedule_apply_response,
        )
        self.assertIsInstance(outcome, TripctlScheduleApplyOutcome)
        safe = outcome.to_dict()
        with self.assertRaises(TypeError):
            pickle.dumps(outcome)
        self.assertEqual("waiting_external", safe["status"])
        self.assertEqual("refresh_external_evidence", safe["next_action"])
        self.assertTrue(safe["result"]["applied"])
        self.assertTrue(safe["result"]["canonical_write_performed"])
        self.assertEqual("performed", safe["result"]["canonical_write_outcome"])
        self.assertFalse(safe["pending_review_retained"])
        self.assertTrue(response.terminal)
        current = self.store.load_plan()
        self.assertEqual(
            "09:00:00",
            current["state"]["itinerary"]["days"][0]["places"][0]["time"],
        )
        self.assertEqual(1, len(current["receipts"]))
        rendered = json.dumps(safe, ensure_ascii=False, sort_keys=True)
        for private_value in (
            PRIVATE_PROVIDER,
            "activity-alpha",
            "day-1",
            "location-alpha",
            "09:00",
        ):
            self.assertNotIn(private_value, rendered)
        with self.assertRaises(TripctlApplyResponseError) as replayed:
            execute_trip_schedule_apply_response(
                review,
                response,
                self.store,
                evaluation_at=EVALUATION_AT + timedelta(seconds=4),
            )
        self.assertEqual(
            "APPLY_RESPONSE_ALREADY_EXECUTED",
            replayed.exception.code,
        )

    def test_request_changes_and_cancel_release_pending_without_write(
        self,
    ) -> None:
        for index, (kind, expected_status, next_action) in enumerate(
            (
                (
                    GuidedCanonicalApplyResponseKind.REQUEST_CHANGES,
                    "changes_requested",
                    "prepare_revised_canonical_proposal",
                ),
                (
                    GuidedCanonicalApplyResponseKind.CANCEL,
                    "cancelled",
                    "stop_canonical_apply",
                ),
            )
        ):
            with self.subTest(kind=kind.value):
                _proposal_ref, _source, review = self._prepare_review(
                    run_id=f"phase533-nonaccept-{index}"
                )
                before = _tree_bytes(self.store.trip_dir)
                response = capture_trip_schedule_apply_response(
                    review,
                    kind,
                    evaluation_at=EVALUATION_AT + timedelta(seconds=2),
                )
                outcome = execute_trip_schedule_apply_response(
                    review,
                    response,
                    self.store,
                    evaluation_at=EVALUATION_AT + timedelta(seconds=3),
                )
                safe = outcome.to_dict()

                self.assertEqual(expected_status, safe["status"])
                self.assertEqual(next_action, safe["next_action"])
                self.assertFalse(safe["result"]["applied"])
                self.assertFalse(
                    safe["result"]["canonical_write_performed"]
                )
                self.assertFalse(safe["pending_review_retained"])
                self.assertTrue(response.terminal)
                self.assertEqual(before, _tree_bytes(self.store.trip_dir))

    def test_response_capture_is_enum_only_expiring_and_single_decision(
        self,
    ) -> None:
        _proposal_ref, _source, review = self._prepare_review()
        before = _tree_bytes(self.store.trip_dir)
        stager = review._review._subject

        with self.assertRaises(TripctlApplyResponseError) as generic:
            capture_trip_schedule_apply_response(
                review,
                "accept_apply",
                evaluation_at=EVALUATION_AT + timedelta(seconds=2),
            )
        self.assertEqual("INVALID_APPLY_RESPONSE_KIND", generic.exception.code)

        with self.assertRaises(TripctlApplyResponseError) as expired:
            capture_trip_schedule_apply_response(
                review,
                GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
                evaluation_at=review.expires_at + timedelta(microseconds=1),
            )
        self.assertEqual("APPLY_REVIEW_EXPIRED", expired.exception.code)
        self.assertFalse(stager.has_pending_review)

        with self.assertRaises(TripctlApplyResponseError) as stale:
            capture_trip_schedule_apply_response(
                review,
                GuidedCanonicalApplyResponseKind.CANCEL,
                evaluation_at=EVALUATION_AT + timedelta(seconds=2),
            )
        self.assertEqual("STALE_APPLY_REVIEW", stale.exception.code)

        _fresh_ref, _fresh_source, fresh_review = self._prepare_review(
            run_id="phase533-single-decision"
        )
        response = capture_trip_schedule_apply_response(
            fresh_review,
            GuidedCanonicalApplyResponseKind.CANCEL,
            evaluation_at=EVALUATION_AT + timedelta(seconds=2),
        )
        self.assertFalse(response.to_dict()["result"]["acceptance_captured"])
        with self.assertRaises(TripctlApplyResponseError) as duplicate:
            capture_trip_schedule_apply_response(
                fresh_review,
                GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
                evaluation_at=EVALUATION_AT + timedelta(seconds=3),
            )
        self.assertEqual(
            "APPLY_RESPONSE_ALREADY_CAPTURED",
            duplicate.exception.code,
        )
        self.assertEqual(before, _tree_bytes(self.store.trip_dir))

    def test_response_rejects_wrong_review_store_and_clock_before_write(
        self,
    ) -> None:
        _proposal_ref, _source, review = self._prepare_review(
            run_id="phase533-execution-binding-a"
        )
        response = capture_trip_schedule_apply_response(
            review,
            GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
            evaluation_at=EVALUATION_AT + timedelta(seconds=2),
        )
        _other_ref, _other_source, other_review = self._prepare_review(
            run_id="phase533-execution-binding-b"
        )
        before = _tree_bytes(self.store.trip_dir)

        with self.assertRaises(TripctlApplyResponseError) as mismatched:
            execute_trip_schedule_apply_response(
                other_review,
                response,
                self.store,
                evaluation_at=EVALUATION_AT + timedelta(seconds=3),
            )
        self.assertEqual(
            "APPLY_RESPONSE_REVIEW_MISMATCH",
            mismatched.exception.code,
        )

        with self.assertRaises(TripctlApplyResponseError) as rollback:
            execute_trip_schedule_apply_response(
                review,
                response,
                self.store,
                evaluation_at=response.captured_at - timedelta(microseconds=1),
            )
        self.assertEqual(
            "APPLY_EXECUTION_CLOCK_ROLLBACK",
            rollback.exception.code,
        )

        with tempfile.TemporaryDirectory() as temporary:
            other_root = Path(temporary) / "trips"
            other_data = other_root / self.slug / "data"
            other_data.mkdir(parents=True)
            (other_data / "plan.json").write_bytes(
                encode_plan(_schedule_plan(self.slug))
            )
            other_store = TripStore(other_root, self.slug)
            with self.assertRaises(TripctlApplyResponseError) as wrong_store:
                execute_trip_schedule_apply_response(
                    review,
                    response,
                    other_store,
                    evaluation_at=EVALUATION_AT + timedelta(seconds=3),
                )
        self.assertEqual("APPLY_STORE_MISMATCH", wrong_store.exception.code)
        self.assertFalse(response.terminal)
        self.assertEqual(before, _tree_bytes(self.store.trip_dir))

    def test_execution_expiry_is_terminal_and_cannot_be_rolled_back(
        self,
    ) -> None:
        _proposal_ref, _source, review = self._prepare_review(
            run_id="phase533-execution-expiry"
        )
        response = capture_trip_schedule_apply_response(
            review,
            GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
            evaluation_at=EVALUATION_AT + timedelta(seconds=2),
        )
        stager = review._review._subject
        before = _tree_bytes(self.store.trip_dir)

        with self.assertRaises(TripctlApplyResponseError) as expired:
            execute_trip_schedule_apply_response(
                review,
                response,
                self.store,
                evaluation_at=review.expires_at + timedelta(microseconds=1),
            )
        self.assertEqual("APPLY_REVIEW_EXPIRED", expired.exception.code)
        self.assertTrue(response.terminal)
        self.assertFalse(stager.has_pending_review)
        self.assertEqual(before, _tree_bytes(self.store.trip_dir))

        with self.assertRaises(TripctlApplyResponseError) as replayed:
            execute_trip_schedule_apply_response(
                review,
                response,
                self.store,
                evaluation_at=EVALUATION_AT + timedelta(seconds=3),
            )
        self.assertEqual(
            "APPLY_RESPONSE_ALREADY_EXECUTED",
            replayed.exception.code,
        )
        self.assertEqual(before, _tree_bytes(self.store.trip_dir))

    def test_execution_rechecks_evidence_and_canonical_cas(self) -> None:
        changed_snapshot = _snapshot(store_revision="8" * 64)
        source = _EvidenceSource(self.snapshot, changed_snapshot)
        _proposal_ref, _source, review = self._prepare_review(
            source=source,
            run_id="phase533-commit-evidence-drift",
        )
        response = capture_trip_schedule_apply_response(
            review,
            GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
            evaluation_at=EVALUATION_AT + timedelta(seconds=2),
        )
        before = _tree_bytes(self.store.trip_dir)

        outcome = execute_trip_schedule_apply_response(
            review,
            response,
            self.store,
            evaluation_at=EVALUATION_AT + timedelta(seconds=3),
        )
        safe = outcome.to_dict()

        self.assertEqual("apply_failed", safe["status"])
        self.assertIn(
            "EVIDENCE_REVISION_CHANGED",
            {item["code"] for item in safe["problems"]},
        )
        self.assertFalse(safe["result"]["canonical_write_performed"])
        self.assertFalse(safe["pending_review_retained"])
        self.assertEqual(before, _tree_bytes(self.store.trip_dir))

        self.plan_path.write_bytes(encode_plan(_schedule_plan(self.slug)))
        _proposal_ref, _source, cas_review = self._prepare_review(
            run_id="phase533-commit-canonical-drift"
        )
        cas_response = capture_trip_schedule_apply_response(
            cas_review,
            GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
            evaluation_at=EVALUATION_AT + timedelta(seconds=2),
        )
        drifted = _schedule_plan(self.slug)
        drifted_state = drifted["state"]
        drifted_state["itinerary"]["days"][0]["places"][0]["note"] = (
            "external canonical change"
        )
        self.plan_path.write_bytes(
            encode_plan(
                build_plan(
                    trip_id=self.slug,
                    generation=2,
                    state=drifted_state,
                )
            )
        )
        before_cas = self.plan_path.read_bytes()

        cas_outcome = execute_trip_schedule_apply_response(
            cas_review,
            cas_response,
            self.store,
            evaluation_at=EVALUATION_AT + timedelta(seconds=3),
        ).to_dict()
        self.assertEqual("apply_failed", cas_outcome["status"])
        self.assertIn(
            "STALE_SCHEDULE_REVIEW",
            {item["code"] for item in cas_outcome["problems"]},
        )
        self.assertFalse(
            cas_outcome["result"]["canonical_write_performed"]
        )
        self.assertEqual(before_cas, self.plan_path.read_bytes())

    def test_lost_ack_recovers_from_exact_receipt_in_one_execution(self) -> None:
        def fail_after_replace(stage: str) -> None:
            if stage == "after_replace":
                raise RuntimeError("forced temporary lost ack")

        faulting_store = TripStore(
            self.trips_root,
            self.slug,
            fault_hook=fail_after_replace,
        )
        _proposal_ref, _source, review = self._prepare_review(
            store=faulting_store,
            run_id="phase533-product-lost-ack",
        )
        response = capture_trip_schedule_apply_response(
            review,
            GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
            evaluation_at=EVALUATION_AT + timedelta(seconds=2),
        )

        outcome = execute_trip_schedule_apply_response(
            review,
            response,
            faulting_store,
            evaluation_at=EVALUATION_AT + timedelta(seconds=3),
        )
        safe = outcome.to_dict()

        self.assertEqual("waiting_external", safe["status"])
        self.assertTrue(safe["result"]["applied"])
        self.assertTrue(safe["result"]["replay_confirmed"])
        self.assertTrue(safe["result"]["canonical_write_performed"])
        self.assertFalse(safe["pending_review_retained"])
        current = self.store.load_plan()
        self.assertEqual(1, len(current["receipts"]))
        self.assertEqual(
            "09:00:00",
            current["state"]["itinerary"]["days"][0]["places"][0]["time"],
        )

    def test_unreconciled_double_write_reports_truthful_unknown(self) -> None:
        original_plan = self.plan_path.read_bytes()
        fault_count = 0

        def obscure_after_replace(stage: str) -> None:
            nonlocal fault_count
            if stage == "after_replace":
                fault_count += 1
                self.plan_path.write_bytes(original_plan)
                raise RuntimeError("forced temporary unresolved write")

        faulting_store = TripStore(
            self.trips_root,
            self.slug,
            fault_hook=obscure_after_replace,
        )
        _proposal_ref, _source, review = self._prepare_review(
            store=faulting_store,
            run_id="phase533-product-outcome-unknown",
        )
        response = capture_trip_schedule_apply_response(
            review,
            GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
            evaluation_at=EVALUATION_AT + timedelta(seconds=2),
        )

        outcome = execute_trip_schedule_apply_response(
            review,
            response,
            faulting_store,
            evaluation_at=EVALUATION_AT + timedelta(seconds=3),
        )
        safe = outcome.to_dict()

        self.assertEqual(2, fault_count)
        self.assertEqual("outcome_unknown", safe["status"])
        self.assertEqual("inspect_apply_outcome", safe["next_action"])
        self.assertIsNone(safe["result"]["canonical_write_performed"])
        self.assertEqual(
            "unknown",
            safe["result"]["canonical_write_outcome"],
        )
        self.assertFalse(safe["pending_review_retained"])
        self.assertFalse(safe["retryable"])
        self.assertTrue(response.terminal)
        self.assertEqual(original_plan, self.plan_path.read_bytes())
        self.assertEqual({}, self.store.load_plan()["receipts"])


if __name__ == "__main__":
    unittest.main()
