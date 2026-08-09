"""Phase 5.32 always-explicit canonical apply response gate."""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from tests.test_phase2_repair_loop import _EVIDENCE_POLICIES
from tests.test_phase532_plan_creation import EVALUATION_AT, _legacy_sources
from tests.test_phase59_guided_itinerary_response import _capture
from trip_planner.codec import build_plan, encode_plan
from trip_planner.facts import EvidenceLedger, EvidenceSnapshot
from trip_planner.guided_canonical_apply import (
    GUIDED_CANONICAL_APPLY_VERSION,
    GuidedCanonicalApplyResponseKind,
    _outcome_from_result,
    capture_guided_canonical_apply_response,
    execute_guided_canonical_apply_response,
    prepare_guided_canonical_create_review,
    prepare_guided_canonical_lodging_review,
    prepare_guided_canonical_migration_review,
    prepare_guided_canonical_repair_review,
    prepare_guided_canonical_schedule_review,
)
from trip_planner.guided_itinerary import GuidedItineraryResponseKind
from trip_planner.lodging import LodgingKind
from trip_planner.lodging_confirmation import (
    LodgingConfirmationAuthority,
    LodgingConfirmationRequest,
    LodgingConfirmationResult,
    LodgingConfirmationState,
    LodgingConfirmationStager,
    LodgingSelectionAnchor,
    LodgingSelectionSegment,
)
from trip_planner.models import DecisionState
from trip_planner.mutations import RemoveActivity, UpdateActivity
from trip_planner.plan_creation import prepare_guided_plan_create_request
from trip_planner.repair import OperationReason, ProposalIntent
from trip_planner.repair_loop import RepairController, RepairResult, RepairState
from trip_planner.schedule_staging import (
    ScheduleCommitResult,
    ScheduleStager,
    ScheduleStageState,
)
from trip_planner.scheduler import SOLVER_VERSION
from trip_planner.scheduling import (
    ScheduleAssignment,
    build_schedule_candidate,
    schedule_problem_from_plan,
)
from trip_planner.store import TripStore


UTC = timezone.utc


class _EvidenceGeneration:
    def __init__(self, generation: int) -> None:
        self.generation = generation

    def snapshot(self, *, evaluation_at: datetime) -> EvidenceSnapshot:
        ledger = EvidenceLedger(
            _EVIDENCE_POLICIES,
            generation=self.generation,
        )
        return EvidenceSnapshot.from_ledger(
            ledger,
            evaluation_at=evaluation_at,
            purge_now=evaluation_at,
        )


class _PostCommitDriftingEvidenceSource:
    def __init__(self) -> None:
        self._generations = (0, 0, 0, 0, 1)
        self._load_count = 0

    def load(self) -> _EvidenceGeneration:
        index = min(self._load_count, len(self._generations) - 1)
        self._load_count += 1
        return _EvidenceGeneration(self._generations[index])


class GuidedCanonicalApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.trips_root = self.root / "trips"
        self.slug = "phase532-create"
        self.data_dir = self.trips_root / self.slug / "data"
        self.data_dir.mkdir(parents=True)
        self.plan_path = self.data_dir / "plan.json"
        self.store = TripStore(self.trips_root, self.slug)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_request(self, *, key: str = "canonical-review-create"):
        context = _capture(
            GuidedItineraryResponseKind.ACCEPT_ITINERARY_CANDIDATE
        )
        return prepare_guided_plan_create_request(
            *context,
            trip_id=self.slug,
            timezone_name="Asia/Taipei",
            idempotency_key=key,
            evaluation_at=EVALUATION_AT,
        )

    def create_plan(self) -> dict[str, object]:
        request = self.create_request()
        created = self.store.commit_create(self.store.preview_create(request))
        self.assertTrue(created.success, created.to_dict())
        return self.store.load_plan()

    def _accept(self, review, *, offset: int = 2):
        return capture_guided_canonical_apply_response(
            review,
            GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
            evaluation_at=EVALUATION_AT + timedelta(seconds=offset),
        )

    def test_create_review_is_informed_and_requires_typed_accept(self) -> None:
        request = self.create_request()
        review = prepare_guided_canonical_create_review(
            self.store,
            request,
            evaluation_at=EVALUATION_AT + timedelta(seconds=1),
        )
        safe = review.to_dict()
        self.assertEqual(
            GUIDED_CANONICAL_APPLY_VERSION,
            safe["contract_version"],
        )
        self.assertTrue(safe["candidate_plan_exposed"])
        self.assertEqual(
            request.mutable_candidate_plan(),
            safe["proposal"]["candidate_plan"],
        )
        self.assertFalse(self.plan_path.exists())

        response = self._accept(review)
        outcome = execute_guided_canonical_apply_response(
            review,
            response,
            self.store,
            evaluation_at=EVALUATION_AT + timedelta(seconds=3),
        )
        self.assertEqual("applied", outcome.status)
        self.assertTrue(outcome.store_result.success)
        self.assertTrue(self.plan_path.exists())

    def test_cancel_and_request_changes_consume_review_without_writes(self) -> None:
        for index, (kind, status) in enumerate(
            (
                (GuidedCanonicalApplyResponseKind.CANCEL, "cancelled"),
                (
                    GuidedCanonicalApplyResponseKind.REQUEST_CHANGES,
                    "changes_requested",
                ),
            )
        ):
            with self.subTest(kind=kind.value):
                created = EVALUATION_AT + timedelta(minutes=index)
                review = prepare_guided_canonical_create_review(
                    self.store,
                    self.create_request(key=f"cancel-{index}"),
                    evaluation_at=created,
                )
                response = capture_guided_canonical_apply_response(
                    review,
                    kind,
                    evaluation_at=created + timedelta(seconds=1),
                )
                outcome = execute_guided_canonical_apply_response(
                    review,
                    response,
                    self.store,
                    evaluation_at=created + timedelta(seconds=2),
                )
                self.assertEqual(status, outcome.status)
                self.assertIsNone(outcome.store_result)
                self.assertFalse(self.plan_path.exists())
                with self.assertRaises(ValueError):
                    self._accept(review)

    def test_expiry_tamper_and_exact_captured_response_fail_closed(self) -> None:
        first = prepare_guided_canonical_create_review(
            self.store,
            self.create_request(),
            evaluation_at=EVALUATION_AT + timedelta(seconds=1),
        )
        with self.assertRaises(ValueError):
            capture_guided_canonical_apply_response(
                first,
                GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
                evaluation_at=first.expires_at + timedelta(microseconds=1),
            )
        original = first.review_id
        object.__setattr__(first, "review_id", "0" * 64)
        try:
            with self.assertRaises(ValueError):
                first.to_dict()
        finally:
            object.__setattr__(first, "review_id", original)

        accepted = self._accept(first)
        object.__setattr__(accepted, "kind", GuidedCanonicalApplyResponseKind.CANCEL)
        with self.assertRaises(ValueError):
            execute_guided_canonical_apply_response(
                first,
                accepted,
                self.store,
                evaluation_at=EVALUATION_AT + timedelta(seconds=3),
            )
        self.assertFalse(self.plan_path.exists())

    def test_duplicate_logical_review_has_one_process_wide_decision(self) -> None:
        cancel_a = prepare_guided_canonical_create_review(
            self.store,
            self.create_request(key="cancel-wins"),
            evaluation_at=EVALUATION_AT + timedelta(seconds=1),
        )
        cancel_b = prepare_guided_canonical_create_review(
            self.store,
            self.create_request(key="cancel-wins"),
            evaluation_at=cancel_a.created_at,
        )
        self.assertEqual(cancel_a.review_id, cancel_b.review_id)
        cancelled = capture_guided_canonical_apply_response(
            cancel_b,
            GuidedCanonicalApplyResponseKind.CANCEL,
            evaluation_at=EVALUATION_AT + timedelta(seconds=2),
        )
        self.assertEqual(
            "cancelled",
            execute_guided_canonical_apply_response(
                cancel_b,
                cancelled,
                self.store,
                evaluation_at=EVALUATION_AT + timedelta(seconds=3),
            ).status,
        )
        with self.assertRaises(ValueError):
            self._accept(cancel_a, offset=2)

        accept_a = prepare_guided_canonical_create_review(
            self.store,
            self.create_request(key="accept-wins"),
            evaluation_at=EVALUATION_AT + timedelta(seconds=4),
        )
        accept_b = prepare_guided_canonical_create_review(
            self.store,
            self.create_request(key="accept-wins"),
            evaluation_at=accept_a.created_at,
        )
        accepted = capture_guided_canonical_apply_response(
            accept_a,
            GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
            evaluation_at=EVALUATION_AT + timedelta(seconds=5),
        )
        with self.assertRaises(ValueError):
            capture_guided_canonical_apply_response(
                accept_b,
                GuidedCanonicalApplyResponseKind.CANCEL,
                evaluation_at=EVALUATION_AT + timedelta(seconds=5),
            )
        outcome = execute_guided_canonical_apply_response(
            accept_a,
            accepted,
            self.store,
            evaluation_at=EVALUATION_AT + timedelta(seconds=6),
        )
        self.assertEqual("applied", outcome.status)

    def test_store_target_is_bound_beyond_a_matching_slug(self) -> None:
        review = prepare_guided_canonical_create_review(
            self.store,
            self.create_request(),
            evaluation_at=EVALUATION_AT + timedelta(seconds=1),
        )
        response = self._accept(review)
        other_root = self.root / "other-trips"
        (other_root / self.slug / "data").mkdir(parents=True)
        other_store = TripStore(other_root, self.slug)
        with self.assertRaises(ValueError):
            execute_guided_canonical_apply_response(
                review,
                response,
                other_store,
                evaluation_at=EVALUATION_AT + timedelta(seconds=3),
            )
        self.assertFalse((other_root / self.slug / "data" / "plan.json").exists())

    def test_create_lost_ack_exact_retry_reaches_receipt_replay(self) -> None:
        def fault(stage: str) -> None:
            if stage == "after_replace":
                raise RuntimeError("forced lost ack")

        faulting = TripStore(self.trips_root, self.slug, fault_hook=fault)
        review = prepare_guided_canonical_create_review(
            faulting,
            self.create_request(),
            evaluation_at=EVALUATION_AT + timedelta(seconds=1),
        )
        response = self._accept(review)
        first = execute_guided_canonical_apply_response(
            review,
            response,
            faulting,
            evaluation_at=EVALUATION_AT + timedelta(seconds=3),
        )
        self.assertEqual("outcome_unknown", first.status)
        self.assertTrue(self.plan_path.exists())

        retry = execute_guided_canonical_apply_response(
            review,
            response,
            TripStore(self.trips_root, self.slug),
            evaluation_at=EVALUATION_AT + timedelta(seconds=4),
        )
        self.assertEqual("replay_confirmed", retry.status)
        self.assertTrue(retry.store_result.replayed)

    def test_migration_accept_and_lost_ack_retry_are_receipt_first(self) -> None:
        trip, itinerary = _legacy_sources(self.slug)
        (self.data_dir / "trip.json").write_text(json.dumps(trip), encoding="utf-8")
        (self.data_dir / "itinerary.json").write_text(
            json.dumps(itinerary), encoding="utf-8"
        )

        def fault(stage: str) -> None:
            if stage == "after_replace":
                raise RuntimeError("forced migration lost ack")

        faulting = TripStore(self.trips_root, self.slug, fault_hook=fault)
        review = prepare_guided_canonical_migration_review(
            faulting,
            evaluation_at=EVALUATION_AT,
        )
        response = self._accept(review, offset=1)
        first = execute_guided_canonical_apply_response(
            review,
            response,
            faulting,
            evaluation_at=EVALUATION_AT + timedelta(seconds=2),
        )
        self.assertEqual("outcome_unknown", first.status)
        replay = execute_guided_canonical_apply_response(
            review,
            response,
            TripStore(self.trips_root, self.slug),
            evaluation_at=EVALUATION_AT + timedelta(seconds=3),
        )
        self.assertEqual("replay_confirmed", replay.status)

    def test_repair_apply_can_only_commit_the_exact_controller_review(self) -> None:
        plan = self.create_plan()
        activity_id = plan["state"]["itinerary"]["days"][0]["places"][0][
            "activity_id"
        ]
        controller = RepairController(
            self.store,
            run_id="phase532-repair",
            evaluation_at=EVALUATION_AT,
        )
        snapshot = controller.inspect()
        issue = next(
            item
            for item in snapshot.issues
            if item.check.code == "ACTIVITY_DURATION_UNVERIFIED"
            and activity_id in item.check.activity_ids
        )
        activity_ids = tuple(issue.check.activity_ids)
        operations = tuple(
            UpdateActivity(
                f"phase532-set-duration-{index}",
                candidate_id,
                {"duration_min": 45},
            )
            for index, candidate_id in enumerate(activity_ids)
        )
        proposal = ProposalIntent(
            operations=operations,
            reasons=tuple(
                OperationReason(
                    op_id=operation.op_id,
                    issue_ids=(issue.issue_id,),
                    option_key="set_activity_duration",
                    reason="Resolve the reviewed missing duration.",
                )
                for operation in operations
            ),
            summary="Set one reviewed duration.",
        )
        domain_review = controller.submit(snapshot.snapshot_id, proposal)
        review = prepare_guided_canonical_repair_review(
            self.store,
            controller,
            domain_review,
            evaluation_at=EVALUATION_AT + timedelta(seconds=1),
        )
        self.assertEqual(len(activity_ids), len(review.patch_changes()))
        outcome = execute_guided_canonical_apply_response(
            review,
            self._accept(review),
            self.store,
            evaluation_at=EVALUATION_AT + timedelta(seconds=3),
        )
        self.assertTrue(outcome.domain_result.applied, outcome.to_dict())
        persisted = self.store.load_plan()
        self.assertEqual(
            45,
            persisted["state"]["itinerary"]["days"][0]["places"][0][
                "duration_min"
            ],
        )

    def test_repair_write_with_post_commit_drift_stays_waiting_external(
        self,
    ) -> None:
        plan = self.create_plan()
        activity_id = plan["state"]["itinerary"]["days"][0]["places"][0][
            "activity_id"
        ]
        evidence_source = _PostCommitDriftingEvidenceSource()
        controller = RepairController(
            self.store,
            run_id="phase532-post-commit-drift",
            evaluation_at=EVALUATION_AT,
            evidence_source=evidence_source,
        )
        snapshot = controller.inspect()
        issue = next(
            item
            for item in snapshot.issues
            if item.check.code == "ACTIVITY_DURATION_UNVERIFIED"
            and activity_id in item.check.activity_ids
        )
        operations = tuple(
            UpdateActivity(
                f"phase532-drift-duration-{index}",
                candidate_id,
                {"duration_min": 45},
            )
            for index, candidate_id in enumerate(issue.check.activity_ids)
        )
        domain_review = controller.submit(
            snapshot.snapshot_id,
            ProposalIntent(
                operations=operations,
                reasons=tuple(
                    OperationReason(
                        op_id=operation.op_id,
                        issue_ids=(issue.issue_id,),
                        option_key="set_activity_duration",
                        reason="Resolve the reviewed missing duration.",
                    )
                    for operation in operations
                ),
                summary="Set the reviewed durations.",
            ),
        )
        review = prepare_guided_canonical_repair_review(
            self.store,
            controller,
            domain_review,
            evaluation_at=EVALUATION_AT + timedelta(seconds=1),
        )
        outcome = execute_guided_canonical_apply_response(
            review,
            self._accept(review),
            self.store,
            evaluation_at=EVALUATION_AT + timedelta(seconds=3),
        )

        self.assertTrue(outcome.domain_result.applied, outcome.to_dict())
        self.assertEqual("waiting_external", outcome.status)
        self.assertEqual("refresh_external_evidence", outcome.next_action)
        self.assertTrue(outcome.to_dict()["canonical_write_performed"])
        self.assertEqual(
            {"EVIDENCE_REVISION_CHANGED"},
            {problem.code for problem in outcome.domain_result.problems},
        )

    def test_domain_replay_confirmation_does_not_claim_a_new_write(self) -> None:
        self.create_plan()
        snapshot = RepairController(
            self.store,
            run_id="phase532-replay-output",
            evaluation_at=EVALUATION_AT,
        ).inspect()
        results = (
            RepairResult(
                state=RepairState.COMPLETE,
                snapshot=snapshot,
                applied=True,
                store_status="replayed",
            ),
            ScheduleCommitResult(
                state=ScheduleStageState.REPLAY_CONFIRMED,
                applied=True,
                review_id="schedule-review",
                problem_id="schedule-problem",
                candidate_id="schedule-candidate",
                store_status="replayed",
                transaction_id="schedule-transaction",
                applied_revision="applied-revision",
                current_revision="current-revision",
                replayed=True,
            ),
            LodgingConfirmationResult(
                state=LodgingConfirmationState.REPLAY_CONFIRMED,
                applied=True,
            ),
        )

        for result in results:
            with self.subTest(result_type=type(result).__name__):
                outcome = _outcome_from_result(result)
                self.assertEqual("replay_confirmed", outcome.status)
                self.assertFalse(
                    outcome.to_dict()["canonical_write_performed"]
                )

    def test_accept_is_the_exact_high_risk_human_checkpoint(self) -> None:
        self.create_plan()
        controller = RepairController(
            self.store,
            run_id="phase532-high-risk-repair",
            evaluation_at=EVALUATION_AT,
        )
        snapshot = controller.inspect()
        issue = next(
            item
            for item in snapshot.issues
            if item.check.code == "ACTIVITY_DURATION_UNVERIFIED"
        )
        operations = tuple(
            RemoveActivity(f"remove-{index}", activity_id)
            for index, activity_id in enumerate(issue.check.activity_ids)
        )
        domain_review = controller.submit(
            snapshot.snapshot_id,
            ProposalIntent(
                operations=operations,
                reasons=tuple(
                    OperationReason(
                        op_id=operation.op_id,
                        issue_ids=(issue.issue_id,),
                        option_key="set_activity_duration",
                        reason="Remove only after an explicit high-risk review.",
                    )
                    for operation in operations
                ),
                summary="Review removing the unresolved activities.",
            ),
        )
        self.assertTrue(domain_review.requires_human_checkpoint)
        review = prepare_guided_canonical_repair_review(
            self.store,
            controller,
            domain_review,
            evaluation_at=EVALUATION_AT + timedelta(seconds=1),
        )
        outcome = execute_guided_canonical_apply_response(
            review,
            self._accept(review),
            self.store,
            evaluation_at=EVALUATION_AT + timedelta(seconds=3),
        )
        self.assertTrue(outcome.domain_result.applied, outcome.to_dict())
        self.assertTrue(
            all(
                not day["places"]
                for day in self.store.load_plan()["state"]["itinerary"]["days"]
            )
        )

    def test_schedule_apply_reuses_strict_improvement_stager(self) -> None:
        self.plan_path.write_bytes(encode_plan(self._schedule_plan()))
        plan = self.store.load_plan()
        problem = schedule_problem_from_plan(plan, evaluation_at=EVALUATION_AT)
        candidate = build_schedule_candidate(
            problem,
            (
                ScheduleAssignment("activity-alpha", "day-1", 0, time(9)),
                ScheduleAssignment("activity-beta", "day-1", 1, time(10)),
            ),
            solver=SOLVER_VERSION,
        )
        stager = ScheduleStager(
            self.store,
            run_id="phase532-schedule",
            max_auto_changes=8,
        )
        domain_review = stager.stage_schedule_candidate(problem, candidate)
        with self.assertRaisesRegex(ValueError, "matching ready review"):
            prepare_guided_canonical_schedule_review(
                self.store,
                stager,
                domain_review,
                evaluation_at=EVALUATION_AT - timedelta(microseconds=1),
            )
        review = prepare_guided_canonical_schedule_review(
            self.store,
            stager,
            domain_review,
            evaluation_at=EVALUATION_AT + timedelta(seconds=1),
        )
        self.assertNotIn("evidence_binding_digest", review.to_dict()["preview"])
        outcome = execute_guided_canonical_apply_response(
            review,
            self._accept(review),
            self.store,
            evaluation_at=EVALUATION_AT + timedelta(seconds=3),
        )
        self.assertTrue(outcome.domain_result.applied, outcome.to_dict())
        self.assertEqual(
            "09:00:00",
            self.store.load_plan()["state"]["itinerary"]["days"][0][
                "places"
            ][0]["time"],
        )

    def test_lodging_accept_never_substitutes_for_signed_confirmation(self) -> None:
        signing_key = b"phase532-lodging-host-key"

        def sign(payload: bytes) -> str:
            return hmac.new(signing_key, payload, hashlib.sha256).hexdigest()

        def verify(grant) -> bool:
            return hmac.compare_digest(
                grant.signature,
                sign(grant.verification_payload()),
            )

        self.plan_path.write_bytes(encode_plan(self._lodging_plan()))
        store = TripStore(
            self.trips_root,
            self.slug,
            lodging_confirmation_verifier=verify,
        )
        stager = LodgingConfirmationStager(store, clock=lambda: EVALUATION_AT)
        domain_review = stager.stage(self._lodging_request(store))
        review = prepare_guided_canonical_lodging_review(
            store,
            stager,
            domain_review,
            evaluation_at=EVALUATION_AT,
        )
        response = capture_guided_canonical_apply_response(
            review,
            GuidedCanonicalApplyResponseKind.ACCEPT_APPLY,
            evaluation_at=EVALUATION_AT,
        )
        missing = execute_guided_canonical_apply_response(
            review,
            response,
            store,
            evaluation_at=EVALUATION_AT,
        )
        self.assertEqual("waiting_confirmation", missing.status)
        self.assertNotIn("lodgings", store.load_plan()["state"]["trip"])

        authority = LodgingConfirmationAuthority(
            confirmed_by="phase532-host",
            issuer_id="phase532-issuer",
            signer=sign,
            clock=lambda: EVALUATION_AT,
        )
        applied = execute_guided_canonical_apply_response(
            review,
            response,
            store,
            evaluation_at=EVALUATION_AT,
            lodging_confirmation=authority.issue_grant(domain_review),
        )
        self.assertEqual("applied", applied.status)
        self.assertIn("lodgings", store.load_plan()["state"]["trip"])

    def _schedule_plan(self) -> dict[str, object]:
        def activity(activity_id: str, start: str, location_id: str, *, window=None):
            value = {
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
            trip_id=self.slug,
            generation=1,
            state={
                "trip": {
                    "slug": self.slug,
                    "title": "Schedule wrapper fixture",
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

    def _lodging_plan(self) -> dict[str, object]:
        return build_plan(
            trip_id=self.slug,
            generation=1,
            state={
                "trip": {
                    "trip_id": self.slug,
                    "title": "Lodging wrapper fixture",
                    "timezone": "Asia/Taipei",
                    "date_range": "2026-10-12 ~ 2026-10-14",
                },
                "itinerary": {
                    "days": [
                        {
                            "day_id": f"day-{index + 1}",
                            "date": (date(2026, 10, 12) + timedelta(days=index)).isoformat(),
                            "timezone": "Asia/Taipei",
                            "places": [],
                            "travel": [],
                        }
                        for index in range(3)
                    ]
                },
            },
        )

    def _lodging_request(self, store: TripStore) -> LodgingConfirmationRequest:
        plan = store.load_plan()
        stay = LodgingSelectionSegment(
            location_id="phase532-hotel",
            check_in=date(2026, 10, 12),
            check_out=date(2026, 10, 15),
            kind=LodgingKind.HOTEL,
            decision_state=DecisionState.SELECTED,
        )
        return LodgingConfirmationRequest(
            trip_id=self.slug,
            base_revision=plan["revision"],
            idempotency_key="test-key",
            segments=(stay,),
            anchors=(
                LodgingSelectionAnchor("day-1", end_lodging_id=stay.lodging_id),
                LodgingSelectionAnchor(
                    "day-2", stay.lodging_id, stay.lodging_id
                ),
                LodgingSelectionAnchor(
                    "day-3", stay.lodging_id, stay.lodging_id
                ),
            ),
            evaluation_at=EVALUATION_AT,
        )


if __name__ == "__main__":
    unittest.main()
