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
from trip_planner.lodging import (
    LodgingRequirement,
    assess_lodging_intake,
)
from trip_planner.store import TripStore
from trip_planner.tripctl import (
    TripctlApplyReviewError,
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


if __name__ == "__main__":
    unittest.main()
